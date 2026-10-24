"""Repository scanner: gitignore-aware walk, binary sniffing, extension→language mapping.

Returns flat file tuples for the parser layer. Deterministic: output is sorted by
repo-relative posix path and never depends on filesystem iteration order.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# (relpath_posix, abs_path, mtime, size, lang)
ScannedFile = tuple[str, str, float, int, str]

#: Languages indexed when the caller does not restrict ``langs``.
DEFAULT_LANGS: tuple[str, ...] = ("py", "ts", "js", "go", "rs")

#: Extension → language. Anything not listed is excluded unless ``txt`` is requested.
EXT_TO_LANG: dict[str, str] = {
    ".py": "py",
    ".pyi": "py",  # stub modules are python source; small documented extension
    ".ts": "ts",
    ".tsx": "ts",
    ".js": "js",
    ".jsx": "js",
    ".mjs": "js",
    ".cjs": "js",
    ".go": "go",
    ".rs": "rs",
    ".h": "c",
    ".c": "c",
    ".cc": "c",
    ".cpp": "c",
}

#: Text documents indexed only when ``"txt"`` is explicitly in ``langs``.
TXT_EXTS: frozenset[str] = frozenset({".txt", ".md", ".rst"})

#: Directories never descended into (spec list plus standard vendored/cache dirs).
SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        "dist",
        "build",
        ".codegraph",
        ".hg",
        ".svn",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".eggs",
        "target",
        ".next",
    }
)

#: Extensions assumed binary without sniffing.
BINARY_EXTS: frozenset[str] = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".ico",
        ".webp",
        ".pdf",
        ".zip",
        ".tar",
        ".gz",
        ".tgz",
        ".bz2",
        ".xz",
        ".7z",
        ".rar",
        ".jar",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".eot",
        ".mp3",
        ".mp4",
        ".avi",
        ".mov",
        ".wav",
        ".flac",
        ".ogg",
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".a",
        ".o",
        ".obj",
        ".lib",
        ".pyc",
        ".pyo",
        ".class",
        ".wasm",
        ".db",
        ".sqlite",
        ".sqlite3",
        ".bin",
        ".iso",
        ".img",
        ".deb",
        ".rpm",
        ".whl",
        ".parquet",
    }
)

#: Files larger than this are skipped (vendored/minified blobs, lockfiles).
MAX_FILE_BYTES = 1_500_000

_SNIFF_BYTES = 8192


@dataclass(frozen=True, slots=True)
class _Rule:
    """One compiled .gitignore pattern (git semantics, negation supported)."""

    base: str  # posix rel dir containing the .gitignore ("" = repo root)
    negate: bool
    dir_only: bool
    anchored: bool  # pattern contains an inner slash → matches from ``base``
    regex: re.Pattern[str]


def _glob_to_regex(pattern: str) -> str:
    """Translate a gitignore glob to a fullmatch regex (supports ``**``)."""
    out: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if pattern.startswith("**", i):
                # "**/" or bare "**" matches across path components.
                if pattern.startswith("**/", i):
                    out.append("(?:[^/]+/)*")
                    i += 3
                else:
                    out.append(".*")
                    i += 2
                continue
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            end = pattern.find("]", i + 1)
            if end == -1:
                out.append(re.escape(c))
                i += 1
            else:
                out.append(pattern[i : end + 1])
                i = end + 1
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


def _parse_gitignore(text: str, base: str) -> list[_Rule]:
    """Parse .gitignore content into rules scoped to directory ``base``."""
    rules: list[_Rule] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        negate = line.startswith("!")
        if negate:
            line = line[1:]
        dir_only = line.endswith("/")
        if dir_only:
            line = line.rstrip("/")
        anchored = "/" in line
        if line.startswith("/"):
            line = line.lstrip("/")
        if not line:
            continue
        # Inner slashes anchor the pattern to the .gitignore's directory
        # (git semantics); "**" segments match across components.
        try:
            regex = re.compile(_glob_to_regex(line))
        except re.error:  # malformed pattern — ignore it, never crash
            logger.debug("unparseable gitignore pattern %r in %s", raw, base or ".")
            continue
        rules.append(
            _Rule(base=base, negate=negate, dir_only=dir_only, anchored=anchored, regex=regex)
        )
    return rules


class Gitignore:
    """Ordered stack of .gitignore rule sets; deeper files override shallower."""

    __slots__ = ("_stack",)

    def __init__(self) -> None:
        self._stack: list[tuple[str, list[_Rule]]] = []

    def push(self, base: str, rules: list[_Rule]) -> None:
        """Add rules from a .gitignore located at posix rel dir ``base``."""
        if rules:
            self._stack.append((base, rules))

    def ignored(self, rel: str, is_dir: bool) -> bool:
        """Return True when posix ``rel`` path is ignored (last match wins)."""
        ignored = False
        for base, rules in self._stack:
            if base:
                if not rel.startswith(base + "/"):
                    continue
                sub = rel[len(base) + 1 :]
            else:
                sub = rel
            for rule in rules:
                if rule.dir_only and not is_dir:
                    continue
                target = sub if rule.anchored else sub.rsplit("/", 1)[-1]
                if rule.regex.fullmatch(target):
                    ignored = not rule.negate
        return ignored

    @staticmethod
    def parse(text: str, base: str = "") -> list[_Rule]:
        """Parse gitignore ``text`` (exposed for tests and reuse)."""
        return _parse_gitignore(text, base)


def lang_for(ext: str, langs: frozenset[str] | set[str]) -> str | None:
    """Map a file extension (with leading dot) to a language, honoring ``langs``."""
    lang = EXT_TO_LANG.get(ext)
    if lang is not None:
        return lang if lang in langs else None
    if ext in TXT_EXTS and "txt" in langs:
        return "txt"
    return None


def _looks_binary(path: Path) -> bool:
    """Null-byte sniff of the first 8KB."""
    try:
        with path.open("rb") as fh:
            return b"\x00" in fh.read(_SNIFF_BYTES)
    except OSError:
        return True


def _load_gitignore(dir_path: Path, base: str) -> list[_Rule]:
    """Load and parse ``<dir>/.gitignore``; missing file → empty rule set."""
    gi = dir_path / ".gitignore"
    try:
        text = gi.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return _parse_gitignore(text, base)


def scan(root: str | Path, langs: list[str] | None = None) -> list[ScannedFile]:
    """Walk ``root`` and return indexable files as ``(relpath, abspath, mtime, size, lang)``.

    Respects .gitignore files (root and nested, negation supported), skips VCS /
    vendored / cache directories, and drops binary files by extension plus a
    null-byte sniff of the first 8KB. Output is sorted by ``relpath``.
    """
    root_path = Path(root).resolve()
    langs_set = set(langs) if langs is not None else set(DEFAULT_LANGS)
    out: list[ScannedFile] = []
    ign = Gitignore()
    ign.push("", _load_gitignore(root_path, ""))
    _walk(root_path, "", ign, langs_set, out)
    out.sort(key=lambda t: t[0])
    logger.debug("scan(%s): %d files (langs=%s)", root_path, len(out), sorted(langs_set))
    return out


def _walk(
    dir_path: Path,
    rel: str,
    ign: Gitignore,
    langs_set: set[str],
    out: list[ScannedFile],
) -> None:
    """Recursive depth-first walk with gitignore stack semantics."""
    try:
        entries = sorted(os.scandir(dir_path), key=lambda e: e.name)
    except OSError as exc:
        logger.warning("cannot list %s: %s", dir_path, exc)
        return
    subdirs: list[tuple[Path, str]] = []
    for entry in entries:
        rel_child = f"{rel}/{entry.name}" if rel else entry.name
        try:
            if entry.is_dir(follow_symlinks=False):
                if entry.name in SKIP_DIRS:
                    continue
                if ign.ignored(rel_child, is_dir=True):
                    continue
                subdirs.append((Path(entry.path), rel_child))
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
            if ign.ignored(rel_child, is_dir=False):
                continue
            lang = lang_for(os.path.splitext(entry.name)[1].lower(), langs_set)
            if lang is None:
                continue
            abspath = Path(entry.path)
            try:
                st = abspath.stat()
            except OSError:
                continue
            if st.st_size > MAX_FILE_BYTES:
                logger.debug("skipping oversized file %s (%d bytes)", rel_child, st.st_size)
                continue
            if os.path.splitext(entry.name)[1].lower() in BINARY_EXTS or _looks_binary(abspath):
                continue
            out.append((rel_child, str(abspath), st.st_mtime, st.st_size, lang))
        except OSError as exc:
            logger.warning("skipping %s: %s", rel_child, exc)

    for child_path, child_rel in subdirs:
        child_ign = ign
        rules = _load_gitignore(child_path, child_rel)
        if rules:
            child_ign = Gitignore()
            child_ign._stack = list(ign._stack)
            child_ign.push(child_rel, rules)
        _walk(child_path, child_rel, child_ign, langs_set, out)


__all__ = ["DEFAULT_LANGS", "Gitignore", "ScannedFile", "lang_for", "scan"]
