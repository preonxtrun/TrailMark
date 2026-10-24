"""Shared data contracts for codegraph. Frozen interfaces — build against these."""

from __future__ import annotations

from dataclasses import dataclass, field

# Markers delimiting the managed block inside AGENTS.md.
BLOCK_START = "<!-- codegraph:start v1 -->"
BLOCK_END = "<!-- codegraph:end -->"

# Default hard cap for the injected block body, in bytes.
# 3072 ≈ 6× cheaper per byte than a mid-run file read it displaces
# (block content replays from turn 0; a turn-5 read replays from turn 5),
# and far below the prose-overview trap (ETH: +20% cost at 60K tokens).
DEFAULT_BUDGET_BYTES = 3072


@dataclass(frozen=True, slots=True)
class Symbol:
    """A definition extracted from one file. Lines are 1-based, inclusive."""

    file: str  # repo-relative posix path
    name: str
    kind: str  # "def" | "class" | "method" | "var" | ...
    line: int
    end_line: int
    signature: str = ""
    doc: str = ""  # first line of docstring, trimmed


@dataclass(frozen=True, slots=True)
class Ref:
    """A reference edge between a source file and a (resolved) target name/file."""

    src_file: str  # repo-relative posix path
    dst_name: str
    dst_file: str  # resolved repo-relative path, "" if unresolved
    line: int


@dataclass(slots=True)
class FileEntry:
    path: str  # repo-relative posix path
    lang: str  # "py" | "ts" | "js" | "go" | "rs" | "txt" (fallback)
    mtime: float
    size: int
    symbols: list[Symbol] = field(default_factory=list)


@dataclass(slots=True)
class IndexData:
    """Full repository index: per-file symbols plus resolved reference edges."""

    root: str
    files: dict[str, FileEntry] = field(default_factory=dict)
    refs: list[Ref] = field(default_factory=list)

    def symbols(self) -> list[Symbol]:
        return [s for f in self.files.values() for s in f.symbols]
