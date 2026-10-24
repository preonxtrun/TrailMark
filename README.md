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

## Real workflows

One-time per repo:

```bash
codegraph init
# keep the map fresh automatically on every commit:
printf '#!/bin/sh\ncommand -v codegraph >/dev/null && codegraph init --quiet --root "$(git rev-parse --show-toplevel)" &\n' > .git/hooks/post-commit && chmod +x .git/hooks/post-commit
```

Per task (any coding CLI, no wrapper needed — the CLI reads the files on its own):

```bash
codegraph focus "implement retry logic for failed uploads in the sync module"
# then just launch your CLI (opencode / codex / claude code) as usual
```

Quality-of-life checks: `codegraph doctor` (cache freshness, block present, budget ok) and
`codegraph stats`. To measure the effect on your own workflow: `python -m bench.minibench`
(see `docs/GENERATIONS.md`).

## Multi-agent runs

Two supported patterns (both proven by the benchmark harness, which uses them itself):

1. **Sequential tasks, one worktree** — re-run `codegraph focus "<next task>"` before each
   agent run; the block is idempotent and hash-stable, so re-focusing is instant.
2. **Concurrent agents — one worktree each** (the safe pattern): agents never share a
   worktree, so each has its own block, index, and leads:
   ```bash
   git worktree add --detach ../wt-a HEAD && git worktree add --detach ../wt-b HEAD
   (cd ../wt-a && codegraph init && codegraph focus "task A") &
   (cd ../wt-b && codegraph init && codegraph focus "task B") &
   ```
   `bench.minibench --parallel 3` runs exactly this automatically. Never point two running
   agents at one worktree — they'd fight over the single block's task leads.

## Multi-harness adapters

Cursor rules, Copilot instructions and GEMINI.md don't natively read AGENTS.md — init can
write tiny pointer files (never duplicated content, so they can't go stale):

```bash
codegraph init --harness claude,gemini,cursor,copilot
# → CLAUDE.md (symlink), GEMINI.md, .cursor/rules/agents-md.mdc, .github/copilot-instructions.md
```

## How it compares

See `docs/COMPARISON.md` — the honest teardown of aider, Sourcegraph Code Finder, repomix,
Claude/Cursor memory, TokenEconomizer and others: what we adopted, what we rejected, and
the seven differentiators no tool combines.

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
