"""Verify probes: ready-made bounded verification scripts written at focus time.

Each edit-verify cycle in an agent run costs 2–8K tokens because the agent
*composes* a test command (variance, unbounded output, interpretation). The
probe replaces composition with execution: one call, output bounded by
construction, plus a STATE line that makes edit-verify loop repetition
visible in the agent's own context.

Double duty: the benchmark runs the same probe post-run for outcome gating
(benchmark-side — the CLI harness is never touched).
"""

from __future__ import annotations

from pathlib import Path

PROBE_DIR = ".trailmark/probes"


def probe_script(test_cmd: str, test_target: str = "") -> str:
    """The bounded verification script (deterministic content)."""
    target = f"  # target: {test_target}" if test_target else ""
    return (
        "#!/bin/sh\n"
        "# trailmark verify probe — one call, bounded output. Run after each edit.\n"
        'cd "$(dirname "$0")/../.." || exit 1\n'
        "echo '== DIFF =='\n"
        "git --no-pager diff --stat 2>/dev/null | tail -5\n"
        "echo '== TEST =='\n"
        f"{test_cmd} 2>&1 | tail -30{target}\n"
        "echo '== NEXT: update .trailmark/scratch.md (HYPOTHESIS / LAST RESULT) =='\n"
    )


def write_probe(root: str | Path, test_cmd: str, test_target: str = "") -> Path | None:
    """Write .trailmark/probes/verify.sh (idempotent). Returns the path or None."""
    root = Path(root)
    if not test_cmd:
        return None
    probe = root / PROBE_DIR / "verify.sh"
    content = probe_script(test_cmd, test_target)
    if probe.exists() and probe.read_text(encoding="utf-8") == content:
        return probe  # no-op write (cache-friendly)
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_text(content, encoding="utf-8")
    probe.chmod(0o755)
    return probe
