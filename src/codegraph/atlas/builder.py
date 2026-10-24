"""MAP section builder: directory roles + pagerank-ordered hot files with line anchors.

Row shapes (SPEC block format and deliverable example):

* first row of a top-level directory carries the dir role::

    - <dir>/ — <role>; hot: <path>:<line> <Sym()> — <desc>
* further rows are plain hot-file rows::

    - <path>:<line> <Sym()> — <desc>

Files are ordered by file-level pagerank (alphabetical fallback when the index
module is unavailable); 3..10 rows. The line anchor is the file's most-referenced
symbol (ties by line, then name). Pagerank scores are used for ordering only —
never emitted. Row text is byte-capped at format time (description dropped first,
then ellipsis) so the guaranteed MAP floor stays well inside a 512-byte budget.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..emit.fmt import truncate
from ..model import FileEntry, IndexData, Symbol
from ..util.tokens import byte_len
from .testgraph import is_test_file

#: Display caps (bytes) and row bounds.
ROW_CAP = 84
ROLE_CAP = 36
DESC_CAP = 28
MAX_ROWS = 10
MIN_ROWS = 3

#: Directory-name → role table used when no README/package docstring exists.
_ROLE_NAMES: dict[str, str] = {
    "api": "API layer",
    "atlas": "atlas section builders",
    "bench": "benchmarks",
    "cli": "CLI entrypoints",
    "core": "core logic",
    "docs": "documentation",
    "emit": "output emitters",
    "focus": "focus planning",
    "index": "code indexer",
    "lib": "library code",
    "models": "data models",
    "scripts": "scripts & tooling",
    "src": "source",
    "test": "test suite",
    "tests": "test suite",
    "tools": "scripts & tooling",
    "util": "utilities",
    "utils": "utilities",
}

_README_SKIP = re.compile(r"^\s*(?:\[!|<|\||={3,}|-{3,})")
_MD_PREFIX = re.compile(r"^\s*#+\s*")
_DOCSTRING = re.compile(r'("""|\'\')\s*\n?\s*(.+)', re.DOTALL)


def pagerank_scores(data: IndexData) -> dict[str, float] | None:
    """File-level pagerank via ``codegraph.index.graph`` (lazy import).

    Returns None — and callers fall back to plain alphabetical ordering — when
    the index module is unavailable or the call fails for any reason, so the
    atlas stays fully functional standalone.
    """
    try:
        from codegraph.index.graph import pagerank  # lazy: index/ may be absent
    except Exception:  # noqa: BLE001 — any import failure falls back to alphabetical
        return None
    try:
        scores = pagerank(data.files, data.refs, damping=0.85, iters=30)
    except TypeError:
        try:  # tolerate implementations taking a path list instead of the file map
            scores = pagerank(sorted(data.files), data.refs, damping=0.85, iters=30)
        except Exception:  # noqa: BLE001 — ordering is best-effort, never fatal
            return None
    except Exception:  # noqa: BLE001 — ordering is best-effort, never fatal
        return None
    try:
        return {path: float(scores.get(path, 0.0)) for path in data.files}
    except Exception:  # noqa: BLE001 — ordering is best-effort, never fatal
        return None


def dir_roles(data: IndexData) -> dict[str, str]:
    """Top-level directory → inferred role text.

    Heuristic priority per directory: README first meaningful line, then the
    package ``__init__.py`` docstring first line, then a name table, then
    ``"code"``. Root-level files (no directory) group under ``""`` and use the
    repo root README. All disk reads are defensive; output is deterministic.
    """
    root = Path(data.root) if data.root else None
    dirs = sorted({p.split("/", 1)[0] if "/" in p else "" for p in data.files})
    return {d: _role_for(root, d) for d in dirs}


def _role_for(root: Path | None, directory: str) -> str:
    if root is not None:
        base = root / directory if directory else root
        for name in ("README.md", "README.rst", "README.txt", "readme.md"):
            line = _first_readme_line(base / name)
            if line:
                return truncate(line, ROLE_CAP)
        if directory:
            line = _docstring_line(base / "__init__.py")
            if line:
                return truncate(line, ROLE_CAP)
    return _ROLE_NAMES.get(directory, "code")


def _first_readme_line(path: Path) -> str:
    """First meaningful README line (badges/tables/HTML skipped, markdown stripped)."""
    try:
        if not path.is_file():
            return ""
        head = path.read_text(encoding="utf-8", errors="replace")[:4096]
    except OSError:
        return ""
    for raw in head.splitlines():
        line = raw.strip()
        if not line or line.startswith("<!--") or _README_SKIP.match(line):
            continue
        line = _MD_PREFIX.sub("", line).strip()
        line = line.strip("*_`#").rstrip(":;,.").strip()
        return line if len(line) >= 3 else ""
    return ""


def _docstring_line(path: Path) -> str:
    """First line of the module docstring, or "" (best-effort, defensive)."""
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:2048]
    except OSError:
        return ""
    match = _DOCSTRING.search(head)
    if not match:
        return ""
    line = match.group(2).split("\n", 1)[0].strip().strip("\"'").strip()
    return line if len(line) >= 3 else ""


def ref_counts(data: IndexData) -> dict[tuple[str, str], int]:
    """Incoming resolved-reference counts per (dst_file, dst_name)."""
    counts: dict[tuple[str, str], int] = {}
    for ref in data.refs:
        if ref.dst_file:
            key = (ref.dst_file, ref.dst_name)
            counts[key] = counts.get(key, 0) + 1
    return counts


def anchor_symbol(entry: FileEntry, counts: dict[tuple[str, str], int]) -> Symbol | None:
    """The file's highest-pagerank-referenced symbol (most incoming refs).

    Ties break by line, then name; files without symbols have no anchor.
    """
    if not entry.symbols:
        return None
    return min(
        entry.symbols,
        key=lambda s: (-(counts.get((entry.path, s.name), 0)), s.line, s.name),
    )


def sym_label(sym: Symbol) -> str:
    """Display form: ``name()`` for callables, bare name otherwise."""
    if "(" in sym.name:
        return sym.name
    if sym.kind in {"def", "method", "function", "fn"}:
        return f"{sym.name}()"
    return sym.name


def hot_files(data: IndexData, pr: dict[str, float] | None) -> list[str]:
    """3..10 hot source files, pagerank-ordered (alphabetical fallback).

    Test files are excluded (they belong to TESTS). Files without symbols carry no
    line anchor, so they only fill up to MIN_ROWS when symbol-bearing files fall
    short of the floor.
    """
    src = sorted(p for p in data.files if not is_test_file(p))
    with_syms = [p for p in src if data.files[p].symbols]
    fillers = [p for p in src if not data.files[p].symbols]
    scores = pr or {}
    if scores:
        ordered = sorted(with_syms, key=lambda p: (-scores.get(p, 0.0), p))
    else:
        ordered = sorted(with_syms)  # pagerank unavailable: plain alphabetical
    hot = ordered[:MAX_ROWS]
    if len(hot) < MIN_ROWS:
        hot += fillers[: MIN_ROWS - len(hot)]
    return hot


def build_map(data: IndexData, pr: dict[str, float] | None) -> list[str]:
    """MAP rows: first row per top-level dir carries the dir role (SPEC shape)."""
    roles = dir_roles(data)
    counts = ref_counts(data)
    rows: list[str] = []
    seen: set[str] = set()
    for path in hot_files(data, pr):
        entry = data.files[path]
        sym = anchor_symbol(entry, counts)
        base = f"{path}:{sym.line if sym else 1}"
        if sym:
            base += f" {sym_label(sym)}"
        first = path.split("/", 1)[0] if "/" in path else ""
        if first not in seen:
            seen.add(first)
            display = f"{first}/" if first else "./"
            row = f"- {display} — {roles.get(first, 'code')}; hot: {base}"
        else:
            row = f"- {base}"
        desc = _desc(sym)
        if desc:
            row += f" — {desc}"
        rows.append(_cap_row(row))

    # Tiny repos: top up to MIN_ROWS with role-only rows for the remaining dirs,
    # ordered by the best pagerank among their files, then by name.
    if len(rows) < MIN_ROWS:
        scores = pr or {}
        remaining = [d for d in sorted(roles) if d not in seen]

        def best_of(directory: str) -> tuple[float, str]:
            contained = [
                p for p in data.files
                if (p.split("/", 1)[0] if "/" in p else "") == directory
            ]
            return (-max((scores.get(p, 0.0) for p in contained), default=0.0), directory)

        for directory in sorted(remaining, key=best_of):
            display = f"{directory}/" if directory else "./"
            rows.append(f"- {display} — {roles.get(directory, 'code')}")
            if len(rows) >= MIN_ROWS:
                break
    return rows


def build_plan(
    data: IndexData, issue: str, pr: dict[str, float] | None
) -> tuple[list[str], list[str]]:
    """(verbose, compact) PLAN items derived deterministically from the issue text.

    Files are scored by occurrences of the issue's keywords in symbol names,
    docs, signatures and paths; the top 5 become numbered leads with the anchor
    symbol's line range. The compact variant is the single top lead (or a "no
    matches" hint). *pr* is accepted for signature symmetry with the other
    builders; lead order is fully determined by (score, path).
    """
    terms = issue_terms(issue)
    matched_files: dict[str, list[str]] = {}
    file_scores: dict[str, int] = {}
    for path, entry in data.files.items():
        body = " ".join(
            f"{s.name} {s.doc or ''} {s.signature or ''}" for s in entry.symbols
        ).lower()
        blob = f"{path.lower()} {body}"
        score = 0
        matched: list[str] = []
        for term in terms:
            hits = blob.count(term)
            if hits:
                matched.append(term)
                score += hits * 2 + (2 if term in path.lower() else 0)
        if score > 0:
            matched_files[path] = matched
            file_scores[path] = score

    counts = ref_counts(data)
    leads: list[str] = []
    top_ref = ""
    for rank, path in enumerate(sorted(file_scores, key=lambda p: (-file_scores[p], p)), 1):
        if len(leads) >= 5:
            break
        sym = anchor_symbol(data.files[path], counts)
        line_range = f"{sym.line}-{sym.end_line}" if sym else "1-1"
        why = truncate(", ".join(matched_files[path][:3]) or "path match", DESC_CAP * 2)
        leads.append(f"{rank}. {path}:{line_range} — matches: {why}")
        top_ref = top_ref or path

    if not leads:
        verbose = [
            "LEADS (verify, don't trust; one ranged read each, then act):",
            "- (no lexical matches in index — start from MAP hot files)",
        ]
        compact = ["(no lexical matches — start from MAP hot files)"]
        return verbose, compact

    verbose = [
        "LEADS (verify, don't trust; one ranged read each, then act):",
        *leads,
        f"OUTLINES: .codegraph/outlines/{top_ref}.md",
    ]
    compact = [leads[0]]
    return verbose, compact


def issue_terms(issue: str, limit: int = 16) -> list[str]:
    """Deterministic keyword list from the issue text: by frequency, then term.

    Prose and identifier stopwords are removed; terms shorter than 4 chars are
    ignored; at most *limit* terms are kept. The only task input ever used is
    this issue text (invariant #3).
    """
    from .glossary import STOPWORDS  # local import keeps glossary the term authority

    freq: dict[str, int] = {}
    for word in re.findall(r"[a-z0-9_]{4,}", issue.lower()):
        word = word.strip("_-")
        if len(word) < 4 or word in _PROSE_STOP or word in STOPWORDS:
            continue
        freq[word] = freq.get(word, 0) + 1
    ranked = sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return [word for word, _count in ranked]


_PROSE_STOP = frozenset(
    """about after also any been before being both could does done during each else
    every from have here into just like more most must need needs only other over
    should since some such sure than that their them then there these they this
    those under want well were what when where which while will with without would
    your yours able across actually allow allows always another anything apply
    based because become been before begin below between bring build built call
    come common crashes create created disable done down due each either error
    errors ensure ensuring even ever every expected fails failure failed false
    file files find fine first fix fixed following force form found from further
    get give given goes gone good got great happen happens hard have having help
    here hold hook hope however idea implement initial inside instead issue issues
    itself keep kept kind know known last late later least left less let like likely
    line lines little long look lot made main make makes many maybe mean means might
    mine more move much must name near need never next none note nothing now null
    occur once ones open order other others out output over own pass passed passing
    patch place please point possible present previously prior probably problem
    provide put reads real really reason related remain remove repeat replaced
    report required result right runs running same scenario seen seems set several
    shown side similar since small some something sometimes soon specific started
    still stop take taken test tests than thats thing things think third three
    through time times today together too took toward true try trying turn twice
    type typical under until upon usage use used uses using usually various very
    wait want wants was way ways well went what whats when where whether which
    while whole whose work works would yet you your""".split()  # noqa: SIM905 — word list reads best as prose
)


def _desc(sym: Symbol | None) -> str:
    """Row description: doc line, else collapsed signature (byte-capped)."""
    if sym is None:
        return ""
    text = " ".join((sym.doc or sym.signature or "").split())
    return truncate(text, DESC_CAP) if text else ""


def _cap_row(row: str, cap: int = ROW_CAP) -> str:
    """Clamp a MAP row to *cap* bytes: drop the description first, then ellipsize.

    This is format-time capping (identical regardless of budget), never a
    budget-time mid-entry truncation — the row is always a well-formed entry.
    """
    if byte_len(row) <= cap:
        return row
    base, sep, desc = row.rpartition(" — ")
    if sep and byte_len(base) <= cap:
        return base
    if sep:
        room = cap - byte_len(base) - byte_len(" — ")
        if room > 0:
            return f"{base} — {truncate(desc, room)}"
    return truncate(row, cap)
