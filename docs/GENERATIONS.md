# codegraph — Optimization Generations

Every change to the codegraph layer is one **generation**: a codegraph commit, a frozen
config, and one paired A/B on the standard SWE-bench Verified pipeline. Nothing is ever
compared across different subsets, different agent models, or different seeds counts.
This ledger is the single history of what changed and what it bought.

## Fixed protocol

- **Benchmark**: SWE-bench Verified, standard pipeline (official images, stock CLI, official grading).
- **Agent under test**: opencode-hyde (`opencode run --format json --auto`, model `hyde-inference/hyde/glm-5.3-flash`).
- **Cost metric**: **tokens/task = cost** (hyde-inference has no public pricing; $ columns are
  placeholders and informational only). Primary metrics: tokens/task, turns/task.
- **Subset**: `bench/subsets/verified-20.jsonl` — 20 instances, stratified across all 12 repos,
  deterministic (no RNG). Regenerate only with a documented reason; never compare across subsets.
- **Arms**: `control` = stock container; `treatment` = container + `codegraph init && codegraph focus`.
- **Seeds**: 2 (temperature-0 agents still vary run-to-run; 2 seeds bound the noise, 3 for headline claims).
- **Gates**: a generation is kept only if resolve% is within −2pp of control AND tokens/task drops
  beyond the seed-noise floor. Otherwise revert the change and document why.
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
