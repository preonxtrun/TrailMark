"""Stigmergic trails: cross-task pheromone memory in ``.trailmark/trails.jsonl``.

Ant-colony style memory for coding sessions (stigmergy): solved tasks deposit
"well-trodden paths" (the files that mattered), failed tasks deposit "dead ends"
(repellents), and future tasks read the accumulated field as a TRAILS section of
the injected block. Two additions over the recorded evidence (arXiv 2609.30725):
signals **evaporate** (the store keeps only the last 32 entries) and failures act
as **negative signals** (files touched only by failed tasks are surfaced as dead
ends to avoid blind retries).

Storage is JSON lines, one entry per task id (latest wins), no timestamps, fixed
key order. All functions are best-effort: a missing or malformed store reads as
empty and never raises (invariant #4), and writes touch only ``.trailmark/**``
(invariant #5). Determinism (invariant #1): the same record calls always produce
byte-identical file content, and ranking uses exact rational weights with stable
``(weight, path)`` tie-breaks — no floats anywhere in output.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import tempfile
from collections.abc import Iterable
from fractions import Fraction
from pathlib import Path
from typing import Any

#: Store location, relative to the repo root.
TRAILS_FILE = ".trailmark/trails.jsonl"

#: Evaporation by truncation: at most the last 32 entries are kept.
MAX_ENTRIES = 32

#: Section caps: top-6 hot files, top-4 dead ends.
HOT_LIMIT = 6
DEAD_END_LIMIT = 4

#: Admissible outcomes; "failed" is the negative (repellent) signal.
OUTCOMES = ("solved", "failed")

#: Header line introducing the dead-end (avoid-retry) rows inside TRAILS.
DEAD_END_HEADER = "DEAD ENDS (avoid blind retries):"

__all__ = [
    "DEAD_END_HEADER",
    "DEAD_END_LIMIT",
    "HOT_LIMIT",
    "MAX_ENTRIES",
    "OUTCOMES",
    "TRAILS_FILE",
    "clear",
    "main",
    "record",
    "section_items",
    "summarize",
]


def _trails_path(root: str) -> Path:
    """Absolute path of the trails store under *root*."""
    return Path(root) / ".trailmark" / "trails.jsonl"


def _load_entries(root: str) -> list[dict[str, Any]]:
    """Parsed entries, oldest first (append order). Missing/malformed store → [].

    Blank and unparseable lines are skipped, as are entries failing the shape
    check (string ``id``, known ``outcome``, list-of-str ``files``) — a store is
    never allowed to crash the pipeline; a later :func:`record` rewrites the
    file and thereby heals damaged lines.
    """
    try:
        raw = _trails_path(root).read_text(encoding="utf-8")
    except OSError:
        return []
    entries: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        task_id = entry.get("id")
        outcome = entry.get("outcome")
        files = entry.get("files")
        if not isinstance(task_id, str) or not task_id:
            continue
        if outcome not in OUTCOMES:
            continue
        if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
            continue
        entries.append({"id": task_id, "outcome": outcome, "files": [f for f in files if f]})
    return entries


def _write_entries(root: str, entries: list[dict[str, Any]]) -> None:
    """Rewrite the store with *entries* (oldest first), atomically.

    The payload is written to a temp file inside ``.trailmark/`` and moved into
    place, so a killed run can never leave a torn store behind. Key order is
    fixed via ``sort_keys`` — same entries always yield identical bytes.
    """
    path = _trails_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(json.dumps(entry, sort_keys=True) + "\n" for entry in entries)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".trails-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)


def record(root: str, task_id: str, files: Iterable[str], outcome: str) -> dict[str, Any]:
    """Append or update one task's trail — one line per task id, latest wins.

    *files* are repo-relative paths that mattered in the task; blanks and
    duplicates are dropped and the rest is sorted for byte-determinism.
    *outcome* must be ``"solved"`` or ``"failed"``. The store keeps at most the
    last :data:`MAX_ENTRIES` entries — older trails evaporate by truncation.
    Re-recording an id moves its trail to the newest position. Returns the
    stored entry.
    """
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}, got {outcome!r}")
    task_id = task_id.strip()
    if not task_id:
        raise ValueError("task id must be a non-empty string")
    clean = sorted({f.strip() for f in files if isinstance(f, str) and f.strip()})
    entry: dict[str, Any] = {"id": task_id, "outcome": outcome, "files": clean}
    entries = [old for old in _load_entries(root) if old["id"] != task_id]
    entries.append(entry)
    _write_entries(root, entries[-MAX_ENTRIES:])
    return entry


def _ranked(root: str) -> tuple[list[tuple[str, int]], list[str]]:
    """Core scoring over the store: (hot as (path, solved_count), dead_ends).

    hot ranks files by recency-weighted solved frequency: an entry *k* positions
    from the newest contributes weight ``1/(k+1)`` to each of its files (exact
    :class:`~fractions.Fraction` arithmetic — no float rounding), ties break by
    path; at most :data:`HOT_LIMIT` files. dead_ends are distinct files appearing
    only in failed entries (never in solved), most-recent-first, ties by path;
    at most :data:`DEAD_END_LIMIT`.
    """
    entries = _load_entries(root)
    total = len(entries)
    weight: dict[str, Fraction] = {}
    solved_count: dict[str, int] = {}
    ever_solved: set[str] = set()
    failed_pos: dict[str, int] = {}
    for index, entry in enumerate(entries):
        pos_from_newest = total - index  # newest entry → 1
        files = dict.fromkeys(entry["files"])  # dedupe, order-preserving
        if entry["outcome"] == "solved":
            w = Fraction(1, pos_from_newest + 1)
            for path in files:
                weight[path] = weight.get(path, Fraction(0)) + w
                solved_count[path] = solved_count.get(path, 0) + 1
                ever_solved.add(path)
        else:  # "failed" — the only other outcome _load_entries admits
            for path in files:
                failed_pos[path] = min(failed_pos.get(path, total + 1), pos_from_newest)
    hot = sorted(weight, key=lambda p: (-weight[p], p))[:HOT_LIMIT]
    dead = sorted(
        (p for p in failed_pos if p not in ever_solved),
        key=lambda p: (failed_pos[p], p),
    )[:DEAD_END_LIMIT]
    return [(path, solved_count[path]) for path in hot], dead


def summarize(root: str) -> tuple[list[str], list[str]]:
    """(hot, dead_ends) from the store at *root*.

    hot — up to :data:`HOT_LIMIT` files ranked by recency-weighted solved
    frequency (recent solved tasks count more), ties by path. dead_ends — up to
    :data:`DEAD_END_LIMIT` distinct files appearing ONLY in failed entries,
    most-recent-first, ties by path. Missing/empty store → ``([], [])``.
    """
    hot, dead = _ranked(root)
    return [path for path, _count in hot], dead


def section_items(root: str) -> list[str]:
    """TRAILS section body items for :func:`trailmark.atlas.assemble`.

    The atlas owns placement (emit order) and the drop rank; this owns the row
    format. Empty when the store has no usable entries — the caller must then
    append nothing, keeping a repo without trails byte-identical.
    """
    hot, dead = _ranked(root)
    if not hot and not dead:
        return []
    items = [f"- {path} — well-trodden ({count} solved tasks)" for path, count in hot]
    if dead:
        items.append(DEAD_END_HEADER)
        items.extend(f"- {path} — dead end (failed tasks)" for path in dead)
    return items


def clear(root: str) -> bool:
    """Delete the trails store at *root*. Returns True when a file was removed."""
    path = _trails_path(root)
    existed = path.exists()
    path.unlink(missing_ok=True)
    return existed


def _build_parser() -> argparse.ArgumentParser:
    """argparse parser for the ``python -m trailmark.trails`` entry point."""
    parser = argparse.ArgumentParser(
        prog="python -m trailmark.trails",
        description="Stigmergic trail memory: record task file sets; trails steer future tasks.",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="{record,clear}")
    rec = sub.add_parser("record", help="append or update one task's trail (latest wins per id)")
    rec.add_argument("--root", default=".", help="repository root (default: .)")
    rec.add_argument("--id", dest="task_id", required=True, help="task id (one trail line per id)")
    rec.add_argument("--files", required=True, help="comma-separated repo-relative paths")
    rec.add_argument("--outcome", required=True, choices=OUTCOMES)
    rec.add_argument("--json", action="store_true", help="print machine-readable JSON")
    clr = sub.add_parser("clear", help="delete the trails store")
    clr.add_argument("--root", default=".", help="repository root (default: .)")
    clr.add_argument("--json", action="store_true", help="print machine-readable JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry: ``python -m trailmark.trails {record,clear}``; 0 on success, 2 on bad input."""
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "record":
            files = [part.strip() for part in str(args.files).split(",")]
            entry = record(args.root, str(args.task_id), files, str(args.outcome))
            payload = {
                "recorded": True,
                "id": entry["id"],
                "outcome": entry["outcome"],
                "files": entry["files"],
                "entries": len(_load_entries(args.root)),
            }
            if args.json:
                print(json.dumps(payload, sort_keys=True))
            else:
                print(
                    f"recorded {entry['id']} ({entry['outcome']}, {len(entry['files'])} files)"
                    f" — {payload['entries']} trail entries"
                )
        else:
            removed = clear(args.root)
            if args.json:
                print(json.dumps({"cleared": True, "removed": removed}, sort_keys=True))
            else:
                print("cleared trails" if removed else "no trails file")
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
