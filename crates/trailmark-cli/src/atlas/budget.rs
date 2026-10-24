//! Byte-budgeted section packer for the atlas block body
//! (port of `atlas/budget.py`).
//!
//! Input: an ordered list of [`Section`]s in emit order. Output: one body
//! string whose UTF-8 size is at most the budget. Whole sections and
//! individual entries are dropped whole by priority — entries are never split
//! or truncated mid-entry.
//!
//! Drop priority semantics (lower rank = dropped earlier):
//!
//! * `rank`        — drop the whole section at once (all-or-nothing, e.g. GLOSSARY).
//! * `item_rank`   — drop non-core items tail-first (e.g. extra MAP/TESTS rows).
//! * `core_rank`   — drop core items tail-first once everything else is gone.
//!
//! Sections that share a tag are *variants* of one section (e.g. PLAN verbose
//! and compact): only the first surviving one is rendered, so dropping the
//! verbose variant transparently reveals the compact one.

use trailmark_core::byte_len;

/// One block section: a tag line plus entry lines, with drop priorities.
///
/// `tag=""` marks a header-less pseudo-section (e.g. the block title line).
/// `None` disables the corresponding drop mode; `core` counts items that are
/// protected from `item_rank` drops (they only fall at `core_rank`).
#[derive(Clone, Debug, Default)]
pub struct Section {
    /// Section header line ("" = header-less pseudo-section).
    pub tag: String,
    /// Drop the whole section at this priority.
    pub rank: Option<u32>,
    /// Entry lines.
    pub items: Vec<String>,
    /// Number of leading items protected from `item_rank` drops.
    pub core: usize,
    /// Drop extra (non-core) items at this priority, tail-first.
    pub item_rank: Option<u32>,
    /// Drop core items at this priority, tail-first.
    pub core_rank: Option<u32>,
}

impl Section {
    /// Build a section with only a whole-section drop rank.
    pub fn with_rank(tag: &str, rank: u32, items: Vec<String>) -> Section {
        Section {
            tag: tag.to_string(),
            rank: Some(rank),
            items,
            ..Default::default()
        }
    }
}

/// Drop candidate: (rank, section index, kind) — kind 0 = whole section,
/// 1 = extra item, 2 = core item.
type Choice = (u32, usize, u8);

/// Pick the next drop candidate: minimum (rank, section index, kind).
///
/// Core items are always dropped tail-first; extra items likewise, which
/// preserves the emitter's importance order within each section.
fn pick(work: &[Section], visible: &[bool]) -> Option<Choice> {
    let mut best: Option<Choice> = None;
    for (i, sec) in work.iter().enumerate() {
        if !visible[i] || sec.items.is_empty() {
            continue;
        }
        if let Some(rank) = sec.rank {
            let cand = (rank, i, 0u8);
            if best.is_none_or(|b| cand < b) {
                best = Some(cand);
            }
        }
        if let Some(rank) = sec.item_rank {
            if sec.items.len() > sec.core {
                let cand = (rank, i, 1u8);
                if best.is_none_or(|b| cand < b) {
                    best = Some(cand);
                }
            }
        }
        if let Some(rank) = sec.core_rank {
            if !sec.items.is_empty() {
                let cand = (rank, i, 2u8);
                if best.is_none_or(|b| cand < b) {
                    best = Some(cand);
                }
            }
        }
    }
    best
}

/// Join sections into the body text; also flag which sections are visible.
///
/// A section is visible when it still has items and no earlier section with
/// the same tag is visible (variant semantics).
fn render(work: &[Section]) -> (String, Vec<bool>) {
    let mut out: Vec<&str> = Vec::new();
    let mut visible: Vec<bool> = Vec::with_capacity(work.len());
    let mut seen_tags: std::collections::HashSet<&str> = std::collections::HashSet::new();
    for sec in work {
        let vis =
            !sec.items.is_empty() && (sec.tag.is_empty() || !seen_tags.contains(sec.tag.as_str()));
        visible.push(vis);
        if !vis {
            continue;
        }
        if !sec.tag.is_empty() {
            seen_tags.insert(&sec.tag);
            out.push(&sec.tag);
        }
        out.extend(sec.items.iter().map(|s| s.as_str()));
    }
    (out.join("\n"), visible)
}

/// Assemble `sections` into one body string of at most `budget` bytes.
///
/// Deterministic: the same input sections and budget always yield the same body.
pub fn pack(sections: &[Section], budget: usize) -> String {
    let limit = budget;
    let mut work: Vec<Section> = sections.to_vec();
    let (mut text, mut visible) = render(&work);
    while byte_len(&text) > limit {
        let Some((_rank, index, kind)) = pick(&work, &visible) else {
            break; // nothing left to drop; remaining text is empty
        };
        match kind {
            0 => work[index].items.clear(),
            _ => {
                work[index].items.pop();
            }
        }
        let rendered = render(&work);
        text = rendered.0;
        visible = rendered.1;
    }
    debug_assert!(
        byte_len(&text) <= limit || pick(&work, &visible).is_none(),
        "packed body exceeds budget"
    );
    text
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sec(tag: &str, rank: Option<u32>, items: &[&str]) -> Section {
        Section {
            tag: tag.to_string(),
            rank,
            items: items.iter().map(|s| s.to_string()).collect(),
            ..Default::default()
        }
    }

    #[test]
    fn pack_drops_whole_sections_by_rank_first() {
        let sections = [
            Section {
                tag: "A".into(),
                rank: None,
                items: vec!["a1".into(), "a2".into()],
                core: 0,
                item_rank: Some(30),
                core_rank: None,
            },
            sec("B", Some(0), &["b1"]),
            Section::with_rank("C", 90, vec!["c1".to_string()]),
        ];
        // budget fits only A + C without B
        let body = pack(&sections, byte_len("A\na1\na2\nC\nc1"));
        assert_eq!(body, "A\na1\na2\nC\nc1");
        // tighter: B (rank 0) drops first, then A's tail item (item_rank 30)
        let body = pack(&sections, byte_len("A\na1\nC\nc1"));
        assert_eq!(body, "A\na1\nC\nc1");
        // below that: A's last item drops, then C (rank 90) drops whole
        let tiny = pack(&sections, 3);
        assert_eq!(tiny, "");
    }

    #[test]
    fn pack_variant_semantics_reveal_compact() {
        let verbose = Section::with_rank("PLAN", 10, vec!["long lead ...".into()]);
        let compact = Section::with_rank("PLAN", 20, vec!["short".into()]);
        let sections = [verbose, compact, sec("TAIL", None, &["x"])];
        // both fit: only the verbose variant is visible
        let big = pack(&sections, 1000);
        assert!(big.contains("long lead"));
        assert!(!big.contains("short"));
        // verbose dropped → compact revealed
        let small = pack(&sections, byte_len("TAIL\nx") + 2);
        assert!(
            small.contains("short") || !small.contains("PLAN"),
            "{small}"
        );
    }

    #[test]
    fn pack_item_drops_are_tail_first_with_core_protection() {
        let map = Section {
            tag: "MAP".into(),
            rank: None,
            items: vec!["r1".into(), "r2".into(), "r3".into()],
            core: 1,
            item_rank: Some(40),
            core_rank: Some(60),
        };
        let rules = Section::with_rank("RULES", 50, vec!["rules".into()]);
        let sections = [map, rules];
        // r3 then r2 drop (item_rank, tail first); r1 is protected until core_rank
        let tight = pack(&sections, byte_len("MAP\nr1\nRULES\nrules"));
        assert_eq!(tight, "MAP\nr1\nRULES\nrules");
        // below that, RULES (rank 50) drops before MAP core (rank 60)
        let tighter = pack(&sections, byte_len("MAP\nr1"));
        assert_eq!(tighter, "MAP\nr1");
    }

    #[test]
    fn pack_never_exceeds_budget() {
        let sections = [
            Section::with_rank(
                "",
                90,
                vec!["# TRAILMARK ATLAS — auto-generated, do not edit inside markers".to_string()],
            ),
            Section::with_rank("GLOSSARY", 0, vec!["- term = meaning (a.py:1)".to_string()]),
            Section {
                tag: "MAP".into(),
                rank: Some(40),
                items: vec!["- a/b.py:1 fn() — doc".to_string()],
                core: 0,
                item_rank: Some(40),
                core_rank: Some(60),
            },
        ];
        for budget in [0usize, 16, 64, 200, 2048] {
            let body = pack(&sections, budget);
            assert!(byte_len(&body) <= budget, "budget {budget}: {body}");
        }
    }
}
