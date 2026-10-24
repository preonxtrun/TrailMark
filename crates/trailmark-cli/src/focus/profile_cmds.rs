//! Profile-gated baseline snapshots (port of `focus/profile_cmds.py`): run
//! `baseline:` commands before the agent starts and store the bounded output
//! at `.trailmark/baseline.txt`.
//!
//! A profile line `baseline: <command>` opts the repo in: at focus time the
//! command runs once (bounded), and its stdout tail is stored at
//! `.trailmark/baseline.txt`. The atlas emits it as a BASELINE section, so the
//! agent compares its own test runs against a known-good baseline instead of
//! establishing one (kills baseline-establishing runs). Opt-in by construction
//! (writing the profile line is the consent); failures skip — baseline is an
//! optimization, never a failure (Python: `cli_focus` wraps `run_baselines` in
//! `except Exception`).

use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use trailmark_core::py_splitlines;

/// Maximum `baseline:` commands run (Python: `cmds[:MAX_COMMANDS]`).
pub const MAX_COMMANDS: usize = 2;
/// Stdout tail lines kept per command (Python: `splitlines()[-TAIL_LINES:]`).
pub const TAIL_LINES: usize = 40;
/// Per-command wall-clock bound in seconds (Python: `timeout=TIMEOUT`); also
/// the number named in the `(timed out after Ns)` entry.
pub const TIMEOUT: u64 = 300;

/// First line of every `baseline.txt` (Python: `sections[0]`).
pub const HEADER: &str = "BASELINE (captured before your changes — report the delta):";

/// Python `shell=True` on POSIX: `/bin/sh -c <command>`.
const SHELL: &str = "/bin/sh";

/// The `baseline:` commands of `.trailmark/profile.txt` (payloads stripped,
/// capped at [`MAX_COMMANDS`]). Python: `baseline_commands` — a missing or
/// unreadable profile yields no commands.
pub fn baseline_commands(root: &str) -> Vec<String> {
    let path = Path::new(root).join(".trailmark").join("profile.txt");
    // Python read_text(encoding="utf-8") raises UnicodeDecodeError past its
    // `except OSError`, and cli_focus then skips the whole baseline run; a
    // Rust read error (missing file or invalid UTF-8) mirrors that skip.
    let text = match std::fs::read_to_string(&path) {
        Ok(text) => text,
        Err(_) => return Vec::new(),
    };
    let mut cmds = Vec::new();
    // Python: read_text().splitlines() — Unicode line-break semantics
    for line in py_splitlines(&text) {
        let line = line.trim();
        // Python: startswith("baseline:") and len(line) > 9 — i.e. the prefix
        // must have at least one payload character after it.
        if let Some(payload) = line.strip_prefix("baseline:") {
            if !payload.is_empty() {
                cmds.push(payload.trim().to_string());
            }
        }
    }
    cmds.truncate(MAX_COMMANDS);
    cmds
}

/// Run the baseline commands (each bounded at [`TIMEOUT`]), write
/// `.trailmark/baseline.txt`. Returns the written path, or `None` when no
/// `baseline:` lines exist (no file is written) or the write itself fails —
/// failures skip (Python: `run_baselines`).
pub fn run_baselines(root: &str) -> Option<PathBuf> {
    run_baselines_with_timeout(root, Duration::from_secs(TIMEOUT))
}

/// [`run_baselines`] with an injectable wall-clock bound (test seam only; the
/// emitted timeout text always names [`TIMEOUT`], exactly like Python's
/// hard-coded `str(TIMEOUT)`).
pub fn run_baselines_with_timeout(root: &str, timeout: Duration) -> Option<PathBuf> {
    let cmds = baseline_commands(root);
    if cmds.is_empty() {
        return None;
    }
    let mut sections = vec![HEADER.to_string()];
    for cmd in &cmds {
        match bounded_shell_run(cmd, Path::new(root), timeout) {
            Outcome::Ran { stdout, exit } => {
                sections.push(format!("$ {cmd}"));
                sections.extend(stdout_tail(&stdout).into_iter().map(|l| format!("  {l}")));
                sections.push(format!("  exit={exit}"));
            }
            Outcome::TimedOut => {
                sections.push(format!("$ {cmd}"));
                sections.push(format!("  (timed out after {TIMEOUT}s)"));
            }
            Outcome::SpawnFailed(repr) => {
                sections.push(format!("$ {cmd}"));
                sections.push(format!("  (failed: {repr})"));
            }
        }
    }
    let out = Path::new(root).join(".trailmark").join("baseline.txt");
    if let Some(parent) = out.parent() {
        // Python: out.parent.mkdir(parents=True, exist_ok=True); a failure
        // surfaces as an OSError from write_text and the caller skips.
        if std::fs::create_dir_all(parent).is_err()
            || std::fs::write(&out, format_sections(&sections)).is_err()
        {
            return None;
        }
        return Some(out);
    }
    None
}

/// Python: `"\n".join(sections) + "\n"`.
fn format_sections(sections: &[String]) -> String {
    let mut text = sections.join("\n");
    text.push('\n');
    text
}

/// One baseline command's bounded shell run.
enum Outcome {
    /// Finished: full stdout bytes and the Python-style return code
    /// (negative signal number on POSIX signal death).
    Ran { stdout: Vec<u8>, exit: i32 },
    /// The bound elapsed (Python `subprocess.TimeoutExpired`).
    TimedOut,
    /// `spawn` itself failed (Python `OSError` → `(failed: repr(exc))`).
    SpawnFailed(String),
}

/// Run `<cmd>` under `/bin/sh -c` in `cwd`, capture stdout (stderr drained and
/// discarded, Python `capture_output` parity), bounded at `timeout`.
fn bounded_shell_run(cmd: &str, cwd: &Path, timeout: Duration) -> Outcome {
    let mut child = match Command::new(SHELL)
        .arg("-c")
        .arg(cmd)
        .current_dir(cwd)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
    {
        Ok(child) => child,
        Err(err) => return Outcome::SpawnFailed(os_error_repr(&err)),
    };
    let stdout_buf: PipeBuf = Arc::new(Mutex::new(Vec::new()));
    let stderr_buf: PipeBuf = Arc::new(Mutex::new(Vec::new()));
    // Concurrent pipe readers so a chatty command can never deadlock on a
    // full pipe while the bounded wait polls for exit.
    let stdout_reader = spawn_reader(child.stdout.take(), Arc::clone(&stdout_buf));
    let stderr_reader = spawn_reader(child.stderr.take(), Arc::clone(&stderr_buf));
    let deadline = Instant::now() + timeout;
    // Bounded wait for the shell itself (Python: communicate(timeout) kills
    // the child at the deadline and reports TimeoutExpired).
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break Some(status),
            Ok(None) if Instant::now() >= deadline => break None,
            Ok(None) => std::thread::sleep(Duration::from_millis(20)),
            Err(_) => break None,
        }
    };
    match status {
        None => {
            // Deadline hit: kill the shell and reap it. The detached readers
            // EOF (or drain) on their own — the caller stays bounded even if
            // orphaned grandchildren keep the pipes open.
            let _ = child.kill();
            let _ = child.wait();
            Outcome::TimedOut
        }
        Some(status) => {
            // Python communicate() keeps reading to pipe EOF after exit,
            // under the same timeout; past the deadline Python reports
            // TimeoutExpired too.
            if !readers_done(&stdout_reader, &stderr_reader, deadline) {
                let _ = child.kill();
                let _ = child.wait();
                return Outcome::TimedOut;
            }
            let stdout = take_buf(&stdout_buf);
            drop(stdout_reader);
            drop(stderr_reader);
            Outcome::Ran {
                stdout,
                exit: exit_code(&status),
            }
        }
    }
}

/// Shared output buffer written by a detached pipe-reader thread.
type PipeBuf = Arc<Mutex<Vec<u8>>>;

/// Drain `pipe` into `buf` to EOF (stdout/stderr capture with Python
/// `errors="replace"` applied later, on the collected bytes).
fn spawn_reader<R: Read + Send + 'static>(pipe: Option<R>, buf: PipeBuf) -> Option<JoinHandle<()>> {
    pipe.map(|mut pipe| {
        std::thread::spawn(move || {
            let mut chunk = [0u8; 8192];
            loop {
                match pipe.read(&mut chunk) {
                    Ok(0) | Err(_) => break,
                    Ok(n) => {
                        let mut guard = match buf.lock() {
                            Ok(guard) => guard,
                            Err(poisoned) => poisoned.into_inner(),
                        };
                        guard.extend_from_slice(&chunk[..n]);
                    }
                }
            }
        })
    })
}

/// Whether both reader threads finished (joined pipes hit EOF) before the
/// deadline. `None` handles mean no pipe to drain (already done).
fn readers_done(
    stdout_reader: &Option<JoinHandle<()>>,
    stderr_reader: &Option<JoinHandle<()>>,
    deadline: Instant,
) -> bool {
    let done = |handle: &Option<JoinHandle<()>>| match handle {
        Some(handle) => handle.is_finished(),
        None => true,
    };
    while !(done(stdout_reader) && done(stderr_reader)) {
        if Instant::now() >= deadline {
            return false;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    true
}

/// Take the collected bytes out of a shared buffer (poison-tolerant).
fn take_buf(buf: &PipeBuf) -> Vec<u8> {
    let mut guard = match buf.lock() {
        Ok(guard) => guard,
        Err(poisoned) => poisoned.into_inner(),
    };
    std::mem::take(&mut *guard)
}

/// Python `str(proc.returncode)`: the exit code, or `-signal` when the shell
/// died to a POSIX signal.
fn exit_code(status: &std::process::ExitStatus) -> i32 {
    if let Some(code) = status.code() {
        return code;
    }
    #[cfg(unix)]
    {
        use std::os::unix::process::ExitStatusExt;
        if let Some(sig) = status.signal() {
            return -sig;
        }
    }
    0
}

/// Python `repr(exc)` for the OSError `subprocess.run` can raise at spawn
/// (bad shell or bad cwd). The two common errnos mirror CPython's
/// `FileNotFoundError(2, 'No such file or directory')` /
/// `PermissionError(13, 'Permission denied')`; other errnos degrade to an
/// `OSError(errno, …)` shape.
fn os_error_repr(err: &std::io::Error) -> String {
    match err.kind() {
        std::io::ErrorKind::NotFound => "FileNotFoundError(2, 'No such file or directory')".into(),
        std::io::ErrorKind::PermissionDenied => "PermissionError(13, 'Permission denied')".into(),
        _ => match err.raw_os_error() {
            Some(errno) => format!("OSError({errno}, '{err}')"),
            None => format!("OSError(-1, '{err}')"),
        },
    }
}

/// Python: `(proc.stdout or "").splitlines()[-TAIL_LINES:]` — lossy UTF-8
/// (Python `errors="replace"`), Python `str.splitlines` semantics, last
/// [`TAIL_LINES`] lines.
fn stdout_tail(stdout: &[u8]) -> Vec<String> {
    let text = String::from_utf8_lossy(stdout);
    let mut lines = py_splitlines(&text);
    if lines.len() > TAIL_LINES {
        lines.drain(..lines.len() - TAIL_LINES);
    }
    lines
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch(tag: &str) -> String {
        let dir = std::env::temp_dir().join(format!(
            "cg_baseline_{tag}_{}_{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_nanos())
                .unwrap_or(0)
        ));
        std::fs::create_dir_all(dir.join(".trailmark")).expect("mkdir .trailmark");
        dir.to_string_lossy().into_owned()
    }

    #[test]
    fn baseline_commands_parse_strip_and_cap() {
        let root = scratch("parse");
        std::fs::write(
            Path::new(&root).join(".trailmark/profile.txt"),
            "# comment — ignored\n\
             fact: not a baseline command\n\
             baseline:   echo one  \n\
             \n\
             baseline:\n\
             baseline: echo two\n\
             baseline: echo three — beyond the cap\n",
        )
        .expect("write profile");
        let cmds = baseline_commands(&root);
        assert_eq!(cmds, vec!["echo one", "echo two"], "{cmds:?}");
        // missing profile → no commands
        assert!(baseline_commands("/nonexistent/repo").is_empty());
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn run_baselines_writes_python_format() {
        let root = scratch("format");
        std::fs::write(
            Path::new(&root).join(".trailmark/profile.txt"),
            "baseline: echo hello-baseline\n",
        )
        .expect("write profile");
        let out = run_baselines(&root).expect("baseline.txt written");
        assert_eq!(
            out,
            Path::new(&root).join(".trailmark").join("baseline.txt")
        );
        let text = std::fs::read_to_string(&out).expect("read baseline.txt");
        assert_eq!(
            text,
            "BASELINE (captured before your changes — report the delta):\n\
             $ echo hello-baseline\n\
             \x20 hello-baseline\n\
             \x20 exit=0\n",
            "{text:?}"
        );
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn run_baselines_exit_codes_and_tail_cap() {
        let root = scratch("exits");
        std::fs::write(
            Path::new(&root).join(".trailmark/profile.txt"),
            "baseline: exit 3\nbaseline: seq 1 50\n",
        )
        .expect("write profile");
        let out = run_baselines(&root).expect("baseline.txt written");
        let text = std::fs::read_to_string(&out).expect("read baseline.txt");
        let lines: Vec<&str> = text.lines().collect();
        assert_eq!(lines[0], HEADER);
        assert_eq!(lines[1], "$ exit 3");
        assert_eq!(lines[2], "  exit=3");
        assert_eq!(lines[3], "$ seq 1 50");
        // stdout tail keeps exactly the LAST 40 lines (11..50), two-space prefix
        assert_eq!(lines[4], "  11");
        assert_eq!(lines[43], "  50");
        assert_eq!(lines[44], "  exit=0");
        assert_eq!(lines.len(), 45, "{text:?}");
        let _ = std::fs::remove_dir_all(&root);
    }

    #[cfg(unix)]
    #[test]
    fn run_baselines_signal_death_is_negative_exit() {
        let root = scratch("signal");
        std::fs::write(
            Path::new(&root).join(".trailmark/profile.txt"),
            "baseline: kill -TERM $$\n",
        )
        .expect("write profile");
        let out = run_baselines(&root).expect("baseline.txt written");
        let text = std::fs::read_to_string(&out).expect("read baseline.txt");
        assert!(
            text.contains("  exit=-15"),
            "SIGTERM death must read as -15:\n{text}"
        );
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn run_baselines_timeout_entry_names_the_reference_bound() {
        let root = scratch("timeout");
        std::fs::write(
            Path::new(&root).join(".trailmark/profile.txt"),
            "baseline: sleep 30\n",
        )
        .expect("write profile");
        let out = run_baselines_with_timeout(&root, Duration::from_millis(400))
            .expect("baseline.txt written");
        let text = std::fs::read_to_string(&out).expect("read baseline.txt");
        assert_eq!(
            text,
            "BASELINE (captured before your changes — report the delta):\n\
             $ sleep 30\n\
             \x20 (timed out after 300s)\n",
            "{text:?}"
        );
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn no_baseline_lines_means_no_file() {
        let root = scratch("negative");
        std::fs::write(
            Path::new(&root).join(".trailmark/profile.txt"),
            "fact: only a fact\nrule: and a rule\n",
        )
        .expect("write profile");
        assert!(run_baselines(&root).is_none());
        assert!(!Path::new(&root)
            .join(".trailmark")
            .join("baseline.txt")
            .exists());
        // no profile at all → same
        let empty = scratch("negative_empty");
        assert!(run_baselines(&empty).is_none());
        let _ = std::fs::remove_dir_all(&root);
        let _ = std::fs::remove_dir_all(&empty);
    }
}
