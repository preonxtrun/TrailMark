"""Trails tests: .trailmark/trails.jsonl pheromone memory + the TRAILS block section.

Covers the store (record/summarize/clear), its determinism and evaporation, the
dead-end (negative-signal) rule, and the additive atlas integration: with no
usable trails file, ``assemble`` output must be byte-identical to a trails-less
build — proven here by before/after snapshots on a repo that never had trails.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from trailmark.atlas import assemble, glossary
from trailmark.trails import (
    DEAD_END_HEADER,
    MAX_ENTRIES,
    clear,
    main,
    record,
    section_items,
    summarize,
)
from trailmark.util.tokens import byte_len

_TAGS = {"RULES", "MAP", "TESTS", "PLAN", "GLOSSARY", "LAB", "TRAILS"}


def section_rows(body: str, tag: str) -> list[str]:
    """Lines belonging to *tag* (until the next known tag), excluding the tag line."""
    out: list[str] = []
    grabbing = False
    for line in body.split("\n"):
        if line in _TAGS:
            grabbing = line == tag
            continue
        if grabbing and line:
            out.append(line)
    return out


def make_repo(tmp_path: Path) -> Path:
    """Tiny two-dir repo (mirrors tests/test_profile.py) with a real index."""
    (tmp_path / "auth").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "auth" / "session.py").write_text(
        'class Session:\n    """Token session."""\n    def refresh(self, ttl=3600):\n        return ttl\n',
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_session.py").write_text(
        "from auth.session import Session\ndef test_refresh():\n    assert Session().refresh(1) == 1\n",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    return tmp_path


def make_data(repo: Path):
    """IndexData for *repo* via the real indexer (repo files exist on disk)."""
    from trailmark.cache import IndexCache
    from trailmark.index import update

    return update(root=str(repo), langs=None, workers=1, cache=IndexCache(repo / ".trailmark"))


def trails_path(repo: Path) -> Path:
    return repo / ".trailmark" / "trails.jsonl"


# -- store: record / summarize ---------------------------------------------------


def test_record_writes_one_line_per_task_id(tmp_path):
    repo = str(tmp_path)
    record(repo, "T1", ["b.py", "a.py"], "solved")
    lines = trails_path(tmp_path).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == {
        "files": ["a.py", "b.py"],  # deduped + sorted for byte-determinism
        "id": "T1",
        "outcome": "solved",
    }


def test_record_is_byte_deterministic(tmp_path):
    repo = str(tmp_path)
    record(repo, "T1", ["b.py", "a.py"], "solved")
    raw1 = trails_path(tmp_path).read_bytes()
    record(repo, "T1", ["a.py", "b.py"], "solved")  # same trail, different input order
    raw2 = trails_path(tmp_path).read_bytes()
    assert raw1 == raw2


def test_summarize_recency_weighting(tmp_path):
    repo = str(tmp_path)
    record(repo, "t1", ["a.py", "b.py"], "solved")
    record(repo, "t2", ["a.py"], "solved")
    assert summarize(repo)[0] == ["a.py", "b.py"]  # a.py: 1/3 + 1/2 > b.py: 1/3
    record(repo, "t3", ["b.py"], "solved")  # the newer solve tips the balance
    assert summarize(repo)[0] == ["b.py", "a.py"]  # b.py: 1/4 + 1/2 > a.py: 1/4 + 1/3


def test_summarize_tie_break_and_caps(tmp_path):
    repo = str(tmp_path)
    record(repo, "t1", ["z.py", "a.py"], "solved")
    assert summarize(repo)[0] == ["a.py", "z.py"]  # equal weight → path order
    for i in range(8):  # 8 more solved files, oldest first
        record(repo, f"s{i}", [f"hot{i}.py"], "solved")
    hot, _dead = summarize(repo)
    assert hot == [f"hot{i}.py" for i in range(7, 1, -1)]  # top 6, newest first


def test_record_updates_by_task_id(tmp_path):
    repo = str(tmp_path)
    record(repo, "T1", ["x.py"], "solved")
    record(repo, "T1", ["y.py"], "solved")  # latest wins, moves to newest position
    assert len(trails_path(tmp_path).read_text(encoding="utf-8").splitlines()) == 1
    assert summarize(repo) == (["y.py"], [])
    # latest outcome wins: re-recording a task as failed flips its files to repellents
    record(repo, "T2", ["p.py"], "solved")
    record(repo, "T2", ["p.py"], "failed")
    hot, dead = summarize(repo)
    assert hot == ["y.py"] and dead == ["p.py"]


def test_truncation_keeps_last_32(tmp_path):
    repo = str(tmp_path)
    for i in range(34):
        record(repo, f"t{i}", [f"f{i}.py"], "solved")
    assert len(trails_path(tmp_path).read_text(encoding="utf-8").splitlines()) == MAX_ENTRIES
    hot, _dead = summarize(repo)
    assert hot == [f"f{i}.py" for i in range(33, 27, -1)]  # t0/t1 evaporated
    assert "f0.py" not in hot and "f1.py" not in hot
    record(repo, "t17", ["f17.py"], "solved")  # update re-positions t17 as newest
    assert len(trails_path(tmp_path).read_text(encoding="utf-8").splitlines()) == MAX_ENTRIES
    assert summarize(repo)[0] == ["f17.py", "f33.py", "f32.py", "f31.py", "f30.py", "f29.py"]


def test_dead_ends_failed_only_most_recent_first(tmp_path):
    repo = str(tmp_path)
    record(repo, "t1", ["shared.py"], "solved")
    record(repo, "t2", ["dead1.py"], "failed")
    record(repo, "t3", ["shared.py", "dead2.py"], "failed")
    hot, dead = summarize(repo)
    assert "shared.py" in hot  # failed AND solved → not a dead end
    assert dead == ["dead2.py", "dead1.py"]  # most-recent-first
    for i in range(5):  # capped at 4
        record(repo, f"d{i}", [f"old{i}.py"], "failed")
    assert summarize(repo)[1] == [f"old{i}.py" for i in range(4, 0, -1)]


def test_dead_end_tie_break_by_path(tmp_path):
    repo = str(tmp_path)
    record(repo, "t1", ["z.py", "a.py"], "failed")
    assert summarize(repo) == ([], ["a.py", "z.py"])


def test_empty_store_reads_as_empty(tmp_path):
    repo = str(tmp_path)
    assert summarize(repo) == ([], [])
    assert section_items(repo) == []
    (tmp_path / ".trailmark").mkdir()
    trails_path(tmp_path).write_text("", encoding="utf-8")
    assert summarize(repo) == ([], [])
    assert section_items(repo) == []


def test_malformed_lines_are_skipped_and_healed(tmp_path):
    cg = tmp_path / ".trailmark"
    cg.mkdir()
    trails_path(tmp_path).write_text(
        "not json\n"
        '["not", "a", "dict"]\n'
        '{"id": "T1", "outcome": "solved", "files": ["a.py"]}\n'
        '{"id": "T2", "outcome": "meh", "files": ["b.py"]}\n'  # unknown outcome
        '{"id": "T3", "outcome": "failed"}\n'  # missing files
        "\n",
        encoding="utf-8",
    )
    assert summarize(str(cg.parent)) == (["a.py"], [])
    record(str(cg.parent), "T9", ["c.py"], "solved")  # rewrite heals damaged lines
    lines = trails_path(tmp_path).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2  # T1 + T9; malformed lines dropped


def test_record_rejects_bad_input(tmp_path):
    repo = str(tmp_path)
    with pytest.raises(ValueError, match="outcome"):
        record(repo, "T1", ["a.py"], "partial")
    with pytest.raises(ValueError, match="task id"):
        record(repo, "   ", ["a.py"], "solved")
    assert summarize(repo) == ([], [])  # nothing was written


def test_clear_removes_store(tmp_path):
    repo = str(tmp_path)
    assert clear(repo) is False  # nothing to remove
    record(repo, "T1", ["a.py"], "solved")
    assert clear(repo) is True
    assert not trails_path(tmp_path).exists()
    assert summarize(repo) == ([], [])


# -- section item format -----------------------------------------------------------


def test_section_items_format(tmp_path):
    repo = str(tmp_path)
    assert section_items(repo) == []  # no store → no items (byte-identity contract)
    record(repo, "t1", ["a.py"], "solved")
    record(repo, "t2", ["d.py"], "failed")
    assert section_items(repo) == [
        "- a.py — well-trodden (1 solved tasks)",
        DEAD_END_HEADER,
        "- d.py — dead end (failed tasks)",
    ]


# -- atlas integration -------------------------------------------------------------


def test_assemble_byte_identical_without_trails(tmp_path):
    """The campaign invariant: an empty trails setup must change nothing."""
    repo = make_repo(tmp_path)
    data = make_data(repo)
    snapshot = {b: assemble(data, None, "generic", b) for b in (512, 2048, 4096)}
    (repo / ".trailmark").mkdir(exist_ok=True)
    trails_path(repo).write_text("", encoding="utf-8")  # empty trails file
    for b, snap in snapshot.items():
        assert assemble(data, None, "generic", b) == snap
    trails_path(repo).write_text("\n   \n{oops\n", encoding="utf-8")  # no usable entries
    for b, snap in snapshot.items():
        assert assemble(data, None, "generic", b) == snap


def test_assemble_without_trails_has_no_trails_tag(tmp_path):
    repo = make_repo(tmp_path)
    data = make_data(repo)
    assert "TRAILS" not in assemble(data, None, "generic", 4096)
    assert "DEAD ENDS" not in assemble(data, None, "generic", 4096)


def test_assemble_with_trails_adds_section(tmp_path):
    repo = make_repo(tmp_path)
    data = make_data(repo)
    before = assemble(data, None, "generic", 4096)
    record(str(repo), "t1", ["auth/session.py"], "solved")
    after = assemble(data, None, "generic", 4096)
    assert after != before and "TRAILS" in after


def test_assemble_trails_content_order_and_budget(tmp_path):
    repo = make_repo(tmp_path)
    data = make_data(repo)
    record(str(repo), "t1", ["auth/session.py"], "solved")
    record(str(repo), "t2", ["auth/session.py", "tests/test_session.py"], "solved")
    record(str(repo), "t3", ["src/legacy.py"], "failed")
    body = assemble(data, None, "generic", 4096)
    assert "- auth/session.py — well-trodden (2 solved tasks)" in body
    assert "- tests/test_session.py — well-trodden (1 solved tasks)" in body
    assert "DEAD ENDS (avoid blind retries):" in body
    assert "- src/legacy.py — dead end (failed tasks)" in body
    assert body.index("TRAILS") < body.index("GLOSSARY")  # emitted after LAB/PLAN
    assert section_rows(body, "TRAILS") == section_items(str(repo))
    assert byte_len(body) <= 4096
    assert assemble(data, None, "generic", 4096) == body  # deterministic
    with_issue = assemble(data, "session refresh", "generic", 4096)
    assert "PLAN" in with_issue and "TRAILS" in with_issue  # coexists with leads


def test_trails_dropped_at_tiny_budget(tmp_path):
    repo = make_repo(tmp_path)
    data = make_data(repo)
    record(str(repo), "t1", ["auth/session.py"], "solved")
    full = assemble(data, None, "generic", 100_000)
    tiny = assemble(data, None, "generic", 512)
    assert byte_len(tiny) <= 512
    assert "TRAILS" not in tiny  # rank 7 dies before any MAP/TESTS floors
    assert section_rows(tiny, "MAP") == section_rows(full, "MAP")  # MAP floor intact
    assert "runner:" in tiny  # TESTS core row survives


def test_trails_drop_rank_between_glossary_and_plan(tmp_path):
    repo = make_repo(tmp_path)
    data = make_data(repo)
    record(str(repo), "t1", ["auth/session.py"], "solved")
    full = assemble(data, None, "generic", 100_000)
    assert "TRAILS" in full and "GLOSSARY" in full
    items = section_items(str(repo))
    glossary_bytes = (
        sum(byte_len(i) + 1 for i in glossary.build_glossary(data)) + len("GLOSSARY") + 1
    )
    trails_bytes = sum(byte_len(i) + 1 for i in items) + len("TRAILS") + 1
    # GLOSSARY (rank 0) drops before TRAILS (rank 7): at full minus glossary
    # bytes the glossary is gone while TRAILS survives.
    b1 = byte_len(full) - glossary_bytes
    body1 = assemble(data, None, "generic", b1)
    assert byte_len(body1) <= b1
    assert "GLOSSARY" not in body1 and "TRAILS" in body1
    # One TRAILS-section lower: the whole TRAILS section drops, MAP survives.
    b2 = b1 - trails_bytes
    body2 = assemble(data, None, "generic", b2)
    assert byte_len(body2) <= b2
    assert "TRAILS" not in body2 and "MAP" in body2


# -- CLI entry (python -m trailmark.trails) ------------------------------------------


def test_cli_record_and_clear_json(tmp_path, capsys):
    repo = str(tmp_path)
    assert main(["record", "--root", repo, "--id", "T1", "--files", "a.py,b.py",
                 "--outcome", "solved", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "recorded": True,
        "id": "T1",
        "outcome": "solved",
        "files": ["a.py", "b.py"],
        "entries": 1,
    }
    assert summarize(repo) == (["a.py", "b.py"], [])
    assert main(["clear", "--root", repo, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"cleared": True, "removed": True}
    assert not trails_path(tmp_path).exists()


def test_cli_human_output(tmp_path, capsys):
    repo = str(tmp_path)
    assert main(["record", "--root", repo, "--id", "T1", "--files", "a.py",
                 "--outcome", "failed"]) == 0
    out = capsys.readouterr().out
    assert "recorded T1" in out and "failed" in out
    assert main(["clear", "--root", repo]) == 0
    assert "cleared trails" in capsys.readouterr().out


def test_cli_rejects_bad_outcome(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["record", "--root", str(tmp_path), "--id", "T", "--files", "a.py",
              "--outcome", "meh"])
    assert exc.value.code == 2
    assert capsys.readouterr().err  # argparse usage/error message


def test_python_m_trails_entrypoint(tmp_path):
    """The exact documented commands, run as a real subprocess."""
    repo = str(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-m", "trailmark.trails", "record", "--root", repo,
         "--id", "T1", "--files", "a.py,b.py", "--outcome", "solved", "--json"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["recorded"] is True and payload["files"] == ["a.py", "b.py"]
    assert summarize(repo) == (["a.py", "b.py"], [])
    proc = subprocess.run(
        [sys.executable, "-m", "trailmark.trails", "clear", "--root", repo, "--json"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"cleared": True, "removed": True}
    assert not trails_path(tmp_path).exists()
