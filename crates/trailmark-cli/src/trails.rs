//! Stigmergic trails: cross-task pheromone memory in `.trailmark/trails.jsonl`
//! (port of `src/trailmark/trails.py`).
//!
//! Ant-colony style memory for coding sessions (stigmergy): solved tasks
//! deposit "well-trodden paths" (the files that mattered), failed tasks deposit
//! "dead ends" (repellents), and future tasks read the accumulated field as a
//! TRAILS section of the injected block. Signals **evaporate** (the store keeps
//! only the last 32 entries) and failures act as **negative signals** (files
//! touched only by failed tasks are surfaced as dead ends to avoid blind
//! retries).
//!
//! Storage is JSON lines, one entry per task id (latest wins), no timestamps,
//! fixed key order (`files`, `id`, `outcome` — Python `json.dumps(...,
//! sort_keys=True)` byte format, `ensure_ascii` escaping included). All
//! functions are best-effort: a missing or malformed store reads as empty and
//! never panics (invariant #4), and writes touch only `.trailmark/**`
//! (invariant #5). Determinism (invariant #1): the same record calls always
//! produce byte-identical file content, and ranking uses exact rational
//! weights (i128 fractions — the Python reference uses `fractions.Fraction`)
//! with stable `(weight, path)` tie-breaks — no floats anywhere in output.

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};

use trailmark_core::py_splitlines;

/// Store location, relative to the repo root.
pub const TRAILS_FILE: &str = ".trailmark/trails.jsonl";

/// Evaporation by truncation: at most the last 32 entries are kept.
pub const MAX_ENTRIES: usize = 32;

/// Section caps: top-6 hot files, top-4 dead ends.
pub const HOT_LIMIT: usize = 6;
/// Maximum dead-end rows.
pub const DEAD_END_LIMIT: usize = 4;

/// Admissible outcomes; "failed" is the negative (repellent) signal.
pub const OUTCOMES: [&str; 2] = ["solved", "failed"];

/// Header line introducing the dead-end (avoid-retry) rows inside TRAILS.
pub const DEAD_END_HEADER: &str = "DEAD ENDS (avoid blind retries):";

/// One stored trail: a task id, its outcome, and the files that mattered.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Entry {
    /// Task id (one trail line per id, latest wins).
    pub id: String,
    /// `"solved"` or `"failed"`.
    pub outcome: String,
    /// Repo-relative paths, deduped and sorted (byte-determinism).
    pub files: Vec<String>,
}

/// Exact rational (Python `Fraction` equivalent): reduced `num/den`, `den > 0`.
///
/// Weights are sums of `1/k` for `k` in `2..=MAX_ENTRIES+1`, so numerators and
/// denominators stay far below i128 overflow even before reduction.
#[derive(Clone, Copy, Debug)]
struct Fraction {
    num: i128,
    den: i128,
}

impl Fraction {
    const ZERO: Fraction = Fraction { num: 0, den: 1 };

    fn new(num: i128, den: i128) -> Fraction {
        debug_assert!(den > 0, "denominator must be positive");
        let g = gcd(num.unsigned_abs(), den.unsigned_abs()).max(1);
        Fraction {
            num: num / i128::try_from(g).unwrap_or(1),
            den: den / i128::try_from(g).unwrap_or(1),
        }
    }

    fn add(self, other: Fraction) -> Fraction {
        Fraction::new(
            self.num * other.den + other.num * self.den,
            self.den * other.den,
        )
    }

    /// Exact comparison (cross-multiplication — no float rounding).
    fn cmp_frac(&self, other: &Fraction) -> std::cmp::Ordering {
        (self.num * other.den).cmp(&(other.num * self.den))
    }
}

fn gcd(a: u128, b: u128) -> u128 {
    if b == 0 {
        a
    } else {
        gcd(b, a % b)
    }
}

/// Absolute path of the trails store under `root`.
fn trails_path(root: &str) -> PathBuf {
    Path::new(root).join(".trailmark").join("trails.jsonl")
}

/// Parse one store line into an entry, applying the Python shape check:
/// string non-empty `id`, known `outcome`, list-of-string `files` (empty
/// strings dropped). Anything else → `None` (the line is skipped).
fn parse_entry(line: &str) -> Option<Entry> {
    let value: serde_json::Value = serde_json::from_str(line).ok()?;
    let obj = value.as_object()?;
    let task_id = obj.get("id")?.as_str()?;
    if task_id.is_empty() {
        return None;
    }
    let outcome = obj.get("outcome")?.as_str()?;
    if !OUTCOMES.contains(&outcome) {
        return None;
    }
    let files_json = obj.get("files")?.as_array()?;
    if !files_json.iter().all(|f| f.is_string()) {
        return None;
    }
    let files = files_json
        .iter()
        .filter_map(|f| f.as_str())
        .filter(|f| !f.is_empty())
        .map(String::from)
        .collect();
    Some(Entry {
        id: task_id.to_string(),
        outcome: outcome.to_string(),
        files,
    })
}

/// Parsed entries, oldest first (append order). Missing/malformed store → `[]`.
///
/// Blank and unparseable lines are skipped, as are entries failing the shape
/// check — a store is never allowed to crash the pipeline; a later [`record`]
/// rewrites the file and thereby heals damaged lines.
pub fn load_entries(root: &str) -> Vec<Entry> {
    let bytes = match std::fs::read(trails_path(root)) {
        Ok(b) => b,
        Err(_) => return Vec::new(),
    };
    py_splitlines(&String::from_utf8_lossy(&bytes))
        .iter()
        .map(|line| line.trim())
        .filter(|line| !line.is_empty())
        .filter_map(parse_entry)
        .collect()
}

/// Python `json.dumps` string escaping with `ensure_ascii=True`: control
/// characters use the short escapes or `\u00xx`, everything outside
/// `0x20..=0x7e` becomes `\uxxxx` (surrogate pairs for astral code points).
fn json_string(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => {
                out.push_str(&format!("\\u{:04x}", c as u32));
            }
            c if (c as u32) > 0x7e => {
                let code = c as u32;
                if code <= 0xffff {
                    out.push_str(&format!("\\u{:04x}", code));
                } else {
                    let v = code - 0x1_0000;
                    let hi = 0xd800 + (v >> 10);
                    let lo = 0xdc00 + (v & 0x3ff);
                    out.push_str(&format!("\\u{:04x}\\u{:04x}", hi, lo));
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

/// One store line in the Python `json.dumps(entry, sort_keys=True)` byte
/// format: keys sorted (`files`, `id`, `outcome`), `", "` / `": "` separators.
fn entry_json_line(entry: &Entry) -> String {
    let files = entry
        .files
        .iter()
        .map(|f| json_string(f))
        .collect::<Vec<_>>()
        .join(", ");
    format!(
        "{{\"files\": [{files}], \"id\": {}, \"outcome\": {}}}",
        json_string(&entry.id),
        json_string(&entry.outcome)
    )
}

/// Rewrite the store with `entries` (oldest first), atomically.
///
/// The payload is written to a temp file inside `.trailmark/` and moved into
/// place, so a killed run can never leave a torn store behind. Same entries
/// always yield identical bytes.
fn write_entries(root: &str, entries: &[Entry]) -> Result<(), String> {
    let path = trails_path(root);
    let parent = path
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_else(|| PathBuf::from("."));
    std::fs::create_dir_all(&parent).map_err(|e| format!("io error: {e}"))?;
    let mut payload = String::new();
    for entry in entries {
        payload.push_str(&entry_json_line(entry));
        payload.push('\n');
    }
    // temp name unique per process/time/counter (tempfile.mkstemp equivalent)
    static COUNTER: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let tmp = parent.join(format!(
        ".trails-{}-{}-{}.tmp",
        std::process::id(),
        nanos,
        COUNTER.fetch_add(1, std::sync::atomic::Ordering::Relaxed)
    ));
    let write = std::fs::write(&tmp, payload.as_bytes());
    let result = write.and_then(|()| std::fs::rename(&tmp, &path));
    if result.is_err() {
        let _ = std::fs::remove_file(&tmp); // never leave torn temp files behind
    }
    result.map_err(|e| format!("io error: {e}"))
}

/// Append or update one task's trail — one line per task id, latest wins.
///
/// `files` are repo-relative paths that mattered in the task; blanks and
/// duplicates are dropped and the rest is sorted for byte-determinism.
/// `outcome` must be `"solved"` or `"failed"`. The store keeps at most the
/// last [`MAX_ENTRIES`] entries — older trails evaporate by truncation.
/// Re-recording an id moves its trail to the newest position. Returns the
/// stored entry (Python raises `ValueError` for a bad outcome or empty id —
/// surfaced here as `Err`).
pub fn record(root: &str, task_id: &str, files: &[String], outcome: &str) -> Result<Entry, String> {
    if !OUTCOMES.contains(&outcome) {
        return Err(format!(
            "outcome must be one of ('solved', 'failed'), got '{outcome}'"
        ));
    }
    let task_id = task_id.trim();
    if task_id.is_empty() {
        return Err("task id must be a non-empty string".to_string());
    }
    let mut clean: Vec<String> = files
        .iter()
        .map(|f| f.trim().to_string())
        .filter(|f| !f.is_empty())
        .collect();
    clean.sort();
    clean.dedup();
    let entry = Entry {
        id: task_id.to_string(),
        outcome: outcome.to_string(),
        files: clean,
    };
    let mut entries: Vec<Entry> = load_entries(root)
        .into_iter()
        .filter(|old| old.id != entry.id)
        .collect();
    entries.push(entry.clone());
    if entries.len() > MAX_ENTRIES {
        entries.drain(..entries.len() - MAX_ENTRIES);
    }
    write_entries(root, &entries)?;
    Ok(entry)
}

/// Core scoring over the store: `(hot as (path, solved_count), dead_ends)`.
///
/// hot ranks files by recency-weighted solved frequency: an entry `k` positions
/// from the newest contributes weight `1/(k+1)` to each of its files (exact
/// rational arithmetic — no float rounding), ties break by path; at most
/// [`HOT_LIMIT`] files. dead_ends are distinct files appearing only in failed
/// entries (never in solved), most-recent-first, ties by path; at most
/// [`DEAD_END_LIMIT`].
fn ranked(root: &str) -> (Vec<(String, usize)>, Vec<String>) {
    let entries = load_entries(root);
    let total = entries.len();
    let mut weight: HashMap<String, Fraction> = HashMap::new();
    let mut solved_count: HashMap<String, usize> = HashMap::new();
    let mut ever_solved: HashSet<String> = HashSet::new();
    let mut failed_pos: HashMap<String, usize> = HashMap::new();
    for (index, entry) in entries.iter().enumerate() {
        let pos_from_newest = total - index; // newest entry → 1
                                             // dedupe, order-preserving (Python `dict.fromkeys(entry["files"])`)
        let mut seen: HashSet<&str> = HashSet::new();
        for path in &entry.files {
            if !seen.insert(path.as_str()) {
                continue;
            }
            if entry.outcome == "solved" {
                let w = Fraction::new(1, i128::from(pos_from_newest as u64 + 1));
                let current = weight.get(path).copied().unwrap_or(Fraction::ZERO);
                weight.insert(path.clone(), current.add(w));
                *solved_count.entry(path.clone()).or_insert(0) += 1;
                ever_solved.insert(path.clone());
            } else {
                // "failed" — the only other outcome `load_entries` admits
                let slot = failed_pos.entry(path.clone()).or_insert(total + 1);
                *slot = (*slot).min(pos_from_newest);
            }
        }
    }
    let mut hot: Vec<&String> = weight.keys().collect();
    hot.sort_by(|a, b| weight[*b].cmp_frac(&weight[*a]).then_with(|| a.cmp(b)));
    hot.truncate(HOT_LIMIT);
    let hot: Vec<(String, usize)> = hot
        .into_iter()
        .map(|path| {
            let count = solved_count.get(path).copied().unwrap_or(0);
            (path.clone(), count)
        })
        .collect();
    let mut dead: Vec<(String, usize)> = failed_pos
        .iter()
        .filter(|(path, _)| !ever_solved.contains(*path))
        .map(|(path, pos)| ((*path).clone(), *pos))
        .collect();
    dead.sort_by(|a, b| a.1.cmp(&b.1).then_with(|| a.0.cmp(&b.0)));
    dead.truncate(DEAD_END_LIMIT);
    let dead: Vec<String> = dead.into_iter().map(|(path, _)| path).collect();
    (hot, dead)
}

/// `(hot, dead_ends)` from the store at `root`.
///
/// hot — up to [`HOT_LIMIT`] files ranked by recency-weighted solved frequency
/// (recent solved tasks count more), ties by path. dead_ends — up to
/// [`DEAD_END_LIMIT`] distinct files appearing ONLY in failed entries,
/// most-recent-first, ties by path. Missing/empty store → `([], [])`.
pub fn summarize(root: &str) -> (Vec<String>, Vec<String>) {
    let (hot, dead) = ranked(root);
    let hot = hot.into_iter().map(|(path, _count)| path).collect();
    (hot, dead)
}

/// TRAILS section body items for the atlas assemble.
///
/// The atlas owns placement (emit order) and the drop rank; this owns the row
/// format. Empty when the store has no usable entries — the caller must then
/// append nothing, keeping a repo without trails byte-identical.
pub fn section_items(root: &str) -> Vec<String> {
    let (hot, dead) = ranked(root);
    if hot.is_empty() && dead.is_empty() {
        return Vec::new();
    }
    let mut items: Vec<String> = hot
        .iter()
        .map(|(path, count)| format!("- {path} — well-trodden ({count} solved tasks)"))
        .collect();
    if !dead.is_empty() {
        items.push(DEAD_END_HEADER.to_string());
        items.extend(
            dead.iter()
                .map(|path| format!("- {path} — dead end (failed tasks)")),
        );
    }
    items
}

/// Delete the trails store at `root`. Returns true when a file was removed.
pub fn clear(root: &str) -> bool {
    let path = trails_path(root);
    let existed = path.exists();
    if existed {
        let _ = std::fs::remove_file(&path);
    }
    existed
}

/// `--json` payload for a successful `trails record`
/// (Python: `json.dumps({...}, sort_keys=True)` → keys entries, files, id,
/// outcome, recorded).
pub fn record_payload_json(entry: &Entry, entries: usize) -> String {
    let files = entry
        .files
        .iter()
        .map(|f| json_string(f))
        .collect::<Vec<_>>()
        .join(", ");
    format!(
        "{{\"entries\": {entries}, \"files\": [{files}], \"id\": {}, \
         \"outcome\": {}, \"recorded\": true}}",
        json_string(&entry.id),
        json_string(&entry.outcome)
    )
}

/// `--json` payload for `trails clear`
/// (Python: `json.dumps({"cleared": True, "removed": removed}, sort_keys=True)`).
pub fn clear_payload_json(removed: bool) -> String {
    format!("{{\"cleared\": true, \"removed\": {removed}}}")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch(tag: &str) -> String {
        let dir = std::env::temp_dir().join(format!(
            "cg_trails_{tag}_{}_{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_nanos())
                .unwrap_or(0)
        ));
        std::fs::create_dir_all(&dir).expect("mkdir trails scratch");
        dir.to_string_lossy().into_owned()
    }

    #[test]
    fn record_writes_one_sorted_deduped_line_per_task() {
        let root = scratch("record");
        let entry = record(
            &root,
            "T1",
            &["b.py".into(), "a.py".into(), "a.py".into()],
            "solved",
        )
        .expect("record ok");
        assert_eq!(entry.id, "T1");
        assert_eq!(entry.files, vec!["a.py", "b.py"]);
        let raw = std::fs::read_to_string(trails_path(&root)).expect("store exists");
        assert_eq!(
            raw,
            "{\"files\": [\"a.py\", \"b.py\"], \"id\": \"T1\", \"outcome\": \"solved\"}\n"
        );
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn record_is_byte_deterministic_and_upserts_by_id() {
        let root = scratch("deterministic");
        record(&root, "T1", &["b.py".into(), "a.py".into()], "solved").expect("ok");
        let raw1 = std::fs::read(trails_path(&root)).expect("read");
        record(&root, "T1", &["a.py".into(), "b.py".into()], "solved").expect("ok");
        let raw2 = std::fs::read(trails_path(&root)).expect("read");
        assert_eq!(raw1, raw2);
        assert_eq!(load_entries(&root).len(), 1, "latest wins per id");
        // re-recording moves the trail to the newest position
        record(&root, "T2", &["p.py".into()], "solved").expect("ok");
        record(&root, "T1", &["y.py".into()], "solved").expect("ok");
        let ids: Vec<String> = load_entries(&root).into_iter().map(|e| e.id).collect();
        assert_eq!(ids, vec!["T2", "T1"]);
        let (hot, dead) = summarize(&root);
        // y.py is the newest solve (1/2) and p.py the older one (1/3)
        assert_eq!(hot, vec!["y.py", "p.py"]);
        assert!(dead.is_empty());
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn record_rejects_bad_outcome_and_empty_id() {
        let root = scratch("reject");
        assert!(record(&root, "T", &["a.py".into()], "meh").is_err());
        assert!(record(&root, "  ", &["a.py".into()], "solved").is_err());
        assert!(load_entries(&root).is_empty());
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn summarize_recency_weighting_is_exact() {
        let root = scratch("recency");
        record(&root, "t1", &["a.py".into(), "b.py".into()], "solved").expect("ok");
        record(&root, "t2", &["a.py".into()], "solved").expect("ok");
        // a.py: 1/3 + 1/2 > b.py: 1/3
        assert_eq!(summarize(&root).0, vec!["a.py", "b.py"]);
        record(&root, "t3", &["b.py".into()], "solved").expect("ok");
        // b.py: 1/4 + 1/2 > a.py: 1/4 + 1/3 (exact: 3/4 > 7/12)
        assert_eq!(summarize(&root).0, vec!["b.py", "a.py"]);
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn summarize_tie_break_and_hot_cap() {
        let root = scratch("ties");
        record(&root, "t1", &["z.py".into(), "a.py".into()], "solved").expect("ok");
        assert_eq!(summarize(&root).0, vec!["a.py", "z.py"]);
        for i in 0..8 {
            record(&root, &format!("s{i}"), &[format!("hot{i}.py")], "solved").expect("ok");
        }
        let (hot, _dead) = summarize(&root);
        assert_eq!(
            hot,
            ["hot7.py", "hot6.py", "hot5.py", "hot4.py", "hot3.py", "hot2.py"]
        );
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn truncation_keeps_last_32_entries() {
        let root = scratch("evaporate");
        for i in 0..40 {
            record(
                &root,
                &format!("t{i:02}"),
                &[format!("f{i:02}.py")],
                "solved",
            )
            .expect("ok");
        }
        let entries = load_entries(&root);
        assert_eq!(entries.len(), MAX_ENTRIES);
        assert_eq!(entries[0].id, "t08");
        assert_eq!(entries.last().expect("non-empty").id, "t39");
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn dead_ends_are_files_only_in_failed_entries() {
        let root = scratch("deadends");
        // re-recording the same id flips its outcome: p.py is never solved
        record(&root, "T2", &["p.py".into()], "solved").expect("ok");
        record(&root, "T2", &["p.py".into()], "failed").expect("ok");
        record(&root, "T4", &["q.py".into()], "failed").expect("ok");
        record(&root, "T5", &["r.py".into(), "s.py".into()], "failed").expect("ok");
        let (hot, dead) = summarize(&root);
        assert!(hot.is_empty(), "no solved entries remain: {hot:?}");
        // most-recent failed first (T5 → pos 1, T4 → pos 2, T2 → pos 3),
        // ties by path (r.py < s.py within T5)
        assert_eq!(dead, vec!["r.py", "s.py", "q.py", "p.py"]);
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn section_items_format_and_empty_store() {
        let root = scratch("sections");
        assert!(section_items(&root).is_empty(), "missing store → no items");
        record(&root, "t1", &["a.py".into(), "b.py".into()], "solved").expect("ok");
        record(&root, "t2", &["a.py".into()], "solved").expect("ok");
        record(&root, "t3", &["c.py".into()], "failed").expect("ok");
        let items = section_items(&root);
        assert_eq!(
            items,
            vec![
                "- a.py — well-trodden (2 solved tasks)".to_string(),
                "- b.py — well-trodden (1 solved tasks)".to_string(),
                "DEAD ENDS (avoid blind retries):".to_string(),
                "- c.py — dead end (failed tasks)".to_string(),
            ]
        );
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn load_entries_skips_blank_malformed_and_misshapen_lines() {
        let root = scratch("heal");
        std::fs::create_dir_all(Path::new(&root).join(".trailmark")).expect("mkdir");
        std::fs::write(
            trails_path(&root),
            concat!(
                "\n",
                "not json\n",
                "[1, 2]\n",
                "{\"id\": \"\", \"outcome\": \"solved\", \"files\": []}\n",
                "{\"id\": \"ok\", \"outcome\": \"meh\", \"files\": []}\n",
                "{\"id\": \"ok\", \"outcome\": \"solved\", \"files\": [1]}\n",
                "{\"id\": \"ok\", \"outcome\": \"solved\", \"files\": [\"a.py\", \"\", \"b.py\"]}\n",
            ),
        )
        .expect("write store");
        let entries = load_entries(&root);
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].files, vec!["a.py", "b.py"]);
        // a record rewrites the store, healing the damaged lines
        record(&root, "fresh", &["x.py".into()], "solved").expect("ok");
        assert_eq!(load_entries(&root).len(), 2);
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn clear_removes_store_and_reports_existance() {
        let root = scratch("clear");
        assert!(!clear(&root), "nothing to clear");
        record(&root, "t1", &["a.py".into()], "solved").expect("ok");
        assert!(clear(&root));
        assert!(!trails_path(&root).exists());
        assert!(!clear(&root));
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn json_string_matches_python_ensure_ascii() {
        assert_eq!(json_string("plain"), "\"plain\"");
        assert_eq!(json_string("a\"b\\c"), "\"a\\\"b\\\\c\"");
        assert_eq!(json_string("tab\there\n"), "\"tab\\there\\n\"");
        // non-ASCII → \u escapes; astral → surrogate pairs
        assert_eq!(json_string("é"), "\"\\u00e9\"");
        assert_eq!(json_string("—"), "\"\\u2014\"");
        assert_eq!(json_string("\u{7f}"), "\"\\u007f\"");
        assert_eq!(json_string("\u{1f600}"), "\"\\ud83d\\ude00\"");
        assert_eq!(json_string("\u{1}"), "\"\\u0001\"");
    }

    #[test]
    fn fraction_exact_ordering_beats_floats() {
        // 1/3 + 1/4 (7/12) vs 1/2 + 1/9 (11/18): exact math says 21/36 vs 22/36
        let a = Fraction::new(1, 3).add(Fraction::new(1, 4));
        let b = Fraction::new(1, 2).add(Fraction::new(1, 9));
        assert_eq!(a.cmp_frac(&b), std::cmp::Ordering::Less);
        // reduced form
        let r = Fraction::new(2, 4);
        assert_eq!((r.num, r.den), (1, 2));
        // deep chains stay exact (sum of 1/2..1/33 vs itself)
        let mut sum = Fraction::ZERO;
        for k in 2..=33 {
            sum = sum.add(Fraction::new(1, i128::from(k)));
        }
        let mut other = Fraction::ZERO;
        for k in (2..=33).rev() {
            other = other.add(Fraction::new(1, i128::from(k)));
        }
        assert_eq!(sum.cmp_frac(&other), std::cmp::Ordering::Equal);
    }

    #[test]
    fn cli_payload_json_matches_python_sort_keys() {
        let entry = Entry {
            id: "T1".into(),
            outcome: "solved".into(),
            files: vec!["a.py".into(), "b.py".into()],
        };
        assert_eq!(
            record_payload_json(&entry, 1),
            "{\"entries\": 1, \"files\": [\"a.py\", \"b.py\"], \"id\": \"T1\", \
             \"outcome\": \"solved\", \"recorded\": true}"
        );
        assert_eq!(
            clear_payload_json(true),
            "{\"cleared\": true, \"removed\": true}"
        );
    }
}
