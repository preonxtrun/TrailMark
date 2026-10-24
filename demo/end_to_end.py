"""End-to-end demo on a real repo: index → focus → injected AGENTS.md block.

Runs the exact container-setup sequence the benchmark treatment arm uses:
  codegraph init  (index.update)   then   codegraph focus "<issue>"

Usage: uv run python demo/end_to_end.py [target_repo] [issue text...]
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import time

REPO = pathlib.Path("/tmp/opencode/demo-target/requests")
ISSUE = (
    "Session cookies are not persisted across redirects in Session.resolve_redirects; "
    "set_cookie seems to lose them when the redirect target has no scheme."
)


def main() -> int:
    target = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else REPO
    issue = " ".join(sys.argv[2:]) or ISSUE

    from codegraph.cache import IndexCache
    from codegraph.focus import cli_focus
    from codegraph.index import update

    cache = IndexCache(target / ".codegraph")

    t0 = time.perf_counter()
    data = update(root=str(target), langs=None, workers=8, cache=cache)
    t_build = time.perf_counter() - t0
    nsyms = sum(len(f.symbols) for f in data.files.values())
    print(f"[init]  files={len(data.files)} symbols={nsyms} refs={len(data.refs)} build={t_build:.2f}s")

    args = argparse.Namespace(
        root=str(target), issue=issue, issue_file=None, cli="generic", budget=2048, json=True
    )
    t0 = time.perf_counter()
    rc = cli_focus.run(args)
    t_focus = (time.perf_counter() - t0) * 1000
    print(f"[focus] rc={rc} in {t_focus:.0f} ms\n")

    block = (target / "AGENTS.md").read_text(encoding="utf-8")
    print("=== AGENTS.md (injected layer) ===")
    print(block)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
