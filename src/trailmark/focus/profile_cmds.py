"""Profile-gated baseline snapshots: run `baseline:` commands before the agent.

A profile line `baseline: <command>` opts the repo in: at focus time the command
runs once (bounded), and its tail is stored at .trailmark/baseline.txt. The atlas
emits it as a BASELINE section, so the agent compares its own test runs against a
known-good baseline instead of establishing one (kills baseline-establishing runs).
Opt-in by construction (writing the profile line is the consent); failures skip.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

MAX_COMMANDS = 2
TAIL_LINES = 40
TIMEOUT = 300


def _profile_lines(root: Path) -> list[str]:
    path = Path(root) / ".trailmark" / "profile.txt"
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


def baseline_commands(root: str | Path) -> list[str]:
    cmds = []
    for line in _profile_lines(root):
        line = line.strip()
        if line.startswith("baseline:") and len(line) > 9:
            cmds.append(line[9:].strip())
    return cmds[:MAX_COMMANDS]


def run_baselines(root: str | Path) -> Path | None:
    """Run baseline commands (bounded), write .trailmark/baseline.txt."""
    root_path = Path(root)
    cmds = baseline_commands(root_path)
    if not cmds:
        return None
    sections = ["BASELINE (captured before your changes — report the delta):"]
    for cmd in cmds:
        try:
            proc = subprocess.run(
                cmd,
                shell=True,
                check=False,
                cwd=str(root_path),
                capture_output=True,
                text=True,
                errors="replace",
                timeout=TIMEOUT,
            )
            tail = (proc.stdout or "").splitlines()[-TAIL_LINES:]
            sections.append("$ " + cmd)
            sections.extend("  " + line for line in tail)
            sections.append("  exit=" + str(proc.returncode))
        except subprocess.TimeoutExpired:
            sections.append("$ " + cmd)
            sections.append("  (timed out after " + str(TIMEOUT) + "s)")
        except OSError as exc:
            sections.append("$ " + cmd)
            sections.append("  (failed: " + repr(exc) + ")")
    out = root_path / ".trailmark" / "baseline.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(sections) + "\n", encoding="utf-8")
    return out
