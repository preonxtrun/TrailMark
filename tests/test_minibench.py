"""Mini-bench tests: trails auto-deposit, --verify gating, and the verified-solve% column.

All runnable WITHOUT docker or the opencode CLI: the end-to-end tests monkeypatch the
three side-effecting steps (``arm_setup`` / ``run_agent`` / ``patch_extract``) and drive
``bench.minibench.main`` against a tmp git repo with a fake opencode stream (real usage
shape, parsed by bench.cost) and a fake patch. The verify fixture repo carries a
Makefile ``test:`` target — ``detect_runner``'s Makefile branch — whose recipe is an
``echo '3 passed'``-style runner, so the bounded verification is exercised for real.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # bench/ is a repo-root package, not an installed one
    sys.path.insert(0, str(REPO_ROOT))

from bench import minibench
from bench.report import _verified_solved, paired_report, render_markdown

GIT_AVAILABLE = shutil.which("git") is not None

FAKE_PATCH = (
    "diff --git a/src/app.py b/src/app.py\n"
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -1 +1 @@\n"
    "-value = 1\n"
    "+value = 42\n"
)


def _stream_line(tokens: dict) -> str:
    """One opencode ``step_finish`` event — the exact shape bench.cost.parse_opencode reads."""
    return json.dumps(
        {
            "type": "step_finish",
            "sessionID": "ses_test",
            "part": {"type": "step-finish", "reason": "stop", "cost": 0, "tokens": tokens},
        }
    )


FAKE_STREAM = (
    json.dumps({"type": "step_start", "sessionID": "ses_test", "part": {"type": "step-start"}})
    + "\n"
    + _stream_line({"input": 120, "output": 30, "reasoning": 5, "cache": {"read": 40, "write": 0}})
    + "\n"
)

OK_VERIFY = {"ran": True, "passed": 3, "failed": 0, "exit": 0}
BAD_VERIFY = {"ran": True, "passed": 0, "failed": 2, "exit": 1}


# --------------------------------------------------------------------------
# fixtures + helpers
# --------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    """Tmp git repo with one commit; a Makefile gives it an `echo`-style test runner."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "bench@example.com", cwd=repo)
    _git("config", "user.name", "bench", cwd=repo)
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("value = 1\n", encoding="utf-8")
    (repo / "Makefile").write_text("test:\n\techo '3 passed'\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    return repo


def _run_bench(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    arm: str,
    extra: tuple[str, ...] = (),
    patch: str = FAKE_PATCH,
) -> list[dict]:
    """Drive minibench.main with the side-effecting steps faked; return parsed records."""
    out = tmp_path / "out"
    tasks = tmp_path / "tasks.jsonl"
    tasks.write_text(
        json.dumps({"id": "t1", "statement": "flip the value constant"}) + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(minibench, "arm_setup", lambda *a, **k: None)
    monkeypatch.setattr(minibench, "run_agent", lambda *a, **k: (FAKE_STREAM, 0))
    monkeypatch.setattr(minibench, "patch_extract", lambda _repo: patch)
    rc = minibench.main(
        [
            "--repo", str(repo),
            "--tasks", str(tasks),
            "--arm", arm,
            "--seeds", "1",
            "--out", str(out),
            *extra,
        ]
    )
    assert rc == 0
    return [
        json.loads(line)
        for line in (out / "records.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _worktree(tmp_path: Path) -> Path:
    return tmp_path / "out" / "worktrees" / "wt0"


def _trails_entries(repo: Path) -> list[dict]:
    path = repo / ".trailmark" / "trails.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# --------------------------------------------------------------------------
# trails auto-deposit
# --------------------------------------------------------------------------


def test_trail_files_from_patch():
    patch = (
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "+++ b/src/deep/new.py\t2026-01-01 00:00:00.000000000 +0000\n"
        "+++ /dev/null\n"  # deletions carry no surviving path
        "+++ b/a.py\n"  # duplicate collapses
    )
    assert minibench.trail_files_from_patch(patch) == ["a.py", "src/deep/new.py"]
    assert minibench.trail_files_from_patch("") == []


@pytest.mark.parametrize(
    ("status", "patch_bytes", "verify", "expected"),
    [
        ("completed", 100, None, "solved"),
        ("completed", 100, {"ran": True, "passed": 3, "failed": 0, "exit": 0}, "solved"),
        ("completed", 100, {"ran": True, "passed": 0, "failed": 2, "exit": 1}, "failed"),
        ("completed", 0, None, "failed"),  # empty patch = nothing landed = dead end
        ("cli_error", 100, None, "failed"),
        ("timeout", 100, None, "failed"),
    ],
)
def test_trail_outcome_heuristic(status, patch_bytes, verify, expected):
    assert minibench.trail_outcome(status, patch_bytes, verify) == expected


def test_deposit_trail_writes_and_updates_store(tmp_path):
    repo = tmp_path / "store-repo"
    repo.mkdir()  # the trail store API only needs a filesystem root
    assert minibench.deposit_trail(repo, "t1", FAKE_PATCH, "completed", 42, None) is True
    # a rerun of the same task id with a worse result → latest wins, one line per id
    assert minibench.deposit_trail(repo, "t1", "", "completed", 0, None) is True
    assert _trails_entries(repo) == [{"id": "t1", "outcome": "failed", "files": []}]
    assert minibench.deposit_trail(repo, "t2", FAKE_PATCH, "cli_error", 10, None) is True
    entries = _trails_entries(repo)
    assert [entry["id"] for entry in entries] == ["t1", "t2"]
    assert entries[1]["outcome"] == "failed"


@pytest.mark.skipif(not GIT_AVAILABLE, reason="git not available")
def test_treatment_run_deposits_trail(git_repo, tmp_path, monkeypatch):
    records = _run_bench(git_repo, tmp_path, monkeypatch, arm="treatment")
    assert len(records) == 1
    record = records[0]
    assert record["status"] == "completed"
    assert record["trail_recorded"] is True
    assert record["verify"] is None  # --verify off → no verification data at all
    assert record["usage"]["input_tokens"] == 120  # fake stream parsed by bench.cost
    wt = _worktree(tmp_path)
    assert _trails_entries(wt) == [{"id": "t1", "outcome": "solved", "files": ["src/app.py"]}]
    # the memory loop is closed: the next treatment focus in this worktree carries TRAILS
    from trailmark.trails import section_items

    assert section_items(str(wt)) == ["- src/app.py — well-trodden (1 solved tasks)"]


@pytest.mark.skipif(not GIT_AVAILABLE, reason="git not available")
def test_control_run_never_deposits(git_repo, tmp_path, monkeypatch):
    records = _run_bench(git_repo, tmp_path, monkeypatch, arm="control")
    record = records[0]
    assert record["status"] == "completed"
    assert record["trail_recorded"] is False
    assert _trails_entries(_worktree(tmp_path)) == []


@pytest.mark.skipif(not GIT_AVAILABLE, reason="git not available")
def test_no_trails_flag_disables_deposit(git_repo, tmp_path, monkeypatch):
    records = _run_bench(
        git_repo, tmp_path, monkeypatch, arm="treatment", extra=("--no-trails",)
    )
    assert records[0]["trail_recorded"] is False
    assert _trails_entries(_worktree(tmp_path)) == []


# --------------------------------------------------------------------------
# --verify: bounded benchmark-side verification
# --------------------------------------------------------------------------


def test_parse_verify_counts_last_summary_wins():
    assert minibench.parse_verify_counts("2 failed, 3 passed in 0.10s\n") == (3, 2)
    assert minibench.parse_verify_counts("all good\n3 passed in 0.01s\n") == (3, 0)
    assert minibench.parse_verify_counts("1 passed\n5 passed\n") == (5, 0)
    assert minibench.parse_verify_counts("1 failed") == (0, 1)
    assert minibench.parse_verify_counts("no tests ran") == (0, 0)
    assert minibench.parse_verify_counts("") == (0, 0)


def test_verify_run_makefile_runner(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Makefile").write_text(
        "test:\n\techo '2 failed, 1 passed'; exit 1\n", encoding="utf-8"
    )
    log = tmp_path / "verify" / "x.treatment.s0.log"
    result = minibench.verify_run(repo, log)
    assert result == {"ran": True, "passed": 1, "failed": 2, "exit": 1}
    assert "2 failed" in log.read_text(encoding="utf-8")


def test_verify_run_no_detectable_runner(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()  # no pytest.ini / Makefile / package.json / Cargo.toml / go.mod
    result = minibench.verify_run(repo, tmp_path / "verify" / "x.log")
    assert result["ran"] is False
    assert not (tmp_path / "verify" / "x.log").exists()  # nothing ran → nothing logged


@pytest.mark.skipif(not GIT_AVAILABLE, reason="git not available")
def test_verify_runs_bounded_runner_and_parses_counts(git_repo, tmp_path, monkeypatch):
    records = _run_bench(git_repo, tmp_path, monkeypatch, arm="treatment", extra=("--verify",))
    record = records[0]
    assert record["verify"] == {"ran": True, "passed": 3, "failed": 0, "exit": 0}
    assert record["trail_recorded"] is True  # no failed tests → heuristic outcome "solved"
    log = tmp_path / "out" / "verify" / "t1.treatment.s0.log"
    assert "3 passed" in log.read_text(encoding="utf-8")
    # verification output must never leak into the agent stream
    stream = (tmp_path / "out" / "streams" / "t1.treatment.s0.jsonl").read_text(encoding="utf-8")
    assert "3 passed" not in stream


@pytest.mark.skipif(not GIT_AVAILABLE, reason="git not available")
def test_verify_applies_to_control_but_never_deposits(git_repo, tmp_path, monkeypatch):
    records = _run_bench(git_repo, tmp_path, monkeypatch, arm="control", extra=("--verify",))
    record = records[0]
    assert record["verify"] == {"ran": True, "passed": 3, "failed": 0, "exit": 0}
    assert record["trail_recorded"] is False
    assert _trails_entries(_worktree(tmp_path)) == []


@pytest.mark.skipif(not GIT_AVAILABLE, reason="git not available")
def test_verify_skipped_on_empty_patch_but_still_recorded(git_repo, tmp_path, monkeypatch):
    records = _run_bench(
        git_repo, tmp_path, monkeypatch, arm="treatment", extra=("--verify",), patch=""
    )
    record = records[0]
    assert record["patch_bytes"] == 0
    assert record["verify"] == {"ran": False, "passed": 0, "failed": 0, "exit": None}
    # a completed run with an empty patch is a dead end: the failed trail still lands
    assert record["trail_recorded"] is True
    assert _trails_entries(_worktree(tmp_path)) == [{"id": "t1", "outcome": "failed", "files": []}]


# --------------------------------------------------------------------------
# report: verified-solve% column
# --------------------------------------------------------------------------


def _verify_record(arm: str, iid: str, verify: dict | None) -> dict:
    return {
        "instance_id": iid,
        "arm": arm,
        "seed": 0,
        "usage": {
            "input_tokens": 100,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "output_tokens": 10,
            "turns": 1,
        },
        "turns": 1,
        "patch_bytes": 100,
        "status": "completed",
        "verify": verify,
    }


def test_verified_solve_rate_and_delta():
    records = [
        _verify_record("control", "a", OK_VERIFY),
        _verify_record("control", "b", BAD_VERIFY),
        _verify_record("treatment", "a", OK_VERIFY),
        _verify_record("treatment", "b", OK_VERIFY),
    ]
    report = paired_report(records, bootstrap=0, seed=0)
    assert report["per_arm"]["control"]["verified_solve_rate"] == pytest.approx(0.5)
    assert report["per_arm"]["control"]["n_verified_solved"] == 1
    assert report["per_arm"]["treatment"]["verified_solve_rate"] == pytest.approx(1.0)
    # paired per-instance: a: 1.0-1.0=0, b: 1.0-0.0=1.0 → mean +0.5
    assert report["paired"]["delta_verified_solve_rate"] == pytest.approx(0.5)


def test_verified_solve_markdown_deterministic():
    records = [
        _verify_record("control", "a", OK_VERIFY),
        _verify_record("control", "b", BAD_VERIFY),
        _verify_record("treatment", "a", OK_VERIFY),
        _verify_record("treatment", "b", OK_VERIFY),
    ]
    first = render_markdown(paired_report(records, bootstrap=0, seed=0))
    second = render_markdown(paired_report(records, bootstrap=0, seed=0))
    assert first == second  # deterministic rendering
    assert "verified-solve %" in first
    assert "50.0%" in first and "100.0%" in first and "+50.0%" in first


def test_verified_solve_definition_edges():
    # exit==0 with zero passed still counts (a green runner without counters)
    assert _verified_solved({"verify": {"ran": True, "passed": 0, "failed": 0, "exit": 0}})
    assert not _verified_solved({"verify": {"ran": True, "passed": 4, "failed": 1, "exit": 0}})
    assert not _verified_solved({"verify": {"ran": True, "passed": 0, "failed": 0, "exit": 1}})
    assert not _verified_solved({"verify": {"ran": False, "passed": 0, "failed": 0, "exit": None}})
    assert not _verified_solved({"verify": None})
    assert not _verified_solved({})  # legacy records without verify data


def test_no_verify_data_shows_dash():
    records = [
        _verify_record("control", "a", None),
        _verify_record("treatment", "a", None),
    ]
    report = paired_report(records, bootstrap=0, seed=0)
    assert report["per_arm"]["control"]["verified_solve_rate"] is None
    assert report["per_arm"]["treatment"]["verified_solve_rate"] is None
    assert report["paired"]["delta_verified_solve_rate"] is None
    markdown = render_markdown(report)
    assert "verified-solve %" in markdown  # column exists, rendered as "—"
