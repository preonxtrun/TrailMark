"""Tests for the INDEX subsystem: scanner, parser, graph, cache extensions, build/update."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from trailmark.cache import IndexCache
from trailmark.index import build, pagerank, update
from trailmark.index.scanner import Gitignore, scan

# ---------------------------------------------------------------------------
# synthetic repo fixture
# ---------------------------------------------------------------------------

REPO_FILES: dict[str, str] = {
    "app/__init__.py": '"""App package."""\n',
    "app/main.py": (
        "from app.core.engine import Engine\n"
        "from app.utils.helpers import helper_a, helper_b\n"
        "\n"
        "class App:\n"
        '    """Main app."""\n'
        "\n"
        "    def refresh(self, ttl=3600):\n"
        '        """Refresh the app state."""\n'
        "        return helper_a() + helper_b()\n"
        "\n"
        "def main():\n"
        "    engine = Engine()\n"
        "    return engine.run()\n"
    ),
    "app/core/__init__.py": '"""Core package."""\n',
    "app/core/engine.py": (
        "from app.utils.helpers import helper_a\n"
        "from .base import Component\n"
        "\n"
        "class Engine(Component):\n"
        '    """The engine."""\n'
        "\n"
        "    def run(self):\n"
        "        return helper_a() * 2\n"
    ),
    "app/core/base.py": (
        "class Component:\n"
        '    """Base component."""\n'
        "\n"
        "    def start(self):\n"
        "        raise NotImplementedError\n"
    ),
    "app/utils/__init__.py": "from .helpers import helper_a\n",
    "app/utils/helpers.py": (
        "def helper_a():\n"
        '    """Helper A does math."""\n'
        "    return 42\n"
        "\n"
        "def helper_b():\n"
        '    """Helper B does strings."""\n'
        '    return "b"\n'
    ),
    "app/utils/format.py": (
        "from app.utils.helpers import helper_a\n\ndef fmt(x):\n    return helper_a()\n"
    ),
    "app/report.py": (
        "import os.path\n"
        "from app.utils.helpers import helper_b\n"
        "\n"
        "def report():\n"
        "    return helper_b()\n"
    ),
    "auth/__init__.py": '"""Auth package."""\n',
    "auth/session.py": (
        "class Session:\n"
        '    """User session."""\n'
        "\n"
        "    def refresh(self, ttl=3600):\n"
        '        """Refresh the session."""\n'
        "        return ttl\n"
    ),
    "tests/test_main.py": (
        "from app.main import App, main\n\ndef test_refresh():\n    assert App().refresh()\n"
    ),
    "broken.py": ("def intact():\n    return 1\n\ndef broken(:\n    pass\n"),
    "web/app.ts": (
        'import { helper } from "./helpers";\n'
        "\n"
        "export function render(name: string): string {\n"
        '  return helper(name) + "!";\n'
        "}\n"
    ),
    "web/helpers.ts": (
        "/** Turn a name into a shout. */\n"
        "export function helper(name: string): string {\n"
        "  return name.toUpperCase();\n"
        "}\n"
    ),
    "web/comp.tsx": (
        'import { helper } from "./helpers";\n'
        "\n"
        "export const Badge = (label: string) => {\n"
        "  return <span>{helper(label)}</span>;\n"
        "};\n"
    ),
}

GITIGNORE = "ignored_dir/\n*.log\n!keep.log\n"

EXTRA_FILES: dict[str, bytes] = {
    "ignored_dir/secret.py": b"def secret():\n    pass\n",
    "node_modules/pkg.js": b"var x = 1;\n",
    "dist/out.js": b"var y = 2;\n",
    "build/art.js": b"var z = 3;\n",
    "__pycache__/m.pyc": b"\x00\x01binary",
    "debug.log": b"noise\n",
    "keep.log": b"precious\n",
    "data.bin": b"text\x00nulls",
    "img.png": b"\x89PNG\r\n\x1a\nrest",
    "notes.txt": b"plain notes\n",
}


def make_repo(root: Path) -> Path:
    """Create the synthetic repo used by most tests."""
    for rel, content in REPO_FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    (root / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    for rel, content in EXTRA_FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return root


def line_of(path: str, needle: str) -> int:
    """1-based line of ``needle`` inside the fixture file ``path``."""
    content = REPO_FILES[path]
    for lineno, line in enumerate(content.splitlines(), start=1):
        if needle in line:
            return lineno
    raise AssertionError(f"{needle!r} not found in fixture {path}")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path)


def find_symbol(data, path: str, name: str):
    """Return the symbol ``name`` defined in ``path`` (or None)."""
    entry = data.files.get(path)
    if entry is None:
        return None
    for sym in entry.symbols:
        if sym.name == name:
            return sym
    return None


def has_ref(data, src: str, dst_name: str, dst_file: str) -> bool:
    return any(
        r.src_file == src and r.dst_name == dst_name and r.dst_file == dst_file for r in data.refs
    )


# ---------------------------------------------------------------------------
# scanner
# ---------------------------------------------------------------------------


class TestScanner:
    def test_scan_skips_ignored_dirs_and_binaries(self, repo: Path) -> None:
        scanned = {rel for rel, *_ in scan(repo)}
        assert "app/main.py" in scanned
        assert "web/helpers.ts" in scanned
        # gitignore
        assert "ignored_dir/secret.py" not in scanned
        assert "debug.log" not in scanned
        # skip dirs
        assert "node_modules/pkg.js" not in scanned
        assert "dist/out.js" not in scanned
        assert "build/art.js" not in scanned
        assert "__pycache__/m.pyc" not in scanned
        # binary sniff + binary extension
        assert "data.bin" not in scanned
        assert "img.png" not in scanned
        # text fallback excluded unless requested
        assert "notes.txt" not in scanned
        assert "notes.txt" in {rel for rel, *_ in scan(repo, langs=["py", "txt"])}

    def test_scan_tuple_shape_and_sorting(self, repo: Path) -> None:
        scanned = scan(repo)
        rels = [s[0] for s in scanned]
        assert rels == sorted(rels)
        for rel, abspath, mtime, size, lang in scanned:
            assert (repo / rel).resolve() == Path(abspath).resolve()
            assert mtime > 0
            assert size > 0
            assert lang in {"py", "ts", "js", "go", "rs", "c", "txt"}

    def test_lang_mapping(self, tmp_path: Path) -> None:
        mapping = [
            ("a.py", "py"),
            ("b.ts", "ts"),
            ("c.tsx", "ts"),
            ("d.js", "js"),
            ("e.jsx", "js"),
            ("f.mjs", "js"),
            ("g.cjs", "js"),
            ("h.go", "go"),
            ("i.rs", "rs"),
            ("j.h", "c"),
            ("k.cc", "c"),
            ("l.rb", None),
            ("m.cpp", "c"),
        ]
        for name, _lang in mapping:
            (tmp_path / name).write_bytes(b"x" * 16)
        langs = ["py", "ts", "js", "go", "rs", "c"]
        scanned = {rel: lang for rel, _, _, _, lang in scan(tmp_path, langs=langs)}
        for name, lang in mapping:
            assert scanned.get(name) == lang, name

    def test_gitignore_semantics(self) -> None:
        ign = Gitignore()
        ign.push("", Gitignore.parse("*.log\n!important.log\nbuild/\n/top.txt\nsub/*.tmp\n"))
        assert ign.ignored("a/deep/b.log", False)
        assert not ign.ignored("important.log", False)
        assert ign.ignored("build", True)
        assert not ign.ignored("build", False)
        assert ign.ignored("top.txt", False)
        assert not ign.ignored("nested/top.txt", False)
        assert ign.ignored("sub/x.tmp", False)
        assert not ign.ignored("other/x.tmp", False)

    def test_gitignore_negation_from_nested_file(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").write_text("*.tmp\n", encoding="utf-8")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / ".gitignore").write_text("!keep.tmp\n", encoding="utf-8")
        scanned = {rel for rel, *_ in scan(tmp_path, langs=["py", "txt"])}
        # .tmp files are not an indexed language; probe via Gitignore directly instead
        ign = Gitignore()
        ign.push("", Gitignore.parse((tmp_path / ".gitignore").read_text()))
        ign.push("sub", Gitignore.parse((sub / ".gitignore").read_text()))
        assert ign.ignored("sub/keep.tmp", False) is False
        assert ign.ignored("sub/other.tmp", False) is True
        assert not scanned  # nothing indexable was created


# ---------------------------------------------------------------------------
# build: symbols, signatures, docs
# ---------------------------------------------------------------------------


class TestBuildSymbols:
    def test_python_symbols_anchors_signatures_docs(self, repo: Path) -> None:
        data = build(repo)
        refresh = find_symbol(data, "app/main.py", "refresh")
        assert refresh is not None
        assert refresh.kind == "method"
        assert refresh.line == line_of("app/main.py", "def refresh(self, ttl=3600)")
        assert refresh.end_line == refresh.line + 2
        assert refresh.signature == "def refresh(self, ttl=3600)"
        assert refresh.doc == "Refresh the app state."

        helper_a = find_symbol(data, "app/utils/helpers.py", "helper_a")
        assert helper_a is not None
        assert helper_a.kind == "def"
        assert helper_a.line == line_of("app/utils/helpers.py", "def helper_a()")
        assert helper_a.signature == "def helper_a()"
        assert helper_a.doc == "Helper A does math."

        engine = find_symbol(data, "app/core/engine.py", "Engine")
        assert engine is not None
        assert engine.kind == "class"
        assert engine.signature == "class Engine(Component)"
        assert engine.doc == "The engine."

        app = find_symbol(data, "app/main.py", "App")
        assert app is not None
        assert app.kind == "class"
        assert app.line == line_of("app/main.py", "class App:")

    def test_ts_symbols(self, repo: Path) -> None:
        data = build(repo)
        render = find_symbol(data, "web/app.ts", "render")
        assert render is not None
        assert render.kind == "def"
        assert render.line == line_of("web/app.ts", "export function render")
        assert render.signature == "function render(name: string)"
        helper = find_symbol(data, "web/helpers.ts", "helper")
        assert helper is not None
        assert helper.doc == "Turn a name into a shout."
        # .tsx files parse with the tsx grammar (JSX inside arrow function is fine)
        badge = find_symbol(data, "web/comp.tsx", "Badge")
        assert badge is not None
        assert badge.kind == "def"
        assert has_ref(data, "web/comp.tsx", "helper", "web/helpers.ts")

    def test_broken_file_uses_fallback(self, repo: Path) -> None:
        data = build(repo)  # must not raise
        assert "broken.py" in data.files
        names = {s.name for s in data.files["broken.py"].symbols}
        assert "intact" in names
        assert "broken" in names
        assert all(s.file == "broken.py" for s in data.files["broken.py"].symbols)

    def test_langs_filter(self, repo: Path) -> None:
        data = build(repo, langs=["py"])
        assert "web/app.ts" not in data.files
        assert "app/main.py" in data.files
        both = build(repo, langs=["py", "ts"])
        assert "web/app.ts" in both.files


# ---------------------------------------------------------------------------
# refs
# ---------------------------------------------------------------------------


class TestRefs:
    def test_refs_resolved_across_files(self, repo: Path) -> None:
        data = build(repo)
        # absolute python import
        assert has_ref(data, "app/main.py", "helper_a", "app/utils/helpers.py")
        assert has_ref(data, "app/main.py", "Engine", "app/core/engine.py")
        # relative python import
        assert has_ref(data, "app/core/engine.py", "Component", "app/core/base.py")
        # package re-export via __init__
        assert has_ref(data, "app/utils/__init__.py", "helper_a", "app/utils/helpers.py")
        # test file → source module
        assert has_ref(data, "tests/test_main.py", "App", "app/main.py")
        # ts relative import
        assert has_ref(data, "web/app.ts", "helper", "web/helpers.ts")

    def test_unresolved_module_import_kept(self, repo: Path) -> None:
        data = build(repo)
        assert any(
            r.src_file == "app/report.py" and r.dst_name == "os.path" and r.dst_file == ""
            for r in data.refs
        )
        # no self-references
        assert all(r.src_file != r.dst_file for r in data.refs)
        # deterministic ordering
        keys = [(r.src_file, r.line, r.dst_name, r.dst_file) for r in data.refs]
        assert keys == sorted(keys)

    def test_go_and_rust_extraction(self, tmp_path: Path) -> None:
        root = tmp_path
        (root / "src").mkdir()
        (root / "pkg" / "util").mkdir(parents=True)
        (root / "go.mod").write_text("module example.com/proj\n", encoding="utf-8")
        (root / "pkg" / "util" / "util.go").write_text(
            "package util\n\nfunc Add(a, b int) int {\n\treturn a + b\n}\n", encoding="utf-8"
        )
        (root / "main.go").write_text(
            'package main\n\nimport "example.com/proj/pkg/util"\n\n'
            "func main() {\n\t_ = util.Add(1, 2)\n}\n",
            encoding="utf-8",
        )
        (root / "src" / "auth.rs").write_text(
            "pub struct Token;\n\npub fn issue() -> Token {\n    Token\n}\n",
            encoding="utf-8",
        )
        (root / "src" / "main.rs").write_text(
            "mod auth;\n\nuse crate::auth::issue;\n\nfn main() {\n    let _ = issue();\n}\n",
            encoding="utf-8",
        )
        data = build(root, langs=["go", "rs"])
        add = find_symbol(data, "pkg/util/util.go", "Add")
        assert add is not None and add.kind == "def"
        assert add.signature == "func Add(a, b int)"
        token = find_symbol(data, "src/auth.rs", "Token")
        assert token is not None and token.kind == "class"
        issue = find_symbol(data, "src/auth.rs", "issue")
        assert issue is not None and issue.signature == "pub fn issue()"
        assert has_ref(data, "main.go", "example.com/proj/pkg/util", "pkg/util/util.go")
        assert has_ref(data, "src/main.rs", "issue", "src/auth.rs")


# ---------------------------------------------------------------------------
# pagerank
# ---------------------------------------------------------------------------


class TestPagerank:
    def test_heavily_imported_module_ranks_first(self, repo: Path) -> None:
        data = build(repo)
        scores = pagerank(data.files, data.refs)
        assert len(scores) == len(data.files)
        assert abs(sum(scores.values()) - 1.0) < 1e-9
        top = max(scores, key=scores.get)
        assert top == "app/utils/helpers.py"

    def test_personalization_seeds_scores(self, repo: Path) -> None:
        data = build(repo)
        organic = pagerank(data.files, data.refs)
        # auth/session.py has a weak organic rank; a strong seed must lift it above
        # files that organically outrank it (engine/base).
        seeded = pagerank(data.files, data.refs, personalization={"auth/session.py": 10.0})
        assert abs(sum(seeded.values()) - 1.0) < 1e-9
        assert seeded["auth/session.py"] > 2 * organic["auth/session.py"]
        assert seeded["auth/session.py"] > seeded["app/core/engine.py"]
        assert seeded["auth/session.py"] > seeded["app/core/base.py"]
        # missing keys get zero weight; still a valid distribution
        weird = pagerank(data.files, data.refs, personalization={"definitely/missing.py": 3.0})
        assert abs(sum(weird.values()) - 1.0) < 1e-9

    def test_pagerank_edge_cases(self, repo: Path) -> None:
        assert pagerank([], []) == {}
        single = pagerank(["only.py"], [])
        assert abs(single["only.py"] - 1.0) < 1e-9
        # dangling mass (targets without out-edges) is redistributed, sum stays 1
        scores = pagerank(
            ["a.py", "b.py"], [type("R", (), {"src_file": "a.py", "dst_file": "b.py"})()]
        )
        assert abs(sum(scores.values()) - 1.0) < 1e-9
        assert scores["b.py"] > scores["a.py"]


# ---------------------------------------------------------------------------
# incremental update
# ---------------------------------------------------------------------------


class TestUpdate:
    def test_update_reuses_unchanged_and_reparses_changed(self, repo: Path) -> None:
        cache = IndexCache(repo / ".trailmark")
        first = build(repo, cache=cache)

        old_entries = first.files
        helpers_path = repo / "app" / "utils" / "helpers.py"
        new_text = helpers_path.read_text() + "\ndef helper_c():\n    return 3\n"
        helpers_path.write_text(new_text)
        stamp = time.time() + 10
        os.utime(helpers_path, (stamp, stamp))

        second = update(repo, cache=cache)

        # changed file: new symbol appears, mtime follows the file
        helper_c = find_symbol(second, "app/utils/helpers.py", "helper_c")
        assert helper_c is not None
        assert second.files["app/utils/helpers.py"].mtime == pytest.approx(stamp)
        # unchanged files: identical entries (same mtime + identical symbol objects),
        # proving they were reused from the cache rather than reparsed
        for rel in ("app/main.py", "auth/session.py", "web/app.ts"):
            assert second.files[rel].mtime == old_entries[rel].mtime
            assert second.files[rel].symbols == old_entries[rel].symbols
            assert second.files[rel].size == old_entries[rel].size

    def test_update_drops_deleted_files(self, repo: Path) -> None:
        cache = IndexCache(repo / ".trailmark")
        build(repo, cache=cache)
        os.unlink(repo / "app" / "core" / "engine.py")
        second = update(repo, cache=cache)
        assert "app/core/engine.py" not in second.files
        assert not any(r.src_file == "app/core/engine.py" for r in second.refs)
        assert not any(r.dst_file == "app/core/engine.py" for r in second.refs)
        # cache agrees
        loaded = IndexCache(repo / ".trailmark").load()
        assert loaded is not None
        assert "app/core/engine.py" not in loaded.files

    def test_update_noop_is_stable(self, repo: Path) -> None:
        cache = IndexCache(repo / ".trailmark")
        first = build(repo, cache=cache)
        second = update(repo, cache=cache)
        assert set(second.files) == set(first.files)
        for rel, entry in first.files.items():
            assert second.files[rel].symbols == entry.symbols
            assert second.files[rel].mtime == entry.mtime

    def test_update_without_cache_falls_back_to_build(self, repo: Path) -> None:
        data = update(repo, cache=None)
        assert "app/main.py" in data.files


# ---------------------------------------------------------------------------
# determinism + cache round-trip + search
# ---------------------------------------------------------------------------


class TestDeterminismAndCache:
    def test_two_builds_identical(self, repo: Path) -> None:
        one = build(repo)
        two = build(repo)
        dump_one = sorted(
            (
                e.path,
                e.lang,
                [(s.name, s.kind, s.line, s.end_line, s.signature, s.doc) for s in e.symbols],
            )
            for e in one.files.values()
        )
        dump_two = sorted(
            (
                e.path,
                e.lang,
                [(s.name, s.kind, s.line, s.end_line, s.signature, s.doc) for s in e.symbols],
            )
            for e in two.files.values()
        )
        assert dump_one == dump_two
        assert one.refs == two.refs

    def test_worker_count_does_not_change_results(self, repo: Path) -> None:
        serial = build(repo, workers=1)
        parallel = build(repo, workers=8)
        assert serial.refs == parallel.refs
        assert {
            p: [(s.name, s.kind, s.line, s.signature) for s in e.symbols]
            for p, e in serial.files.items()
        } == {
            p: [(s.name, s.kind, s.line, s.signature) for s in e.symbols]
            for p, e in parallel.files.items()
        }

    def test_cache_roundtrip(self, repo: Path) -> None:
        cache = IndexCache(repo / ".trailmark")
        data = build(repo, cache=cache)
        loaded = IndexCache(repo / ".trailmark").load()
        assert loaded is not None
        assert loaded.root == data.root
        assert set(loaded.files) == set(data.files)
        for rel, entry in data.files.items():
            other = loaded.files[rel]
            assert other.lang == entry.lang
            assert other.mtime == entry.mtime
            assert other.size == entry.size
            assert other.symbols == entry.symbols
        assert loaded.refs == data.refs

    def test_fts_search_session_refresh_first(self, repo: Path) -> None:
        cache = IndexCache(repo / ".trailmark")
        build(repo, cache=cache)
        hits = cache.search("session refresh")
        assert hits, "search returned no hits"
        assert hits[0].file == "auth/session.py"
        assert hits[0].name == "refresh"
        assert hits[0].kind == "method"
        assert hits[0].line == line_of("auth/session.py", "def refresh(self, ttl=3600)")

    def test_remove_files_extension(self, repo: Path) -> None:
        cache = IndexCache(repo / ".trailmark")
        build(repo, cache=cache)
        cache.remove_files(["app/main.py"])
        loaded = cache.load()
        assert loaded is not None
        assert "app/main.py" not in loaded.files
        assert not any(r.src_file == "app/main.py" for r in loaded.refs)
        assert not any(h.file == "app/main.py" for h in cache.search("refresh"))
        # removing a non-existent path is a safe no-op
        cache.remove_files(["does/not/exist.py"])


# ---------------------------------------------------------------------------
# performance (invariant #6: build < 60s, warm update < 2s on ~3k files)
# ---------------------------------------------------------------------------


class TestPerformance:
    def test_build_and_warm_update_on_2000_files(self, tmp_path: Path) -> None:
        root = tmp_path / "big"
        root.mkdir()
        (root / "core").mkdir()
        (root / "core" / "api.py").write_text(
            'def shared_api(x):\n    """Shared hot module."""\n    return x + 1\n',
            encoding="utf-8",
        )
        n_packages, per_package = 40, 50
        for p in range(n_packages):
            pkg = root / f"pkg{p:02d}"
            pkg.mkdir()
            for f in range(per_package):
                (pkg / f"mod{f:02d}.py").write_text(
                    f'"""Module {p}.{f}."""\n'
                    "from core.api import shared_api\n"
                    f"from .sibling{f:02d} import sibling_{f}\n"
                    "\n"
                    f"def fn_{p}_{f}(x):\n"
                    f'    """Compute {p}.{f}."""\n'
                    f"    return shared_api(x) + sibling_{f}(x)\n"
                    "\n"
                    f"class Thing{p}_{f}:\n"
                    f'    """Thing {p}.{f}."""\n'
                    "\n"
                    "    def run(self, v):\n"
                    f"        return fn_{p}_{f}(v)\n",
                    encoding="utf-8",
                )
                (pkg / f"sibling{f:02d}.py").write_text(
                    f"def sibling_{f}(x):\n    return x * 2\n",
                    encoding="utf-8",
                )
        total = n_packages * per_package * 2 + 1
        assert total == 4001

        cache = IndexCache(root / ".trailmark")
        t0 = time.perf_counter()
        data = build(root, cache=cache)
        build_seconds = time.perf_counter() - t0
        print(f"\n[perf] build of {len(data.files)} files: {build_seconds:.2f}s")
        assert len(data.files) == total
        assert build_seconds < 60.0

        # warm update: touch exactly one file
        victim = root / "pkg00" / "mod00.py"
        text = victim.read_text()
        stamp = time.time() + 5
        victim.write_text(text + "\ndef extra():\n    return 1\n")
        os.utime(victim, (stamp, stamp))
        t1 = time.perf_counter()
        updated = update(root, cache=cache)
        update_seconds = time.perf_counter() - t1
        print(f"[perf] warm update (1 changed file): {update_seconds:.3f}s")
        assert find_symbol(updated, "pkg00/mod00.py", "extra") is not None
        assert update_seconds < 2.0

        # pagerank on the big graph stays cheap and normalized
        t2 = time.perf_counter()
        scores = pagerank(updated.files, updated.refs)
        pr_seconds = time.perf_counter() - t2
        print(f"[perf] pagerank: {pr_seconds:.3f}s")
        assert abs(sum(scores.values()) - 1.0) < 1e-9
        assert max(scores, key=scores.get) == "core/api.py"
