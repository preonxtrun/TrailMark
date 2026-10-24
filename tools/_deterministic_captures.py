"""Deterministic tree-sitter capture ordering for the Python oracle tools.

WHY THIS EXISTS (ref-count drift diagnosis, psf/requests clone):

py-tree-sitter's ``QueryCursor.captures()`` — the dict-of-lists API used by
``codegraph.index.parser._extract_refs`` — returns captured nodes in a
per-parse-random order: the order is stable for one tree object, but differs
between two parses of the same bytes, between processes, and is NOT stabilised
by ``PYTHONHASHSEED`` (observed on tree-sitter 0.25.x and 0.26.0).

``_extract_refs`` consumes that order and stops at ``MAX_REFS_PER_FILE``
(2000 raw refs). For files above the cap — on the requests clone exactly one
file, ``tests/test_requests.py`` with 5588 identifier captures — the retained
subset is random, so the Python index drops a different handful of real
identifier refs on every run (final ref counts observed: 1887, 1900, 1911;
the Rust port is deterministic at 1921). Below the cap the extracted SET is
order-independent, which is why small fixtures never exposed this.

The reference's documented intent (parser docstring: "imports first (highest
value), then identifier occurrences, capped") is source order. Sorting every
capture list by ``(start_byte, end_byte)`` restores that intent. Calling
:func:`apply` from an oracle tool pins the order WITHOUT touching ``src/``:
capture sets, symbols (sorted downstream anyway) and resolution semantics are
unchanged; only the above-cap subset selection becomes deterministic and equal
to the Rust implementation's document-order behaviour.

If ``src/codegraph/index/parser.py`` is ever fixed to sort captures itself,
this shim becomes redundant and can be dropped.
"""

from __future__ import annotations

_APPLIED = "_codegraph_deterministic_captures"


def _node_order(node) -> tuple[int, int]:
    return (node.start_byte, node.end_byte)


def apply() -> None:
    """Make ``QueryCursor.captures()`` return document-ordered node lists.

    Idempotent and best-effort: if tree_sitter is unavailable the shim is a
    no-op (the parser degrades to its regex fallback anyway).
    """
    try:
        from tree_sitter import QueryCursor
    except Exception:  # noqa: BLE001 — oracle tooling must never crash on import
        return
    if getattr(QueryCursor.captures, _APPLIED, False):
        return
    original = QueryCursor.captures

    def captures(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if isinstance(result, dict):
            for nodes in result.values():
                nodes.sort(key=_node_order)
        return result

    setattr(captures, _APPLIED, True)
    QueryCursor.captures = captures
