//! ATLAS: IndexData → map / test-graph / glossary / rules / profile facts →
//! byte-budgeted block body (port of `atlas/__init__.py`).
//!
//! The public entry point is [`assemble`]: it returns the block BODY (no
//! markers); the caller writes it via `trailmark_core::emit`. Output is
//! deterministic — same (repo state, issue text, cli, budget) ⇒ byte-identical
//! body: every list is sorted with total tie-breaks, pagerank scores are used
//! for ordering only, and no timestamps or floats are ever emitted (invariant
//! #1). The optional user profile (`.trailmark/profile.txt`) is read once per
//! assemble and contributes the LAB facts section plus extra RULES lines; the
//! optional `.trailmark/baseline.txt` becomes the BASELINE section and the
//! stigmergic trail store (`.trailmark/trails.jsonl`) the TRAILS section.
//! [`assemble_with_leads`] is the focus-path variant driven by pre-ranked
//! leads (PLAN from leads + CONTRACTS / SNIPPETS / CLEARANCE sections).

pub mod budget;
pub mod builder;
pub mod glossary;
pub mod rules;
pub mod testgraph;

use std::collections::{HashMap, HashSet};
use std::path::Path;

use budget::{pack, Section};
use trailmark_core::{byte_len, py_splitlines, wrap_default, IndexData};

use crate::focus::ranker::Lead;

/// First line of every block body (SPEC block format).
pub const TITLE: &str = "# TRAILMARK ATLAS — auto-generated, do not edit inside markers";

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

/// Drop rank for cross-task trail memory (stigmergy,
/// `.trailmark/trails.jsonl`): after LAB (5), before PLAN verbose (10) —
/// recency hints die before planning (Python: `R_TRAILS = 7`).
pub const R_TRAILS: u32 = 7;

/// Drop rank for inline lead contracts + surgical snippets: high displacement
/// value — they replace early full-file reads (the highest replay-damage
/// content in a run). Python: `R_CONTRACTS = 32` / `R_SNIPPETS = 31`.
pub const R_CONTRACTS: u32 = 32;
/// Drop rank for the SNIPPETS section (Python: `R_SNIPPETS = 31`).
pub const R_SNIPPETS: u32 = 31;

/// Per-lead-file byte cap for CONTRACTS signature linemaps (Python:
/// `_contracts(..., per_file_cap=280)`).
pub const CONTRACTS_PER_FILE_CAP: usize = 280;
/// Per-lead byte cap for SNIPPETS bodies (Python:
/// `_extract_snippets(..., per_lead_cap=1400)`).
pub const SNIPPETS_PER_LEAD_CAP: usize = 1400;

/// BASELINE drop rank: right after LAB (`R_LAB + 1`).
pub const R_BASELINE: u32 = R_LAB + 1;
/// Maximum BASELINE lines kept (Python: `baseline_lines[:46]`).
pub const MAX_BASELINE_LINES: usize = 46;

/// Python `str.rstrip()` (no args): strip trailing whitespace — the Unicode
/// whitespace property plus `\\x1c`–`\\x1f`, which CPython's `str.isspace()`
/// also reports as space.
fn py_rstrip(text: &str) -> String {
    text.trim_end_matches(|c: char| matches!(c, '\u{1c}'..='\u{1f}') || c.is_whitespace())
        .to_string()
}

/// `(facts, extra_rules)` from an optional `.trailmark/profile.txt` in `root`.
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
    let path = Path::new(root).join(".trailmark").join("profile.txt");
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
/// compact variants), LAB (profile facts), BASELINE (snapshot tail), TRAILS
/// (cross-task trail memory), GLOSSARY. The optional
/// `.trailmark/profile.txt` is read once per assemble; profile rules extend
/// the full-RULES section and profile facts become the LAB section. The byte
/// budget is enforced by [`pack`] with the SPEC drop order; entries are never
/// split.
pub fn assemble(data: &IndexData, issue: Option<&str>, cli: &str, budget: usize) -> String {
    let pr = builder::pagerank_scores(data);
    assemble_full(data, issue, cli, budget, None, pr.as_ref())
}

/// [`assemble`] with a precomputed pagerank map and no leads (the PLAN section
/// falls back to the internal issue-keyword scorer). The pagerank values are
/// identical to `builder::pagerank_scores(data)` — e.g. the plain prior the
/// ranker computes — so callers can share one power-iteration run.
pub fn assemble_with_pagerank(
    data: &IndexData,
    issue: Option<&str>,
    cli: &str,
    budget: usize,
    pr: Option<&HashMap<String, f64>>,
) -> String {
    assemble_full(data, issue, cli, budget, None, pr)
}

/// [`assemble`] with focus leads: PLAN comes from the pre-ranked leads
/// (`_plan_from_leads`), and the CONTRACTS / SNIPPETS / CLEARANCE sections are
/// emitted only when non-empty leads exist — the exact `assemble(..., leads=…)`
/// flow of the Python reference. Empty leads fall back to the internal
/// issue-keyword PLAN (Python `if leads:` is a truthiness check).
pub fn assemble_with_leads(
    data: &IndexData,
    issue: Option<&str>,
    cli: &str,
    budget: usize,
    leads: Option<&[Lead]>,
    pr: Option<&HashMap<String, f64>>,
) -> String {
    assemble_full(data, issue, cli, budget, leads, pr)
}

/// Shared assemble core (the Python `atlas.assemble` body).
fn assemble_full(
    data: &IndexData,
    issue: Option<&str>,
    cli: &str,
    budget: usize,
    leads: Option<&[Lead]>,
    pr: Option<&HashMap<String, f64>>,
) -> String {
    let empty_pr: HashMap<String, f64> = HashMap::new();
    let pr_map = pr.unwrap_or(&empty_pr);
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
    // Python: `if issue:` (truthy) and, for the leads path, `if leads:`.
    let issue_live = issue.is_some_and(|i| !i.is_empty());
    let leads_live = leads.is_some_and(|l| !l.is_empty());
    if issue_live {
        let issue_text = issue.unwrap_or_default();
        let (verbose, compact) = if leads_live {
            plan_from_leads(leads.unwrap_or_default())
        } else {
            builder::build_plan(data, issue_text, pr)
        };
        sections.push(Section::with_rank("PLAN", R_PLAN_VERBOSE, verbose));
        sections.push(Section::with_rank("PLAN", R_PLAN_COMPACT, compact));
        if leads_live {
            let ls = leads.unwrap_or_default();
            let contract_items = contracts(data, ls, CONTRACTS_PER_FILE_CAP);
            if !contract_items.is_empty() {
                sections.push(Section::with_rank("CONTRACTS", R_CONTRACTS, contract_items));
            }
            let snippet_items = extract_snippets(data, ls, SNIPPETS_PER_LEAD_CAP);
            if !snippet_items.is_empty() {
                sections.push(Section::with_rank("SNIPPETS", R_SNIPPETS, snippet_items));
            }
        }
    }
    if issue_live && leads_live {
        if let Some(items) = clearance_items(pr_map, leads.unwrap_or_default()) {
            sections.push(Section::with_rank("CLEARANCE", R_PLAN_COMPACT, items));
        }
    }
    if !facts.is_empty() {
        sections.push(Section::with_rank(
            "LAB",
            R_LAB,
            facts.iter().map(|fact| format!("- {fact}")).collect(),
        ));
    }
    let baseline_lines = load_baseline(&data.root);
    if !baseline_lines.is_empty() {
        sections.push(Section::with_rank("BASELINE", R_BASELINE, baseline_lines));
    }
    let trail_items = crate::trails::section_items(&data.root);
    if !trail_items.is_empty() {
        sections.push(Section::with_rank("TRAILS", R_TRAILS, trail_items));
    }
    sections.push(Section::with_rank(
        "GLOSSARY",
        R_GLOSSARY,
        glossary::build_glossary(data, glossary::MAX_TERMS),
    ));
    pack(&sections, budget)
}

/// `(verbose, compact)` PLAN items from pre-ranked focus leads.
///
/// Port of `atlas._plan_from_leads`: the leads are the single source of truth
/// (produced by `focus::ranker::rank`); this only re-formats. An empty `why`
/// falls back to `"path match"` (Python `getattr(lead, "why", "") or ...`).
pub fn plan_from_leads(leads: &[Lead]) -> (Vec<String>, Vec<String>) {
    let mut lines: Vec<String> = Vec::new();
    let mut top_ref = String::new();
    for (i, lead) in leads.iter().take(5).enumerate() {
        let line_range = format!("{}-{}", lead.start_line, lead.end_line);
        let why = if lead.why.is_empty() {
            "path match"
        } else {
            lead.why.as_str()
        };
        lines.push(format!("{}. {}:{} — {}", i + 1, lead.path, line_range, why));
        if top_ref.is_empty() {
            top_ref = lead.path.clone();
        }
    }
    if lines.is_empty() {
        return (
            vec![
                "LEADS (verify, don't trust; one ranged read each, then act):".to_string(),
                "- (no lexical matches in index — start from MAP hot files)".to_string(),
            ],
            vec!["(no lexical matches — start from MAP hot files)".to_string()],
        );
    }
    let mut verbose =
        vec!["LEADS (verify, don't trust; one ranged read each, then act):".to_string()];
    verbose.extend(lines.iter().cloned());
    verbose.push(format!("OUTLINES: .trailmark/outlines/{top_ref}.md"));
    let compact = vec![lines[0].clone()];
    (verbose, compact)
}

/// Compact signature outlines for the top lead files (verify with one ranged
/// read). Port of `atlas._contracts`: one `"<path> (<n> lines):"` header per
/// lead file, then symbol linemap markers spread ~every 100 lines so the FIRST
/// ranged read lands right; each file capped at `per_file_cap` bytes. Only the
/// first 3 distinct lead paths are used; index-missing paths are skipped.
pub fn contracts(data: &IndexData, leads: &[Lead], per_file_cap: usize) -> Vec<String> {
    let mut items: Vec<String> = Vec::new();
    let mut seen: HashSet<String> = HashSet::new();
    for lead in leads.iter().take(3) {
        let path = lead.path.as_str();
        if path.is_empty() || !seen.insert(path.to_string()) {
            continue;
        }
        let Some(entry) = data.files.get(path) else {
            continue;
        };
        let total_lines = entry.symbols.iter().map(|s| s.end_line).max().unwrap_or(0);
        items.push(format!("{path} ({total_lines} lines):"));
        let mut used = byte_len(items.last().unwrap_or(&String::new())) + 1;
        let mut syms: Vec<&trailmark_core::Symbol> = entry.symbols.iter().collect();
        syms.sort_by(|a, b| (a.line, a.name.as_str()).cmp(&(b.line, b.name.as_str())));
        // linemap: spread markers ~every 100 lines so the FIRST ranged read lands right
        let mut last_marked: i64 = -1000;
        for sym in &syms {
            if (i64::from(sym.line) - last_marked) < 100 && used > 0 {
                continue;
            }
            let line = py_rstrip(&format!("  L{} {} {}", sym.line, sym.kind, sym.signature));
            if byte_len(&line) + used > per_file_cap {
                break;
            }
            items.push(line);
            used += byte_len(items.last().unwrap_or(&String::new())) + 1;
            last_marked = i64::from(sym.line);
        }
        if used >= per_file_cap {
            break;
        }
    }
    items
}

/// Extract the best-matching symbol's BODY for the top leads (surgical
/// context). Port of `atlas._extract_snippets`: reads the lead file from disk
/// at assemble time (Python `errors="replace"` semantics via lossy UTF-8),
/// slices the lead's `start..end` line range, and caps each lead at
/// `per_lead_cap` bytes with a deterministic truncation marker. Reads only
/// (G6 purity); unreadable files skip that lead, never fail the assemble.
pub fn extract_snippets(data: &IndexData, leads: &[Lead], per_lead_cap: usize) -> Vec<String> {
    let mut items: Vec<String> = Vec::new();
    let mut seen: HashSet<String> = HashSet::new();
    for lead in leads.iter().take(3) {
        let path = lead.path.as_str();
        if path.is_empty() || !seen.insert(path.to_string()) {
            continue;
        }
        if !data.files.contains_key(path) {
            continue;
        }
        // Python: max(1, int(lead.start_line or 1)) / int(lead.end_line or start)
        let start = i64::from(lead.start_line.max(1));
        let end = if lead.end_line == 0 {
            start
        } else {
            i64::from(lead.end_line)
        };
        let end = end.max(start);
        let source = match std::fs::read(Path::new(&data.root).join(path)) {
            Ok(bytes) => String::from_utf8_lossy(&bytes).into_owned(),
            Err(_) => continue, // OSError → skip this lead
        };
        let lines = py_splitlines(&source);
        let from = start.saturating_sub(1);
        let to = end.min(start.saturating_sub(1) + per_lead_cap as i64);
        let chunk: Vec<&str> = lines
            .iter()
            .skip(from as usize)
            .take((to as usize).saturating_sub(from as usize))
            .map(String::as_str)
            .collect();
        if chunk.is_empty() {
            continue;
        }
        items.push(format!("{path} L{start}-{end}:"));
        let mut used = byte_len(items.last().unwrap_or(&String::new())) + 1;
        for line_text in chunk {
            let cost = byte_len(line_text) + 3;
            if used + cost > per_lead_cap {
                items.push("  … (truncated — read the ranged lines for the rest)".to_string());
                break;
            }
            items.push(format!("  {line_text}"));
            used += cost;
        }
    }
    items
}

/// The CLEARANCE manifest: the 3 lowest-pagerank top-level directories that no
/// lead touches, ranked irrelevant for the task. Port of the Python
/// `if issue and leads:` block. Files without a `/` in their path group under
/// `"."`. Returns `None` when nothing is cleared (no section is appended).
pub fn clearance_items(pr: &HashMap<String, f64>, leads: &[Lead]) -> Option<Vec<String>> {
    let lead_dirs: HashSet<&str> = leads
        .iter()
        .map(|l| l.path.split('/').next().unwrap_or(""))
        .filter(|d| !d.is_empty())
        .collect();
    let mut dir_rank: HashMap<&str, f64> = HashMap::new();
    for (fpath, score) in pr {
        let d = if fpath.contains('/') {
            fpath.split('/').next().unwrap_or(".")
        } else {
            "."
        };
        let slot = dir_rank.entry(d).or_insert(0.0);
        if *score > *slot {
            *slot = *score;
        }
    }
    let mut cleared: Vec<(&str, f64)> = dir_rank
        .iter()
        .filter(|(d, _)| !lead_dirs.contains(*d))
        .map(|(d, s)| (*d, *s))
        .collect();
    cleared.sort_by(|a, b| {
        a.1.partial_cmp(&b.1)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then(a.0.cmp(b.0))
    });
    cleared.truncate(3);
    if cleared.is_empty() {
        return None;
    }
    let joined = cleared
        .iter()
        .map(|(d, _)| format!("{d}/"))
        .collect::<Vec<_>>()
        .join(", ");
    Some(vec![format!(
        "CLEARED (ranked irrelevant for this task — do not explore without a stated \
         reason): {joined}"
    )])
}

/// BASELINE lines from an optional `.trailmark/baseline.txt` in `root`
/// (Python: `baseline_path.read_text(...).splitlines()[:46]`, `[]` on OSError).
pub fn load_baseline(root: &str) -> Vec<String> {
    let path = Path::new(root).join(".trailmark").join("baseline.txt");
    let bytes = match std::fs::read(&path) {
        Ok(b) => b,
        Err(_) => return Vec::new(),
    };
    let mut lines = py_splitlines(&String::from_utf8_lossy(&bytes));
    lines.truncate(MAX_BASELINE_LINES);
    lines
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;
    use trailmark_core::{FileEntry, Ref, Symbol};

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
            let bytes = trailmark_core::byte_len(&body);
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
        assert!(full.contains("LEADS (verify"));
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
        assert!(trailmark_core::byte_len(&body) <= 2048);
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
        std::fs::create_dir_all(dir.join(".trailmark")).expect("mkdir .trailmark");
        std::fs::write(dir.join(".trailmark/profile.txt"), text).expect("write profile");
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

    fn lead(path: &str, start: u32, end: u32, why: &str) -> Lead {
        Lead {
            path: path.to_string(),
            start_line: start,
            end_line: end,
            why: why.to_string(),
            score: 0.5,
        }
    }

    fn scratch_repo(tag: &str) -> String {
        let dir = std::env::temp_dir().join(format!(
            "cg_atlas_{tag}_{}_{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_nanos())
                .unwrap_or(0)
        ));
        std::fs::create_dir_all(&dir).expect("mkdir scratch repo");
        dir.to_string_lossy().into_owned()
    }

    #[test]
    fn plan_from_leads_formats_and_sets_top_ref() {
        let leads = vec![
            lead(
                "app/service.py",
                12,
                30,
                "refresh a session token (SessionService.refresh)",
            ),
            lead("web/client.ts", 5, 9, "fetch json (fetchJson)"),
        ];
        let (verbose, compact) = plan_from_leads(&leads);
        assert_eq!(
            verbose[0],
            "LEADS (verify, don't trust; one ranged read each, then act):"
        );
        assert_eq!(
            verbose[1],
            "1. app/service.py:12-30 — refresh a session token (SessionService.refresh)"
        );
        assert_eq!(verbose[2], "2. web/client.ts:5-9 — fetch json (fetchJson)");
        assert_eq!(
            verbose[3],
            "OUTLINES: .trailmark/outlines/app/service.py.md"
        );
        assert_eq!(compact, vec![verbose[1].clone()]);
    }

    #[test]
    fn plan_from_leads_empty_why_falls_back_to_path_match() {
        let leads = vec![lead("a.py", 1, 2, "")];
        let (verbose, _compact) = plan_from_leads(&leads);
        assert_eq!(verbose[1], "1. a.py:1-2 — path match");
    }

    #[test]
    fn contracts_spread_markers_and_cap_per_file() {
        let mut files = BTreeMap::new();
        let mut syms: Vec<Symbol> = Vec::new();
        for i in 0..30 {
            syms.push(Symbol {
                file: "big/mod.py".into(),
                name: format!("fn_{i:02}"),
                kind: "def".into(),
                line: 10 * i + 1, // ~every 100 lines a marker survives the spread
                end_line: 10 * i + 5,
                signature: format!("fn_{i:02}(payload, options=None) -> dict:"),
                doc: String::new(),
            });
        }
        files.insert(
            "big/mod.py".to_string(),
            FileEntry {
                path: "big/mod.py".into(),
                lang: "py".into(),
                mtime: 1.0,
                size: 1,
                symbols: syms,
            },
        );
        let data = IndexData {
            root: "/r".into(),
            files,
            refs: vec![],
        };
        let leads = vec![lead("big/mod.py", 1, 5, "fn_00 (big.mod)")];
        let items = contracts(&data, &leads, 280);
        assert_eq!(items[0], "big/mod.py (295 lines):");
        // first marker at the earliest symbol; the next only ~100 lines later
        assert_eq!(items[1], "  L1 def fn_00(payload, options=None) -> dict:");
        assert_eq!(items[2], "  L101 def fn_10(payload, options=None) -> dict:");
        assert_eq!(items[3], "  L201 def fn_20(payload, options=None) -> dict:");
        let used: usize = items.iter().map(|l| byte_len(l) + 1).sum();
        assert!(used <= 280, "per-file cap exceeded: {used}\n{items:?}");
        // two leads sharing a path collapse to one; index-missing paths skip
        let dup = vec![
            lead("big/mod.py", 1, 5, "a"),
            lead("big/mod.py", 1, 5, "b"),
            lead("ghost/missing.py", 1, 2, "c"),
        ];
        let again = contracts(&data, &dup, 280);
        assert_eq!(again, items);
    }

    #[test]
    fn contracts_breaks_leads_once_a_file_fills_its_cap() {
        let mut files = BTreeMap::new();
        for (path, sig_len) in [("a/one.py", 63usize), ("b/two.py", 63)] {
            files.insert(
                path.to_string(),
                FileEntry {
                    path: path.to_string(),
                    lang: "py".into(),
                    mtime: 1.0,
                    size: 1,
                    symbols: (0..4)
                        .map(|i| Symbol {
                            file: path.to_string(),
                            name: format!("f{i}"),
                            kind: "def".into(),
                            line: 1000 * i + 1,
                            end_line: 1000 * i + 2,
                            signature: format!("f{}({})", i, "x".repeat(sig_len)),
                            doc: String::new(),
                        })
                        .collect(),
                },
            );
        }
        let data = IndexData {
            root: "/r".into(),
            files,
            refs: vec![],
        };
        let leads = vec![lead("a/one.py", 1, 2, "x"), lead("b/two.py", 1, 2, "y")];
        // cap tuned so the first file's single marker saturates `used` exactly
        // (22-byte header + 76-byte marker + 2 newlines = 100): Python's
        // `if used >= per_file_cap: break` then abandons the remaining leads.
        let items = contracts(&data, &leads, 100);
        assert_eq!(items.len(), 2, "{items:?}");
        assert!(items[0].starts_with("a/one.py"), "{items:?}");
        assert!(items.iter().all(|l| !l.contains("b/two.py")), "{items:?}");
        // with a huge cap both leads appear
        let items = contracts(&data, &leads, 10_000);
        assert!(items.iter().any(|l| l.starts_with("b/two.py")), "{items:?}");
    }

    #[test]
    fn snippets_body_extraction_cap_and_truncation_marker() {
        let root = scratch_repo("snippets");
        let body: String = (1..=50)
            .map(|i| format!("line {i} — some payload text for the snippet body\n"))
            .collect();
        std::fs::create_dir_all(Path::new(&root).join("app")).expect("mkdir app");
        std::fs::write(Path::new(&root).join("app/mod.py"), &body).expect("write mod.py");
        let mut files = BTreeMap::new();
        files.insert(
            "app/mod.py".to_string(),
            FileEntry {
                path: "app/mod.py".into(),
                lang: "py".into(),
                mtime: 1.0,
                size: 1,
                symbols: vec![Symbol {
                    file: "app/mod.py".into(),
                    name: "f".into(),
                    kind: "def".into(),
                    line: 2,
                    end_line: 51,
                    signature: "def f(x)".into(),
                    doc: String::new(),
                }],
            },
        );
        let data = IndexData {
            root,
            files,
            refs: vec![],
        };
        let leads = vec![lead("app/mod.py", 2, 51, "f (app.mod)")];
        let items = extract_snippets(&data, &leads, 400);
        assert_eq!(items[0], "app/mod.py L2-51:");
        assert_eq!(
            items[1],
            "  line 2 — some payload text for the snippet body"
        );
        assert!(
            items.contains(&"  … (truncated — read the ranged lines for the rest)".to_string()),
            "cap must produce the truncation marker:\n{items:?}"
        );
        // every emitted line (except the marker itself) fit within the cap at
        // emission time — Python's `used + cost > per_lead_cap` guard
        let used: usize = items
            .iter()
            .filter(|l| !l.contains("(truncated"))
            .map(|l| byte_len(l) + 1)
            .sum();
        assert!(used <= 400, "per-lead cap exceeded: {used}");
        // index-missing or unreadable files are skipped silently
        let ghost = vec![lead("app/ghost.py", 1, 2, "g")];
        assert!(extract_snippets(&data, &ghost, 400).is_empty());
        let _ = std::fs::remove_dir_all(&data.root);
    }

    #[test]
    fn clearance_picks_three_lowest_ranked_non_lead_dirs() {
        let mut pr = HashMap::new();
        for (path, score) in [
            ("app/high.py", 0.9),
            ("web/high.ts", 0.8),
            ("docs/low.py", 0.05),
            ("misc/lower.py", 0.02),
            ("scripts/tiny.py", 0.01),
            ("extra.py", 0.5), // root-level file → dir "."
        ] {
            pr.insert(path.to_string(), score);
        }
        let leads = vec![
            lead("app/high.py", 1, 2, "x"),
            lead("web/high.ts", 1, 2, "y"),
        ];
        let items = clearance_items(&pr, &leads).expect("cleared dirs exist");
        assert_eq!(
            items[0],
            "CLEARED (ranked irrelevant for this task — do not explore without a \
             stated reason): scripts/, misc/, docs/"
        );
        // lead dirs and nothing else are excluded; the "." dir joins the race
        let mut pr_root_only = HashMap::new();
        pr_root_only.insert("solo.py".to_string(), 0.1);
        let items = clearance_items(&pr_root_only, &leads).expect("root dir cleared");
        assert!(items[0].ends_with(": ./"), "{items:?}");
        // no pr entries → nothing to clear
        assert!(clearance_items(&HashMap::new(), &leads).is_none());
    }

    #[test]
    fn baseline_section_reads_and_caps_at_46_lines() {
        let root = scratch_repo("baseline");
        std::fs::create_dir_all(Path::new(&root).join(".trailmark")).expect("mkdir");
        let text: String = (1..=60).map(|i| format!("baseline line {i}\n")).collect();
        std::fs::write(Path::new(&root).join(".trailmark/baseline.txt"), text)
            .expect("write baseline");
        assert_eq!(load_baseline(&root).len(), MAX_BASELINE_LINES);
        assert_eq!(load_baseline(&root)[0], "baseline line 1");
        assert_eq!(load_baseline(&root)[45], "baseline line 46");
        // absent file → empty
        assert!(load_baseline("/nonexistent/repo").is_empty());
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn assemble_renders_baseline_and_trails_sections() {
        let root = scratch_repo("sections");
        let mut data = repo();
        data.root = root.clone();
        std::fs::create_dir_all(Path::new(&root).join(".trailmark")).expect("mkdir");
        std::fs::write(
            Path::new(&root).join(".trailmark/baseline.txt"),
            "BASELINE (captured before your changes — report the delta):\n$ pytest -q\n  exit=0\n",
        )
        .expect("write baseline");
        std::fs::write(
            Path::new(&root).join(".trailmark/trails.jsonl"),
            concat!(
                "{\"files\": [\"src/api.py\"], \"id\": \"t1\", \"outcome\": \"solved\"}\n",
                "{\"files\": [\"web/client.ts\"], \"id\": \"t2\", \"outcome\": \"failed\"}\n"
            ),
        )
        .expect("write trails");
        let body = assemble(&data, Some("handle_request"), "generic", 8192);
        assert!(body.contains("BASELINE"), "{body}");
        assert!(body.contains("$ pytest -q"));
        assert!(body.contains("TRAILS"), "{body}");
        assert!(body.contains("- src/api.py — well-trodden (1 solved tasks)"));
        assert!(body.contains("DEAD ENDS (avoid blind retries):"));
        assert!(body.contains("- web/client.ts — dead end (failed tasks)"));
        // trails are additive: without a store the body equals the trails-less build
        let mut no_trails = repo();
        no_trails.root = scratch_repo("notrails");
        let with = assemble(&data, Some("handle_request"), "generic", 8192);
        let without = assemble(&no_trails, Some("handle_request"), "generic", 8192);
        let strip = |body: &str| -> String {
            body.lines()
                .filter(|l| {
                    !l.starts_with("BASELINE")
                        && !l.starts_with("$ pytest")
                        && !l.starts_with("  exit=")
                        && !l.starts_with("TRAILS")
                        && !l.starts_with("- src/api.py — well-trodden")
                        && !l.starts_with("DEAD ENDS")
                        && !l.starts_with("- web/client.ts — dead end")
                })
                .collect::<Vec<_>>()
                .join("\n")
        };
        assert_eq!(strip(&with), strip(&without));
        let _ = std::fs::remove_dir_all(&root);
        let _ = std::fs::remove_dir_all(&no_trails.root);
    }
}
