"""Tests for the DIAGNOSTICS toolkit: why / search / refs / block / selftest.

The fixture repo has one deliberately unresolved dotted import (``import
os.path`` in ``beta/factory.py``) so ``refs`` can be asserted for both
resolved and unresolved reference handling. The explain hook is pinned to
``ranker.rank`` so the two surfaces can never drift.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from codegraph import cli
from codegraph.cache import IndexCache
from codegraph.emit.agents_md import write_block
from codegraph.focus import ranker
from codegraph.index import update
from codegraph.util.tokens import byte_len

ISSUE = "greet"


# ---------------------------------------------------------------------------
# fixture
# ---------------------------------------------------------------------------


def _repo(tmp_path: Path) -> Path:
    """Two packages, cross-imports, a test file — plus one unresolved import."""
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "alpha" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "alpha" / "greeter.py").write_text(
        'def greet(name):\n    """Say hello."""\n    return "hello " + name\n',
        encoding="utf-8",
    )
    (tmp_path / "beta" / "factory.py").write_text(
        "import os.path\n"
        "from alpha.greeter import greet\n"
        "def make_greeting(user):\n"
        "    return greet(user)\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_greeter.py").write_text(
        "from alpha.greeter import greet\n"
        "def test_greet():\n"
        "    assert greet('x').startswith('hello')\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """Indexed fixture repo (real pipeline, workers=1, cache persisted)."""
    base = _repo(tmp_path)
    update(root=str(base), langs=None, workers=1, cache=IndexCache(base / ".codegraph"))
    return base


def _run(cli_args: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    """Run cli.main, returning (exit code, stdout, stderr)."""
    rc = cli.main(cli_args)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _run_json(cli_args: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
    """Run cli.main in --json mode and parse the payload."""
    rc, out, _err = _run([*cli_args, "--json"], capsys)
    return rc, json.loads(out)


# ---------------------------------------------------------------------------
# why (explain hook)
# ---------------------------------------------------------------------------


class TestWhy:
    def test_deterministic_two_runs_identical(self, repo: Path, capsys) -> None:
        args = ["why", ISSUE, "--root", str(repo)]
        first = _run_json(args, capsys)
        second = _run_json(args, capsys)
        assert first[0] == second[0] == 0
        assert first[1] == second[1], "why must be byte-identical across runs"

    def test_json_payload_shape(self, repo: Path, capsys) -> None:
        rc, payload = _run_json(["why", ISSUE, "--root", str(repo)], capsys)
        assert rc == 0
        assert payload["cmd"] == "why"
        assert payload["issue"] == ISSUE
        assert payload["tokens"] == ["greet"]
        assert sum(payload["weights"].values()) == pytest.approx(1.0)
        assert payload["count"] == len(payload["leads"]) > 0
        for lead in payload["leads"]:
            comps = lead["components"]
            assert set(comps) == {"lexical", "graph", "test", "prior"}
            assert all(0.0 <= v <= 1.0 for v in comps.values())
            assert 0.0 <= lead["score"] <= 1.0
            # final score is the weight sum of the components (rounding tolerance)
            expected = (
                0.45 * comps["lexical"]
                + 0.25 * comps["graph"]
                + 0.15 * comps["test"]
                + 0.15 * comps["prior"]
            )
            assert lead["score"] == pytest.approx(expected, abs=1e-5)
            assert lead["symbol"]
            assert 1 <= lead["start_line"] <= lead["end_line"]
            assert lead["hit"] is None or set(lead["hit"]) == {"name", "line", "bm25"}
            assert set(lead["raw"]) == {"lexical", "graph", "test", "prior"}
        assert any(l["path"] == "alpha/greeter.py" for l in payload["leads"])

    def test_human_output_has_table_and_top_leads(self, repo: Path, capsys) -> None:
        rc, out, _err = _run(["why", ISSUE, "--root", str(repo)], capsys)
        assert rc == 0
        assert "codegraph why:" in out
        assert "weights: lexical=0.45 graph=0.25 test=0.15 prior=0.15" in out
        assert "alpha/greeter.py" in out
        assert "top leads (what `focus` injects into PLAN):" in out
        assert re.search(r"^1\. \S+:\d+-\d+ — .+ \(0\.\d+\)$", out, re.MULTILINE)

    def test_k_limits_candidate_rows(self, repo: Path, capsys) -> None:
        rc, payload = _run_json(["why", ISSUE, "--root", str(repo), "--k", "1"], capsys)
        assert rc == 0
        assert len(payload["leads"]) <= 1

    def test_missing_cache_exit_2(self, tmp_path: Path, capsys) -> None:
        rc, _out, err = _run(["why", ISSUE, "--root", str(tmp_path)], capsys)
        assert rc == 2
        assert "run `codegraph init` first" in err

    def test_root_flag_before_subcommand(self, repo: Path, capsys) -> None:
        rc, payload = _run_json(["--root", str(repo), "why", ISSUE], capsys)
        assert rc == 0
        assert payload["leads"]


# ---------------------------------------------------------------------------
# explain hook drift pin (must always equal rank)
# ---------------------------------------------------------------------------


class TestExplainHook:
    @pytest.mark.parametrize("k", [1, 3, 8])
    def test_explain_leads_equal_rank(self, repo: Path, k: int) -> None:
        cache = IndexCache(repo / ".codegraph")
        data = cache.load()
        assert data is not None
        explained = ranker.explain(ISSUE, data, cache, k)
        assert [lead for lead, _ in explained] == ranker.rank(data, cache, ISSUE, k=k)

    def test_explain_vague_issue_fallback_matches_rank(self, repo: Path) -> None:
        cache = IndexCache(repo / ".codegraph")
        data = cache.load()
        assert data is not None
        explained = ranker.explain("zzzqqq unm4tchable", data, cache, 5)
        assert [lead for lead, _ in explained] == ranker.rank(
            data, cache, "zzzqqq unm4tchable", k=5
        )
        assert explained, "pagerank fallback must still produce leads"

    def test_explain_empty_inputs(self, repo: Path) -> None:
        cache = IndexCache(repo / ".codegraph")
        data = cache.load()
        assert data is not None
        assert ranker.explain(ISSUE, data, cache, 0) == []
        from codegraph.model import IndexData as _IndexData

        assert ranker.explain(ISSUE, _IndexData(root="", files={}, refs=[]), cache, 8) == []


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


class TestSearch:
    def test_hits_best_first_with_fields(self, repo: Path, capsys) -> None:
        rc, payload = _run_json(["search", "greet", "--root", str(repo)], capsys)
        assert rc == 0
        assert payload["cmd"] == "search"
        assert payload["query"] == "greet"
        hits = payload["hits"]
        assert hits, "the greet symbol must be findable"
        scores = [h["score"] for h in hits]
        assert scores == sorted(scores), "hits must be best (lowest bm25) first"
        for hit in hits:
            assert set(hit) == {"file", "name", "kind", "line", "score"}
        assert any(h["file"] == "alpha/greeter.py" and h["name"] == "greet" for h in hits)

    def test_human_output(self, repo: Path, capsys) -> None:
        rc, out, _err = _run(["search", "greet", "--root", str(repo)], capsys)
        assert rc == 0
        assert "hits (best first)" in out
        assert "alpha/greeter.py" in out

    def test_no_hits_is_exit_0(self, repo: Path, capsys) -> None:
        rc, payload = _run_json(["search", "zzzqqq", "--root", str(repo)], capsys)
        assert rc == 0
        assert payload["hits"] == []

    def test_missing_cache_exit_2(self, tmp_path: Path, capsys) -> None:
        rc, _out, err = _run(["search", "greet", "--root", str(tmp_path)], capsys)
        assert rc == 2
        assert "run `codegraph init` first" in err


# ---------------------------------------------------------------------------
# refs
# ---------------------------------------------------------------------------


class TestRefs:
    def test_resolved_and_unresolved(self, repo: Path, capsys) -> None:
        rc, payload = _run_json(["refs", "beta/factory.py", "--root", str(repo)], capsys)
        assert rc == 0
        assert payload["cmd"] == "refs"
        assert payload["path"] == "beta/factory.py"
        assert payload["pagerank"] > 0
        assert 1 <= payload["pagerank_rank"] <= payload["files"]
        assert any(s["name"] == "make_greeting" and s["kind"] == "def" for s in payload["symbols"])
        resolved = [r for r in payload["refs"] if r["resolved"]]
        unresolved = [r for r in payload["refs"] if not r["resolved"]]
        assert payload["resolved_count"] == len(resolved) > 0
        assert payload["unresolved_count"] == len(unresolved) == 1
        assert any(
            r["dst_name"] == "greet" and r["dst_file"] == "alpha/greeter.py" for r in resolved
        )
        assert unresolved[0]["dst_name"] == "os.path"
        assert unresolved[0]["dst_file"] == ""

    def test_incoming_refs_shown(self, repo: Path, capsys) -> None:
        rc, payload = _run_json(["refs", "alpha/greeter.py", "--root", str(repo)], capsys)
        assert rc == 0
        sources = {inc["src_file"] for inc in payload["incoming"]}
        assert "beta/factory.py" in sources
        assert "tests/test_greeter.py" in sources

    def test_absolute_path_normalized(self, repo: Path, capsys) -> None:
        rc, payload = _run_json(["refs", str(repo / "beta" / "factory.py"), "--root", str(repo)], capsys)
        assert rc == 0
        assert payload["path"] == "beta/factory.py"

    def test_unknown_path_exit_1_with_hint(self, repo: Path, capsys) -> None:
        rc, _out, err = _run(["refs", "factory.py", "--root", str(repo)], capsys)
        assert rc == 1
        assert "not in the index" in err
        assert "beta/factory.py" in err, "basename hint must suggest the indexed file"

    def test_missing_cache_exit_2(self, tmp_path: Path, capsys) -> None:
        rc, _out, err = _run(["refs", "x.py", "--root", str(tmp_path)], capsys)
        assert rc == 2
        assert "run `codegraph init` first" in err

    def test_human_output_sections(self, repo: Path, capsys) -> None:
        rc, out, _err = _run(["refs", "beta/factory.py", "--root", str(repo)], capsys)
        assert rc == 0
        for section in ("symbols (1):", "refs resolved (2):", "refs unresolved (1):"):
            assert section in out
        assert "→ greet → alpha/greeter.py" in out
        assert "→ os.path (L1) — no in-repo target" in out


# ---------------------------------------------------------------------------
# block
# ---------------------------------------------------------------------------


class TestBlock:
    BODY = "MAP\n- alpha/ — code; hot: alpha/greeter.py:1 greet()\nTESTS\n- runner: python -m pytest -x -q"

    def test_absent_is_clean_exit_0(self, tmp_path: Path, capsys) -> None:
        rc, payload = _run_json(["block", "--root", str(tmp_path)], capsys)
        assert rc == 0
        assert payload == {
            "cmd": "block",
            "path": str(tmp_path / "AGENTS.md"),
            "present": False,
            "bytes": 0,
            "body": None,
        }

    def test_absent_human_message(self, tmp_path: Path, capsys) -> None:
        rc, out, _err = _run(["block", "--root", str(tmp_path)], capsys)
        assert rc == 0
        assert "no managed block" in out

    def test_present_json_wraps_body(self, repo: Path, capsys) -> None:
        write_block(repo / "AGENTS.md", self.BODY)
        rc, payload = _run_json(["block", "--root", str(repo)], capsys)
        assert rc == 0
        assert payload["present"] is True
        assert payload["body"] == self.BODY
        assert payload["bytes"] == byte_len(self.BODY)

    def test_present_human_prints_body_verbatim(self, repo: Path, capsys) -> None:
        write_block(repo / "AGENTS.md", self.BODY)
        rc, out, _err = _run(["block", "--root", str(repo)], capsys)
        assert rc == 0
        assert out == self.BODY + "\n"


# ---------------------------------------------------------------------------
# selftest
# ---------------------------------------------------------------------------


class TestSelftest:
    def test_passes_with_json_report(self, capsys) -> None:
        rc, payload = _run_json(["selftest"], capsys)
        assert rc == 0
        assert payload["cmd"] == "selftest"
        assert payload["ok"] is True
        assert payload["checks"], "expected a full check battery"
        assert set(payload["checks"]) == {
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
        }
        assert all(c["ok"] for c in payload["checks"].values()), payload["checks"]
        assert payload["block_bytes"] > 0
        assert payload["kept"] is False

    def test_passes_in_human_mode(self, capsys) -> None:
        rc, out, _err = _run(["selftest"], capsys)
        assert rc == 0
        assert out.count("PASS") >= 10
        assert "selftest: PASS (10/10 checks)" in out

    def test_keep_leaves_inspectable_sandbox(self, capsys) -> None:
        payload: dict = {}
        try:
            rc, payload = _run_json(["selftest", "--keep"], capsys)
            assert rc == 0
            sandbox = Path(payload["sandbox"])
            assert payload["kept"] is True
            assert sandbox.is_dir()
            assert (sandbox / ".codegraph" / "index.db").exists()
            outlines = sandbox / ".codegraph" / "outlines"
            assert outlines.is_dir() and any(outlines.iterdir()), "focus wrote outlines"
            # the final check removes the block again — the kept AGENTS.md holds
            # the restored outside-marker content (g1-remove-restores evidence)
            assert (sandbox / "AGENTS.md").read_text(encoding="utf-8") == "user notes\n"
        finally:
            shutil.rmtree(payload.get("sandbox", ""), ignore_errors=True)
