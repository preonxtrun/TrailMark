//! ATLAS: IndexData → map / test-graph / glossary / rules / profile facts →
//! byte-budgeted block body (port of `atlas/__init__.py`).
//!
//! The public entry point is [`assemble`]: it returns the block BODY (no
//! markers); the caller writes it via `codegraph_core::emit`. Output is
//! deterministic — same (repo state, issue text, cli, budget) ⇒ byte-identical
//! body: every list is sorted with total tie-breaks, pagerank scores are used
//! for ordering only, and no timestamps or floats are ever emitted (invariant
//! #1). The optional user profile (`.codegraph/profile.txt`) is read once per
//! assemble and contributes the LAB facts section plus extra RULES lines.

pub mod budget;
pub mod builder;
pub mod glossary;
pub mod rules;
pub mod testgraph;

use std::path::Path;

use budget::{pack, Section};
use codegraph_core::{wrap_default, IndexData};

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

/// Drop rank for user-authored environment facts (profile file): after
/// GLOSSARY, before PLAN — facts are cheap and high-value on lab/cluster
/// workloads (Python: `R_LAB = 5`).
pub const R_LAB: u32 = 5;

/// Maximum facts kept from the profile file (Python: `facts[:8]`).
pub const MAX_PROFILE_FACTS: usize = 8;
/// Maximum extra rules kept from the profile file (Python: `extra_rules[:6]`).
pub const MAX_PROFILE_RULES: usize = 6;

/// Python `str.splitlines()` equivalent: split on the full separator set
/// (`\n`, `\r`, `\r\n`, `\v`, `\f`, `\x1c`–`\x1e`, `\u{85}`, U+2028/9) without
/// keep-ends. A trailing separator does not produce a trailing empty line.
fn py_splitlines(text: &str) -> Vec<String> {
    let mut lines: Vec<String> = Vec::new();
    let mut cur = String::new();
    let mut chars = text.chars().peekable();
    while let Some(c) = chars.next() {
        match c {
            // \r\n is one separator
            '\r' if chars.peek() == Some(&'\n') => {
                chars.next();
                lines.push(std::mem::take(&mut cur));
            }
            '\n' | '\r' | '\u{0b}' | '\u{0c}' | '\u{1c}' | '\u{1d}' | '\u{1e}' | '\u{85}'
            | '\u{2028}' | '\u{2029}' => {
                lines.push(std::mem::take(&mut cur));
            }
            other => cur.push(other),
        }
    }
    if !cur.is_empty() {
        lines.push(cur);
    }
    lines
}

/// `(facts, extra_rules)` from an optional `.codegraph/profile.txt` in `root`.
///
/// Port of `atlas.load_profile`: lines beginning `fact:` become facts and
/// `rule:` lines become extra rules (payload stripped; caps 8 / 6); everything
/// else — `#` comments included — is ignored. A missing or unreadable file
/// yields `([], [])`. The payload check is "one more character after the
/// prefix", which for a `fact:`/`rule:`-prefixed line is exactly the Python
/// `len(line) > 5` guard (the prefix alone is 5 chars). Invalid UTF-8 is
/// replaced rather than raising (the reference would raise `UnicodeDecodeError`
/// past its `except OSError`; Rust library paths must not panic).
pub fn load_profile(root: &str) -> (Vec<String>, Vec<String>) {
    let path = Path::new(root).join(".codegraph").join("profile.txt");
    let bytes = match std::fs::read(&path) {
        Ok(b) => b,
        Err(_) => return (Vec::new(), Vec::new()),
    };
    let mut facts: Vec<String> = Vec::new();
    let mut extra_rules: Vec<String> = Vec::new();
    for line in py_splitlines(&String::from_utf8_lossy(&bytes)) {
        let line = line.trim();
        if let Some(payload) = line.strip_prefix("fact:") {
            if !payload.is_empty() {
                facts.push(payload.trim().to_string());
            }
        } else if let Some(payload) = line.strip_prefix("rule:") {
            if !payload.is_empty() {
                extra_rules.push(payload.trim().to_string());
            }
        }
    }
    facts.truncate(MAX_PROFILE_FACTS);
    extra_rules.truncate(MAX_PROFILE_RULES);
    (facts, extra_rules)
}

/// Build the deterministic atlas block BODY (no markers) within `budget` bytes.
///
/// Sections, in emit order: title, RULES (full text, with a one-line compact
/// fallback), MAP (directory roles plus pagerank-ordered hot files), TESTS
/// (runner and src→test map), PLAN (only when `issue` is given, verbose and
/// compact variants), LAB (profile facts), GLOSSARY. The optional
/// `.codegraph/profile.txt` is read once per assemble; profile rules extend
/// the full-RULES section and profile facts become the LAB section. The byte
/// budget is enforced by [`pack`] with the SPEC drop order; entries are never
/// split.
pub fn assemble(data: &IndexData, issue: Option<&str>, cli: &str, budget: usize) -> String {
    let pr = builder::pagerank_scores(data);
    assemble_with_pagerank(data, issue, cli, budget, pr.as_ref())
}

/// [`assemble`] with a precomputed pagerank map (the focus path reuses the
/// ranker's plain-pagerank scores instead of recomputing them; identical
/// values, one fewer power-iteration run).
pub fn assemble_with_pagerank(
    data: &IndexData,
    issue: Option<&str>,
    cli: &str,
    budget: usize,
    pr: Option<&HashMap<String, f64>>,
) -> String {
    let mut sections: Vec<Section> = Vec::new();
    sections.push(Section::with_rank("", R_HEADER, vec![TITLE.to_string()]));
    let (facts, extra_rules) = load_profile(&data.root);
    let mut rules_items: Vec<String> = rules::flight_rules(cli)
        .split('\n')
        .map(String::from)
        .collect();
    for extra in &extra_rules {
        rules_items.push(wrap_default(extra, rules::WRAP_WIDTH));
    }
    sections.push(Section::with_rank("RULES", R_RULES_FULL, rules_items));
    sections.push(Section::with_rank(
        "RULES",
        R_RULES_COMPACT,
        vec![format!("- {}", rules::compact_rules(cli))],
    ));
    sections.push(Section {
        tag: "MAP".into(),
        rank: None,
        items: builder::build_map(data, pr),
        core: MAP_CORE_ROWS,
        item_rank: Some(R_MAP_ITEM),
        core_rank: Some(R_MAP_CORE),
    });
    sections.push(Section {
        tag: "TESTS".into(),
        rank: None,
        items: testgraph::build_tests(data, pr),
        core: TESTS_CORE_ROWS,
        item_rank: Some(R_TESTS_ITEM),
        core_rank: Some(R_TESTS_CORE),
    });
    if let Some(issue) = issue {
        if !issue.is_empty() {
            let (verbose, compact) = builder::build_plan(data, issue, pr);
            sections.push(Section::with_rank("PLAN", R_PLAN_VERBOSE, verbose));
            sections.push(Section::with_rank("PLAN", R_PLAN_COMPACT, compact));
        }
    }
    if !facts.is_empty() {
        sections.push(Section::with_rank(
            "LAB",
            R_LAB,
            facts.iter().map(|fact| format!("- {fact}")).collect(),
        ));
    }
    sections.push(Section::with_rank(
        "GLOSSARY",
        R_GLOSSARY,
        glossary::build_glossary(data, glossary::MAX_TERMS),
    ));
    pack(&sections, budget)
}

use std::collections::HashMap;

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
            Ref {
                src_file: "tests/test_api.py".into(),
                dst_name: "handle_request".into(),
                dst_file: "src/api.py".into(),
                line: 4,
            },
            Ref {
                src_file: "src/api.py".into(),
                dst_name: "parse_config".into(),
                dst_file: "src/util.py".into(),
                line: 5,
            },
            Ref {
                src_file: "web/client.ts".into(),
                dst_name: "mountApp".into(),
                dst_file: "web/index.ts".into(),
                line: 6,
            },
        ];
        IndexData {
            root: "/repo".into(),
            files,
            refs,
        }
    }

    #[test]
    fn assemble_respects_budget_and_order() {
        let data = repo();
        for budget in [512usize, 1024, 2048, 8192] {
            let body = assemble(
                &data,
                Some("fix the fetchJson client parsing"),
                "generic",
                budget,
            );
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
        let map_rows = mid
            .split('\n')
            .filter(|l| l.starts_with("- src") || l.starts_with("- web"))
            .count();
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

    fn write_profile(tag: &str, text: &str) -> String {
        let dir = std::env::temp_dir().join(format!(
            "cg_profile_{tag}_{}_{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_nanos())
                .unwrap_or(0)
        ));
        std::fs::create_dir_all(dir.join(".codegraph")).expect("mkdir .codegraph");
        std::fs::write(dir.join(".codegraph/profile.txt"), text).expect("write profile");
        dir.to_string_lossy().into_owned()
    }

    #[test]
    fn load_profile_parses_facts_rules_comments_and_caps() {
        let text = "\
# cluster profile (comment — ignored)
fact: partitions: gpu(a100, 24h), cpu; submit with sbatch -A acct
rule:   check sacct once per job; never poll squeue in a loop
note: neither fact nor rule — ignored
fact:

rule:
fact: scratch is /scratch/$USER, purged every 30 days
fact: two
fact: three
fact: four
fact: five
fact: six
fact: seven
fact: eight
fact: nine — beyond the cap
rule: r1
rule: r2
rule: r3
rule: r4
rule: r5
rule: r6
rule: r7 — beyond the cap
";
        let root = write_profile("caps", text);
        let (facts, rules) = load_profile(&root);
        assert_eq!(facts.len(), 8, "facts cap at 8: {facts:?}");
        assert_eq!(
            facts[0],
            "partitions: gpu(a100, 24h), cpu; submit with sbatch -A acct"
        );
        assert!(!facts.iter().any(|f| f.contains("beyond the cap")));
        assert_eq!(rules.len(), 6, "rules cap at 6: {rules:?}");
        assert_eq!(
            rules[0],
            "check sacct once per job; never poll squeue in a loop"
        );
        assert!(!rules.iter().any(|r| r.contains("beyond the cap")));
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn load_profile_missing_or_empty_file_is_empty() {
        let (facts, rules) = load_profile("/nonexistent/repo/root");
        assert!(facts.is_empty() && rules.is_empty());
        let root = write_profile("empty", "# only comments\n\nplain line\n");
        let (facts, rules) = load_profile(&root);
        assert!(facts.is_empty() && rules.is_empty());
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn py_splitlines_matches_python_str_splitlines() {
        assert_eq!(py_splitlines("a\nb"), vec!["a", "b"]);
        assert_eq!(py_splitlines("a\n"), vec!["a"]); // no trailing empty line
        assert_eq!(py_splitlines("a\r\nb"), vec!["a", "b"]); // \r\n is one break
        assert_eq!(py_splitlines("a\rb"), vec!["a", "b"]);
        assert_eq!(py_splitlines("\n"), vec![""]);
        assert_eq!(py_splitlines(""), Vec::<String>::new());
        assert_eq!(py_splitlines("a\n\nb"), vec!["a", "", "b"]);
        assert_eq!(py_splitlines("a\u{2028}b\u{85}c"), vec!["a", "b", "c"]);
    }

    #[test]
    fn assemble_renders_lab_and_wrapped_profile_rules() {
        let data = repo();
        // a profile rule long enough to wrap at the 76-byte rules width (its
        // wording deliberately differs from the built-in bullet #5)
        let profile_text = concat!(
            "# profile\nfact: gpu queue drains at midnight\nfact: scratch quota 1 TB\n",
            "rule: check sacct once per job\n",
            "rule: when a delegated exploration returns a summary, keep its file:line ",
            "references so the very same probe is never retrieved twice\n",
        );
        let root = write_profile("lab", profile_text);
        let mut data = data;
        data.root = root.clone();
        let body = assemble(&data, Some("fix shared_api"), "generic", 8192);
        assert!(body.contains("LAB"), "LAB section missing:\n{body}");
        assert!(body.contains("- gpu queue drains at midnight"));
        assert!(body.contains("- scratch quota 1 TB"));
        // extra rules are wrapped with the same fmt as base rules and appended
        // to RULES after the built-in bullets
        let rules_idx = body.find("RULES").expect("RULES header");
        let lab_idx = body.find("LAB").expect("LAB header");
        let rules_block = &body[rules_idx..lab_idx];
        assert!(rules_block.contains("check sacct once per job"));
        assert!(rules_block.contains("  references so the very same probe"));
        // LAB drops after GLOSSARY but before PLAN under budget pressure
        let tight = assemble(&data, Some("fix shared_api"), "generic", 900);
        if tight.contains("PLAN") {
            assert!(
                tight.contains("LAB"),
                "LAB survives while PLAN does:\n{tight}"
            );
        }
        let _ = std::fs::remove_dir_all(&root);
    }
}
