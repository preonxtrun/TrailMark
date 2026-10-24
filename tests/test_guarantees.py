"""Conformance suite — the hard guarantees of the trailmark layer.

Every test here enforces an invariant listed in docs/GUARANTEES.md. These must hold
for ANY implementation generation (Python core, Rust core, future rewrites): the
same scenarios are mirrored in the Rust parity tests (crates/cli/tests).

G1  Non-destruction   — only content between the markers ever changes
G2  Determinism       — same inputs → byte-identical output
G3  Budget            — the block never exceeds its byte budget
G4  No leakage        — the issue text is the ONLY task input
G5  Never crashes     — adversarial repos are indexed gracefully
G6  Filesystem purity — init/focus writes ONLY AGENTS.md and .trailmark/
G7  Idempotence       — re-running focus with the same issue changes nothing
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pytest

from trailmark.atlas import assemble
from trailmark.cache import IndexCache
from trailmark.emit.agents_md import read_block, remove_block, write_block
from trailmark.focus.outlines import generate_top
from trailmark.index import update
from trailmark.model import BLOCK_START, DEFAULT_BUDGET_BYTES, IndexData

BODY = "# TEST ATLAS\nMAP\n- x.py:1 foo()"  # read_block strips surrounding newlines


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): _sha(p)
        for p in sorted(root.rglob("*"))
        if p.is_file() and ".trailmark" not in p.parts and p.name != "AGENTS.md"
    }


def make_repo(tmp_path: Path) -> Path:
    """Small synthetic repo: two packages, cross-imports, a test file."""
    (tmp_path / "auth").mkdir(parents=True)
    (tmp_path / "util").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "auth" / "session.py").write_text(
        'class Session:\n    """Token session."""\n'
        "    def refresh(self, ttl=3600):\n        return ttl\n",
        encoding="utf-8",
    )
    (tmp_path / "auth" / "__init__.py").write_text("from auth.session import Session\n")
    (tmp_path / "util" / "ids.py").write_text("def new_id(n=8):\n    return n * 'a'\n")
    (tmp_path / "tests" / "test_session.py").write_text(
        "from auth.session import Session\n"
        "def test_refresh():\n    assert Session().refresh(1) == 1\n",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------- G1


@pytest.mark.parametrize(
    "original",
    [
        "",
        "user notes with no markers\n",
        "emoji 🚀 and unicode naïve\n",
        "no trailing newline",
        "line1\n" * 5000,
    ],
    ids=["empty", "plain", "unicode", "no-newline", "large"],
)
def test_g1_non_destruction(tmp_path: Path, original: str):
    p = tmp_path / "AGENTS.md"
    p.write_text(original, encoding="utf-8")
    write_block(p, BODY)
    assert read_block(p) == BODY
    remove_block(p)
    assert p.exists(), "remove must restore, never delete"
    assert p.read_text(encoding="utf-8") == original, "content outside markers must survive"


def test_g1_write_refuses_malformed_markers(tmp_path: Path):
    """A fake/dangling marker (e.g. inside a code fence) must freeze the writer, not mutate."""
    p = tmp_path / "AGENTS.md"
    original = "code fence with fake marker:\n```\n<!-- trailmark:start v1 -->\n```\n"
    p.write_text(original, encoding="utf-8")
    with pytest.raises(ValueError):
        write_block(p, BODY)
    assert p.read_text(encoding="utf-8") == original, "refusal must leave the file untouched"
    remove_block(p)  # cleanup is best-effort on remove
    assert BLOCK_START not in p.read_text(encoding="utf-8")
    assert "code fence with fake marker" in p.read_text(encoding="utf-8")


# ---------------------------------------------------------------- G2 / G3


def _index(tmp_path: Path) -> tuple[IndexData, IndexCache]:
    repo = make_repo(tmp_path)
    cache = IndexCache(repo / ".trailmark")
    data = update(root=str(repo), langs=None, workers=1, cache=cache)
    return data, cache


def test_g2_determinism(tmp_path: Path):
    data, _ = _index(tmp_path)
    issue = "session refresh drops tokens on redirect"
    a = assemble(data, issue, "generic", DEFAULT_BUDGET_BYTES)
    b = assemble(data, issue, "generic", DEFAULT_BUDGET_BYTES)
    assert a == b, "same inputs must produce byte-identical bodies"


def test_g3_budget_sweep(tmp_path: Path):
    data, _ = _index(tmp_path)
    from trailmark.util.tokens import byte_len

    for budget in (512, 768, 1024, 1536, 2048, 4096):
        body = assemble(data, "refresh session tokens", "generic", budget)
        assert byte_len(body) <= budget, f"budget {budget} exceeded"


# ---------------------------------------------------------------- G4


def test_g4_no_leakage(tmp_path: Path):
    """The issue text is the ONLY task input: record canaries must never surface."""
    repo = make_repo(tmp_path)
    cache = IndexCache(repo / ".trailmark")
    update(root=str(repo), langs=None, workers=1, cache=cache)

    canary_test = "tests/test_leak.py::test_canary_98123"
    canary_patch = "GOLD_PATCH_CANARY_XYZQ"
    record = {  # what the bench harness holds — trailmark must never see these
        "problem_statement": "session refresh drops tokens on redirect",
        "FAIL_TO_PASS": json.dumps([canary_test]),
        "patch": canary_patch,
    }
    issue = record["problem_statement"]  # the ONLY field handed to the layer

    from trailmark.focus.cli_focus import run as focus_run

    args = argparse.Namespace(
        root=str(repo), issue=issue, issue_file=None, cli="generic", budget=DEFAULT_BUDGET_BYTES, json=False
    )
    assert focus_run(args) == 0

    artifacts = (repo / "AGENTS.md").read_text(encoding="utf-8")
    for path in (repo / ".trailmark").rglob("*"):
        if path.is_file():
            artifacts += path.read_text(encoding="utf-8", errors="ignore")
    assert canary_test not in artifacts
    assert canary_patch not in artifacts


# ---------------------------------------------------------------- G5


def test_g5_adversarial_repo_never_crashes(tmp_path: Path):
    repo = tmp_path
    (repo / "bin.dat").write_bytes(bytes(range(256)) * 40)
    (repo / "empty.py").write_text("", encoding="utf-8")
    (repo / "nulls.py").write_bytes(b"\x00" * 1024)
    (repo / "huge.txt").write_text("x" * (2 * 1024 * 1024), encoding="utf-8")
    (repo / "broken.py").write_text("def oops(:\n  ^^^ not python\n", encoding="utf-8")
    (repo / "wéird_ünïcode.py").write_text("def héllo():\n    return 'wörld'\n")
    deep = repo / "a" / "b" / "c" / "d" / "e"
    deep.mkdir(parents=True)
    (deep / "deep.py").write_text("def deep_fn():\n    return 1\n")

    cache = IndexCache(repo / ".trailmark")
    data = update(root=str(repo), langs=None, workers=1, cache=cache)  # must not raise
    body = assemble(data, "deep function héllo", "generic", 2048)  # must not raise
    assert isinstance(body, str)


# ---------------------------------------------------------------- G6


def test_g6_filesystem_purity(tmp_path: Path):
    """init/focus may only write AGENTS.md and .trailmark/** — nothing else."""
    repo = make_repo(tmp_path)
    before = _snapshot(repo)

    cache = IndexCache(repo / ".trailmark")
    data = update(root=str(repo), langs=None, workers=1, cache=cache)
    body = assemble(data, "session refresh", "generic", 2048)
    write_block(repo / "AGENTS.md", body)
    generate_top(data, repo / ".trailmark" / "outlines", k=8)

    after = _snapshot(repo)
    changed = {path for path in set(before) | set(after) if before.get(path) != after.get(path)}
    deleted = set(before) - set(after)
    assert not deleted, f"files were deleted: {sorted(deleted)}"
    assert changed <= {"AGENTS.md"}, f"unexpected writes: {sorted(changed)}"


# ---------------------------------------------------------------- G7


def test_g7_focus_idempotence(tmp_path: Path):
    from trailmark.focus.cli_focus import run as focus_run

    make_repo(tmp_path)
    cache = IndexCache(tmp_path / ".trailmark")
    update(root=str(tmp_path), langs=None, workers=1, cache=cache)

    def run_once():
        args = argparse.Namespace(
            root=str(tmp_path),
            issue="session refresh drops tokens",
            issue_file=None,
            cli="generic",
            budget=2048,
            json=False,
        )
        assert focus_run(args) == 0
        return _sha(tmp_path / "AGENTS.md")

    first = run_once()
    second = run_once()
    assert first == second, "re-running focus with the same issue must not change the block"


# ---------------------------------------------------------------- G9

#: Harness vocabulary that must NEVER appear in the injected block. The layer is
#: CLI-agnostic: the only surface is a plain AGENTS.md readable by any agent —
#: no tool names, no vendor features, no runtime mechanisms.
HARNESS_DENYLIST = [
    "mcp",
    "subagent",
    "sub-agent",
    "parallel tool",
    "tool block",
    "claude",
    "codex",
    "opencode",
    "cursor",
    "cline",
    "aider",
    " windsurf",  # leading space: avoid matching common substrings
]


def test_g9_layer_is_cli_agnostic(tmp_path: Path):
    import re

    data, _ = _index(tmp_path)
    body = assemble(data, "session refresh drops tokens on redirect", "generic", 2048)
    lowered = body.lower()
    for term in HARNESS_DENYLIST:
        assert term not in lowered, f"harness vocabulary {term!r} leaked into the block"
    # no capitalized vendor tool names anywhere
    for tool in ("Read", "Grep", "Glob", "Bash", "Edit", "MultiEdit", "Write"):
        assert not re.search(rf"\b{tool}\b", body), f"tool name {tool!r} leaked into the block"
    # every path reference is repo-relative or trailmark-private
    for token in body.replace("(", " ").replace(")", " ").replace(":", " ").split():
        if "/" in token and not token.startswith((".trailmark", "#")):
            assert not token.startswith(("/", "~")), f"absolute path leaked: {token}"
