"""PLAN section renderer (byte-budgeted) per the SPEC block format.

Full form::

    PLAN
    LEADS (verify, don't trust):
    1. <path>:<line>-<end> — <why>
    OUTLINES: .codegraph/outlines/<rel>.md

Verbosity drops whole units only (SPEC invariant #2): full -> compact (all leads,
no ``why``) -> fewer leads -> header only. ``""`` is returned only when the budget
is below the 120-byte minimum (or there is nothing renderable at all).
"""

from __future__ import annotations

from ..util.tokens import byte_len
from .outlines import outline_posix
from .ranker import Lead

_PLAN_HEADER = "PLAN"
_LEADS_HEADER = "LEADS (verify, don't trust):"
_MIN_BUDGET_BYTES = 120


def _render(leads: list[Lead], *, with_why: bool, count: int | None = None) -> str:
    """Render the PLAN section for the first *count* leads at a given verbosity."""
    shown = leads if count is None else leads[:count]
    lines = [_PLAN_HEADER, _LEADS_HEADER]
    for number, lead in enumerate(shown, 1):
        entry = f"{number}. {lead.path}:{lead.start_line}-{lead.end_line}"
        if with_why and lead.why:
            entry += f" — {lead.why}"
        lines.append(entry)
    lines.extend(f"OUTLINES: {outline_posix(lead.path)}" for lead in shown)
    return "\n".join(lines)


def render_plan(leads: list[Lead], budget_bytes: int) -> str:
    """Render the PLAN section for *leads* within *budget_bytes* (UTF-8, inclusive).

    Never exceeds the budget and never truncates mid-entry. Returns "" only when
    *budget_bytes* is below 120 (or there is nothing renderable at all).
    """
    if budget_bytes < _MIN_BUDGET_BYTES:
        return ""
    header_only = f"{_PLAN_HEADER}\n{_LEADS_HEADER}"
    if not leads:
        return header_only
    full = _render(leads, with_why=True)
    if byte_len(full) <= budget_bytes:
        return full
    compact = _render(leads, with_why=False)
    if byte_len(compact) <= budget_bytes:
        return compact
    for count in range(len(leads) - 1, 0, -1):
        trimmed = _render(leads, with_why=False, count=count)
        if byte_len(trimmed) <= budget_bytes:
            return trimmed
    return header_only
