# Debugging the layer

Five diagnostic commands answer "why did it do that". All support `--root` and `--json`.

```bash
codegraph why "<issue>"    # the ranking debugger: per-file breakdown of the four
                           # scoring components (lexical / graph / test / prior),
                           # final scores, chosen leads — exactly what focus injects
codegraph search "<query>" # what BM25 retrieval sees, best-first (is the vocabulary
                           # in your repo matching the issue's words?)
codegraph refs <path>      # one file's symbols, resolved + unresolved refs, incoming
                           # refs, PageRank score and rank — the ref-resolution debugger
codegraph block            # print the managed block verbatim, as the agent sees it
codegraph selftest         # 10-check install sanity in a throwaway sandbox
                           # (pipeline, determinism, budget, markers, idempotence)
```

Trails memory:

```bash
python -m codegraph.trails record --id task-7 --files a.py,b.py --outcome solved
python -m codegraph.trails record --id task-8 --files dead.py --outcome failed
python -m codegraph.trails clear --root .
```

## Field notes (real bugs these tools caught)

- **Nondeterministic refs** (Python differed run-to-run; Rust differed from Python):
  `why`/`refs` exposed it; root cause was tree-sitter's `captures()` dict order — fixed by
  sorting captures by byte span. Both languages now report identical ref sets.
- **Section vanished from the block**: sections drop in priority order when the byte
  budget is tight (GLOSSARY → LAB → TRAILS → PLAN extras). `block` + a larger `--budget`
  shows what was dropped. This is G3 working, not a bug.
- **"run codegraph init first" (exit 2)**: no usable cache — don't mix cores on one
  `.codegraph/` (Rust- and Python-built caches share the schema but re-init when switching).
- **Missing cache mid-workflow**: an agent running `git clean` sweeps untracked
  `.codegraph/` — the benchmark registers the footprint in `.git/info/exclude`; for real
  workflows use the git post-commit hook from the README (or the same exclude entry).
- **Lexical scores all ~0.5 on tiny repos**: fewer candidates than the normalization floor
  — `why` makes this visible; add files or lower expectations.
