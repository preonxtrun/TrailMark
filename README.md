# codegraph

A **hidden optimization layer** between coding CLIs (opencode, Codex, Claude Code) and the
codebase. It writes plain files the CLIs already read — nothing else. No MCP, no plugins, no
proxies, no CLI modification, no runtime LLM calls.

```
standard pipeline:   official task → container setup → STOCK CLI → official grading
codegraph footprint: one extra setup step:  codegraph init && codegraph focus "<issue>"
                     (writes an AGENTS.md block + .codegraph/ index — plain, auditable files)
```

## Install

```bash
uv tool install /path/to/codegraph   # or: pip install .
```

## Usage (inside any repo)

```bash
codegraph init                 # index repo → atlas + test graph + flight rules (<60s cold)
codegraph focus "issue text"   # preflight: predicted files + leads injected into the block
codegraph remove --purge       # strip everything back out
```

The injected `AGENTS.md` block is byte-budgeted (default 2048 bytes), hash-stable, and delimited
by `<!-- codegraph:start v1 -->` / `<!-- codegraph:end -->` markers. Nothing outside the markers
is ever touched.

## Benchmark (SWE-bench Verified, standard pipeline)

See `bench/README.md`. Control arm = stock container; treatment arm = container + one setup
command. Both run the identical standard pipeline and are graded by the official harness.

## Layout

- `src/codegraph/index/` — tree-sitter symbol graph, PageRank, sqlite+FTS5 cache
- `src/codegraph/atlas/` — map/test-graph/glossary/rules assembly under byte budget
- `src/codegraph/focus/` — issue-conditioned preflight ranking, flight plan, outlines
- `src/codegraph/emit/` — hash-stable AGENTS.md block writer
- `bench/` — standard-pipeline SWE-bench runner (control vs treatment arms)
- `SPEC.md` — frozen contracts every module builds against
