"""TESTS section: test-file detection, deterministic runner detection, src→test map.

Runner detection reads repo config files under ``data.root`` in a fixed priority
order (pytest configs → Makefile ``test:`` target → package.json → Cargo.toml →
go.mod). Source-to-test mapping uses :class:`~codegraph.model.Ref` edges: a test
file referencing a source file creates an edge, and the top rows are ranked by
source-file pagerank (scores order only — never emitted).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..emit.fmt import truncate
from ..model import IndexData

#: Exact command emitted when any pytest-style config is found.
PYTEST_RUNNER = "python -m pytest -x -q"
#: Placeholder when no runner can be detected from repo config.
UNDETECTED = "(undetected — inspect repo config)"

RUNNER_CAP = 64
MAX_PAIRS = 5

_TEST_DIRS = {"test", "tests", "__tests__"}
_JS_EXTS = {"js", "jsx", "mjs", "cjs", "ts", "tsx"}
_MAKE_TARGET = re.compile(r"^\s*test\s*:\s*(.*)$")


def is_test_file(path: str) -> bool:
    """True for test modules, by name or directory convention.

    Detects ``test_*.py`` / ``*_test.py``, files under ``test(s)|__tests__``
    directories, and ``*.test|spec.(ts|js|tsx|jsx|mjs|cjs)`` patterns.
    """
    parts = path.lower().split("/")
    if not parts:
        return False
    if any(seg in _TEST_DIRS for seg in parts[:-1]):
        return True
    name = parts[-1]
    _stem, dot, ext = name.rpartition(".")
    if dot and ext == "py":
        return name.startswith("test_") or name.endswith("_test.py")
    if dot and ext in _JS_EXTS:
        return ".test." in name or ".spec." in name
    return False


def detect_runner(root: str | Path) -> str | None:
    """Detect the test command from repo config (deterministic priority order).

    Order: pytest.ini / pyproject ``[tool.pytest.ini_options]`` / setup.cfg
    ``[tool:pytest]`` / tox.ini ``[pytest]`` → ``python -m pytest -x -q``;
    Makefile ``test:`` target → its recipe line; ``package.json`` scripts.test →
    ``npm test``; ``Cargo.toml`` → ``cargo test``; ``go.mod`` → ``go test ./...``.
    Returns None when nothing matches.
    """
    base = Path(root)

    def read(name: str) -> str | None:
        try:
            return (base / name).read_text(encoding="utf-8", errors="replace")[:65536]
        except OSError:
            return None

    if (base / "pytest.ini").is_file():
        return PYTEST_RUNNER
    pyproject = read("pyproject.toml")
    if pyproject is not None and "[tool.pytest.ini_options]" in pyproject:
        return PYTEST_RUNNER
    setup_cfg = read("setup.cfg")
    if setup_cfg is not None and "[tool:pytest]" in setup_cfg:
        return PYTEST_RUNNER
    tox = read("tox.ini")
    if tox is not None and "[pytest]" in tox:
        return PYTEST_RUNNER

    makefile = read("Makefile") or read("makefile")
    if makefile:
        recipe = _makefile_test_recipe(makefile)
        if recipe:
            return truncate(recipe, RUNNER_CAP)

    pkg = read("package.json")
    if pkg:
        try:
            scripts = json.loads(pkg).get("scripts", {})
            if isinstance(scripts, dict) and str(scripts.get("test", "")).strip():
                return "npm test"
        except (ValueError, AttributeError):
            pass  # malformed package.json — fall through to the next candidate

    if (base / "Cargo.toml").is_file():
        return "cargo test"
    if (base / "go.mod").is_file():
        return "go test ./..."
    return None


def _makefile_test_recipe(text: str) -> str | None:
    """Recipe line of the first ``test:`` target, or None (defensive parsing)."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        match = _MAKE_TARGET.match(line)
        if not match:
            continue
        inline = match.group(1).strip()
        if inline.startswith(";"):  # GNU make inline recipe: `test: ; cmd`
            cmd = inline.lstrip(";").strip()
            if cmd:
                return cmd
        for follow in lines[i + 1 :]:
            if not follow.strip():
                continue
            if follow.startswith(("\t", "    ")):
                return follow.strip()
            break  # recipe block ended without a recipe; try later targets
    return None


def src_test_pairs(
    data: IndexData, pr: dict[str, float] | None, limit: int = MAX_PAIRS
) -> list[tuple[str, str]]:
    """Top ≤ limit (src, test) pairs derived from resolved refs.

    A test file referencing a source file creates an edge; pairs are ranked by
    source pagerank (descending), then reference weight, then (src, test) — so
    the order is total and deterministic even without pagerank scores.
    """
    weight: dict[tuple[str, str], int] = {}
    files = data.files
    for ref in data.refs:
        if not ref.dst_file or ref.dst_file == ref.src_file:
            continue
        if ref.src_file not in files or ref.dst_file not in files:
            continue
        if not is_test_file(ref.src_file) or is_test_file(ref.dst_file):
            continue
        key = (ref.dst_file, ref.src_file)  # (src, test)
        weight[key] = weight.get(key, 0) + 1
    scores = pr or {}
    ordered = sorted(
        weight.items(),
        key=lambda kv: (-scores.get(kv[0][0], 0.0), -kv[1], kv[0][0], kv[0][1]),
    )
    return [(src, test) for (src, test), _w in ordered[:limit]]


def build_tests(data: IndexData, pr: dict[str, float] | None) -> list[str]:
    """TESTS rows: the runner line plus up to :data:`MAX_PAIRS` src→test rows."""
    runner = detect_runner(data.root) if data.root else None
    rows = [f"- runner: {runner if runner else UNDETECTED}"]
    rows.extend(f"- {src} → {test}" for src, test in src_test_pairs(data, pr))
    return rows
