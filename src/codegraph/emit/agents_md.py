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


def write_block(path: Path, body: str) -> bool:
    """Insert/replace the managed block at the top of *path*. Returns True if changed.

    - Creates the file (and parents) if missing.
    - Idempotent: writing an identical body is a no-op.
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
    if start == -1 or end == -1 or end < start:
        new_text = block + "\n\n" + existing
    else:
        new_text = existing[:start] + block + existing[end + len(BLOCK_END) :]
    if new_text == existing:
        return False
    path.write_text(new_text, encoding="utf-8")
    return True


def remove_block(path: Path) -> bool:
    """Strip the managed block from *path*. Returns True if the file changed."""
    path = Path(path)
    try:
        existing = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    start = existing.find(BLOCK_START)
    end = existing.find(BLOCK_END)
    if start == -1 or end == -1 or end < start:
        return False
    new_text = (existing[:start] + existing[end + len(BLOCK_END) :]).lstrip("\n")
    if new_text.strip():
        path.write_text(new_text, encoding="utf-8")
    else:
        path.unlink()
    return True
