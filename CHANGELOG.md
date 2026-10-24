# Changelog

## 0.2.0 — Gen 1.1 (verified token reductions)

The first versioned release with paired-A/B measurements. Every number below is a
paired control-vs-treatment run on the same tasks, same model (opencode-hyde), same
seed protocol, with the damage-ranked analyzer and outcome gating (`--verify`).

### Measured results (psf/requests, real repo)

| suite | control | with trailmark | Δ | quality gate |
|---|---:|---:|---|---|
| orientation (3 tasks) | 63.9K tok / 5.7 turns | 29.4K / 2.7 | **−54%** | 0B patches = "don't modify" honored |
| bugfix trap task | 3.49M tok / 72 turns | 386K / 18 | **−89%** | correct no-op detected (feature already existed); control built a wrong-target patch |
| feature implementation (2 valid pairs) | 3.77M tok / 95 turns | 1.23M / 31 | **−67%** | **100% verified-solve** (full test suite passed per patch) |

### Added
- CONTRACTS (linemap signatures) + SNIPPETS (surgical symbol bodies) — displace early
  full-file reads, the highest replay-damage content in a run
- CLEARANCE manifest — ranked-irrelevant directories the agent should not explore
- BASELINE snapshots (profile-gated) — pre-run test baselines; the agent reads deltas
- Verify probes — one-call bounded edit-verify cycles with loop-escalation counters
- Trails — stigmergic cross-task memory (well-trodden paths + dead ends, evaporation)
- Scratchpad protocol — externalized working memory (.trailmark/scratch.md)
- Turn-control policy (`--max-turns`) — optional cost ceiling (arXiv 2510.16786)
- Outcome gating (`--verify`) + `verified-solve%` metric in paired reports
- Multi-harness adapters (claude/gemini/cursor/copilot pointer files)
- Diagnostics toolkit (`why` / `search` / `refs` / `block` / `selftest`)
- Damage-ranked trajectory analyzer (new_tokens × remaining turns)
- Dual implementation: Python + Rust cores, byte-identical parity suite (11 tests)

### Fixed
- Nondeterministic tree-sitter capture order (refs wobbled ±25 per run)
- Enforceable run budgets (process-group kill; no orphaned agents, no hung runners)
- Worktree path resolution (relative targets silently created worktrees inside the
  repo under test)
- Malformed-marker repair (write refuses loudly instead of nesting blocks)

### Renamed
- `codegraph` → **trailmark** (the old name collided with an unrelated tool in the
  cost literature)

## 0.1.0 — initial working layer

Atlas + preflight + profiles + bench harness. Measured −63% on orientation tasks.
