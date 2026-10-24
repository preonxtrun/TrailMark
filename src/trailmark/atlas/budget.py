"""Byte-budgeted section packer for the atlas block body.

Input: an ordered list of :class:`Section` objects in emit order. Output: one body
string whose UTF-8 size (``util.tokens.byte_len``) is at most the budget. Whole
sections and individual entries are dropped whole by priority — entries are never
split or truncated mid-entry, and the final size is asserted to fit.

Drop priority semantics (lower rank = dropped earlier):

* ``rank``        — drop the whole section at once (all-or-nothing, e.g. GLOSSARY).
* ``item_rank``   — drop non-core items tail-first (e.g. extra MAP/TESTS rows).
* ``core_rank``   — drop core items tail-first once everything else is gone.

Sections that share a tag are *variants* of one section (e.g. PLAN verbose and
compact): only the first surviving one is rendered, so dropping the verbose
variant transparently reveals the compact one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from ..util.tokens import byte_len


@dataclass
class Section:
    """One block section: a tag line plus entry lines, with drop priorities.

    ``tag=""`` marks a header-less pseudo-section (e.g. the block title line).
    ``None`` disables the corresponding drop mode; ``core`` counts items that are
    protected from ``item_rank`` drops (they only fall at ``core_rank``).
    """

    tag: str
    rank: int | None = None
    items: list[str] = field(default_factory=list)
    core: int = 0
    item_rank: int | None = None
    core_rank: int | None = None


def pack(sections: Sequence[Section], budget: int) -> str:
    """Assemble *sections* into one body string of at most *budget* bytes.

    Deterministic: the same input sections and budget always yield the same body.
    """
    limit = max(0, int(budget))
    work = [
        Section(
            tag=s.tag,
            rank=s.rank,
            items=list(s.items),
            core=s.core,
            item_rank=s.item_rank,
            core_rank=s.core_rank,
        )
        for s in sections
    ]
    text, visible = _render(work)
    while byte_len(text) > limit:
        choice = _pick(work, visible)
        if choice is None:  # nothing left to drop; remaining text is empty
            break
        _, index, kind = choice
        if kind == 0:
            work[index].items.clear()
        else:
            work[index].items.pop()
        text, visible = _render(work)
    assert byte_len(text) <= limit, f"packed body exceeds budget: {byte_len(text)} > {limit}"
    return text


def _render(work: Sequence[Section]) -> tuple[str, list[bool]]:
    """Join sections into the body text; also flag which sections are visible.

    A section is visible when it still has items and no earlier section with the
    same tag is visible (variant semantics).
    """
    out: list[str] = []
    visible: list[bool] = []
    seen_tags: set[str] = set()
    for sec in work:
        vis = bool(sec.items) and (not sec.tag or sec.tag not in seen_tags)
        visible.append(vis)
        if not vis:
            continue
        if sec.tag:
            seen_tags.add(sec.tag)
            out.append(sec.tag)
        out.extend(sec.items)
    return "\n".join(out), visible


def _pick(work: Sequence[Section], visible: Sequence[bool]) -> tuple[int, int, int] | None:
    """Pick the next drop candidate: minimum (rank, section index, kind).

    kind 0 = whole section, 1 = extra (non-core) item, 2 = core item. Core items
    are always dropped tail-first; extra items likewise, which preserves the
    emitter's importance order within each section.
    """
    best: tuple[int, int, int] | None = None
    for i, sec in enumerate(work):
        if not visible[i] or not sec.items:
            continue
        if sec.rank is not None:
            cand = (sec.rank, i, 0)
            if best is None or cand < best:
                best = cand
        if sec.item_rank is not None and len(sec.items) > sec.core:
            cand = (sec.item_rank, i, 1)
            if best is None or cand < best:
                best = cand
        if sec.core_rank is not None and sec.items:
            cand = (sec.core_rank, i, 2)
            if best is None or cand < best:
                best = cand
    return best
