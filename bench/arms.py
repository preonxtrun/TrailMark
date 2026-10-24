"""Arm definitions, issue-file writing, and leakage gates for the bench harness.

Standard SWE-bench pipeline, treatment-vs-control:

- CONTROL: stock official image, stock CLI. ``SETUP_CONTROL`` is empty.
- TREATMENT: identical run plus ONE extra setup step inside the container — install
  codegraph from the mounted source tree, ``codegraph init``, ``codegraph focus`` with the
  issue text. Pure files under the repo (``.codegraph/`` + a marked block in
  ``AGENTS.md``); the CLI itself is never modified (SPEC invariant #5).

The ONLY task text that ever reaches the agent is the dataset's ``problem_statement``.
:func:`assert_no_leakage` gates every issue-file write and the plan itself, raising
:class:`LeakageError` when the issue text embeds grading secrets (FAIL_TO_PASS /
PASS_TO_PASS / gold-patch material) — see its docstring for the exact policy.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import tempfile
from collections.abc import Mapping
from pathlib import Path

logger = logging.getLogger(__name__)

#: Path of the issue file written inside the container (SETUP_TREATMENT and run.py agree).
ISSUE_PATH = "/tmp/issue.txt"

#: Injected-block budget passed to ``codegraph init`` (bytes; SPEC default 2048).
BUDGET_BYTES = 2048

#: Patch pathspec: everything staged EXCEPT codegraph's own footprint (SPEC bench rules).
PATCH_PATHSPEC = "-- . ':(exclude).codegraph' ':(exclude)AGENTS.md'"

#: Fields whose values must never appear (even partially) in the issue text. Checked both
#: lower-case and UPPER-case because SWE-bench records use either convention.
_FORBIDDEN_FIELDS: tuple[str, ...] = (
    "fail_to_pass",
    "pass_to_pass",
    "patch",
    "gold_patch",
    "golden_patch",
    "test_patch",
)

#: Literal key markers that must never appear in the issue text (case-insensitive).
_KEY_MARKERS: tuple[str, ...] = (
    "fail_to_pass",
    "pass_to_pass",
    "gold_patch",
    "gold-patch",
    "test_patch",
)

#: Lines/elements shorter than this are exempt from chunk-overlap checks: a problem
#: statement may naturally mention a short test id, but a >=40-char verbatim line is a
#: copied secret.
LEAK_CHUNK_MIN = 40


class LeakageError(RuntimeError):
    """Raised when the issue text embeds grading secrets (SPEC invariant #3)."""


def SETUP_TREATMENT(
    cli: str, repo_dir: str, src_mount: str, bin_mount: str | None = None
) -> list[str]:
    """Docker-exec shell steps for the treatment arm, in order.

    1. install codegraph: when *bin_mount* is given (path of a prebuilt
       ``codegraph`` binary inside the container, e.g. the Rust release build
       bind-mounted by run.py), it is copied onto PATH — no pip/uv, no Python
       needed in the image. Otherwise codegraph is installed from the mounted
       Python source (``pip install -e`` with a ``uv tool install`` fallback;
       assumes the image has a build backend and network),
    2. ``codegraph init`` — atlas-only block, deterministic, byte-budgeted,
    3. ``codegraph focus`` — full block (atlas + PLAN) regenerated from cache + issue.

    The issue file at :data:`ISSUE_PATH` must exist before step 3 (run.py copies it into
    the container before executing these steps). Each string is run via
    ``docker exec <container> sh -lc <step>``.
    """
    src = shlex.quote(src_mount)
    repo = shlex.quote(repo_dir)
    cli_quoted = shlex.quote(cli)
    if bin_mount:
        bin_src = shlex.quote(bin_mount)
        install = f"install -m 0755 {bin_src} /usr/local/bin/codegraph"
    else:
        install = f"pip install --quiet -e {src} || uv tool install --force {src}"
    return [
        install,
        f"codegraph init --root {repo} --budget {BUDGET_BYTES} --json",
        f"codegraph focus --issue-file {ISSUE_PATH} --cli {cli_quoted} --root {repo} --json",
    ]


#: Control arm: stock container, zero extra steps.
SETUP_CONTROL: list[str] = []


def _added_lines(diff: str) -> list[str]:
    """``+`` lines of a unified diff, stripped of the marker and whitespace.

    Diff CONTEXT lines are pre-existing repository content (public), so only added lines
    — the actual solution — are treated as secret.
    """
    out = []
    for line in diff.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("+"):
            out.append(line[1:].strip())
    return out


def _leak_chunks(value: object) -> list[str]:
    """Secret strings derived from a forbidden field value.

    The whole serialized value is always checked (any length — nobody accidentally writes
    an exact JSON test list into an issue); individual diff-added lines and list elements
    are checked only at >= :data:`LEAK_CHUNK_MIN` chars so naturally-occurring short test
    ids in an issue do not false-positive.
    """
    if isinstance(value, (list, tuple)):
        whole = json.dumps(value, sort_keys=True)
        parts = [str(item).strip() for item in value]
    elif isinstance(value, str):
        whole = value
        lines = value.splitlines()
        looks_like_diff = any(
            line[:1] in "+-" and not line.startswith(("+++", "---")) for line in lines
        )
        parts = _added_lines(value) if looks_like_diff else [line.strip() for line in lines]
    else:
        whole = json.dumps(value, sort_keys=True)
        parts = []
    chunks = [whole.strip()]
    chunks += [part for part in parts if len(part) >= LEAK_CHUNK_MIN]
    return [chunk for chunk in chunks if chunk]


def assert_no_leakage(record: Mapping[str, object]) -> None:
    """Raise :class:`LeakageError` unless the issue text is safe to show the agent.

    Policy (SPEC invariant #3 — the only task input is the problem statement):

    1. the issue text must not contain literal grading key names (``FAIL_TO_PASS``,
       ``PASS_TO_PASS``, gold/test-patch markers; case-insensitive),
    2. for every forbidden field present on the record (``fail_to_pass``,
       ``pass_to_pass``, ``patch``, ``gold_patch``, ``golden_patch``, ``test_patch``,
       matched lower- or UPPER-case): neither the whole serialized value nor any
       diff-added line / list element of >= :data:`LEAK_CHUNK_MIN` chars may appear
       verbatim in the issue text.

    Records without ``problem_statement`` (nothing to show) pass trivially.
    """
    text = record.get("problem_statement")
    if not isinstance(text, str) or not text.strip():
        return
    lowered = text.lower()
    for marker in _KEY_MARKERS:
        if marker in lowered:
            raise LeakageError(f"issue text mentions grading key {marker!r}")
    for field in _FORBIDDEN_FIELDS:
        value = record.get(field)
        if value is None:
            value = record.get(field.upper())
        if value is None:
            continue
        for chunk in _leak_chunks(value):
            if chunk in text:
                raise LeakageError(
                    f"issue text overlaps forbidden field {field!r}: {chunk[:60]!r}"
                )


def write_issue_file(
    issue_text: str,
    dest_dir: str | Path | None = None,
    record: Mapping[str, object] | None = None,
) -> Path:
    """Persist the issue text to a file for ``docker cp`` into the container.

    ``issue_text`` must be derived from ``problem_statement`` ONLY (bench/run.py holds the
    single call site). When ``record`` is supplied, :func:`assert_no_leakage` gates the
    write. Returns the created file path (the caller may delete it afterwards).
    """
    if record is not None:
        assert_no_leakage(record)
    directory = Path(dest_dir) if dest_dir is not None else None
    if directory is not None:
        directory.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(
        prefix="issue_", suffix=".txt", dir=str(directory) if directory else None
    )
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(issue_text)
    return Path(raw)


def patch_cmd(repo_dir: str) -> str:
    """Shell one-liner producing the agent patch, codegraph's footprint excluded.

    ``git add -A`` stages everything (including untracked files); the cached diff then
    excludes ``.codegraph/`` and ``AGENTS.md`` so the graded patch contains only what the
    agent changed. ``--binary`` keeps binary edits intact, matching official grading.
    """
    return (
        f"cd {shlex.quote(repo_dir)} && git add -A && "
        f"git diff --cached --binary {PATCH_PATHSPEC}"
    )
