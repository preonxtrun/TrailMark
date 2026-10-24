"""Index subsystem: scan → parse → resolve → (persist).

Public API (SPEC module signatures):
- :func:`build`  — full index of the repository tree.
- :func:`update` — mtime+size incremental index: unchanged files are reused from the
  cache without re-reading them, deleted files are dropped, changed/new files are
  reparsed.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from ..cache import IndexCache
from ..model import FileEntry, IndexData, Ref, Symbol
from .graph import pagerank
from .parser import parse_files, read_go_module, resolve_refs
from .scanner import DEFAULT_LANGS, scan

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .scanner import ScannedFile

__all__ = ["build", "pagerank", "update"]

logger = logging.getLogger(__name__)


def _norm_langs(langs: Sequence[str] | None) -> list[str]:
    """Normalize a language list (default: all default languages)."""
    if langs is None:
        return list(DEFAULT_LANGS)
    return sorted({str(l).strip().lower() for l in langs if str(l).strip()})


def _name_index(files: dict[str, FileEntry]) -> dict[str, list[str]]:
    """Map every defined symbol name to the sorted list of files defining it."""
    idx: dict[str, set[str]] = defaultdict(set)
    for entry in files.values():
        for sym in entry.symbols:
            idx[sym.name].add(entry.path)
    return {name: sorted(paths) for name, paths in idx.items()}


def _sorted_symbols(symbols: list[Symbol]) -> list[Symbol]:
    return sorted(symbols, key=lambda s: (s.line, s.name))


def _entry(rel: str, lang: str, mtime: float, size: int, symbols: list[Symbol]) -> FileEntry:
    return FileEntry(path=rel, lang=lang, mtime=mtime, size=size, symbols=_sorted_symbols(symbols))


def _assemble(
    root_str: str,
    scanned: Sequence[ScannedFile],
    parses: dict[str, tuple[str, list[Symbol], list[Ref]]],
) -> IndexData:
    """Build a complete IndexData from scanned files plus parse results."""
    files: dict[str, FileEntry] = {}
    refs_by_file: dict[str, list[Ref]] = {}
    lang_of: dict[str, str] = {}
    for rel, _abs, mtime, size, lang in scanned:
        _p_lang, symbols, refs = parses.get(rel, (lang, [], []))
        files[rel] = _entry(rel, lang, mtime, size, symbols)
        refs_by_file[rel] = refs
        lang_of[rel] = lang
    files = dict(sorted(files.items()))
    refs = resolve_refs(
        refs_by_file,
        _name_index(files),
        set(files),
        lang_of,
        read_go_module(root_str),
    )
    return IndexData(root=root_str, files=files, refs=refs)


def build(
    root: str | Path = ".",
    langs: Sequence[str] | None = None,
    workers: int = 0,
    cache: IndexCache | None = None,
) -> IndexData:
    """Fully index the repository at ``root`` and persist to ``cache`` when given.

    Args:
        root: repository root directory (resolved to an absolute path).
        langs: languages to index, e.g. ``["py", "ts"]``; None → all defaults.
        workers: parser process count; 0 → ``min(8, cpu_count)``.
        cache: optional :class:`IndexCache` the result is saved to.

    Returns:
        The complete :class:`IndexData` (files sorted by path, refs sorted).
    """
    started = time.perf_counter()
    root_str = str(Path(root).resolve())
    scanned = scan(root_str, _norm_langs(langs))
    parses = parse_files(root_str, scanned, workers)
    data = _assemble(root_str, scanned, parses)
    if cache is not None:
        cache.save(data)
    elapsed = time.perf_counter() - started
    n_syms = sum(len(e.symbols) for e in data.files.values())
    logger.info(
        "build: %d files, %d symbols, %d refs in %.2fs (root=%s)",
        len(data.files),
        n_syms,
        len(data.refs),
        elapsed,
        root_str,
    )
    return data


def update(
    root: str | Path = ".",
    langs: Sequence[str] | None = None,
    workers: int = 0,
    cache: IndexCache | None = None,
) -> IndexData:
    """Incrementally index ``root``: reuse unchanged files, reparse only deltas.

    A file is reprocessed when it is new, or its ``(mtime, size, lang)`` differs from
    the cached entry; files absent from the current scan are dropped. When no usable
    cache exists this falls back to a full :func:`build`.
    """
    started = time.perf_counter()
    root_str = str(Path(root).resolve())
    langs_list = _norm_langs(langs)
    if cache is None:
        return build(root_str, langs_list, workers, None)
    cached = cache.load()
    if cached is None or cached.root != root_str:
        logger.info("update: no usable cache for %s — full build", root_str)
        return build(root_str, langs_list, workers, cache)

    scanned = scan(root_str, langs_list)
    scan_map = {rel: (abspath, mtime, size, lang) for rel, abspath, mtime, size, lang in scanned}

    changed: list[ScannedFile] = []
    keep: dict[str, FileEntry] = {}
    for rel, (abspath, mtime, size, lang) in scan_map.items():
        old = cached.files.get(rel)
        if old is None or old.mtime != mtime or old.size != size or old.lang != lang:
            changed.append((rel, abspath, mtime, size, lang))
        else:
            keep[rel] = old
    removed = [rel for rel in cached.files if rel not in scan_map]

    parses = parse_files(root_str, changed, workers) if changed else {}
    new_files: dict[str, FileEntry] = {}
    new_refs: dict[str, list[Ref]] = {}
    for rel, _abs, mtime, size, lang in changed:
        _p_lang, symbols, refs = parses.get(rel, (lang, [], []))
        new_files[rel] = _entry(rel, lang, mtime, size, symbols)
        new_refs[rel] = refs

    files = dict(sorted({**keep, **new_files}.items()))

    # Cached refs survive for unchanged files; stale targets (deleted/renamed) are
    # filtered, unresolved module-path refs are re-matched against the fresh index.
    cached_refs: dict[str, list[Ref]] = defaultdict(list)
    for ref in cached.refs:
        if ref.src_file in keep:
            cached_refs[ref.src_file].append(ref)
    cached_refs.update(new_refs)
    refs = resolve_refs(
        cached_refs,
        _name_index(files),
        set(files),
        {rel: entry.lang for rel, entry in files.items()},
        read_go_module(root_str),
    )
    data = IndexData(root=root_str, files=files, refs=refs)

    if removed or changed:
        cache.remove_files([*removed, *new_files])
    if new_files:
        cache.upsert_files(
            list(new_files.values()),
            [r for rel in sorted(new_files) for r in new_refs.get(rel, [])],
        )
    elapsed = time.perf_counter() - started
    n_syms = sum(len(e.symbols) for e in data.files.values())
    logger.info(
        "update: %d files (+%d changed, -%d removed), %d symbols, %d refs in %.2fs",
        len(files),
        len(new_files),
        len(removed),
        n_syms,
        len(refs),
        elapsed,
    )
    return data
