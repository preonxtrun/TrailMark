"""Resumable, parallel SWE-bench Verified campaign runner (bench harness).

Standard pipeline, nothing else differs between arms (SPEC "bench/"):

    python -m bench.run --dataset princeton-nlp/SWE-bench_Verified --slice 0:100 \\
        --cli claude --arm control,treatment --seeds 3 --parallel 4 --out runs/demo [--grade] [--dry]

Per (instance, arm, seed): start the official image ``swebench/sweb.eval.x86_64.<id>``,
exec the arm's setup (treatment installs codegraph + runs ``init``/``focus``; control does
nothing), pipe the issue text (``problem_statement`` ONLY, leak-gated) to the stock
headless CLI, extract the patch with the exclude-pathspec, parse usage via ``bench.cost``,
and append a record. State is resumable: completed tasks are skipped on rerun.

Statuses: completed | cli_error (agent ran, non-zero exit) | timeout | skipped_timeout |
failed. Exit code 0 only when every run completed (failed runs are retried on rerun).

``--dry`` validates dataset/slice/arms, prints the exact per-instance plan, runs the
cost-adapter self-test on bench/fixtures, and never touches docker.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shlex
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .arms import (
    ISSUE_PATH,
    SETUP_CONTROL,
    SETUP_TREATMENT,
    LeakageError,
    assert_no_leakage,
    patch_cmd,
    write_issue_file,
)
from .cost import cost_usd, price_for
from .cost import parse as parse_usage
from .report import load_records

logger = logging.getLogger(__name__)

DEFAULT_DATASET = "princeton-nlp/SWE-bench_Verified"
DEFAULT_REPO_DIR = "/testbed"
DEFAULT_SRC_MOUNT = "/opt/codegraph-src"
DEFAULT_AGENT_TIMEOUT_S = 1800
IMAGE_REPO = "swebench/sweb.eval.x86_64"
SETUP_TIMEOUT_S = 600
PATCH_TIMEOUT_S = 300
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

#: Host env vars forwarded into the container (API keys/base URLs for the stock CLIs).
ENV_PASSTHROUGH: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_MODEL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
    "OPENCODE_API_KEY",
    "OPENROUTER_API_KEY",
    "MOONSHOT_API_KEY",
    "DEEPSEEK_API_KEY",
    "HF_TOKEN",
)

#: Headless agent invocation per CLI. The issue text is piped on stdin — equivalent to
#: passing "<problem>" as argv but safe for arbitrary multi-line text. CLI flags drift
#: between versions: verify these exact commands on a docker host before a real campaign
#: (see bench/README.md).
AGENTS: dict[str, str] = {
    "claude": "claude -p --output-format stream-json --max-turns 80",
    "codex": "codex exec --json -",
    "opencode": "opencode run --standalone --format json --auto",
}

#: Default agent model for CLIs whose invocation takes an explicit model (opencode).
#: Verified against `opencode run --help` (v2.1.4) and the models catalog.
DEFAULT_MODEL = "hyde-inference/hyde/glm-5.3-flash"


class CampaignError(RuntimeError):
    """Configuration/dataset problem that should abort the campaign with exit code 2."""


@dataclass(frozen=True)
class Task:
    """One (instance, arm, seed) run."""

    instance_id: str
    arm: str
    seed: int
    record: Mapping[str, object]  # full dataset record (leak-gated before any use)

    @property
    def problem_statement(self) -> str:
        """The ONLY task text handed to the agent (SPEC invariant #3)."""
        value = self.record.get("problem_statement")
        return value if isinstance(value, str) else ""


@dataclass(frozen=True)
class RunConfig:
    cli: str
    out_dir: Path
    repo_dir: str
    src_host: Path
    src_container: str
    timeout: int
    dataset: str
    model: str = DEFAULT_MODEL


# --------------------------------------------------------------------------
# dataset / plan
# --------------------------------------------------------------------------


def _parse_slice(spec: str, n: int) -> tuple[int, int]:
    """Parse ``'lo:hi'`` (0-based, hi-exclusive) against ``n`` records.

    Bounds are clamped to the available range (e.g. ``0:100`` over a 50-record
    instances file selects all 50); only a malformed spec raises.
    """
    match = re.fullmatch(r"(\d*):(\d*)", spec.strip())
    if not match:
        raise CampaignError(f"--slice must look like '0:100', got {spec!r}")
    lo = min(int(match.group(1)) if match.group(1) else 0, n)
    hi = min(int(match.group(2)) if match.group(2) else n, n)
    if lo > hi:
        raise CampaignError(f"--slice {spec!r} selects an empty range for {n} records")
    if hi < n:
        logger.debug("slice %s clamped to %d:%d of %d records", spec, lo, hi, n)
    return lo, hi


def load_instances(args: argparse.Namespace) -> list[dict]:
    """Load + validate + leak-gate the dataset slice.

    Source: ``--instances-file`` (JSONL with instance_id + problem_statement) when given,
    otherwise the HuggingFace dataset (lazy ``datasets`` import, optional extra). Records
    lacking instance_id/problem_statement are skipped; records failing the leakage gate
    are dropped loudly (fail-closed). Raises :class:`CampaignError` when nothing usable
    remains.
    """
    if args.instances_file:
        records: list[dict] = []
        with Path(args.instances_file).open(encoding="utf-8") as handle:
            for lineno, line in enumerate(handle, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise CampaignError(
                        f"{args.instances_file}:{lineno}: malformed JSON ({exc})"
                    ) from exc
                if not isinstance(obj, dict):
                    raise CampaignError(f"{args.instances_file}:{lineno}: not a JSON object")
                records.append(obj)
    else:
        try:
            from datasets import load_dataset  # lazy optional extra (swebench/datasets)
        except ImportError as exc:
            raise CampaignError(
                "--dataset needs the datasets extra: pip install -e '.[bench]' "
                "(or pass --instances-file)"
            ) from exc
        logger.info("loading dataset %s (split=test) via HF datasets ...", args.dataset)
        try:
            dataset = load_dataset(args.dataset, split="test")
        except Exception as exc:
            raise CampaignError(f"failed to load dataset {args.dataset}: {exc}") from exc
        records = [dict(row) for row in dataset]

    lo, hi = _parse_slice(args.slice, len(records))
    sliced = records[lo:hi]
    clean: list[dict] = []
    for record in sliced:
        instance_id = record.get("instance_id")
        statement = record.get("problem_statement")
        if not isinstance(instance_id, str) or not instance_id.strip():
            logger.warning("skipping record without instance_id: %r", instance_id)
            continue
        if not isinstance(statement, str) or not statement.strip():
            logger.warning("skipping %s — no problem_statement", instance_id)
            continue
        try:
            assert_no_leakage(record)
        except LeakageError as exc:
            logger.error("dropping %s — leakage gate: %s", instance_id, exc)
            continue
        clean.append(record)
    if sliced and not clean:
        raise CampaignError(
            "every record in the slice failed validation/leakage gating — nothing to run"
        )
    return clean


def build_tasks(instances: Sequence[Mapping[str, object]], arms: Sequence[str], seeds: int) -> list[Task]:
    """Deterministic plan: sorted by (instance_id, arm, seed)."""
    tasks: list[Task] = []
    for instance in sorted(instances, key=lambda r: str(r.get("instance_id"))):
        for arm in arms:
            for seed in range(seeds):
                tasks.append(
                    Task(
                        instance_id=str(instance["instance_id"]),
                        arm=arm,
                        seed=seed,
                        record=instance,
                    )
                )
    return tasks


def _parse_arms(spec: str) -> list[str]:
    """Validate the ``--arm`` list; canonical order is control first."""
    arms = [arm.strip() for arm in spec.split(",") if arm.strip()]
    unknown = [arm for arm in arms if arm not in ("control", "treatment")]
    if unknown:
        raise CampaignError(f"unknown arm(s) {unknown} — choose from control,treatment")
    if not arms:
        raise CampaignError("no arms requested")
    return sorted(dict.fromkeys(arms), key=lambda arm: (arm != "control", arm))


# --------------------------------------------------------------------------
# docker plumbing (lazy — nothing here runs without docker)
# --------------------------------------------------------------------------


def _alnum(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def image_tag(instance_id: str) -> str:
    """Docker tag derived from an instance id.

    Current SWE-bench releases build instance images as
    ``swebench/sweb.eval.x86_64.<instance_id>:latest`` with the instance id verbatim
    (lowercased, docker-tag-safe), e.g. ``swebench/sweb.eval.x86_64.django__django-11099``.
    Older releases mangled the repo separator (``__`` → ``_117_``/``_1777_``-style), so
    :func:`resolve_image` cross-checks ``docker images`` at runtime and matches any of
    those variants by normalized comparison.
    """
    stem = instance_id.strip().lower().replace("/", "__")
    tag = re.sub(r"[^a-z0-9_.-]+", "_", stem)
    return tag.lstrip("-.") or "unknown"


@lru_cache(maxsize=4096)
def resolve_image(instance_id: str) -> str:
    """Best-effort local image resolution for an instance.

    Primary candidate follows the current harness convention (see :func:`image_tag`).
    When a docker daemon is reachable, ``docker images`` is queried for any local tag
    containing the instance prefix (matched on alphanumeric-normalized forms, which
    absorbs the ``__``/``_117_``/``_1777_`` separator drift between swebench releases);
    the matched image wins. Without docker (or without a match) the primary candidate is
    returned and a missing image surfaces as a normal ``docker run`` error.
    """
    primary = f"{IMAGE_REPO}.{image_tag(instance_id)}:latest"
    try:
        proc = subprocess.run(
            ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        images = proc.stdout.split() if proc.returncode == 0 else []
    except (OSError, subprocess.SubprocessError):
        images = []
    if not images:
        return primary
    prefix = "sweb.eval.x86_64."
    want = _alnum(instance_id)
    matches = sorted(
        image
        for image in images
        if prefix in image and _alnum(image.split(prefix, 1)[1].rsplit(":", 1)[0]) == want
    )
    if not matches:
        logger.warning("no local image matched %s — using candidate %s", instance_id, primary)
        return primary
    if len(matches) > 1:
        logger.warning("multiple local images match %s — using %s", instance_id, matches[0])
    return matches[0]


_DOCKER_LOCK = threading.Lock()
_DOCKER_AVAILABLE: bool | None = None


def docker_available(timeout: float = 20.0) -> bool:
    """Lazy one-shot ``docker info`` probe, cached process-wide."""
    global _DOCKER_AVAILABLE
    with _DOCKER_LOCK:
        if _DOCKER_AVAILABLE is None:
            try:
                proc = subprocess.run(
                    ["docker", "info", "--format", "{{.ServerVersion}}"],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
                _DOCKER_AVAILABLE = proc.returncode == 0
            except (OSError, subprocess.SubprocessError):
                _DOCKER_AVAILABLE = False
            if not _DOCKER_AVAILABLE:
                logger.error("docker is not usable — start it or rerun with --dry")
        return _DOCKER_AVAILABLE


def _exec(
    name: str,
    shell_cmd: str,
    *,
    timeout: float,
    workdir: str | None = None,
    stdin_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """``docker exec sh -lc <shell_cmd>`` with output capture and a hard timeout."""
    cmd = ["docker", "exec"]
    if stdin_text is not None:
        cmd.append("-i")
    if workdir:
        cmd += ["-w", workdir]
    cmd += [name, "sh", "-lc", shell_cmd]
    return subprocess.run(
        cmd,
        input=stdin_text,
        check=False,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
    )


def _docker_run(image: str, name: str, cfg: RunConfig) -> None:
    """Start the detached eval container with the source tree mounted."""
    cmd = [
        "docker",
        "run",
        "-d",
        "--name",
        name,
        "-w",
        cfg.repo_dir,
        "-v",
        f"{cfg.src_host}:{cfg.src_container}",
    ]
    if cfg.cli == "opencode":
        # The agent inside the container needs the host's opencode auth/config
        # (provider credentials live in the host user's opencode state dir).
        # Read-only mounts, each skipped when absent on the host. Images run as
        # root, so the container side is /root.
        for host_rel, container_path in (
            (".local/share/opencode", "/root/.local/share/opencode"),
            (".config/opencode", "/root/.config/opencode"),
        ):
            host_dir = Path.home() / host_rel
            if host_dir.is_dir():
                cmd += ["-v", f"{host_dir}:{container_path}:ro"]
    for key in ENV_PASSTHROUGH:
        if os.environ.get(key):
            cmd += ["-e", key]
    # the container self-terminates shortly after the agent budget even if something hangs
    cmd += [image, "sleep", str(cfg.timeout + 120)]
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        raise RuntimeError(f"docker run failed: {(proc.stderr or proc.stdout).strip()[-300:]}")


def _docker_cp(local: Path, name: str, remote: str) -> None:
    proc = subprocess.run(
        ["docker", "cp", str(local), f"{name}:{remote}"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"docker cp failed: {proc.stderr.strip()[-200:]}")


# --------------------------------------------------------------------------
# per-run state machine
# --------------------------------------------------------------------------


def container_name(run_name: str, task: Task) -> str:
    """Docker-safe container name, deterministic per (run, instance, arm, seed)."""
    raw = f"cg-{run_name}-{task.instance_id}-{task.arm}-s{task.seed}"
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", raw)[:100]


def _fallback_record(task: Task, cli: str, exc: Exception) -> dict[str, object]:
    return {
        "instance_id": task.instance_id,
        "arm": task.arm,
        "seed": task.seed,
        "cli": cli,
        "usage": None,
        "cost_usd": None,
        "turns": 0,
        "patch_bytes": 0,
        "status": "failed",
        "error": f"{type(exc).__name__}: {exc}"[:500],
    }


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _save_patch(out_dir: Path, task: Task, patch: str) -> Path:
    path = out_dir / "patches" / f"{task.instance_id}.{task.arm}.s{task.seed}.patch"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(patch, encoding="utf-8")
    return path


def _extract_patch(name: str, cfg: RunConfig) -> str:
    """Best-effort patch extraction; returns '' (with a warning) on any failure."""
    try:
        proc = _exec(
            name, patch_cmd(cfg.repo_dir), timeout=PATCH_TIMEOUT_S, workdir=cfg.repo_dir
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("patch extraction error for %s: %s", name, exc)
        return ""
    if proc.returncode != 0:
        logger.warning(
            "patch extraction failed rc=%s: %s", proc.returncode, proc.stderr.strip()[-200:]
        )
        return ""
    return proc.stdout


def run_task(task: Task, cfg: RunConfig) -> dict[str, object]:
    """Run one (instance, arm, seed) in its own container; never raises."""
    record: dict[str, object] = {
        "instance_id": task.instance_id,
        "arm": task.arm,
        "seed": task.seed,
        "cli": cfg.cli,
        "usage": None,
        "cost_usd": None,
        "turns": 0,
        "patch_bytes": 0,
        "status": "failed",
        "error": None,
    }
    name = container_name(cfg.out_dir.name, task)
    started = False
    try:
        image = resolve_image(task.instance_id)
        _docker_run(image, name, cfg)
        started = True

        # Leakage gate: the ONLY task text that leaves this process is problem_statement
        # (single call site below); the gate raises before anything is written.
        assert_no_leakage(task.record)
        issue_text = task.problem_statement
        issue_file = write_issue_file(
            issue_text, dest_dir=cfg.out_dir / "issues", record=task.record
        )
        try:
            _docker_cp(issue_file, name, ISSUE_PATH)
        finally:
            issue_file.unlink(missing_ok=True)

        steps = (
            SETUP_TREATMENT(cfg.cli, cfg.repo_dir, cfg.src_container)
            if task.arm == "treatment"
            else SETUP_CONTROL
        )
        for step in steps:
            proc = _exec(name, step, timeout=SETUP_TIMEOUT_S, workdir=cfg.repo_dir)
            if proc.returncode != 0:
                raise RuntimeError(
                    f"setup step failed rc={proc.returncode}: {step} :: "
                    f"{proc.stderr.strip()[-300:]}"
                )

        stdout = ""
        status, error = "completed", None
        try:
            agent_cmd = AGENTS[cfg.cli]
            stdin_text = issue_text
            if cfg.cli == "opencode":
                # Verified invocation (opencode v2.1.4): the issue is a positional
                # argument (multi-line safe via shlex), the JSON stream carries
                # per-step usage in step_finish events. --auto approves tool
                # permissions for unattended runs.
                agent_cmd = (
                    f"{agent_cmd} --model {shlex.quote(cfg.model)}"
                    f" {shlex.quote(issue_text or '')}"
                )
                stdin_text = None
            proc = _exec(
                name,
                agent_cmd,
                timeout=cfg.timeout,
                workdir=cfg.repo_dir,
                stdin_text=stdin_text,
            )
            stdout = proc.stdout
            if proc.returncode != 0:
                status = "cli_error"
                error = f"exit={proc.returncode} stderr={proc.stderr.strip()[-300:]}"
        except subprocess.TimeoutExpired as exc:
            stdout = _as_text(exc.stdout)
            status = "timeout"
            error = f"agent exceeded {cfg.timeout}s timeout"

        usage = parse_usage(
            cfg.cli,
            stdout.splitlines(),
            model=cfg.model if cfg.cli == "opencode" else None,
        )
        record["usage"] = usage.as_dict()
        record["cost_usd"] = cost_usd(usage)
        record["turns"] = usage.turns
        patch = _extract_patch(name, cfg)
        record["patch_bytes"] = len(patch.encode("utf-8"))
        _save_patch(cfg.out_dir, task, patch)
        record["status"], record["error"] = status, error
        return record
    except Exception as exc:  # noqa: BLE001 — per-worker isolation: never propagate
        if record["status"] != "timeout":
            record["status"] = "failed"
        record["error"] = f"{type(exc).__name__}: {exc}"[:500]
        return record
    finally:
        if started:
            try:
                subprocess.run(
                    ["docker", "rm", "-f", name],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                logger.warning("could not remove container %s — remove it manually", name)


# --------------------------------------------------------------------------
# persistence: resumable state + append-only records
# --------------------------------------------------------------------------


class State:
    """Resume-safe run state persisted as sorted JSON (atomic tmp+replace writes)."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, object]] = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                self._data = loaded if isinstance(loaded, dict) else {}
            except (OSError, json.JSONDecodeError):
                logger.warning("unreadable state file %s — starting fresh", path)
                self._data = {}

    @staticmethod
    def key(instance_id: str, arm: str, seed: int) -> str:
        return f"{instance_id}|{arm}|{seed}"

    def status(self, key: str) -> str | None:
        entry = self._data.get(key)
        return entry.get("status") if isinstance(entry, dict) else None

    def set(self, key: str, status: str, error: str | None) -> None:
        with self._lock:
            self._data[key] = {"status": status, "error": error}
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._data, sort_keys=True, indent=1) + "\n", encoding="utf-8")
            os.replace(tmp, self._path)


class RecordWriter:
    """Thread-safe append-only JSONL writer (sorted keys, deterministic bytes)."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def write(self, record: Mapping[str, object]) -> None:
        line = json.dumps(record, sort_keys=True) + "\n"
        with self._lock, self._path.open("a", encoding="utf-8") as handle:
            handle.write(line)


# --------------------------------------------------------------------------
# grading
# --------------------------------------------------------------------------


def _print_manual_grading(predictions_path: Path, run_id: str, args: argparse.Namespace) -> None:
    """Print the exact manual grading commands (allowed print: grade fallback)."""
    print(
        "grade manually with either:\n"
        f"  sb-cli submit swebench-verified {run_id} {predictions_path}\n"
        "  python -m swebench.harness.run_evaluation"
        f" --dataset_name {args.dataset} --predictions_path {predictions_path}"
        f" --run_id {run_id} --max_workers {args.parallel}"
    )


def _collect_graded(out_dir: Path, run_id: str, arm: str) -> None:
    """Best-effort merge of swebench per-instance reports into graded.<arm>.json."""
    resolved: dict[str, bool] = {}
    for base in (Path("report") / run_id, Path("reports") / run_id, out_dir / run_id):
        if not base.is_dir():
            continue
        for report_file in sorted(base.glob("*.json")):
            try:
                data = json.loads(report_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            value = data.get("resolved")
            if isinstance(value, (bool, int)) and not isinstance(value, bool) and value not in (0, 1):
                continue
            if isinstance(value, (bool, int)):
                instance_id = data.get("instance_id")
                resolved[str(instance_id) if instance_id else report_file.stem] = bool(value)
    if not resolved:
        logger.info(
            "no report files found for %s — collect results via sb-cli or the swebench logs dir",
            run_id,
        )
        return
    target = out_dir / f"graded.{arm}.json"
    target.write_text(json.dumps(resolved, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    logger.info(
        "graded %s: %d/%d resolved (written to %s)",
        run_id,
        sum(resolved.values()),
        len(resolved),
        target,
    )


def _grade(out_dir: Path, args: argparse.Namespace) -> None:
    """Write per-(arm, seed) predictions files and grade them via swebench when available.

    Deviation from a single predictions.jsonl: paired control-vs-treatment resolve%
    requires grading BOTH arms, so one predictions file per (arm, seed) is written
    (predictions.<arm>.s<seed>.jsonl); report.py consumes the graded.<arm>.json outputs.
    """
    records = load_records(out_dir / "records.jsonl")
    by_arm_seed: dict[tuple[str, int], list[dict]] = {}
    for record in records:
        if record.get("status") != "completed":
            continue
        key = (str(record.get("arm")), int(record.get("seed", 0)))
        by_arm_seed.setdefault(key, []).append(record)
    if not by_arm_seed:
        logger.error("--grade: no completed runs to grade")
        return
    for (arm, seed), arm_records in sorted(by_arm_seed.items()):
        instance_ids = sorted({str(record["instance_id"]) for record in arm_records})
        cli = str(min(str(record.get("cli", args.cli)) for record in arm_records))
        predictions = []
        for instance_id in instance_ids:
            patch_file = out_dir / "patches" / f"{instance_id}.{arm}.s{seed}.patch"
            patch = (
                patch_file.read_text(encoding="utf-8", errors="replace")
                if patch_file.exists()
                else ""
            )
            predictions.append(
                {
                    "instance_id": instance_id,
                    "model_patch": patch,
                    "model_name_or_path": cli if cli != "opencode" else DEFAULT_MODEL,
                }
            )
        path = out_dir / f"predictions.{arm}.s{seed}.jsonl"
        path.write_text(
            "".join(json.dumps(p, sort_keys=True) + "\n" for p in predictions),
            encoding="utf-8",
        )
        run_id = f"{out_dir.name}-{arm}-s{seed}"
        logger.info("wrote %s (%d predictions, run_id=%s)", path, len(predictions), run_id)
        try:
            from swebench.harness.run_evaluation import run_evaluation  # lazy optional extra
        except Exception:  # noqa: BLE001 — swebench may be absent or broken; degrade
            run_evaluation = None
        if run_evaluation is None:
            _print_manual_grading(path, run_id, args)
            continue
        try:
            run_evaluation(
                dataset_name=args.dataset,
                predictions_path=str(path),
                instance_ids=instance_ids,
                run_id=run_id,
            )
        except Exception as exc:  # noqa: BLE001 — grading must not kill the campaign
            logger.error("swebench run_evaluation failed (%s) — grade manually:", exc)
            _print_manual_grading(path, run_id, args)
        else:
            _collect_graded(out_dir, run_id, arm)


# --------------------------------------------------------------------------
# dry run / campaign
# --------------------------------------------------------------------------


def _default_src() -> Path | None:
    """Repo root hosting the codegraph project (mounted into containers)."""
    src = Path(__file__).resolve().parents[1]
    return src if (src / "pyproject.toml").is_file() else None


def _fixture_selftest() -> int:
    """Parse bench/fixtures/*.jsonl with each adapter; logs one line per CLI."""
    bad = 0
    for cli in ("claude", "codex", "opencode"):
        path = FIXTURES_DIR / f"{cli}.jsonl"
        if not path.is_file():
            logger.error("missing fixture %s", path)
            bad += 1
            continue
        usage = parse_usage(cli, path.read_text(encoding="utf-8").splitlines())
        logger.info(
            "selftest %-8s model=%s in=%d cr=%d cw=%d out=%d turns=%d estimated=%s "
            "cost=$%.6f (price=%s)",
            cli,
            usage.model,
            usage.input_tokens,
            usage.cache_read_tokens,
            usage.cache_write_tokens,
            usage.output_tokens,
            usage.turns,
            usage.estimated,
            cost_usd(usage),
            price_for(usage.model),
        )
        if usage.total_tokens <= 0 or usage.estimated:
            logger.error("fixture %s did not parse to real usage", path)
            bad += 1
    return bad


def _dry(args: argparse.Namespace, tasks: Sequence[Task]) -> int:
    """Validate config, print the exact plan, self-test cost adapters. No docker."""
    logger.info("DRY RUN — no docker, no agents, no files written")
    if args.grade:
        logger.warning("--grade is ignored with --dry")
    src = Path(args.src_mount).resolve() if args.src_mount else _default_src()
    if src is None:
        logger.warning("could not locate the codegraph source to mount — pass --src-mount")
    for task in tasks:
        setup_steps = (
            len(SETUP_TREATMENT(args.cli, args.repo_dir, DEFAULT_SRC_MOUNT))
            if task.arm == "treatment"
            else len(SETUP_CONTROL)
        )
        logger.info(
            "plan %s | arm=%-9s | seed=%d | image=%s | setup_steps=%d",
            task.instance_id,
            task.arm,
            task.seed,
            f"{IMAGE_REPO}.{image_tag(task.instance_id)}:latest",
            setup_steps,
        )
    bad = _fixture_selftest()
    logger.info(
        "dry plan: %d run(s) | cli=%s | timeout=%ss | parallel=%d | src_mount=%s | out=%s",
        len(tasks),
        args.cli,
        args.timeout,
        args.parallel,
        src,
        args.out,
    )
    return 0 if bad == 0 else 1


def _campaign(args: argparse.Namespace, tasks: Sequence[Task]) -> int:
    if not tasks:
        raise CampaignError("nothing to run — empty plan")
    src = Path(args.src_mount).resolve() if args.src_mount else _default_src()
    if src is None or not (src / "pyproject.toml").is_file():
        raise CampaignError(
            f"codegraph source for mounting not found (pass --src-mount); got {src}"
        )
    if not docker_available():
        return 2
    cfg = RunConfig(
        cli=args.cli,
        out_dir=Path(args.out),
        repo_dir=args.repo_dir,
        src_host=src,
        src_container=DEFAULT_SRC_MOUNT,
        timeout=max(60, args.timeout),
        dataset=args.dataset,
        model=args.model or DEFAULT_MODEL,
    )
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    state = State(cfg.out_dir / "state.json")
    pending = [
        task
        for task in tasks
        if state.status(State.key(task.instance_id, task.arm, task.seed)) != "completed"
    ]
    logger.info(
        "campaign: %d planned | %d already completed (resume) | %d to run | out=%s",
        len(tasks),
        len(tasks) - len(pending),
        len(pending),
        cfg.out_dir,
    )
    if not pending:
        logger.info("nothing to run — rerun with --grade or a different --out")
        if args.grade:
            _grade(cfg.out_dir, args)
        return 0
    writer = RecordWriter(cfg.out_dir / "records.jsonl")
    failures = 0
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, args.parallel)) as pool:
        futures = {pool.submit(run_task, task, cfg): task for task in pending}
        try:
            for future in as_completed(futures, timeout=args.global_timeout or None):
                task = futures[future]
                try:
                    record = future.result()
                except Exception as exc:  # noqa: BLE001 — belt-and-braces isolation
                    record = _fallback_record(task, cfg.cli, exc)
                writer.write(record)
                state.set(
                    State.key(str(record["instance_id"]), str(record["arm"]), int(record["seed"])),
                    str(record["status"]),
                    record["error"] if isinstance(record["error"], str) else None,
                )
                if record["status"] != "completed":
                    failures += 1
                logger.info(
                    "done %s arm=%s seed=%s → %s (cost=%s, patch=%sB)",
                    record["instance_id"],
                    record["arm"],
                    record["seed"],
                    record["status"],
                    f"${record['cost_usd']:.6f}" if isinstance(record["cost_usd"], float) else "n/a",
                    record["patch_bytes"],
                )
        except TimeoutError:
            logger.error("global timeout (%ss) hit — cancelling remaining runs", args.global_timeout)
            for future, task in futures.items():
                if future.cancel():
                    state.set(
                        State.key(task.instance_id, task.arm, task.seed),
                        "skipped_timeout",
                        None,
                    )
                    failures += 1
    logger.info(
        "campaign done in %.0fs | %d ok | %d not completed",
        time.monotonic() - started,
        len(pending) - failures,
        failures,
    )
    if args.grade:
        _grade(cfg.out_dir, args)
    return 0 if failures == 0 else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bench.run",
        description=(
            "Resumable, parallel SWE-bench Verified campaign: stock headless CLI inside "
            "official images, control vs treatment arm (one extra setup step)."
        ),
    )
    parser.add_argument("--dataset", default=DEFAULT_DATASET, help="HF dataset name (lazy datasets import)")
    parser.add_argument(
        "--instances-file",
        default=None,
        help="JSONL with instance_id + problem_statement; overrides --dataset",
    )
    parser.add_argument("--slice", default="0:100", help="dataset slice 'lo:hi' (0-based, hi-exclusive)")
    parser.add_argument("--cli", default="claude", choices=sorted(AGENTS), help="headless coding CLI under test")
    parser.add_argument("--arm", default="control,treatment", help="comma-separated: control,treatment")
    parser.add_argument(
        "--seeds",
        type=int,
        default=1,
        help="repeat runs per (instance, arm) — labels for variance; stock CLIs are not seeded",
    )
    parser.add_argument("--parallel", type=int, default=4, help="concurrent docker runs")
    parser.add_argument("--out", default="runs/bench", help="output dir (state.json, records.jsonl, patches/)")
    parser.add_argument(
        "--timeout", type=int, default=DEFAULT_AGENT_TIMEOUT_S, help="per-run agent timeout (seconds)"
    )
    parser.add_argument(
        "--global-timeout", type=int, default=0, help="wall-clock cap for the whole campaign (0 = off)"
    )
    parser.add_argument("--repo-dir", default=DEFAULT_REPO_DIR, help="repo path inside the container")
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help="agent model id (used for opencode --model and records; default: hyde-inference/hyde/glm-5.3-flash)",
    )
    parser.add_argument(
        "--src-mount",
        default=None,
        help=f"host path to the codegraph project (mounted at {DEFAULT_SRC_MOUNT}); default: this repo",
    )
    parser.add_argument("--grade", action="store_true", help="run official grading afterwards (swebench extra)")
    parser.add_argument("--dry", action="store_true", help="validate config + plan + cost fixtures; no docker")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        force=True,
    )
    try:
        arms = _parse_arms(args.arm)
        instances = load_instances(args)
        tasks = build_tasks(instances, arms, args.seeds)
        if args.dry:
            return _dry(args, tasks)
        return _campaign(args, tasks)
    except CampaignError as exc:
        logger.error("%s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
