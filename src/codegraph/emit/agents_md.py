"""Hash-stable marked-block writer for AGENTS.md.

The managed block sits between BLOCK_START/BLOCK_END markers. Content outside the markers is
never modified. Writes are no-ops when the body is unchanged (cache-friendliness).
"""

from __future__ import annotations

from pathlib import Path

from ..model import BLOCK_END, BLOCK_START

AGENTS_FILENAME = "AGENTS.md"


def render(body: str) -> str:
    """Render the full marked block (markers + body)."""
    return f"{BLOCK_START}\n{body.rstrip()}\n{BLOCK_END}"


def read_block(path: Path) -> str | None:
    """Return the current block body (without markers) or None if absent."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    start = text.find(BLOCK_START)
    end = text.find(BLOCK_END)
    if start == -1 or end == -1 or end < start:
        return None
    body = text[start + len(BLOCK_START) : end]
    return body.strip("\n")


def _strip_dangling(text: str) -> str:
    """Remove marker occurrences when the file has one marker without its pair.

    A half-pasted block (start without end, or vice versa) must never cause a
    second block to be inserted (that would corrupt the file); the dangling
    marker is removed first so exactly one well-formed block always exists.
    """
    has_start = BLOCK_START in text
    has_end = BLOCK_END in text
    if has_start != has_end:
        return text.replace(BLOCK_START, "").replace(BLOCK_END, "")
    return text


def write_block(path: Path, body: str) -> bool:
    """Insert/replace the managed block at the top of *path*. Returns True if changed.

    - Creates the file (and parents) if missing.
    - Idempotent: writing an identical body is a no-op.
    - Refuses (ValueError) when the file contains malformed markers (one marker
      without its pair): we never silently mutate content we cannot restore.
    """
    path = Path(path)
    block = render(body)
    try:
        existing = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(block + "\n", encoding="utf-8")
        return True

    start = existing.find(BLOCK_START)
    end = existing.find(BLOCK_END)
    if (start == -1) != (end == -1) or (start != -1 and end < start):
        raise ValueError(
            f"{path}: malformed codegraph block (marker without its pair) — "
            "fix or remove the file manually; refusing to guess"
        )
    if start == -1:
        new_text = block + "\n\n" + existing
    else:
        new_text = existing[:start] + block + existing[end + len(BLOCK_END) :]
    if new_text == existing:
        return False
    path.write_text(new_text, encoding="utf-8")
    return True


def remove_block(path: Path) -> bool:
    """Strip the managed block from *path*. Returns True if the file changed.

    Never deletes the file: an empty remainder is written back as an empty file
    (byte-identical restoration, even when the file existed empty before).
    Dangling markers (one without its pair) are cleaned up as best effort.
    """
    path = Path(path)
    try:
        existing = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    start = existing.find(BLOCK_START)
    end = existing.find(BLOCK_END)
    if start == -1 or end == -1 or end < start:
        stripped = _strip_dangling(existing)
        if stripped != existing:
            path.write_text(stripped, encoding="utf-8")
            return True
        return False
    new_text = existing[:start] + existing[end + len(BLOCK_END) :]
    new_text = new_text.lstrip("\n") if start == 0 else new_text
    if new_text != existing:
        path.write_text(new_text, encoding="utf-8")
        return True
    return False
