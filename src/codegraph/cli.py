"""codegraph CLI: init / focus / remove / stats / doctor / watch (SPEC CLI contract),
plus the diagnostics toolkit (why / search / refs / block / selftest in
``codegraph.diag``).

Global flags ``--root`` (default ``.``), ``--budget`` and ``--json`` are accepted
both before and after the subcommand. Heavy sibling packages (``codegraph.index``,
``codegraph.focus``) are imported lazily inside the handlers with clean errors, so
every command works (or fails with a clear exit code) even while those modules are
still being built: exit 0 = success, 1 = doctor failures / runtime error, 2 =
missing cache or index ("run ``codegraph init`` first"), 3 = focus unavailable.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

from .atlas import assemble
from .cache import IndexCache
from .diag import SELFTEST_ISSUE
from .emit.agents_md import read_block, remove_block, write_block
from .model import DEFAULT_BUDGET_BYTES, IndexData
from .util.tokens import byte_len

_DEFAULT_LANGS = ("py", "ts", "js", "go", "rs")

#: Directories excluded from the watch signature scan.
_SKIP_DIRS = frozenset(
    {
        ".codegraph", ".git", ".hg", ".mypy_cache", ".pytest_cache", ".ruff_cache",
        ".tox", ".venv", "__pycache__", "build", "dist", "env", "node_modules", "venv",
    }
)


def _add_common(parser: argparse.ArgumentParser, suppress: bool) -> None:
    """Add the global flags.

    Subparser copies use ``default=argparse.SUPPRESS`` so a flag passed before the
    subcommand is not clobbered, while the same flag after the subcommand still works.
    """
    if suppress:
        parser.add_argument("--root", default=argparse.SUPPRESS, help="repository root")
        parser.add_argument("--budget", type=int, default=argparse.SUPPRESS, help="block budget bytes")
        parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="JSON output")
    else:
        parser.add_argument("--root", default=".", help="repository root (default: .)")
        parser.add_argument(
            "--budget", type=int, default=None, help=f"block budget bytes (default: {DEFAULT_BUDGET_BYTES})"
        )
        parser.add_argument("--json", action="store_true", help="print machine-readable JSON")


def build_parser() -> argparse.ArgumentParser:
    """Build the codegraph argument parser (SPEC CLI contract)."""
    parser = argparse.ArgumentParser(
        prog="codegraph",
        description="Hidden optimization layer between coding CLIs and the codebase.",
    )
    _add_common(parser, suppress=False)
    sub = parser.add_subparsers(dest="cmd", metavar="command")

    p_init = sub.add_parser(
        "init", help="build/update the index and write the AGENTS.md atlas block"
    )
    _add_common(p_init, suppress=True)
    p_init.add_argument(
        "--langs", default=",".join(_DEFAULT_LANGS), help="comma-separated languages (default: py,ts,js,go,rs)"
    )
    p_init.add_argument("--workers", type=int, default=None, help="parser workers (default: cpu count)")
    p_init.add_argument(
        "--harness",
        default="",
        help="comma-separated adapter files to write: claude,gemini,cursor,copilot (default: none)",
    )
    p_init.add_argument(
        "--force", action="store_true", help="full rebuild instead of mtime-incremental update"
    )
    p_init.set_defaults(func=_cmd_init)

    p_focus = sub.add_parser(
        "focus", help="regenerate the FULL block (atlas + PLAN) for an issue"
    )
    _add_common(p_focus, suppress=True)
    p_focus.add_argument("issue", nargs="?", default=None, help="issue / problem-statement text")
    p_focus.add_argument(
        "--issue-file", dest="issue_file", default=None, help="read the issue text from this file"
    )
    p_focus.add_argument(
        "--cli",
        dest="cli",
        choices=("auto", "claude", "codex", "opencode", "generic"),
        default="auto",
        help="target coding CLI (auto resolves via env, done by the focus module)",
    )
    p_focus.set_defaults(func=_cmd_focus)

    p_rm = sub.add_parser("remove", help="remove the managed block (and optionally .codegraph/)")
    _add_common(p_rm, suppress=True)
    p_rm.add_argument("--purge", action="store_true", help="also delete the .codegraph/ state tree")
    p_rm.set_defaults(func=_cmd_remove)

    p_stats = sub.add_parser("stats", help="print index statistics from the cache")
    _add_common(p_stats, suppress=True)
    p_stats.set_defaults(func=_cmd_stats)

    p_doc = sub.add_parser("doctor", help="sanity-check cache freshness and the written block")
    _add_common(p_doc, suppress=True)
    p_doc.set_defaults(func=_cmd_doctor)

    p_watch = sub.add_parser("watch", help="poll the repo and refresh the block on change")
    _add_common(p_watch, suppress=True)
    p_watch.add_argument(
        "--interval", type=float, default=2.0, help="poll interval seconds (default: 2)"
    )
    p_watch.set_defaults(func=_cmd_watch)

    # -- diagnostics toolkit (codegraph.diag) ---------------------------------

    p_why = sub.add_parser(
        "why", help="explain the ranking for an issue: per-file component scores"
    )
    _add_common(p_why, suppress=True)
    p_why.add_argument("issue", help="issue / problem-statement text")
    p_why.add_argument("--k", type=int, default=8, help="candidate files to explain (default: 8)")
    p_why.set_defaults(func=_cmd_why)

    p_search = sub.add_parser(
        "search", help="show the BM25 lexical hits a query produces (best first)"
    )
    _add_common(p_search, suppress=True)
    p_search.add_argument("query", help="search query text")
    p_search.add_argument("--k", type=int, default=20, help="max hits (default: 20)")
    p_search.set_defaults(func=_cmd_search)

    p_refs = sub.add_parser(
        "refs", help="show one file's symbols, resolved/unresolved refs and pagerank"
    )
    _add_common(p_refs, suppress=True)
    p_refs.add_argument("path", help="file path (repo-relative or absolute)")
    p_refs.set_defaults(func=_cmd_refs)

    p_block = sub.add_parser(
        "block", help="print the managed AGENTS.md block exactly as the agent sees it"
    )
    _add_common(p_block, suppress=True)
    p_block.set_defaults(func=_cmd_block)

    p_self = sub.add_parser(
        "selftest", help="run the full pipeline on a throwaway sandbox and verify the guarantees"
    )
    _add_common(p_self, suppress=True)
    p_self.add_argument(
        "--issue", default="", help=f"issue text for the focus stage (default: {SELFTEST_ISSUE!r})"
    )
    p_self.add_argument("--keep", action="store_true", help="keep the sandbox dir for inspection")
    p_self.set_defaults(func=_cmd_selftest)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Console entrypoint (``codegraph.cli:main``); returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "cmd", None) is None:
        parser.print_help()
        return 0
    return int(args.func(args))


# -- shared helpers -----------------------------------------------------------


def _root_of(args: argparse.Namespace) -> Path:
    return Path(str(getattr(args, "root", ".") or ".")).resolve()


def _budget_of(args: argparse.Namespace) -> int:
    raw = getattr(args, "budget", None)
    return DEFAULT_BUDGET_BYTES if raw is None else max(0, int(raw))


def _json_of(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "json", False))


def _parse_langs(raw: str | None) -> list[str]:
    langs = [part.strip() for part in (raw or "").split(",") if part.strip()]
    return langs or list(_DEFAULT_LANGS)


def _default_workers() -> int:
    return os.cpu_count() or 4


def _emit(payload: dict[str, object], args: argparse.Namespace, human: str) -> None:
    """Print the command result: JSON object in --json mode, human line otherwise."""
    if _json_of(args):
        print(json.dumps(payload, sort_keys=True))
    else:
        print(human)


# -- command handlers ---------------------------------------------------------


def _cmd_init(args: argparse.Namespace) -> int:
    root = _root_of(args)
    budget = _budget_of(args)
    try:
        # lazy: heavy tree-sitter stack, may be absent; import_module (not
        # `import ... as`) so test doubles installed in sys.modules are honored
        index = importlib.import_module("codegraph.index")
    except ImportError as exc:
        print(f"codegraph: index module unavailable ({exc}) — cannot init", file=sys.stderr)
        return 2
    langs = _parse_langs(getattr(args, "langs", None))
    workers = getattr(args, "workers", None) or _default_workers()
    cache = IndexCache(root / ".codegraph")
    try:
        if getattr(args, "force", False):
            data = index.build(root, langs, workers, cache)
        else:
            data = index.update(root, langs, workers, cache)
        cache.save(data)
    except Exception as exc:  # noqa: BLE001 — never crash on a bad repo (invariant #4)
        print(f"codegraph: init failed: {exc}", file=sys.stderr)
        return 1
    body = assemble(data, None, "generic", budget)
    changed = write_block(root / "AGENTS.md", body)
    harnesses = getattr(args, "harness", None)
    if harnesses:
        from .emit.harnesses import write_adapters

        adapters = write_adapters(
            root, [h.strip() for h in str(harnesses).split(",") if h.strip()]
        )
    else:
        adapters = []
    outlines = _generate_top_outlines(data, root)
    files, symbols, refs = len(data.files), len(data.symbols()), len(data.refs)
    _emit(
        {
            "cmd": "init",
            "root": str(root),
            "files": files,
            "symbols": symbols,
            "refs": refs,
            "budget": budget,
            "block_bytes": byte_len(body),
            "block_changed": changed,
            "harness_adapters": [str(p.relative_to(root)) for p in adapters],
            "outlines": outlines,
        },
        args,
        f"codegraph init: {files} files, {symbols} symbols, {refs} refs → AGENTS.md"
        f" ({byte_len(body)}/{budget} B, {'updated' if changed else 'unchanged'};"
        f" outlines: {outlines})",
    )
    return 0


def _generate_top_outlines(data: IndexData, root: Path) -> str:
    """Outlines for the top-32 pagerank files; never fatal (invariant #4)."""
    try:
        from codegraph.focus import outlines as focus_outlines  # lazy: focus may be absent
    except ImportError:
        print("codegraph: outlines module unavailable, skipping", file=sys.stderr)
        return "skipped"
    try:
        focus_outlines.generate_top(data, root / ".codegraph" / "outlines", k=32)
    except Exception as exc:  # noqa: BLE001 — outlines must never fail init (invariant #4)
        print(f"codegraph: outlines generation failed ({exc}) — skipping", file=sys.stderr)
        return "failed"
    return "ok"


def _cmd_focus(args: argparse.Namespace) -> int:
    """Delegate entirely to codegraph.focus.cli_focus (exit 3 when unavailable)."""
    try:
        from codegraph.focus import cli_focus  # lazy: focus is an optional sibling module
    except ImportError:
        print(
            "codegraph: focus module unavailable — rebuild/install the package",
            file=sys.stderr,
        )
        return 3
    return int(cli_focus.run(args))


def _cmd_remove(args: argparse.Namespace) -> int:
    root = _root_of(args)
    changed = remove_block(root / "AGENTS.md")
    purged = False
    if getattr(args, "purge", False):
        shutil.rmtree(root / ".codegraph", ignore_errors=True)
        purged = True
    _emit(
        {"cmd": "remove", "root": str(root), "block_removed": changed, "purged": purged},
        args,
        "codegraph remove: block "
        + ("removed" if changed else "not found")
        + (", .codegraph/ purged" if purged else ""),
    )
    return 0


def _cmd_stats(args: argparse.Namespace) -> int:
    root = _root_of(args)
    data = IndexCache(root / ".codegraph").load()
    if data is None:
        print("codegraph: no index cache — run `codegraph init` first", file=sys.stderr)
        return 2
    langs: dict[str, int] = {}
    for entry in data.files.values():
        langs[entry.lang] = langs.get(entry.lang, 0) + 1
    counts = dict(sorted(langs.items()))
    files, symbols, refs = len(data.files), len(data.symbols()), len(data.refs)
    _emit(
        {
            "cmd": "stats",
            "root": data.root or str(root),
            "files": files,
            "symbols": symbols,
            "refs": refs,
            "langs": counts,
        },
        args,
        f"codegraph stats: {files} files, {symbols} symbols, {refs} refs"
        f" [{', '.join(f'{k}={v}' for k, v in counts.items())}]",
    )
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    root = _root_of(args)
    budget = _budget_of(args)
    cache = IndexCache(root / ".codegraph")
    data = cache.load()
    results: list[tuple[str, bool, str]] = []
    if data is None:
        results.append(("cache", False, "absent — run `codegraph init` first"))
    else:
        stale = missing = 0
        for rel, mtime in cache.file_mtimes().items():
            try:
                current = (root / rel).stat().st_mtime
            except OSError:
                missing += 1
                continue
            if current != mtime:
                stale += 1
        fresh = stale == 0 and missing == 0
        total = len(data.files)
        detail = f"{total} files, {stale} stale, {missing} missing"
        results.append(("cache", fresh, detail if fresh else f"stale — {detail}"))
    block = read_block(root / "AGENTS.md")
    results.append(
        (
            "block",
            block is not None,
            "present" if block is not None else "absent — run `codegraph init` first",
        )
    )
    size = byte_len(block) if block is not None else 0
    results.append(("block-size", block is not None and size <= budget, f"{size}/{budget} bytes"))
    ok = all(good for _, good, _ in results)
    if _json_of(args):
        print(
            json.dumps(
                {
                    "cmd": "doctor",
                    "ok": ok,
                    **{name: {"ok": good, "detail": detail} for name, good, detail in results},
                },
                sort_keys=True,
            )
        )
    else:
        for name, good, detail in results:
            print(f"{name}: {'ok' if good else 'FAIL'} — {detail}")
        print(f"doctor: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def _cmd_watch(args: argparse.Namespace) -> int:
    root = _root_of(args)
    try:
        # lazy: required for refreshes; import_module honors sys.modules test doubles
        index = importlib.import_module("codegraph.index")
    except ImportError:
        print("codegraph: index module unavailable — run `codegraph init` first", file=sys.stderr)
        return 2
    interval = max(0.1, float(getattr(args, "interval", 2.0) or 2.0))
    budget = _budget_of(args)
    langs = _parse_langs(getattr(args, "langs", None))
    if not _json_of(args):
        print(f"codegraph watch: polling {root} every {interval:g}s (Ctrl-C to stop)")
    last: str | None = None
    try:
        while True:
            signature = _scan_signature(root)
            if signature != last:
                cache = IndexCache(root / ".codegraph")
                data = index.update(root, langs, _default_workers(), cache)
                cache.save(data)
                body = assemble(data, None, "generic", budget)
                changed = write_block(root / "AGENTS.md", body)
                _emit(
                    {
                        "cmd": "watch",
                        "files": len(data.files),
                        "block_bytes": byte_len(body),
                        "changed": changed,
                    },
                    args,
                    f"watch: {len(data.files)} files — block "
                    + ("updated" if changed else "unchanged"),
                )
                last = signature
            time.sleep(interval)
    except KeyboardInterrupt:
        if not _json_of(args):
            print("codegraph watch: stopped")
        return 0
    except Exception as exc:  # noqa: BLE001 — report and exit non-zero, never crash
        print(f"codegraph: watch failed: {exc}", file=sys.stderr)
        return 1


def _scan_signature(root: Path) -> str:
    """Cheap deterministic repo-state fingerprint (paths, sizes, mtimes) for watch.

    Skips state directories and AGENTS.md itself — our own writes must not
    re-trigger the loop. The signature is internal only, never emitted.
    """
    parts: list[str] = []
    for base, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames if d not in _SKIP_DIRS and not d.endswith(".egg-info")
        )
        for name in sorted(filenames):
            if name == "AGENTS.md":
                continue
            path = Path(base) / name
            try:
                st = path.stat()
            except OSError:
                continue
            rel = path.relative_to(root)
            parts.append(f"{rel}:{st.st_size}:{int(st.st_mtime_ns)}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


# -- diagnostics handlers (thin delegation to codegraph.diag) -----------------


def _cmd_why(args: argparse.Namespace) -> int:
    """Delegate to codegraph.diag.why (exit 2 when the index cache is missing)."""
    from .diag import why  # lazy: diagnostics are optional siblings

    raw_k = getattr(args, "k", None)
    k = 8 if raw_k is None else int(raw_k)
    return int(why(_root_of(args), str(getattr(args, "issue", "") or ""), k, _json_of(args)))


def _cmd_search(args: argparse.Namespace) -> int:
    """Delegate to codegraph.diag.search (exit 2 when the index cache is missing)."""
    from .diag import search  # lazy: diagnostics are optional siblings

    raw_k = getattr(args, "k", None)
    k = 20 if raw_k is None else int(raw_k)
    return int(search(_root_of(args), str(getattr(args, "query", "") or ""), k, _json_of(args)))


def _cmd_refs(args: argparse.Namespace) -> int:
    """Delegate to codegraph.diag.refs (exit 2 missing cache, 1 unknown path)."""
    from .diag import refs  # lazy: diagnostics are optional siblings

    return int(refs(_root_of(args), str(getattr(args, "path", "") or ""), _json_of(args)))


def _cmd_block(args: argparse.Namespace) -> int:
    """Delegate to codegraph.diag.block (exit 0 with or without a block)."""
    from .diag import block  # lazy: diagnostics are optional siblings

    return int(block(_root_of(args), _json_of(args)))


def _cmd_selftest(args: argparse.Namespace) -> int:
    """Delegate to codegraph.diag.selftest (exit 0 only when every check passes)."""
    from .diag import SELFTEST_ISSUE, selftest  # lazy: diagnostics are optional siblings

    issue = str(getattr(args, "issue", "") or "").strip() or SELFTEST_ISSUE
    return int(
        selftest(
            budget=_budget_of(args),
            issue=issue,
            keep=bool(getattr(args, "keep", False)),
            as_json=_json_of(args),
        )
    )


if __name__ == "__main__":  # pragma: no cover — convenience: python -m codegraph.cli
    sys.exit(main())
