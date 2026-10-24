#!/usr/bin/env python
"""Print the codegraph AGENTS.md block body from the PYTHON reference pipeline.

Used by the Rust ``parity_with_python`` test as the behavioral oracle: it runs
``index.update`` + ``atlas.assemble`` on a repo+issue and writes the exact block
body (no markers) to stdout, so the two implementations can be diffed byte for
byte.

Usage:
    uv run python tools/dump_block.py --root REPO --issue "text" \
        [--cli generic] [--budget 2048] [--langs py,ts,js,go,rs] [--workers N]

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

from codegraph.atlas import assemble  # noqa: E402
from codegraph.cache import IndexCache  # noqa: E402
from codegraph.index import update  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="repository root")
    parser.add_argument("--issue", default=None, help="issue / problem-statement text")
    parser.add_argument("--issue-file", default=None, help="read the issue from this file")
    parser.add_argument("--cli", default="generic", help="rules variant (generic/claude/shell)")
    parser.add_argument("--budget", type=int, default=2048, help="block budget bytes")
    parser.add_argument("--langs", default="py,ts,js,go,rs", help="comma-separated languages")
    parser.add_argument("--workers", type=int, default=0, help="parser workers")
    args = parser.parse_args()

    root = str(Path(args.root).resolve())
    issue = args.issue
    if issue is None and args.issue_file:
        issue = Path(args.issue_file).read_text(encoding="utf-8")

    cache = IndexCache(Path(root) / ".codegraph")
    langs = [part.strip() for part in args.langs.split(",") if part.strip()]
    data = update(root, langs, args.workers, cache)
    body = assemble(data, issue, args.cli, args.budget)
    sys.stdout.write(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
