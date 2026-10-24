# Trails — cross-task stigmergy

Ants coordinate without direct communication: individuals deposit pheromones in a
shared environment, and the accumulated field steers the behavior of those that
come later (stigmergy). Trails apply the same mechanism to coding sessions —
solved tasks deposit **well-trodden paths** (the files that mattered), failed
tasks deposit **dead ends** (repellents), and every future task reads the field
as a section of the injected block. This is cross-task behavioral memory, the
effect measured at up to −41.7% token cost in the recorded evidence
(arXiv 2609.30725), extended with two additions of our design: **evaporation**
(only the last 32 entries are kept, so stale signals fade) and **negative
signals** (files touched only by failed tasks are marked "avoid blind retries").

## File format

`.trailmark/trails.jsonl` inside the repo — JSON lines, one line per task id
(latest wins), no timestamps, fixed key order:

```json
{"files": ["auth/session.py", "auth/tokens.py"], "id": "task-17", "outcome": "solved"}
```

- `id` — task identifier; re-recording an id replaces its line and moves it to
  the newest position.
- `files` — repo-relative paths that mattered in the task (deduped, sorted).
- `outcome` — `solved` or `failed`.
- The store keeps at most the last **32** entries (evaporation by truncation).

## Commands

```
python -m trailmark.trails record --root R --id TASK-17 --files auth/session.py,auth/tokens.py --outcome solved
python -m trailmark.trails clear --root R
```

Both accept `--json` for machine-readable output. No `trailmark` CLI changes and
nothing is written outside `.trailmark/`; a missing or damaged store reads as
empty and a rewrite heals malformed lines (writes are atomic replace).

## How it reaches the block

`trailmark init` / `trailmark focus` append a `TRAILS` section whenever the store
has usable entries (drop rank 7: after LAB, before PLAN — whole-section drop
under budget pressure, MAP/RULES floors always win):

```
TRAILS
- auth/session.py — well-trodden (3 solved tasks)
- auth/tokens.py — well-trodden (1 solved tasks)
DEAD ENDS (avoid blind retries):
- src/legacy/handler_old.py — dead end (failed tasks)
```

- **hot**: top 6 files by recency-weighted solved frequency — an entry *k*
  positions from the newest contributes weight `1/(k+1)` per file, so recent
  solves count more; the shown count is the raw number of solved tasks that
  touched the file.
- **dead ends**: top 4 distinct files appearing only in failed entries (never in
  solved), most-recent-first — the repellent half of the field.

## Workflow

Record after each task finishes — success or failure; failed tasks are as
valuable as solved ones, they mark the repellent field. Record the files that
actually mattered (edited, read repeatedly, or proven irrelevant on failure),
not everything the agent opened. A wrapper around the coding CLI can do this
mechanically; `bench.minibench` will auto-record in a later generation.

## Determinism

Same trails file → byte-identical section: no timestamps, no floats in ranking
output (exact rational weights internally, stable `(weight, path)` tie-breaks),
fixed JSON key order, and re-recording identical input rewrites identical bytes.
A repo without a trails file produces a block byte-identical to a trails-less
build — the section is purely additive.
