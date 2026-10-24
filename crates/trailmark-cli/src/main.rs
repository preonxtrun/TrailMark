//! trailmark CLI: init / focus / remove / stats / doctor / watch
//! (Rust port of the SPEC CLI contract).
//!
//! Exit codes: 0 = success, 1 = doctor failures / runtime error, 2 = missing
//! cache ("run `trailmark init` first") or usage errors. `--json` prints a
//! single machine-readable JSON object consumed by the bench pipeline.

use std::io::Read as _;
use std::path::{Path, PathBuf};
use std::time::Duration;

use clap::{CommandFactory, Parser, Subcommand, ValueEnum};
use trailmark_cli::atlas::assemble;
use trailmark_cli::focus::{generate_for, generate_top, Lead};
use trailmark_core::{
    build, byte_len, read_block, remove_block, update, write_block, IndexCache,
    DEFAULT_BUDGET_BYTES,
};

fn main() {
    let cli = Cli::parse();
    let root_flag = cli.root.clone();
    let budget_flag = cli.budget;
    let json_flag = cli.json;
    let code = match cli.cmd {
        Some(Cmd::Init {
            langs,
            workers,
            force,
        }) => cmd_init(
            &invocation(&root_flag, budget_flag, json_flag),
            langs.as_ref(),
            workers,
            force,
        ),
        Some(Cmd::Focus {
            issue,
            issue_file,
            cli: cli_name,
        }) => cmd_focus(
            &invocation(&root_flag, budget_flag, json_flag),
            issue.as_deref(),
            issue_file.as_deref(),
            cli_name,
        ),
        Some(Cmd::Remove { purge }) => {
            cmd_remove(&invocation(&root_flag, budget_flag, json_flag), purge)
        }
        Some(Cmd::Stats) => cmd_stats(&invocation(&root_flag, budget_flag, json_flag)),
        Some(Cmd::Refs { langs }) => cmd_refs(
            &invocation(&root_flag, budget_flag, json_flag),
            langs.as_deref(),
        ),
        Some(Cmd::Doctor) => cmd_doctor(&invocation(&root_flag, budget_flag, json_flag)),
        Some(Cmd::Watch { interval }) => {
            cmd_watch(&invocation(&root_flag, budget_flag, json_flag), interval)
        }
        None => {
            // no subcommand: print help like the Python reference
            let _ = Cli::command().print_help();
            0
        }
    };
    std::process::exit(code);
}

// ---------------------------------------------------------------------------
// argument parsing
// ---------------------------------------------------------------------------

#[derive(Parser, Debug)]
#[command(
    name = "trailmark",
    version,
    about = "Hidden optimization layer between coding CLIs and the codebase."
)]
struct Cli {
    /// Repository root.
    #[arg(long, global = true, default_value = ".")]
    root: PathBuf,
    /// Block budget bytes.
    #[arg(long, global = true)]
    budget: Option<i64>,
    /// Print machine-readable JSON.
    #[arg(long, global = true)]
    json: bool,
    #[command(subcommand)]
    cmd: Option<Cmd>,
}

#[derive(Subcommand, Debug)]
enum Cmd {
    /// Build/update the index and write the AGENTS.md atlas block.
    Init {
        /// Comma-separated languages (default: py,ts,js,go,rs).
        #[arg(long, global = true)]
        langs: Option<String>,
        /// Parser workers (default: rayon pool).
        #[arg(long)]
        workers: Option<usize>,
        /// Full rebuild instead of mtime-incremental update.
        #[arg(long)]
        force: bool,
    },
    /// Regenerate the FULL block (atlas + PLAN) for an issue.
    Focus {
        /// Issue / problem-statement text (or read from --issue-file/stdin).
        issue: Option<String>,
        /// Read the issue text from this file.
        #[arg(long = "issue-file")]
        issue_file: Option<PathBuf>,
        /// Target coding CLI (auto resolves via env).
        #[arg(long, value_enum)]
        cli: Option<CliName>,
    },
    /// Remove the managed block (and optionally .trailmark/).
    Remove {
        /// Also delete the .trailmark/ state tree.
        #[arg(long)]
        purge: bool,
    },
    /// Debug: print the fully-resolved reference set (diagnostic tool).
    Refs {
        /// Comma-separated languages (default: py,ts,js,go,rs).
        #[arg(long, global = true)]
        langs: Option<String>,
    },
    /// Print index statistics from the cache.
    Stats,
    /// Sanity-check cache freshness and the written block.
    Doctor,
    /// Poll the repo and refresh the block on change.
    Watch {
        /// Poll interval seconds.
        #[arg(long, default_value_t = 2.0)]
        interval: f64,
    },
}

/// `--cli` choices (auto resolves via the CLAUDECODE env var).
#[derive(Clone, Copy, Debug, ValueEnum)]
enum CliName {
    Auto,
    Claude,
    Codex,
    Opencode,
    Generic,
}

impl CliName {
    fn as_str(&self) -> &'static str {
        match self {
            CliName::Auto => "auto",
            CliName::Claude => "claude",
            CliName::Codex => "codex",
            CliName::Opencode => "opencode",
            CliName::Generic => "generic",
        }
    }
}

// ---------------------------------------------------------------------------
// shared helpers
// ---------------------------------------------------------------------------

fn root_of(inv: &Invocation) -> PathBuf {
    trailmark_core::scanner::resolve_root(inv.root.to_string_lossy().as_ref()).into()
}

fn budget_of(inv: &Invocation) -> usize {
    match inv.budget {
        None => DEFAULT_BUDGET_BYTES,
        Some(v) => v.max(0) as usize,
    }
}

fn parse_langs(raw: Option<&String>) -> Vec<String> {
    let langs: Vec<String> = raw
        .map(|s| {
            s.split(',')
                .map(|p| p.trim().to_string())
                .filter(|p| !p.is_empty())
                .collect()
        })
        .unwrap_or_default();
    if langs.is_empty() {
        trailmark_core::default_langs()
    } else {
        langs
    }
}

fn json_out(_cli: &Invocation, value: serde_json::Value) {
    println!("{}", serde_json::to_string(&value).unwrap_or_default());
}

/// The resolved global flags (root / budget / json), independent of the
/// subcommand enum so command handlers can borrow them freely.
struct Invocation {
    root: PathBuf,
    budget: Option<i64>,
    json: bool,
}

fn invocation(root: &Path, budget: Option<i64>, json: bool) -> Invocation {
    Invocation {
        root: root.to_path_buf(),
        budget,
        json,
    }
}

/// Python `round(value, 4)` equivalent: correctly-rounded 4-decimal digits,
/// returned as the nearest f64.
fn round4(value: f64) -> f64 {
    let s = format!("{value:.4}");
    s.parse().unwrap_or(value)
}

fn resolve_cli_name(name: Option<CliName>) -> String {
    match name {
        Some(CliName::Auto) | None => {
            if std::env::var_os("CLAUDECODE").is_some() {
                "claude".to_string()
            } else {
                "generic".to_string()
            }
        }
        Some(other) => other.as_str().to_string(),
    }
}

fn read_issue_text(issue: Option<&str>, issue_file: Option<&Path>) -> Result<String, String> {
    if let Some(issue) = issue {
        if !issue.trim().is_empty() {
            return Ok(issue.to_string());
        }
    }
    if let Some(file) = issue_file {
        return std::fs::read_to_string(file)
            .map_err(|_| format!("cannot read issue file: {}", file.display()));
    }
    let mut buf = String::new();
    let _ = std::io::stdin().read_to_string(&mut buf);
    Ok(buf)
}

// ---------------------------------------------------------------------------
// commands
// ---------------------------------------------------------------------------

fn cmd_init(inv: &Invocation, langs: Option<&String>, workers: Option<usize>, force: bool) -> i32 {
    let root = root_of(inv);
    let budget = budget_of(inv);
    let langs = parse_langs(langs);
    let workers = workers.unwrap_or(0);
    let cache = IndexCache::new(&root.join(".trailmark"));
    let data = if force {
        build(
            root.to_string_lossy().as_ref(),
            Some(&langs),
            workers,
            Some(&cache),
        )
    } else {
        update(
            root.to_string_lossy().as_ref(),
            Some(&langs),
            workers,
            Some(&cache),
        )
    };
    let body = assemble(&data, None, "generic", budget);
    let changed = match write_block(&root.join(trailmark_core::AGENTS_FILENAME), &body) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("trailmark: cannot write AGENTS.md: {e}");
            return 1;
        }
    };
    // outlines for the top-32 pagerank files; never fatal
    if let Err(e) = generate_top(&data, &root.join(".trailmark").join("outlines"), 32) {
        eprintln!("trailmark: outlines generation failed ({e}) — skipping");
    }
    let files = data.files.len();
    let symbols = data.symbols().len();
    let refs = data.refs.len();
    if inv.json {
        json_out(
            inv,
            serde_json::json!({
                "status": "ok",
                "files": files,
                "symbols": symbols,
                "refs": refs,
                "block_bytes": byte_len(&body),
                "changed": changed,
            }),
        );
    } else {
        println!(
            "trailmark init: {files} files, {symbols} symbols, {refs} refs → AGENTS.md \
             ({}/{budget} B, {})",
            byte_len(&body),
            if changed { "updated" } else { "unchanged" }
        );
    }
    0
}

fn cmd_focus(
    inv: &Invocation,
    issue_arg: Option<&str>,
    issue_file: Option<&Path>,
    cli_choice: Option<CliName>,
) -> i32 {
    let root = root_of(inv);
    let trailmark_dir = root.join(".trailmark");
    let cache = IndexCache::new(&trailmark_dir);
    let Some(data) = cache.load() else {
        eprintln!("run `trailmark init` first");
        return 2;
    };
    let raw = match read_issue_text(issue_arg, issue_file) {
        Ok(text) => text,
        Err(err) => {
            eprintln!("{err}");
            return 2;
        }
    };
    let issue = raw.trim().to_string();
    if issue.is_empty() {
        eprintln!("no issue text provided (pass ISSUE, --issue-file, or pipe stdin)");
        return 2;
    }
    let cli_name = resolve_cli_name(cli_choice);
    // Python cli_focus: a zero/invalid --budget falls back to the default
    let budget = match inv.budget {
        Some(v) if v > 0 => v as usize,
        _ => DEFAULT_BUDGET_BYTES,
    };
    // rank computes the personalized graph pagerank and the plain prior in one
    // pass; the prior is handed to the assembler so the block body reuses the
    // same power iteration instead of recomputing it
    let (leads, prior) = trailmark_cli::focus::rank_and_prior(&data, &cache, &issue, 5);
    let body = trailmark_cli::atlas::assemble_with_pagerank(
        &data,
        Some(&issue),
        &cli_name,
        budget,
        Some(&prior),
    );
    let changed = match write_block(&root.join(trailmark_core::AGENTS_FILENAME), &body) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("trailmark: cannot write AGENTS.md: {e}");
            return 1;
        }
    };
    let lead_paths: Vec<String> = leads.iter().map(|l| l.path.clone()).collect();
    if let Err(e) = generate_for(&data, &lead_paths, &trailmark_dir.join("outlines")) {
        eprintln!("trailmark: outlines generation failed ({e})");
    }
    if inv.json {
        let leads_json: Vec<serde_json::Value> = leads
            .iter()
            .map(|lead: &Lead| {
                serde_json::json!({
                    "path": lead.path,
                    "start": lead.start_line,
                    "end": lead.end_line,
                    "why": lead.why,
                    "score": round4(lead.score),
                })
            })
            .collect();
        json_out(
            inv,
            serde_json::json!({
                "leads": leads_json,
                "bytes": byte_len(&body),
                "changed": changed,
                "cli": cli_name,
            }),
        );
    }
    0
}

fn cmd_remove(inv: &Invocation, purge: bool) -> i32 {
    let root = root_of(inv);
    let changed = match remove_block(&root.join(trailmark_core::AGENTS_FILENAME)) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("trailmark: cannot remove block: {e}");
            return 1;
        }
    };
    let mut purged = false;
    if purge {
        let _ = std::fs::remove_dir_all(root.join(".trailmark"));
        purged = true;
    }
    if inv.json {
        json_out(
            inv,
            serde_json::json!({ "removed": changed, "purged": purged }),
        );
    } else {
        println!(
            "trailmark remove: block {}{}",
            if changed { "removed" } else { "not found" },
            if purged { ", .trailmark/ purged" } else { "" }
        );
    }
    0
}

/// Debug: build the full index (no cache) and print every resolved ref as
/// `src_file\tdst_name\tdst_file\tline` (TSV) or a JSON object. Mirrors
/// `tools/dump_refs.py` so the two implementations' ref sets can be diffed.
fn cmd_refs(inv: &Invocation, langs_raw: Option<&str>) -> i32 {
    let root = root_of(inv);
    let langs = parse_langs(langs_raw.map(|s| s.to_string()).as_ref());
    let data = build(root.to_string_lossy().as_ref(), Some(&langs), 0, None);
    if inv.json {
        let refs: Vec<serde_json::Value> = data
            .refs
            .iter()
            .map(|r| {
                serde_json::json!({
                    "src_file": r.src_file,
                    "dst_name": r.dst_name,
                    "dst_file": r.dst_file,
                    "line": r.line,
                })
            })
            .collect();
        json_out(
            inv,
            serde_json::json!({
                "root": data.root,
                "files": data.files.len(),
                "refs": refs,
            }),
        );
        return 0;
    }
    for r in &data.refs {
        println!("{}\t{}\t{}\t{}", r.src_file, r.dst_name, r.dst_file, r.line);
    }
    eprintln!(
        "trailmark refs: {} files, {} refs",
        data.files.len(),
        data.refs.len()
    );
    0
}

fn cmd_stats(inv: &Invocation) -> i32 {
    let root = root_of(inv);
    let cache = IndexCache::new(&root.join(".trailmark"));
    let Some(data) = cache.load() else {
        eprintln!("run `trailmark init` first");
        return 2;
    };
    let block_bytes = read_block(&root.join(trailmark_core::AGENTS_FILENAME))
        .map(|b| byte_len(&b))
        .unwrap_or(0);
    let files = data.files.len();
    let symbols = data.symbols().len();
    let refs = data.refs.len();
    if inv.json {
        json_out(
            inv,
            serde_json::json!({
                "files": files,
                "symbols": symbols,
                "refs": refs,
                "block_bytes": block_bytes,
            }),
        );
    } else {
        println!(
            "trailmark stats: {files} files, {symbols} symbols, {refs} refs, block {block_bytes} B"
        );
    }
    0
}

fn cmd_doctor(inv: &Invocation) -> i32 {
    let root = root_of(inv);
    let budget = budget_of(inv);
    let cache = IndexCache::new(&root.join(".trailmark"));
    let data = cache.load();
    let cache_ok = data.is_some();
    let mut fresh = false;
    if let Some(data) = &data {
        let mtimes = cache.file_mtimes();
        let mut stale = 0usize;
        let mut missing = 0usize;
        for (rel, mtime) in &mtimes {
            match std::fs::metadata(root.join(rel)) {
                Ok(meta) => {
                    let current = trailmark_core::scanner::systemtime_to_f64(meta.modified().ok());
                    if current != *mtime {
                        stale += 1;
                    }
                }
                Err(_) => missing += 1,
            }
        }
        fresh = stale == 0 && missing == 0 && !mtimes.is_empty();
        let _ = data; // files enumerated via mtimes
    }
    let block = read_block(&root.join(trailmark_core::AGENTS_FILENAME));
    let block_ok = block.is_some();
    let bytes = block.as_ref().map(|b| byte_len(b)).unwrap_or(0);
    let ok = cache_ok && fresh && block_ok && bytes <= budget;
    if inv.json {
        json_out(
            inv,
            serde_json::json!({
                "cache": cache_ok,
                "fresh": fresh,
                "block": block_ok,
                "bytes": bytes,
                "ok": ok,
            }),
        );
    } else {
        println!(
            "cache: {}",
            if cache_ok {
                "ok"
            } else {
                "FAIL — run `trailmark init` first"
            }
        );
        println!("fresh: {}", if fresh { "ok" } else { "FAIL" });
        println!(
            "block: {}",
            if block_ok {
                "ok"
            } else {
                "FAIL — run `trailmark init` first"
            }
        );
        println!("block-size: {bytes}/{budget} bytes");
        println!("doctor: {}", if ok { "PASS" } else { "FAIL" });
    }
    if ok {
        0
    } else {
        1
    }
}

fn cmd_watch(inv: &Invocation, interval: f64) -> i32 {
    let root = root_of(inv);
    let interval = interval.max(0.1);
    let budget = budget_of(inv);
    let langs = parse_langs(None);
    if !inv.json {
        println!(
            "trailmark watch: polling {} every {interval:.1}s (Ctrl-C to stop)",
            root.display()
        );
    }
    let mut last: Option<String> = None;
    loop {
        let signature = scan_signature(&root);
        if last.as_ref() != Some(&signature) {
            let cache = IndexCache::new(&root.join(".trailmark"));
            let data = update(
                root.to_string_lossy().as_ref(),
                Some(&langs),
                0,
                Some(&cache),
            );
            let body = assemble(&data, None, "generic", budget);
            let changed = match write_block(&root.join(trailmark_core::AGENTS_FILENAME), &body) {
                Ok(c) => c,
                Err(e) => {
                    eprintln!("trailmark: watch failed: {e}");
                    return 1;
                }
            };
            if inv.json {
                json_out(
                    inv,
                    serde_json::json!({
                        "files": data.files.len(),
                        "block_bytes": byte_len(&body),
                        "changed": changed,
                    }),
                );
            } else {
                println!(
                    "watch: {} files — block {}",
                    data.files.len(),
                    if changed { "updated" } else { "unchanged" }
                );
            }
            last = Some(signature);
        }
        std::thread::sleep(Duration::from_secs_f64(interval));
    }
}

/// Cheap deterministic repo-state fingerprint (paths, sizes, mtimes) for watch.
///
/// Skips state directories and AGENTS.md itself — our own writes must not
/// re-trigger the loop. The signature is internal only, never emitted.
fn scan_signature(root: &Path) -> String {
    fn skip(name: &str) -> bool {
        matches!(
            name,
            ".trailmark"
                | ".git"
                | ".hg"
                | ".mypy_cache"
                | ".pytest_cache"
                | ".ruff_cache"
                | ".tox"
                | ".venv"
                | "__pycache__"
                | "build"
                | "dist"
                | "env"
                | "node_modules"
                | "venv"
        ) || name.ends_with(".egg-info")
    }

    let mut parts: Vec<String> = Vec::new();
    let mut stack = vec![root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        let Ok(entries) = std::fs::read_dir(&dir) else {
            continue;
        };
        let mut names: Vec<(String, PathBuf, bool)> = Vec::new();
        for entry in entries.flatten() {
            let name = entry.file_name().to_string_lossy().to_string();
            let is_dir = entry.file_type().map(|t| t.is_dir()).unwrap_or(false);
            names.push((name, entry.path(), is_dir));
        }
        names.sort_by(|a, b| a.0.cmp(&b.0));
        for (name, path, is_dir) in names {
            if is_dir {
                if !skip(&name) {
                    stack.push(path);
                }
                continue;
            }
            if name == trailmark_core::AGENTS_FILENAME {
                continue;
            }
            let Ok(meta) = std::fs::metadata(&path) else {
                continue;
            };
            let rel = path.strip_prefix(root).unwrap_or(&path).to_string_lossy();
            let mtime_ns = meta
                .modified()
                .ok()
                .and_then(|m| m.duration_since(std::time::UNIX_EPOCH).ok())
                .map(|d| d.as_nanos())
                .unwrap_or(0);
            parts.push(format!("{rel}:{}:{mtime_ns}", meta.len()));
        }
    }
    // FNV-1a 64-bit over the sorted parts (internal fingerprint, never emitted)
    let mut hash: u64 = 0xcbf2_9ce4_8422_2325;
    for b in parts.join("\n").as_bytes() {
        hash ^= u64::from(*b);
        hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
    }
    format!("{hash:016x}")
}
