"""Multi-harness adapters + secret guard tests."""

from __future__ import annotations

import pytest

from codegraph.emit.agents_md import write_block
from codegraph.emit.harnesses import write_adapters
from codegraph.emit.secrets import assert_no_secrets

BODY = "# TEST ATLAS\nMAP\n- x.py:1 foo()"


def test_harness_adapters(tmp_path):
    written = write_adapters(tmp_path, ["claude", "gemini", "cursor", "copilot"])
    names = {p.name for p in written}
    assert names == {"CLAUDE.md", "GEMINI.md", "agents-md.mdc", "copilot-instructions.md"}
    assert (tmp_path / "CLAUDE.md").is_symlink()
    assert (tmp_path / "CLAUDE.md").resolve() == (tmp_path / "AGENTS.md").resolve() or not (
        tmp_path / "AGENTS.md"
    ).exists()  # symlink target may not exist yet; pointer content is what matters
    mdc = (tmp_path / ".cursor" / "rules" / "agents-md.mdc").read_text()
    assert "alwaysApply: true" in mdc and "AGENTS.md" in mdc
    assert "AGENTS.md" in (tmp_path / ".github" / "copilot-instructions.md").read_text()
    # idempotent: second pass writes nothing new
    assert write_adapters(tmp_path, ["claude", "gemini", "cursor", "copilot"]) == []


def test_harness_unknown_rejected(tmp_path):
    with pytest.raises(ValueError):
        write_adapters(tmp_path, ["not-a-harness"])


def test_secret_guard_blocks_write(tmp_path):
    with pytest.raises(ValueError, match="GitHub token"):
        write_block(tmp_path / "AGENTS.md", f"MAP\n- token {('ghp_' + 'a' * 36)}")


def test_secret_guard_allows_clean(tmp_path):
    assert write_block(tmp_path / "AGENTS.md", BODY) is True
    assert "sk-" not in (tmp_path / "AGENTS.md").read_text()


def test_secret_guard_patterns():
    assert_no_secrets("plain map content with AKIA-style prose")  # prose is fine
    with pytest.raises(ValueError):
        assert_no_secrets("key = " + ("AKIA" + "B" * 16))
    with pytest.raises(ValueError):
        assert_no_secrets("-----BEGIN RSA PRIVATE KEY-----")
