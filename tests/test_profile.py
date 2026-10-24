"""Profile tests: .trailmark/profile.txt → LAB facts + user rules in the block."""

from __future__ import annotations

from pathlib import Path

from trailmark.atlas import assemble, load_profile
from trailmark.util.tokens import byte_len


def make_repo(tmp_path: Path) -> Path:
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


def test_load_profile_parses_facts_and_rules(tmp_path: Path):
    (tmp_path / ".trailmark").mkdir()
    (tmp_path / ".trailmark" / "profile.txt").write_text(
        "# comment\n"
        "fact: partitions gpu, cpu\n"
        "rule: check sacct once per job\n"
        "garbage line without prefix\n"
        "\n",
        encoding="utf-8",
    )
    facts, extra = load_profile(str(tmp_path))
    assert facts == ["partitions gpu, cpu"]
    assert extra == ["check sacct once per job"]


def test_load_profile_absent(tmp_path: Path):
    assert load_profile(str(tmp_path)) == ([], [])


def test_assemble_includes_lab_and_user_rules(tmp_path: Path):
    repo = make_repo(tmp_path)
    (repo / ".trailmark").mkdir(exist_ok=True)
    (repo / ".trailmark" / "profile.txt").write_text(
        "fact: partitions gpu, cpu\n"
        "rule: never poll squeue in a loop\n",
        encoding="utf-8",
    )
    from trailmark.cache import IndexCache
    from trailmark.index import update

    data = update(root=str(repo), langs=None, workers=1, cache=IndexCache(repo / ".trailmark"))
    body = assemble(data, None, "generic", 4096)
    assert "LAB" in body and "partitions gpu, cpu" in body
    assert "never poll squeue in a loop" in body
    assert byte_len(body) <= 4096


def test_assemble_without_profile_has_no_lab(tmp_path: Path):
    repo = make_repo(tmp_path)
    from trailmark.cache import IndexCache
    from trailmark.index import update

    data = update(root=str(repo), langs=None, workers=1, cache=IndexCache(repo / ".trailmark"))
    body = assemble(data, None, "generic", 4096)
    assert "LAB" not in body
