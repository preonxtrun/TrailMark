"""Flight rules: fixed hand-written bullet lists with per-CLI variants (SPEC-frozen text).

Three variants exist — ``generic`` / ``claude`` / ``shell``. The wrapper is
deterministic, so the same variant always renders byte-identically (~700 bytes
wrapped for the claude/shell variants, tighter for generic).

A one-line :data:`COMPACT_RULES` fallback per variant exists for tight budgets:
below roughly 900 bytes the full wrapped text cannot coexist with the guaranteed
MAP floor (invariant #2), so the packer swaps to the compact line while keeping
the RULES section present.
"""

from __future__ import annotations

from ..emit.fmt import wrap

#: Wrap width for the full rules text (bytes).
WRAP_WIDTH = 76

#: SPEC base rules — prohibitive, turn-shaped guidance for any coding CLI.
BASE_RULES: tuple[str, ...] = (
    "Consult MAP/TESTS before any grep; state a concrete hypothesis before each retrieval.",
    (
        "Batch all candidate file reads into ONE parallel tool block; use line-ranged reads"
        " for files >200 lines; after editing, re-read only the edited range."
    ),
    (
        "Run the exact test target from TESTS; never rerun a test that passed with"
        " unchanged inputs."
    ),
    "Persist probes to one scratch file and edit it; never regenerate similar scripts.",
    "Stop-loss: after 2 failed searches, re-read the map — do not keep guessing.",
)

#: Extra bullet appended for specific CLI variants (SPEC).
VARIANT_EXTRA: dict[str, str] = {
    "claude": (
        "Require file:line references in subagent summaries; never re-retrieve code a"
        " subagent already covered."
    ),
    "shell": (
        "One edit per change; verify with a single targeted `sed -n 'A,Bp'`; persist"
        " probe scripts — do not regenerate variants."
    ),
}
#: The three rules variants (SPEC).
VARIANTS: tuple[str, ...] = ("generic", "claude", "shell")

# ``codegraph focus --cli`` name → rules variant. codex/opencode drive edits through
# shell commands, so they get the shell-flavored rules; anything unknown is generic.
_CLI_TO_VARIANT: dict[str, str] = {
    "claude": "claude",
    "codex": "shell",
    "opencode": "shell",
    "shell": "shell",
    "generic": "generic",
}

#: One-line fallback rules for tight budgets (deterministic per variant).
COMPACT_RULES: dict[str, str] = {
    "generic": "Map first; batch reads; run the TESTS runner; stop-loss after 2 failed searches.",
    "claude": "Map first; batch reads; run the TESTS runner; require file:line refs from subagents.",
    "shell": "Map first; one edit per change; run the TESTS runner; persist probe scripts.",
}


def resolve_variant(cli: str) -> str:
    """Map a CLI name to its rules variant (unknown names fall back to ``generic``)."""
    return _CLI_TO_VARIANT.get((cli or "").strip().lower(), "generic")


def bullet_lines(variant: str) -> list[str]:
    """Base rules plus the variant-specific extra bullet."""
    extra = VARIANT_EXTRA.get(variant, "")
    return list(BASE_RULES) + ([extra] if extra else [])


def flight_rules(cli: str) -> str:
    """Full wrapped flight-rules text for *cli* (deterministic, byte-stable)."""
    variant = resolve_variant(cli)
    return "\n".join(wrap(bullet, WRAP_WIDTH) for bullet in bullet_lines(variant))


def compact_rules(cli: str) -> str:
    """Single-line rules fallback for very tight budgets (deterministic)."""
    return COMPACT_RULES[resolve_variant(cli)]
