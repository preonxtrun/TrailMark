//! Flight rules: fixed hand-written bullet lists (port of `atlas/rules.py`,
//! strings copied verbatim from the reference).
//!
//! The layer ships one CLI-agnostic variant: `resolve_variant` keeps the
//! per-CLI API shape (the reference kept it "for API compatibility") but every
//! CLI resolves to the same agnostic text.

use trailmark_core::wrap_default;

/// Wrap width for the full rules text (bytes).
pub const WRAP_WIDTH: usize = 76;

/// The rules — prohibitive, turn-shaped guidance phrased CLI-neutrally.
pub const BASE_RULES: [&str; 10] = [
    "Consult MAP/TESTS before any search; state a concrete hypothesis before each retrieval.",
    "Work through tool calls: keep any text between them to one short line. Every turn replays all prior context — front-load discovery, batch reads, and never read more than ~150 lines per turn.",
    "Locate candidate files with as few requests as possible; read line ranges, not whole files >200 lines; after editing, re-read only the edited range.",
    "Run the exact test target from TESTS as given (its output is already bounded); never rerun a test that passed with unchanged inputs.",
    "Persist probes to one scratch file and edit it; never regenerate similar scripts.",
    "When a delegated exploration returns a summary, keep its file:line references so the same code is never retrieved twice.",
    "Progress-stall: same failure twice or two ineffective fixes — write one new hypothesis, act on it, or report the blocker.",
    "Scratchpad: keep facts learned, dead ends and the current hypothesis in .trailmark/scratch.md; consult it before re-reading any file.",
    "Verify probes: after each edit run .trailmark/probes/verify.sh exactly as written — one call, bounded output; update the scratchpad HYPOTHESIS/RESULT lines before the next edit. Never compose your own test command.",
    "Stop-loss: after 2 failed searches, re-read the map — do not keep guessing.",
];

/// Kept for API compatibility; the layer ships one agnostic variant only.
pub const VARIANTS: [&str; 1] = ["generic"];

/// One-line fallback rules for tight budgets (deterministic).
pub const COMPACT_RULES: &str = "Map first; fewest requests possible; line-ranged reads; exact test targets; stop-loss after 2 failed searches.";

/// Every CLI gets the one agnostic variant (kept for API compatibility).
pub fn resolve_variant(_cli: &str) -> &'static str {
    "generic"
}

/// The rules bullets (single agnostic variant).
pub fn bullet_lines(_variant: &str) -> Vec<&'static str> {
    BASE_RULES.to_vec()
}

/// Full wrapped flight-rules text (deterministic, byte-stable, CLI-agnostic).
pub fn flight_rules(_cli: &str) -> String {
    bullet_lines("generic")
        .iter()
        .map(|bullet| wrap_default(bullet, WRAP_WIDTH))
        .collect::<Vec<_>>()
        .join("\n")
}

/// Single-line rules fallback for very tight budgets (deterministic).
pub fn compact_rules(_cli: &str) -> &'static str {
    COMPACT_RULES
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn variants_all_resolve_to_the_agnostic_variant() {
        for cli in [
            "claude", "codex", "opencode", "shell", "generic", "weird", "",
        ] {
            assert_eq!(resolve_variant(cli), "generic");
        }
    }

    #[test]
    fn flight_rules_are_byte_stable_and_bulleted() {
        let generic = flight_rules("generic");
        assert_eq!(generic, flight_rules("claude"));
        assert!(generic.starts_with("- Consult MAP/TESTS before any search;"));
        assert!(generic.contains("- Stop-loss: after 2 failed searches"));
        assert!(
            generic.lines().count() >= 18,
            "wrapped shape tracks the 10 bullets"
        );
    }

    #[test]
    fn compact_rules_match_python_strings() {
        assert_eq!(
            compact_rules("generic"),
            "Map first; fewest requests possible; line-ranged reads; exact test targets; stop-loss after 2 failed searches."
        );
    }
}
