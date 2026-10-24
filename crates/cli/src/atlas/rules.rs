//! Flight rules: fixed hand-written bullet lists with per-CLI variants
//! (port of `atlas/rules.py` — SPEC-frozen text, copied verbatim).

use codegraph_core::wrap_default;

/// Wrap width for the full rules text (bytes).
pub const WRAP_WIDTH: usize = 76;

/// SPEC base rules — prohibitive, turn-shaped guidance for any coding CLI.
pub const BASE_RULES: [&str; 5] = [
    "Consult MAP/TESTS before any grep; state a concrete hypothesis before each retrieval.",
    "Batch all candidate file reads into ONE parallel tool block; use line-ranged reads for files >200 lines; after editing, re-read only the edited range.",
    "Run the exact test target from TESTS; never rerun a test that passed with unchanged inputs.",
    "Persist probes to one scratch file and edit it; never regenerate similar scripts.",
    "Stop-loss: after 2 failed searches, re-read the map — do not keep guessing.",
];

/// Extra bullet appended for specific CLI variants (SPEC).
fn variant_extra(variant: &str) -> Option<&'static str> {
    match variant {
        "claude" => Some(
            "Require file:line references in subagent summaries; never re-retrieve code a subagent already covered.",
        ),
        "shell" => Some(
            "One edit per change; verify with a single targeted `sed -n 'A,Bp'`; persist probe scripts — do not regenerate variants.",
        ),
        _ => None,
    }
}

/// The three rules variants (SPEC).
pub const VARIANTS: [&str; 3] = ["generic", "claude", "shell"];

/// ``codegraph focus --cli`` name → rules variant. codex/opencode drive edits
/// through shell commands, so they get the shell-flavored rules; anything
/// unknown is generic.
fn cli_to_variant(cli: &str) -> &'static str {
    match cli.trim().to_lowercase().as_str() {
        "claude" => "claude",
        "codex" | "opencode" | "shell" => "shell",
        _ => "generic",
    }
}

/// One-line fallback rules for tight budgets (deterministic per variant).
fn compact_rule(variant: &str) -> &'static str {
    match variant {
        "claude" => {
            "Map first; batch reads; run the TESTS runner; require file:line refs from subagents."
        }
        "shell" => "Map first; one edit per change; run the TESTS runner; persist probe scripts.",
        _ => "Map first; batch reads; run the TESTS runner; stop-loss after 2 failed searches.",
    }
}

/// Map a CLI name to its rules variant (unknown names fall back to `generic`).
pub fn resolve_variant(cli: &str) -> &'static str {
    cli_to_variant(cli)
}

/// Base rules plus the variant-specific extra bullet.
pub fn bullet_lines(variant: &str) -> Vec<&'static str> {
    let mut out: Vec<&'static str> = BASE_RULES.to_vec();
    if let Some(extra) = variant_extra(variant) {
        out.push(extra);
    }
    out
}

/// Full wrapped flight-rules text for `cli` (deterministic, byte-stable).
pub fn flight_rules(cli: &str) -> String {
    let variant = resolve_variant(cli);
    bullet_lines(variant)
        .iter()
        .map(|bullet| wrap_default(bullet, WRAP_WIDTH))
        .collect::<Vec<_>>()
        .join("\n")
}

/// Single-line rules fallback for very tight budgets (deterministic).
pub fn compact_rules(cli: &str) -> &'static str {
    compact_rule(resolve_variant(cli))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn variants_resolve_exactly_like_python() {
        assert_eq!(resolve_variant("claude"), "claude");
        assert_eq!(resolve_variant("codex"), "shell");
        assert_eq!(resolve_variant("opencode"), "shell");
        assert_eq!(resolve_variant("shell"), "shell");
        assert_eq!(resolve_variant("generic"), "generic");
        assert_eq!(resolve_variant("weird-cli"), "generic");
        assert_eq!(resolve_variant(""), "generic");
    }

    #[test]
    fn flight_rules_are_byte_stable_and_bulleted() {
        let generic = flight_rules("generic");
        assert_eq!(generic, flight_rules("generic"));
        assert!(generic.starts_with("- Consult MAP/TESTS"));
        assert!(!generic.contains("subagent"));
        let claude = flight_rules("claude");
        assert!(claude.contains("Require file:line references"));
        assert!(codegraph_core::byte_len(&claude) > codegraph_core::byte_len(&generic));
    }

    #[test]
    fn compact_rules_match_python_strings() {
        assert_eq!(
            compact_rules("generic"),
            "Map first; batch reads; run the TESTS runner; stop-loss after 2 failed searches."
        );
    }
}
