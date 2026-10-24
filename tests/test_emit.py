"""Emit-layer tests: marked-block writer semantics plus the fmt helpers."""

from __future__ import annotations

from codegraph.emit import agents_md, fmt
from codegraph.model import BLOCK_END, BLOCK_START
from codegraph.util.tokens import byte_len

BODY = "# CODEGRAPH ATLAS — auto-generated\ntitle\nRULES\n- keep MAP/TESTS in mind\n"
BODY2 = "replacement body\nwith two lines"


def test_write_block_creates_file(tmp_path):
    path = tmp_path / "AGENTS.md"
    assert agents_md.write_block(path, BODY) is True
    text = path.read_text(encoding="utf-8")
    assert text == f"{BLOCK_START}\n{BODY.rstrip()}\n{BLOCK_END}\n"
    assert agents_md.read_block(path) == BODY.rstrip()


def test_write_block_idempotent_on_same_body(tmp_path):
    path = tmp_path / "AGENTS.md"
    assert agents_md.write_block(path, BODY) is True
    before = path.read_text(encoding="utf-8")
    assert agents_md.write_block(path, BODY) is False
    assert path.read_text(encoding="utf-8") == before


def test_write_block_replaces_and_preserves_outside_markers(tmp_path):
    path = tmp_path / "AGENTS.md"
    path.write_text(f"Intro\n\n{BLOCK_START}\nold body\n{BLOCK_END}\n\nOutro\n", encoding="utf-8")
    assert agents_md.write_block(path, BODY2) is True
    text = path.read_text(encoding="utf-8")
    assert text.startswith("Intro\n\n")
    assert text.endswith("\n\nOutro\n")
    assert BODY2 in text and "old body" not in text


def test_write_block_prepends_when_no_markers(tmp_path):
    path = tmp_path / "AGENTS.md"
    path.write_text("Existing notes\n", encoding="utf-8")
    agents_md.write_block(path, BODY)
    text = path.read_text(encoding="utf-8")
    assert text.startswith(f"{BLOCK_START}\n")
    assert text.endswith("Existing notes\n")


def test_remove_block_restores_original_text(tmp_path):
    path = tmp_path / "AGENTS.md"
    path.write_text("Intro line\n", encoding="utf-8")
    agents_md.write_block(path, BODY)
    assert agents_md.remove_block(path) is True
    assert path.read_text(encoding="utf-8") == "Intro line\n"


def test_remove_block_unlinks_block_only_file(tmp_path):
    path = tmp_path / "AGENTS.md"
    agents_md.write_block(path, BODY)
    assert agents_md.remove_block(path) is True
    assert not path.exists()
    assert agents_md.remove_block(path) is False  # already gone


def test_remove_block_noop_without_markers(tmp_path):
    path = tmp_path / "AGENTS.md"
    path.write_text("plain notes\n", encoding="utf-8")
    assert agents_md.remove_block(path) is False
    assert path.read_text(encoding="utf-8") == "plain notes\n"
    assert agents_md.read_block(tmp_path / "missing.md") is None


def test_render_roundtrip():
    block = agents_md.render("x\ny")
    assert block.startswith(BLOCK_START) and block.endswith(BLOCK_END)
    assert "x\ny" in block


def test_truncate_byte_safe_and_deterministic():
    assert fmt.truncate("hello", 10) == "hello"
    assert fmt.truncate("hello world", 8) == "hello…"
    assert byte_len(fmt.truncate("hello world", 8)) <= 8
    out = fmt.truncate("héllo wörld ünïcode", 6)
    assert byte_len(out) <= 6 and out.endswith("…")
    assert fmt.truncate("abc", 2) == "ab"  # below ellipsis size: hard cut
    assert fmt.truncate("abc", 0) == ""
    assert fmt.truncate("", 5) == ""
    assert fmt.truncate("hello", 5) == fmt.truncate("hello", 5)


def test_wrap_deterministic_shape():
    text = "one two three four five six seven"
    out = fmt.wrap(text, width=12)
    assert out == fmt.wrap(text, width=12)
    lines = out.split("\n")
    assert lines[0].startswith("- ")
    assert all(len(line) <= 13 for line in lines)
    assert out.replace("- ", "", 1).split() == text.split()  # no word lost or split
    assert fmt.wrap("", 10) == ""
    single = fmt.wrap("supercalifragilistic", width=4)
    assert single == "- supercalifragilistic"  # long words are never split
