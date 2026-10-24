//! Vocabulary extraction for the GLOSSARY section (port of `atlas/glossary.py`).
//!
//! Terms are snake/camel sub-tokens of symbol names and signatures with
//! `len >= 5` and `freq >= 3` minus a small stopword list. A term's score is
//! `freq * log(1 + distinct_files)` — used for ordering only, never emitted.
//! The top ≤ 12 terms are emitted as:
//!
//! ```text
//! - <term> = <short meaning> (<path>:<line>)
//! ```

use std::collections::{HashMap, HashSet};

use trailmark_core::{truncate, IndexData, Symbol};

/// Fast FNV-1a hasher for the glossary's short-string maps.
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

/// Maximum number of glossary rows.
pub const MAX_TERMS: usize = 12;
/// Minimum sub-token length and total frequency for a term to qualify.
pub const MIN_LEN: usize = 5;
pub const MIN_FREQ: usize = 3;
/// Display caps (bytes).
pub const MEANING_CAP: usize = 48;

/// Small stopword list for code identifiers (len >= 5 noise words plus keywords).
pub const STOPWORDS: &[&str] = &[
    "about", "after", "again", "assert", "before", "check", "class", "default", "entry", "except",
    "false", "field", "input", "item", "items", "kwargs", "object", "optional", "output", "param",
    "params", "print", "raise", "result", "results", "return", "self", "should", "string", "super",
    "value", "values", "verify", "while",
];

/// Split snake/camel identifiers into lowercase sub-tokens.
///
/// Mirrors `re.findall(r"[A-Za-z_][A-Za-z0-9_]*")` + `split("_")` +
/// `re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")`, keeping only tokens with
/// `len >= MIN_LEN` that are not in [`STOPWORDS`].
pub fn tokenize(text: &str) -> Vec<String> {
    let mut out = Vec::new();
    for part in text.split(|c: char| !(c.is_ascii_alphanumeric() || c == '_')) {
        if part.is_empty() {
            continue;
        }
        for sub in subtoken_runs(part) {
            // len >= MIN_LEN (chars) and not a stopword, mirroring the reference
            if sub.chars().count() >= MIN_LEN && !STOPWORDS.contains(&sub) {
                // sub: &str
                out.push(sub.to_lowercase());
            }
        }
    }
    out
}

/// `[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])` runs of one identifier part, as byte-slice
/// substrings (no per-token allocation). Implements Python's leftmost,
/// first-alternative, backtracking semantics manually (the regex crate has no
/// lookahead).
fn subtoken_runs(part: &str) -> Vec<&str> {
    let chars: &[u8] = part.as_bytes();
    let is_lower_digit = |b: u8| b.is_ascii_lowercase() || b.is_ascii_digit();
    let mut out: Vec<&str> = Vec::new();
    let mut i = 0;
    while i < chars.len() {
        let c = chars[i];
        if is_lower_digit(c) {
            // alternative 1 without the optional uppercase
            let mut j = i + 1;
            while j < chars.len() && is_lower_digit(chars[j]) {
                j += 1;
            }
            out.push(&part[i..j]);
            i = j;
        } else if c.is_ascii_uppercase() {
            // alternative 1: optional uppercase followed by [a-z0-9]+
            if chars.get(i + 1).copied().is_some_and(is_lower_digit) {
                let mut j = i + 2;
                while j < chars.len() && is_lower_digit(chars[j]) {
                    j += 1;
                }
                out.push(&part[i..j]);
                i = j;
            } else {
                // alternative 2: [A-Z]+(?![a-z]) with backtracking
                let mut j = i + 1;
                while j < chars.len() && chars[j].is_ascii_uppercase() {
                    j += 1;
                }
                while j > i + 1
                    && chars
                        .get(j)
                        .copied()
                        .is_some_and(|b| b.is_ascii_lowercase())
                {
                    j -= 1;
                }
                if j > i
                    && !chars
                        .get(j)
                        .copied()
                        .is_some_and(|b| b.is_ascii_lowercase())
                {
                    out.push(&part[i..j]);
                    i = j;
                } else {
                    i += 1;
                }
            }
        } else {
            i += 1;
        }
    }
    out
}

/// Short meaning: doc first line, else `kind + signature` (byte-capped).
fn meaning(sym: &Symbol) -> String {
    let doc: String = sym.doc.split_whitespace().collect::<Vec<_>>().join(" ");
    if !doc.is_empty() {
        return truncate(&doc, MEANING_CAP);
    }
    let base = format!("{} {}", sym.kind, sym.signature)
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ");
    let base = if base.is_empty() {
        sym.kind.clone()
    } else {
        base
    };
    truncate(&base, MEANING_CAP)
}

/// GLOSSARY entry rows for `data` (deterministic; empty when nothing qualifies).
pub fn build_glossary(data: &IndexData, max_terms: usize) -> Vec<String> {
    let mut freq: FnvMap<String, usize> = FnvMap::default();
    // term -> distinct file indices (only the count feeds the score, so
    // indices instead of cloned paths)
    let mut file_sets: FnvMap<String, HashSet<usize>> = FnvMap::default();
    // term -> (hits in symbol, exemplar): most occurrences first, ties broken
    // by (path, line, name) — invariant #1.
    // exemplars are borrowed; only the ≤12 emitted rows clone a Symbol
    let mut best: FnvMap<String, (usize, &Symbol)> = FnvMap::default();
    // distinct-file counting must be per PATH (a file with several matching
    // symbols still counts once), so index the map keys
    let file_index: HashMap<&str, usize> = data
        .files
        .keys()
        .enumerate()
        .map(|(i, k)| (k.as_str(), i))
        .collect();
    for sym in data.files.values().flat_map(|f| f.symbols.iter()) {
        let file_idx = file_index[sym.file.as_str()];
        // name and signature are tokenized separately — the reference joins
        // them with a space, which the tokenizer treats as a boundary anyway
        let mut tokens = tokenize(&sym.name);
        tokens.extend(tokenize(&sym.signature));
        if tokens.is_empty() {
            continue;
        }
        // Counter(tokens): per-symbol token counts (tiny n → linear Vec)
        let mut counts: Vec<(&str, usize)> = Vec::with_capacity(tokens.len());
        for t in &tokens {
            if let Some(slot) = counts.iter_mut().find(|(w, _)| *w == t.as_str()) {
                slot.1 += 1;
            } else {
                counts.push((t.as_str(), 1));
            }
        }
        for (word, n) in counts {
            *freq.entry(word.to_string()).or_insert(0) += n;
            file_sets
                .entry(word.to_string())
                .or_default()
                .insert(file_idx);
            let cur = best.get(word);
            let replace = match cur {
                None => true,
                Some((cn, csym)) => {
                    n > *cn
                        || (n == *cn
                            && (sym.file.as_str(), sym.line, sym.name.as_str())
                                < (csym.file.as_str(), csym.line, csym.name.as_str()))
                }
            };
            if replace {
                best.insert(word.to_string(), (n, sym));
            }
        }
    }

    let mut scored: Vec<(f64, usize, String)> = freq
        .iter()
        .filter(|(_, f)| **f >= MIN_FREQ)
        .map(|(term, f)| {
            let files = file_sets[term].len();
            ((*f as f64) * (1.0 + files as f64).ln(), *f, term.clone())
        })
        .collect();
    scored.sort_by(|a, b| {
        b.0.partial_cmp(&a.0)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then(b.1.cmp(&a.1))
            .then(a.2.cmp(&b.2))
    });

    let mut rows = Vec::new();
    for (_score, _f, term) in scored.iter().take(max_terms) {
        let (_n, sym) = best[term.as_str()];
        rows.push(format!(
            "- {term} = {} ({}:{})",
            meaning(sym),
            sym.file,
            sym.line
        ));
    }
    rows
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tokenize_splits_snake_and_camel() {
        // "http" (4 chars) and "get" (3) are filtered by MIN_LEN=5; "response2"
        // keeps its digit ([a-z0-9]+)
        assert_eq!(
            tokenize("parse_files HTTPServer getHTTPResponse2"),
            vec!["parse", "files", "server", "response2"]
        );
        assert!(!tokenize("self value print").contains(&"value".to_string()));
        assert!(!tokenize("self value print").contains(&"print".to_string()));
        // len >= 5 filter
        assert!(tokenize("abc abcd abcdef").contains(&"abcdef".to_string()));
        assert!(!tokenize("abc abcd abcdef").contains(&"abcd".to_string()));
    }

    #[test]
    fn subtoken_semantics_match_python_regex() {
        // [A-Z]?[a-z0-9]+ wins when an uppercase is followed by lowercase
        assert_eq!(subtoken_runs("SharedAPI"), vec!["Shared", "API"]);
        assert_eq!(
            subtoken_runs("getHTTPResponse"),
            vec!["get", "HTTP", "Response"]
        );
        assert_eq!(subtoken_runs("ABCd"), vec!["AB", "Cd"]); // backtrack, then alt1
        assert_eq!(subtoken_runs("AB9"), vec!["AB", "9"]); // raw runs; tokenize filters len
        assert_eq!(subtoken_runs("a1_b2"), vec!["a1", "b2"]);
        assert_eq!(subtoken_runs("ABC"), vec!["ABC"]); // end-of-string lookahead ok
    }
}
