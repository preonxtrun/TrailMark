# trailmark — Build Spec v1 (frozen contracts)

Read this fully before writing code. The data model, cache API, and block emitter already exist;
build against them, do not change their public interfaces.

## Mission

A hidden optimization layer between any coding CLI (opencode / codex / claude code) and the
codebase. Pure file injection: a marked block in `AGENTS.md` plus `.trailmark/` state.
No MCP, no plugins, no proxies, no CLI modification, no runtime LLM calls.
Goal: −20..−40% $/task on SWE-bench Verified at noise-level resolve% delta.

## Hard invariants (apply to every module)

1. **Determinism.** Same (repo state, issue text, cli, budget) → byte-identical outputs.
   Sort everything; stable tie-break by `(path, name)`; never emit timestamps or raw floats.
2. **Budget.** Injected block ≤ `budget` bytes (default 2048). Drop whole sections/entries by
   priority — never truncate mid-entry, never exceed budget. Drop order: GLOSSARY → PLAN
   (verbose→compact→omit) → extra TESTS rows → extra MAP rows. RULES and ≥3 MAP rows survive
   at any budget ≥ 512 bytes.
3. **No leakage.** Never emit FAIL_TO_PASS / PASS_TO_PASS / gold-patch information. The only
   task input is the issue/problem-statement text.
4. **Never crash.** Unsupported language → regex fallback extractor. Missing cache → clean
   error "run `trailmark init` first" (exit 2). Malformed repo → best-effort index.
5. **CLI untouched.** We only write files under the repo. Nothing may hook, patch, or wrap the
   coding CLI. Block lives between `BLOCK_START`/`BLOCK_END` markers; nothing outside them is
   modified.
6. **Performance.** init <60s cold / <2s warm on a ~3k-file repo (22 cores available; use
   ProcessPoolExecutor — tree-sitter parsing is GIL-bound). focus <1s warm.
   Heavy optimization required: mtime-incremental indexing, sqlite FTS5 for BM25 (no external
   ranker), numpy PageRank, zero-copy string assembly, no repeated disk reads.

## Architecture

```
index/   scanner → parser (tree-sitter, parallel) → symbol def/ref graph → PageRank → cache
atlas/   IndexData → map / test-graph / glossary / rules → byte-budgeted block body
focus/   (IndexData, issue) → ranked leads → PLAN section + outlines; full block regen
emit/    hash-stable AGENTS.md marked-block writer (exists; do not change API)
bench/   standard SWE-bench pipeline runner: control vs treatment arm
```

## Shared contracts (already implemented — build against these)

- `model.py`: `Symbol(file,name,kind,line,end_line,signature,doc)`, `Ref(src_file,dst_name,dst_file,line)`,
  `FileEntry(path,lang,mtime,size,symbols)`, `IndexData(root,files,refs)` with `.symbols()`,
  constants `BLOCK_START`, `BLOCK_END`, `DEFAULT_BUDGET_BYTES=2048`.
- `cache.py`: `IndexCache(trailmark_dir)` with `load() -> IndexData | None`, `save(IndexData)`,
  `search(query, k) -> list[Hit]` (FTS5 BM25 over symbols; lower bm25 = better), `close()`.
- `emit/agents_md.py`: `read_block(path)`, `write_block(path, body) -> bool` (True if changed;
  creates file if missing, preserves content outside markers, no-op when body unchanged),
  `remove_block(path)`, `render(body)`.
- `util/tokens.py`: `estimate_tokens(text)`.

## Block format (emitted into AGENTS.md)

Order and format (sections dropped per budget rules above):

```
# TRAILMARK ATLAS — auto-generated, do not edit inside markers
RULES
<flight rules, wrapped tight, per-cli variant>
MAP
- <dir>/ — <role>; hot: <path>:<line> <Symbol()>
- ...   (3..10 rows, page-rank ordered, with line anchors)
TESTS
- runner: <exact command>
- <src.py> → <test path>            (top rows only)
PLAN
LEADS (verify, don't trust):
1. <path>:<line>-<end> — <why>      (focus only, top-5)
OUTLINES: .trailmark/outlines/<rel>.md
GLOSSARY
- <term> = <meaning> (<path>:<line>)
```

## Flight rules content (atlas/rules.py)

Fixed hand-written text, three variants: `generic`, `claude`, `shell`.
Base (prohibitive, turn-shaped, ~700 bytes wrapped):
- Consult MAP/TESTS before any grep; state a concrete hypothesis before each retrieval.
- Batch all candidate file reads into ONE parallel tool block; use line-ranged reads for
  files >200 lines; after editing, re-read only the edited range.
- Run the exact test target from TESTS; never rerun a test that passed with unchanged inputs.
- Persist probes to one scratch file and edit it; never regenerate similar scripts.
- Stop-loss: after 2 failed searches, re-read the map — do not keep guessing.
Variants: `claude` adds "Require file:line references in subagent summaries; never re-retrieve
code a subagent already covered." `shell` adds "One edit per change; verify with a single
targeted `sed -n 'A,Bp'`; persist probe scripts — do not regenerate variants."

## CLI contract (cli.py)

```
trailmark init   [--root .] [--budget N] [--langs py,ts,js,go,rs] [--workers N] [--force]
trailmark focus  "ISSUE" | --issue-file F  [--cli auto|claude|codex|opencode|generic] [--budget N]
trailmark remove [--purge]
trailmark stats
trailmark doctor
trailmark watch  [--interval S]        # optional, simple polling loop
```

- `init`: build/update index → outlines for top-32 PageRank files → write atlas-only block.
- `focus`: requires cache; regenerates FULL block (atlas + PLAN) from cache + issue; writes
  outlines for predicted files; `--cli auto` resolves via env (`CLAUDECODE` → claude, else
  generic). Exit 0 on success, 2 on "init first".
- All commands: `--json` flag prints machine-readable result (bench consumes this).

## Module signatures (cross-agent interfaces — implement exactly)

- `index/__init__.py`: `build(root, langs, workers, cache) -> IndexData` and
  `update(root, langs, workers, cache) -> IndexData` (mtime-incremental).
- `index/graph.py`: `pagerank(files, refs, damping=0.85, iters=30, personalization=None) -> dict[str, float]`
  (file-level scores; edge weight = ref count; deterministic).
- `atlas/__init__.py`: `assemble(data: IndexData, issue: str | None, cli: str, budget: int, leads: list | None = None) -> str`
  (returns block BODY; deterministic; caller writes via emit). When *leads* (focus
  `Lead` objects from `focus.ranker.rank`) are provided they drive the PLAN section —
  focus.ranker is the single source of truth for leads; atlas's internal issue-keyword
  scorer is only the fallback when leads are absent.
- `focus/cli_focus.py`: `run(args: argparse.Namespace) -> int` (imported lazily by cli.py).
- `index/cache extras`: `file_mtimes() -> dict[str, float]` for incremental updates.

## bench/ (standard pipeline, separate package — no imports from src/ except none)

Subcommands under `bench/run.py` (argparse, resumable, parallel containers):

```
python -m bench.run --dataset princeton-nlp/SWE-bench_Verified --slice 0:100 \
       --cli claude --arm control,treatment --seeds 3 --parallel 4 --out runs/<id> [--grade] [--dry]
```

- Per (instance, arm, seed): official image `swebench/sweb.eval.x86_64.<instance>`; treatment
  execs the setup (pip install -e mounted src; `trailmark init`; `trailmark focus --issue-file
  /tmp/issue.txt --cli <cli>`); agent run via stock headless CLI inside container; patch
  extracted with `git diff -- . ':(exclude).trailmark' ':(exclude)AGENTS.md'`; usage parsed by
  `bench/cost.py` adapters; record JSONL per run; `--grade` runs official grading (swebench
  harness or prints exact `sb-cli` command when unavailable).
- `cost.py`: adapters for claude stream-json (`usage`: input/cache_read/cache_creation/output
  tokens, model), codex `exec --json`, opencode; defensive parsing with documented assumptions;
  pricing table dict per model with fallback; cost = in*p_in + cr*p_cr + cw*p_cw + out*p_out.
  Must be fully testable WITHOUT docker via `bench/fixtures/*.jsonl` (handcraft small samples).
- `report.py`: paired control-vs-treatment stats — mean $/task, tokens/task, cache-read share,
  resolve%; bootstrap 95% CI (10k resamples, paired by instance); markdown table out.
- NO docker in this dev environment: everything unit-testable without it; `--dry` validates
  plans. No-leakage rule enforced in code (assert problem_statement is the only task text used).

## Ownership (do not touch files outside your list)

- INDEX: `src/trailmark/index/**`, `src/trailmark/cache.py` (extend, keep API), parser deps in
  `pyproject.toml` (only if the language pack is unavailable), `tests/test_index*.py`
- ATLAS: `src/trailmark/atlas/**`, `src/trailmark/cli.py`, `src/trailmark/emit/**`,
  `tests/test_atlas*.py`, `tests/test_emit.py`
- FOCUS: `src/trailmark/focus/**`, `tests/test_focus*.py`
- BENCH: `bench/**`, `tests/test_bench*.py`
- Shared (frozen): `model.py`, `util/**`, `emit/agents_md.py` API, `pyproject.toml` (except
  INDEX's parser deps), `SPEC.md`, `README.md`

## Quality bar

- Type hints + docstrings on public functions; ruff clean (`uv run ruff check src bench tests`);
  owned tests green (`uv run pytest tests/<yours> -q`); no TODOs left in code.
- Environment: Python 3.12, `~/.local/bin/uv` on PATH, pip network OK, 22 cores, NO docker.
