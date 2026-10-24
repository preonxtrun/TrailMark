//! TESTS section (port of `atlas/testgraph.py`): test-file detection,
//! deterministic runner detection, src→test map.

use std::collections::HashMap;
use std::path::Path;

use codegraph_core::{truncate, IndexData};

/// Exact command emitted when any pytest-style config is found.
pub const PYTEST_RUNNER: &str = "python -m pytest -x -q";
/// Placeholder when no runner can be detected from repo config.
pub const UNDETECTED: &str = "(undetected — inspect repo config)";

/// Maximum src→test rows.
pub const MAX_PAIRS: usize = 5;
/// Runner display cap (bytes).
pub const RUNNER_CAP: usize = 64;

/// True for test modules, by name or directory convention.
///
/// Detects `test_*.py` / `*_test.py`, files under `test(s)|__tests__`
/// directories, and `*.test|spec.(ts|js|tsx|jsx|mjs|cjs)` patterns.
pub fn is_test_file(path: &str) -> bool {
    let lower = path.to_lowercase();
    let parts: Vec<&str> = lower.split('/').collect();
    if parts.is_empty() {
        return false;
    }
    for seg in &parts[..parts.len() - 1] {
        if matches!(*seg, "test" | "tests" | "__tests__") {
            return true;
        }
    }
    let name = parts[parts.len() - 1];
    match name.rsplit_once('.') {
        Some((_stem, "py")) => name.starts_with("test_") || name.ends_with("_test.py"),
        Some((_stem, "js" | "jsx" | "mjs" | "cjs" | "ts" | "tsx")) => {
            name.contains(".test.") || name.contains(".spec.")
        }
        _ => false,
    }
}

/// Read at most `max_chars` characters of a file (None when unreadable).
fn read_head(base: &Path, name: &str, max_chars: usize) -> Option<String> {
    let bytes = std::fs::read(base.join(name)).ok()?;
    let text = String::from_utf8_lossy(&bytes).into_owned();
    Some(text.chars().take(max_chars).collect())
}

/// Detect the test command from repo config (deterministic priority order).
///
/// Order: pytest.ini / pyproject `[tool.pytest.ini_options]` / setup.cfg
/// `[tool:pytest]` / tox.ini `[pytest]` → `python -m pytest -x -q`; Makefile
/// `test:` target → its recipe line; `package.json` scripts.test → `npm test`;
/// `Cargo.toml` → `cargo test`; `go.mod` → `go test ./...`.
/// Returns None when nothing matches.
pub fn detect_runner(root: &str) -> Option<String> {
    let base = Path::new(root);

    if base.join("pytest.ini").is_file() {
        return Some(PYTEST_RUNNER.to_string());
    }
    if read_head(base, "pyproject.toml", 65536)
        .is_some_and(|t| t.contains("[tool.pytest.ini_options]"))
    {
        return Some(PYTEST_RUNNER.to_string());
    }
    if read_head(base, "setup.cfg", 65536).is_some_and(|t| t.contains("[tool:pytest]")) {
        return Some(PYTEST_RUNNER.to_string());
    }
    if read_head(base, "tox.ini", 65536).is_some_and(|t| t.contains("[pytest]")) {
        return Some(PYTEST_RUNNER.to_string());
    }

    let makefile =
        read_head(base, "Makefile", 65536).or_else(|| read_head(base, "makefile", 65536));
    if let Some(text) = makefile {
        if let Some(recipe) = makefile_test_recipe(&text) {
            return Some(truncate(&recipe, RUNNER_CAP));
        }
    }

    if let Some(pkg) = read_head(base, "package.json", 65536) {
        // defensive JSON parse (mirrors the Python try/except fall-through)
        if let Ok(value) = serde_json::from_str::<serde_json::Value>(&pkg) {
            if let Some(scripts) = value.get("scripts").and_then(|s| s.as_object()) {
                if scripts
                    .get("test")
                    .and_then(|t| t.as_str())
                    .is_some_and(|t| !t.trim().is_empty())
                {
                    return Some("npm test".to_string());
                }
            }
        }
    }

    if base.join("Cargo.toml").is_file() {
        return Some("cargo test".to_string());
    }
    if base.join("go.mod").is_file() {
        return Some("go test ./...".to_string());
    }
    None
}

/// `^\s*test\s*:(.*)$` — returns everything after the colon, or None.
fn make_target(line: &str) -> Option<&str> {
    let stripped = line.trim_start_matches([' ', '\t']);
    let rest = stripped.strip_prefix("test")?;
    let rest = rest.trim_start_matches([' ', '\t']);
    rest.strip_prefix(':')
}

/// Recipe line of the first `test:` target, or None (defensive parsing).
pub fn makefile_test_recipe(text: &str) -> Option<String> {
    let lines: Vec<&str> = text.lines().collect();
    for (i, line) in lines.iter().enumerate() {
        let Some(after) = make_target(line) else {
            continue;
        };
        let inline = after.trim();
        if let Some(rest) = inline.strip_prefix(';') {
            // GNU make inline recipe: `test: ; cmd`
            let cmd = rest.trim();
            if !cmd.is_empty() {
                return Some(cmd.to_string());
            }
        }
        for follow in &lines[i + 1..] {
            if follow.trim().is_empty() {
                continue;
            }
            if follow.starts_with('\t') || follow.starts_with("    ") {
                return Some(follow.trim().to_string());
            }
            break; // recipe block ended without a recipe; try later targets
        }
    }
    None
}

/// Top ≤ limit (src, test) pairs derived from resolved refs.
///
/// A test file referencing a source file creates an edge; pairs are ranked by
/// source pagerank (descending), then reference weight, then (src, test) — so
/// the order is total and deterministic even without pagerank scores.
pub fn src_test_pairs(
    data: &IndexData,
    pr: Option<&HashMap<String, f64>>,
    limit: usize,
) -> Vec<(String, String)> {
    let mut weight: HashMap<(String, String), usize> = HashMap::new();
    for r in &data.refs {
        if r.dst_file.is_empty() || r.dst_file == r.src_file {
            continue;
        }
        if !data.files.contains_key(&r.src_file) || !data.files.contains_key(&r.dst_file) {
            continue;
        }
        if !is_test_file(&r.src_file) || is_test_file(&r.dst_file) {
            continue;
        }
        *weight
            .entry((r.dst_file.clone(), r.src_file.clone()))
            .or_insert(0) += 1;
    }
    let scores = pr.cloned().unwrap_or_default();
    let mut ordered: Vec<(&(String, String), &usize)> = weight.iter().collect();
    ordered.sort_by(|(ka, wa), (kb, wb)| {
        let sa = scores.get(&ka.0).copied().unwrap_or(0.0);
        let sb = scores.get(&kb.0).copied().unwrap_or(0.0);
        sb.partial_cmp(&sa)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then(wb.cmp(wa))
            .then(ka.0.cmp(&kb.0))
            .then(ka.1.cmp(&kb.1))
    });
    ordered
        .into_iter()
        .take(limit)
        .map(|(k, _)| (k.0.clone(), k.1.clone()))
        .collect()
}

/// TESTS rows: the runner line plus up to [`MAX_PAIRS`] src→test rows.
pub fn build_tests(data: &IndexData, pr: Option<&HashMap<String, f64>>) -> Vec<String> {
    let runner = if data.root.is_empty() {
        None
    } else {
        detect_runner(&data.root)
    };
    let mut rows = vec![format!(
        "- runner: {}",
        runner.as_deref().unwrap_or(UNDETECTED)
    )];
    rows.extend(
        src_test_pairs(data, pr, MAX_PAIRS)
            .into_iter()
            .map(|(src, test)| format!("- {src} → {test}")),
    );
    rows
}

#[cfg(test)]
mod tests {
    use super::*;
    use codegraph_core::{FileEntry, Ref, Symbol};

    fn entry(path: &str, lang: &str) -> FileEntry {
        FileEntry {
            path: path.to_string(),
            lang: lang.to_string(),
            mtime: 1.0,
            size: 1,
            symbols: vec![Symbol {
                file: path.to_string(),
                name: "x".into(),
                kind: "def".into(),
                line: 1,
                end_line: 1,
                ..Default::default()
            }],
        }
    }

    #[test]
    fn test_file_detection_matches_python() {
        assert!(is_test_file("tests/test_a.py"));
        assert!(is_test_file("tests/a_test.py"));
        assert!(is_test_file("test/test_b.py"));
        assert!(is_test_file("__tests__/c.ts"));
        assert!(is_test_file("src/d.test.ts"));
        assert!(is_test_file("src/e.spec.js"));
        assert!(!is_test_file("src/testa.py"));
        assert!(!is_test_file("src/testing.py"));
        assert!(!is_test_file("src/a.py"));
        assert!(!is_test_file("app/test_utils/mod.rs"));
    }

    #[test]
    fn runner_detection_priority() {
        let dir = std::env::temp_dir().join(format!("cg_runner_{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        assert_eq!(detect_runner(dir.to_str().unwrap()), None);
        std::fs::write(dir.join("pytest.ini"), "[pytest]\n").unwrap();
        assert_eq!(
            detect_runner(dir.to_str().unwrap()).as_deref(),
            Some(PYTEST_RUNNER)
        );
        let _ = std::fs::remove_file(dir.join("pytest.ini"));
        std::fs::write(dir.join("Cargo.toml"), "[package]\n").unwrap();
        assert_eq!(
            detect_runner(dir.to_str().unwrap()).as_deref(),
            Some("cargo test")
        );
        let _ = std::fs::remove_file(dir.join("Cargo.toml"));
        std::fs::write(
            dir.join("package.json"),
            r#"{"scripts": {"test": "jest --ci"}}"#,
        )
        .unwrap();
        assert_eq!(
            detect_runner(dir.to_str().unwrap()).as_deref(),
            Some("npm test")
        );
        let _ = std::fs::remove_file(dir.join("package.json"));
        std::fs::write(dir.join("Makefile"), "test:\n\tpytest -q\n").unwrap();
        assert_eq!(
            detect_runner(dir.to_str().unwrap()).as_deref(),
            Some("pytest -q")
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn makefile_inline_and_recipe_forms() {
        assert_eq!(
            makefile_test_recipe("all:\n\techo hi\ntest: ; make unit\n"),
            Some("make unit".to_string())
        );
        assert_eq!(
            makefile_test_recipe("unit:\n\techo x\ntest: unit\n\tcargo test\n"),
            Some("cargo test".to_string())
        );
        assert_eq!(makefile_test_recipe("no target here\n"), None);
        // a test target without a recipe falls through to a later target
        assert_eq!(
            makefile_test_recipe("test:\nlint:\n\techo lint\ntest2: ; real\n"),
            None
        );
    }

    #[test]
    fn src_test_pairs_rank_by_pr_then_weight() {
        let mut files = std::collections::BTreeMap::new();
        files.insert("src/a.py".to_string(), entry("src/a.py", "py"));
        files.insert(
            "tests/test_a.py".to_string(),
            entry("tests/test_a.py", "py"),
        );
        files.insert("src/b.py".to_string(), entry("src/b.py", "py"));
        files.insert(
            "tests/test_b.py".to_string(),
            entry("tests/test_b.py", "py"),
        );
        let refs = vec![
            Ref {
                src_file: "tests/test_a.py".into(),
                dst_name: "a".into(),
                dst_file: "src/a.py".into(),
                line: 1,
            },
            Ref {
                src_file: "tests/test_a.py".into(),
                dst_name: "a".into(),
                dst_file: "src/a.py".into(),
                line: 2,
            },
            Ref {
                src_file: "tests/test_b.py".into(),
                dst_name: "b".into(),
                dst_file: "src/b.py".into(),
                line: 1,
            },
        ];
        let data = IndexData {
            root: "/r".into(),
            files,
            refs,
        };
        let mut pr = HashMap::new();
        pr.insert("src/b.py".to_string(), 0.9); // b wins despite lower weight
        let pairs = src_test_pairs(&data, Some(&pr), 5);
        assert_eq!(
            pairs[0],
            ("src/b.py".to_string(), "tests/test_b.py".to_string())
        );
        let pairs = src_test_pairs(&data, None, 5);
        assert_eq!(
            pairs[0],
            ("src/a.py".to_string(), "tests/test_a.py".to_string())
        );
    }

    #[test]
    fn build_tests_emits_runner_and_pairs() {
        let mut files = std::collections::BTreeMap::new();
        files.insert("src/a.py".to_string(), entry("src/a.py", "py"));
        files.insert(
            "tests/test_a.py".to_string(),
            entry("tests/test_a.py", "py"),
        );
        let data = IndexData {
            root: "/r".into(),
            files,
            refs: vec![Ref {
                src_file: "tests/test_a.py".into(),
                dst_name: "a".into(),
                dst_file: "src/a.py".into(),
                line: 1,
            }],
        };
        let rows = build_tests(&data, None);
        assert_eq!(rows[0], "- runner: (undetected — inspect repo config)");
        assert_eq!(rows[1], "- src/a.py → tests/test_a.py");
    }
}
