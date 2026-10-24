"""ATLAS: IndexData → map / test-graph / glossary / rules → byte-budgeted block body.

The public entry point is :func:`assemble` (SPEC-frozen signature): it returns the
block BODY (no markers); the caller writes it via ``emit.write_block``. Output is
deterministic — same (repo state, issue text, cli, budget) ⇒ byte-identical body:
every list is sorted with total tie-breaks, pagerank scores are used for ordering
only, and no timestamps or floats are ever emitted (invariant #1).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import trails
from ..emit import fmt
from ..model import IndexData
from ..util.tokens import byte_len
from . import budget, builder, glossary, rules, testgraph
from .budget import Section, pack

#: First line of every block body (SPEC block format).
TITLE = "# TRAILMARK ATLAS — auto-generated, do not edit inside markers"

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

    Single source of truth for leads is :func:`trailmark.focus.ranker.rank` (the
    BM25/PageRank hybrid); this adapter only re-formats. Duck-typed on purpose —
    no import of ``trailmark.focus`` (the dependency direction is cli → atlas,
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
                "LEADS (verify, don't trust; one ranged read each, then act):",
                "- (no lexical matches in index — start from MAP hot files)",
            ],
            ["(no lexical matches — start from MAP hot files)"],
        )
    verbose = [
        "LEADS (verify, don't trust; one ranged read each, then act):",
        *lines,
        f"OUTLINES: .trailmark/outlines/{top_ref}.md",
    ]
    compact = [lines[0]]
    return verbose, compact


#: Drop rank for user-authored environment facts (profile file): after GLOSSARY,
#: before PLAN — facts are cheap and high-value on lab/cluster workloads.
R_LAB = 5

#: Drop rank for cross-task trail memory (stigmergy, ``.trailmark/trails.jsonl``):
#: after LAB (5), before PLAN verbose (10) — recency hints die before planning.
R_TRAILS = 7


def load_profile(root: str) -> tuple[list[str], list[str]]:
    """(facts, extra_rules) from an optional ``.trailmark/profile.txt``.

    Zero-dependency line format — one entry per line::

        fact: partitions: gpu(a100, 24h), cpu; submit with sbatch -A <account>
        rule: check sacct once per job; never poll squeue in a loop

    The profile carries user-authored static facts and behavioral rules for
    workloads where an index doesn't help: greenfield projects, experiment
    loops, Slurm/GPU clusters (the evidence-backed "developer-designed skills"
    mechanism — arXiv 2609.30725).
    """
    facts: list[str] = []
    extra_rules: list[str] = []
    path = Path(root) / ".trailmark" / "profile.txt"
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("fact:") and len(line) > 5:
                facts.append(line[5:].strip())
            elif line.startswith("rule:") and len(line) > 5:
                extra_rules.append(line[5:].strip())
    except OSError:
        return [], []
    return facts[:8], extra_rules[:6]


#: Drop rank for inline lead contracts + surgical snippets: high displacement value —
#: they replace early full-file reads (the highest replay-damage content in a run).
R_CONTRACTS = 32
R_SNIPPETS = 31


def _contracts(data: IndexData, leads: list[Any], per_file_cap: int = 280) -> list[str]:
    """Compact signature outlines for the top lead files (verify with one ranged read)."""
    items: list[str] = []
    seen: set[str] = set()
    for lead in leads[:3]:
        path = getattr(lead, "path", "")
        if not path or path in seen:
            continue
        seen.add(path)
        entry = data.files.get(path)
        if entry is None:
            continue
        total_lines = max((s2.end_line for s2 in entry.symbols), default=0)
        items.append(f"{path} ({total_lines} lines):")
        used = byte_len(items[-1]) + 1
        syms = sorted(entry.symbols, key=lambda s2: (s2.line, s2.name))
        # linemap: spread markers ~every 100 lines so the FIRST ranged read lands right
        last_marked = -1000
        for sym in syms:
            if sym.line - last_marked < 100 and used > 0:
                continue
            line = f"  L{sym.line} {sym.kind} {sym.signature}".rstrip()
            if byte_len(line) + used > per_file_cap:
                break
            items.append(line)
            used += byte_len(line) + 1
            last_marked = sym.line
        if used >= per_file_cap:
            break
    return items


def _extract_snippets(data: IndexData, leads: list[Any], per_lead_cap: int = 1400) -> list[str]:
    """Extract the best-matching symbol's BODY for the top leads (surgical context).

    Displaces the agent's own early full-file reads — the highest replay-damage
    content in a run (damage = new tokens × remaining turns). Deterministic;
    reads only (G6 purity); capped per lead.
    """
    items: list[str] = []
    seen: set[str] = set()
    for lead in leads[:3]:
        path = getattr(lead, "path", "")
        if not path or path in seen:
            continue
        seen.add(path)
        entry = data.files.get(path)
        if entry is None:
            continue
        start = max(1, int(getattr(lead, "start_line", 1) or 1))
        end = int(getattr(lead, "end_line", start) or start)
        end = max(end, start)
        try:
            source = (Path(data.root) / path).read_text(encoding="utf-8", errors="replace")
            lines = source.splitlines()
            chunk = lines[start - 1 : min(end, start - 1 + per_lead_cap)]
        except OSError:
            continue
        if not chunk:
            continue
        items.append(f"{path} L{start}-{end}:")
        used = byte_len(items[-1]) + 1
        for line_text in chunk:
            cost = byte_len(line_text) + 3
            if used + cost > per_lead_cap:
                items.append("  … (truncated — read the ranged lines for the rest)")
                break
            items.append("  " + line_text)
            used += cost
    return items


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
    variants), LAB (only when a profile file exists), TRAILS (only when a trails
    file with entries exists — cross-task hot files and dead ends, rank
    :data:`R_TRAILS`), GLOSSARY. When *leads* (focus ``Lead`` objects, pre-ranked by
    ``focus.ranker.rank``) are supplied they drive PLAN; otherwise PLAN falls
    back to internal issue-keyword scoring. The byte budget is enforced by
    :func:`pack` with the SPEC drop order; entries are never split. A repo
    without trails yields byte-identical output to a trails-less build.
    """
    pr = builder.pagerank_scores(data)
    facts, extra_rules = load_profile(data.root)
    sections: list[Section] = [Section(tag="", rank=R_HEADER, items=[TITLE])]
    rules_items = rules.flight_rules(cli).split("\n")
    for extra in extra_rules:
        rules_items.append(fmt.wrap(extra, rules.WRAP_WIDTH))
    sections.append(Section(tag="RULES", rank=R_RULES_FULL, items=rules_items))
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
            contracts = _contracts(data, leads)
            snippets = _extract_snippets(data, leads)
        else:
            verbose, compact = builder.build_plan(data, issue, pr)
            contracts, snippets = [], []
        sections.append(Section(tag="PLAN", rank=R_PLAN_VERBOSE, items=verbose))
        sections.append(Section(tag="PLAN", rank=R_PLAN_COMPACT, items=compact))
        if contracts:
            sections.append(Section(tag="CONTRACTS", rank=R_CONTRACTS, items=contracts))
        if snippets:
            sections.append(Section(tag="SNIPPETS", rank=R_SNIPPETS, items=snippets))
    if issue and leads:
        lead_dirs = {
            str(lp).split("/")[0]
            for lp in (getattr(l, "path", "") for l in leads)
            if lp
        }
        dir_rank: dict[str, float] = {}
        for fpath, score in pr.items():
            d = fpath.split("/")[0] if "/" in fpath else "."
            dir_rank[d] = max(dir_rank.get(d, 0.0), score)
        cleared = sorted(
            (d for d in dir_rank if d not in lead_dirs),
            key=lambda d: (dir_rank[d], d),
        )[:3]
        if cleared:
            sections.append(
                Section(
                    tag="CLEARANCE",
                    rank=R_PLAN_COMPACT,
                    items=[
                        "CLEARED (ranked irrelevant for this task — do not explore"
                        " without a stated reason): " + ", ".join(f"{d}/" for d in cleared)
                    ],
                )
            )
    if facts:
        sections.append(
            Section(tag="LAB", rank=R_LAB, items=[f"- {fact}" for fact in facts])
        )
    baseline_path = Path(data.root) / ".trailmark" / "baseline.txt"
    try:
        baseline_lines = baseline_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        baseline_lines = []
    if baseline_lines:
        sections.append(Section(tag="BASELINE", rank=R_LAB + 1, items=baseline_lines[:46]))
    trail_items = trails.section_items(data.root)
    if trail_items:
        sections.append(Section(tag="TRAILS", rank=R_TRAILS, items=trail_items))
    sections.append(
        Section(
            tag="GLOSSARY",
            rank=R_GLOSSARY,
            items=glossary.build_glossary(data),
        )
    )
    return pack(sections, budget)
