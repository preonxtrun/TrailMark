#!/usr/bin/env python
"""Print the trailmark AGENTS.md block body from the PYTHON reference pipeline.

Used by the Rust ``parity_with_python`` test as the behavioral oracle: it runs
``index.update`` + ``atlas.assemble`` on a repo+issue and writes the exact block
body (no markers) to stdout, so the two implementations can be diffed byte for
byte.

Usage:
    uv run python tools/dump_block.py --root REPO --issue "text" \
        [--cli generic] [--budget 2048] [--langs py,ts,js,go,rs] [--workers N] \
        [--leads]

With ``--leads`` the focus pipeline is mirrored: ``focus.ranker.rank`` (k=5)
produces the leads, ``focus.cli_focus.effective_budget_for`` raises the budget,
and ``atlas.assemble`` is called with ``leads=...`` so PLAN / CONTRACTS /
SNIPPETS / CLEARANCE take the leads path — byte-identical to the Rust
``assemble_with_leads`` flow used by ``trailmark focus``.

The body is written to stdout without any added trailing newline, so
``stdout == emit body`` holds exactly.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Prefer the in-tree source (the behavioral oracle), then any installed package.
_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir():
    sys.path.insert(0, str(_SRC))

# Pin deterministic (document-order) tree-sitter captures BEFORE the pipeline
# imports: py-tree-sitter's QueryCursor.captures() returns nodes in a
# per-parse-random order, which makes _extract_refs' MAX_REFS_PER_FILE cap keep
# a random subset in big files (see _deterministic_captures.py for the full
# diagnosis). No effect on files under the cap.
import _deterministic_captures  # noqa: E402

_deterministic_captures.apply()

from trailmark.atlas import assemble  # noqa: E402
from trailmark.cache import IndexCache  # noqa: E402
from trailmark.focus.cli_focus import effective_budget_for  # noqa: E402
from trailmark.focus.ranker import rank  # noqa: E402
from trailmark.index import update  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="repository root")
    parser.add_argument("--issue", default=None, help="issue / problem-statement text")
    parser.add_argument("--issue-file", default=None, help="read the issue from this file")
    parser.add_argument("--cli", default="generic", help="rules variant (generic/claude/shell)")
    parser.add_argument("--budget", type=int, default=2048, help="block budget bytes")
    parser.add_argument("--langs", default="py,ts,js,go,rs", help="comma-separated languages")
    parser.add_argument("--workers", type=int, default=0, help="parser workers")
    parser.add_argument(
        "--leads",
        action="store_true",
        help="mirror `trailmark focus`: rank leads and drive PLAN/CONTRACTS/"
        "SNIPPETS/CLEARANCE from them (requires an issue)",
    )
    args = parser.parse_args()

    root = str(Path(args.root).resolve())
    issue = args.issue
    if issue is None and args.issue_file:
        issue = Path(args.issue_file).read_text(encoding="utf-8")

    cache = IndexCache(Path(root) / ".trailmark")
    langs = [part.strip() for part in args.langs.split(",") if part.strip()]
    data = update(root, langs, args.workers, cache)

    leads = None
    budget = args.budget
    if args.leads:
        if not issue:
            parser.error("--leads requires issue text (pass --issue or --issue-file)")
        leads = rank(data, cache, issue, k=5)
        budget = effective_budget_for(budget, len(leads))
    body = assemble(data, issue, args.cli, budget, leads=leads)
    sys.stdout.write(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
