# codegraph — Optimization Generations

Every change to the codegraph layer is one **generation**: a codegraph commit, a frozen
config, and one paired A/B on the standard SWE-bench Verified pipeline. Nothing is ever
compared across different subsets, different agent models, or different seeds counts.
This ledger is the single history of what changed and what it bought.

## Fixed protocol

- **Primary dev signal — mini-bench** (`bench/minibench.py`): local, docker-free A/B on one real
  repo (psf/requests) × 5 realistic tasks. Control (no codegraph) vs treatment (focus block),
  tokens/task = cost. This is the per-generation iteration loop — minutes, not hours.
- **Standard check — SWE-bench Verified** (`bench/run.py` + `bench/prepare_images.py`):
  subset `bench/subsets/verified-5.jsonl` (5 instances, one per repo) × 2 arms × 1 seed = 10 runs.
  Run it occasionally (before a GitHub release, or when mini-bench and expectations disagree).
  `verified-20.jsonl` exists for a headline run if ever needed. Official eval images are NOT on
  any registry — build locally first: `uv run python -m bench.prepare_images --ids-file
  bench/subsets/verified-5.jsonl --workers 3`.
- **Agent under test**: opencode-hyde (`opencode run --standalone --format json --auto`,
  model `hyde-inference/hyde/glm-5.3-flash`).
- **Cost metric**: **tokens/task = cost** (hyde-inference has no public pricing; $ columns are
  placeholders and informational only). Primary metrics: tokens/task, turns/task.
- **Seeds**: 1 for dev iterations; 2–3 for anything quoted publicly.
- **Gates**: a generation is kept only if task success (patch produced / tests where runnable)
  stays equal AND tokens/task drops beyond seed noise. Otherwise revert and document why.
- **Standing reference**: the Gen-0 control run is reused by all later generations (re-run only
  when the agent model or opencode version changes).

## Per-generation record format

```
## Gen N — <short name>
- date / commit:
- change: <what changed in the layer, one paragraph>
- config: budget=<B> rules=<variant> focus-weights=<0.45/0.25/0.15/0.15> ...
- runs: <N> instances × <arms> × <seeds>
- control:  tokens/task=<..> turns=<..> cache-read%=<..> resolve%=<..>
- treatment: tokens/task=<..> turns=<..> cache-read%=<..> resolve%=<..>
- delta: tokens/task <Δ%> [95% CI], turns <Δ%>, resolve <Δpp>
- verdict: KEEP | REVERT | ITERATE (one sentence why)
- next hypothesis: <the single most promising next change>
```

---

## Gen 0 — reference baseline + first effect size

- **date**: 2026-10-24 · **commit**: `768bcc7` + runner/opencode alignment (post-parity)
- **change**: none under test — this generation establishes (a) the standing control reference
  and (b) the first control-vs-treatment delta of the full layer as built.
- **config**: block budget=2048B · rules=`generic` (opencode → generic variant) ·
  focus weights 0.45 lexical / 0.25 graph-proximity / 0.15 test-proximity / 0.15 prior ·
  leads: single source of truth = `focus.ranker` · outlines on top leads.
- **runs**: 20 instances × {control, treatment} × 2 seeds = 80 runs.
- **control**: PENDING
- **treatment**: PENDING
- **delta**: PENDING
- **verdict**: — (reference generation)
- **next hypothesis**: rule phrasing matters more than rule presence (PointFive,
  arXiv 2608.01347) — if the delta is < 10%, A/B the RULES section alone (atlas-only arm)
  before touching ranking weights.

### Local sanity numbers (not benchmark results)

| probe | value |
|---|---|
| init (cold, 4001-file synthetic) | 3.2 s |
| init (warm, requests) | 0.02 s |
| focus (warm, requests) | 37 ms |
| block size | 2012 / 2048 B |
| opencode cost-adapter | verified against a real `--format json` capture (1538 in / 3456 cache-read / 101 out / 1 step) |

## Running a generation

```bash
# 0) one-time: enable Docker Desktop → WSL integration, then pull/build images
uv sync --extra bench
uv run python -m bench.make_subset --total 20 --out bench/subsets/verified-20.jsonl  # only when intentionally changing the subset

# 1) the generation run (resumable; safe to Ctrl-C and re-run)
uv run python -m bench.run \
  --instances-file bench/subsets/verified-20.jsonl \
  --cli opencode --arm control,treatment --seeds 2 --parallel 4 \
  --out runs/gen0 --timeout 1800

# 2) grading (official; sb-cli fallback printed automatically)
uv run python -m bench.run --grade --out runs/gen0

# 3) report
uv run python -m bench.report --out runs/gen0   # paired deltas + bootstrap CI
```

Then fill the Gen-N entry above from `bench.report` output and commit.

## Gen 0.1 — orientation suite: FIRST PAIRED RESULT — verdict: KEEP

- **date**: 2026-10-24 · **commit**: `8f8d7e1` (layer v0.1, single agnostic rules variant)
- **suite**: `requests-orient-3.jsonl` (3 orientation tasks) × control/treatment × 1 seed
- **result**: tokens/task **53,106 → 19,554 (−63%)**, CI [−44K, −23K] (excludes 0);
  turns/task **6.0 → 2.7 (−55%)**, CI [−5.0, −2.0]; all 6 runs completed; treatment init 0.4s
- **analysis** (`bench.analyze`): control runs re-read the same file up to 3×
  (`subsumed-read` ×3-4 per run, `repeat-command` ×2-3); treatment runs went straight
  to target in 2 turns. Weakest case `orient-redirect-auth` (−25%) — header-trace spans
  files the map only partially covers.
- **diagnosed during run**: the agent (with --auto) ran git hygiene that deleted
  `.codegraph/` → index rebuilt per task (0.4s, no token cost). Fixed bench-side:
  layer footprint registered in `.git/info/exclude`. Candidate for `codegraph init`
  (`--protect`) in a later generation.
- **verdict**: **KEEP** — H4 confirmed (orientation = map's home turf). Directionally
  strong (n=3, 1 seed); confirmation on the bugfix suite pending (costlier: ~0.5-1M tokens).

## Gen 0.2 — feature suite: turns cut, tail dominates — verdict: KEEP + ITERATE RULES

- **date**: 2026-10-24 · **commit**: `723d042` era (layer v0.1 + multi-harness)
- **suite**: `requests-feature-3.jsonl` × control/treatment × 1 seed
- **result**: turns 121→95 (−21%); tokens 3.81M→3.56M (−6%); NOTE: feat-retries control was
  tainted (mid-run kill) → purged, pair re-running. Per-task: onrequest −23% tok (53→47 turns),
  verify-precedence **−48% turns** (42→22) but tokens −3%.
- **analysis**:
  1. Feature cost is dominated by implementation/debug **tail** loops (42–53-turn controls),
     not discovery — map+leads cut discovery but the tail burns cache-replay.
  2. **Stop-loss never bound** (47–53 turns) — rules cover search loops, not edit-verify
     stalls ("progress stalls" behavior, arXiv 2609.30725).
  3. `subsumed-read`/`repeat-command` UP under treatment (12/14 vs 1/9 on onrequest) —
     5-file lead lists invite ping-pong reads.
- **planned improvements (next generations)**:
  - G-imp1: progress-stall rule — "same test failure twice OR 2 failed searches → change
    hypothesis explicitly or stop and report"; plus turn-budget awareness near the end.
  - G-imp2: lead phrasing — "one ranged read per lead, then act; do not re-open a lead
    without a new hypothesis".
  - G-imp3: trails auto-deposit from mini-bench (solved→hot, failed→dead ends) so the
    second run of a repo starts with memory.
- **verdict**: KEEP (no task completion regression; turns down) — rule/lead wording iteration next.

## Iteration backlog (from the v0 baseline datapoint)

First real control run (no codegraph, `req-redirect-cookies`): **89 turns, 250K input +
4.3M cache-read (93.4% of all tokens) + 57K output, 655s**. Since tokens ARE the cost
(hyde-inference), and cache-read dominates total tokens, the per-turn replay is the
burn — so **turns/task is the headline KPI**; every hypothesis below targets turn count.

| # | Hypothesis | Expected lever | How measured |
|---|---|---|---|
| H1 | Control shows unbounded search loops (89 turns ≫ needed); the layer's stop-loss + map cut exploration turns 30-50% | Flight rules (stop-loss wording), MAP | turns/task, `subsumed-read` + `repeat-command` counts in `bench.analyze` |
| H2 | Leads (preflight) delete the first ~5-10 discovery turns; biggest single cut available | Preflight PLAN | turns to first edit; lead hit@k vs task-relevant files |
| H3 | 93.4% cache-read share means per-turn uncached input is small (~2.8K) — map+rules cost ≈ one turn-equivalent; net positive iff ≥1 discovery turn is deleted | whole layer | tokens/task net delta |
| H4 | Orientation tasks (orient-3 suite) benefit most: near-zero exploration when the map answers "where is X" | Atlas + glossary | turns/task on orient suite |
| H5 | The 89-turn run ended in a hard failure (exit -9) — runaway trajectories correlate with failure; capping wandering may RAISE success rate, not just cut cost | stop-loss + TESTS discipline | success proxy (patch produced) alongside tokens |

Rule for iterations: one change per generation, mini-bench (5 bugfix + 3 orient tasks,
1 seed) → `bench.analyze` → keep/revert by turns/task and the waste table.

## Mini-bench generations (primary loop)

```bash
# one generation = change codegraph → treatment re-run → analyze → ledger entry
uv run python -m bench.minibench --repo /tmp/opencode/demo-target/requests \
  --tasks bench/tasks/requests-5.jsonl --arm control,treatment --seeds 1 \
  --out runs/mini<gen>            # control is cached/resumable after the first run
uv run python -m bench.analyze --out runs/mini<gen>   # waste table + arm comparison
```

Other task suites stress different mechanisms: `requests-orient-3.jsonl` (atlas/preflight),
`requests-feature-3.jsonl` (edit discipline + rules). Always re-run the arm you changed only —
the standing control reference is reused.
