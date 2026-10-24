"""FOCUS subsystem: (IndexData, issue) -> ranked leads -> PLAN section + outlines.

Public API: :func:`rank`, :class:`Lead`, :func:`render_plan`, :func:`generate_top`,
:func:`generate_for`, :func:`issue_tokens`, :func:`file_pagerank`. The CLI handler
(:mod:`trailmark.focus.cli_focus`) is imported lazily by ``trailmark.cli`` per SPEC.
"""

from .outlines import generate_for, generate_top, outline_posix
from .plan import render_plan
from .ranker import Lead, file_pagerank, issue_tokens, rank

__all__ = [
    "Lead",
    "file_pagerank",
    "generate_for",
    "generate_top",
    "issue_tokens",
    "outline_posix",
    "rank",
    "render_plan",
]
