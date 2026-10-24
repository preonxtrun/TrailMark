//! Persistent index cache (port of `cache.py`): `.codegraph/index.db`
//! (SQLite via rusqlite "bundled", which includes FTS5 for BM25).

use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use rusqlite::{params, Connection, OpenFlags};

use crate::model::{FileEntry, IndexData, Ref, Symbol};

const SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY, lang TEXT NOT NULL, mtime REAL NOT NULL, size INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS symbols (
    file TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL,
    line INTEGER NOT NULL, end_line INTEGER NOT NULL,
    signature TEXT NOT NULL DEFAULT '', doc TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS refs (
    src_file TEXT NOT NULL, dst_name TEXT NOT NULL, dst_file TEXT NOT NULL, line INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file);
CREATE INDEX IF NOT EXISTS idx_refs_src ON refs(src_file);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
    name, sig, doc, path, line UNINDEXED, kind UNINDEXED, tokenize='porter unicode61'
);
";

const FTS_COLUMNS: [&str; 6] = ["name", "sig", "doc", "path", "line", "kind"];

/// One BM25 search result over the symbol table.
#[derive(Clone, Debug)]
pub struct Hit {
    /// File containing the matched symbol.
    pub file: String,
    /// Matched symbol name.
    pub name: String,
    /// Symbol kind.
    pub kind: String,
    /// 1-based line.
    pub line: u32,
    /// bm25 rank value (lower is better).
    pub score: f64,
}

/// Library error type: errors surface as `Result`, never panics.
#[derive(Debug, thiserror::Error)]
pub enum CodegraphError {
    /// SQLite failure.
    #[error("cache error: {0}")]
    Cache(#[from] rusqlite::Error),
    /// Filesystem failure.
    #[error("io error: {0}")]
    Io(#[from] std::io::Error),
    /// Path is not valid UTF-8 where it must be.
    #[error("path is not valid utf-8: {0:?}")]
    Path(PathBuf),
}

/// True when an existing fts table predates the line/kind columns.
fn fts_outdated(con: &Connection) -> bool {
    let mut stmt = match con.prepare("PRAGMA table_info(fts)") {
        Ok(s) => s,
        Err(_) => return false,
    };
    let cols: Vec<String> = match stmt.query_map([], |row| row.get::<_, String>(1)) {
        Ok(rows) => rows.filter_map(|r| r.ok()).collect(),
        Err(_) => return false,
    };
    !cols.is_empty() && cols.iter().map(|s| s.as_str()).collect::<Vec<_>>() != FTS_COLUMNS
}

/// SQLite-backed store for [`IndexData`] with FTS5 lexical search.
pub struct IndexCache {
    /// The `.codegraph` directory.
    pub dir: PathBuf,
    /// Full path of `index.db`.
    pub db_path: PathBuf,
}

impl IndexCache {
    /// Cache rooted at `codegraph_dir` (usually `<repo>/.codegraph`).
    pub fn new(codegraph_dir: &Path) -> Self {
        IndexCache {
            dir: codegraph_dir.to_path_buf(),
            db_path: codegraph_dir.join("index.db"),
        }
    }

    /// Open a writable connection with reduced fsync durability.
    ///
    /// The index is a derived cache (rebuildable with `codegraph init`), so the
    /// incremental write path trades crash-atomicity for speed — a deviation
    /// from the Python reference (which keeps SQLite's default `synchronous=FULL`)
    /// justified by the SPEC warm-update performance target. The initial
    /// `save()` keeps default durability: it renames a fully-built temp db over
    /// the live file, so a crash can only leave the previous (stale but valid)
    /// index in place.
    fn write_conn(path: &Path) -> Result<Connection, CodegraphError> {
        let con = Connection::open(path)?;
        let _ = con.pragma_update(None, "synchronous", "OFF");
        Ok(con)
    }

    fn read_only(&self) -> Result<Connection, CodegraphError> {
        // same URI form as the Python reference: file:<path>?mode=ro
        let uri = format!("file:{}?mode=ro", self.db_path.display());
        let con = Connection::open_with_flags(
            uri,
            OpenFlags::SQLITE_OPEN_URI | OpenFlags::SQLITE_OPEN_READ_ONLY,
        )?;
        // memory-mapped I/O for the read path (pure speed; no on-disk change)
        let _ = con.pragma_update(None, "mmap_size", 268_435_456i64);
        Ok(con)
    }

    // -- persistence ---------------------------------------------------------

    /// Load the cached index, or None if absent/corrupt.
    ///
    /// The three tables are materialized in parallel on separate read-only
    /// connections (the row→struct conversion dominates, not the SQL), then
    /// merged in path order — the on-disk insert order is path-sorted, so the
    /// merge reproduces the Python reference's per-file symbol order exactly
    /// (`(line, name)` within each file).
    pub fn load(&self) -> Option<IndexData> {
        use std::thread;
        if !self.db_path.exists() {
            return None;
        }
        thread::scope(|scope| {
            let files_handle = scope.spawn(|| {
                let con = self.read_only().ok()?;
                let mut out: Vec<FileEntry> = Vec::with_capacity(1024);
                let mut stmt = con
                    .prepare("SELECT path, lang, mtime, size FROM files")
                    .ok()?;
                let mut rows = stmt.query([]).ok()?;
                while let Ok(Some(row)) = rows.next() {
                    out.push(FileEntry {
                        path: row.get(0).ok()?,
                        lang: row.get(1).ok()?,
                        mtime: row.get(2).ok()?,
                        size: (row.get::<_, i64>(3).ok()?).max(0) as u64,
                        symbols: Vec::new(),
                    });
                }
                Some(out)
            });
            // natural rowid order is file-grouped and path-ascending (the
            // writer inserts per path in (line, name) order) — the same order
            // the Python reference relies on when loading, so a two-pointer
            // merge reproduces it exactly
            let symbols_handle = scope.spawn(|| {
                let con = self.read_only().ok()?;
                let mut out: Vec<Symbol> = Vec::with_capacity(1024);
                let mut stmt = con
                    .prepare("SELECT file, name, kind, line, end_line, signature, doc FROM symbols")
                    .ok()?;
                let mut rows = stmt.query([]).ok()?;
                while let Ok(Some(row)) = rows.next() {
                    out.push(Symbol {
                        file: row.get(0).ok()?,
                        name: row.get(1).ok()?,
                        kind: row.get(2).ok()?,
                        line: (row.get::<_, i64>(3).ok()?).max(0) as u32,
                        end_line: (row.get::<_, i64>(4).ok()?).max(0) as u32,
                        signature: row.get(5).ok()?,
                        doc: row.get(6).ok()?,
                    });
                }
                Some(out)
            });
            let refs_handle = scope.spawn(|| {
                let con = self.read_only().ok()?;
                let mut out: Vec<Ref> = Vec::with_capacity(1024);
                let mut stmt = con
                    .prepare("SELECT src_file, dst_name, dst_file, line FROM refs")
                    .ok()?;
                let mut rows = stmt.query([]).ok()?;
                while let Ok(Some(row)) = rows.next() {
                    out.push(Ref {
                        src_file: row.get(0).ok()?,
                        dst_name: row.get(1).ok()?,
                        dst_file: row.get(2).ok()?,
                        line: (row.get::<_, i64>(3).ok()?).max(0) as u32,
                    });
                }
                Some(out)
            });

            let mut file_list = files_handle.join().ok()??;
            let symbols = symbols_handle.join().ok()??;
            let refs = refs_handle.join().ok()??;

            // two-pointer merge: `file_list` is path-ordered (rowid order) and
            // `symbols` is file-grouped in the same byte order — rows are
            // MOVED into their entry (no per-row clone)
            let mut files: std::collections::BTreeMap<String, FileEntry> =
                std::collections::BTreeMap::new();
            let mut sym_iter = symbols.into_iter();
            let mut pending = sym_iter.next();
            for entry in file_list.drain(..) {
                let mut owned = entry;
                // drop symbol rows for paths absent from the `files` table
                while let Some(sym) = pending.as_ref() {
                    if sym.file.as_str() < owned.path.as_str() {
                        pending = sym_iter.next();
                    } else {
                        break;
                    }
                }
                while let Some(sym) = pending.as_ref() {
                    if sym.file == owned.path {
                        owned.symbols.push(pending.take().expect("pending symbol"));
                        pending = sym_iter.next();
                    } else {
                        break;
                    }
                }
                files.insert(owned.path.clone(), owned);
            }
            let root: String = {
                let con = self.read_only().ok()?;
                con.query_row("SELECT value FROM meta WHERE key='root'", [], |row| {
                    row.get(0)
                })
                .unwrap_or_default()
            };
            Some(IndexData { root, files, refs })
        })
    }

    /// Atomically persist `data` (write temp db, then rename over the live file).
    pub fn save(&self, data: &IndexData) -> Result<(), CodegraphError> {
        fs::create_dir_all(&self.dir)?;
        static TMP_SEQ: AtomicU64 = AtomicU64::new(0);
        let tmp = self.dir.join(format!(
            "index.{}.{}.tmp.db",
            std::process::id(),
            TMP_SEQ.fetch_add(1, Ordering::Relaxed)
        ));
        let result = self.save_to(&tmp, data);
        match result {
            Ok(()) => {
                fs::rename(&tmp, &self.db_path)?;
                Ok(())
            }
            Err(e) => {
                let _ = fs::remove_file(&tmp);
                Err(e)
            }
        }
    }

    fn save_to(&self, tmp: &Path, data: &IndexData) -> Result<(), CodegraphError> {
        let con = Connection::open(tmp)?;
        con.execute_batch(SCHEMA)?;
        con.execute(
            "INSERT INTO meta(key, value) VALUES ('root', ?)",
            params![data.root],
        )?;
        let tx = con.unchecked_transaction()?;
        for f in data.files.values() {
            con.execute(
                "INSERT INTO files(path, lang, mtime, size) VALUES (?,?,?,?)",
                params![f.path, f.lang, f.mtime, f.size],
            )?;
        }
        for f in data.files.values() {
            for s in sorted_symbols(&f.symbols) {
                con.execute(
                    "INSERT INTO symbols(file, name, kind, line, end_line, signature, doc) \
                     VALUES (?,?,?,?,?,?,?)",
                    params![
                        s.file,
                        s.name,
                        s.kind,
                        s.line,
                        s.end_line,
                        s.signature,
                        s.doc
                    ],
                )?;
            }
        }
        for r in &data.refs {
            con.execute(
                "INSERT INTO refs(src_file, dst_name, dst_file, line) VALUES (?,?,?,?)",
                params![r.src_file, r.dst_name, r.dst_file, r.line],
            )?;
        }
        for f in data.files.values() {
            for s in sorted_symbols(&f.symbols) {
                con.execute(
                    "INSERT INTO fts(name, sig, doc, path, line, kind) VALUES (?,?,?,?,?,?)",
                    params![s.name, s.signature, s.doc, s.file, s.line, s.kind],
                )?;
            }
        }
        tx.commit()?;
        Ok(())
    }

    // -- incremental helpers -------------------------------------------------

    /// Cached mtimes for incremental updates.
    pub fn file_mtimes(&self) -> HashMap<String, f64> {
        if !self.db_path.exists() {
            return HashMap::new();
        }
        let con = match self.read_only() {
            Ok(c) => c,
            Err(_) => return HashMap::new(),
        };
        let mut out = HashMap::new();
        let Ok(mut stmt) = con.prepare("SELECT path, mtime FROM files") else {
            return out;
        };
        let Ok(rows) = stmt.query([]) else {
            return out;
        };
        let mut rows = rows;
        while let Ok(Some(row)) = rows.next() {
            if let (Ok(p), Ok(m)) = (row.get::<_, String>(0), row.get::<_, f64>(1)) {
                out.insert(p, m);
            }
        }
        out
    }

    // -- incremental mutation (INDEX subsystem) -------------------------------

    /// Remove files plus all their symbols, refs and FTS rows from the cache.
    ///
    /// Missing paths are ignored; a no-op when the database does not exist yet.
    pub fn remove_files(&self, paths: &[String]) -> Result<(), CodegraphError> {
        let mut sorted: Vec<&String> = paths.iter().collect();
        sorted.sort();
        sorted.dedup();
        if sorted.is_empty() || !self.db_path.exists() {
            return Ok(());
        }
        let con = Self::write_conn(&self.db_path)?;
        con.execute_batch(SCHEMA)?;
        let tx = con.unchecked_transaction()?;
        for p in &sorted {
            con.execute("DELETE FROM files WHERE path = ?", params![*p])?;
            con.execute("DELETE FROM symbols WHERE file = ?", params![*p])?;
            con.execute("DELETE FROM refs WHERE src_file = ?", params![*p])?;
            con.execute("DELETE FROM fts WHERE path = ?", params![*p])?;
        }
        tx.commit()?;
        Ok(())
    }

    /// Insert or replace the given file entries and the refs originating from them.
    ///
    /// Symbols, FTS rows and refs whose `src_file` is one of the entry paths are
    /// replaced wholesale, so repeated calls stay idempotent. Refs from other
    /// files are ignored (callers pass refs grouped by their source file).
    /// Creates the database (with schema) when it does not exist yet.
    pub fn upsert_files(&self, entries: &[FileEntry], refs: &[Ref]) -> Result<(), CodegraphError> {
        let mut entry_list: Vec<&FileEntry> = entries.iter().collect();
        entry_list.sort_by(|a, b| a.path.cmp(&b.path));
        let path_set: std::collections::HashSet<&String> =
            entry_list.iter().map(|e| &e.path).collect();
        let mut ref_list: Vec<&Ref> = refs
            .iter()
            .filter(|r| path_set.contains(&r.src_file))
            .collect();
        ref_list.sort_by(|a, b| {
            (
                a.src_file.as_str(),
                a.line,
                a.dst_name.as_str(),
                a.dst_file.as_str(),
            )
                .cmp(&(
                    b.src_file.as_str(),
                    b.line,
                    b.dst_name.as_str(),
                    b.dst_file.as_str(),
                ))
        });
        if entry_list.is_empty() && ref_list.is_empty() {
            return Ok(());
        }
        fs::create_dir_all(&self.dir)?;
        let con = Self::write_conn(&self.db_path)?;
        if fts_outdated(&con) {
            con.execute("DROP TABLE fts", [])?; // derived index — rebuilt by save()
        }
        con.execute_batch(SCHEMA)?;
        let tx = con.unchecked_transaction()?;
        for e in &entry_list {
            con.execute(
                "INSERT OR REPLACE INTO files(path, lang, mtime, size) VALUES (?,?,?,?)",
                params![e.path, e.lang, e.mtime, e.size],
            )?;
        }
        for p in &path_set {
            con.execute("DELETE FROM symbols WHERE file = ?", params![*p])?;
        }
        for e in &entry_list {
            for s in sorted_symbols(&e.symbols) {
                con.execute(
                    "INSERT INTO symbols(file, name, kind, line, end_line, signature, doc) \
                     VALUES (?,?,?,?,?,?,?)",
                    params![
                        s.file,
                        s.name,
                        s.kind,
                        s.line,
                        s.end_line,
                        s.signature,
                        s.doc
                    ],
                )?;
            }
        }
        for p in &path_set {
            con.execute("DELETE FROM refs WHERE src_file = ?", params![*p])?;
        }
        for r in &ref_list {
            con.execute(
                "INSERT INTO refs(src_file, dst_name, dst_file, line) VALUES (?,?,?,?)",
                params![r.src_file, r.dst_name, r.dst_file, r.line],
            )?;
        }
        for p in &path_set {
            con.execute("DELETE FROM fts WHERE path = ?", params![*p])?;
        }
        for e in &entry_list {
            for s in sorted_symbols(&e.symbols) {
                con.execute(
                    "INSERT INTO fts(name, sig, doc, path, line, kind) VALUES (?,?,?,?,?,?)",
                    params![s.name, s.signature, s.doc, s.file, s.line, s.kind],
                )?;
            }
        }
        tx.commit()?;
        Ok(())
    }

    /// Fused incremental delta: delete `removed` + `changed` paths, then insert
    /// the changed entries and their refs in ONE connection and transaction.
    ///
    /// Equivalent to calling [`Self::remove_files`] followed by
    /// [`Self::upsert_files`], but halves the journal/fsync cycles and the FTS
    /// full-scan deletes (the Python reference rescans FTS for changed files
    /// twice). Used by the warm-update path for the SPEC performance target.
    pub fn apply_delta(
        &self,
        removed: &[String],
        entries: &[FileEntry],
        refs: &[Ref],
    ) -> Result<(), CodegraphError> {
        let mut paths: Vec<&String> = removed
            .iter()
            .chain(entries.iter().map(|e| &e.path))
            .collect();
        paths.sort();
        paths.dedup();
        if paths.is_empty() {
            return Ok(());
        }
        let mut entry_list: Vec<&FileEntry> = entries.iter().collect();
        entry_list.sort_by(|a, b| a.path.cmp(&b.path));
        let path_set: std::collections::HashSet<&String> =
            entry_list.iter().map(|e| &e.path).collect();
        let mut ref_list: Vec<&Ref> = refs
            .iter()
            .filter(|r| path_set.contains(&r.src_file))
            .collect();
        ref_list.sort_by(|a, b| {
            (
                a.src_file.as_str(),
                a.line,
                a.dst_name.as_str(),
                a.dst_file.as_str(),
            )
                .cmp(&(
                    b.src_file.as_str(),
                    b.line,
                    b.dst_name.as_str(),
                    b.dst_file.as_str(),
                ))
        });

        if !self.db_path.exists() {
            // nothing to delete; a plain upsert creates the database
            return self.upsert_files(entries, refs);
        }
        let con = Self::write_conn(&self.db_path)?;
        con.execute_batch(SCHEMA)?;
        if fts_outdated(&con) {
            con.execute("DROP TABLE fts", [])?;
        }
        let tx = con.unchecked_transaction()?;
        for p in &paths {
            con.execute("DELETE FROM files WHERE path = ?", params![*p])?;
            con.execute("DELETE FROM symbols WHERE file = ?", params![*p])?;
            con.execute("DELETE FROM refs WHERE src_file = ?", params![*p])?;
            con.execute("DELETE FROM fts WHERE path = ?", params![*p])?;
        }
        for e in &entry_list {
            con.execute(
                "INSERT OR REPLACE INTO files(path, lang, mtime, size) VALUES (?,?,?,?)",
                params![e.path, e.lang, e.mtime, e.size],
            )?;
            for s in sorted_symbols(&e.symbols) {
                con.execute(
                    "INSERT INTO symbols(file, name, kind, line, end_line, signature, doc) \
                     VALUES (?,?,?,?,?,?,?)",
                    params![
                        s.file,
                        s.name,
                        s.kind,
                        s.line,
                        s.end_line,
                        s.signature,
                        s.doc
                    ],
                )?;
                con.execute(
                    "INSERT INTO fts(name, sig, doc, path, line, kind) VALUES (?,?,?,?,?,?)",
                    params![s.name, s.signature, s.doc, s.file, s.line, s.kind],
                )?;
            }
        }
        for r in &ref_list {
            con.execute(
                "INSERT INTO refs(src_file, dst_name, dst_file, line) VALUES (?,?,?,?)",
                params![r.src_file, r.dst_name, r.dst_file, r.line],
            )?;
        }
        tx.commit()?;
        Ok(())
    }

    // -- lexical search ------------------------------------------------------

    /// BM25 search over symbols via FTS5 (lower score = better match).
    pub fn search(&self, query: &str, k: usize) -> Result<Vec<Hit>, CodegraphError> {
        if !self.db_path.exists() {
            return Ok(vec![]);
        }
        // FTS5 query syntax: quote each token to avoid syntax errors on odd input.
        let blob: String = query
            .chars()
            .map(|c| if c.is_alphanumeric() { c } else { ' ' })
            .collect();
        let terms: Vec<&str> = blob.split_whitespace().filter(|t| !t.is_empty()).collect();
        if terms.is_empty() {
            return Ok(vec![]);
        }
        let quoted: Vec<String> = terms.iter().take(32).map(|t| format!("\"{t}\"")).collect();
        let match_expr = quoted.join(" OR ");
        let con = self.read_only()?;
        let try_query = |sql: &str| -> Result<Vec<Hit>, CodegraphError> {
            let mut stmt = con.prepare(sql)?;
            let rows = stmt.query_map(params![match_expr, k as i64], |row| {
                Ok(Hit {
                    file: row.get(0)?,
                    name: row.get(1)?,
                    kind: row.get(2)?,
                    line: (row.get::<_, i64>(3)?).max(0) as u32,
                    score: row.get(4)?,
                })
            })?;
            Ok(rows.filter_map(|r| r.ok()).collect())
        };
        match try_query(
            "SELECT path, name, kind, line, bm25(fts) FROM fts WHERE fts MATCH ? \
             ORDER BY bm25(fts) LIMIT ?",
        ) {
            Ok(hits) => Ok(hits),
            Err(_) => {
                // Legacy index without the line/kind columns — degrade gracefully.
                match try_query(
                    "SELECT path, name, '', 0, bm25(fts) FROM fts WHERE fts MATCH ? \
                     ORDER BY bm25(fts) LIMIT ?",
                ) {
                    Ok(hits) => Ok(hits),
                    Err(_) => Ok(vec![]),
                }
            }
        }
    }
}

/// `(line, name)`-sorted copy, matching the Python insert order.
fn sorted_symbols(symbols: &[Symbol]) -> Vec<&Symbol> {
    let mut out: Vec<&Symbol> = symbols.iter().collect();
    out.sort_by(|a, b| (a.line, a.name.as_str()).cmp(&(b.line, b.name.as_str())));
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::sort_symbols;

    fn scratch(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!(
            "cg_cache_{name}_{}_{:?}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|t| t.as_nanos())
                .unwrap_or(0)
        ));
        fs::create_dir_all(&d).unwrap();
        d
    }

    fn sample() -> IndexData {
        let mk = |path: &str, syms: Vec<(&str, &str, u32, u32)>| FileEntry {
            path: path.to_string(),
            lang: "py".into(),
            mtime: 100.5,
            size: 42,
            symbols: syms
                .into_iter()
                .map(|(name, kind, line, end)| Symbol {
                    file: path.to_string(),
                    name: name.to_string(),
                    kind: kind.to_string(),
                    line,
                    end_line: end,
                    signature: format!("def {name}(x)"),
                    doc: format!("Doc for {name}."),
                })
                .collect(),
        };
        let mut a = mk(
            "app/a.py",
            vec![("alpha", "def", 1, 3), ("Beta", "class", 5, 9)],
        );
        sort_symbols(&mut a.symbols);
        let b = mk("app/b.py", vec![("beta_fn", "def", 2, 4)]);
        IndexData {
            root: "/repo".into(),
            files: [("app/a.py".to_string(), a), ("app/b.py".to_string(), b)]
                .into_iter()
                .collect(),
            refs: vec![Ref {
                src_file: "app/b.py".into(),
                dst_name: "alpha".into(),
                dst_file: "app/a.py".into(),
                line: 3,
            }],
        }
    }

    #[test]
    fn save_load_roundtrip_preserves_data() {
        let dir = scratch("roundtrip");
        let cache = IndexCache::new(&dir);
        let data = sample();
        cache.save(&data).unwrap();
        let loaded = cache.load().expect("cache loads");
        assert_eq!(loaded.root, "/repo");
        assert_eq!(loaded.files.len(), 2);
        let a = &loaded.files["app/a.py"];
        assert_eq!(a.symbols.len(), 2);
        assert_eq!(a.symbols[0].name, "alpha");
        assert_eq!(a.symbols[0].line, 1);
        assert_eq!(loaded.refs.len(), 1);
        assert_eq!(loaded.refs[0].dst_file, "app/a.py");
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn load_absent_or_corrupt_returns_none() {
        let dir = scratch("absent");
        let cache = IndexCache::new(&dir);
        assert!(cache.load().is_none());
        fs::create_dir_all(&dir).unwrap();
        fs::write(&cache.db_path, b"this is not sqlite").unwrap();
        assert!(cache.load().is_none());
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn search_ranks_bm25_lower_better() {
        let dir = scratch("search");
        let cache = IndexCache::new(&dir);
        cache.save(&sample()).unwrap();
        let hits = cache.search("alpha", 20).unwrap();
        assert!(!hits.is_empty());
        assert_eq!(hits[0].file, "app/a.py");
        assert_eq!(hits[0].name, "alpha");
        assert_eq!(hits[0].kind, "def");
        assert!(hits[0].score <= 0.0, "bm25 is negative (lower=better)");
        // degenerate queries are safe
        assert!(cache.search("   ", 5).unwrap().is_empty());
        assert!(cache.search("!!! @@@ ???", 5).unwrap().is_empty());
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn remove_and_upsert_are_idempotent() {
        let dir = scratch("mutate");
        let cache = IndexCache::new(&dir);
        let data = sample();
        cache.save(&data).unwrap();

        cache.remove_files(&["app/b.py".into()]).unwrap();
        let loaded = cache.load().unwrap();
        assert!(!loaded.files.contains_key("app/b.py"));
        assert!(!loaded.refs.iter().any(|r| r.src_file == "app/b.py"));
        // removing again / missing paths: safe no-op
        cache
            .remove_files(&["app/b.py".into(), "does/not/exist.py".into()])
            .unwrap();

        let b = data.files["app/b.py"].clone();
        cache.upsert_files(&[b], &data.refs).unwrap();
        let loaded = cache.load().unwrap();
        assert!(loaded.files.contains_key("app/b.py"));
        assert_eq!(loaded.files["app/b.py"].symbols.len(), 1);
        assert_eq!(loaded.refs.len(), 1);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn file_mtimes_reflect_saved_entries() {
        let dir = scratch("mtimes");
        let cache = IndexCache::new(&dir);
        assert!(cache.file_mtimes().is_empty());
        cache.save(&sample()).unwrap();
        let mtimes = cache.file_mtimes();
        assert_eq!(mtimes.len(), 2);
        assert_eq!(mtimes["app/a.py"], 100.5);
        let _ = fs::remove_dir_all(&dir);
    }
}
