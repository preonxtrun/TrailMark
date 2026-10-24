//! PLAN section renderer (port of `focus/plan.py`), byte-budgeted.
//!
//! Full form:
//!
//! ```text
//! PLAN
//! LEADS (verify, don't trust; one ranged read each, then act):
//! 1. <path>:<line>-<end> — <why>
//! OUTLINES: .trailmark/outlines/<rel>.md
//! ```
//!
//! Verbosity drops whole units only (SPEC invariant #2): full → compact (all
//! leads, no `why`) → fewer leads → header only. `""` is returned only when the
//! budget is below the 120-byte minimum (or there is nothing renderable at all).

use trailmark_core::byte_len;

use super::outlines::outline_posix;
use super::ranker::Lead;

const PLAN_HEADER: &str = "PLAN";
const LEADS_HEADER: &str = "LEADS (verify, don't trust; one ranged read each, then act):";
const MIN_BUDGET_BYTES: usize = 120;

/// Render the PLAN section for the first `count` leads at a given verbosity.
fn render(leads: &[Lead], with_why: bool, count: Option<usize>) -> String {
    let shown = match count {
        Some(n) => &leads[..n.min(leads.len())],
        None => leads,
    };
    let mut lines = vec![PLAN_HEADER.to_string(), LEADS_HEADER.to_string()];
    for (i, lead) in shown.iter().enumerate() {
        let mut entry = format!(
            "{}. {}:{}-{}",
            i + 1,
            lead.path,
            lead.start_line,
            lead.end_line
        );
        if with_why && !lead.why.is_empty() {
            entry = format!("{entry} — {}", lead.why);
        }
        lines.push(entry);
    }
    lines.extend(
        shown
            .iter()
            .map(|lead| format!("OUTLINES: {}", outline_posix(&lead.path))),
    );
    lines.join("\n")
}

/// Render the PLAN section for `leads` within `budget_bytes` (UTF-8, inclusive).
///
/// Never exceeds the budget and never truncates mid-entry. Returns "" only when
/// `budget_bytes` is below 120 (or there is nothing renderable at all).
pub fn render_plan(leads: &[Lead], budget_bytes: usize) -> String {
    if budget_bytes < MIN_BUDGET_BYTES {
        return String::new();
    }
    let header_only = format!("{PLAN_HEADER}\n{LEADS_HEADER}");
    if leads.is_empty() {
        return header_only;
    }
    let full = render(leads, true, None);
    if byte_len(&full) <= budget_bytes {
        return full;
    }
    let compact = render(leads, false, None);
    if byte_len(&compact) <= budget_bytes {
        return compact;
    }
    for count in (1..leads.len()).rev() {
        let trimmed = render(leads, false, Some(count));
        if byte_len(&trimmed) <= budget_bytes {
            return trimmed;
        }
    }
    header_only
}

#[cfg(test)]
mod tests {
    use super::*;

    fn lead(path: &str, why: &str) -> Lead {
        Lead {
            path: path.to_string(),
            start_line: 3,
            end_line: 12,
            why: why.to_string(),
            score: 0.9,
        }
    }

    #[test]
    fn render_plan_full_form() {
        let leads = vec![lead("src/a.py", "the handler")];
        let out = render_plan(&leads, 400);
        assert_eq!(
            out,
            "PLAN\nLEADS (verify, don't trust; one ranged read each, then act):\n1. src/a.py:3-12 — the handler\n\
             OUTLINES: .trailmark/outlines/src/a.py.md"
        );
    }

    #[test]
    fn render_plan_drops_why_then_leads() {
        let leads: Vec<Lead> = (0..3)
            .map(|i| lead(&format!("src/f{i}.py"), "w".repeat(80).as_str()))
            .collect();
        let full = render_plan(&leads, 10000);
        let compact = render_plan(&leads, byte_len(&full) - 1);
        assert!(
            !compact.contains(&format!(" — {}", "w".repeat(80))),
            "{compact}"
        );
        assert!(compact.contains("3. src/f2.py:3-12"));
        let tighter = render_plan(&leads, 200);
        assert!(byte_len(&tighter) <= 200);
        assert!(tighter.starts_with("PLAN\nLEADS (verify, don't trust; one ranged read each, then act):"));
        assert_eq!(render_plan(&leads, 119), "");
        // long paths: even one compact lead exceeds a 130-byte budget → header only
        let long: Vec<Lead> = (0..2)
            .map(|i| {
                lead(
                    &format!("src/{}", "long_dir_name/".repeat(4) + &format!("f{i}.py")),
                    "w",
                )
            })
            .collect();
        let header = render_plan(&long, 130);
        assert_eq!(header, "PLAN\nLEADS (verify, don't trust; one ranged read each, then act):");
    }

    #[test]
    fn render_plan_empty_leads_gives_header() {
        assert_eq!(render_plan(&[], 400), "PLAN\nLEADS (verify, don't trust; one ranged read each, then act):");
    }
}
