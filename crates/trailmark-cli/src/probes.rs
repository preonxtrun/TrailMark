//! Verify probes: ready-made bounded verification scripts written at focus
//! time (port of `src/trailmark/probes.py`).
//!
//! Each edit-verify cycle in an agent run costs 2–8K tokens because the agent
//! *composes* a test command (variance, unbounded output, interpretation). The
//! probe replaces composition with execution: one call, output bounded by
//! construction, plus a STATE line that makes edit-verify loop repetition
//! visible in the agent's own context.
//!
//! The binary writes `.trailmark/probes/verify.sh` at focus time (runner
//! detection via `atlas::testgraph::detect_runner`, mirroring the Python
//! `cli_focus.run` flow); the script TEXT is byte-identical to
//! `trailmark.probes.probe_script`.

use std::path::{Path, PathBuf};

/// Probe location under the repo root.
pub const PROBE_DIR: &str = ".trailmark/probes";

/// The bounded verification script (deterministic content).
pub fn probe_script(test_cmd: &str, test_target: &str) -> String {
    let target = if test_target.is_empty() {
        String::new()
    } else {
        format!("  # target: {test_target}")
    };
    format!(
        "#!/bin/sh\n\
         # trailmark verify probe — one call, bounded output. Run after each edit.\n\
         cd \"$(dirname \"$0\")/../..\" || exit 1\n\
         # loop-escalation counter: long runs must see their own repetition\n\
         N=$(cat .trailmark/.probe_count 2>/dev/null || echo 0)\n\
         N=$((N+1))\n\
         echo \"$N\" > .trailmark/.probe_count\n\
         echo \"== PROBE RUN #$N ==\"\n\
         if [ \"$N\" -ge 12 ]; then echo \"LOOP ESCALATION: 12+ probe runs — this edit-verify loop is not converging. STOP editing. Write the blocker + evidence to scratch.md, then conclude the task with what you have or report failure.\"; fi\n\
         if [ \"$N\" -ge 20 ]; then echo \"HARD ESCALATION: conclude NOW. Output the current state, the remaining blocker, and the single next action a human should take.\"; fi\n\
         echo '== DIFF =='\n\
         git --no-pager diff --stat 2>/dev/null | tail -5\n\
         echo '== TEST =='\n\
         {test_cmd} 2>&1 | tail -30{target}\n\
         echo '== NEXT: update .trailmark/scratch.md (HYPOTHESIS / LAST RESULT) =='\n"
    )
}

/// Write `.trailmark/probes/verify.sh` (idempotent, best-effort).
///
/// Returns the probe path, or `None` when there is no test command or the
/// write fails — the probe is an optimization, never a failure (the Python
/// caller wraps this in `except Exception: pass`). An existing file with
/// identical content is left untouched (cache-friendly no-op).
pub fn write_probe(root: &Path, test_cmd: &str, test_target: &str) -> Option<PathBuf> {
    if test_cmd.is_empty() {
        return None;
    }
    let probe = root.join(PROBE_DIR).join("verify.sh");
    let content = probe_script(test_cmd, test_target);
    if let Ok(existing) = std::fs::read(&probe) {
        if String::from_utf8_lossy(&existing) == content {
            return Some(probe); // no-op write (cache-friendly)
        }
    }
    probe
        .parent()
        .map(std::fs::create_dir_all)
        .transpose()
        .ok()?;
    std::fs::write(&probe, content).ok()?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&probe, std::fs::Permissions::from_mode(0o755));
    }
    Some(probe)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn probe_script_is_deterministic_and_byte_shaped() {
        let script = probe_script("pytest -q", "app/service.py");
        let expected = concat!(
            "#!/bin/sh\n",
            "# trailmark verify probe — one call, bounded output. Run after each edit.\n",
            "cd \"$(dirname \"$0\")/../..\" || exit 1\n",
            "echo '== DIFF =='\n",
            "git --no-pager diff --stat 2>/dev/null | tail -5\n",
            "echo '== TEST =='\n",
            "pytest -q 2>&1 | tail -30  # target: app/service.py\n",
            "echo '== NEXT: update .trailmark/scratch.md (HYPOTHESIS / LAST RESULT) =='\n",
        );
        assert_eq!(script, expected);
        // empty target → no comment suffix
        let bare = probe_script("pytest -q", "");
        assert!(bare.contains("pytest -q 2>&1 | tail -30\n"));
        assert!(!bare.contains("# target:"));
        assert_eq!(script, probe_script("pytest -q", "app/service.py"));
    }

    #[test]
    fn write_probe_is_idempotent_and_never_fails_hard() {
        let root = std::env::temp_dir().join(format!(
            "cg_probe_{}_{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_nanos())
                .unwrap_or(0)
        ));
        let probe = write_probe(&root, "pytest -q", "a.py").expect("probe written");
        assert_eq!(probe, root.join(".trailmark/probes/verify.sh"));
        let first = std::fs::read_to_string(&probe).expect("read probe");
        assert!(first.starts_with("#!/bin/sh\n"));
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = std::fs::metadata(&probe)
                .expect("stat probe")
                .permissions()
                .mode();
            assert_eq!(mode & 0o777, 0o755, "probe must be executable");
        }
        // same content → untouched no-op
        let mtime = std::fs::metadata(&probe)
            .expect("stat")
            .modified()
            .expect("mtime");
        assert_eq!(write_probe(&root, "pytest -q", "a.py"), Some(probe.clone()));
        assert_eq!(
            std::fs::metadata(&probe)
                .expect("stat")
                .modified()
                .expect("mtime"),
            mtime
        );
        // no test command → no probe
        assert!(write_probe(&root, "", "a.py").is_none());
        // changed command → rewritten
        write_probe(&root, "cargo test", "a.py").expect("rewrite");
        assert!(std::fs::read_to_string(&probe)
            .expect("read")
            .contains("cargo test 2>&1"));
        let _ = std::fs::remove_dir_all(&root);
    }
}
