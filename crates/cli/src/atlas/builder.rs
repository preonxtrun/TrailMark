//! MAP section builder (port of `atlas/builder.py`): directory roles +
//! pagerank-ordered hot files with line anchors, and the PLAN leads builder.
//!
//! Row shapes (SPEC block format):
//!
//! * first row of a top-level directory carries the dir role:
//!   `- <dir>/ — <role>; hot: <path>:<line> <Sym()> — <desc>`
//! * further rows are plain hot-file rows: `- <path>:<line> <Sym()> — <desc>`

use std::collections::{BTreeMap, HashMap, HashSet};
use std::path::Path;

use codegraph_core::{byte_len, pagerank, truncate, FileEntry, IndexData, Symbol};

use super::glossary::STOPWORDS;
use super::testgraph::is_test_file;

/// Display caps (bytes) and row bounds.
pub const ROW_CAP: usize = 84;
/// Directory role display cap (bytes).
pub const ROLE_CAP: usize = 36;
/// Description display cap (bytes).
pub const DESC_CAP: usize = 28;
/// Maximum MAP rows.
pub const MAX_ROWS: usize = 10;
/// Minimum MAP rows (SPEC floor).
pub const MIN_ROWS: usize = 3;

/// Directory-name → role table used when no README/package docstring exists.
fn role_name(directory: &str) -> &'static str {
    match directory {
        "api" => "API layer",
        "atlas" => "atlas section builders",
        "bench" => "benchmarks",
        "cli" => "CLI entrypoints",
        "core" => "core logic",
        "docs" => "documentation",
        "emit" => "output emitters",
        "focus" => "focus planning",
        "index" => "code indexer",
        "lib" => "library code",
        "models" => "data models",
        "scripts" => "scripts & tooling",
        "src" => "source",
        "test" | "tests" => "test suite",
        "tools" => "scripts & tooling",
        "util" | "utils" => "utilities",
        _ => "code",
    }
}

/// File-level pagerank scores for ordering only (never emitted).
pub fn pagerank_scores(data: &IndexData) -> Option<HashMap<String, f64>> {
    let raw = pagerank(data, 0.85, 30, None);
    Some(
        data.files
            .keys()
            .map(|p| (p.clone(), raw.get(p).copied().unwrap_or(0.0)))
            .collect(),
    )
}

/// Top-level directory → inferred role text.
///
/// Heuristic priority per directory: README first meaningful line, then the
/// package `__init__.py` docstring first line, then a name table, then
/// `"code"`. Root-level files group under `""` and use the repo root README.
pub fn dir_roles(data: &IndexData) -> HashMap<String, String> {
    let mut dirs: Vec<&str> = data
        .files
        .keys()
        .map(|p| match p.split_once('/') {
            Some((top, _)) => top,
            None => "",
        })
        .collect();
    dirs.sort();
    dirs.dedup();
    dirs.into_iter()
        .map(|d| (d.to_string(), role_for(Path::new(&data.root), d)))
        .collect()
}

fn role_for(root: &Path, directory: &str) -> String {
    let base = if directory.is_empty() {
        root.to_path_buf()
    } else {
        root.join(directory)
    };
    for name in ["README.md", "README.rst", "README.txt", "readme.md"] {
        let line = first_readme_line(&base.join(name));
        if !line.is_empty() {
            return truncate(&line, ROLE_CAP);
        }
    }
    if !directory.is_empty() {
        let line = docstring_line(&base.join("__init__.py"));
        if !line.is_empty() {
            return truncate(&line, ROLE_CAP);
        }
    }
    role_name(directory).to_string()
}

fn read_head(path: &Path, max_chars: usize) -> Option<String> {
    let bytes = std::fs::read(path).ok()?;
    let text = String::from_utf8_lossy(&bytes).into_owned();
    Some(text.chars().take(max_chars).collect())
}

fn first_readme_line(path: &Path) -> String {
    let Some(head) = read_head(path, 4096) else {
        return String::new();
    };
    for raw in head.lines() {
        let line = raw.trim();
        if line.is_empty() || line.starts_with("<!--") || readme_skip(line) {
            continue;
        }
        let line = strip_md_prefix(line).trim().to_string();
        let line = line
            .trim_matches(['*', '_', '`', '#'])
            .trim_end_matches([':', ';', ',', '.'])
            .trim()
            .to_string();
        if line.chars().count() >= 3 {
            return line;
        }
    }
    String::new()
}

/// `_README_SKIP`: lines starting with GitHub alerts, HTML, tables, rules.
fn readme_skip(line: &str) -> bool {
    line.starts_with("[!")
        || line.starts_with('<')
        || line.starts_with('|')
        || line.starts_with("===")
        || line.starts_with("---")
}

/// `^\s*#+\s*` — strip a markdown heading prefix.
fn strip_md_prefix(line: &str) -> &str {
    let hashes = line.bytes().take_while(|&b| b == b'#').count();
    if hashes == 0 {
        return line;
    }
    let rest = &line[hashes..];
    rest.trim_start()
}

/// `("""|''')\s*\n?\s*(.+)` with DOTALL — module docstring first line.
fn docstring_line(path: &Path) -> String {
    let Some(head) = read_head(path, 2048) else {
        return String::new();
    };
    let chars: Vec<char> = head.chars().collect();
    let Some(pos) = (0..chars.len().saturating_sub(2)).find(|&i| {
        (chars[i] == '"' && chars[i + 1] == '"' && chars[i + 2] == '"')
            || (chars[i] == '\'' && chars[i + 1] == '\'' && chars[i + 2] == '\'')
    }) else {
        return String::new();
    };
    // skip all whitespace after the opening quotes
    let mut j = pos + 3;
    while j < chars.len() && chars[j].is_whitespace() {
        j += 1;
    }
    if j >= chars.len() {
        return String::new();
    }
    let mut line = String::new();
    while j < chars.len() && chars[j] != '\n' {
        line.push(chars[j]);
        j += 1;
    }
    let line = line.trim().trim_matches(['"', '\'']).trim().to_string();
    if line.chars().count() >= 3 {
        line
    } else {
        String::new()
    }
}

/// Incoming resolved-reference counts per (dst_file, dst_name).
pub fn ref_counts(data: &IndexData) -> HashMap<(String, String), usize> {
    ref_counts_ref(data)
        .into_iter()
        .map(|((f, n), c)| ((f.to_string(), n.to_string()), c))
        .collect()
}

/// Borrowed-key variant of [`ref_counts`] (no per-ref String clones).
fn ref_counts_ref(data: &IndexData) -> FnvMap<(&str, &str), usize> {
    let mut counts: FnvMap<(&str, &str), usize> = FnvMap::default();
    for r in &data.refs {
        if !r.dst_file.is_empty() {
            *counts
                .entry((r.dst_file.as_str(), r.dst_name.as_str()))
                .or_insert(0) += 1;
        }
    }
    counts
}

/// Fast FNV-1a builder (short-string keys, rebuilt per call).
#[derive(Default)]
struct FnvBuild;

impl std::hash::BuildHasher for FnvBuild {
    type Hasher = FnvHasher;
    fn build_hasher(&self) -> FnvHasher {
        FnvHasher(0xcbf2_9ce4_8422_2325)
    }
}

#[derive(Default)]
struct FnvHasher(u64);

impl std::hash::Hasher for FnvHasher {
    fn finish(&self) -> u64 {
        self.0
    }
    fn write(&mut self, bytes: &[u8]) {
        for &b in bytes {
            self.0 ^= u64::from(b);
            self.0 = self.0.wrapping_mul(0x0000_0100_0000_01b3);
        }
    }
}

type FnvMap<K, V> = HashMap<K, V, std::hash::BuildHasherDefault<FnvHasher>>;

/// The file's most-referenced symbol (ties by line, then name).
pub fn anchor_symbol(
    entry: &FileEntry,
    counts: &HashMap<(String, String), usize>,
) -> Option<Symbol> {
    use std::cmp::Reverse;
    entry
        .symbols
        .iter()
        .min_by(|a, b| {
            let ca = counts
                .get(&(entry.path.clone(), a.name.clone()))
                .copied()
                .unwrap_or(0);
            let cb = counts
                .get(&(entry.path.clone(), b.name.clone()))
                .copied()
                .unwrap_or(0);
            (Reverse(ca), a.line, a.name.as_str()).cmp(&(Reverse(cb), b.line, b.name.as_str()))
        })
        .cloned()
}

/// [`anchor_symbol`] over the borrowed-key count map (fast path).
fn anchor_symbol_ref(entry: &FileEntry, counts: &FnvMap<(&str, &str), usize>) -> Option<Symbol> {
    use std::cmp::Reverse;
    entry
        .symbols
        .iter()
        .min_by(|a, b| {
            let ca = counts
                .get(&(entry.path.as_str(), a.name.as_str()))
                .copied()
                .unwrap_or(0);
            let cb = counts
                .get(&(entry.path.as_str(), b.name.as_str()))
                .copied()
                .unwrap_or(0);
            (Reverse(ca), a.line, a.name.as_str()).cmp(&(Reverse(cb), b.line, b.name.as_str()))
        })
        .cloned()
}

/// Display form: `name()` for callables, bare name otherwise.
pub fn sym_label(sym: &Symbol) -> String {
    if sym.name.contains('(') {
        return sym.name.clone();
    }
    if matches!(sym.kind.as_str(), "def" | "method" | "function" | "fn") {
        return format!("{}()", sym.name);
    }
    sym.name.clone()
}

/// 3..10 hot source files, pagerank-ordered (alphabetical fallback).
///
/// Test files are excluded (they belong to TESTS). Files without symbols carry
/// no line anchor, so they only fill up to MIN_ROWS when symbol-bearing files
/// fall short of the floor.
pub fn hot_files(data: &IndexData, pr: Option<&HashMap<String, f64>>) -> Vec<String> {
    let src: Vec<&String> = data.files.keys().filter(|p| !is_test_file(p)).collect();
    let with_syms: Vec<&String> = src
        .iter()
        .copied()
        .filter(|p| !data.files[*p].symbols.is_empty())
        .collect();
    let fillers: Vec<&String> = src
        .iter()
        .copied()
        .filter(|p| data.files[*p].symbols.is_empty())
        .collect();
    let ordered: Vec<&String> = match pr {
        Some(scores) => {
            let mut v = with_syms;
            v.sort_by(|a, b| {
                let sa = scores.get(*a).copied().unwrap_or(0.0);
                let sb = scores.get(*b).copied().unwrap_or(0.0);
                sb.partial_cmp(&sa)
                    .unwrap_or(std::cmp::Ordering::Equal)
                    .then(a.cmp(b))
            });
            v
        }
        None => {
            let mut v = with_syms;
            v.sort();
            v
        }
    };
    let mut hot: Vec<String> = ordered
        .iter()
        .take(MAX_ROWS)
        .map(|p| (*p).clone())
        .collect();
    if hot.len() < MIN_ROWS {
        hot.extend(
            fillers
                .iter()
                .take(MIN_ROWS - hot.len())
                .map(|p| (*p).clone()),
        );
    }
    hot
}

/// MAP rows: first row per top-level dir carries the dir role (SPEC shape).
pub fn build_map(data: &IndexData, pr: Option<&HashMap<String, f64>>) -> Vec<String> {
    let roles = dir_roles(data);
    let counts = ref_counts_ref(data);
    let mut rows: Vec<String> = Vec::new();
    let mut seen: HashSet<String> = HashSet::new();
    for path in hot_files(data, pr) {
        let entry = &data.files[&path];
        let sym = anchor_symbol_ref(entry, &counts);
        let mut base = format!("{}:{}", path, sym.as_ref().map_or(1, |s| s.line));
        if let Some(sym) = &sym {
            base.push(' ');
            base.push_str(&sym_label(sym));
        }
        let first = match path.split_once('/') {
            Some((top, _)) => top.to_string(),
            None => String::new(),
        };
        let mut row;
        if seen.insert(first.clone()) {
            let display = if first.is_empty() {
                "./".to_string()
            } else {
                format!("{first}/")
            };
            row = format!(
                "- {display} — {}; hot: {base}",
                roles.get(&first).map(|s| s.as_str()).unwrap_or("code")
            );
        } else {
            row = format!("- {base}");
        }
        let desc = desc_of(sym.as_ref());
        if !desc.is_empty() {
            row = format!("{row} — {desc}");
        }
        rows.push(cap_row(&row, ROW_CAP));
    }

    // Tiny repos: top up to MIN_ROWS with role-only rows for the remaining dirs,
    // ordered by the best pagerank among their files, then by name.
    if rows.len() < MIN_ROWS {
        let scores = pr.cloned().unwrap_or_default();
        let mut remaining: Vec<&String> = roles.keys().filter(|d| !seen.contains(*d)).collect();
        let best_of = |directory: &str| -> f64 {
            data.files
                .keys()
                .filter(|p| match p.split_once('/') {
                    Some((top, _)) => top == directory,
                    None => directory.is_empty(),
                })
                .map(|p| scores.get(p).copied().unwrap_or(0.0))
                .fold(0.0f64, f64::max)
        };
        remaining.sort_by(|a, b| {
            // Python key: (-max_score, directory) → max_score descending, then name
            let ma = best_of(a);
            let mb = best_of(b);
            mb.partial_cmp(&ma)
                .unwrap_or(std::cmp::Ordering::Equal)
                .then(a.cmp(b))
        });
        for directory in remaining {
            let display = if directory.is_empty() {
                "./".to_string()
            } else {
                format!("{directory}/")
            };
            rows.push(format!(
                "- {display} — {}",
                roles.get(directory).map(|s| s.as_str()).unwrap_or("code")
            ));
            if rows.len() >= MIN_ROWS {
                break;
            }
        }
    }
    rows
}

/// (verbose, compact) PLAN items derived deterministically from the issue text.
///
/// Files are scored by occurrences of the issue's keywords in symbol names,
/// docs, signatures and paths; the top 5 become numbered leads with the anchor
/// symbol's line range. The compact variant is the single top lead (or a "no
/// matches" hint). Lead order is fully determined by (score, path).
pub fn build_plan(
    data: &IndexData,
    issue: &str,
    pr: Option<&HashMap<String, f64>>,
) -> (Vec<String>, Vec<String>) {
    let _ = pr; // accepted for signature symmetry with the other builders
    let terms = issue_terms(issue, 16);
    let mut matched_files: HashMap<String, Vec<String>> = HashMap::new();
    let mut file_scores: HashMap<String, usize> = HashMap::new();
    for (path, entry) in &data.files {
        let mut blob = String::with_capacity(64 + entry.symbols.len() * 32);
        blob.push_str(&path.to_lowercase());
        for s in &entry.symbols {
            blob.push(' ');
            blob.push_str(&s.name.to_lowercase());
            if !s.doc.is_empty() {
                blob.push(' ');
                blob.push_str(&s.doc.to_lowercase());
            }
            if !s.signature.is_empty() {
                blob.push(' ');
                blob.push_str(&s.signature.to_lowercase());
            }
        }
        let mut score = 0usize;
        let mut matched: Vec<String> = Vec::new();
        for term in &terms {
            let hits = blob.matches(term).count();
            if hits > 0 {
                matched.push(term.clone());
                score += hits * 2 + usize::from(path.to_lowercase().contains(term));
            }
        }
        if score > 0 {
            matched_files.insert(path.clone(), matched);
            file_scores.insert(path.clone(), score);
        }
    }

    let counts = ref_counts_ref(data);
    let mut leads: Vec<String> = Vec::new();
    let mut top_ref = String::new();
    let mut ranked: Vec<&String> = file_scores.keys().collect();
    ranked.sort_by(|a, b| file_scores[*b].cmp(&file_scores[*a]).then(a.cmp(b)));
    for (rank, path) in ranked.iter().enumerate() {
        if leads.len() >= 5 {
            break;
        }
        let sym = anchor_symbol_ref(&data.files[*path], &counts);
        let line_range = match &sym {
            Some(s) => format!("{}-{}", s.line, s.end_line),
            None => "1-1".to_string(),
        };
        let matched = &matched_files[*path];
        let joined = if matched.is_empty() {
            "path match".to_string()
        } else {
            matched
                .iter()
                .take(3)
                .cloned()
                .collect::<Vec<_>>()
                .join(", ")
        };
        let why = truncate(&joined, DESC_CAP * 2);
        leads.push(format!(
            "{}. {path}:{line_range} — matches: {why}",
            rank + 1
        ));
        if top_ref.is_empty() {
            top_ref = (*path).clone();
        }
    }

    if leads.is_empty() {
        return (
            vec![
                "LEADS (verify, don't trust):".to_string(),
                "- (no lexical matches in index — start from MAP hot files)".to_string(),
            ],
            vec!["(no lexical matches — start from MAP hot files)".to_string()],
        );
    }

    let mut verbose = vec!["LEADS (verify, don't trust):".to_string()];
    verbose.extend(leads.iter().cloned());
    verbose.push(format!("OUTLINES: .codegraph/outlines/{top_ref}.md"));
    let compact = vec![leads[0].clone()];
    (verbose, compact)
}

/// Deterministic keyword list from the issue text: by frequency, then term.
///
/// Prose and identifier stopwords are removed; terms shorter than 4 chars are
/// ignored; at most `limit` terms are kept. The only task input ever used is
/// this issue text (invariant #3).
pub fn issue_terms(issue: &str, limit: usize) -> Vec<String> {
    let mut freq: HashMap<String, usize> = HashMap::new();
    for raw in issue_runs(issue) {
        let word = raw.trim_matches(['_', '-']);
        if word.chars().count() < 4 || PROSE_STOP.contains(&word) || STOPWORDS.contains(&word) {
            continue;
        }
        *freq.entry(word.to_string()).or_insert(0) += 1;
    }
    let mut ranked: Vec<(usize, String)> = freq.into_iter().map(|(k, v)| (v, k)).collect();
    ranked.sort_by(|a, b| b.0.cmp(&a.0).then(a.1.cmp(&b.1)));
    ranked.truncate(limit);
    ranked.into_iter().map(|(_, w)| w).collect()
}

/// `re.findall(r"[a-z0-9_]{4,}", issue.lower())` — runs of 4+ keyword chars.
fn issue_runs(issue: &str) -> Vec<String> {
    let lower = issue.to_lowercase();
    let mut runs = Vec::new();
    let mut cur = String::new();
    for c in lower.chars() {
        if c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_' {
            cur.push(c);
        } else if !cur.is_empty() {
            if cur.chars().count() >= 4 {
                runs.push(std::mem::take(&mut cur));
            } else {
                cur.clear();
            }
        }
    }
    if cur.chars().count() >= 4 {
        runs.push(cur);
    }
    runs
}

/// Prose stopwords for issue-term extraction (copied from `atlas/builder.py`).
pub const PROSE_STOP: &[&str] = &[
    "about",
    "after",
    "also",
    "any",
    "been",
    "before",
    "being",
    "both",
    "could",
    "does",
    "done",
    "during",
    "each",
    "else",
    "every",
    "from",
    "have",
    "here",
    "into",
    "just",
    "like",
    "more",
    "most",
    "must",
    "need",
    "needs",
    "only",
    "other",
    "over",
    "should",
    "since",
    "some",
    "such",
    "sure",
    "than",
    "that",
    "their",
    "them",
    "then",
    "there",
    "these",
    "they",
    "this",
    "those",
    "under",
    "want",
    "well",
    "were",
    "what",
    "when",
    "where",
    "which",
    "while",
    "will",
    "with",
    "without",
    "would",
    "your",
    "yours",
    "able",
    "across",
    "actually",
    "allow",
    "allows",
    "always",
    "another",
    "anything",
    "apply",
    "based",
    "because",
    "become",
    "begin",
    "below",
    "between",
    "bring",
    "build",
    "built",
    "call",
    "come",
    "common",
    "crashes",
    "create",
    "created",
    "disable",
    "down",
    "due",
    "either",
    "error",
    "errors",
    "ensure",
    "ensuring",
    "even",
    "ever",
    "every",
    "expected",
    "fails",
    "failure",
    "failed",
    "false",
    "file",
    "files",
    "find",
    "fine",
    "first",
    "fix",
    "fixed",
    "following",
    "force",
    "form",
    "found",
    "further",
    "get",
    "give",
    "given",
    "goes",
    "gone",
    "good",
    "got",
    "great",
    "happen",
    "happens",
    "hard",
    "have",
    "having",
    "help",
    "here",
    "hold",
    "hook",
    "hope",
    "however",
    "idea",
    "implement",
    "initial",
    "inside",
    "instead",
    "issue",
    "issues",
    "itself",
    "keep",
    "kept",
    "kind",
    "know",
    "known",
    "last",
    "late",
    "later",
    "least",
    "left",
    "less",
    "let",
    "like",
    "likely",
    "line",
    "lines",
    "little",
    "long",
    "look",
    "lot",
    "made",
    "main",
    "make",
    "makes",
    "many",
    "maybe",
    "mean",
    "means",
    "might",
    "mine",
    "more",
    "move",
    "much",
    "must",
    "name",
    "near",
    "need",
    "never",
    "next",
    "none",
    "note",
    "nothing",
    "now",
    "null",
    "occur",
    "once",
    "ones",
    "open",
    "order",
    "other",
    "others",
    "out",
    "output",
    "over",
    "own",
    "pass",
    "passed",
    "passing",
    "patch",
    "place",
    "please",
    "point",
    "possible",
    "present",
    "previously",
    "prior",
    "probably",
    "problem",
    "provide",
    "put",
    "reads",
    "real",
    "really",
    "reason",
    "related",
    "remain",
    "remove",
    "repeat",
    "replaced",
    "report",
    "required",
    "result",
    "right",
    "runs",
    "running",
    "same",
    "scenario",
    "seen",
    "seems",
    "set",
    "several",
    "shown",
    "side",
    "similar",
    "since",
    "small",
    "some",
    "something",
    "sometimes",
    "soon",
    "specific",
    "started",
    "still",
    "stop",
    "take",
    "taken",
    "test",
    "tests",
    "than",
    "thats",
    "thing",
    "things",
    "think",
    "third",
    "three",
    "through",
    "time",
    "times",
    "today",
    "together",
    "too",
    "took",
    "toward",
    "true",
    "try",
    "trying",
    "turn",
    "twice",
    "type",
    "typical",
    "under",
    "until",
    "upon",
    "usage",
    "use",
    "used",
    "uses",
    "using",
    "usually",
    "various",
    "very",
    "wait",
    "want",
    "wants",
    "was",
    "way",
    "ways",
    "well",
    "went",
    "what",
    "whats",
    "when",
    "where",
    "whether",
    "which",
    "while",
    "whole",
    "whose",
    "work",
    "works",
    "would",
    "yet",
    "you",
    "your",
];

/// Row description: doc line, else collapsed signature (byte-capped).
fn desc_of(sym: Option<&Symbol>) -> String {
    let Some(sym) = sym else {
        return String::new();
    };
    let text = if !sym.doc.is_empty() {
        sym.doc.split_whitespace().collect::<Vec<_>>().join(" ")
    } else {
        sym.signature
            .split_whitespace()
            .collect::<Vec<_>>()
            .join(" ")
    };
    if text.is_empty() {
        String::new()
    } else {
        truncate(&text, DESC_CAP)
    }
}

/// Clamp a MAP row to `cap` bytes: drop the description first, then ellipsize.
///
/// This is format-time capping (identical regardless of budget), never a
/// budget-time mid-entry truncation — the row is always a well-formed entry.
fn cap_row(row: &str, cap: usize) -> String {
    if byte_len(row) <= cap {
        return row.to_string();
    }
    // rpartition(" — ")
    if let Some(idx) = row.rfind(" — ") {
        let base = &row[..idx];
        let desc = &row[idx + " — ".len()..];
        if byte_len(base) <= cap {
            return base.to_string();
        }
        let room = cap.saturating_sub(byte_len(base) + " — ".len());
        if room > 0 {
            return format!("{base} — {}", truncate(desc, room));
        }
    }
    truncate(row, cap)
}

/// MAP row cap (re-exported for tests).
pub fn cap_map_row(row: &str) -> String {
    cap_row(row, ROW_CAP)
}

/// `BTreeMap` type alias used in tests.
#[allow(dead_code)]
type PathMap = BTreeMap<String, FileEntry>;

#[cfg(test)]
mod tests {
    use super::*;
    use codegraph_core::Ref;

    fn data(files: &[&str], refs: &[(u32, u32)]) -> IndexData {
        let mut map = BTreeMap::new();
        for (i, f) in files.iter().enumerate() {
            let kind = if f.ends_with(".py") { "py" } else { "ts" };
            map.insert(
                f.to_string(),
                FileEntry {
                    path: f.to_string(),
                    lang: kind.to_string(),
                    mtime: 1.0,
                    size: 10,
                    symbols: vec![Symbol {
                        file: f.to_string(),
                        name: format!("symbol_{i}"),
                        kind: "def".into(),
                        line: 1,
                        end_line: 2,
                        signature: format!("def symbol_{i}(x)"),
                        doc: String::new(),
                    }],
                },
            );
        }
        IndexData {
            root: "/r".into(),
            files: map,
            refs: refs
                .iter()
                .map(|(s, d)| Ref {
                    src_file: files[*s as usize].to_string(),
                    dst_name: format!("fn{}", d),
                    dst_file: files[*d as usize].to_string(),
                    line: 1,
                })
                .collect(),
        }
    }

    #[test]
    fn map_rows_cap_at_84_bytes_and_drop_desc_first() {
        let long_desc = "x".repeat(200);
        let row = format!("- src/a.py:1 fn0() — {long_desc}");
        let capped = cap_map_row(&row);
        assert!(byte_len(&capped) <= ROW_CAP);
        assert!(capped.starts_with("- src/a.py:1 fn0()"));
        assert!(!capped.contains(&long_desc));
    }

    #[test]
    fn issue_terms_skip_prose_and_short_words() {
        let terms = issue_terms(
            "Fix the session token refresh handler: refresh must handle expiry, not tests",
            16,
        );
        assert!(terms.contains(&"refresh".to_string()));
        assert!(terms.contains(&"token".to_string()));
        assert!(terms.contains(&"expiry".to_string()));
        assert!(!terms.contains(&"must".to_string()), "prose stopword");
        assert!(!terms.contains(&"test".to_string()), "short word");
        assert!(!terms.contains(&"tests".to_string()), "prose stopword");
        assert!(is_test_file("tests/test_a.py"));
    }

    #[test]
    fn plan_leads_rank_by_score_then_path() {
        let d = data(&["app/a.py", "app/b.py"], &[(0, 1)]);
        let (verbose, compact) = build_plan(&d, "symbol_0 symbol_1", None);
        assert!(verbose[0] == "LEADS (verify, don't trust):");
        assert!(
            verbose[1].starts_with("1. app/a.py:1-2 — matches: symbol_0"),
            "{verbose:?}"
        );
        assert_eq!(compact, vec![verbose[1].clone()]);
        assert!(verbose
            .last()
            .unwrap()
            .starts_with("OUTLINES: .codegraph/outlines/app/a.py.md"));
    }

    #[test]
    fn plan_no_matches_falls_back_to_hint() {
        let d = data(&["app/a.py"], &[]);
        let (verbose, compact) = build_plan(&d, "zzzz qqqq", None);
        assert_eq!(
            verbose[1],
            "- (no lexical matches in index — start from MAP hot files)"
        );
        assert_eq!(
            compact[0],
            "(no lexical matches — start from MAP hot files)"
        );
    }

    #[test]
    fn map_never_fewer_than_min_rows() {
        let d = data(&["a.py", "b.py", "c.py", "d.py"], &[]);
        let rows = build_map(&d, None);
        assert!(rows.len() >= MIN_ROWS);
        assert!(
            rows[0].starts_with("- ./ — code; hot: a.py:1 symbol_0()"),
            "{rows:?}"
        );
        assert!(
            rows[0].ends_with("def symbol_0(x)"),
            "signature used as desc: {rows:?}"
        );
    }
}
