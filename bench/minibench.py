"""Local, docker-free A/B mini-bench for codegraph development iterations.

One real repo, N task statements, control vs treatment, tokens/turns delta.
This is the fast per-generation signal (minutes); the SWE-bench standard check
(``bench.run`` + ``bench.prepare_images``) stays available for occasional
verification on a small subset.

Protocol per (task, arm, seed):
    1. reset the repo worktree (git reset --hard + clean, preserving .codegraph)
    2. arm setup: control → strip the codegraph block; treatment → ``codegraph focus``
    3. run stock opencode headless in the repo; parse usage via bench.cost
    4. record patch (official exclude pathspec) + usage/turns

Usage:
    uv run python -m bench.minibench --repo /path/to/repo \
        --tasks bench/tasks/requests-5.jsonl --arm control,treatment \
        --seeds 1 --out runs/mini0
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import time
from pathlib import Path

from .arms import patch_cmd
from .cost import parse as parse_usage
from .report import paired_report, render_markdown
from .run import DEFAULT_MODEL

logger = logging.getLogger("bench.minibench")

RESET_EXCLUDES = ["-e", ".codegraph"]


def _git(repo: Path, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=False, capture_output=True, text=True, timeout=timeout
    )


def reset_repo(repo: Path) -> None:
    """Hard-reset the worktree, preserving .codegraph (untracked files cleaned)."""
    for args in (("reset", "--hard", "HEAD"), ("clean", "-fd", *RESET_EXCLUDES)):
        proc = _git(repo, *args)
        if proc.returncode != 0:
            logger.warning("git %s failed: %s", args[0], proc.stderr.strip()[-200:])


def patch_extract(repo: Path) -> str:
    proc = subprocess.run(
        patch_cmd(str(repo)), shell=True, check=False, capture_output=True, text=True, timeout=120
    )
    return proc.stdout if proc.returncode == 0 else ""


def arm_setup(repo: Path, arm: str, statement: str, model: str, budget: int) -> None:
    """control → strip the block; treatment → focus (in-process, warm)."""
    from codegraph.emit.agents_md import remove_block

    if arm == "control":
        remove_block(repo / "AGENTS.md")
        return
    if arm != "treatment":
        raise ValueError(f"unknown arm {arm!r}")
    from codegraph.cache import IndexCache
    from codegraph.focus import cli_focus

    cache = IndexCache(repo / ".codegraph")
    if cache.load() is None:
        from codegraph.index import update

        t0 = time.perf_counter()
        update(root=str(repo), langs=None, workers=8, cache=cache)
        logger.info("treatment init (cold): %.2fs", time.perf_counter() - t0)
    args = argparse.Namespace(
        root=str(repo), issue=statement, issue_file=None, cli="generic", budget=budget, json=False
    )
    rc = cli_focus.run(args)
    if rc != 0:
        raise RuntimeError(f"codegraph focus failed rc={rc}")
    # Protect the layer's footprint from the agent's own git hygiene (with --auto the
    # agent can run git clean; untracked .codegraph/ would be swept). .git/info/exclude
    # is local-only: no repo pollution, clean patches, and cleans stop deleting state.
    exclude = repo / ".git" / "info" / "exclude"
    try:
        current = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        needed = [entry for entry in (".codegraph/", "AGENTS.md") if entry not in current]
        if needed:
            exclude.parent.mkdir(parents=True, exist_ok=True)
            with exclude.open("a", encoding="utf-8") as fh:
                fh.write("\n# codegraph layer (local)\n" + "\n".join(needed) + "\n")
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


def run_agent(repo: Path, statement: str, model: str, timeout: int) -> tuple[str, int]:
    cmd = (
        f"opencode run --standalone --format json --auto"
        f" --model {json.dumps(model)} {json.dumps(statement)}"
    )
    stdout, _stderr, rc = run_with_timeout(cmd, str(repo), timeout)
    return stdout, rc


def load_tasks(tasks_file: Path, limit: int | None) -> list[dict]:
    tasks = [
        json.loads(line)
        for line in tasks_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    for i, task in enumerate(tasks):
        task.setdefault("id", f"task-{i}")
    return tasks[:limit] if limit else tasks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--tasks", default="bench/tasks/requests-5.jsonl")
    parser.add_argument("--arm", default="control,treatment")
    parser.add_argument("--seeds", type=int, default=1)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--budget", type=int, default=2048)
    parser.add_argument("--timeout", type=int, default=900, help="per agent run, seconds")
    parser.add_argument("--limit", type=int, default=None, help="first N tasks only")
    parser.add_argument("--out", default="runs/minibench")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

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

    with records_path.open("a", encoding="utf-8") as fh:
        for seed in range(args.seeds):
            for arm in arms:
                for task in tasks:
                    key = (task["id"], arm, seed)
                    if key in done:
                        logger.info("skip (done): %s", key)
                        continue
                    t_start = time.perf_counter()
                    record = {
                        "instance_id": task["id"],
                        "arm": arm,
                        "seed": seed,
                        "cli": "opencode",
                        "status": "completed",
                        "error": None,
                        "usage": None,
                        "cost_usd": None,
                        "turns": 0,
                        "patch_bytes": 0,
                        "seconds": 0.0,
                    }
                    try:
                        reset_repo(repo)
                        arm_setup(repo, arm, task["statement"], args.model, args.budget)
                        stdout, rc = run_agent(repo, task["statement"], args.model, args.timeout)
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
                        patch = patch_extract(repo)
                        record["patch_bytes"] = len(patch.encode("utf-8"))
                        (out_dir / "patches").mkdir(exist_ok=True)
                        (out_dir / "patches" / f"{task['id']}.{arm}.s{seed}.patch").write_text(patch)
                    except subprocess.TimeoutExpired:
                        record["status"] = "timeout"
                    except Exception as exc:  # per-run isolation
                        record["status"] = "error"
                        record["error"] = repr(exc)[:300]
                        logger.exception("run failed: %s", key)
                    finally:
                        record["seconds"] = round(time.perf_counter() - t_start, 1)
                        reset_repo(repo)
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
