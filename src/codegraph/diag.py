"""Diagnostics toolkit: ``why`` / ``search`` / ``refs`` / ``block`` / ``selftest``.

A read-mostly debugging surface over the exact contracts the pipeline uses —
no re-implementations:

- ``why``     delegates to :func:`codegraph.focus.ranker.explain` (the same
  scoring internals as ``rank``), exposing per-file component scores and the
  chosen lead symbols.
- ``search``  shows exactly what BM25 sees via ``IndexCache.search``.
- ``refs``    reads one file's symbols/refs from the loaded :class:`IndexData`
  plus its file-level PageRank (``index.graph.pagerank``).
- ``block``   reads the managed block through ``emit.read_block`` — the same
  bytes the coding CLI's agent sees in AGENTS.md.
- ``selftest`` drives the real pipeline in-process (``index.update`` →
  ``atlas.assemble`` → focus write via ``focus.cli_focus.run``) against a
  synthetic sandbox repo and re-checks the G1/G2/G3/G7-style guarantee
  assertions inline. It never touches the caller's repository.

Exit codes follow cli.py: 0 = success (for ``block`` an absent block is a
valid answer), 1 = failed checks / unknown path, 2 = missing index cache
("run ``codegraph init`` first"). ``--json`` prints one machine-readable
object; floats are rounded so output stays deterministic (invariant #1).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import shutil
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .cache import IndexCache
from .emit.agents_md import AGENTS_FILENAME, read_block, remove_block
from .model import BLOCK_END, BLOCK_START, IndexData
from .util.tokens import byte_len

if TYPE_CHECKING:  # pragma: no cover — typing only (focus is an optional sibling)
    from .focus.ranker import Lead

LOGGER = logging.getLogger(__name__)

#: Default issue text for the ``selftest`` focus stage (token-rich, deterministic).
SELFTEST_ISSUE = "session token refresh expires and drops the user id"

#: Decimal places for machine-readable floats (never emit raw floats, invariant #1).
_RND = 6

#: Names of the selftest checks, in execution order (pipeline-failure reporting).
_CHECK_NAMES = (
    "index",
    "cache",
    "g2-atlas-deterministic",
    "focus-write",
    "g3-budget",
    "no-lab",
    "tests-runner",
    "g1-block-markers",
    "g7-focus-idempotent",
    "g1-remove-restores",
)

#: Outside-marker content planted in the sandbox AGENTS.md (G1 non-destruction probe).
_SANDBOX_USER_TEXT = "user notes\n"


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------


def _no_cache_error() -> int:
    """Print the clean missing-cache error and return the exit code (2)."""
    print("codegraph: no index cache — run `codegraph init` first", file=sys.stderr)
    return 2


def _norm_rel(root: Path, raw: str) -> str:
    """Normalize a user-supplied path to the index's repo-relative posix form."""
    text = str(raw).replace("\\", "/").strip()
    if text.startswith("/"):
        try:
            text = Path(text).resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            pass  # outside the repo — the lookup below fails with a clean error
    return "/".join(part for part in text.split("/") if part not in ("", "."))


def _similar_files(data: IndexData, rel: str, limit: int = 5) -> list[str]:
    """Indexed files sharing *rel*'s basename (deterministic hint list)."""
    base = rel.rsplit("/", 1)[-1]
    return sorted(p for p in data.files if p.rsplit("/", 1)[-1] == base)[:limit]


def _pagerank(data: IndexData) -> dict[str, float]:
    """Plain file-level PageRank: ``index.graph.pagerank``, ranker fallback on failure."""
    try:
        from .index.graph import pagerank  # lazy: index/ may be absent

        return {str(p): float(s) for p, s in dict(pagerank(data.files, data.refs)).items()}
    except Exception:  # diagnostics must degrade, never crash
        LOGGER.debug("index.graph.pagerank unavailable; using ranker fallback", exc_info=True)
        from .focus.ranker import file_pagerank

        return file_pagerank(data.files, data.refs)


def _sha(path: Path) -> str:
    """SHA-256 of a file's bytes, or "" when unreadable."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# codegraph why — the ranking debugger
# ---------------------------------------------------------------------------


def why(root: Path, issue: str, k: int, as_json: bool) -> int:
    """Explain the hybrid ranking for *issue* (``codegraph why``).

    Loads the cache, runs ``ranker.explain`` (the exact ``rank`` internals) and
    prints, per candidate file, the four normalized component scores, the final
    score and the chosen lead symbol — plus the top-5 leads as focus injects
    them into PLAN. Exit 0, or 2 when the index cache is missing.
    """
    from .focus.ranker import W_GRAPH, W_LEXICAL, W_PRIOR, W_TEST, explain, issue_tokens

    cache = IndexCache(root / ".codegraph")
    data = cache.load()
    if data is None:
        return _no_cache_error()
    rows = explain(issue, data, cache, int(k))
    tokens = issue_tokens(issue)
    if as_json:
        print(
            json.dumps(
                {
                    "cmd": "why",
                    "issue": issue,
                    "k": int(k),
                    "tokens": tokens,
                    "weights": {
                        "lexical": W_LEXICAL,
                        "graph": W_GRAPH,
                        "test": W_TEST,
                        "prior": W_PRIOR,
                    },
                    "count": len(rows),
                    "leads": [_why_entry(lead, detail) for lead, detail in rows],
                },
                sort_keys=True,
            )
        )
        return 0
    print(_why_human(issue, tokens, rows, int(k)))
    return 0


def _why_entry(lead: Lead, detail: dict[str, Any]) -> dict[str, Any]:
    """One machine-readable ``why`` row (components nested, floats rounded)."""
    hit = detail.get("hit")
    return {
        "path": lead.path,
        "start_line": int(detail["line"]),
        "end_line": int(detail["end_line"]),
        "why": lead.why,
        "symbol": str(detail["symbol"]),
        "score": round(float(detail["score"]), _RND),
        "components": {
            "lexical": round(float(detail["lexical"]), _RND),
            "graph": round(float(detail["graph"]), _RND),
            "test": round(float(detail["test"]), _RND),
            "prior": round(float(detail["prior"]), _RND),
        },
        "raw": {key: round(float(val), _RND) for key, val in detail.get("raw", {}).items()},
        "hit": None
        if hit is None
        else {
            "name": str(hit["name"]),
            "line": int(hit["line"]),
            "bm25": round(float(hit["bm25"]), _RND),
        },
    }


def _why_human(
    issue: str, tokens: list[str], rows: list[tuple[Lead, dict[str, Any]]], k: int
) -> str:
    """Human table: one line per candidate with the four component scores."""
    from .focus.ranker import W_GRAPH, W_LEXICAL, W_PRIOR, W_TEST

    lines = [
        f'codegraph why: "{issue}"',
        f"tokens: {', '.join(tokens) if tokens else '(none — pagerank fallback)'}",
        f"weights: lexical={W_LEXICAL:g} graph={W_GRAPH:g} test={W_TEST:g} prior={W_PRIOR:g}",
        f"candidates explained (top {k}, best first):",
        "",
        " #   score    lex graph  test prior  file — lead symbol:line",
    ]
    for i, (lead, d) in enumerate(rows, 1):
        lines.append(
            f"{i:>2}  {float(d['score']):6.4f}  {float(d['lexical']):5.3f}"
            f" {float(d['graph']):5.3f} {float(d['test']):5.3f} {float(d['prior']):5.3f}"
            f"  {lead.path} — {d['symbol']}:{d['line']}"
        )
    if not rows:
        lines.append("(no leads — nothing in the index matched; try `codegraph init`)")
    lines.append("")
    lines.append("top leads (what `focus` injects into PLAN):")
    for i, (lead, d) in enumerate(rows[:5], 1):
        lines.append(
            f"{i}. {lead.path}:{lead.start_line}-{lead.end_line}"
            f" — {lead.why} ({float(d['score']):.4f})"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# codegraph search — what BM25 sees
# ---------------------------------------------------------------------------


def search(root: Path, query: str, k: int, as_json: bool) -> int:
    """Print the BM25 hits *query* produces (``codegraph search``), best first.

    Exit 0 (also with zero hits), or 2 when the index cache is missing —
    distinguishing "not indexed yet" from a legitimate empty result.
    """
    cache = IndexCache(root / ".codegraph")
    if cache.load() is None:
        return _no_cache_error()
    hits = cache.search(query, k=max(0, int(k)))
    if as_json:
        print(
            json.dumps(
                {
                    "cmd": "search",
                    "query": query,
                    "k": int(k),
                    "count": len(hits),
                    "hits": [
                        {
                            "file": h.file,
                            "name": h.name,
                            "kind": h.kind,
                            "line": h.line,
                            "score": round(float(h.score), _RND),
                        }
                        for h in hits
                    ],
                },
                sort_keys=True,
            )
        )
        return 0
    lines = [f'codegraph search: "{query}" — {len(hits)} hits (best first)']
    for i, h in enumerate(hits, 1):
        lines.append(f"{i:>3}  {float(h.score):8.4f}  {h.kind:<7} {h.name}  {h.file}:{h.line}")
    if not hits:
        lines.append("(no symbol matches — check spelling or run `codegraph init`)")
    print("\n".join(lines))
    return 0


# ---------------------------------------------------------------------------
# codegraph refs — the ref-resolution debugger
# ---------------------------------------------------------------------------


def refs(root: Path, path_arg: str, as_json: bool) -> int:
    """Show one file's symbols, resolved/unresolved refs and PageRank (exit 0/1/2)."""
    cache = IndexCache(root / ".codegraph")
    data = cache.load()
    if data is None:
        return _no_cache_error()
    rel = _norm_rel(root, path_arg)
    entry = data.files.get(rel)
    if entry is None:
        print(f"codegraph: {path_arg} is not in the index (root {root})", file=sys.stderr)
        similar = _similar_files(data, rel)
        if similar:
            print("did you mean: " + ", ".join(similar), file=sys.stderr)
        return 1

    syms = sorted(entry.symbols, key=lambda s: (s.line, s.name))
    outgoing = sorted(
        (r for r in data.refs if r.src_file == rel),
        key=lambda r: (r.line, r.dst_name, r.dst_file),
    )
    resolved = [r for r in outgoing if r.dst_file]
    unresolved = [r for r in outgoing if not r.dst_file]
    incoming = sorted(
        (r for r in data.refs if r.dst_file == rel),
        key=lambda r: (r.src_file, r.line, r.dst_name),
    )
    pr = _pagerank(data)
    order = sorted(pr, key=lambda p: (-pr.get(p, 0.0), p))
    rank_pos = order.index(rel) + 1 if rel in order else 0
    payload: dict[str, Any] = {
        "cmd": "refs",
        "path": rel,
        "lang": entry.lang,
        "size": entry.size,
        "pagerank": round(float(pr.get(rel, 0.0)), _RND),
        "pagerank_rank": rank_pos,
        "files": len(data.files),
        "resolved_count": len(resolved),
        "unresolved_count": len(unresolved),
        "symbols": [
            {
                "name": s.name,
                "kind": s.kind,
                "line": s.line,
                "end_line": s.end_line,
                "signature": s.signature,
            }
            for s in syms
        ],
        "refs": [
            {
                "dst_name": r.dst_name,
                "dst_file": r.dst_file,
                "line": r.line,
                "resolved": bool(r.dst_file),
            }
            for r in outgoing
        ],
        "incoming": [
            {"src_file": r.src_file, "dst_name": r.dst_name, "line": r.line} for r in incoming
        ],
    }
    if as_json:
        print(json.dumps(payload, sort_keys=True))
        return 0
    print(_refs_human(rel, entry.lang, entry.size, payload, syms, resolved, unresolved, incoming))
    return 0


def _refs_human(
    rel: str,
    lang: str,
    size: int,
    payload: dict[str, Any],
    syms: list[Any],
    resolved: list[Any],
    unresolved: list[Any],
    incoming: list[Any],
) -> str:
    """Human rendering of the ``refs`` report (fixed section order)."""
    lines = [f"file: {rel} (lang={lang}, {size} B)"]
    lines.append(
        f"pagerank: {payload['pagerank']:.6f} — rank {payload['pagerank_rank']}"
        f" of {payload['files']}"
    )
    lines.append(f"symbols ({len(syms)}):")
    for s in syms:
        lines.append(f"  L{s.line}-{s.end_line}  {s.kind:<7} {s.name}")
    lines.append(f"refs resolved ({len(resolved)}):")
    for r in resolved:
        lines.append(f"  → {r.dst_name} → {r.dst_file} (L{r.line})")
    lines.append(f"refs unresolved ({len(unresolved)}):")
    for r in unresolved:
        lines.append(f"  → {r.dst_name} (L{r.line}) — no in-repo target")
    lines.append(f"incoming refs ({len(incoming)}):")
    for r in incoming:
        lines.append(f"  ← {r.src_file}:{r.line} {r.dst_name}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# codegraph block — exactly what the agent sees
# ---------------------------------------------------------------------------


def block(root: Path, as_json: bool) -> int:
    """Print the managed block body from AGENTS.md (``codegraph block``).

    Absent/malformed block is a valid answer: a clean "no block" message and
    exit 0. ``--json`` wraps ``{present, bytes, body}`` (body is null when
    absent).
    """
    path = root / AGENTS_FILENAME
    body = read_block(path)
    if as_json:
        print(
            json.dumps(
                {
                    "cmd": "block",
                    "path": str(path),
                    "present": body is not None,
                    "bytes": byte_len(body) if body is not None else 0,
                    "body": body,
                },
                sort_keys=True,
            )
        )
        return 0
    if body is None:
        print(f"codegraph block: no managed block in {path} — run `codegraph init` first")
        return 0
    print(body)
    return 0


# ---------------------------------------------------------------------------
# codegraph selftest — end-to-end install sanity
# ---------------------------------------------------------------------------


def _build_sandbox(sandbox: Path) -> None:
    """Synthetic repo: two packages, cross-imports, a test file, pytest.ini."""
    (sandbox / "auth").mkdir(parents=True)
    (sandbox / "util").mkdir()
    (sandbox / "tests").mkdir()
    (sandbox / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (sandbox / "auth" / "__init__.py").write_text(
        "from auth.session import Session\n", encoding="utf-8"
    )
    (sandbox / "auth" / "session.py").write_text(
        "class Session:\n"
        '    """Token session with expiry."""\n'
        "    def refresh(self, ttl=3600):\n"
        "        return ttl\n",
        encoding="utf-8",
    )
    (sandbox / "auth" / "report.py").write_text(
        "from util.ids import new_id\n"
        "from auth.session import Session\n"
        "def report(user_id):\n"
        "    return Session(), new_id(user_id)\n",
        encoding="utf-8",
    )
    (sandbox / "util" / "__init__.py").write_text("", encoding="utf-8")
    (sandbox / "util" / "ids.py").write_text("def new_id(n=8):\n    return n * 'a'\n", encoding="utf-8")
    (sandbox / "tests" / "test_session.py").write_text(
        "from auth.session import Session\n"
        "def test_refresh():\n"
        "    assert Session().refresh(1) == 1\n",
        encoding="utf-8",
    )


def _focus_args(sandbox: Path, issue: str, budget: int) -> argparse.Namespace:
    """A fully-populated focus namespace (``cli='generic'`` for determinism)."""
    return argparse.Namespace(
        root=str(sandbox), issue=issue, issue_file=None, cli="generic", budget=budget, json=False
    )


def _check(fn: Callable[[], tuple[bool, str]], checks: list[tuple[str, bool, str]], name: str) -> None:
    """Run one check, appending (name, ok, detail); a raising check fails, never crashes."""
    try:
        ok, detail = fn()
    except Exception as exc:  # a selftest check must never crash the run
        LOGGER.debug("selftest check %s raised", name, exc_info=True)
        ok, detail = False, f"error: {type(exc).__name__}: {exc}"
    checks.append((name, ok, detail))


def _check_index(data: IndexData) -> tuple[bool, str]:
    """The sandbox was indexed: enough files/symbols/refs to exercise the pipeline."""
    files, syms, refs_n = len(data.files), len(data.symbols()), len(data.refs)
    ok = files >= 4 and syms >= 4 and refs_n >= 2
    detail = f"{files} files, {syms} symbols, {refs_n} refs"
    return ok, detail if ok else "too sparse — " + detail


def _check_cache(cache: IndexCache, data: IndexData) -> tuple[bool, str]:
    """The cache round-trips: reload yields the same files and recorded root."""
    loaded = cache.load()
    if loaded is None:
        return False, "reload returned None"
    ok = set(loaded.files) == set(data.files) and loaded.root == data.root
    return ok, f"reload matches ({len(loaded.files)} files, root recorded)"


def _check_deterministic(bodies: tuple[str, str]) -> tuple[bool, str]:
    """G2-style: two ``atlas.assemble`` runs on identical inputs are byte-identical."""
    a, b = bodies
    if not a:
        return False, "assembled body is empty"
    if a != b:
        return False, "two assemblies differ"
    return True, f"two assemblies byte-identical ({byte_len(a)} B)"


def _check_focus_write(rc: int, block_body: str | None) -> tuple[bool, str]:
    """The focus stage exited 0 and wrote a block that carries PLAN leads."""
    if rc != 0:
        return False, f"cli_focus.run exited {rc}"
    if block_body is None:
        return False, "no block written"
    if "LEADS (verify, don't trust):" not in block_body:
        return False, "block has no PLAN leads"
    return True, f"block written ({byte_len(block_body)} B, PLAN leads present)"


def _check_budget(
    block_body: str | None, budget: int, assemble: Any, data: IndexData, issue: str
) -> tuple[bool, str]:
    """G3-style: the written block fits *budget*; a 512..4096 sweep never exceeds."""
    if block_body is None:
        return False, "block absent"
    if byte_len(block_body) > budget:
        return False, f"block {byte_len(block_body)} B > budget {budget} B"
    for b in (512, 1024, 2048, 4096):
        if byte_len(assemble(data, issue, "generic", b)) > b:
            return False, f"sweep budget {b} exceeded"
    return True, f"block {byte_len(block_body)}/{budget} B; sweep 512..4096 B within budget"


def _check_no_lab(block_body: str | None) -> tuple[bool, str]:
    """No LAB section may appear when no ``.codegraph/profile.txt`` exists."""
    if block_body is None:
        return False, "block absent"
    if any(line.strip() == "LAB" for line in block_body.splitlines()):
        return False, "LAB section present without a profile"
    return True, "no LAB section (sandbox has no profile)"


def _check_runner(block_body: str | None) -> tuple[bool, str]:
    """The pytest.ini in the sandbox must be detected as the TESTS runner."""
    if block_body is None:
        return False, "block absent"
    for line in block_body.splitlines():
        if line.startswith("- runner:") and "pytest" in line:
            return True, line.removeprefix("- runner: ").strip()
    return False, "no pytest runner row (pytest.ini present but undetected)"


def _check_markers(agents: Path, block_body: str | None) -> tuple[bool, str]:
    """G1-style: exactly one well-formed marker pair; outside content preserved."""
    try:
        text = agents.read_text(encoding="utf-8")
    except OSError as exc:
        return False, f"unreadable AGENTS.md: {exc}"
    starts, ends = text.count(BLOCK_START), text.count(BLOCK_END)
    body = read_block(agents)
    problems: list[str] = []
    if starts != 1 or ends != 1:
        problems.append(f"{starts} start / {ends} end markers")
    if body is None or body != block_body:
        problems.append("read_block does not round-trip the body")
    if _SANDBOX_USER_TEXT not in text:
        problems.append("outside-marker content lost")
    if problems:
        return False, "; ".join(problems)
    return True, "one well-formed block; outside-marker content preserved"


def _check_idempotent(
    agents: Path, cli_focus: Any, sandbox: Path, issue: str, budget: int
) -> tuple[bool, str]:
    """G7-style: re-running focus leaves AGENTS.md byte-identical."""
    before = _sha(agents)
    rc = cli_focus.run(_focus_args(sandbox, issue, budget))
    after = _sha(agents)
    if rc != 0:
        return False, f"re-run exited {rc}"
    if before != after:
        return False, "re-run changed AGENTS.md"
    return True, "re-run left AGENTS.md byte-identical"


def _check_remove_restores(agents: Path) -> tuple[bool, str]:
    """G1-style: remove_block restores the original file bytes exactly."""
    remove_block(agents)
    restored = agents.read_bytes() if agents.exists() else b""
    if restored == _SANDBOX_USER_TEXT.encode("utf-8"):
        return True, "original content restored byte-for-byte"
    return False, f"content after remove: {restored[:40]!r}"


def selftest(budget: int, issue: str, keep: bool, as_json: bool) -> int:
    """End-to-end install sanity in a throwaway sandbox (``codegraph selftest``).

    Builds a synthetic repo (two packages, cross-imports, a test file,
    pytest.ini), runs the real pipeline in-process (``index.update`` →
    ``atlas.assemble`` → focus write via ``focus.cli_focus.run``) and verifies
    the guarantee checks inline. The caller's repository is never touched;
    the sandbox is removed unless *keep* is set. Exit 0 only when every
    check passes.
    """
    sandbox = Path(tempfile.mkdtemp(prefix="codegraph-selftest-"))
    checks: list[tuple[str, bool, str]] = []
    block_body: str | None = None
    try:
        _build_sandbox(sandbox)
        agents = sandbox / AGENTS_FILENAME
        agents.write_text(_SANDBOX_USER_TEXT, encoding="utf-8")
        cache = IndexCache(sandbox / ".codegraph")

        state: dict[str, Any] = {}
        pipeline_error = ""
        try:
            _run_pipeline(sandbox, agents, cache, issue, int(budget), state)
        except Exception as exc:  # report as failed checks, never crash
            pipeline_error = f"{type(exc).__name__}: {exc}"
            LOGGER.debug("selftest pipeline failed", exc_info=True)

        if pipeline_error:
            for name in _CHECK_NAMES:
                checks.append((name, False, f"not run — pipeline failed: {pipeline_error}"))
        else:
            data: IndexData = state["data"]
            assemble = state["assemble"]
            cli_focus = state["cli_focus"]
            block_body = state["block"]
            _check(lambda: _check_index(data), checks, "index")
            _check(lambda: _check_cache(cache, data), checks, "cache")
            _check(lambda: _check_deterministic(state["bodies"]), checks, "g2-atlas-deterministic")
            _check(lambda: _check_focus_write(state["focus_rc"], block_body), checks, "focus-write")
            _check(
                lambda: _check_budget(block_body, int(budget), assemble, data, issue),
                checks,
                "g3-budget",
            )
            _check(lambda: _check_no_lab(block_body), checks, "no-lab")
            _check(lambda: _check_runner(block_body), checks, "tests-runner")
            _check(lambda: _check_markers(agents, block_body), checks, "g1-block-markers")
            _check(
                lambda: _check_idempotent(agents, cli_focus, sandbox, issue, int(budget)),
                checks,
                "g7-focus-idempotent",
            )
            _check(lambda: _check_remove_restores(agents), checks, "g1-remove-restores")

        ok = all(good for _, good, _ in checks)
        passed = sum(1 for _, good, _ in checks if good)
        if as_json:
            print(
                json.dumps(
                    {
                        "cmd": "selftest",
                        "ok": ok,
                        "sandbox": str(sandbox),
                        "kept": bool(keep),
                        "block_bytes": byte_len(block_body) if block_body is not None else 0,
                        "checks": {name: {"ok": good, "detail": detail} for name, good, detail in checks},
                    },
                    sort_keys=True,
                )
            )
        else:
            print(f"codegraph selftest: sandbox {sandbox}")
            for name, good, detail in checks:
                print(f"{name:<22} {'PASS' if good else 'FAIL'} — {detail}")
            print(f"selftest: {'PASS' if ok else 'FAIL'} ({passed}/{len(checks)} checks)")
        return 0 if ok else 1
    finally:
        if not keep:
            shutil.rmtree(sandbox, ignore_errors=True)


def _run_pipeline(
    sandbox: Path, agents: Path, cache: IndexCache, issue: str, budget: int, state: dict[str, Any]
) -> None:
    """Run the real pipeline in-process and stash every artifact into *state*."""
    from codegraph.atlas import assemble  # lazy: atlas is an optional sibling
    from codegraph.focus import cli_focus  # lazy: focus is an optional sibling
    from codegraph.index import update  # lazy: heavy tree-sitter stack

    data = update(root=str(sandbox), langs=None, workers=1, cache=cache)
    state["data"] = data
    state["assemble"] = assemble
    state["cli_focus"] = cli_focus
    state["bodies"] = (assemble(data, issue, "generic", budget), assemble(data, issue, "generic", budget))
    state["focus_rc"] = cli_focus.run(_focus_args(sandbox, issue, budget))
    state["block"] = read_block(agents)
