# How codegraph compares — what we learned from everyone

The space is active. This is the honest teardown of the best-known tools, what we
adopted from each, and where the deliberate differences are.

| Tool | Their trick | What we took | What we do differently |
|---|---|---|---|
| **aider repo-map** | tree-sitter + personalized PageRank, token-budget binary search, ranks around *chat files* | The core map algorithm (our "prior"); budget binary-search idea; seeding from task text (= their chat-file personalization, conditioned on the issue instead) | Ours is a **standalone file layer**: any CLI, not just aider. Plus test-graph, glossary, profiles, preflight — none of which aider has |
| **Sourcegraph Code Finder** (2026-07) | Agentic search loop hands the agent exact files+line ranges "cheaper than the agent searching itself" | The insight that discovery turns are the cost | They intercept at runtime via **MCP**; we **precompute files** — zero runtime surface, works in locked-down containers, obeys our G6/G9 guarantees. Their own research paper (arXiv 2609.30725) showed runtime tools can *add* 8–28% cost; we're the static extreme |
| **repomix / gitingest / Repo Prompt** | Repo packing, token counting, CodeMaps, skill generation | Budget accounting; the value of curated (not dumped) context | They're **human-curated snapshots**; we're **automatic and per-task** (preflight re-ranks per issue). They dump; we navigate |
| **Claude Code auto-memory / Cursor memories** | Write learnings across sessions | The cross-task persistence idea (= our Trails generation, planned) | They're **vendor-locked and session-shaped**; ours is file-based, portable, and repo-indexed. Also their memory has no cost model |
| **cairn-framework** | Blueprint → map → drift gate at commit boundary | "Context as enforced artifact" | They gate *architecture conformance*; we optimize *cost per task*. Complementary, different goal |
| **agentsgen / agents-repo / OpenWiki** | Auto-generate AGENTS.md conventions | The AGENTS.md channel itself (their cautionary tale: generic overviews measured **+20% cost**, arXiv 2609.30725-adjacent ETH result) | We never generate prose overviews — the block is a **structural index + behavioral rules** only, and our ledger gates every change against the baseline |
| **TokenEconomizer** (arXiv 2610.22732) | Micro-skills injection, tool pruning, loop intervention across task sequences | Cross-task learning premise (validated: −18.4–30.9%) | They modify the CLI (tool pruning, runtime intervention); we **never touch it**. Complementary — could stack |
| **agents-lint** | Staleness detection for context files | `codegraph doctor` staleness checks | We also auto-refresh (watch mode, git hook) instead of only linting |
| **GEPA** (ICLR '26) | Reflective prompt evolution, 35× fewer rollouts than RL | Noted for the backlog | Rejected for now: needs an eval meta-loop around the CLI; our flight rules ship hand-written (the measured −41.7% mechanism) |

## The differentiators that no tool combines

1. **Standalone + CLI-agnostic + zero runtime** — files only; one AGENTS.md block; no MCP, no
   plugins, no proxies. Any AGENTS.md-reading CLI inherits it (G9 test-enforced).
2. **Task-conditioned preflight** — the block re-ranks per issue (graph-seeded lexical ranking),
   not a static summary.
3. **Test graph** — file→test mapping from import analysis; kills test-discovery and re-run waste.
4. **Profile mode** — user-authored facts/rules for non-indexed workloads (greenfield, clusters,
   experiment loops): the developer-designed-skills mechanism, file-based.
5. **Measured, not claimed** — every generation runs a paired A/B on the same tasks with a
   ledger (Gen 0.1: −63% tokens / −55% turns, CIs exclude zero).
6. **Guarantee suite** — non-destruction, purity, no-leakage, never-crash, time budgets:
   properties no competitor tests for.
7. **Dual implementation** — Python + Rust cores kept in byte-identical parity; each proof-tests
   the other.

## Closest products, positioning in one line each

- *Sourcegraph Code Finder*: runtime search replacement (MCP, per-query cost). Ours: precomputed
  navigation layer (per-task zero marginal cost).
- *aider*: best-in-class map locked inside aider. Ours: the same class of map for every CLI.
- *repomix/Repo Prompt*: context curation for humans. Ours: curation for agents, automatic.
- *TokenEconomizer*: behavioral optimizer that edits the CLI. Ours: navigation optimizer that
  edits nothing — they stack.
