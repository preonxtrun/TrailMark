"""Flight rules: ONE fixed, CLI-agnostic hand-written bullet list (SPEC-frozen text).

The layer is agnostic by contract: the same rules ship to every coding CLI, phrased
only in terms every agent can follow (files, line ranges, tests, searches) — never
tool names, vendor features, or runtime mechanisms. ``flight_rules(cli)`` accepts a
cli name for API compatibility but does NOT fork behavior: the rules are identical
for every CLI.

A one-line :data:`COMPACT_RULES` fallback exists for tight budgets: below roughly
900 bytes the full wrapped text cannot coexist with the guaranteed MAP floor
(invariant #2), so the packer swaps to the compact line while keeping the RULES
section present.
"""

from __future__ import annotations

from ..emit.fmt import wrap

#: Wrap width for the full rules text (bytes).
WRAP_WIDTH = 76

#: The rules — prohibitive, turn-shaped guidance phrased CLI-neutrally.
BASE_RULES: tuple[str, ...] = (
    "Consult MAP/TESTS before any search; state a concrete hypothesis before each retrieval.",
    (
        "Locate candidate files with as few requests as possible; read line ranges,"
        " not whole files >200 lines; after editing, re-read only the edited range."
    ),
    (
        "Run the exact test target from TESTS; never rerun a test that passed with"
        " unchanged inputs."
    ),
    "Persist probes to one scratch file and edit it; never regenerate similar scripts.",
    (
        "When a delegated exploration returns a summary, keep its file:line references"
        " so the same code is never retrieved twice."
    ),
    (
        "Progress-stall: same failure twice or two ineffective fixes — write one new"
        " hypothesis, act on it, or report the blocker."
    ),
    "Stop-loss: after 2 failed searches, re-read the map — do not keep guessing.",
)

#: Kept for API compatibility; the layer ships one agnostic variant only.
VARIANTS: tuple[str, ...] = ("generic",)

#: One-line fallback rules for tight budgets (deterministic).
COMPACT_RULES: dict[str, str] = {
    "generic": (
        "Map first; fewest requests possible; line-ranged reads; exact test targets;"
        " stop-loss after 2 failed searches."
    )
}


def resolve_variant(cli: str) -> str:
    """Every CLI gets the one agnostic variant (kept for API compatibility)."""
    return "generic"


def bullet_lines(variant: str = "generic") -> list[str]:
    """The rules bullets (single agnostic variant)."""
    return list(BASE_RULES)


def flight_rules(cli: str) -> str:
    """Full wrapped flight-rules text (deterministic, byte-stable, CLI-agnostic)."""
    return "\n".join(wrap(bullet, WRAP_WIDTH) for bullet in bullet_lines())


def compact_rules(cli: str) -> str:
    """Single-line rules fallback for very tight budgets (deterministic)."""
    return COMPACT_RULES["generic"]
