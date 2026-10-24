"""Hygiene guard: refuse to write content that smells like a live secret.

Profiles are user-authored and blocks reference repo content — a pasted API key
must never reach an agent's context (or a benchmark patch). Cheap denylist of
high-confidence patterns; deliberately narrow to avoid false positives on code.
"""

from __future__ import annotations

import re

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("AWS access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("GitHub token", re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}")),
    ("OpenAI-style key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("Slack token", re.compile(r"xox[bpars]-[A-Za-z0-9-]{10,}")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")),
)


def assert_no_secrets(text: str) -> None:
    """Raise ValueError if *text* matches a high-confidence secret pattern."""
    for label, pattern in _SECRET_PATTERNS:
        match = pattern.search(text)
        if match:
            raise ValueError(
                f"refusing to write: possible {label} in block content "
                f"(matched {match.group(0)[:8]}…) — remove it from the source/profile"
            )
