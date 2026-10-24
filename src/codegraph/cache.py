"""Persistent index cache: .codegraph/index.db (sqlite, WAL, FTS5 for BM25)."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from .model import FileEntry, IndexData, Ref, Symbol

_SCHEMA = """
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
"""

_FTS_COLUMNS = ("name", "sig", "doc", "path", "line", "kind")


@dataclass(frozen=True, slots=True)
class Hit:
    """One BM25 search result over the symbol table."""

    file: str
    name: str
    kind: str
    line: int
    score: float  # bm25 rank value (lower is better)


def _fts_outdated(con: sqlite3.Connection) -> bool:
    """True when an existing fts table predates the line/kind columns."""
    cols = [row[1] for row in con.execute("PRAGMA table_info(fts)")]
    return bool(cols) and tuple(cols) != _FTS_COLUMNS


class IndexCache:
    """SQLite-backed store for IndexData with FTS5 lexical search."""

    def __init__(self, codegraph_dir: Path) -> None:
        self.dir = Path(codegraph_dir)
        self.db_path = self.dir / "index.db"

    # -- persistence ---------------------------------------------------------

    def load(self) -> IndexData | None:
        """Load the cached index, or None if absent/corrupt."""
        if not self.db_path.exists():
            return None
        try:
            con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            try:
                files: dict[str, FileEntry] = {}
                for path, lang, mtime, size in con.execute(
                    "SELECT path, lang, mtime, size FROM files"
                ):
                    files[path] = FileEntry(path=path, lang=lang, mtime=mtime, size=size)
                for file, name, kind, line, end_line, sig, doc in con.execute(
                    "SELECT file, name, kind, line, end_line, signature, doc FROM symbols"
                ):
                    if file in files:
                        files[file].symbols.append(
                            Symbol(
                                file=file,
                                name=name,
                                kind=kind,
                                line=line,
                                end_line=end_line,
                                signature=sig,
                                doc=doc,
                            )
                        )
                refs = [
                    Ref(src_file=s, dst_name=dn, dst_file=df, line=ln)
                    for s, dn, df, ln in con.execute(
                        "SELECT src_file, dst_name, dst_file, line FROM refs"
                    )
                ]
                root = ""
                row = con.execute("SELECT value FROM meta WHERE key='root'").fetchone()
                if row:
                    root = row[0]
            finally:
                con.close()
        except sqlite3.DatabaseError:
            return None
        return IndexData(root=root, files=files, refs=refs)

    def save(self, data: IndexData) -> None:
        """Atomically persist *data* (write temp db, then os.replace)."""
        self.dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="index.", suffix=".db", dir=self.dir)
        os.close(fd)
        os.unlink(tmp)  # let sqlite create the file itself
        con = sqlite3.connect(tmp)
        try:
            con.executescript(_SCHEMA)
            con.execute("INSERT INTO meta(key, value) VALUES ('root', ?)", (data.root,))
            con.executemany(
                "INSERT INTO files(path, lang, mtime, size) VALUES (?,?,?,?)",
                [
                    (f.path, f.lang, f.mtime, f.size)
                    for f in sorted(data.files.values(), key=lambda e: e.path)
                ],
            )
            con.executemany(
                "INSERT INTO symbols(file, name, kind, line, end_line, signature, doc)"
                " VALUES (?,?,?,?,?,?,?)",
                [
                    (s.file, s.name, s.kind, s.line, s.end_line, s.signature, s.doc)
                    for f in sorted(data.files.values(), key=lambda e: e.path)
                    for s in sorted(f.symbols, key=lambda x: (x.line, x.name))
                ],
            )
            con.executemany(
                "INSERT INTO refs(src_file, dst_name, dst_file, line) VALUES (?,?,?,?)",
                [(r.src_file, r.dst_name, r.dst_file, r.line) for r in data.refs],
            )
            con.executemany(
                "INSERT INTO fts(name, sig, doc, path, line, kind) VALUES (?,?,?,?,?,?)",
                [
                    (s.name, s.signature, s.doc, s.file, s.line, s.kind)
                    for f in sorted(data.files.values(), key=lambda e: e.path)
                    for s in sorted(f.symbols, key=lambda x: (x.line, x.name))
                ],
            )
            con.commit()
        finally:
            con.close()
        os.replace(tmp, self.db_path)

    # -- incremental helpers -------------------------------------------------

    def file_mtimes(self) -> dict[str, float]:
        """Cached mtimes for incremental updates."""
        if not self.db_path.exists():
            return {}
        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            return dict(con.execute("SELECT path, mtime FROM files"))
        finally:
            con.close()

    # -- incremental mutation (INDEX subsystem) --------------------------------

    def remove_files(self, paths: Iterable[str]) -> None:
        """Remove files plus all their symbols, refs and FTS rows from the cache.

        Missing paths are ignored; a no-op when the database does not exist yet.
        """
        path_list = sorted(set(paths))
        if not path_list or not self.db_path.exists():
            return
        con = sqlite3.connect(self.db_path)
        try:
            with con:
                con.executemany("DELETE FROM files WHERE path = ?", ((p,) for p in path_list))
                con.executemany("DELETE FROM symbols WHERE file = ?", ((p,) for p in path_list))
                con.executemany("DELETE FROM refs WHERE src_file = ?", ((p,) for p in path_list))
                con.executemany("DELETE FROM fts WHERE path = ?", ((p,) for p in path_list))
        finally:
            con.close()

    def upsert_files(self, entries: Iterable[FileEntry], refs: Iterable[Ref] = ()) -> None:
        """Insert or replace the given file entries and the refs originating from them.

        Symbols, FTS rows and refs whose ``src_file`` is one of the entry paths are
        replaced wholesale, so repeated calls stay idempotent. Refs from other files
        are ignored (callers pass refs grouped by their source file). Creates the
        database (with schema) when it does not exist yet.
        """
        entry_list = sorted(entries, key=lambda e: e.path)
        path_set = {e.path for e in entry_list}
        ref_list = sorted(
            (r for r in refs if r.src_file in path_set),
            key=lambda r: (r.src_file, r.line, r.dst_name, r.dst_file),
        )
        if not entry_list and not ref_list:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.db_path)
        try:
            if _fts_outdated(con):
                con.execute("DROP TABLE fts")  # derived index — rebuilt by save()
            con.executescript(_SCHEMA)
            with con:
                con.executemany(
                    "INSERT OR REPLACE INTO files(path, lang, mtime, size) VALUES (?,?,?,?)",
                    [(e.path, e.lang, e.mtime, e.size) for e in entry_list],
                )
                con.executemany("DELETE FROM symbols WHERE file = ?", ((p,) for p in path_set))
                con.executemany(
                    "INSERT INTO symbols(file, name, kind, line, end_line, signature, doc)"
                    " VALUES (?,?,?,?,?,?,?)",
                    [
                        (s.file, s.name, s.kind, s.line, s.end_line, s.signature, s.doc)
                        for e in entry_list
                        for s in sorted(e.symbols, key=lambda x: (x.line, x.name))
                    ],
                )
                con.executemany("DELETE FROM refs WHERE src_file = ?", ((p,) for p in path_set))
                con.executemany(
                    "INSERT INTO refs(src_file, dst_name, dst_file, line) VALUES (?,?,?,?)",
                    [(r.src_file, r.dst_name, r.dst_file, r.line) for r in ref_list],
                )
                con.executemany("DELETE FROM fts WHERE path = ?", ((p,) for p in path_set))
                con.executemany(
                    "INSERT INTO fts(name, sig, doc, path, line, kind) VALUES (?,?,?,?,?,?)",
                    [
                        (s.name, s.signature, s.doc, s.file, s.line, s.kind)
                        for e in entry_list
                        for s in sorted(e.symbols, key=lambda x: (x.line, x.name))
                    ],
                )
        finally:
            con.close()

    # -- lexical search ------------------------------------------------------

    def search(self, query: str, k: int = 20) -> list[Hit]:
        """BM25 search over symbols via FTS5 (lower score = better match)."""
        if not self.db_path.exists():
            return []
        # FTS5 query syntax: quote each token to avoid syntax errors on odd input.
        terms = [t for t in "".join(c if c.isalnum() else " " for c in query).split() if t]
        if not terms:
            return []
        match = " OR ".join(f'"{t}"' for t in terms[:32])
        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            try:
                rows = con.execute(
                    "SELECT path, name, kind, line, bm25(fts) FROM fts WHERE fts MATCH ?"
                    " ORDER BY bm25(fts) LIMIT ?",
                    (match, k),
                ).fetchall()
            except sqlite3.OperationalError:
                # Legacy index without the line/kind columns — degrade gracefully.
                rows = con.execute(
                    "SELECT path, name, '', 0, bm25(fts) FROM fts WHERE fts MATCH ?"
                    " ORDER BY bm25(fts) LIMIT ?",
                    (match, k),
                ).fetchall()
        except sqlite3.OperationalError:
            return []
        finally:
            con.close()
        return [Hit(file=p, name=n, kind=kd, line=ln, score=sc) for p, n, kd, ln, sc in rows]

    def close(self) -> None:  # symmetry for context-manager use
        return None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
