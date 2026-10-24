# bench — SWE-bench Verified campaign harness

Measures codegraph's effect with the **standard** SWE-bench pipeline: official instances,
official docker images, stock headless coding CLIs, official grading. The treatment arm's
entire footprint is ONE extra setup step inside the container (`pip install -e` +
`codegraph init` + `codegraph focus` — plain files: a marked block in `AGENTS.md` and
`.codegraph/` state). The control arm is the stock container, untouched.

This package is standalone: it imports **nothing** from `src/codegraph`.

## Layout

| file | purpose |
|---|---|
| `run.py` | resumable, parallel campaign runner (`python -m bench.run`) |
| `cost.py` | usage/cost adapters per CLI + pricing table |
| `arms.py` | arm setup steps, issue-file writer, no-leakage gate, patch pathspec |
| `report.py` | paired control-vs-treatment stats + markdown (`python -m bench.report`) |
| `fixtures/` | handcrafted CLI-output samples (adapters are unit-tested without docker) |

## Commands

```bash
# 0) validate everything without docker: plan + cost-adapter self-test on fixtures
python -m bench.run --instances-file inst.jsonl --slice 0:10 --cli claude \
    --arm control,treatment --seeds 1 --out runs/demo --dry

# 1) real campaign (needs docker + API keys in the env)
python -m bench.run --dataset princeton-nlp/SWE-bench_Verified --slice 0:100 \
    --cli claude --arm control,treatment --seeds 3 --parallel 4 --out runs/demo

# 2) resume after interruption: rerun the same command — completed runs are skipped
#    (runs/demo/state.json holds per-(instance,arm,seed) status; failures are retried)

# 3) grade (after the campaign): writes predictions.<arm>.s<seed>.jsonl and calls the
#    official harness when the swebench extra is installed, else prints the manual command
python -m bench.run ... --grade        # or run --grade on an already-finished --out

# 4) report: paired stats + markdown table (deterministic, seeded bootstrap)
python -m bench.report --out runs/demo --bootstrap 10000 --seed 0
```

Instances come from `--instances-file` (JSONL with `instance_id` + `problem_statement`)
or from HuggingFace via `--dataset` (lazy `datasets` import; install the extra with
`pip install -e '.[bench]'`). Exit codes: 0 = all runs completed, 1 = some runs failed
(rerun to retry), 2 = config/dataset/leakage error.

## Record schema and state

Per run, `records.jsonl` gets one append-only JSON line (sorted keys, deterministic):

```json
{"instance_id": "...", "arm": "control|treatment", "seed": 0, "cli": "claude",
 "usage": {"model": ..., "input_tokens": ..., "cache_read_tokens": ...,
           "cache_write_tokens": ..., "output_tokens": ..., "turns": ...,
           "reported_cost_usd": ..., "estimated": false},
 "cost_usd": 0.0123, "turns": 3, "patch_bytes": 1234, "status": "completed", "error": null}
```

Statuses: `completed`, `cli_error` (agent ran, non-zero exit), `timeout`,
`skipped_timeout`, `failed`. Raw patches land in `patches/<instance>.<arm>.s<seed>.patch`,
issue text in `issues/` (audit trail), grading results in `graded.<arm>.json`.

## Cost model

`cost.py` computes billing from **token math**, never from a CLI-reported cost figure
(reported figures are recorded as `usage.reported_cost_usd` for reference only):

```
cost = in*p_in + cache_read*p_cr + cache_write*p_cw + out*p_out   (prices per 1M tokens)
```

`PRICES` keys on model-id substrings (`claude-sonnet-4`, `claude-opus`, `gpt-5`,
`gpt-4.1`, `o3`, `qwen`, `kimi`, `glm`, ...); the longest matching key wins, and unknown
models fall back to `DEFAULT_PRICE = (3.0, 0.30, 3.75, 15.0)`. Prices are list prices as
of early 2026 — spot-check before a real campaign or pass `cost_usd(usage, prices=...)`.

Adapter assumptions (field paths), all defensive — unexpected output degrades to a
char/4 estimate flagged `estimated=true`, never crashes:

- **claude** (`claude -p --output-format stream-json`): tokens summed over
  `{"type":"assistant","message":{"model":...,"usage":{"input_tokens",
  "cache_creation_input_tokens","cache_read_input_tokens","output_tokens"}}}` events; the
  final `{"type":"result",...,"usage":{...},"total_cost_usd":...}` carries the CUMULATIVE
  total, so it is used only for `total_cost_usd` and as a fallback when no assistant
  event had usage (no double counting).
- **codex** (`codex exec --json`): event style `{"type":"turn.completed","usage":{
  "input_tokens","cached_input_tokens","output_tokens","reasoning_output_tokens",...}}`
  is per-turn → summed; message style `{"msg":{"type":"token_count","info":{
  "total_token_usage":{...},"last_token_usage":{...}}}}` has CUMULATIVE
  `total_token_usage` → per-bucket max, while `last_token_usage` is skipped. If both
  styles appear, totals win. `reasoning_output_tokens` is billed as output (added to the
  output bucket). A usage dict's own `total_tokens` FIELD does not make it cumulative —
  only an ancestor KEY like `total_token_usage` does.
- **opencode** (`opencode run`): defensive scan; assistant messages shaped like
  `{"type":"message.updated","info":{"role":"assistant","modelID":...,"tokens":{
  "input":..,"output":..,"cache":{"read":..,"write":..}},"cost":<float>}}` are
  per-message → summed; `turns` counts assistant messages; `reported_cost_usd` sums
  numeric `cost` fields.

## Leakage safety (SPEC invariant #3)

The only task text that ever leaves the runner is `problem_statement`:

1. `bench/run.py` has the single `problem_statement` extraction call site.
2. `arms.assert_no_leakage(record)` gates the plan (fail-closed: leaky records are
   dropped and an all-leaky slice aborts with exit 2) and every issue-file write. It
   raises when the issue text (a) mentions grading key names (`FAIL_TO_PASS`,
   `PASS_TO_PASS`, gold/test-patch markers), or (b) overlaps a forbidden field
   (`fail_to_pass`, `pass_to_pass`, `patch`, `gold_patch`, `golden_patch`,
   `test_patch`): the whole serialized value verbatim, or any diff-added `+` line /
   list element of ≥ 40 chars. Diff *context* lines are exempt by design — they are
   pre-existing repo content, not gold information; the 40-char threshold keeps naturally
   occurring short test-id mentions from false-positiving.
3. Patch extraction never sees grading data: `git add -A && git diff --cached --binary
   -- . ':(exclude).codegraph' ':(exclude)AGENTS.md'` — the agent's patch only, with
   codegraph's own footprint excluded.

## Image tags

Primary candidate: `swebench/sweb.eval.x86_64.<instance_id>:latest` with the instance id
verbatim, lowercased, docker-tag-safe (current harness convention, e.g.
`swebench/sweb.eval.x86_64.django__django-11099`). Older swebench releases mangled the
repo separator (`__` → `_117_`/`_1777_`-style), so at runtime `resolve_image()` queries
`docker images` and matches any local tag whose alphanumeric-normalized form equals the
instance id — absorbing all known separator variants. Without docker (or without a
match) the primary candidate is returned and a missing image surfaces as a normal
`docker run` error. **Verify the exact naming on your docker host with
`docker images | grep sweb.eval` before a campaign.**

## Grading

`--grade` writes `predictions.<arm>.s<seed>.jsonl` (`{instance_id, model_patch,
model_name_or_path: cli}`) per arm+seed — per-arm files rather than one combined
`predictions.jsonl` because paired resolve% requires grading BOTH arms — then tries
`from swebench.harness.run_evaluation import run_evaluation` (lazy, optional extra) with
`dataset_name` / `predictions_path` / `instance_ids` / `run_id`. On absence or failure it
prints the exact manual commands:

```bash
sb-cli submit swebench-verified <run_id> <predictions_path>
python -m swebench.harness.run_evaluation --dataset_name <dataset> \
    --predictions_path <predictions_path> --run_id <run_id> --max_workers 4
```

Per-instance swebench reports are best-effort merged into `graded.<arm>.json`
(`{instance_id: resolved_bool}`), which `report.py` reads (it also accepts a single
`graded.json` mapping either `{instance_id: bool}` or `{arm: {instance_id: bool}}`).

Report metrics: mean $/task, mean tokens/task, mean turns, cache-read share, resolve%;
paired treatment−control deltas per instance (seed-averaged) with a seeded
percentile-bootstrap 95% CI over instances for cost/tokens/turns. Same inputs →
byte-identical report (deterministic, seeded `numpy.random.default_rng`).

## Environment variables forwarded into containers

`ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_MODEL`,
`OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_API_BASE`, `OPENCODE_API_KEY`,
`OPENROUTER_API_KEY`, `MOONSHOT_API_KEY`, `DEEPSEEK_API_KEY`, `HF_TOKEN` (when set in the
host env). Add more in `bench/run.py:ENV_PASSTHROUGH`.

## Assumptions and limitations

- **No docker in the dev environment.** Everything here is unit-tested via
  `bench/fixtures/*.jsonl`, temp git repos, and `--dry`; the docker path is written but
  NOT executed here. Smoke-test with one instance (`--slice 0:1 --seeds 1`) on a docker
  host before launching a campaign.
- **CLI flag drift.** The exact agent invocations live in `bench/run.py:AGENTS`:
  `claude -p --output-format stream-json --max-turns 80` (issue on stdin),
  `codex exec --json -` (issue on stdin), `opencode run` (issue on stdin). The issue is
  piped on stdin instead of embedded in argv (equivalent, but safe for arbitrary
  multi-line text). Flags drift between CLI versions — verify each command manually on
  your machine before a real campaign, and check that the CLIs accept stdin.
- **Treatment setup** assumes the image has `pip` (or `uv`) and network access to
  install `-e /opt/codegraph-src` (the host `--src-mount`, default: this repo). The
  container self-terminates ~2 min after the agent budget; `docker rm -f` also runs in a
  `finally`.
- **Seeds are labels, not RNG seeds.** Stock CLIs are not seeded; `--seeds N` repeats
  each (instance, arm) N times for variance, and the report averages over seeds per
  instance before pairing.
- **Image-tag caveat** above; `--timeout` (default 1800s) bounds the agent step, and
  `--global-timeout` (off by default) bounds the whole campaign.
- Pricing table and the codex/opencode event formats are best-effort as of early 2026;
  re-verify against your installed CLI versions and provider pricing before spending.
