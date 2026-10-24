"""Cheap deterministic token estimation (budgets are enforced in bytes; this is advisory)."""

from __future__ import annotations


def estimate_tokens(text: str) -> int:
    """Estimate LLM tokens for *text*. ~4 chars/token for mixed code+prose."""
    return max(1, (len(text) + 3) // 4)


def byte_len(text: str) -> int:
    """UTF-8 byte length (the enforced budget unit)."""
    return len(text.encode("utf-8"))
