"""Deterministic per-file symbol outlines under ``.codegraph/outlines/``.

Outline file for a source ``<relpath>`` lives at ``<out_dir>/<relpath>.md`` (parents
created on demand, ``..`` components stripped):

    # <relpath>
    L<line>-<end>: <kind> <signature>[ — <doc>]

One line per symbol sorted by (line, name); ``method`` entries are indented two
spaces. Output is capped at 200 lines per file. Writes are no-ops when the target
already holds identical content (no churn, mtime preserved).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path

from ..model import FileEntry, IndexData
from .ranker import file_pagerank

LOGGER = logging.getLogger(__name__)

CODEGRAPH_DIR = ".codegraph"
OUTLINES_SUBDIR = "outlines"
MAX_LINES = 200
TRUNCATION_MARK = "... (truncated)"
_SIG_PREFIXES = ("async def ", "def ", "class ")


def _sanitize(rel_path: str) -> str:
    """Normalize a repo-relative path: posix separators, no '', '.', '..' parts."""
    parts = [
        part
        for part in rel_path.replace("\\", "/").split("/")
        if part not in ("", ".", "..")
    ]
    return "/".join(parts) if parts else "_unnamed_"


def outline_posix(rel_path: str) -> str:
    """Repo-relative posix path of the outline file for source *rel_path*."""
    return f"{CODEGRAPH_DIR}/{OUTLINES_SUBDIR}/{_sanitize(rel_path)}.md"


def _outline_text(entry: FileEntry) -> str:
    """Render the outline body for one indexed file (deterministic, \\n-terminated)."""
    lines = [f"# {entry.path}"]
    for sym in sorted(entry.symbols, key=lambda s: (s.line, s.name)):
        sig = " ".join(sym.signature.split())
        for prefix in _SIG_PREFIXES:
            if sig.startswith(prefix):
                sig = sig[len(prefix) :]
                break
        indent = "  " if sym.kind == "method" else ""
        line = f"{indent}L{sym.line}-{sym.end_line}: {sym.kind} {sig}".rstrip()
        if sym.doc:
            line = f"{line} — {' '.join(sym.doc.split())}"
        lines.append(line)
    if len(lines) > MAX_LINES:
        lines = lines[: MAX_LINES - 1] + [TRUNCATION_MARK]
    return "\n".join(lines) + "\n"


def _current_text(path: Path) -> str | None:
    """Content of *path*, or None when absent/unreadable (treated as stale)."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def generate_for(data: IndexData, paths: Iterable[str], out_dir: str | Path) -> list[str]:
    """Write outlines for *paths* into *out_dir*; return out-dir-relative posix paths.

    Paths missing from the index are skipped. Unchanged outlines are not rewritten
    (no-op for mtime and content); they are still reported as present.
    """
    out_root = Path(out_dir)
    written: list[str] = []
    for rel in sorted({str(p) for p in paths}):
        entry = data.files.get(rel)
        if entry is None:
            continue
        rel_out = f"{_sanitize(rel)}.md"
        target = out_root / rel_out
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if _current_text(target) == _outline_text(entry):
                written.append(rel_out)
                continue
            target.write_text(_outline_text(entry), encoding="utf-8")
            written.append(rel_out)
        except OSError:
            LOGGER.warning("could not write outline for %s", rel, exc_info=True)
    return written


def generate_top(data: IndexData, out_dir: str | Path, k: int = 32) -> list[str]:
    """Write outlines for the top-*k* PageRank files; returns paths like generate_for."""
    if not data.files or k <= 0:
        return []
    prior = file_pagerank(data.files, data.refs)
    top = sorted(data.files, key=lambda path: (-prior.get(path, 0.0), path))[:k]
    return generate_for(data, top, out_dir)
