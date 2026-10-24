"""``trailmark focus`` handler: full block regen + outlines + optional JSON report.

Flow: cache -> IndexData -> ranked leads -> full block body (lazy ``atlas.assemble``
with a PLAN-only fallback) -> marked-block write -> outlines for lead paths.

Exit codes: 0 success, 2 missing cache ("run `trailmark init` first") or missing
issue text, 1 when AGENTS.md cannot be written. ``--json`` prints a single JSON
object on stdout — that output is the programmatic interface (bench consumes it).
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import sys
from pathlib import Path

from ..cache import IndexCache
from ..emit.agents_md import AGENTS_FILENAME, write_block
from ..model import DEFAULT_BUDGET_BYTES, IndexData
from ..util.tokens import byte_len
from .outlines import OUTLINES_SUBDIR, generate_for
from .plan import render_plan
from .ranker import Lead, rank

LOGGER = logging.getLogger(__name__)


def effective_budget_for(budget: int, n_leads: int) -> int:
    """G-imp4: contracts+snippets need headroom — each contracted lead displaces an
    early full-file read (the highest replay-damage content in a run)."""
    return max(budget, 2048 + 2200 * min(3, n_leads)) if n_leads else budget


def run(args: argparse.Namespace) -> int:
    """Handle ``trailmark focus``; returns the process exit code.

    Tolerates partially-constructed namespaces: every field is read via
    ``getattr`` with a default (root, issue, issue_file, cli, budget, json).
    """
    root = Path(getattr(args, "root", None) or ".")
    trailmark_dir = root / ".trailmark"
    cache = IndexCache(trailmark_dir)
    data = cache.load()
    if data is None:
        print("run `trailmark init` first", file=sys.stderr)
        return 2

    raw, err = _read_issue(args)
    if err is not None:
        print(err, file=sys.stderr)
        return 2
    issue = (raw or "").strip()
    if not issue:
        print("no issue text provided (pass ISSUE, --issue-file, or pipe stdin)", file=sys.stderr)
        return 2

    cli_name = _resolve_cli(getattr(args, "cli", None))
    budget = _budget(args)
    leads = rank(data, cache, issue, k=5)
    effective_budget = effective_budget_for(budget, len(leads))
    body = _assemble_body(data, leads, issue, cli_name, effective_budget)
    # G-imp-probe: write the bounded verify probe (one-call edit-verify cycles)
    try:
        from .testgraph import detect_runner

        runner = detect_runner(str(Path(root)))
        if runner:
            from .probes import write_probe

            target = leads[0].path if leads else ""
            write_probe(root, runner, target)
    except Exception:  # probe is an optimization, never a failure
        logging.getLogger(__name__).debug("probe write skipped", exc_info=True)
    try:
        changed = write_block(root / AGENTS_FILENAME, body)
    except OSError:
        LOGGER.exception("failed to write %s", root / AGENTS_FILENAME)
        return 1
    generate_for(data, [lead.path for lead in leads], trailmark_dir / OUTLINES_SUBDIR)

    if getattr(args, "json", False):
        print(json.dumps(_report(leads, body, changed, cli_name), sort_keys=True))
    return 0


# ---------------------------------------------------------------------------
# Argument handling helpers
# ---------------------------------------------------------------------------


def _read_issue(args: argparse.Namespace) -> tuple[str | None, str | None]:
    """Resolve issue text: positional ISSUE, else --issue-file, else stdin.

    Returns ``(text, None)`` on success or ``(None, error_message)`` on failure;
    an empty/whitespace result is reported by the caller.
    """
    issue = getattr(args, "issue", None)
    if isinstance(issue, str) and issue.strip():
        return issue, None
    issue_file = getattr(args, "issue_file", None)
    if issue_file:
        try:
            return Path(str(issue_file)).read_text(encoding="utf-8"), None
        except OSError:
            return None, f"cannot read issue file: {issue_file}"
    try:
        text = sys.stdin.read() if sys.stdin is not None else ""
    except (OSError, ValueError, AttributeError):
        text = ""
    return text, None


def _resolve_cli(cli: str | None) -> str:
    """Explicit --cli value, or auto-detection: CLAUDECODE env -> claude, else generic."""
    cli = cli or "auto"
    if cli == "auto":
        return "claude" if os.environ.get("CLAUDECODE") else "generic"
    return str(cli)


def _budget(args: argparse.Namespace) -> int:
    """--budget as int, or DEFAULT_BUDGET_BYTES when absent/zero/invalid."""
    value = getattr(args, "budget", None)
    if not value:
        return DEFAULT_BUDGET_BYTES
    try:
        return int(value)
    except (TypeError, ValueError):
        LOGGER.warning("invalid --budget %r; using default", value)
        return DEFAULT_BUDGET_BYTES


# ---------------------------------------------------------------------------
# Block assembly and reporting
# ---------------------------------------------------------------------------


def _load_atlas() -> object | None:
    """Import ``trailmark.atlas`` lazily; None when the sibling package is absent."""
    try:
        return importlib.import_module("..atlas", __package__ or "trailmark")
    except ImportError:
        LOGGER.info("atlas package unavailable; emitting PLAN-only body")
        return None


def _assemble_body(
    data: IndexData, leads: list[Lead], issue: str, cli_name: str, budget: int
) -> str:
    """Full block body via ``atlas.assemble``; falls back to a PLAN-only body."""
    atlas = _load_atlas()
    assemble = getattr(atlas, "assemble", None) if atlas is not None else None
    if callable(assemble):
        try:
            # leads (focus.ranker output) are the single source of truth for PLAN;
            # atlas falls back to its internal scorer only when they are absent.
            return str(assemble(data, issue, cli_name, budget, leads=leads))
        except Exception:
            LOGGER.exception("atlas.assemble failed; falling back to PLAN-only body")
    return render_plan(leads, budget)


def _report(leads: list[Lead], body: str, changed: bool, cli_name: str) -> dict[str, object]:
    """The --json payload. Scores are rounded to 4 decimals (no raw floats emitted)."""
    return {
        "leads": [
            {
                "path": lead.path,
                "start": lead.start_line,
                "end": lead.end_line,
                "why": lead.why,
                "score": round(lead.score, 4),
            }
            for lead in leads
        ],
        "bytes": byte_len(body),
        "changed": changed,
        "cli": cli_name,
    }
