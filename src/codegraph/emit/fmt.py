"""Deterministic text-formatting helpers shared by emitters and atlas sections.

All helpers are pure functions of their inputs: no locale, no randomness, no floats.
Budgets are enforced in UTF-8 bytes (see :mod:`codegraph.util.tokens`).
"""

from __future__ import annotations

from ..util.tokens import byte_len

ELLIPSIS = "…"


def truncate(text: str, max_bytes: int) -> str:
    """Shorten *text* to at most *max_bytes* UTF-8 bytes, ellipsizing when cut.

    Never splits a code point; returns *text* unchanged when it already fits.
    Below the ellipsis' own byte size the result is a hard cut without marker.
    """
    if max_bytes <= 0:
        return ""
    if byte_len(text) <= max_bytes:
        return text
    tail = ELLIPSIS if max_bytes >= byte_len(ELLIPSIS) else ""
    limit = max_bytes - byte_len(tail)
    data = text.encode("utf-8")[:limit]
    while data:
        try:
            return data.decode("utf-8") + tail
        except UnicodeDecodeError:
            data = data[:-1]
    return tail[:max_bytes] if byte_len(tail) <= max_bytes else ""


def wrap(text: str, width: int = 76, indent: str = "  ", bullet: str = "- ") -> str:
    """Greedy deterministic word wrap at *width* bytes.

    The first line starts with *bullet*; continuation lines start with *indent*.
    Words longer than the available width are never split mid-word.
    """
    words = text.split()
    if not words:
        return ""
    lines: list[str] = []
    cur = bullet
    has_word = False  # does *cur* already carry a word?
    for word in words:
        cand = cur + word if not has_word else f"{cur} {word}"
        if has_word and byte_len(cand) > width:
            lines.append(cur)
            cur = indent + word  # line now carries one word again
        else:
            cur = cand
            has_word = True
    lines.append(cur)
    return "\n".join(lines)
