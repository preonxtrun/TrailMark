"""Atlas subsystem tests: determinism, budget packing, glossary, testgraph, rules, CLI.

Unit tests build synthetic :class:`~codegraph.model.IndexData` objects directly and
never import ``codegraph.index`` (a sibling module may not exist yet); the pagerank
path is exercised by stubbing ``sys.modules["codegraph.index.graph"]``. CLI tests
monkeypatch a fake ``codegraph.index`` module returning the synthetic data, proving
the CLI works end-to-end without the real indexer.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

from codegraph import cli
from codegraph.atlas import assemble, builder, glossary, rules, testgraph
from codegraph.atlas.budget import Section, pack
from codegraph.emit import agents_md
from codegraph.model import FileEntry, IndexData, Ref, Symbol
from codegraph.util.tokens import byte_len

MT = 1_700_000_000.0  # fixed fake mtime (never emitted)

_TAGS = {"RULES", "MAP", "TESTS", "PLAN", "GLOSSARY"}


# -- synthetic IndexData helper (2 dirs, 10 files, symbols + refs incl. tests) --


def _sym(file: str, name: str, kind: str, line: int, end: int, sig: str = "", doc: str = "") -> Symbol:
    return Symbol(file=file, name=name, kind=kind, line=line, end_line=end, signature=sig, doc=doc)


_SESSION_SYMS = [
    _sym("src/auth/session.py", "SessionStore", "class", 10, 90,
         "class SessionStore", "Session lifecycle and token storage"),
    _sym("src/auth/session.py", "open_session", "def", 20, 30,
         "def open_session(user: str) -> SessionStore", "Open a session and mint a token"),
    _sym("src/auth/session.py", "refresh_token", "def", 40, 64,
         "def refresh_token(self, token: str) -> bool", "Refresh the stored auth token"),
    _sym("src/auth/session.py", "close_session", "def", 80, 88,
         "def close_session(self) -> None", "Close a session and revoke its token"),
]
_TOKENS_SYMS = [
    _sym("src/auth/tokens.py", "encode_token", "def", 12, 18,
         "def encode_token(payload: str) -> str", "Encode a session token payload"),
    _sym("src/auth/tokens.py", "decode_token", "def", 30, 36,
         "def decode_token(payload: str) -> str", "Decode and verify a session token"),
]
_GRAPH_SYMS = [
    _sym("src/graph/pagerank.py", "pagerank", "def", 15, 40,
         "def pagerank(files, refs) -> dict", "Deterministic pagerank over the file graph"),
    _sym("src/graph/pagerank.py", "normalize", "def", 44, 50,
         "def normalize(scores: dict) -> dict", "Normalize scores to sum to one"),
]
_DB_SYMS = [
    _sym("src/graph/db.py", "open_db", "def", 8, 12,
         "def open_db(path: str) -> Connection", "Open the sqlite index database"),
    _sym("src/graph/db.py", "store_value", "def", 16, 20,
         "def store_value(key: str) -> None", "Persist a value row"),
    _sym("src/graph/db.py", "load_value", "def", 24, 28,
         "def load_value(key: str) -> None", "Load a value row"),
    _sym("src/graph/db.py", "default_value", "def", 32, 36,
         "def default_value() -> int", "Fallback value"),
]
_CLI_SYMS = [
    _sym("src/cli.py", "main", "def", 5, 30, "def main(argv=None) -> int",
         "Console entrypoint for codegraph"),
]
_TEST_SESSION_SYMS = [
    _sym("tests/test_session.py", "test_refresh_token", "def", 6, 10,
         "def test_refresh_token() -> None", "Refresh keeps the session usable"),
    _sym("tests/test_session.py", "test_open_session", "def", 14, 18,
         "def test_open_session() -> None", "Open mints a token"),
]
_TEST_TOKENS_SYMS = [
    _sym("tests/test_tokens.py", "test_encode_token", "def", 4, 8,
         "def test_encode_token() -> None", "Round-trips a session token"),
]

_ENTRIES: list[tuple[str, str, list[Symbol]]] = [
    ("src/auth/session.py", "py", _SESSION_SYMS),
    ("src/auth/tokens.py", "py", _TOKENS_SYMS),
    ("src/graph/pagerank.py", "py", _GRAPH_SYMS),
    ("src/graph/db.py", "py", _DB_SYMS),
    ("src/cli.py", "py", _CLI_SYMS),
    ("tests/test_session.py", "py", _TEST_SESSION_SYMS),
    ("tests/test_tokens.py", "py", _TEST_TOKENS_SYMS),
    ("tests/conftest.py", "py", []),
    ("README.md", "txt", []),
    ("pyproject.toml", "txt", []),
]

_REFS = [
    Ref("tests/test_session.py", "refresh_token", "src/auth/session.py", 7),
    Ref("tests/test_session.py", "refresh_token", "src/auth/session.py", 9),
    Ref("tests/test_session.py", "open_session", "src/auth/session.py", 15),
    Ref("tests/test_tokens.py", "encode_token", "src/auth/tokens.py", 5),
    Ref("tests/test_tokens.py", "encode_token", "src/auth/tokens.py", 6),
    Ref("src/cli.py", "open_db", "src/graph/db.py", 10),
    Ref("src/cli.py", "refresh_token", "src/auth/session.py", 12),
    Ref("src/auth/session.py", "encode_token", "src/auth/tokens.py", 22),
]


def make_data(root: str = "/repo", on_disk: bool = False) -> IndexData:
    """Synthetic IndexData: src/ + tests/ dirs; mtimes from disk when *on_disk*."""
    files: dict[str, FileEntry] = {}
    for path, lang, syms in _ENTRIES:
        if on_disk:
            full = Path(root) / path
            if not full.exists():
                continue
            st = full.stat()
            files[path] = FileEntry(path=path, lang=lang, mtime=st.st_mtime,
                                    size=st.st_size, symbols=list(syms))
        else:
            files[path] = FileEntry(path=path, lang=lang, mtime=MT,
                                    size=100 + 10 * len(path), symbols=list(syms))
    refs = [r for r in _REFS if r.src_file in files and r.dst_file in files]
    return IndexData(root=root, files=files, refs=refs)


def install_fake_index(monkeypatch, data: IndexData) -> None:
    """Stub codegraph.index with the frozen build/update signature."""
    fake = types.ModuleType("codegraph.index")
    fake.update = lambda root, langs, workers, cache: data
    fake.build = lambda root, langs, workers, cache: data
    monkeypatch.setitem(sys.modules, "codegraph.index", fake)


def install_stub_pagerank(monkeypatch) -> None:
    """Stub codegraph.index.graph.pagerank with a deterministic ref-count score."""
    graph = types.ModuleType("codegraph.index.graph")

    def pagerank(files, refs, damping=0.85, iters=30, personalization=None):
        scores = {p: 0.0 for p in files}
        for ref in refs:
            if ref.dst_file:
                scores[ref.dst_file] = scores.get(ref.dst_file, 0.0) + 1.0
        return scores

    graph.pagerank = pagerank
    index = types.ModuleType("codegraph.index")
    index.graph = graph
    monkeypatch.setitem(sys.modules, "codegraph.index", index)
    monkeypatch.setitem(sys.modules, "codegraph.index.graph", graph)


def section_lines(body: str, tag: str) -> list[str]:
    """Lines belonging to *tag* (until the next known tag), excluding the tag line."""
    out: list[str] = []
    grabbing = False
    for line in body.split("\n"):
        if line in _TAGS:
            grabbing = line == tag
            continue
        if grabbing and line:
            out.append(line)
    return out


# -- determinism ----------------------------------------------------------------


def test_assemble_is_byte_identical_across_calls():
    data = make_data()
    for budget in (512, 2048, 4096):
        first = assemble(data, None, "generic", budget)
        second = assemble(data, None, "generic", budget)
        assert first == second
        assert byte_len(first) <= budget
    with_issue = assemble(data, "refresh token expiry breaks session open", "claude", 4096)
    assert with_issue == assemble(
        data, "refresh token expiry breaks session open", "claude", 4096
    )


def test_assemble_has_no_floats_or_timestamps():
    body = assemble(make_data(), None, "generic", 4096)
    for token in ("1700000000", "e-0", "0.85", "iters"):
        assert token not in body


# -- budget behaviour (512 / 2048 / 4096) ----------------------------------------


def test_budget_512_floor():
    body = assemble(make_data(), None, "generic", 512)
    assert byte_len(body) <= 512
    assert "GLOSSARY" not in body  # dropped first
    assert "PLAN" not in body
    assert "RULES" in body
    assert "Consult MAP/TESTS" not in body  # full text compacted to one line
    assert "Map first" in body
    assert len(section_lines(body, "MAP")) == 3  # ≥3 MAP rows survive
    assert len(section_lines(body, "TESTS")) == 1  # runner row only
    assert "runner:" in body


def test_budget_2048_and_4096_full():
    for budget in (2048, 4096):
        body = assemble(make_data(), None, "generic", budget)
        assert byte_len(body) <= budget
        assert "GLOSSARY" in body and section_lines(body, "GLOSSARY")
        assert len(section_lines(body, "MAP")) == 5
        assert len(section_lines(body, "TESTS")) == 3  # runner + 2 pairs
        assert "Consult MAP/TESTS" in body  # full rules text
        terms = [row.split(" = ")[0][2:] for row in section_lines(body, "GLOSSARY")]
        assert "token" in terms and "session" in terms


def test_drop_order_monotone_across_budgets():
    data = make_data()
    bodies = {b: assemble(data, None, "generic", b) for b in (512, 1024, 2048, 4096)}
    for b, body in bodies.items():
        assert byte_len(body) <= b, b
    # glossary present at ≥2048, gone at 1024 (dropped before any TESTS/MAP extras)
    assert "GLOSSARY" in bodies[2048]
    assert "GLOSSARY" not in bodies[1024]
    assert 1 <= len(section_lines(bodies[1024], "TESTS")) <= 3
    # row counts never increase as the budget shrinks
    for tag, core in (("MAP", 3), ("TESTS", 1)):
        counts = [len(section_lines(bodies[b], tag)) for b in (4096, 2048, 1024, 512)]
        assert counts == sorted(counts, reverse=True)
        assert counts[-1] >= min(core, counts[0])


# -- packer (budget.py) -----------------------------------------------------------


def _sample_sections() -> list[Section]:
    glossary_rows = [f"- term{i} = meaning {i} (a.py:{i + 1})" for i in range(5)]
    return [
        Section(tag="", rank=90, items=["# T"]),
        Section(tag="RULES", rank=50, items=rules.flight_rules("generic").split("\n")),
        Section(tag="RULES", rank=80, items=["- " + rules.compact_rules("generic")]),
        Section(
            tag="MAP",
            items=[f"- src/f{i}.py:1 fn{i}() — desc {i}" for i in range(6)],
            core=3,
            item_rank=40,
            core_rank=60,
        ),
        Section(
            tag="TESTS",
            items=["- runner: python -m pytest -x -q"] + [f"- s{i}.py → t{i}.py" for i in range(3)],
            core=1,
            item_rank=30,
            core_rank=70,
        ),
        Section(
            tag="PLAN",
            rank=10,
            items=[
                "LEADS (verify, don't trust):",
                "1. a.py:1-2 — x",
                "OUTLINES: .codegraph/outlines/a.py.md",
            ],
        ),
        Section(tag="PLAN", rank=20, items=["1. a.py:1-2 — x"]),
        Section(tag="GLOSSARY", rank=0, items=glossary_rows),
    ]


def _section_size(items: list[str], tag: str) -> int:
    """Bytes a section contributes when fully dropped (tag line + item lines)."""
    return sum(byte_len(i) + 1 for i in items) + (byte_len(tag) + 1 if tag else 0)


def test_pack_never_exceeds_and_never_splits_entries():
    secs = _sample_sections()
    allowed = set(_TAGS) | {"# T"}
    for s in secs:
        allowed.update(s.items)
    for budget in (64, 128, 300, 512, 760, 1024, 4096):
        out = pack(secs, budget)
        assert byte_len(out) <= budget, budget
        assert out == pack(secs, budget)
        for line in out.split("\n"):
            assert not line or line in allowed, f"partial/unknown entry at {budget}: {line!r}"


def test_pack_drop_order_exact():
    secs = _sample_sections()
    full = pack(secs, 100_000)
    assert "GLOSSARY" in full and "LEADS (verify" in full

    # Step 1: drop exactly the GLOSSARY (rank 0) → PLAN still verbose.
    b1 = byte_len(full) - _section_size(secs[7].items, "GLOSSARY")
    out = pack(secs, b1)
    assert byte_len(out) == b1
    assert "GLOSSARY" not in out and "PLAN" in out and "LEADS (verify" in out
    assert "Consult MAP/TESTS" in out
    assert len(section_lines(out, "MAP")) == 6 and len(section_lines(out, "TESTS")) == 4

    # Step 2: drop PLAN verbose (rank 10). Revealing the compact variant re-adds its
    # bytes, so the budget must account for them: verbose out, compact in, one tag.
    b2 = b1 - _section_size(secs[5].items, "PLAN") + _section_size(secs[6].items, "PLAN")
    out = pack(secs, b2)
    assert byte_len(out) == b2
    assert out.count("PLAN") == 1 and "LEADS (verify" not in out
    assert "1. a.py:1-2 — x" in out and "OUTLINES:" not in out

    # Step 3: drop PLAN compact (20), extra TESTS (30), extra MAP rows (40).
    b3 = b2 - _section_size(secs[6].items, "PLAN")
    b3 -= sum(byte_len(i) + 1 for i in secs[4].items[1:])
    b3 -= sum(byte_len(i) + 1 for i in secs[3].items[3:])
    out = pack(secs, b3)
    assert byte_len(out) == b3
    assert "PLAN" not in out
    assert len(section_lines(out, "MAP")) == 3 and len(section_lines(out, "TESTS")) == 1
    assert "Consult MAP/TESTS" in out  # full rules still present

    # Step 4: RULES full (50) compacts to the one-line variant.
    b4 = b3 - sum(byte_len(i) + 1 for i in secs[1].items) + (byte_len(secs[2].items[0]) + 1)
    out = pack(secs, b4)
    assert byte_len(out) == b4
    assert "Consult MAP/TESTS" not in out and "Map first" in out
    assert len(section_lines(out, "MAP")) == 3 and len(section_lines(out, "TESTS")) == 1


def test_pack_empty_budget():
    assert pack(_sample_sections(), 0) == ""
    assert pack(_sample_sections(), -5) == ""


# -- rules (flight rules content) --------------------------------------------------


def test_rules_single_agnostic_variant():
    """G9 contract: one rules text, byte-identical for every CLI, no harness vocabulary."""
    texts = {cli: rules.flight_rules(cli) for cli in ("claude", "codex", "opencode", "generic", "unknown")}
    assert len(set(texts.values())) == 1, "rules must NOT fork per CLI"
    text = next(iter(texts.values()))
    assert text == rules.flight_rules("whatever")  # deterministic
    assert 400 <= byte_len(text) <= 900
    assert max(len(line) for line in text.split("\n")) <= rules.WRAP_WIDTH + 2
    lowered = text.lower()
    for term in ("mcp", "subagent", "sub-agent", "parallel tool", "claude", "codex", "opencode"):
        assert term not in lowered, f"harness vocabulary {term!r} leaked into rules"
    for snippet in (
        "Consult MAP/TESTS before any search",
        "as few requests as possible",
        "Run the exact test target from TESTS",
        "Persist probes to one scratch file",
        "Stop-loss: after 2 failed searches",
    ):
        assert snippet in text
    assert rules.compact_rules("claude") == rules.compact_rules("shell") == rules.compact_rules("x")


def test_rules_cli_mapping():
    """Every CLI resolves to the single agnostic variant; assemble ignores cli for RULES."""
    assert rules.resolve_variant("claude") == "generic"
    assert rules.resolve_variant("codex") == "generic"
    assert rules.resolve_variant("opencode") == "generic"
    assert rules.resolve_variant("unknown-cli") == "generic"
    data = make_data()
    assert assemble(data, None, "claude", 4096) == assemble(data, None, "generic", 4096)


# -- glossary -----------------------------------------------------------------------


def test_glossary_terms_rows_and_stopwords():
    data = make_data()
    rows = glossary.build_glossary(data)
    assert rows == glossary.build_glossary(data)
    assert 0 < len(rows) <= glossary.MAX_TERMS
    terms = [row.split(" = ")[0][2:] for row in rows]
    assert terms[0] == "token"  # freq 6 × log(1+2) — top scorer
    assert {"session", "encode"} <= set(terms)
    assert "value" not in terms  # freq ≥ 3 but stopworded
    for row in rows:
        assert row.startswith("- ") and " = " in row and row.endswith(")")
        assert row.split("(")[-1].rstrip(")").count(":") == 1  # (path:line)
    # meanings come from the exemplar symbol's doc line
    token_row = next(r for r in rows if r.startswith("- token "))
    assert "Refresh the stored auth token" in token_row
    assert "(src/auth/session.py:40)" in token_row


def test_glossary_empty_without_qualifying_terms():
    data = make_data()
    body = assemble(data, None, "generic", 4096)
    assert "GLOSSARY" in body  # synthetic vocab qualifies
    empty = IndexData(root="/repo", files={}, refs=[])
    assert glossary.build_glossary(empty) == []
    assert "GLOSSARY" not in assemble(empty, None, "generic", 4096)


# -- testgraph ------------------------------------------------------------------------


def test_is_test_file_detection():
    assert testgraph.is_test_file("tests/test_session.py")
    assert testgraph.is_test_file("test_login.py")
    assert testgraph.is_test_file("login_test.py")
    assert testgraph.is_test_file("spec/auth/login.test.ts")
    assert testgraph.is_test_file("src/auth/login.spec.js")
    assert testgraph.is_test_file("pkg/__tests__/a.tsx")
    assert not testgraph.is_test_file("src/auth/session.py")
    assert not testgraph.is_test_file("src/conftest.py")
    assert not testgraph.is_test_file("tests_util.py")


def test_runner_detection_from_repo_config(tmp_path):
    assert testgraph.detect_runner(tmp_path) is None  # empty repo
    (tmp_path / "pyproject.toml").write_text('[tool.pytest.ini_options]\naddopts = "-q"\n')
    assert testgraph.detect_runner(tmp_path) == testgraph.PYTEST_RUNNER
    for name, marker in (
        ("pytest.ini", "[pytest]\n"),
        ("setup.cfg", "[tool:pytest]\n"),
        ("tox.ini", "[pytest]\n"),
    ):
        cfg = tmp_path / name
        cfg.write_text(marker)
        assert testgraph.detect_runner(tmp_path) == testgraph.PYTEST_RUNNER, name
        cfg.unlink()
    (tmp_path / "pyproject.toml").unlink()  # pytest configs win over the rest

    (tmp_path / "Makefile").write_text(
        "all:\n\techo hi\n\n.PHONY: test\ntest:\n\tpython -m pytest tests -q\n"
    )
    assert testgraph.detect_runner(tmp_path) == "python -m pytest tests -q"

    (tmp_path / "Makefile").write_text("test: lint\n\tpython -m pytest -q\n")
    assert testgraph.detect_runner(tmp_path) == "python -m pytest -q"  # deps skipped
    (tmp_path / "Makefile").unlink()  # Makefile wins over package.json per priority

    (tmp_path / "package.json").write_text('{"scripts": {"test": "vitest run"}}')
    assert testgraph.detect_runner(tmp_path) == "npm test"
    (tmp_path / "package.json").write_text("{not json")  # malformed → skipped
    (tmp_path / "Cargo.toml").write_text("[package]\nname = \"x\"\n")
    assert testgraph.detect_runner(tmp_path) == "cargo test"
    (tmp_path / "Cargo.toml").unlink()
    (tmp_path / "go.mod").write_text("module x\n")
    assert testgraph.detect_runner(tmp_path) == "go test ./..."


def test_testgraph_maps_src_to_tests_from_refs():
    data = make_data()
    pairs = testgraph.src_test_pairs(data, None)
    assert pairs == [
        ("src/auth/session.py", "tests/test_session.py"),
        ("src/auth/tokens.py", "tests/test_tokens.py"),
    ]
    # with pagerank scores: session.py (4 edges) ranks above tokens.py (3)
    pr = {"src/auth/session.py": 4.0, "src/auth/tokens.py": 3.0}
    assert testgraph.src_test_pairs(data, pr)[0][0] == "src/auth/session.py"

    rows = testgraph.build_tests(data, pr)
    assert rows[0] == f"- runner: {testgraph.UNDETECTED}"  # /repo has no config on disk
    assert "- src/auth/session.py → tests/test_session.py" in rows
    assert len(rows) == 3


def test_testgraph_runner_pytest_from_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    data = make_data(root=str(tmp_path))
    rows = testgraph.build_tests(data, None)
    assert rows[0] == "- runner: python -m pytest -x -q"


# -- MAP builder -----------------------------------------------------------------------


def test_map_orders_by_pagerank_with_stub(monkeypatch):
    install_stub_pagerank(monkeypatch)
    data = make_data()
    body = assemble(data, None, "generic", 4096)
    map_rows = section_lines(body, "MAP")
    assert len(map_rows) == 5
    assert "src/auth/session.py" in map_rows[0]  # hottest file first
    assert "; hot: src/auth/session.py:40 refresh_token()" in map_rows[0]
    assert map_rows[0].startswith("- src/ — ")  # dir role on the first row of a dir
    assert body.index("src/auth/session.py") < body.index("src/graph/pagerank.py")


def test_map_alphabetical_fallback_without_pagerank(monkeypatch):
    # hide both the package and the (possibly cached) graph submodule
    monkeypatch.setitem(sys.modules, "codegraph.index", None)
    monkeypatch.setitem(sys.modules, "codegraph.index.graph", None)
    data = make_data()
    rows = builder.build_map(data, None)
    assert rows == builder.build_map(data, None)
    assert builder.pagerank_scores(data) is None  # absent index → clean fallback
    paths = []
    for row in rows:
        anchor = row.split("; hot: ", 1)[1] if "; hot: " in row else row[2:]
        paths.append(anchor.split(":")[0])
    assert paths == sorted(paths)


def test_map_anchor_is_most_referenced_symbol():
    data = make_data()
    counts = builder.ref_counts(data)
    entry = data.files["src/auth/session.py"]
    sym = builder.anchor_symbol(entry, counts)
    assert sym is not None and sym.name == "refresh_token"  # 3 incoming refs


def test_map_rows_byte_capped():
    data = make_data()
    for row in builder.build_map(data, None):
        assert byte_len(row) <= builder.ROW_CAP


# -- PLAN section (issue text) -----------------------------------------------------------


def test_plan_section_with_issue():
    data = make_data()
    body = assemble(data, "refresh token expiry breaks session open", "claude", 4096)
    assert "PLAN" in body and "LEADS (verify, don't trust):" in body
    assert "OUTLINES: .codegraph/outlines/src/auth/session.py.md" in body
    assert "1. src/auth/session.py:40-64 — matches:" in body
    plan_rows = section_lines(body, "PLAN")
    assert 1 < len(plan_rows) <= 7  # header + ≤5 leads + outlines row
    assert "refresh" in plan_rows[1]


def test_plan_compact_under_tight_budget():
    data = make_data()
    issue = "refresh token expiry breaks session open"
    # exact budget: full block minus GLOSSARY (rank 0) minus PLAN-verbose (rank 10)
    # plus the compact PLAN that gets revealed — computed from the same builders
    # assemble() uses, so the assertion is size-independent and deterministic.
    full = byte_len(assemble(data, issue, "generic", 100_000))
    pr = builder.pagerank_scores(data)
    verbose, compact = builder.build_plan(data, issue, pr)
    glossary_bytes = sum(byte_len(i) + 1 for i in glossary.build_glossary(data)) + len("GLOSSARY") + 1
    verbose_bytes = sum(byte_len(i) + 1 for i in verbose) + len("PLAN") + 1
    compact_bytes = sum(byte_len(i) + 1 for i in compact) + len("PLAN") + 1
    budget = full - glossary_bytes - verbose_bytes + compact_bytes
    body = assemble(data, issue, "generic", budget)
    assert byte_len(body) == budget
    assert "GLOSSARY" not in body and "PLAN" in body
    assert "LEADS (verify" not in body and body.count("PLAN") == 1  # compact only
    assert compact[0] in body
    # one byte lower: the compact variant is omitted entirely
    below = assemble(data, issue, "generic", budget - 1)
    assert byte_len(below) <= budget - 1 and "PLAN" not in below


def test_plan_no_lexical_matches():
    data = make_data()
    body = assemble(data, "zzzzqqqq unrelated wibble", "generic", 4096)
    assert "no lexical matches" in body


# -- CLI (init / stats / doctor / remove / watch / focus) ---------------------------------


def _write_repo(tmp: Path) -> None:
    (tmp / "src" / "auth").mkdir(parents=True)
    (tmp / "tests").mkdir()
    (tmp / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    (tmp / "README.md").write_text("# tiny fixture repo — atlas CLI roundtrip\n\nbody\n")
    (tmp / "src" / "auth" / "session.py").write_text("class SessionStore:\n    pass\n")
    (tmp / "src" / "auth" / "tokens.py").write_text("def encode_token(p):\n    return p\n")
    (tmp / "tests" / "test_session.py").write_text("from src.auth import session\n")


def test_cli_init_stats_doctor_remove_roundtrip(tmp_path, monkeypatch, capsys):
    _write_repo(tmp_path)
    agents = tmp_path / "AGENTS.md"
    agents.write_text("Intro line\n")  # content outside the markers must survive
    data = make_data(str(tmp_path), on_disk=True)
    install_fake_index(monkeypatch, data)
    monkeypatch.setitem(sys.modules, "codegraph.focus", None)  # force lazy-skip paths
    monkeypatch.setitem(sys.modules, "codegraph.focus.cli_focus", None)

    rc = cli.main(["--root", str(tmp_path), "--json", "init"])
    assert rc == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["files"] == len(data.files) and payload["budget"] == 2048
    assert payload["outlines"] == "skipped"
    assert "outlines module unavailable, skipping" in captured.err
    body = agents_md.read_block(agents)
    assert body is not None and byte_len(body) <= payload["budget"]
    assert "runner: python -m pytest -x -q" in body  # runner detected from pyproject

    # flags after the subcommand too; same data ⇒ same body ⇒ idempotent no-op write
    rc = cli.main(["init", "--root", str(tmp_path), "--json"])
    assert rc == 0 and json.loads(capsys.readouterr().out)["block_changed"] is False

    rc = cli.main(["--root", str(tmp_path), "--budget", "512", "init", "--json"])
    assert rc == 0
    json.loads(capsys.readouterr().out)
    assert byte_len(agents_md.read_block(agents)) <= 512

    rc = cli.main(["--root", str(tmp_path), "stats", "--json"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["files"] == len(data.files)

    rc = cli.main(["--root", str(tmp_path), "doctor", "--json"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

    rc = cli.main(["--root", str(tmp_path), "remove", "--json"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["block_removed"] is True
    assert agents.read_text() == "Intro line\n"  # outside-marker content restored

    rc = cli.main(["--root", str(tmp_path), "remove", "--purge", "--json"])
    assert rc == 0
    assert not (tmp_path / ".codegraph").exists()


def test_cli_init_without_index_module(tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "codegraph.index", None)
    rc = cli.main(["--root", str(tmp_path), "init"])
    assert rc == 2
    assert "index module unavailable" in capsys.readouterr().err


def test_cli_stats_without_cache(tmp_path, capsys):
    rc = cli.main(["--root", str(tmp_path), "stats"])
    assert rc == 2
    assert "codegraph init" in capsys.readouterr().err


def test_cli_doctor_reports_missing_block(tmp_path, monkeypatch, capsys):
    _write_repo(tmp_path)
    data = make_data(str(tmp_path), on_disk=True)
    install_fake_index(monkeypatch, data)
    rc = cli.main(["--root", str(tmp_path), "init"])  # block written
    assert rc == 0
    capsys.readouterr()
    (tmp_path / "AGENTS.md").unlink()
    rc = cli.main(["--root", str(tmp_path), "doctor", "--json"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False and payload["block"]["ok"] is False


def test_cli_focus_delegates_or_exit_3(tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "codegraph.focus", None)
    monkeypatch.setitem(sys.modules, "codegraph.focus.cli_focus", None)
    rc = cli.main(["--root", str(tmp_path), "focus", "fix the token refresh bug"])
    assert rc == 3
    assert "focus module unavailable" in capsys.readouterr().err


def test_cli_watch_refreshes_once_and_exits_cleanly(tmp_path, monkeypatch, capsys):
    _write_repo(tmp_path)
    data = make_data(str(tmp_path), on_disk=True)
    install_fake_index(monkeypatch, data)
    monkeypatch.setitem(sys.modules, "codegraph.focus", None)
    calls = {"n": 0}

    def fake_sleep(_seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] >= 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    rc = cli.main(["--root", str(tmp_path), "watch", "--interval", "0.01", "--json"])
    assert rc == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert lines and lines[0]["files"] == len(data.files)
    assert agents_md.read_block(tmp_path / "AGENTS.md") is not None
