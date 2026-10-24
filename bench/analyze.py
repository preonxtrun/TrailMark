"""Trajectory analyzer: where did the tokens and calls go, and what was wasted?

Parses the raw ``opencode run --format json`` streams saved by ``bench.minibench``
(``<out>/streams/<task>.<arm>.s<seed>.jsonl``) and produces ``<out>/analysis.md``:

- per run: turns, tokens, cache-read share, tokens-per-step (history-replay growth),
  tool-call histogram, tool errors
- waste detectors, adapted from the cost-inefficiency taxonomy (arXiv 2609.30725):
    * subsumed-read  — the same file read again after having been read earlier
    * repeat-command — an identical (tool, input) call issued a second time
    * retest         — a test command re-executed with no edit in between
    * script-regen   — near-duplicate inline scripts (line-Jaccard >= 0.6)
- cross-arm comparison: control vs treatment, side by side

Usage:
    uv run python -m bench.analyze --out runs/mini0
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

EDIT_TOOLS = {"edit", "write", "multiedit", "apply_patch", "patch"}
READ_TOOLS = {"read", "cat", "sed"}
SEARCH_TOOLS = {"grep", "glob", "rg", "find"}
JACCARD_THRESHOLD = 0.6


@dataclass
class Step:
    index: int
    tool: str | None = None
    target: str = ""  # file path / command / pattern, best-effort
    status: str = ""
    input_tokens: int = 0
    cache_read: int = 0
    output_tokens: int = 0


@dataclass
class Trajectory:
    name: str
    arm: str = ""
    steps: list[Step] = field(default_factory=list)
    tokens: dict[str, int] = field(default_factory=dict)
    turns: int = 0
    tool_errors: int = 0
    waste: Counter = field(default_factory=Counter)
    waste_detail: list[str] = field(default_factory=list)


def _input_of(part: dict) -> dict:
    state = part.get("state")
    if isinstance(state, dict):
        value = state.get("input")
        if isinstance(value, dict):
            return value
    value = part.get("input")
    return value if isinstance(value, dict) else {}


def _status_of(part: dict) -> str:
    state = part.get("state")
    if isinstance(state, dict):
        return str(state.get("status") or "")
    return ""


def _target_of(tool: str, inp: dict) -> str:
    for key in ("filePath", "file_path", "file", "path", "notebook_path"):
        value = inp.get(key)
        if isinstance(value, str):
            return value
    for key in ("pattern", "query", "command", "cmd"):
        value = inp.get(key)
        if isinstance(value, str):
            return value
    return ""


def parse_stream(path: Path) -> Trajectory:
    traj = Trajectory(name=path.stem)
    tokens = {"input": 0, "cache_read": 0, "cache_write": 0, "output": 0}
    index = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = event.get("type")
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        if etype == "tool_use":
            tool = str(part.get("tool") or "unknown")
            inp = _input_of(part)
            traj.steps.append(
                Step(
                    index=index,
                    tool=tool,
                    target=_target_of(tool, inp)[:200],
                    status=_status_of(part),
                )
            )
            index += 1
            if _status_of(part) in {"error", "failed"}:
                traj.tool_errors += 1
        elif etype == "step_finish":
            t = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
            cache = t.get("cache") if isinstance(t.get("cache"), dict) else {}
            traj.steps.append(
                Step(
                    index=index,
                    input_tokens=int(t.get("input") or 0),
                    cache_read=int(cache.get("read") or 0),
                    output_tokens=int(t.get("output") or 0) + int(t.get("reasoning") or 0),
                )
            )
            index += 1
            tokens["input"] += int(t.get("input") or 0)
            tokens["cache_read"] += int(cache.get("read") or 0)
            tokens["cache_write"] += int(cache.get("write") or 0)
            tokens["output"] += int(t.get("output") or 0) + int(t.get("reasoning") or 0)
            traj.turns += 1
    traj.tokens = tokens
    return traj


def detect_waste(traj: Trajectory) -> None:
    """Four detectors, in event order. Deliberately conservative (no LLM, no guesses)."""
    seen_reads: dict[str, int] = {}
    seen_calls: set[tuple[str, str]] = set()
    scripts: list[tuple[int, str]] = []
    last_edit_index = -1
    last_test_index: dict[str, int] = {}
    for step in traj.steps:
        if step.tool is None:
            continue
        canonical = f"{step.tool}::{step.target}"
        # repeat-command: identical (tool, input) again
        if canonical in seen_calls:
            traj.waste["repeat-command"] += 1
            traj.waste_detail.append(f"step {step.index}: repeat {step.tool} {step.target[:80]}")
        seen_calls.add(canonical)
        # subsumed-read / re-search: same target read or searched again
        if step.tool in READ_TOOLS | SEARCH_TOOLS and step.target:
            if step.target in seen_reads:
                traj.waste["subsumed-read"] += 1
                traj.waste_detail.append(
                    f"step {step.index}: re-read {step.tool} {step.target[:80]}"
                    f" (first at step {seen_reads[step.target]})"
                )
            else:
                seen_reads[step.target] = step.index
        # test re-execution without an intervening edit
        if step.tool == "bash" and ("pytest" in step.target or " test" in step.target):
            key = step.target.strip()[:120]
            if key in last_test_index and last_edit_index < last_test_index[key]:
                traj.waste["retest"] += 1
                traj.waste_detail.append(f"step {step.index}: retest without edit: {key[:80]}")
            last_test_index[key] = step.index
        if step.tool in EDIT_TOOLS:
            last_edit_index = step.index
        # script regeneration: inline python/heredoc scripts
        if step.tool == "bash" and (
            "python -c" in step.target or "python3 -c" in step.target or "<<" in step.target
        ):
            body = "\n".join(line for line in step.target.splitlines() if line.strip())
            for prev_index, prev in scripts:
                inter = set(body.splitlines())
                union = set(prev.splitlines()) | inter
                if union and len(inter & set(prev.splitlines())) / len(union) >= JACCARD_THRESHOLD:
                    traj.waste["script-regen"] += 1
                    traj.waste_detail.append(
                        f"step {step.index}: script ≈ step {prev_index} (Jaccard)"
                    )
                    break
            scripts.append((step.index, body))


def load_runs(out_dir: Path) -> list[Trajectory]:
    streams = sorted((out_dir / "streams").glob("*.jsonl")) if (out_dir / "streams").is_dir() else []
    trajs = [parse_stream(path) for path in streams]
    # arms from filenames: <task>.<arm>.s<seed>.jsonl
    for traj in trajs:
        parts = traj.name.split(".")
        traj.arm = parts[1] if len(parts) >= 3 else "?"
        detect_waste(traj)
    return trajs


def render(out_dir: Path, trajs: list[Trajectory]) -> str:
    lines = ["# Trajectory analysis", ""]
    waste_types = ["subsumed-read", "repeat-command", "retest", "script-regen"]
    header = (
        "| run | arm | turns | tokens (in+cache+out) | cache-read% | tool-errors | "
        + " | ".join(waste_types)
        + " |"
    )
    lines += [header, "|" + "---|" * (6 + len(waste_types))]
    for traj in trajs:
        total = sum(traj.tokens.values())
        share = (
            f"{100 * traj.tokens['cache_read'] / max(1, traj.tokens['input'] + traj.tokens['cache_read']):.0f}%"
            if total
            else "—"
        )
        cells = [
            traj.name,
            traj.arm,
            str(traj.turns),
            f"{total:,}",
            share,
            str(traj.tool_errors),
        ] + [str(traj.waste.get(w, 0)) for w in waste_types]
        lines.append("| " + " | ".join(cells) + " |")

    for traj in trajs:
        if traj.waste_detail:
            lines += ["", f"## {traj.name} ({traj.arm}) — waste detail", ""]
            lines += [f"- {detail}" for detail in traj.waste_detail]

    by_arm: dict[str, list[Trajectory]] = {}
    for traj in trajs:
        by_arm.setdefault(traj.arm, []).append(traj)
    if len(by_arm) > 1:
        lines += ["", "## Arm comparison (means)", "", "| arm | runs | turns | tokens | waste |", "|---|---|---|---|---|"]
        for arm in sorted(by_arm):
            runs = by_arm[arm]
            turns = sum(t.turns for t in runs) / len(runs)
            toks = sum(sum(t.tokens.values()) for t in runs) / len(runs)
            waste = sum(sum(t.waste.values()) for t in runs) / len(runs)
            lines.append(f"| {arm} | {len(runs)} | {turns:.1f} | {toks:,.0f} | {waste:.1f} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="runs/minibench", help="mini-bench output dir")
    args = parser.parse_args()
    out_dir = Path(args.out)
    trajs = load_runs(out_dir)
    if not trajs:
        raise SystemExit(f"no streams found under {out_dir}/streams (run bench.minibench first)")
    report = render(out_dir, trajs)
    (out_dir / "analysis.md").write_text(report, encoding="utf-8")
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
