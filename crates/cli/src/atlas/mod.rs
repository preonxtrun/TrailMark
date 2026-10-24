//! ATLAS: IndexData → map / test-graph / glossary / rules → byte-budgeted block
//! body (port of `atlas/__init__.py`).
//!
//! The public entry point is [`assemble`]: it returns the block BODY (no
//! markers); the caller writes it via `codegraph_core::emit`. Output is
//! deterministic — same (repo state, issue text, cli, budget) ⇒ byte-identical
//! body: every list is sorted with total tie-breaks, pagerank scores are used
//! for ordering only, and no timestamps or floats are ever emitted (invariant
//! #1).

pub mod budget;
pub mod builder;
pub mod glossary;
pub mod rules;
pub mod testgraph;

use budget::{pack, Section};
use codegraph_core::IndexData;

/// First line of every block body (SPEC block format).
pub const TITLE: &str = "# CODEGRAPH ATLAS — auto-generated, do not edit inside markers";

// Drop ranks (lower = dropped earlier). SPEC drop order: GLOSSARY → PLAN
// (verbose → compact → omit) → extra TESTS rows → extra MAP rows. Beyond that,
// only where the full rules text cannot coexist with the guaranteed floors,
// RULES compacts to its one-line variant; header, ≥3 MAP rows and the TESTS
// runner row survive at any budget ≥ 512 bytes.
pub const R_GLOSSARY: u32 = 0;
pub const R_PLAN_VERBOSE: u32 = 10;
pub const R_PLAN_COMPACT: u32 = 20;
pub const R_TESTS_ITEM: u32 = 30;
pub const R_MAP_ITEM: u32 = 40;
pub const R_RULES_FULL: u32 = 50;
pub const R_MAP_CORE: u32 = 60;
pub const R_TESTS_CORE: u32 = 70;
pub const R_RULES_COMPACT: u32 = 80;
pub const R_HEADER: u32 = 90;

/// Rows protected from budget drops (invariant #2 floors).
pub const MAP_CORE_ROWS: usize = 3;
/// TESTS runner row protected from item drops.
pub const TESTS_CORE_ROWS: usize = 1;

/// Build the deterministic atlas block BODY (no markers) within `budget` bytes.
///
/// Sections, in emit order: title, RULES (full text, with a one-line compact
/// fallback), MAP (directory roles + pagerank-ordered hot files), TESTS (runner
/// + src→test map), PLAN (only when `issue` is given, verbose + compact
/// variants), GLOSSARY. The byte budget is enforced by [`pack`] with the SPEC
/// drop order; entries are never split.
pub fn assemble(data: &IndexData, issue: Option<&str>, cli: &str, budget: usize) -> String {
    let pr = builder::pagerank_scores(data);
    let mut sections: Vec<Section> = Vec::new();
    sections.push(Section::with_rank("", R_HEADER, vec![TITLE.to_string()]));
    sections.push(Section::with_rank(
        "RULES",
        R_RULES_FULL,
        rules::flight_rules(cli).split('\n').map(String::from).collect(),
    ));
    sections.push(Section::with_rank(
        "RULES",
        R_RULES_COMPACT,
        vec![format!("- {}", rules::compact_rules(cli))],
    ));
    sections.push(Section {
        tag: "MAP".into(),
        rank: None,
        items: builder::build_map(data, pr.as_ref()),
        core: MAP_CORE_ROWS,
        item_rank: Some(R_MAP_ITEM),
        core_rank: Some(R_MAP_CORE),
    });
    sections.push(Section {
        tag: "TESTS".into(),
        rank: None,
        items: testgraph::build_tests(data, pr.as_ref()),
        core: TESTS_CORE_ROWS,
        item_rank: Some(R_TESTS_ITEM),
        core_rank: Some(R_TESTS_CORE),
    });
    if let Some(issue) = issue {
        if !issue.is_empty() {
            let (verbose, compact) = builder::build_plan(data, issue, pr.as_ref());
            sections.push(Section::with_rank("PLAN", R_PLAN_VERBOSE, verbose));
            sections.push(Section::with_rank("PLAN", R_PLAN_COMPACT, compact));
        }
    }
    sections.push(Section::with_rank(
        "GLOSSARY",
        R_GLOSSARY,
        glossary::build_glossary(data, glossary::MAX_TERMS),
    ));
    pack(&sections, budget)
}

#[cfg(test)]
mod tests {
    use super::*;
    use codegraph_core::{FileEntry, Ref, Symbol};
    use std::collections::BTreeMap;

    fn repo() -> IndexData {
        let mut files = BTreeMap::new();
        for (path, lang, name) in [
            ("src/api.py", "py", "handle_request"),
            ("src/util.py", "py", "parse_config"),
            ("web/client.ts", "ts", "fetchJson"),
            ("web/index.ts", "ts", "mountApp"),
            ("tests/test_api.py", "py", "test_handle_request"),
        ] {
            files.insert(
                path.to_string(),
                FileEntry {
                    path: path.to_string(),
                    lang: lang.to_string(),
                    mtime: 1.0,
                    size: 100,
                    symbols: vec![Symbol {
                        file: path.to_string(),
                        name: name.to_string(),
                        kind: "def".into(),
                        line: 3,
                        end_line: 9,
                        signature: format!("def {name}(request)"),
                        doc: format!("The {name} entrypoint."),
                    }],
                },
            );
        }
        let refs = vec![
            Ref { src_file: "tests/test_api.py".into(), dst_name: "handle_request".into(), dst_file: "src/api.py".into(), line: 4 },
            Ref { src_file: "src/api.py".into(), dst_name: "parse_config".into(), dst_file: "src/util.py".into(), line: 5 },
            Ref { src_file: "web/client.ts".into(), dst_name: "mountApp".into(), dst_file: "web/index.ts".into(), line: 6 },
        ];
        IndexData { root: "/repo".into(), files, refs }
    }

    #[test]
    fn assemble_respects_budget_and_order() {
        let data = repo();
        for budget in [512usize, 1024, 2048, 8192] {
            let body = assemble(&data, Some("fix the fetchJson client parsing"), "generic", budget);
            let bytes = codegraph_core::byte_len(&body);
            assert!(bytes <= budget, "budget {budget} exceeded: {bytes}\n{body}");
            assert!(body.starts_with(TITLE), "{body}");
        }
    }

    #[test]
    fn assemble_rules_and_three_map_rows_survive_512() {
        let data = repo();
        let body = assemble(&data, None, "claude", 512);
        assert!(body.contains("RULES"), "{body}");
        assert!(body.contains("MAP"), "{body}");
        let map_rows = body.split('\n').filter(|l| l.starts_with("- ")).count();
        assert!(map_rows >= 3, "MAP floor violated at 512B:\n{body}");
    }

    #[test]
    fn assemble_drop_order_glossary_first() {
        let data = repo();
        let full = assemble(&data, Some("handle_request parsing"), "generic", 8192);
        // with a large budget the PLAN leads exist
        assert!(full.contains("PLAN"));
        assert!(full.contains("LEADS (verify, don't trust):"));
        // shrink: PLAN compacts before MAP loses rows
        let mid = assemble(&data, Some("handle_request parsing"), "generic", 900);
        let has_plan = mid.contains("PLAN");
        let map_rows = mid.split('\n').filter(|l| l.starts_with("- src") || l.starts_with("- web")).count();
        if !has_plan {
            assert!(map_rows >= 3);
        }
    }

    #[test]
    fn assemble_is_deterministic() {
        let data = repo();
        let a = assemble(&data, Some("issue text"), "shell", 2048);
        let b = assemble(&data, Some("issue text"), "shell", 2048);
        assert_eq!(a, b);
    }

    #[test]
    fn assemble_generic_rules_at_2048_exclude_glossary_or_fit() {
        let data = repo();
        let body = assemble(&data, Some("parse_config"), "generic", 2048);
        assert!(codegraph_core::byte_len(&body) <= 2048);
    }
}
