"""Tests for the FOCUS subsystem: ranker, PLAN renderer, outlines, CLI handler."""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import types
from pathlib import Path

import pytest

from trailmark.cache import IndexCache
from trailmark.emit.agents_md import read_block
from trailmark.focus import cli_focus
from trailmark.focus.outlines import MAX_LINES, TRUNCATION_MARK, generate_for, generate_top
from trailmark.focus.plan import render_plan
from trailmark.focus.ranker import Lead, file_pagerank, rank
from trailmark.model import FileEntry, IndexData, Ref, Symbol
from trailmark.util.tokens import byte_len

ISSUE = "refresh the expired session token"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _sym(file: str, name: str, kind: str, line: int, end: int, sig: str, doc: str = "") -> Symbol:
    return Symbol(file=file, name=name, kind=kind, line=line, end_line=end, signature=sig, doc=doc)


def make_data(root: str = "") -> IndexData:
    """Synthetic ~10-file index: auth/session cluster plus one matching test file."""
    files: dict[str, FileEntry] = {}

    def add(path: str, symbols: list[Symbol]) -> None:
        files[path] = FileEntry(path=path, lang="py", mtime=1.0, size=100 * len(symbols), symbols=symbols)

    add(
        "src/auth/session.py",
        [
            _sym("src/auth/session.py", "_hash_password", "def", 1, 4, "def _hash_password(pw: str) -> bytes"),
            _sym("src/auth/session.py", "Session", "class", 6, 70, "class Session", "User session state and lifecycle."),
            _sym(
                "src/auth/session.py",
                "refresh",
                "method",
                12,
                30,
                "def refresh(self, ttl: int) -> str",
                "Refresh the session token for a user.",
            ),
            _sym(
                "src/auth/session.py",
                "revoke",
                "method",
                32,
                38,
                "def revoke(self) -> None",
                "Invalidate this session immediately.",
            ),
            _sym(
                "src/auth/session.py",
                "refresh_session_token",
                "def",
                72,
                84,
                "def refresh_session_token(session: Session, ttl: int) -> str",
                "Refresh an expired session token for a user.",
            ),
            _sym(
                "src/auth/session.py",
                "session_expired",
                "def",
                86,
                92,
                "def session_expired(session: Session) -> bool",
                "True if the session token has expired.",
            ),
        ],
    )
    add(
        "src/auth/models.py",
        [
            _sym("src/auth/models.py", "User", "class", 1, 30, "class User", "A user record."),
            _sym(
                "src/auth/models.py",
                "load_user",
                "def",
                32,
                40,
                "def load_user(user_id: int) -> User",
                "Load a user record by id.",
            ),
        ],
    )
    add(
        "src/api/routes.py",
        [
            _sym(
                "src/api/routes.py",
                "post_login",
                "def",
                10,
                25,
                "def post_login(req: Request) -> Response",
                "Handle a login request and start a session.",
            ),
            _sym(
                "src/api/routes.py",
                "post_logout",
                "def",
                27,
                35,
                "def post_logout(req: Request) -> Response",
                "End the current session.",
            ),
        ],
    )
    add(
        "src/db/store.py",
        [
            _sym("src/db/store.py", "Store", "class", 1, 40, "class Store", "Key-value persistence layer."),
            _sym("src/db/store.py", "get", "method", 42, 48, "def get(self, key: str) -> bytes", "Fetch the value stored at key."),
            _sym(
                "src/db/store.py",
                "put",
                "method",
                50,
                58,
                "def put(self, key: str, value: bytes) -> None",
                "Store a value under key.",
            ),
        ],
    )
    add(
        "src/core/utils.py",
        [
            _sym("src/core/utils.py", "slugify", "def", 1, 8, "def slugify(text: str) -> str", "Convert text to a URL-safe slug."),
            _sym("src/core/utils.py", "truncate", "def", 10, 16, "def truncate(text: str, n: int) -> str", "Truncate text to n characters."),
        ],
    )
    add(
        "src/parsers/logfmt.py",
        [
            _sym(
                "src/parsers/logfmt.py",
                "parse_line",
                "def",
                1,
                20,
                "def parse_line(line: str) -> dict[str, str]",
                "Parse one logfmt line into key-value pairs.",
            )
        ],
    )
    add(
        "src/payments/charge.py",
        [
            _sym(
                "src/payments/charge.py",
                "charge_customer",
                "def",
                5,
                30,
                "def charge_customer(card: Card, amount_cents: int) -> Receipt",
                "Charge a customer card for an amount.",
            ),
            _sym("src/payments/charge.py", "Invoice", "class", 32, 60, "class Invoice", "An invoice for a completed charge."),
        ],
    )
    add(
        "tests/test_session.py",
        [
            _sym(
                "tests/test_session.py",
                "test_refresh_session_token",
                "def",
                1,
                10,
                "def test_refresh_session_token() -> None",
                "Check token refresh.",
            ),
            _sym("tests/test_session.py", "test_revoke", "def", 12, 18, "def test_revoke() -> None", "Check revocation clears the session."),
        ],
    )
    add(
        "tests/test_store.py",
        [
            _sym(
                "tests/test_store.py",
                "test_store_roundtrip",
                "def",
                1,
                12,
                "def test_store_roundtrip() -> None",
                "Roundtrip put and get through Store.",
            )
        ],
    )
    add(
        "tests/test_charge.py",
        [
            _sym(
                "tests/test_charge.py",
                "test_charge_declines",
                "def",
                1,
                12,
                "def test_charge_declines() -> None",
                "Declined cards raise an error.",
            )
        ],
    )

    refs = [
        Ref("src/api/routes.py", "refresh_session_token", "src/auth/session.py", 8),
        Ref("src/api/routes.py", "load_user", "src/auth/models.py", 9),
        Ref("src/auth/session.py", "load_user", "src/auth/models.py", 71),
        Ref("tests/test_session.py", "refresh_session_token", "src/auth/session.py", 3),
        Ref("tests/test_session.py", "Session", "src/auth/session.py", 4),
        Ref("tests/test_store.py", "Store", "src/db/store.py", 2),
        Ref("tests/test_charge.py", "charge_customer", "src/payments/charge.py", 2),
        Ref("src/db/store.py", "slugify", "src/core/utils.py", 5),
    ]
    return IndexData(root=root, files=files, refs=refs)


@pytest.fixture()
def indexed(tmp_path: Path) -> tuple[IndexData, IndexCache]:
    """IndexData persisted through a real IndexCache and loaded back (mirrors prod)."""
    cache = IndexCache(tmp_path / ".trailmark")
    cache.save(make_data(root=str(tmp_path)))
    data = cache.load()
    assert data is not None
    return data, cache


def make_args(**kwargs: object) -> argparse.Namespace:
    """A fully-populated focus namespace (all fields cli.py would provide)."""
    defaults: dict[str, object] = {
        "root": ".",
        "issue": None,
        "issue_file": None,
        "cli": None,
        "budget": None,
        "json": False,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def install_fake_atlas(monkeypatch: pytest.MonkeyPatch, body: str = "TEST BODY") -> None:
    """Patch atlas.assemble to a stub whether or not the real atlas package exists."""

    def fake_assemble(
        data: object,
        issue: str | None,
        cli: str,
        budget: int,
        leads: list | None = None,
    ) -> str:
        return body

    try:
        import trailmark.atlas as atlas_module
    except ImportError:
        stub = types.SimpleNamespace(assemble=fake_assemble)
        monkeypatch.setitem(sys.modules, "trailmark.atlas", stub)
        import trailmark as trailmark_pkg

        monkeypatch.setattr(trailmark_pkg, "atlas", stub, raising=False)
    else:
        monkeypatch.setattr(atlas_module, "assemble", fake_assemble)


# ---------------------------------------------------------------------------
# ranker
# ---------------------------------------------------------------------------


class TestRank:
    def test_matching_file_ranks_first(self, indexed: tuple[IndexData, IndexCache]) -> None:
        data, cache = indexed
        leads = rank(data, cache, ISSUE, k=5)
        assert leads, "expected at least one lead"
        assert leads[0].path == "src/auth/session.py"
        assert (leads[0].start_line, leads[0].end_line) == (72, 84)
        assert len(leads[0].why) <= 90
        assert "expired session token" in leads[0].why
        assert "(refresh_session_token)" in leads[0].why
        paths = {lead.path for lead in leads}
        assert "tests/test_session.py" in paths, "test-proximity file should surface"

    def test_k_limit_scores_and_order(self, indexed: tuple[IndexData, IndexCache]) -> None:
        data, cache = indexed
        leads = rank(data, cache, ISSUE, k=2)
        assert len(leads) <= 2
        for lead in leads:
            assert 0.0 <= lead.score <= 1.0
            assert 1 <= lead.start_line <= lead.end_line
        scores = [lead.score for lead in leads]
        assert scores == sorted(scores, reverse=True)

    def test_deterministic(self, indexed: tuple[IndexData, IndexCache]) -> None:
        data, cache = indexed
        first = rank(data, cache, ISSUE, k=5)
        second = rank(data, cache, ISSUE, k=5)
        assert first == second

    def test_unmatched_issue_falls_back_gracefully(
        self, indexed: tuple[IndexData, IndexCache]
    ) -> None:
        data, cache = indexed
        leads = rank(data, cache, "zzzqqq unm4tchable", k=3)
        assert len(leads) <= 3
        for lead in leads:
            assert 0.0 <= lead.score <= 1.0
        again = rank(data, cache, "zzzqqq unm4tchable", k=3)
        assert again == leads

    def test_empty_and_degenerate_inputs(self, indexed: tuple[IndexData, IndexCache]) -> None:
        data, cache = indexed
        assert rank(data, cache, ISSUE, k=0) == []
        assert rank(IndexData(root="", files={}, refs=[]), cache, ISSUE, k=5) == []

    def test_lead_is_frozen(self) -> None:
        lead = Lead(path="a.py", start_line=1, end_line=2, why="w", score=0.5)
        with pytest.raises(dataclasses.FrozenInstanceError):
            lead.score = 0.9  # type: ignore[misc]

    def test_fallback_pagerank_sums_to_one(self, indexed: tuple[IndexData, IndexCache]) -> None:
        data, _ = indexed
        scores = file_pagerank(data.files, data.refs)
        assert set(scores) == set(data.files)
        assert sum(scores.values()) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# plan renderer
# ---------------------------------------------------------------------------


def plan_leads() -> list[Lead]:
    return [
        Lead(
            path="src/auth/session.py",
            start_line=12,
            end_line=30,
            why="Refresh the session token (Session.refresh)",
            score=0.9,
        ),
        Lead(path="src/db/store.py", start_line=1, end_line=40, why="Persistence layer (Store)", score=0.5),
        Lead(
            path="src/very/deeply/nested/module/directory/structure/impl.py",
            start_line=100,
            end_line=200,
            why="Entry point (run)",
            score=0.1,
        ),
    ]


class TestRenderPlan:
    def test_full_format(self) -> None:
        text = render_plan(plan_leads(), 4096)
        assert text.startswith("PLAN\nLEADS (verify, don't trust):\n")
        lines = text.splitlines()
        assert "1. src/auth/session.py:12-30 — Refresh the session token (Session.refresh)" in lines
        assert "OUTLINES: .trailmark/outlines/src/auth/session.py.md" in lines
        assert sum(line.startswith("OUTLINES:") for line in lines) == 3
        assert sum(line[:1].isdigit() for line in lines) == 3

    def test_compact_drops_why_keeps_leads(self) -> None:
        leads = plan_leads()
        full = render_plan(leads, 10**6)
        compact = render_plan(leads, byte_len(full) - 1)
        assert byte_len(compact) <= byte_len(full) - 1
        assert "—" not in compact, "compact verbosity must drop the why phrase"
        assert "src/auth/session.py:12-30" in compact
        assert "src/very/deeply/nested/module/directory/structure/impl.py:100-200" in compact

    def test_drops_whole_entries_under_pressure(self) -> None:
        leads = plan_leads()
        full = render_plan(leads, 10**6)
        compact = render_plan(leads, byte_len(full) - 1)
        assert "—" not in compact
        trimmed = render_plan(leads, byte_len(compact) - 1)
        assert byte_len(trimmed) <= byte_len(compact) - 1
        numbered = [line for line in trimmed.splitlines() if line[:1].isdigit()]
        assert len(numbered) == 2, "entries must be dropped whole, never truncated"

    def test_header_only_fallback(self) -> None:
        long_lead = plan_leads()[2]
        full = render_plan([long_lead], 10**6)
        compact = render_plan([long_lead], byte_len(full) - 1)
        assert byte_len(compact) > 120, "single long-path lead must exceed the minimum"
        minimal = render_plan([long_lead], byte_len(compact) - 1)
        assert minimal == "PLAN\nLEADS (verify, don't trust):"

    @pytest.mark.parametrize("budget", [4096, 2048, 512, 200, 120, 119, 100, 33, 0])
    def test_budget_sweep(self, budget: int) -> None:
        text = render_plan(plan_leads(), budget)
        if budget < 120:
            assert text == ""
        else:
            assert text != ""
            assert byte_len(text) <= budget
            assert text.startswith("PLAN\n")


# ---------------------------------------------------------------------------
# outlines
# ---------------------------------------------------------------------------


class TestOutlines:
    def test_outline_content(self, tmp_path) -> None:
        cache = IndexCache(tmp_path / ".trailmark")
        cache.save(make_data(root=str(tmp_path)))
        data = cache.load()
        assert data is not None
        out_dir = tmp_path / "outlines"

        written = generate_for(data, ["src/auth/session.py"], out_dir)
        assert written == ["src/auth/session.py.md"]
        text = (out_dir / "src/auth/session.py.md").read_text(encoding="utf-8")
        lines = text.splitlines()
        assert lines[0] == "# src/auth/session.py"
        assert lines[1] == "L1-4: def _hash_password(pw: str) -> bytes"
        assert lines[2] == "L6-70: class Session — User session state and lifecycle."
        assert (
            "  L12-30: method refresh(self, ttl: int) -> str — Refresh the session token for a user."
            in lines
        )
        # symbols sorted by line
        anchors = [int(line.split(":")[0].lstrip(" L").split("-")[0]) for line in lines[1:]]
        assert anchors == sorted(anchors)

    def test_truncation_cap(self, tmp_path) -> None:
        big = FileEntry(
            path="src/big.py",
            lang="py",
            mtime=1.0,
            size=10_000,
            symbols=[
                Symbol(file="src/big.py", name=f"f_{i:04d}", kind="def", line=i + 1, end_line=i + 1, signature=f"def f_{i:04d}() -> int")
                for i in range(250)
            ],
        )
        data = IndexData(root=str(tmp_path), files={"src/big.py": big}, refs=[])
        out_dir = tmp_path / "outlines"
        written = generate_for(data, ["src/big.py"], out_dir)
        assert written == ["src/big.py.md"]
        lines = (out_dir / "src/big.py.md").read_text(encoding="utf-8").splitlines()
        assert len(lines) == MAX_LINES
        assert lines[-1] == TRUNCATION_MARK
        assert lines[0] == "# src/big.py"

    def test_idempotent_noop_rewrite(self, tmp_path) -> None:
        cache = IndexCache(tmp_path / ".trailmark")
        cache.save(make_data(root=str(tmp_path)))
        data = cache.load()
        assert data is not None
        out_dir = tmp_path / "outlines"
        generate_for(data, ["src/auth/session.py"], out_dir)
        target = out_dir / "src/auth/session.py.md"
        before_content = target.read_text(encoding="utf-8")
        before_mtime = target.stat().st_mtime_ns

        again = generate_for(data, ["src/auth/session.py"], out_dir)
        assert again == ["src/auth/session.py.md"]
        assert target.read_text(encoding="utf-8") == before_content
        assert target.stat().st_mtime_ns == before_mtime, "unchanged outline must not be rewritten"

    def test_missing_paths_skipped_and_top_generation(self, tmp_path) -> None:
        cache = IndexCache(tmp_path / ".trailmark")
        cache.save(make_data(root=str(tmp_path)))
        data = cache.load()
        assert data is not None
        out_dir = tmp_path / "outlines"
        assert generate_for(data, ["nope/missing.py"], out_dir) == []

        top = generate_top(data, out_dir, k=3)
        assert 0 < len(top) <= 3
        for rel in top:
            assert (out_dir / rel).exists()
        again = generate_top(data, out_dir, k=3)
        assert again == top
        assert generate_top(IndexData(root="", files={}, refs=[]), out_dir, k=3) == []


# ---------------------------------------------------------------------------
# cli handler
# ---------------------------------------------------------------------------


class TestCliFocus:
    def test_end_to_end(self, tmp_path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        monkeypatch.delenv("CLAUDECODE", raising=False)
        cache = IndexCache(tmp_path / ".trailmark")
        cache.save(make_data(root=str(tmp_path)))
        (tmp_path / "AGENTS.md").write_text("user notes\n", encoding="utf-8")
        install_fake_atlas(monkeypatch)

        args = make_args(root=str(tmp_path), issue=ISSUE, json=True)
        assert cli_focus.run(args) == 0

        agents = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
        assert "user notes" in agents, "content outside the block must survive"
        assert read_block(tmp_path / "AGENTS.md") == "TEST BODY"
        assert (tmp_path / ".trailmark" / "outlines" / "src/auth/session.py.md").exists()

        payload = json.loads(capsys.readouterr().out)
        assert payload["cli"] == "generic"
        assert payload["changed"] is True
        assert payload["bytes"] == byte_len("TEST BODY")
        assert payload["leads"][0]["path"] == "src/auth/session.py"
        assert 0.0 <= payload["leads"][0]["score"] <= 1.0

    def test_block_write_is_idempotent(self, tmp_path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        monkeypatch.delenv("CLAUDECODE", raising=False)
        cache = IndexCache(tmp_path / ".trailmark")
        cache.save(make_data(root=str(tmp_path)))
        install_fake_atlas(monkeypatch)
        args = make_args(root=str(tmp_path), issue=ISSUE, json=True)

        assert cli_focus.run(args) == 0
        assert json.loads(capsys.readouterr().out)["changed"] is True
        assert cli_focus.run(args) == 0
        assert json.loads(capsys.readouterr().out)["changed"] is False

    def test_issue_file_and_cli_passthrough(self, tmp_path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        issue_file = tmp_path / "issue.txt"
        issue_file.write_text(f"{ISSUE}\n", encoding="utf-8")
        cache = IndexCache(tmp_path / ".trailmark")
        cache.save(make_data(root=str(tmp_path)))
        install_fake_atlas(monkeypatch)

        args = make_args(root=str(tmp_path), issue_file=str(issue_file), cli="codex", json=True)
        assert cli_focus.run(args) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["cli"] == "codex"
        assert payload["leads"], "issue from --issue-file must drive ranking"

    def test_missing_cache_exit_2(self, tmp_path, capsys) -> None:
        args = make_args(root=str(tmp_path), issue=ISSUE)
        assert cli_focus.run(args) == 2
        assert "run `trailmark init` first" in capsys.readouterr().err

    def test_empty_issue_exit_2(self, tmp_path, capsys) -> None:
        cache = IndexCache(tmp_path / ".trailmark")
        cache.save(make_data(root=str(tmp_path)))
        assert cli_focus.run(make_args(root=str(tmp_path), issue="   ")) == 2
        assert "no issue text" in capsys.readouterr().err

    def test_stdin_issue_exit_2_when_empty(self, tmp_path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        class _EmptyStdin:
            def read(self) -> str:
                return ""

        monkeypatch.setattr(sys, "stdin", _EmptyStdin())
        cache = IndexCache(tmp_path / ".trailmark")
        cache.save(make_data(root=str(tmp_path)))
        assert cli_focus.run(make_args(root=str(tmp_path))) == 2
        assert "no issue text" in capsys.readouterr().err

    def test_missing_atlas_falls_back_to_plan_body(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "trailmark.atlas", None)  # force ImportError
        cache = IndexCache(tmp_path / ".trailmark")
        cache.save(make_data(root=str(tmp_path)))

        args = make_args(root=str(tmp_path), issue=ISSUE)
        assert cli_focus.run(args) == 0
        body = read_block(tmp_path / "AGENTS.md")
        assert body is not None and body.startswith("PLAN\nLEADS (verify, don't trust):")
        assert "src/auth/session.py" in body
