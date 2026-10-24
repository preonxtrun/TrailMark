"""ATLAS: IndexData → map / test-graph / glossary / rules → byte-budgeted block body.

The public entry point is :func:`assemble` (SPEC-frozen signature): it returns the
block BODY (no markers); the caller writes it via ``emit.write_block``. Output is
deterministic — same (repo state, issue text, cli, budget) ⇒ byte-identical body:
every list is sorted with total tie-breaks, pagerank scores are used for ordering
only, and no timestamps or floats are ever emitted (invariant #1).
"""

from __future__ import annotations

from typing import Any

from ..model import IndexData
from . import budget, builder, glossary, rules, testgraph
from .budget import Section, pack

#: First line of every block body (SPEC block format).
TITLE = "# CODEGRAPH ATLAS — auto-generated, do not edit inside markers"

# Drop ranks (lower = dropped earlier). SPEC drop order: GLOSSARY → PLAN
# (verbose → compact → omit) → extra TESTS rows → extra MAP rows. Beyond that,
# only where the full rules text cannot coexist with the guaranteed floors,
# RULES compacts to its one-line variant; header, ≥3 MAP rows and the TESTS
# runner row survive at any budget ≥ 512 bytes.
R_GLOSSARY = 0
R_PLAN_VERBOSE = 10
R_PLAN_COMPACT = 20
R_TESTS_ITEM = 30
R_MAP_ITEM = 40
R_RULES_FULL = 50
R_MAP_CORE = 60
R_TESTS_CORE = 70
R_RULES_COMPACT = 80
R_HEADER = 90

#: Rows protected from budget drops (invariant #2 floors).
MAP_CORE_ROWS = 3
TESTS_CORE_ROWS = 1

__all__ = [
    "TITLE",
    "Section",
    "assemble",
    "budget",
    "builder",
    "glossary",
    "pack",
    "rules",
    "testgraph",
]


def _plan_from_leads(leads: list[Any]) -> tuple[list[str], list[str]]:
    """(verbose, compact) PLAN items from pre-ranked focus leads.

    Single source of truth for leads is :func:`codegraph.focus.ranker.rank` (the
    BM25/PageRank hybrid); this adapter only re-formats. Duck-typed on purpose —
    no import of ``codegraph.focus`` (the dependency direction is cli → atlas,
    cli → focus). Item format mirrors :func:`builder.build_plan`.
    """
    lines: list[str] = []
    top_ref = ""
    for i, lead in enumerate(leads[:5], 1):
        line_range = f"{lead.start_line}-{lead.end_line}"
        why = getattr(lead, "why", "") or "path match"
        lines.append(f"{i}. {lead.path}:{line_range} — {why}")
        top_ref = top_ref or lead.path
    if not lines:
        return (
            [
                "LEADS (verify, don't trust):",
                "- (no lexical matches in index — start from MAP hot files)",
            ],
            ["(no lexical matches — start from MAP hot files)"],
        )
    verbose = [
        "LEADS (verify, don't trust):",
        *lines,
        f"OUTLINES: .codegraph/outlines/{top_ref}.md",
    ]
    compact = [lines[0]]
    return verbose, compact


def assemble(
    data: IndexData,
    issue: str | None,
    cli: str,
    budget: int,
    leads: list[Any] | None = None,
) -> str:
    """Build the deterministic atlas block BODY (no markers) within *budget* bytes.

    Sections, in emit order: title, RULES (full text, with a one-line compact
    fallback), MAP (directory roles + pagerank-ordered hot files), TESTS (runner
    + src→test map), PLAN (only when *issue* is given, verbose + compact
    variants), GLOSSARY. When *leads* (focus ``Lead`` objects, pre-ranked by
    ``focus.ranker.rank``) are supplied they drive PLAN; otherwise PLAN falls
    back to internal issue-keyword scoring. The byte budget is enforced by
    :func:`pack` with the SPEC drop order; entries are never split.
    """
    pr = builder.pagerank_scores(data)
    sections: list[Section] = [Section(tag="", rank=R_HEADER, items=[TITLE])]
    sections.append(
        Section(tag="RULES", rank=R_RULES_FULL, items=rules.flight_rules(cli).split("\n"))
    )
    sections.append(
        Section(tag="RULES", rank=R_RULES_COMPACT, items=[f"- {rules.compact_rules(cli)}"])
    )
    sections.append(
        Section(
            tag="MAP",
            items=builder.build_map(data, pr),
            core=MAP_CORE_ROWS,
            item_rank=R_MAP_ITEM,
            core_rank=R_MAP_CORE,
        )
    )
    sections.append(
        Section(
            tag="TESTS",
            items=testgraph.build_tests(data, pr),
            core=TESTS_CORE_ROWS,
            item_rank=R_TESTS_ITEM,
            core_rank=R_TESTS_CORE,
        )
    )
    if issue:
        if leads:
            verbose, compact = _plan_from_leads(leads)
        else:
            verbose, compact = builder.build_plan(data, issue, pr)
        sections.append(Section(tag="PLAN", rank=R_PLAN_VERBOSE, items=verbose))
        sections.append(Section(tag="PLAN", rank=R_PLAN_COMPACT, items=compact))
    sections.append(
        Section(
            tag="GLOSSARY",
            rank=R_GLOSSARY,
            items=glossary.build_glossary(data),
        )
    )
    return pack(sections, budget)
