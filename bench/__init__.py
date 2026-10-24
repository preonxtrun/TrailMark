"""bench — SWE-bench Verified campaign harness for codegraph (control vs treatment).

Standalone package: it imports NOTHING from ``src/codegraph`` so the measurement harness
stays decoupled from the optimization layer it evaluates (SPEC "bench/" rules).

Layout:
- ``cost.py``    usage/cost adapters per CLI (claude stream-json, codex exec --json, opencode)
- ``arms.py``    arm setup steps, issue-file writing, no-leakage gate, patch pathspec
- ``run.py``     resumable, parallel campaign runner (``python -m bench.run``, ``--dry``)
- ``report.py``  paired control-vs-treatment statistics + markdown table
- ``fixtures/``  handcrafted CLI-output samples so adapters are testable without docker
"""

__all__ = ["arms", "cost", "report", "run"]
