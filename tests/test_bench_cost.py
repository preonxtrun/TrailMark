"""Tests for the bench harness — all runnable WITHOUT docker.

Covers: cost adapters against handcrafted fixtures (exact token sums + hand-computed
cost math), defensive degradation on garbage, pricing table/fallback, the no-leakage
gate, the patch pathspec against a real temp git repo, report determinism with a seeded
bootstrap, and `python -m bench.run --dry` against a local instances file.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # bench/ is a repo-root package, not an installed one
    sys.path.insert(0, str(REPO_ROOT))

from bench import cost
from bench.arms import (
    PATCH_PATHSPEC,
    SETUP_CONTROL,
    SETUP_TREATMENT,
    LeakageError,
    assert_no_leakage,
    patch_cmd,
    write_issue_file,
)
from bench.report import load_records, paired_report, render_markdown
from bench.run import image_tag, resolve_image

FIXTURES = REPO_ROOT / "bench" / "fixtures"
DockerEnv = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}


def _lines(name: str) -> list[str]:
    return (FIXTURES / name).read_text(encoding="utf-8").splitlines()


def _cost(inp: int, cr: int, cw: int, out: int, price: tuple[float, float, float, float]) -> float:
    """Hand-computed token math: (in*p_in + cr*p_cr + cw*p_cw + out*p_out) / 1M."""
    p_in, p_cr, p_cw, p_out = price
    return (inp * p_in + cr * p_cr + cw * p_cw + out * p_out) / 1_000_000


# --------------------------------------------------------------------------
# cost adapters vs fixtures
# --------------------------------------------------------------------------


def test_parse_claude_fixture():
    usage = cost.parse_claude(_lines("claude.jsonl"))
    assert usage.input_tokens == 4 + 5 + 3
    assert usage.cache_read_tokens == 0 + 1523 + 2981
    assert usage.cache_write_tokens == 1523 + 0 + 112
    assert usage.output_tokens == 97 + 212 + 55
    assert usage.turns == 3
    assert usage.model == "claude-sonnet-4-5-20250929"
    assert usage.reported_cost_usd == pytest.approx(0.012)
    assert usage.estimated is False
    # The result event carries the cumulative session total; it must NOT be double-counted
    # on top of the assistant events (a naive sum over all events would return 2x here).
    price = cost.PRICES["claude-sonnet-4"]
    assert cost.cost_usd(usage) == pytest.approx(_cost(12, 4504, 1635, 364, price))
    assert cost.cost_usd(usage) == pytest.approx(0.01297845)


def test_parse_codex_fixture():
    usage = cost.parse_codex(_lines("codex.jsonl"))
    assert usage.input_tokens == 1523 + 2411
    assert usage.cache_read_tokens == 0 + 1820
    assert usage.cache_write_tokens == 0
    # codex reports reasoning separately; billed as output, so it joins the output bucket
    assert usage.output_tokens == (97 + 31) + (212 + 64)
    assert usage.turns == 2
    assert usage.model == "gpt-5.1-codex"
    assert usage.reported_cost_usd is None
    assert cost.cost_usd(usage) == pytest.approx(_cost(3934, 1820, 0, 404, cost.PRICES["gpt-5"]))
    assert cost.cost_usd(usage) == pytest.approx(0.009185)


def test_parse_codex_cumulative_totals_win():
    # message-style codex output: total_token_usage is cumulative (max wins);
    # last_token_usage is a per-turn delta and must be skipped to avoid double counting.
    lines = [
        (
            '{"msg":{"type":"token_count","info":{"total_token_usage":'
            '{"input_tokens":100,"cached_input_tokens":50,"output_tokens":20},'
            '"last_token_usage":{"input_tokens":60,"cached_input_tokens":30,"output_tokens":10}}}}'
        ),
        (
            '{"msg":{"type":"token_count","info":{"total_token_usage":'
            '{"input_tokens":180,"cached_input_tokens":90,"output_tokens":40},'
            '"last_token_usage":{"input_tokens":80,"cached_input_tokens":40,"output_tokens":20}}}}'
        ),
    ]
    usage = cost.parse_codex(lines)
    assert (usage.input_tokens, usage.cache_read_tokens, usage.output_tokens) == (180, 90, 40)
    assert usage.turns == 2  # two usage-bearing events


def test_parse_opencode_fixture():
    """Real capture from `opencode run --format json` (opencode v2.1.4, hyde model)."""
    usage = cost.parse_opencode(
        _lines("opencode.jsonl"), model="hyde-inference/hyde/glm-5.3-flash"
    )
    assert usage.input_tokens == 1538
    assert usage.output_tokens == 22 + 79  # output + reasoning, billed as output
    assert usage.cache_read_tokens == 3456
    assert usage.cache_write_tokens == 0
    assert usage.turns == 1
    assert usage.model == "hyde-inference/hyde/glm-5.3-flash"
    assert usage.reported_cost_usd is None  # cost field is 0 — no public pricing
    price = cost.PRICES["glm"]  # substring match for hyde/glm-5.3-flash (placeholder)
    assert cost.price_for("hyde-inference/hyde/glm-5.3-flash") == price
    assert cost.cost_usd(usage) == pytest.approx(_cost(1538, 3456, 0, 101, price))


def test_parsers_degrade_on_garbage():
    for parse in (cost.parse_claude, cost.parse_codex, cost.parse_opencode):
        usage = parse(["this is not json at all", "{broken", ""])
        assert usage.estimated is True
        assert usage.total_tokens > 0
        assert cost.cost_usd(usage) > 0.0  # unknown model → DEFAULT_PRICE, still billable
    empty = cost.parse_claude([])
    assert empty.total_tokens == 0
    assert empty.estimated is False
    assert empty.reported_cost_usd is None


def test_parse_dispatcher_unknown_cli_uses_generic_scanner():
    usage = cost.parse("mystery-cli", ['{"usage":{"input_tokens":10,"output_tokens":5}}'])
    assert (usage.input_tokens, usage.output_tokens) == (10, 5)


def test_price_matching_and_fallback():
    assert cost.PRICES["claude-sonnet-4"] == (3.0, 0.30, 3.75, 15.0)
    assert cost.DEFAULT_PRICE == (3.0, 0.30, 3.75, 15.0)
    assert cost.price_for("claude-sonnet-4-5-20250929") == cost.PRICES["claude-sonnet-4"]
    assert cost.price_for("claude-opus-4-1-20250805") == cost.PRICES["claude-opus"]
    assert cost.price_for("gpt-5.1-codex") == cost.PRICES["gpt-5"]
    assert cost.price_for("totally-unknown-7b") == cost.DEFAULT_PRICE
    assert cost.price_for(None) == cost.DEFAULT_PRICE
    million = cost.Usage(model=None, input_tokens=1_000_000)
    assert cost.cost_usd(million) == pytest.approx(cost.DEFAULT_PRICE[0])
    custom = cost.Usage(model="custom-model-x", input_tokens=1_000_000)
    assert cost.cost_usd(custom, prices={"custom": (9.0, 0.0, 0.0, 9.0)}) == pytest.approx(9.0)


# --------------------------------------------------------------------------
# leakage gate
# --------------------------------------------------------------------------

GOLD_PATCH = (
    "diff --git a/src/trailmark/parse.py b/src/trailmark/parse.py\n"
    "--- a/src/trailmark/parse.py\n"
    "+++ b/src/trailmark/parse.py\n"
    "@@ -10,7 +10,7 @@\n"
    "+    return sorted(nodes, key=lambda node: (node.path, node.name, node.kind))\n"
)

CLEAN_RECORD = {
    "instance_id": "acme__widget-101",
    "problem_statement": (
        "Title: sorting crashes on empty node lists\n\n"
        "When the parser receives an empty node list, collect() raises IndexError.\n"
        "Expected: it should return an empty result instead.\n"
    ),
    "FAIL_TO_PASS": '["tests/test_parse.py::test_empty_nodes"]',
    "PASS_TO_PASS": '["tests/test_parse.py::test_basic", "tests/test_parse.py::test_more"]',
    "patch": GOLD_PATCH,
}


def test_assert_no_leakage_passes_clean_record():
    assert_no_leakage(CLEAN_RECORD)  # must not raise


def test_assert_no_leakage_raises_on_key_marker():
    poisoned = {**CLEAN_RECORD, "problem_statement": "Fix the bug. See FAIL_TO_PASS below."}
    with pytest.raises(LeakageError):
        assert_no_leakage(poisoned)


def test_assert_no_leakage_raises_on_added_patch_line():
    added = "return sorted(nodes, key=lambda node: (node.path, node.name, node.kind))"
    assert len(added) >= 40
    poisoned = {**CLEAN_RECORD, "problem_statement": f"The fix is:\n{added}\nplease apply."}
    with pytest.raises(LeakageError):
        assert_no_leakage(poisoned)


def test_assert_no_leakage_raises_on_whole_field_embedded():
    poisoned = {
        **CLEAN_RECORD,
        "problem_statement": f"Run {CLEAN_RECORD['FAIL_TO_PASS']} when done.",
    }
    with pytest.raises(LeakageError):
        assert_no_leakage(poisoned)


def test_assert_no_leakage_allows_short_test_id_mention():
    # A problem statement naturally mentioning one short test id is NOT leakage
    # (chunk threshold: only >=40-char verbatim overlaps are flagged).
    poisoned = {
        **CLEAN_RECORD,
        "problem_statement": "pytest tests/test_parse.py::test_empty_nodes fails on empty input.",
    }
    assert_no_leakage(poisoned)  # must not raise


def test_write_issue_file_gates_and_roundtrips(tmp_path):
    path = write_issue_file(CLEAN_RECORD["problem_statement"], dest_dir=tmp_path, record=CLEAN_RECORD)
    assert path.read_text(encoding="utf-8") == CLEAN_RECORD["problem_statement"]
    poisoned = {**CLEAN_RECORD, "problem_statement": "See FAIL_TO_PASS below."}
    with pytest.raises(LeakageError):
        write_issue_file(poisoned["problem_statement"], dest_dir=tmp_path, record=poisoned)


# --------------------------------------------------------------------------
# arms: setup steps + patch pathspec
# --------------------------------------------------------------------------


def test_setup_control_empty_and_treatment_shape():
    assert SETUP_CONTROL == []
    steps = SETUP_TREATMENT("claude", "/testbed", "/opt/trailmark-src")
    assert len(steps) == 3
    assert "pip install" in steps[0] and "uv tool install" in steps[0]
    assert "trailmark init --root /testbed --budget 2048 --json" in steps[1]
    assert (
        "trailmark focus --issue-file /tmp/issue.txt --cli claude --root /testbed --json"
        in steps[2]
    )
    assert PATCH_PATHSPEC == "-- . ':(exclude).trailmark' ':(exclude)AGENTS.md'"


GIT_AVAILABLE = shutil.which("git") is not None


@pytest.mark.skipif(not GIT_AVAILABLE, reason="git not available")
def test_patch_pathspec_excludes_trailmark_and_agents_md(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "bench@example.com")
    git("config", "user.name", "bench")
    (repo / "app.py").write_text("value = 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "init")
    (repo / "app.py").write_text("value = 42  # the real change\n", encoding="utf-8")
    (repo / ".trailmark").mkdir()
    (repo / ".trailmark" / "cache.sqlite3").write_text("state", encoding="utf-8")
    (repo / "AGENTS.md").write_text("TRAILMARK ATLAS block\n", encoding="utf-8")

    proc = subprocess.run(
        patch_cmd(str(repo)),
        shell=True,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    patch = proc.stdout
    assert "app.py" in patch
    assert "the real change" in patch
    assert ".trailmark" not in patch
    assert "AGENTS.md" not in patch


# --------------------------------------------------------------------------
# report: determinism + markdown
# --------------------------------------------------------------------------


def _record(arm: str, iid: str, seed: int, cost_value: float, turns: int) -> dict:
    return {
        "instance_id": iid,
        "arm": arm,
        "seed": seed,
        "cli": "claude",
        "usage": {
            "model": "claude-sonnet-4-5-20250929",
            "input_tokens": 1000 + 100 * int(iid[-1]),
            "cache_read_tokens": 2000 + 100 * int(iid[-1]),
            "cache_write_tokens": 0,
            "output_tokens": 500,
            "turns": turns,
        },
        "cost_usd": cost_value,
        "turns": turns,
        "patch_bytes": 100,
        "status": "completed",
        "error": None,
    }


@pytest.fixture()
def synthetic_records() -> list[dict]:
    records = []
    for i in range(5):
        iid = f"inst__repo-{i}"
        for arm, cost_value in (("control", 0.10 + 0.01 * i), ("treatment", 0.08 + 0.01 * i)):
            for seed in (0, 1):
                records.append(_record(arm, iid, seed, cost_value, turns=8 + i))
    return records


def test_paired_report_deterministic(synthetic_records):
    first = paired_report(synthetic_records, bootstrap=500, seed=7)
    second = paired_report(synthetic_records, bootstrap=500, seed=7)
    assert first == second  # seeded bootstrap → byte-identical report
    # hand-computed paired delta: treatment is exactly $0.02 cheaper on every instance
    assert first["paired"]["delta_cost_usd"] == pytest.approx(-0.02)
    assert first["paired"]["delta_tokens"] == pytest.approx(0.0)
    assert first["per_arm"]["control"]["mean_tokens"] == pytest.approx(3900.0)
    assert first["per_arm"]["control"]["n_records"] == 10
    lo, hi = first["paired"]["ci95_cost_usd"]
    assert lo <= -0.02 <= hi


def test_render_markdown_table(synthetic_records):
    graded = {
        "control": {f"inst__repo-{i}": i % 2 == 0 for i in range(5)},  # 3/5 resolved
        "treatment": {f"inst__repo-{i}": i < 4 for i in range(5)},  # 4/5 resolved
    }
    report = paired_report(synthetic_records, bootstrap=500, seed=7, graded=graded)
    markdown = render_markdown(report)
    assert "| metric |" in markdown
    assert "$/task" in markdown and "resolve %" in markdown and "cache-read share" in markdown
    assert "60.0%" in markdown and "80.0%" in markdown  # control vs treatment resolve
    assert "+20.0%" in markdown  # paired resolve delta
    assert "bootstrap=500 seed=7" in markdown


def test_load_records_skips_malformed_lines(tmp_path):
    path = tmp_path / "records.jsonl"
    path.write_text('{"a": 1}\nnot json\n\n{"b": 2}\n', encoding="utf-8")
    records = load_records(path)
    assert [r.get("a", r.get("b")) for r in records] == [1, 2]


# --------------------------------------------------------------------------
# run.py: image tags + --dry (no docker)
# --------------------------------------------------------------------------


def test_image_tag_docker_safe():
    tag = image_tag("django__django-11099")
    assert tag == tag.lower()
    assert "/" not in tag and " " not in tag
    assert all(ch.isalnum() or ch in "_-." for ch in tag)
    # resolve_image must return something usable even without a docker daemon (fallback)
    assert "sweb.eval.x86_64." in resolve_image("django__django-11099")


def _instances_file(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "instances.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def _bench_run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "bench.run", *args],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
        env=DockerEnv,
    )


def test_run_dry_with_instances_file_exits_zero(tmp_path):
    rows = [
        {"instance_id": f"acme__widget-{i}", "problem_statement": f"Bug {i}: crash on empty input."}
        for i in range(2)
    ]
    out = tmp_path / "runs" / "demo"
    proc = _bench_run(
        "--instances-file", str(_instances_file(tmp_path, rows)),
        "--slice", "0:10",
        "--cli", "claude",
        "--arm", "control,treatment",
        "--seeds", "1",
        "--parallel", "2",
        "--out", str(out),
        "--dry",
    )
    assert proc.returncode == 0, proc.stderr
    combined = proc.stdout + proc.stderr
    assert "acme__widget-0" in combined and "acme__widget-1" in combined
    assert "selftest claude" in combined and "selftest codex" in combined
    # dry mode must not write any campaign artifacts
    assert not (out / "records.jsonl").exists()
    assert not (out / "state.json").exists()


def test_run_rejects_unknown_arm(tmp_path):
    rows = [{"instance_id": "a__b-1", "problem_statement": "crash on empty input."}]
    proc = _bench_run(
        "--instances-file", str(_instances_file(tmp_path, rows)),
        "--arm", "bogus",
        "--out", str(tmp_path / "out"),
        "--dry",
    )
    assert proc.returncode == 2


def test_run_dry_refuses_leaky_dataset(tmp_path):
    rows = [{"instance_id": "a__b-1", "problem_statement": "See FAIL_TO_PASS below."}]
    proc = _bench_run(
        "--instances-file", str(_instances_file(tmp_path, rows)),
        "--arm", "control",
        "--out", str(tmp_path / "out"),
        "--dry",
    )
    assert proc.returncode == 2
