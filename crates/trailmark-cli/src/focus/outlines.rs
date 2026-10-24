//! Deterministic per-file symbol outlines under `.trailmark/outlines/`
//! (port of `focus/outlines.py`).
//!
//! Outline file for a source `<relpath>` lives at `<out_dir>/<relpath>.md`
//! (parents created on demand, `..` components stripped):
//!
//! ```text
//! # <relpath>
//! L<line>-<end>: <kind> <signature>[ — <doc>]
//! ```
//!
//! One line per symbol sorted by (line, name); `method` entries are indented
//! two spaces. Output is capped at 200 lines per file. Writes are no-ops when
//! the target already holds identical content (no churn, mtime preserved).

use std::collections::BTreeMap;
use std::io;
use std::path::{Path, PathBuf};

use trailmark_core::{pagerank, FileEntry, IndexData};

pub const TRAILMARK_DIR: &str = ".trailmark";
pub const OUTLINES_SUBDIR: &str = "outlines";
pub const MAX_LINES: usize = 200;
pub const TRUNCATION_MARK: &str = "... (truncated)";
const SIG_PREFIXES: [&str; 3] = ["async def ", "def ", "class "];

/// Normalize a repo-relative path: posix separators, no "", ".", ".." parts.
fn sanitize(rel_path: &str) -> String {
    let normalized = rel_path.replace('\\', "/");
    let parts: Vec<&str> = normalized
        .split('/')
        .filter(|p| !p.is_empty() && *p != "." && *p != "..")
        .collect();
    if parts.is_empty() {
        "_unnamed_".to_string()
    } else {
        parts.join("/")
    }
}

/// Repo-relative posix path of the outline file for source `rel_path`.
pub fn outline_posix(rel_path: &str) -> String {
    format!(
        "{TRAILMARK_DIR}/{OUTLINES_SUBDIR}/{}.md",
        sanitize(rel_path)
    )
}

/// Render the outline body for one indexed file (deterministic, \n-terminated).
pub fn outline_text(entry: &FileEntry) -> String {
    let mut syms = entry.symbols.clone();
    syms.sort_by(|a, b| (a.line, a.name.as_str()).cmp(&(b.line, b.name.as_str())));
    let mut lines = vec![format!("# {}", entry.path)];
    for sym in &syms {
        let mut sig = sym
            .signature
            .split_whitespace()
            .collect::<Vec<_>>()
            .join(" ");
        for prefix in SIG_PREFIXES {
            if let Some(rest) = sig.strip_prefix(prefix) {
                sig = rest.to_string();
                break;
            }
        }
        let indent = if sym.kind == "method" { "  " } else { "" };
        let mut line = format!(
            "{indent}L{}-{}: {} {}",
            sym.line, sym.end_line, sym.kind, sig
        )
        .trim_end()
        .to_string();
        if !sym.doc.is_empty() {
            line = format!(
                "{line} — {}",
                sym.doc.split_whitespace().collect::<Vec<_>>().join(" ")
            );
        }
        lines.push(line);
    }
    if lines.len() > MAX_LINES {
        lines.truncate(MAX_LINES - 1);
        lines.push(TRUNCATION_MARK.to_string());
    }
    format!("{}\n", lines.join("\n"))
}

/// Content of `path`, or None when absent/unreadable (treated as stale).
fn current_text(path: &Path) -> Option<String> {
    std::fs::read_to_string(path).ok()
}

/// Write outlines for `paths` into `out_dir`; return out-dir-relative posix paths.
///
/// Paths missing from the index are skipped. Unchanged outlines are not
/// rewritten (no-op for mtime and content); they are still reported as present.
pub fn generate_for(data: &IndexData, paths: &[String], out_dir: &Path) -> io::Result<Vec<String>> {
    let mut written: Vec<String> = Vec::new();
    let mut unique: Vec<&String> = paths.iter().collect();
    unique.sort();
    unique.dedup();
    for rel in unique {
        let Some(entry) = data.files.get(rel) else {
            continue;
        };
        let rel_out = format!("{}.md", sanitize(rel));
        let target: PathBuf = out_dir.join(&rel_out);
        let text = outline_text(entry);
        if current_text(&target) == Some(text.clone()) {
            written.push(rel_out);
            continue;
        }
        if let Some(parent) = target.parent() {
            std::fs::create_dir_all(parent)?;
        }
        std::fs::write(&target, text)?;
        written.push(rel_out);
    }
    Ok(written)
}

/// Write outlines for the top-`k` PageRank files; returns paths like generate_for.
pub fn generate_top(data: &IndexData, out_dir: &Path, k: usize) -> io::Result<Vec<String>> {
    if data.files.is_empty() || k == 0 {
        return Ok(vec![]);
    }
    let prior = pagerank(data, 0.85, 30, None);
    let mut top: Vec<&String> = data.files.keys().collect();
    top.sort_by(|a, b| {
        prior
            .get(*b)
            .copied()
            .unwrap_or(0.0)
            .partial_cmp(&prior.get(*a).copied().unwrap_or(0.0))
            .unwrap_or(std::cmp::Ordering::Equal)
            .then(a.cmp(b))
    });
    let paths: Vec<String> = top.into_iter().take(k).cloned().collect();
    generate_for(data, &paths, out_dir)
}

/// Alias matching the Python module-level constant layout (used by tests).
#[allow(dead_code)]
pub type PathEntryMap = BTreeMap<String, FileEntry>;

#[cfg(test)]
mod tests {
    use super::*;
    use trailmark_core::Symbol;

    fn entry(path: &str) -> FileEntry {
        FileEntry {
            path: path.to_string(),
            lang: "py".into(),
            mtime: 1.0,
            size: 1,
            symbols: vec![
                Symbol {
                    file: path.into(),
                    name: "run".into(),
                    kind: "method".into(),
                    line: 10,
                    end_line: 14,
                    signature: "def run(self, v)".into(),
                    doc: "Run it.".into(),
                },
                Symbol {
                    file: path.into(),
                    name: "Thing".into(),
                    kind: "class".into(),
                    line: 5,
                    end_line: 20,
                    signature: "class Thing".into(),
                    doc: String::new(),
                },
            ],
        }
    }

    #[test]
    fn outline_text_shape_and_cap() {
        let e = entry("app/thing.py");
        let text = outline_text(&e);
        assert_eq!(
            text,
            "# app/thing.py\nL5-20: class Thing\n  L10-14: method run(self, v) — Run it.\n"
        );
    }

    #[test]
    fn outline_posix_sanitizes() {
        assert_eq!(outline_posix("src/a.py"), ".trailmark/outlines/src/a.py.md");
        assert_eq!(outline_posix("../x.py"), ".trailmark/outlines/x.py.md");
        assert_eq!(outline_posix(""), ".trailmark/outlines/_unnamed_.md");
    }

    #[test]
    fn generate_for_is_idempotent_and_skips_missing() {
        let dir = std::env::temp_dir().join(format!("cg_out_{}", std::process::id()));
        let out = dir.join("outlines");
        let _ = std::fs::remove_dir_all(&dir);
        let mut files = BTreeMap::new();
        files.insert("app/thing.py".to_string(), entry("app/thing.py"));
        let data = IndexData {
            root: "/r".into(),
            files,
            refs: vec![],
        };
        let paths = vec!["app/thing.py".to_string(), "nope.py".to_string()];
        let written = generate_for(&data, &paths, &out).unwrap();
        assert_eq!(written, vec!["app/thing.py.md"]);
        let target = out.join("app/thing.py.md");
        let content = std::fs::read_to_string(&target).unwrap();
        let meta_before = target.metadata().unwrap().modified().unwrap();
        // second call: identical content → no rewrite
        let written = generate_for(&data, &paths, &out).unwrap();
        assert_eq!(written, vec!["app/thing.py.md"]);
        assert_eq!(std::fs::read_to_string(&target).unwrap(), content);
        assert_eq!(meta_before, target.metadata().unwrap().modified().unwrap());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn outline_cap_at_200_lines() {
        let syms: Vec<Symbol> = (0..250)
            .map(|i| Symbol {
                file: "big.py".into(),
                name: format!("fn{i}"),
                kind: "def".into(),
                line: i + 1,
                end_line: i + 1,
                ..Default::default()
            })
            .collect();
        let e = FileEntry {
            path: "big.py".into(),
            lang: "py".into(),
            mtime: 1.0,
            size: 1,
            symbols: syms,
        };
        let text = outline_text(&e);
        let lines: Vec<&str> = text.lines().collect();
        assert_eq!(lines.len(), MAX_LINES);
        assert_eq!(lines[199], TRUNCATION_MARK);
    }
}
