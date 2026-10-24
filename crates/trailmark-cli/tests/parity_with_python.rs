//! `parity_with_python`: builds a small synthetic repo (py + ts + test files,
//! pytest.ini), generates the AGENTS.md block with BOTH the Python reference
//! pipeline (via tools/dump_block.py) and this Rust implementation for the same
//! issue + cli=generic + budget=2048, and asserts the bodies are byte-identical.
//!
//! The test is gated at runtime: when `uv run python -c "import trailmark"`
//! fails (no Python toolchain available), it prints a notice and returns —
//! the rest of the suite still runs.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

use trailmark_cli::atlas::{assemble, assemble_with_leads};
use trailmark_cli::probes::probe_script;
use trailmark_core::{update, IndexCache};

const ISSUE: &str = "fix the session token refresh expiry handling in the client";
const CLI: &str = "generic";
const BUDGET: usize = 2048;

/// The synthetic repo: (relative path, content).
const REPO_FILES: &[(&str, &str)] = &[
    ("pytest.ini", "[pytest]\ntestpaths = tests\n"),
    (".gitignore", "dist/\n*.log\n"),
    (
        "core/api.py",
        r#""""Shared hot API module."""
from app.util import normalize


class Handler:
    """Request handler base."""

    def handle(self, request):
        """Handle an incoming request payload."""
        return normalize(request)


def shared_api(x):
    """Compute the shared api value."""
    return x + 1
"#,
    ),
    (
        "app/util.py",
        r#""""Utility helpers for the app package."""


def normalize(value):
    """Normalize a raw value into canonical form."""
    return value.strip().lower()


def format_key(key):
    """Format an identifier key."""
    return normalize(key).replace(" ", "_")
"#,
    ),
    (
        "app/service.py",
        r#""""Session service: refresh and expiry handling."""
from .util import format_key
from core.api import Handler


class SessionService(Handler):
    """Manages session tokens and their expiry."""

    def refresh(self, token, ttl=3600):
        """Refresh a session token before its expiry deadline."""
        return {"token": token, "ttl": ttl, "key": format_key(token)}

    def expire(self, token):
        """Drop an expired session token from the store."""
        return None
"#,
    ),
    (
        "web/client.ts",
        r##"/**
 * HTTP client helpers.
 */
import { mountApp } from "./index";

export interface ClientConfig {
  baseUrl: string;
  timeout: number;
}

export function fetchJson(config: ClientConfig, path: string): Promise<unknown> {
  /** Fetch JSON from the configured base url. */
  const url = config.baseUrl + path;
  return fetch(url).then((r) => r.json());
}

export class ApiClient {
  /** Typed client around fetchJson. */
  constructor(public config: ClientConfig) {}

  async get(path: string): Promise<unknown> {
    return fetchJson(this.config, path);
  }
}

export const boot = () => mountApp("#app");
"##,
    ),
    (
        "web/index.ts",
        r#"/** Application entrypoint. */
import { ApiClient } from "./client";

export function mountApp(selector: string): void {
  /** Mount the app at the given selector. */
  const el = document.querySelector(selector);
  if (el) el.textContent = "ready";
}

export const defaultClient = new ApiClient({ baseUrl: "/", timeout: 1000 });
"#,
    ),
    (
        "tests/test_service.py",
        r#""""Tests for the session service."""
import pytest

from app.service import SessionService


def test_refresh_token():
    """Refresh extends the token ttl."""
    svc = SessionService()
    out = svc.refresh("abc", ttl=60)
    assert out["ttl"] == 60


def test_expire_token():
    """Expired tokens are dropped."""
    svc = SessionService()
    assert svc.expire("abc") is None
"#,
    ),
    (
        "tests/client.test.ts",
        r#"/** Client tests. */
import { ApiClient, fetchJson } from "../web/client";

test("fetchJson builds the url", () => {
  const cfg = { baseUrl: "/", timeout: 5 };
  expect(cfg.baseUrl).toBe("/");
});
"#,
    ),
];

/// The optional user profile written into `.trailmark/profile.txt` for the
/// profile parity fixture: exercises fact/rule parsing, `#` comments, ignored
/// lines, whitespace stripping, bare `fact:`/`rule:` prefixes, and both caps
/// (8 facts / 6 rules). One rule is long enough to wrap at 76 bytes.
const PROFILE_TXT: &str = "\
# cluster profile — comments and everything else are ignored
fact: partitions: gpu(a100, 24h), cpu; submit with sbatch -A acct
rule:   check sacct once per job; never poll squeue in a loop
note: neither fact nor rule — ignored
fact:
rule:
fact: scratch is /scratch/$USER and is purged every 30 days
fact: the gpu queue drains at midnight sharp
fact: python on login nodes is 3.12 with numpy preloaded
fact: datasets live under /datasets, read-only
fact: node local scratch is /tmp/scratch, wiped on reboot
fact: srun rejects jobs without --account
fact: the shared filesystem quotas reset on the 1st
fact: ninth fact — beyond the cap, must be dropped
rule: prefer srun over interactive shells on login nodes
rule: never commit .trailmark/ or AGENTS.md
rule: long rule that must wrap: when a delegated exploration returns a summary, keep its file:line references so the same code is never retrieved twice
rule: r4
rule: r5
rule: r6
rule: seventh rule — beyond the cap, must be dropped
";

fn workspace_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .expect("workspace root exists")
}

/// Locate `uv` (the task environment exposes it at ~/.local/bin/uv).
fn uv_path() -> Option<PathBuf> {
    if let Ok(uv) = std::env::var("UV") {
        let p = PathBuf::from(uv);
        if p.exists() {
            return Some(p);
        }
    }
    let home = std::env::var_os("HOME").map(PathBuf::from)?;
    let candidate = home.join(".local/bin/uv");
    if candidate.exists() {
        return Some(candidate);
    }
    which("uv")
}

fn which(program: &str) -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    for dir in std::env::split_paths(&path) {
        let candidate = dir.join(program);
        if candidate.is_file() {
            return Some(candidate);
        }
    }
    None
}

fn python_available(uv: &Path) -> bool {
    Command::new(uv)
        .args(["run", "python", "-c", "import trailmark"])
        .current_dir(workspace_root())
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
}

/// Write the synthetic repo into `root` (byte-identical content every time).
fn build_repo(root: &Path) {
    for (rel, content) in REPO_FILES {
        let path = root.join(rel);
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).expect("mkdir for repo file");
        }
        fs::write(path, content).expect("write repo file");
    }
}

fn unique_dir(tag: &str) -> PathBuf {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let dir = std::env::temp_dir().join(format!("cg_parity_{tag}_{}_{nanos}", std::process::id()));
    fs::create_dir_all(&dir).expect("parity scratch dir");
    dir
}

/// Run `tools/dump_block.py` with `args` and return the block body it printed.
fn run_dump(uv: &Path, args: &[&str]) -> String {
    let dump = workspace_root().join("tools/dump_block.py");
    let mut cmd: Vec<&str> = vec!["run", "python", dump.to_str().expect("dump path")];
    cmd.extend_from_slice(args);
    let out = Command::new(uv)
        .args(&cmd)
        .current_dir(workspace_root())
        .output()
        .expect("run dump_block.py");
    assert!(
        out.status.success(),
        "dump_block.py failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8(out.stdout).expect("python body is utf-8")
}

/// The Rust pipeline for one sandbox: incremental index build, then the
/// assembler (`with_leads` mirrors `trailmark focus`'s leads path).
fn rust_block(repo: &Path, issue: Option<&str>, budget: usize, with_leads: bool) -> String {
    let langs = trailmark_core::default_langs();
    let cache = IndexCache::new(&repo.join(".trailmark"));
    let data = update(
        repo.to_str().expect("repo path"),
        Some(&langs),
        0,
        Some(&cache),
    );
    if with_leads {
        let (leads, prior) =
            trailmark_cli::focus::rank_and_prior(&data, &cache, issue.expect("issue"), 5);
        let budget = trailmark_cli::focus::effective_budget_for(budget, leads.len());
        assemble_with_leads(&data, issue, CLI, budget, Some(&leads), Some(&prior))
    } else {
        assemble(&data, issue, CLI, budget)
    }
}

/// Fail with both bodies written next to each other for easy diffing.
fn assert_bodies_equal(tag: &str, body_py: &str, body_rs: &str, base: &Path) {
    if body_py != body_rs {
        let py_path = base.join(format!("body_py_{tag}.txt"));
        let rs_path = base.join(format!("body_rs_{tag}.txt"));
        fs::write(&py_path, body_py).expect("write py body");
        fs::write(&rs_path, body_rs).expect("write rs body");
        panic!(
            "bodies differ ({tag})\nfirst difference at byte {} / line {}\n\
             python body: {}\nrust body:   {}",
            body_py
                .bytes()
                .zip(body_rs.bytes())
                .position(|(a, b)| a != b)
                .unwrap_or(body_py.len().max(body_rs.len())),
            body_py
                .lines()
                .zip(body_rs.lines())
                .position(|(a, b)| a != b)
                .map(|i| i + 1)
                .unwrap_or(0),
            py_path.display(),
            rs_path.display(),
        );
    }
    assert_eq!(body_py, body_rs);
}

#[test]
fn parity_with_python() {
    let Some(uv) = uv_path() else {
        eprintln!("parity_with_python: SKIPPED — uv not found on this machine");
        return;
    };
    if !python_available(&uv) {
        eprintln!("parity_with_python: SKIPPED — `uv run python -c \"import trailmark\"` failed");
        return;
    }

    let base = unique_dir("base");
    let repo_py = base.join("repo_py");
    let repo_rs = base.join("repo_rs");
    build_repo(&repo_py);
    build_repo(&repo_rs);

    // --- Python reference pipeline ----------------------------------------
    let dump = workspace_root().join("tools/dump_block.py");
    let out = Command::new(&uv)
        .args([
            "run",
            "python",
            dump.to_str().unwrap(),
            "--root",
            repo_py.to_str().unwrap(),
            "--issue",
            ISSUE,
            "--cli",
            CLI,
            "--budget",
            &BUDGET.to_string(),
        ])
        .current_dir(workspace_root())
        .output()
        .expect("run dump_block.py");
    assert!(
        out.status.success(),
        "dump_block.py failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    let body_py = String::from_utf8(out.stdout).expect("python body is utf-8");

    // --- Rust pipeline -----------------------------------------------------
    let langs = trailmark_core::default_langs();
    let cache = IndexCache::new(&repo_rs.join(".trailmark"));
    let data = update(repo_rs.to_str().unwrap(), Some(&langs), 0, Some(&cache));
    let body_rs = assemble(&data, Some(ISSUE), CLI, BUDGET);

    // --- compare ------------------------------------------------------------
    if body_py != body_rs {
        let py_path = base.join("body_py.txt");
        let rs_path = base.join("body_rs.txt");
        fs::write(&py_path, &body_py).unwrap();
        fs::write(&rs_path, &body_rs).unwrap();
        panic!(
            "block bodies differ (python {} bytes, rust {} bytes)\n\
             python body: {}\n\
             rust body:   {}\n\
             first difference at byte {} / line {}",
            body_py.len(),
            body_rs.len(),
            py_path.display(),
            rs_path.display(),
            body_py
                .bytes()
                .zip(body_rs.bytes())
                .position(|(a, b)| a != b)
                .unwrap_or(body_py.len().max(body_rs.len())),
            body_py
                .lines()
                .zip(body_rs.lines())
                .position(|(a, b)| a != b)
                .map(|i| i + 1)
                .unwrap_or(0),
        );
    }
    assert_eq!(body_py, body_rs);
    assert!(body_py.len() <= BUDGET, "body exceeds budget");
    assert!(body_py.contains("PLAN"), "issue path must include PLAN");
    fs::remove_dir_all(&base).ok();
}

#[test]
fn parity_with_python_no_issue_atlas_only() {
    let Some(uv) = uv_path() else {
        eprintln!("parity_with_python_no_issue: SKIPPED — uv not found");
        return;
    };
    if !python_available(&uv) {
        eprintln!("parity_with_python_no_issue: SKIPPED — python unavailable");
        return;
    }
    let base = unique_dir("noissue");
    let repo_py = base.join("repo_py");
    let repo_rs = base.join("repo_rs");
    build_repo(&repo_py);
    build_repo(&repo_rs);

    let dump = workspace_root().join("tools/dump_block.py");
    let out = Command::new(&uv)
        .args([
            "run",
            "python",
            dump.to_str().unwrap(),
            "--root",
            repo_py.to_str().unwrap(),
            "--cli",
            CLI,
            "--budget",
            &BUDGET.to_string(),
        ])
        .current_dir(workspace_root())
        .output()
        .expect("run dump_block.py");
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let body_py = String::from_utf8(out.stdout).expect("python body is utf-8");

    let langs = trailmark_core::default_langs();
    let cache = IndexCache::new(&repo_rs.join(".trailmark"));
    let data = update(repo_rs.to_str().unwrap(), Some(&langs), 0, Some(&cache));
    let body_rs = assemble(&data, None, CLI, BUDGET);

    if body_py != body_rs {
        let py_path = base.join("body_py.txt");
        let rs_path = base.join("body_rs.txt");
        fs::write(&py_path, &body_py).unwrap();
        fs::write(&rs_path, &body_rs).unwrap();
        panic!(
            "atlas-only bodies differ\npython body: {}\nrust body: {}",
            py_path.display(),
            rs_path.display()
        );
    }
    fs::remove_dir_all(&base).ok();
}

/// Profile parity: a fixture repo WITH `.trailmark/profile.txt` + an issue.
/// The profile contributes a LAB facts section (drop rank 5 — after GLOSSARY,
/// before PLAN) and extra RULES lines wrapped with the base-rule fmt; both
/// pipelines must produce byte-identical bodies.
#[test]
fn parity_with_python_profile_lab_and_extra_rules() {
    let Some(uv) = uv_path() else {
        eprintln!("parity_with_python_profile: SKIPPED — uv not found");
        return;
    };
    if !python_available(&uv) {
        eprintln!("parity_with_python_profile: SKIPPED — python unavailable");
        return;
    }
    let base = unique_dir("profile");
    let repo_py = base.join("repo_py");
    let repo_rs = base.join("repo_rs");
    build_repo(&repo_py);
    build_repo(&repo_rs);
    for repo in [&repo_py, &repo_rs] {
        let cg = repo.join(".trailmark");
        fs::create_dir_all(&cg).expect("mkdir .trailmark");
        fs::write(cg.join("profile.txt"), PROFILE_TXT).expect("write profile.txt");
    }

    // At a generous budget every section survives; at 2048 the packer drops
    // GLOSSARY/LAB/PLAN variants in rank order — both sides must agree either
    // way, byte for byte.
    for budget in [4096usize, 2048] {
        let dump = workspace_root().join("tools/dump_block.py");
        let out = Command::new(&uv)
            .args([
                "run",
                "python",
                dump.to_str().unwrap(),
                "--root",
                repo_py.to_str().unwrap(),
                "--issue",
                ISSUE,
                "--cli",
                CLI,
                "--budget",
                &budget.to_string(),
            ])
            .current_dir(workspace_root())
            .output()
            .expect("run dump_block.py");
        assert!(
            out.status.success(),
            "dump_block.py failed: {}",
            String::from_utf8_lossy(&out.stderr)
        );
        let body_py = String::from_utf8(out.stdout).expect("python body is utf-8");

        let langs = trailmark_core::default_langs();
        let cache = IndexCache::new(&repo_rs.join(".trailmark"));
        let data = update(repo_rs.to_str().unwrap(), Some(&langs), 0, Some(&cache));
        let body_rs = assemble(&data, Some(ISSUE), CLI, budget);

        if body_py != body_rs {
            let py_path = base.join(format!("body_py_{budget}.txt"));
            let rs_path = base.join(format!("body_rs_{budget}.txt"));
            fs::write(&py_path, &body_py).unwrap();
            fs::write(&rs_path, &body_rs).unwrap();
            panic!(
                "profile bodies differ at budget {budget}\npython body: {}\nrust body:   {}",
                py_path.display(),
                rs_path.display()
            );
        }
        assert_eq!(body_py, body_rs);
        assert!(body_py.len() <= budget, "body exceeds budget {budget}");
        if budget >= 4096 {
            // every profile feature is visible in the assembled block
            assert!(body_py.contains("LAB"), "LAB section missing:\n{body_py}");
            assert!(
                body_py.contains("- partitions: gpu(a100, 24h), cpu; submit with sbatch -A acct")
            );
            assert!(
                !body_py.contains("ninth fact"),
                "facts beyond the cap must not leak:\n{body_py}"
            );
            assert!(body_py.contains("- check sacct once per job"));
            assert!(
                body_py.contains("same code is never retrieved twice"),
                "wrapped extra rule must survive:\n{body_py}"
            );
            assert!(
                !body_py.contains("seventh rule"),
                "rules beyond the cap must not leak:\n{body_py}"
            );
            assert!(
                !body_py.contains("neither fact nor rule"),
                "ignored lines must not leak:\n{body_py}"
            );
        }
    }
    fs::remove_dir_all(&base).ok();
}

/// Negative control for the profile fixture: with no `.trailmark/profile.txt`
/// the LAB section must be absent (profile reads are optional, never fatal).
#[test]
fn rust_profile_absent_means_no_lab_section() {
    let base = unique_dir("noprofile");
    let repo_rs = base.join("repo_rs");
    build_repo(&repo_rs);
    let langs = trailmark_core::default_langs();
    let cache = IndexCache::new(&repo_rs.join(".trailmark"));
    let data = update(repo_rs.to_str().unwrap(), Some(&langs), 0, Some(&cache));
    let body = assemble(&data, Some(ISSUE), CLI, 4096);
    assert!(
        !body.contains("LAB"),
        "no profile must mean no LAB:\n{body}"
    );
    fs::remove_dir_all(&base).ok();
}

// ---------------------------------------------------------------------------
// leads path: PLAN from ranker leads + CONTRACTS + SNIPPETS + CLEARANCE
// ---------------------------------------------------------------------------

/// Leads parity (dump_block.py `--leads`): both pipelines rank the same leads
/// with the same ranker algorithm, format PLAN from them (`_plan_from_leads`),
/// and add the CONTRACTS (signature linemap, 280B/file) and SNIPPETS (lead
/// body, 1400B/lead, truncation marker) sections. The budget also goes through
/// `effective_budget_for` (2048 → 2048 + 2200·3 = 8648 with 3+ leads).
#[test]
fn parity_with_python_leads_contracts_and_snippets() {
    let Some(uv) = uv_path() else {
        eprintln!("parity_with_python_leads: SKIPPED — uv not found");
        return;
    };
    if !python_available(&uv) {
        eprintln!("parity_with_python_leads: SKIPPED — python unavailable");
        return;
    }
    let base = unique_dir("leads");
    let repo_py = base.join("repo_py");
    let repo_rs = base.join("repo_rs");
    build_repo(&repo_py);
    build_repo(&repo_rs);

    let body_py = run_dump(
        &uv,
        &[
            "--root",
            repo_py.to_str().unwrap(),
            "--issue",
            ISSUE,
            "--cli",
            CLI,
            "--budget",
            &BUDGET.to_string(),
            "--leads",
        ],
    );
    let body_rs = rust_block(&repo_rs, Some(ISSUE), BUDGET, true);

    assert_bodies_equal("leads", &body_py, &body_rs, &base);
    // the effective budget (2048 + 2200·min(3, n_leads)) with leads present
    assert!(body_py.len() > 2048, "effective budget must allow headroom");
    assert!(
        body_py.contains("CONTRACTS"),
        "CONTRACTS section missing:\n{body_py}"
    );
    assert!(
        body_py.contains("SNIPPETS"),
        "SNIPPETS section missing:\n{body_py}"
    );
    assert!(
        body_py.contains("lines):"),
        "contract header format missing:\n{body_py}"
    );
    assert!(
        body_py.contains("  … (truncated") || body_py.contains("L"),
        "snippet line anchors missing:\n{body_py}"
    );
    assert!(body_py.contains("OUTLINES: .trailmark/outlines/"));
    fs::remove_dir_all(&base).ok();
}

/// Clearance parity: with extra top-level dirs that no lead touches, the
/// CLEARANCE manifest (3 lowest-pagerank non-lead dirs) must be byte-identical.
#[test]
fn parity_with_python_clearance_manifest() {
    const EXTRA_FILES: &[(&str, &str)] = &[
        (
            "docs/guide.py",
            "\"\"\"Usage guide helpers.\"\"\"\n\n\ndef overview(topic):\n    \"\"\"Return the overview text for a topic.\"\"\"\n    return topic + 1\n",
        ),
        (
            "scripts/gen.py",
            "\"\"\"Artifact generation helpers.\"\"\"\n\n\ndef run_gen(count):\n    \"\"\"Generate the artifact set.\"\"\"\n    return count * 2\n",
        ),
        (
            "misc/notes.py",
            "\"\"\"Miscellaneous notes helpers.\"\"\"\n\n\ndef tidy(text):\n    \"\"\"Tidy up a note body.\"\"\"\n    return text.strip()\n",
        ),
    ];
    let Some(uv) = uv_path() else {
        eprintln!("parity_with_python_clearance: SKIPPED — uv not found");
        return;
    };
    if !python_available(&uv) {
        eprintln!("parity_with_python_clearance: SKIPPED — python unavailable");
        return;
    }
    let base = unique_dir("clearance");
    let repo_py = base.join("repo_py");
    let repo_rs = base.join("repo_rs");
    for repo in [&repo_py, &repo_rs] {
        build_repo(repo);
        for (rel, content) in EXTRA_FILES {
            let path = repo.join(rel);
            fs::create_dir_all(path.parent().unwrap()).expect("mkdir extra dir");
            fs::write(path, content).expect("write extra file");
        }
    }

    let body_py = run_dump(
        &uv,
        &[
            "--root",
            repo_py.to_str().unwrap(),
            "--issue",
            ISSUE,
            "--cli",
            CLI,
            "--budget",
            "8192",
            "--leads",
        ],
    );
    let body_rs = rust_block(&repo_rs, Some(ISSUE), 8192, true);

    assert_bodies_equal("clearance", &body_py, &body_rs, &base);
    assert!(
        body_py.contains("CLEARANCE"),
        "CLEARANCE section missing:\n{body_py}"
    );
    assert!(
        body_py.contains(
            "CLEARED (ranked irrelevant for this task — do not explore without a stated reason):"
        ),
        "clearance manifest format missing:\n{body_py}"
    );
    fs::remove_dir_all(&base).ok();
}

// ---------------------------------------------------------------------------
// trails: store byte-parity via both CLIs + TRAILS section byte-parity
// ---------------------------------------------------------------------------

/// Trails parity: the same `record` commands are issued through
/// `python -m trailmark.trails` and the Rust `trailmark trails` subcommand —
/// the resulting `trails.jsonl` store AND the record `--json` payloads must be
/// byte-identical. Then the TRAILS block section (rank 7, recency-weighted hot
/// files with exact-rational ordering + dead ends) must be byte-identical in
/// the assembled body.
#[test]
fn parity_with_python_trails_store_and_section() {
    let Some(uv) = uv_path() else {
        eprintln!("parity_with_python_trails: SKIPPED — uv not found");
        return;
    };
    if !python_available(&uv) {
        eprintln!("parity_with_python_trails: SKIPPED — python unavailable");
        return;
    }
    let bin = PathBuf::from(env!("CARGO_BIN_EXE_trailmark"));
    let base = unique_dir("trails");
    let repo_py = base.join("repo_py");
    let repo_rs = base.join("repo_rs");
    build_repo(&repo_py);
    build_repo(&repo_rs);

    // (id, files, outcome) — mixed outcomes and recency so the exact-rational
    // hot ordering and the dead-end rule are both exercised.
    let records: &[(&str, &str, &str)] = &[
        ("t1", "core/api.py,app/util.py,web/client.ts", "solved"),
        ("t2", "app/util.py", "solved"),
        ("t3", "web/client.ts", "failed"),
        ("t4", "app/service.py,core/api.py", "solved"),
        ("t5", "web/index.ts", "failed"),
    ];
    for (id, files, outcome) in records {
        let py = Command::new(&uv)
            .args([
                "run",
                "python",
                "-m",
                "trailmark.trails",
                "record",
                "--root",
                repo_py.to_str().unwrap(),
                "--id",
                id,
                "--files",
                files,
                "--outcome",
                outcome,
                "--json",
            ])
            .current_dir(workspace_root())
            .output()
            .expect("python trails record");
        assert!(
            py.status.success(),
            "{}",
            String::from_utf8_lossy(&py.stderr)
        );
        let rs = Command::new(&bin)
            .args([
                "--root",
                repo_rs.to_str().unwrap(),
                "trails",
                "record",
                "--id",
                id,
                "--files",
                files,
                "--outcome",
                outcome,
                "--json",
            ])
            .output()
            .expect("rust trails record");
        assert!(
            rs.status.success(),
            "{}",
            String::from_utf8_lossy(&rs.stderr)
        );
        // the machine-readable payload must be byte-identical
        assert_eq!(
            String::from_utf8(py.stdout).expect("py json"),
            String::from_utf8(rs.stdout).expect("rs json"),
            "record --json payload mismatch for {id}"
        );
    }

    // the stores must be byte-identical (json.dumps sort_keys byte format)
    let store_py = fs::read(repo_py.join(".trailmark/trails.jsonl")).expect("py store");
    let store_rs = fs::read(repo_rs.join(".trailmark/trails.jsonl")).expect("rs store");
    assert_eq!(store_py, store_rs, "trails.jsonl bytes differ");

    // ...and the TRAILS section must be byte-identical in the assembled body
    let body_py = run_dump(
        &uv,
        &[
            "--root",
            repo_py.to_str().unwrap(),
            "--cli",
            CLI,
            "--budget",
            "4096",
        ],
    );
    let body_rs = rust_block(&repo_rs, None, 4096, false);
    assert_bodies_equal("trails", &body_py, &body_rs, &base);
    assert!(
        body_py.contains("TRAILS"),
        "TRAILS section missing:\n{body_py}"
    );
    assert!(body_py.contains("- core/api.py — well-trodden (2 solved tasks)"));
    assert!(body_py.contains("- app/service.py — well-trodden (1 solved tasks)"));
    assert!(body_py.contains("- app/util.py — well-trodden (2 solved tasks)"));
    assert!(body_py.contains("DEAD ENDS (avoid blind retries):"));
    assert!(body_py.contains("- web/index.ts — dead end (failed tasks)"));

    // clear parity (after the body comparisons — clear removes the store)
    let py = Command::new(&uv)
        .args([
            "run",
            "python",
            "-m",
            "trailmark.trails",
            "clear",
            "--root",
            repo_py.to_str().unwrap(),
            "--json",
        ])
        .current_dir(workspace_root())
        .output()
        .expect("python trails clear");
    let rs = Command::new(&bin)
        .args([
            "--root",
            repo_rs.to_str().unwrap(),
            "trails",
            "clear",
            "--json",
        ])
        .output()
        .expect("rust trails clear");
    assert_eq!(
        String::from_utf8(py.stdout).expect("py json"),
        String::from_utf8(rs.stdout).expect("rs json")
    );
    assert!(!repo_py.join(".trailmark/trails.jsonl").exists());
    assert!(!repo_rs.join(".trailmark/trails.jsonl").exists());
    fs::remove_dir_all(&base).ok();
}

// ---------------------------------------------------------------------------
// probes: verify.sh script text parity + focus-time write
// ---------------------------------------------------------------------------

/// The Rust `probe_script` must be byte-identical to the Python reference's
/// `trailmark.probes.probe_script` for the runner command the fixture repo
/// detects (pytest.ini → PYTEST_RUNNER), with and without a target.
#[test]
fn parity_probe_script_text() {
    let Some(uv) = uv_path() else {
        eprintln!("parity_probe_script_text: SKIPPED — uv not found");
        return;
    };
    if !python_available(&uv) {
        eprintln!("parity_probe_script_text: SKIPPED — python unavailable");
        return;
    }
    let runner = "python -m pytest -x -q --tb=line 2>&1 | tail -40";
    for target in ["app/service.py", ""] {
        let out = Command::new(&uv)
            .args([
                "run",
                "python",
                "-c",
                &format!(
                    "import sys; from trailmark.probes import probe_script; \
                     sys.stdout.write(probe_script({runner:?}, {target:?}))"
                ),
            ])
            .current_dir(workspace_root())
            .output()
            .expect("python probe_script");
        assert!(
            out.status.success(),
            "{}",
            String::from_utf8_lossy(&out.stderr)
        );
        let py = String::from_utf8(out.stdout).expect("py script utf-8");
        let rs = probe_script(runner, target);
        assert_eq!(
            py, rs,
            "probe script bytes differ (target={target:?})\npy: {py:?}\nrs: {rs:?}"
        );
    }
}

/// Rust-only end-to-end: `trailmark focus` writes `.trailmark/probes/verify.sh`
/// with the exact probe_script bytes for the detected runner, target = the top
/// lead's path, mode 0755 (mirrors the cli_focus probe flow).
#[test]
fn rust_focus_writes_verify_probe() {
    let bin = PathBuf::from(env!("CARGO_BIN_EXE_trailmark"));
    let base = unique_dir("probe_e2e");
    let repo = base.join("repo");
    build_repo(&repo); // fixture contains pytest.ini → PYTEST_RUNNER detected
    let root_arg = format!("--root={}", repo.display());
    let init = Command::new(&bin)
        .args([&root_arg, "init"])
        .output()
        .expect("init");
    assert!(
        init.status.success(),
        "{}",
        String::from_utf8_lossy(&init.stderr)
    );
    let focus = Command::new(&bin)
        .args([&root_arg, "focus", ISSUE, "--json"])
        .output()
        .expect("focus");
    assert!(
        focus.status.success(),
        "{}",
        String::from_utf8_lossy(&focus.stderr)
    );
    let report: serde_json::Value =
        serde_json::from_slice(&focus.stdout).expect("focus --json payload");
    let lead_path = report["leads"][0]["path"].as_str().expect("top lead path");

    let probe_path = repo.join(".trailmark/probes/verify.sh");
    let written = fs::read_to_string(&probe_path).expect("probe written at focus");
    assert_eq!(
        written,
        probe_script(
            "python -m pytest -x -q --tb=line 2>&1 | tail -40",
            lead_path
        ),
        "probe bytes must equal probe_script(runner, top-lead path)"
    );
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mode = fs::metadata(&probe_path)
            .expect("stat probe")
            .permissions()
            .mode();
        assert_eq!(mode & 0o777, 0o755, "probe must be executable");
    }
    fs::remove_dir_all(&base).ok();
}

// ---------------------------------------------------------------------------
// baseline snapshots: profile_cmds parity + BASELINE section parity
// ---------------------------------------------------------------------------

/// The baseline fixture profile: comments and non-baseline lines are ignored,
/// `baseline:` payloads are stripped, and the third `baseline:` line must be
/// dropped (and never run) by the MAX_COMMANDS=2 cap.
const BASELINE_PROFILE_TXT: &str = "\
# baseline profile — comments and everything else are ignored
fact: the gpu queue drains at midnight sharp
baseline:   echo hello-baseline
note: neither a fact nor a baseline — ignored
baseline: expr 2 + 2
baseline: echo should-never-run
";

/// Run the PYTHON `focus.profile_cmds.run_baselines` against `repo` and return
/// its truthiness ("written"/"none") as reported on stdout.
fn python_run_baselines(uv: &Path, repo: &Path) -> String {
    let snippet = "import sys; from trailmark.focus.profile_cmds import run_baselines; \
                   sys.stdout.write('written' if run_baselines(sys.argv[1]) else 'none')";
    let out = Command::new(uv)
        .args([
            "run",
            "python",
            "-c",
            snippet,
            repo.to_str().expect("repo path"),
        ])
        .current_dir(workspace_root())
        .output()
        .expect("python run_baselines");
    assert!(
        out.status.success(),
        "python run_baselines failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8_lossy(&out.stdout).trim().to_string()
}

/// Baseline snapshot parity: a fixture profile with two `baseline:` commands
/// (echo + arithmetic) must make BOTH implementations write byte-identical
/// `.trailmark/baseline.txt`, and the assembled block containing the BASELINE
/// section must be byte-identical too.
#[test]
fn parity_with_python_baseline_snapshot_and_section() {
    let Some(uv) = uv_path() else {
        eprintln!("parity_with_python_baseline: SKIPPED — uv not found");
        return;
    };
    if !python_available(&uv) {
        eprintln!("parity_with_python_baseline: SKIPPED — python unavailable");
        return;
    }
    let base = unique_dir("baseline");
    let repo_py = base.join("repo_py");
    let repo_rs = base.join("repo_rs");
    build_repo(&repo_py);
    build_repo(&repo_rs);
    for repo in [&repo_py, &repo_rs] {
        let cg = repo.join(".trailmark");
        fs::create_dir_all(&cg).expect("mkdir .trailmark");
        fs::write(cg.join("profile.txt"), BASELINE_PROFILE_TXT).expect("write profile.txt");
    }

    // --- baseline.txt generation on both sides ------------------------------
    assert_eq!(python_run_baselines(&uv, &repo_py), "written");
    let py_bytes = fs::read(repo_py.join(".trailmark/baseline.txt")).expect("py baseline.txt");

    assert!(
        trailmark_cli::focus::run_baselines(repo_rs.to_str().unwrap()).is_some(),
        "rust run_baselines must write the file"
    );
    let rs_bytes = fs::read(repo_rs.join(".trailmark/baseline.txt")).expect("rs baseline.txt");

    assert_eq!(
        String::from_utf8_lossy(&py_bytes),
        String::from_utf8_lossy(&rs_bytes),
        "baseline.txt bytes differ"
    );
    let text = String::from_utf8_lossy(&py_bytes).into_owned();
    let lines: Vec<&str> = text.lines().collect();
    assert_eq!(
        lines[0],
        "BASELINE (captured before your changes — report the delta):"
    );
    assert_eq!(lines[1], "$ echo hello-baseline");
    assert_eq!(lines[2], "  hello-baseline");
    assert_eq!(lines[3], "  exit=0");
    assert_eq!(lines[4], "$ expr 2 + 2");
    assert_eq!(lines[5], "  4");
    assert_eq!(lines[6], "  exit=0");
    assert_eq!(lines.len(), 7, "exactly the two capped commands: {text:?}");
    assert!(
        !text.contains("should-never-run"),
        "commands beyond MAX_COMMANDS must never run or appear:\n{text}"
    );

    // --- assembled block parity with the BASELINE section -------------------
    let body_py = run_dump(
        &uv,
        &[
            "--root",
            repo_py.to_str().unwrap(),
            "--issue",
            ISSUE,
            "--cli",
            CLI,
            "--budget",
            "4096",
            "--baseline",
        ],
    );
    let body_rs = rust_block(&repo_rs, Some(ISSUE), 4096, false);

    assert_bodies_equal("baseline", &body_py, &body_rs, &base);
    assert!(
        body_py.contains("BASELINE (captured before your changes — report the delta):"),
        "BASELINE section missing from the assembled body:\n{body_py}"
    );
    assert!(body_py.contains("$ echo hello-baseline"));
    assert!(body_py.contains("$ expr 2 + 2"));
    // BASELINE (rank 6) survives at 4096; verify its position after LAB
    let lab_idx = body_py.find("\nLAB\n").expect("LAB section present");
    let baseline_idx = body_py
        .find("\nBASELINE\n")
        .expect("BASELINE section present");
    assert!(
        lab_idx < baseline_idx,
        "BASELINE must follow LAB:\n{body_py}"
    );
    fs::remove_dir_all(&base).ok();
}

/// Negative control: a profile WITHOUT `baseline:` lines must not create
/// `.trailmark/baseline.txt` in either implementation, and the assembled body
/// must contain no BASELINE section.
#[test]
fn rust_no_baseline_lines_means_no_file_and_no_section() {
    let base = unique_dir("nobaseline");
    let repo_rs = base.join("repo_rs");
    build_repo(&repo_rs);
    let cg = repo_rs.join(".trailmark");
    fs::create_dir_all(&cg).expect("mkdir .trailmark");
    fs::write(
        cg.join("profile.txt"),
        "fact: only a fact\nrule: and a rule\n",
    )
    .expect("write profile.txt");

    let root_rs = repo_rs.to_str().unwrap();
    assert!(
        trailmark_cli::focus::run_baselines(root_rs).is_none(),
        "no baseline lines must mean no run"
    );
    assert!(!repo_rs.join(".trailmark/baseline.txt").exists());
    let langs = trailmark_core::default_langs();
    let cache = IndexCache::new(&repo_rs.join(".trailmark"));
    let data = update(root_rs, Some(&langs), 0, Some(&cache));
    let body = assemble(&data, Some(ISSUE), CLI, 4096);
    assert!(
        !body.contains("BASELINE"),
        "no baseline.txt must mean no BASELINE section:\n{body}"
    );

    // Python side: run_baselines reports None and writes nothing either
    let Some(uv) = uv_path() else {
        fs::remove_dir_all(&base).ok();
        return;
    };
    if !python_available(&uv) {
        fs::remove_dir_all(&base).ok();
        return;
    }
    let repo_py = base.join("repo_py");
    build_repo(&repo_py);
    let cg = repo_py.join(".trailmark");
    fs::create_dir_all(&cg).expect("mkdir .trailmark");
    fs::write(
        cg.join("profile.txt"),
        "fact: only a fact\nrule: and a rule\n",
    )
    .expect("write profile.txt");
    assert_eq!(python_run_baselines(&uv, &repo_py), "none");
    assert!(!repo_py.join(".trailmark/baseline.txt").exists());
    fs::remove_dir_all(&base).ok();
}
