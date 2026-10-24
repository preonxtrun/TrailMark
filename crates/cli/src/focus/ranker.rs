//! Lead ranking (port of `focus/ranker.py`): (IndexData, cache, issue) → top-k
//! file leads.
//!
//! Pure, deterministic, zero-LLM, zero-network scoring. Per-file hybrid score:
//!
//! - `0.45 * lexical`   FTS5 BM25 over symbols (`cache.search`); best (lowest)
//!   bm25 hit per file, inverted (`raw = max(0.0, -score)`) and min-max
//!   normalized across candidate files (constant 0.5 when degenerate).
//! - `0.25 * graph`     personalized PageRank seeded from the BM25-top-20 files,
//!   seed weight proportional to inverted BM25 rank (`1/(rank+1)`, normalized
//!   to sum 1), min-max normalized over candidates.
//! - `0.15 * test-prox` issue tokens matching test-file symbol names / paths
//!   (files whose path contains `test`), propagated one hop to the src files
//!   they import via `data.refs`, min-max normalized over candidates.
//! - `0.15 * prior`     plain PageRank over all files, min-max normalized.
//!
//! Deterministic tie-break: `(-score, path, best-symbol name)`.

use std::cmp::Reverse;
use std::collections::HashMap;

use codegraph_core::cache::{Hit, IndexCache};
use codegraph_core::{pagerank, FileEntry, IndexData, Symbol};

/// Hybrid component weights (sum to 1.0).
pub const W_LEXICAL: f64 = 0.45;
pub const W_GRAPH: f64 = 0.25;
pub const W_TEST: f64 = 0.15;
pub const W_PRIOR: f64 = 0.15;

/// BM25 hits pulled from the cache.
const LEXICAL_K: usize = 64;
/// Personalized-PageRank seed count (BM25-top-N files).
const SEED_LIMIT: usize = 20;
/// Hard cap for the terse `why` phrase, in characters.
const WHY_MAX: usize = 90;
/// Min-max value when all candidates are equal / single.
const DEGENERATE: f64 = 0.5;
const EPS: f64 = 1e-12;

/// Function words only — they add BM25 noise without topic signal.
const STOPWORDS: &[&str] = &[
    "a", "all", "also", "an", "and", "any", "are", "as", "at", "be", "but", "by", "can", "could",
    "did", "do", "does", "each", "for", "from", "had", "has", "have", "how", "if", "in", "into",
    "is", "it", "its", "may", "might", "must", "not", "of", "on", "or", "our", "per", "shall",
    "should", "so", "some", "than", "that", "the", "their", "then", "there", "this", "to", "us",
    "via", "was", "we", "were", "what", "when", "which", "why", "will", "with", "would", "you",
    "your",
];

/// One ranked entry of the PLAN section: a file plus its most relevant symbol.
#[derive(Clone, Debug)]
pub struct Lead {
    /// Repo-relative posix path.
    pub path: String,
    /// Anchor symbol start line (1-based).
    pub start_line: u32,
    /// Anchor symbol end line (inclusive).
    pub end_line: u32,
    /// Terse role description, <= 90 chars.
    pub why: String,
    /// Hybrid score in [0, 1].
    pub score: f64,
}

/// Distinct lowercase alphanumeric tokens of `issue` (stopwords / len<3 dropped).
pub fn issue_tokens(issue: &str) -> Vec<String> {
    let blob: String = issue
        .to_lowercase()
        .chars()
        .map(|c| if c.is_alphanumeric() { c } else { ' ' })
        .collect();
    let mut tokens: Vec<String> = Vec::new();
    let mut seen: std::collections::HashSet<String> = std::collections::HashSet::new();
    for tok in blob.split_whitespace() {
        if tok.chars().count() < 3 || STOPWORDS.contains(&tok) || !seen.insert(tok.to_string()) {
            continue;
        }
        tokens.push(tok.to_string());
    }
    tokens
}

/// `needle in hay.to_lowercase()` without the allocation (ASCII paths).
fn contains_ignore_case(hay: &str, needle: &str) -> bool {
    let h = hay.as_bytes();
    let n = needle.as_bytes();
    if n.is_empty() || h.len() < n.len() {
        return false;
    }
    (0..=h.len() - n.len()).any(|i| {
        h[i..i + n.len()]
            .iter()
            .zip(n)
            .all(|(x, y)| x.eq_ignore_ascii_case(y))
    })
}

/// Min-max normalize `values` into [0, 1]; `degenerate` when hi == lo (or single).
fn minmax(values: &HashMap<String, f64>) -> HashMap<String, f64> {
    if values.is_empty() {
        return HashMap::new();
    }
    let lo = values.values().cloned().fold(f64::INFINITY, f64::min);
    let hi = values.values().cloned().fold(f64::NEG_INFINITY, f64::max);
    if hi - lo <= EPS {
        return values.keys().map(|k| (k.clone(), DEGENERATE)).collect();
    }
    values
        .iter()
        .map(|(k, v)| (k.clone(), (v - lo) / (hi - lo)))
        .collect()
}

/// Best (lowest-bm25) symbol hit per file, or {} when nothing matches.
fn lexical(cache: &IndexCache, tokens: &[String]) -> HashMap<String, Hit> {
    if tokens.is_empty() {
        return HashMap::new();
    }
    let hits = match cache.search(&tokens.join(" "), LEXICAL_K) {
        Ok(h) => h,
        Err(_) => return HashMap::new(),
    };
    let mut sorted: Vec<&Hit> = hits.iter().collect();
    sorted.sort_by(|a, b| {
        (a.score, a.file.as_str(), a.name.as_str(), a.line)
            .partial_cmp(&(b.score, b.file.as_str(), b.name.as_str(), b.line))
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    let mut best: HashMap<String, Hit> = HashMap::new();
    for hit in sorted {
        best.entry(hit.file.clone()).or_insert_with(|| hit.clone());
    }
    best
}

/// Raw test-proximity signal: matched-token counts for test files, propagated
/// one hop to the src files they import via `data.refs`.
fn test_proximity(data: &IndexData, tokens: &[String]) -> HashMap<String, f64> {
    if tokens.is_empty() {
        return HashMap::new();
    }
    let mut test_paths: Vec<&String> = data
        .files
        .keys()
        .filter(|p| contains_ignore_case(p, "test"))
        .collect();
    test_paths.sort();
    if test_paths.is_empty() {
        return HashMap::new();
    }
    let mut raw: HashMap<String, f64> = HashMap::new();
    for path in test_paths {
        let entry = &data.files[path];
        let mut haystack = path.to_lowercase();
        for s in &entry.symbols {
            haystack.push(' ');
            haystack.push_str(&s.name.to_lowercase());
        }
        let matched = tokens
            .iter()
            .filter(|t| haystack.contains(t.as_str()))
            .count();
        if matched > 0 {
            raw.insert(path.clone(), matched as f64);
        }
    }
    let mut signal = raw.clone();
    for r in &data.refs {
        if let Some(src_score) = raw.get(&r.src_file) {
            if !r.dst_file.is_empty() && data.files.contains_key(&r.dst_file) {
                let prev = signal.get(&r.dst_file).copied().unwrap_or(0.0);
                signal.insert(r.dst_file.clone(), prev.max(*src_score));
            }
        }
    }
    signal
}

/// Innermost class symbol in the same file whose range contains `sym`, if any.
fn enclosing_class(data: &IndexData, sym: &Symbol) -> String {
    let Some(entry) = data.files.get(&sym.file) else {
        return String::new();
    };
    let mut classes: Vec<&Symbol> = entry
        .symbols
        .iter()
        .filter(|c| {
            c.kind == "class"
                && !(c.line == sym.line && c.end_line == sym.end_line && c.name == sym.name)
                && c.line <= sym.line
                && sym.line <= c.end_line
        })
        .collect();
    if classes.is_empty() {
        return String::new();
    }
    // smallest span first: (end_line - line) ascending, then (line, name)
    classes.sort_by_key(|c| (c.end_line - c.line, c.line, c.name.clone()));
    classes[0].name.clone()
}

/// Terse role phrase from the symbol's doc/signature plus its context, <= 90 chars.
fn why_text(data: &IndexData, sym: &Symbol) -> String {
    let enclosing = enclosing_class(data, sym);
    let context = if enclosing.is_empty() {
        sym.name.clone()
    } else {
        format!("{enclosing}.{}", sym.name)
    };
    let role = if !sym.doc.is_empty() {
        sym.doc
            .split_whitespace()
            .collect::<Vec<_>>()
            .join(" ")
            .trim_end_matches('.')
            .to_string()
    } else {
        String::new()
    };
    let role = if role.is_empty() {
        let mut sig = sym
            .signature
            .split_whitespace()
            .collect::<Vec<_>>()
            .join(" ");
        for prefix in ["async def ", "def ", "class "] {
            if let Some(rest) = sig.strip_prefix(prefix) {
                sig = rest.to_string();
                break;
            }
        }
        let sig = sig.trim().to_string();
        if sig.is_empty() {
            format!("{} {}", sym.kind, sym.name)
        } else {
            sig
        }
    } else {
        role
    };
    let mut why = format!("{role} ({context})");
    if why.chars().count() > WHY_MAX {
        let head: String = why.chars().take(WHY_MAX - 3).collect();
        why = format!("{head}...");
    }
    why
}

/// The lead symbol for `path`: the BM25-best hit's symbol, else best token
/// overlap, else the earliest symbol. Ties break by earliest line, then name.
fn best_symbol(
    data: &IndexData,
    path: &str,
    hit: Option<&Hit>,
    tokens: &[String],
) -> Option<Symbol> {
    let entry: &FileEntry = data.files.get(path)?;
    let mut syms: Vec<Symbol> = entry.symbols.clone();
    syms.sort_by(|a, b| (a.line, a.name.as_str()).cmp(&(b.line, b.name.as_str())));
    if syms.is_empty() {
        return None;
    }
    if let Some(hit) = hit {
        if let Some(s) = syms.iter().find(|s| s.name == hit.name) {
            return Some(s.clone());
        }
    }
    let overlap = |sym: &Symbol| -> usize {
        tokens
            .iter()
            .filter(|t| sym.name.to_lowercase().contains(t.as_str()))
            .count()
    };
    syms.into_iter().min_by(|a, b| {
        (Reverse(overlap(a)), a.line, a.name.as_str()).cmp(&(
            Reverse(overlap(b)),
            b.line,
            b.name.as_str(),
        ))
    })
}

/// Rank candidate files for `issue` and return at most `k` leads.
///
/// Deterministic: same inputs yield byte-identical leads. Scores are clamped to
/// [0, 1]. Files without symbols are skipped as leads (no line anchor possible)
/// but still participate in normalization.
pub fn rank(data: &IndexData, cache: &IndexCache, issue: &str, k: usize) -> Vec<Lead> {
    rank_and_prior(data, cache, issue, k).0
}

/// [`rank`] plus the plain (unpersonalized) pagerank prior it computed — the
/// focus path reuses those scores for the atlas block instead of running the
/// same power iteration again. Values are identical to
/// `codegraph_core::pagerank(data, 0.85, 30, None)`.
pub fn rank_and_prior(
    data: &IndexData,
    cache: &IndexCache,
    issue: &str,
    k: usize,
) -> (Vec<Lead>, HashMap<String, f64>) {
    if k == 0 || data.files.is_empty() {
        return (vec![], HashMap::new());
    }
    let tokens = issue_tokens(issue);

    let lex_raw_hits = lexical(cache, &tokens);
    let best_by_file: HashMap<String, Hit> = lex_raw_hits
        .into_iter()
        .filter(|(f, _)| data.files.contains_key(f))
        .collect();
    let test_signal = test_proximity(data, &tokens);

    let mut candidates: Vec<String> = best_by_file
        .keys()
        .cloned()
        .chain(test_signal.keys().cloned())
        .collect::<std::collections::HashSet<_>>()
        .into_iter()
        .collect();
    candidates.sort();

    let mut personalization: Option<HashMap<String, f64>> = None;
    if candidates.is_empty() {
        // No lexical and no test signal (vague issue): fall back to PageRank top-k.
        let prior0 = pagerank(data, 0.85, 30, None);
        let mut all: Vec<&String> = data.files.keys().collect();
        all.sort_by(|a, b| {
            prior0
                .get(*a)
                .copied()
                .unwrap_or(0.0)
                .partial_cmp(&prior0.get(*b).copied().unwrap_or(0.0))
                .unwrap_or(std::cmp::Ordering::Equal)
                .reverse()
                .then(a.cmp(b))
        });
        candidates = all.into_iter().take(k).cloned().collect();
    } else {
        // Personalized PageRank seeded from the BM25-top-20 files.
        let mut lex_order: Vec<&String> = best_by_file.keys().collect();
        lex_order.sort_by(|a, b| {
            let sa = best_by_file[*a].score;
            let sb = best_by_file[*b].score;
            sa.partial_cmp(&sb)
                .unwrap_or(std::cmp::Ordering::Equal)
                .then(a.cmp(b))
        });
        let seeds: Vec<&String> = lex_order.into_iter().take(SEED_LIMIT).collect();
        if !seeds.is_empty() {
            let mut raw: HashMap<String, f64> = HashMap::new();
            for (i, f) in seeds.iter().enumerate() {
                raw.insert((*f).clone(), 1.0 / (i as f64 + 1.0));
            }
            let z: f64 = raw.values().sum();
            let pers: HashMap<String, f64> = data
                .files
                .keys()
                .map(|p| (p.clone(), raw.get(p).copied().unwrap_or(0.0) / z))
                .collect();
            personalization = Some(pers);
        }
    }

    let mut lex_raw: HashMap<String, f64> = HashMap::new();
    for f in best_by_file.keys() {
        lex_raw.insert(f.clone(), 0.0f64.max(-best_by_file[f].score));
    }
    let lex_norm = minmax(&lex_raw);

    // one shared edge build powers both the personalized and the plain run
    let (graph, prior) =
        codegraph_core::graph::pagerank_pair(data, 0.85, 30, personalization.as_ref(), None);
    let prior_out = prior.clone();

    let graph_norm = minmax(
        &candidates
            .iter()
            .map(|f| (f.clone(), graph.get(f).copied().unwrap_or(0.0)))
            .collect(),
    );
    let prior_norm = minmax(
        &candidates
            .iter()
            .map(|f| (f.clone(), prior.get(f).copied().unwrap_or(0.0)))
            .collect(),
    );
    let test_norm = minmax(
        &candidates
            .iter()
            .map(|f| (f.clone(), test_signal.get(f).copied().unwrap_or(0.0)))
            .collect(),
    );

    let mut scored: Vec<(String, f64)> = candidates
        .iter()
        .map(|path| {
            let score = W_LEXICAL * lex_norm.get(path).copied().unwrap_or(0.0)
                + W_GRAPH * graph_norm.get(path).copied().unwrap_or(0.0)
                + W_TEST * test_norm.get(path).copied().unwrap_or(0.0)
                + W_PRIOR * prior_norm.get(path).copied().unwrap_or(0.0);
            (path.clone(), score.clamp(0.0, 1.0))
        })
        .collect();
    scored.sort_by(|a, b| {
        b.1.partial_cmp(&a.1)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then(a.0.cmp(&b.0))
    });

    let mut leads: Vec<Lead> = Vec::new();
    for (path, score) in scored {
        if leads.len() >= k {
            break;
        }
        let Some(sym) = best_symbol(data, &path, best_by_file.get(&path), &tokens) else {
            continue;
        };
        leads.push(Lead {
            path,
            start_line: sym.line,
            end_line: sym.end_line,
            why: why_text(data, &sym),
            score,
        });
    }
    (leads, prior_out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::atlas::testgraph::is_test_file;

    #[test]
    fn issue_tokens_drop_stopwords_and_short() {
        let tokens = issue_tokens("Fix the session refresh bug in SessionStore");
        assert!(tokens.contains(&"fix".to_string()));
        assert!(tokens.contains(&"session".to_string()));
        assert!(tokens.contains(&"refresh".to_string()));
        assert!(tokens.contains(&"bug".to_string()));
        assert!(!tokens.contains(&"the".to_string()));
        assert_eq!(
            tokens.len(),
            tokens
                .iter()
                .collect::<std::collections::HashSet<_>>()
                .len()
        );
    }

    #[test]
    fn minmax_normalizes_and_handles_degenerate() {
        let mut v = HashMap::new();
        v.insert("a".to_string(), 2.0);
        v.insert("b".to_string(), 4.0);
        let n = minmax(&v);
        assert_eq!(n["a"], 0.0);
        assert_eq!(n["b"], 1.0);
        let mut same = HashMap::new();
        same.insert("a".to_string(), 3.0);
        same.insert("b".to_string(), 3.0);
        let n = minmax(&same);
        assert_eq!(n["a"], DEGENERATE);
        assert!(minmax(&HashMap::new()).is_empty());
    }

    #[test]
    fn why_text_caps_at_90_chars() {
        let mut files = std::collections::BTreeMap::new();
        files.insert(
            "a.py".to_string(),
            FileEntry {
                path: "a.py".into(),
                lang: "py".into(),
                mtime: 1.0,
                size: 1,
                symbols: vec![Symbol {
                    file: "a.py".into(),
                    name: "huge_function_name".into(),
                    kind: "def".into(),
                    line: 1,
                    end_line: 2,
                    signature: "def huge_function_name(x, y, z)".into(),
                    doc: "x".repeat(200),
                }],
            },
        );
        let data = IndexData {
            root: "/r".into(),
            files,
            refs: vec![],
        };
        let sym = data.files["a.py"].symbols[0].clone();
        let why = why_text(&data, &sym);
        assert!(why.chars().count() <= WHY_MAX, "{}", why.len());
        assert!(why.ends_with("..."));
    }

    #[test]
    fn enclosing_class_prefers_smallest_span() {
        let mut files = std::collections::BTreeMap::new();
        files.insert(
            "a.py".into(),
            FileEntry {
                path: "a.py".into(),
                lang: "py".into(),
                mtime: 1.0,
                size: 1,
                symbols: vec![
                    Symbol {
                        file: "a.py".into(),
                        name: "Outer".into(),
                        kind: "class".into(),
                        line: 1,
                        end_line: 50,
                        ..Default::default()
                    },
                    Symbol {
                        file: "a.py".into(),
                        name: "Inner".into(),
                        kind: "class".into(),
                        line: 10,
                        end_line: 20,
                        ..Default::default()
                    },
                    Symbol {
                        file: "a.py".into(),
                        name: "method".into(),
                        kind: "method".into(),
                        line: 12,
                        end_line: 14,
                        ..Default::default()
                    },
                ],
            },
        );
        let data = IndexData {
            root: "/r".into(),
            files,
            refs: vec![],
        };
        let sym = data.files["a.py"].symbols[2].clone();
        assert_eq!(enclosing_class(&data, &sym), "Inner");
        assert!(!is_test_file("a.py"));
    }
}
