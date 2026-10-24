//! Shared data model: the Rust mirror of `src/trailmark/model.py` (frozen contract).
//!
//! Lines are 1-based and inclusive. All paths are repo-relative posix strings.

/// Markers delimiting the managed block inside AGENTS.md.
pub const BLOCK_START: &str = "<!-- trailmark:start v1 -->";
/// Markers delimiting the managed block inside AGENTS.md.
pub const BLOCK_END: &str = "<!-- trailmark:end -->";

/// Default hard cap for the injected block body, in bytes.
pub const DEFAULT_BUDGET_BYTES: usize = 2048;

use std::collections::BTreeMap;

/// A definition extracted from one file.
#[derive(Clone, Debug, Default, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Symbol {
    /// Repo-relative posix path of the defining file.
    pub file: String,
    /// Defined name.
    pub name: String,
    /// `"def" | "class" | "method" | "var" | ...`
    pub kind: String,
    /// 1-based start line.
    pub line: u32,
    /// 1-based inclusive end line.
    pub end_line: u32,
    /// Display signature (may be empty).
    pub signature: String,
    /// First line of the docstring / leading comment, trimmed.
    pub doc: String,
}

/// A reference edge between a source file and a (resolved) target name/file.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Ref {
    /// Repo-relative posix path of the referencing file.
    pub src_file: String,
    /// Referenced name (import name, identifier, module path...).
    pub dst_name: String,
    /// Resolved repo-relative target path, `""` when unresolved.
    pub dst_file: String,
    /// 1-based line of the reference in `src_file`.
    pub line: u32,
}

/// Per-file index record: language, timestamps and extracted symbols.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct FileEntry {
    /// Repo-relative posix path.
    pub path: String,
    /// `"py" | "ts" | "js" | "go" | "rs" | "c" | "txt"`.
    pub lang: String,
    /// File mtime as seconds since the epoch (float, like Python `st_mtime`).
    pub mtime: f64,
    /// File size in bytes.
    pub size: u64,
    /// Symbols defined in this file, sorted by `(line, name)`.
    pub symbols: Vec<Symbol>,
}

/// Full repository index: per-file symbols plus resolved reference edges.
///
/// `files` is a [`BTreeMap`] so iteration order is always sorted by path
/// (hard invariant #1: determinism).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct IndexData {
    /// Absolute repository root this index was built from.
    pub root: String,
    /// Indexed files keyed by repo-relative posix path (sorted).
    pub files: BTreeMap<String, FileEntry>,
    /// Resolved reference edges, sorted by `(src_file, line, dst_name, dst_file)`.
    pub refs: Vec<Ref>,
}

impl IndexData {
    /// Every symbol of every file, in `(file path, line, name)` order.
    pub fn symbols(&self) -> Vec<Symbol> {
        self.files
            .values()
            .flat_map(|f| f.symbols.iter().cloned())
            .collect()
    }
}

/// Sort helper used everywhere symbols are stored: `(line, name)`.
pub fn sort_symbols(symbols: &mut [Symbol]) {
    symbols.sort_by(|a, b| (a.line, a.name.as_str()).cmp(&(b.line, b.name.as_str())));
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn symbols_iterates_files_in_path_order() {
        let mut a = FileEntry {
            path: "a.py".into(),
            lang: "py".into(),
            ..Default::default()
        };
        a.symbols.push(Symbol {
            file: "a.py".into(),
            name: "fa".into(),
            kind: "def".into(),
            line: 1,
            end_line: 2,
            ..Default::default()
        });
        let mut b = FileEntry {
            path: "b.py".into(),
            lang: "py".into(),
            ..Default::default()
        };
        b.symbols.push(Symbol {
            file: "b.py".into(),
            name: "fb".into(),
            kind: "def".into(),
            line: 3,
            end_line: 3,
            ..Default::default()
        });
        let data = IndexData {
            root: "/r".into(),
            files: BTreeMap::from([("b.py".into(), b), ("a.py".into(), a)]),
            refs: vec![],
        };
        let names: Vec<String> = data.symbols().into_iter().map(|s| s.name).collect();
        assert_eq!(names, vec!["fa", "fb"]);
    }
}
