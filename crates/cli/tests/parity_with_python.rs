//! `parity_with_python`: builds a small synthetic repo (py + ts + test files,
//! pytest.ini), generates the AGENTS.md block with BOTH the Python reference
//! pipeline (via tools/dump_block.py) and this Rust implementation for the same
//! issue + cli=generic + budget=2048, and asserts the bodies are byte-identical.
//!
//! The test is gated at runtime: when `uv run python -c "import codegraph"`
//! fails (no Python toolchain available), it prints a notice and returns —
//! the rest of the suite still runs.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

use codegraph_cli::atlas::assemble;
use codegraph_core::{update, IndexCache};

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
        .args(["run", "python", "-c", "import codegraph"])
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

#[test]
fn parity_with_python() {
    let Some(uv) = uv_path() else {
        eprintln!("parity_with_python: SKIPPED — uv not found on this machine");
        return;
    };
    if !python_available(&uv) {
        eprintln!("parity_with_python: SKIPPED — `uv run python -c \"import codegraph\"` failed");
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
    let langs = codegraph_core::default_langs();
    let cache = IndexCache::new(&repo_rs.join(".codegraph"));
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

    let langs = codegraph_core::default_langs();
    let cache = IndexCache::new(&repo_rs.join(".codegraph"));
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
