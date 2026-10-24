#!/usr/bin/env python
"""Print the resolved reference set from the PYTHON reference pipeline.

Diagnostic tool for index parity work: builds the FULL index (no cache reuse,
so the output is a pure function of the repo state) and writes one line per
resolved ref to stdout in the pipeline's own deterministic order:

    src_file<TAB>dst_name<TAB>dst_file<TAB>line

Diff this byte-for-byte against the Rust equivalent (`trailmark refs --tsv`) to
diagnose ref-resolution drift between the two implementations.

Usage:
    uv run python tools/dump_refs.py --root REPO [--langs py,ts,js,go,rs] [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Prefer the in-tree source (the behavioral oracle), then any installed package.
_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir():
    sys.path.insert(0, str(_SRC))

# Pin deterministic (document-order) tree-sitter captures BEFORE the pipeline
# imports — see _deterministic_captures.py for the drift diagnosis. No effect
# on files under the MAX_REFS_PER_FILE cap.
import _deterministic_captures  # noqa: E402

_deterministic_captures.apply()

from trailmark.index import build  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="repository root")
    parser.add_argument("--langs", default="py,ts,js,go,rs", help="comma-separated languages")
    parser.add_argument("--workers", type=int, default=0, help="parser workers")
    parser.add_argument("--json", action="store_true", help="emit a JSON object instead of TSV")
    args = parser.parse_args()

    root = str(Path(args.root).resolve())
    langs = [part.strip() for part in args.langs.split(",") if part.strip()]
    data = build(root, langs, args.workers, None)

    if args.json:
        json.dump(
            {
                "root": data.root,
                "files": len(data.files),
                "refs": [
                    {
                        "src_file": r.src_file,
                        "dst_name": r.dst_name,
                        "dst_file": r.dst_file,
                        "line": r.line,
                    }
                    for r in data.refs
                ],
            },
            sys.stdout,
        )
        sys.stdout.write("\n")
        return 0

    for r in data.refs:
        sys.stdout.write(f"{r.src_file}\t{r.dst_name}\t{r.dst_file}\t{r.line}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
