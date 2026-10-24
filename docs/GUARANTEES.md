# Guarantees

Five safety guarantees, enforced by `tests/test_guarantees.py`. They exist to make the
layer safe to drop into any repo: it can never damage your files, leak task data, hang,
or write anywhere it shouldn't. CI fails if one regresses.

| # | Guarantee | Meaning |
|---|-----------|---------|
| G1 | **Non-destruction** | Only bytes between the markers ever change; removal restores the original byte-for-byte (a pre-existing empty file stays). Malformed markers → write refuses loudly, touches nothing. |
| G4 | **No leakage** | The issue text is the only task input. Benchmark fields (gold patches, FAIL_TO_PASS) can never reach the block or `.codegraph/`. |
| G5 | **Never crashes** | Binary files, broken syntax, huge files, unicode names, deep nesting — indexed gracefully, fallbacks everywhere. |
| G6 | **Filesystem purity** | `init`/`focus` write only `AGENTS.md` and `.codegraph/**` — verified by full-tree hashing. |
| G8 | **Enforceable time budget** | Runs execute in their own process group; the whole group dies at the timeout (no orphaned agents, no hung runners). |

Plus the design contract: **G9 — the layer is CLI-agnostic.** One plain AGENTS.md block,
identical for every CLI, no tool names, no vendor vocabulary, no MCP. Any AGENTS.md-reading
CLI inherits the layer; any other ignores it.

## Hygiene properties (tested, not contract)

Determinism (byte-stable output — keeps prompt caches warm), byte budget (hard cap,
whole-entry drops), idempotence (re-running focus rewrites nothing), vocabulary check.
These make the tool well-behaved and cheap; they may be relaxed for iteration speed.

## What is explicitly NOT guaranteed

- That agents obey the directives — they're probabilistic; we *measure* (waste
  detectors, turns/task) rather than promise.
- Lead accuracy — rankings are framed as "leads (verify, don't trust)".
- Cross-version block stability — markers carry a version tag on purpose.

Policy: a PR touching the emit path keeps G1/G4/G5/G6/G8 green. That's the whole process.
