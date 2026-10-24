# Profiles — lab / cluster / greenfield mode

Some workloads have no codebase worth indexing: greenfield projects, experiment loops,
Slurm/GPU clusters. For those, the layer's payload is **static facts and rules you write
once** — the mechanism the cost-inefficiency research measured at up to −41.7% token cost
("developer-designed skills": high-level, trace-agnostic guidance prepared ahead of time).

## Format

`.trailmark/profile.txt` inside the repo — zero dependencies, one entry per line:

```
fact: partitions: gpu (A100-80G, max 24h), cpu (max 72h)
rule: check sacct once per job id; never poll squeue in a loop
```

- `fact:` lines → a **LAB** section in the injected block (dropped after RULES/MAP/TESTS
  when the budget tightens, before PLAN).
- `rule:` lines → appended to the flight rules (they are user-authored, so they count as
  developer-designed skills, not agent-synthesized ones).
- Lines starting with `#` are comments. Caps: 8 facts, 6 rules, byte budget still applies.

## Why this shape

- **Greenfield**: there is nothing to index on day 0 — the profile carries the structure
  contract (layout, naming, stack, entry points) *before* the first line of code exists.
  Once code accretes, `trailmark init` builds a real map and both mechanisms coexist.
- **Slurm/GPU clusters**: cluster facts are tribal knowledge agents re-derive every run
  (partitions, gres, modules, scratch paths, accounting). The profile states them once;
  the rules encode job discipline (sacct once, reuse job templates, ledger experiments,
  read log tails before resubmitting).
- **Experiment loops**: keep an `EXPERIMENTS.md` ledger; the profile's rules make the
  agent append configs before launching and results after finishing — persistent memory
  across runs, file-based, CLI-agnostic.

## Example

See `bench/profiles/lab-slurm.example.profile.txt` — a complete Slurm GPU-cluster profile.
Copy it to `.trailmark/profile.txt`, edit to your cluster, then use the layer exactly as
always: `trailmark focus "<task>"` before launching any CLI.

## Standard benchmark for lab mode (backlog)

MLE-bench (OpenAI, ICLR 2025 oral; 75 Kaggle competitions, end-to-end ML engineering) is
the eventual standard check for this mode — its lite development split (7 competitions)
is the right entry size. Heavy setup (Kaggle data, docker); parked until the mini-bench
loop has converged.
