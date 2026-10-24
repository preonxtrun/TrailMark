"""Local, docker-free A/B mini-bench for trailmark development iterations.

One real repo, N task statements, control vs treatment, tokens/turns delta.
This is the fast per-generation signal (minutes); the SWE-bench standard check
(``bench.run`` + ``bench.prepare_images``) stays available for occasional
verification on a small subset.

Protocol per (task, arm, seed):
    1. reset the repo worktree (git reset --hard + clean, preserving .trailmark)
    2. arm setup: control → strip the trailmark block; treatment → ``trailmark focus``
    3. run stock opencode headless in the repo; parse usage via bench.cost
    4. record patch (official exclude pathspec) + usage/turns
    5. (``--verify``) bounded benchmark-side verification: run the repo's detected
       test runner and parse pytest-style pass/fail counts into the record
    6. trails auto-deposit (treatment only): the patch footprint is recorded into the
       worktree's trail store, so the next treatment run's block carries TRAILS

Usage:
    uv run python -m bench.minibench --repo /path/to/repo \
        --tasks bench/tasks/requests-5.jsonl --arm control,treatment \
        --seeds 1 --out runs/mini0
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import threading
import time
from collections.abc import Mapping
from pathlib import Path

from .arms import patch_cmd
from .cost import parse as parse_usage
from .report import paired_report, render_markdown
from .run import DEFAULT_MODEL

logger = logging.getLogger("bench.minibench")

RESET_EXCLUDES = ["-e", ".trailmark"]

#: Hard timeout (seconds) for the bounded ``--verify`` step. Verification is
#: benchmark-side and never agent-facing, so its budget is independent of the agent's.
VERIFY_TIMEOUT = 600

#: Only the tail of the runner output is scanned for the pytest-style summary.
VERIFY_TAIL_CHARS = 4000

#: ``verify`` payload for a run whose verification did not execute (empty patch or no
#: detectable runner; with ``--verify`` off the record carries ``verify: null`` instead).
VERIFY_SKIPPED: dict[str, object] = {"ran": False, "passed": 0, "failed": 0, "exit": None}

#: Pytest-style summary counters scanned by :func:`parse_verify_counts`.
_PASSED_RE = re.compile(r"(\d+) passed")
_FAILED_RE = re.compile(r"(\d+) failed")


def _git(repo: Path, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=False, capture_output=True, text=True, timeout=timeout
    )


def reset_repo(repo: Path) -> None:
    """Hard-reset the worktree, preserving .trailmark (untracked files cleaned)."""
    for args in (("reset", "--hard", "HEAD"), ("clean", "-fd", *RESET_EXCLUDES)):
        proc = _git(repo, *args)
        if proc.returncode != 0:
            logger.warning("git %s failed: %s", args[0], proc.stderr.strip()[-200:])


def patch_extract(repo: Path) -> str:
    proc = subprocess.run(
        patch_cmd(str(repo)), shell=True, check=False, capture_output=True, text=True, timeout=120
    )
    return proc.stdout if proc.returncode == 0 else ""


def trail_files_from_patch(patch: str) -> list[str]:
    """Repo-relative paths a unified diff touches, from its ``+++ b/<path>`` lines.

    Deletions (``+++ /dev/null``) carry no surviving path and are dropped; duplicates
    are collapsed and the result is sorted (the trail store canonicalizes order
    anyway). Trailing tab-separated timestamps (``+++ b/f.py\\t2026-…``) are stripped.
    """
    files: set[str] = set()
    for line in patch.splitlines():
        if not line.startswith("+++ "):
            continue
        path = line[len("+++ ") :].split("\t", 1)[0].strip()
        if path.startswith("b/"):
            files.add(path[len("b/") :])
    return sorted(files)


def trail_outcome(status: str, patch_bytes: int, verify: Mapping[str, object] | None) -> str:
    """Heuristic trail outcome — honest caveat: no per-task test gating exists yet.

    ``"solved"`` iff the run completed, the patch is non-empty, and the bounded
    verification — when it ran — reported no failed tests; every other combination
    deposits ``"failed"`` (the repellent signal: dead ends steer future tasks away).
    """
    if status != "completed" or patch_bytes <= 0:
        return "failed"
    if isinstance(verify, Mapping):
        failed = verify.get("failed")
        if isinstance(failed, int) and not isinstance(failed, bool) and failed > 0:
            return "failed"
    return "solved"


def deposit_trail(
    repo: Path,
    task_id: str,
    patch: str,
    status: str,
    patch_bytes: int,
    verify: Mapping[str, object] | None,
) -> bool:
    """Record one task's footprint into *repo*'s trail store (best-effort).

    Closes the memory loop: treatment runs deposit ``(task_id, outcome, files)`` into
    ``.trailmark/trails.jsonl`` (one line per task id, latest wins), and the next
    treatment run's ``trailmark focus`` reads that store and injects the TRAILS
    section into the agent block. Control runs never reach this (guarded in
    :func:`run_bookkeeping`). Returns True when a trail was recorded; failures are
    logged, never raised — a deposit must not flip a completed measurement into an
    error.
    """
    from trailmark.trails import record as trails_record  # lazy, like the other layer hooks

    try:
        trails_record(
            root=str(repo),
            task_id=task_id,
            files=trail_files_from_patch(patch),
            outcome=trail_outcome(status, patch_bytes, verify),
        )
        return True
    except Exception:  # per-run isolation: bookkeeping is never worth a lost measurement
        logger.exception("trail deposit failed: task=%s repo=%s", task_id, repo)
        return False


def _rust_binary() -> Path:
    """Locate the release Rust binary (Gen 1.0 treatment core)."""
    root = Path(__file__).resolve().parents[1]
    binary = root / "target" / "release" / "trailmark"
    if not binary.is_file():
        raise SystemExit(f"rust binary not found at {binary} — run: cargo build --release")
    return binary


def arm_setup(
    repo: Path,
    arm: str,
    statement: str,
    model: str,
    budget: int,
    core: str = "python",
) -> None:
    """control → strip the block; treatment → focus (python in-process or rust binary)."""
    from trailmark.emit.agents_md import remove_block

    if arm == "control":
        remove_block(repo / "AGENTS.md")
        return
    if arm != "treatment":
        raise ValueError(f"unknown arm {arm!r}")

    if core == "rust":
        import subprocess

        binary = str(_rust_binary())
        if not (repo / ".trailmark" / "index.db").exists():
            subprocess.run(
                [binary, "init", "--root", str(repo), "--budget", str(budget)],
                check=True, capture_output=True, text=True, timeout=300,
            )
        proc = subprocess.run(
            [
                binary, "focus", statement,
                "--root", str(repo), "--budget", str(budget),
                "--cli", "generic",
            ],
            check=False, capture_output=True, text=True, timeout=300,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"rust focus failed rc={proc.returncode}: {proc.stderr[-200:]}")
        return

    from trailmark.cache import IndexCache
    from trailmark.focus import cli_focus

    cache = IndexCache(repo / ".trailmark")
    if cache.load() is None:
        from trailmark.index import update

        t0 = time.perf_counter()
        update(root=str(repo), langs=None, workers=8, cache=cache)
        logger.info("treatment init (cold): %.2fs", time.perf_counter() - t0)
    args = argparse.Namespace(
        root=str(repo), issue=statement, issue_file=None, cli="generic", budget=budget, json=False
    )
    rc = cli_focus.run(args)
    if rc != 0:
        raise RuntimeError(f"trailmark focus failed rc={rc}")
    # Protect the layer's footprint from the agent's own git hygiene (with --auto the
    # agent can run git clean; untracked .trailmark/ would be swept). .git/info/exclude
    # is local-only: no repo pollution, clean patches, and cleans stop deleting state.
    exclude = repo / ".git" / "info" / "exclude"
    try:
        current = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        needed = [entry for entry in (".trailmark/", "AGENTS.md") if entry not in current]
        if needed:
            exclude.parent.mkdir(parents=True, exist_ok=True)
            with exclude.open("a", encoding="utf-8") as fh:
                fh.write("\n# trailmark layer (local)\n" + "\n".join(needed) + "\n")
    except OSError:
        logger.warning("could not update .git/info/exclude")


def run_with_timeout(
    cmd: str, cwd: str, timeout: int, env: dict[str, str] | None = None
) -> tuple[str, str, int]:
    """Run *cmd* in its own process group; on timeout, kill the WHOLE group.

    Why not ``subprocess.run(timeout=...)``: with ``shell=True`` the timeout kills
    only the wrapping ``sh``, while the actual agent keeps the stdout pipe open —
    the runner then hangs forever despite its timeout. Process groups make the
    budget enforceable (guarantee: a run can never outlive its slot).
    """
    import os
    import signal

    proc = subprocess.Popen(
        cmd,
        shell=True,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env=env,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return stdout, stderr, proc.returncode or 0
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        stdout, stderr = proc.communicate()
        return stdout, stderr, -9


def parse_verify_counts(text: str) -> tuple[int, int]:
    """(passed, failed) from the pytest-style summary in the tail of runner output.

    Recognizes ``N passed`` / ``M failed`` (e.g. ``2 failed, 3 passed in 0.10s``); the
    LAST occurrence of each wins (pytest prints a single final summary, and a runner
    script that prints several naturally reports the latest). No counts → (0, 0).
    Never raises.
    """
    tail = text[-VERIFY_TAIL_CHARS:]
    passed = _PASSED_RE.findall(tail)
    failed = _FAILED_RE.findall(tail)
    return (int(passed[-1]) if passed else 0, int(failed[-1]) if failed else 0)


def verify_run(repo: Path, log_path: Path, timeout: int = VERIFY_TIMEOUT) -> dict[str, object]:
    """Bounded benchmark-side verification of a completed run.

    Detects the repo's test runner via ``trailmark.atlas.testgraph.detect_runner``
    (lazy + read-only: it only reads repo config files), executes it in *repo* under a
    hard process-group timeout, writes the combined output to *log_path* (a side log
    in the run dir — never into the agent stream), and parses pytest-style pass/fail
    counts from the output tail. Returns the record's ``verify`` payload:
    ``{"ran": True, "passed": N, "failed": M, "exit": rc}`` — or the skipped payload
    (``ran: False``) when the repo exposes no detectable runner.
    """
    from trailmark.atlas.testgraph import detect_runner  # lazy + read-only

    command = detect_runner(str(repo))
    if not command:
        return dict(VERIFY_SKIPPED)
    stdout, stderr, rc = run_with_timeout(command, cwd=str(repo), timeout=timeout)
    passed, failed = parse_verify_counts(stdout)
    output = (stdout + stderr).strip() or f"(no output, exit={rc})"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(output + "\n", encoding="utf-8")
    except OSError:
        logger.warning("could not write verify log %s", log_path)
    return {"ran": True, "passed": passed, "failed": failed, "exit": rc}


def run_agent(
    repo: Path, statement: str, model: str, timeout: int, max_turns: int | None = None
) -> tuple[str, int]:
    """Run opencode headless with an enforceable TURN budget (turn-control policy).

    Counts `step_finish` events in the JSON stream; at max_turns the whole process
    group is killed (arXiv 2510.16786: fixed turn limits cut cost 24-68% with
    minimal solve impact). A turn-capped run is a completed measurement, not an
    error — it caps the 100+-turn runaway trajectories that dominate token cost.
    """
    import os
    import signal
    import threading

    cmd = (
        f"opencode run --standalone --format json --auto"
        f" --model {json.dumps(model)} {json.dumps(statement)}"
    )
    proc = subprocess.Popen(
        cmd,
        shell=True,
        cwd=str(repo),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,  # merge: no pipe deadlock; parser skips non-JSON
        text=True,
        errors="replace",
        start_new_session=True,
    )
    deadline = time.monotonic() + timeout
    killed = {"reason": ""}

    def watchdog() -> None:
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        if proc.poll() is None:
            killed["reason"] = "timeout"
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()

    threading.Thread(target=watchdog, daemon=True).start()

    out_lines: list[str] = []
    turns = 0
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            out_lines.append(line)
            if max_turns and '"type":"step_finish"' in line:
                turns += 1
                if turns >= max_turns:
                    killed["reason"] = "turn-limit"
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        proc.kill()
                    break
    finally:
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
    output = "".join(out_lines)
    rc = proc.returncode if proc.returncode is not None else -9
    if killed["reason"] == "turn-limit":
        rc = 0  # turn-capped = completed measurement
    return output, rc


def ensure_worktrees(repo: Path, out_dir: Path, count: int) -> list[Path]:
    """One git worktree per parallel worker (detached at HEAD), reused across reruns.

    Paths are ABSOLUTE: `git worktree add` resolves relative targets against the
    repo root, which would silently create worktrees inside the repo under test.
    """
    wt_root = (Path.cwd() / out_dir / "worktrees").resolve()
    wt_root.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(count):
        wt = (wt_root / f"wt{i}").resolve()
        if not wt.is_dir():
            proc = subprocess.run(
                ["git", "-C", str(repo), "worktree", "add", "--detach", str(wt), "HEAD"],
                check=False, capture_output=True, text=True, timeout=120,
            )
            if proc.returncode != 0 and not wt.is_dir():
                raise SystemExit(f"worktree creation failed: {proc.stderr.strip()[-200:]}")
        paths.append(wt)
    return paths


def load_tasks(tasks_file: Path, limit: int | None) -> list[dict]:
    tasks = [
        json.loads(line)
        for line in tasks_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    for i, task in enumerate(tasks):
        task.setdefault("id", f"task-{i}")
    return tasks[:limit] if limit else tasks


def run_bookkeeping(
    wt: Path,
    out_dir: Path,
    task_id: str,
    arm: str,
    seed: int,
    record: dict[str, object],
    patch: str,
    *,
    verify_enabled: bool,
    trails_enabled: bool,
) -> None:
    """Post-run bookkeeping for a finished run (mutates *record* in place; never raises).

    Runs BEFORE the ``finally`` reset, so verification sees the same working tree the
    patch was extracted from.

    - ``--verify`` (opt-in): bounded benchmark-side verification of completed runs.
      Runs with an empty patch skip verification — nothing to verify — and are
      recorded with ``verify.ran=false``; with ``--verify`` off the record carries
      ``verify: null`` (no verification data at all).
    - trails auto-deposit (default; ``--no-trails`` disables): completed *treatment*
      runs record their patch footprint into the worktree's trail store so the next
      treatment run's block carries TRAILS; control runs never deposit.

    A completed measurement stays completed: bookkeeping problems degrade to
    ``verify: null`` / ``trail_recorded: false`` and are logged.
    """
    record["trail_recorded"] = False
    record["verify"] = None
    if record.get("status") != "completed":
        return
    if verify_enabled:
        record["verify"] = dict(VERIFY_SKIPPED)
        if int(record.get("patch_bytes") or 0) > 0:
            log_path = out_dir / "verify" / f"{task_id}.{arm}.s{seed}.log"
            try:
                record["verify"] = verify_run(wt, log_path)
            except Exception:
                record["verify"] = None
                logger.exception("verify failed: %s", (task_id, arm, seed))
    if arm == "treatment" and trails_enabled:
        try:
            record["trail_recorded"] = deposit_trail(
                wt,
                task_id,
                patch,
                str(record.get("status")),
                int(record.get("patch_bytes") or 0),
                record["verify"] if isinstance(record["verify"], Mapping) else None,
            )
        except Exception:
            logger.exception("trail deposit failed: %s", (task_id, arm, seed))


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; *argv* defaults to ``sys.argv[1:]`` (injectable for tests)."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--tasks", default="bench/tasks/requests-5.jsonl")
    parser.add_argument("--arm", default="control,treatment")
    parser.add_argument("--seeds", type=int, default=1)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--budget", type=int, default=2048)
    parser.add_argument("--timeout", type=int, default=900, help="per agent run, seconds")
    parser.add_argument(
        "--max-turns",
        type=int,
        default=None,
        help="turn-control policy: kill the agent at N completed steps (cost ceiling)",
    )
    parser.add_argument(
        "--core",
        choices=("python", "rust"),
        default="python",
        help="treatment implementation: python in-process, or the release rust binary",
    )
    parser.add_argument("--limit", type=int, default=None, help="first N tasks only")
    parser.add_argument(
        "--verify",
        action="store_true",
        help=(
            "benchmark-side gating: after each completed run with a non-empty patch, run"
            " the repo's detected test runner (bounded, 600s) and record pass/fail counts"
        ),
    )
    parser.add_argument(
        "--no-trails",
        action="store_true",
        help=(
            "disable trails auto-deposit (completed treatment runs record their patch"
            " footprint into the worktree's trail store by default)"
        ),
    )
    parser.add_argument("--parallel", type=int, default=1, help="concurrent runs (each gets its own git worktree)")
    parser.add_argument("--out", default="runs/minibench")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        force=True,
    )
    repo = Path(args.repo).resolve()
    if not (repo / ".git").is_dir():
        raise SystemExit(f"--repo {repo} is not a git worktree")
    tasks = load_tasks(Path(args.tasks), args.limit)
    arms = [a.strip() for a in args.arm.split(",") if a.strip()]
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    records_path = out_dir / "records.jsonl"

    done: set[tuple[str, str, int]] = set()
    if records_path.exists():
        for line in records_path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
                if rec.get("status") == "completed":
                    done.add((rec["instance_id"], rec["arm"], rec["seed"]))
            except (json.JSONDecodeError, KeyError):
                continue

    logger.info("mini-bench: %d task(s) × %s × %d seed(s) → %s", len(tasks), arms, args.seeds, out_dir)

    pending = []
    for seed in range(args.seeds):
        for arm in arms:
            for task in tasks:
                key = (task["id"], arm, seed)
                if key in done:
                    logger.info("skip (done): %s", key)
                    continue
                pending.append((key, task, arm, seed))

    n_workers = max(1, min(args.parallel, len(pending) or 1))
    worktrees = ensure_worktrees(repo, out_dir, n_workers)
    record_lock = threading.Lock()

    def run_one(wt: Path, task: dict, arm: str, seed: int, fh, lock) -> None:
        t_start = time.perf_counter()
        record = {
            "instance_id": task["id"],
            "arm": arm,
            "seed": seed,
            "cli": "opencode",
            "max_turns": args.max_turns,
            "status": "completed",
            "error": None,
            "usage": None,
            "cost_usd": None,
            "turns": 0,
            "patch_bytes": 0,
            "verify": None,
            "trail_recorded": False,
            "seconds": 0.0,
        }
        try:
            reset_repo(wt)
            arm_setup(wt, arm, task["statement"], args.model, args.budget, core=args.core)
            stdout, rc = run_agent(
                wt, task["statement"], args.model, args.timeout, max_turns=args.max_turns
            )
            streams_dir = out_dir / "streams"
            streams_dir.mkdir(exist_ok=True)
            (streams_dir / f"{task['id']}.{arm}.s{seed}.jsonl").write_text(stdout)
            usage = parse_usage("opencode", stdout.splitlines(), model=args.model)
            record["usage"] = usage.as_dict()
            record["turns"] = usage.turns
            record["cost_usd"] = None  # tokens are the cost metric
            if rc != 0:
                record["status"] = "cli_error"
                record["error"] = f"exit={rc}"
            patch = patch_extract(wt)
            record["patch_bytes"] = len(patch.encode("utf-8"))
            (out_dir / "patches").mkdir(exist_ok=True)
            (out_dir / "patches" / f"{task['id']}.{arm}.s{seed}.patch").write_text(patch)
            run_bookkeeping(
                wt,
                out_dir,
                task["id"],
                arm,
                seed,
                record,
                patch,
                verify_enabled=args.verify,
                trails_enabled=not args.no_trails,
            )
        except subprocess.TimeoutExpired:
            record["status"] = "timeout"
        except Exception as exc:  # per-run isolation
            record["status"] = "error"
            record["error"] = repr(exc)[:300]
            logger.exception("run failed: %s", (task["id"], arm, seed))
        finally:
            record["seconds"] = round(time.perf_counter() - t_start, 1)
            reset_repo(wt)
        with lock:
            fh.write(json.dumps(record) + "\n")
            fh.flush()
        logger.info(
            "done %s arm=%s seed=%d → %s in=%d out=%d turns=%d (%.0fs)",
            task["id"],
            arm,
            seed,
            record["status"],
            (record["usage"] or {}).get("input_tokens", 0),
            (record["usage"] or {}).get("output_tokens", 0),
            record["turns"],
            record["seconds"],
        )

    def worker(worker_index: int, assignment: list) -> None:
        wt = worktrees[worker_index]
        with records_path.open("a", encoding="utf-8") as fh:
            for key, task, arm, seed in assignment:
                logger.info("worker %d ← %s", worker_index, key)
                run_one(wt, task, arm, seed, fh, record_lock)

    if n_workers == 1:
        worker(0, pending)
    else:
        from concurrent.futures import ThreadPoolExecutor

        assignments = [pending[i::n_workers] for i in range(n_workers)]
        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            list(pool.map(worker, range(n_workers), assignments))
    records = [
        json.loads(line)
        for line in records_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    report = paired_report(records)
    md = render_markdown(report)
    (out_dir / "report.md").write_text(md, encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
