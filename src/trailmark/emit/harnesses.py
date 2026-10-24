"""Multi-harness adapters: point other agents' config files at the managed block.

The layer's only real content surface is AGENTS.md. Some harnesses don't read it
natively (Cursor rules, Copilot instructions) or expect their own filename
(GEMINI.md, CLAUDE.md on older versions). These adapters write tiny **pointer**
files — the block content is never duplicated, so staleness is impossible by
construction and the byte budget is paid once.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..model import BLOCK_START

POINTER_BODY = (
    "This repository carries an agent context layer in AGENTS.md (managed block\n"
    f"`{BLOCK_START}`).\n"
    "Read AGENTS.md first: it contains the repository map, test index, flight\n"
    "rules, and task leads. Re-read it if it changed since you started.\n"
)

CURSOR_FRONTMATTER = (
    "---\n"
    "description: Repository agent context (pointer to AGENTS.md)\n"
    "alwaysApply: true\n"
    "---\n"
)


def write_adapters(root: Path, harnesses: list[str]) -> list[Path]:
    """Write pointer/symlink adapters for the requested harnesses. Idempotent."""
    root = Path(root)
    written: list[Path] = []

    def pointer(rel: Path, frontmatter: str = "") -> None:
        if rel.exists():
            return
        rel.parent.mkdir(parents=True, exist_ok=True)
        rel.write_text(frontmatter + POINTER_BODY, encoding="utf-8")
        written.append(rel)

    for harness in harnesses:
        name = harness.strip().lower()
        if name == "claude":
            target = root / "CLAUDE.md"
            # lexists (not exists): a dangling symlink still means "already set up"
            if not (target.exists() or target.is_symlink()):
                os.symlink("AGENTS.md", target)
                written.append(target)
        elif name == "gemini":
            pointer(root / "GEMINI.md")
        elif name == "cursor":
            pointer(root / ".cursor" / "rules" / "agents-md.mdc", CURSOR_FRONTMATTER)
        elif name == "copilot":
            pointer(root / ".github" / "copilot-instructions.md")
        else:
            raise ValueError(f"unknown harness {harness!r} (claude/gemini/cursor/copilot)")
    return written
