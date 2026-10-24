"""Generate a deterministic, repo-stratified subset of SWE-bench Verified.

Usage:
    uv run python -m bench.make_subset --total 20 --out bench/subsets/verified-20.jsonl

Strategy: group instances by repository (instance_id prefix before "__"), sort
groups by size, allocate the budget across repos by largest-remainder (min 1 per
repo when possible), and within each repo pick evenly spaced instances from the
sorted id list. No RNG → the same command always yields the same subset, so
generations are directly comparable.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

DEFAULT_DATASET = "princeton-nlp/SWE-bench_Verified"


def repo_of(instance_id: str) -> str:
    return instance_id.split("__", 1)[0]


def allocate(total: int, sizes: dict[str, int]) -> dict[str, int]:
    """Largest-remainder allocation of *total* picks across repos, min 1 each."""
    n_items = sum(sizes.values())
    raw = {repo: total * size / n_items for repo, size in sizes.items()}
    # floor-then-distribute remainder by largest fractional part (deterministic)
    base = {repo: max(1, int(v)) for repo, v in raw.items()} if total >= len(sizes) else {}
    if not base:
        # fewer slots than repos: take repos by size desc
        ordered = sorted(sizes, key=lambda r: (-sizes[r], r))
        return {repo: 1 for repo in ordered[:total]}
    while sum(base.values()) > total:
        victim = max(base, key=lambda r: (raw[r] - base[r] >= 0, base[r], r))
        if base[victim] <= 1:
            break
        base[victim] -= 1
    remaining = total - sum(base.values())
    fracs = sorted(raw, key=lambda r: (-(raw[r] - int(raw[r])), r))
    i = 0
    while remaining > 0 and fracs:
        repo = fracs[i % len(fracs)]
        if base[repo] < sizes[repo]:
            base[repo] += 1
            remaining -= 1
        i += 1
        if i > 4 * total:  # safety
            break
    return base


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--total", type=int, default=20)
    parser.add_argument("--out", default="bench/subsets/verified-20.jsonl")
    args = parser.parse_args()

    from datasets import load_dataset

    ds = load_dataset(args.dataset, split="test")
    by_repo: dict[str, list[dict]] = defaultdict(list)
    for record in ds:
        by_repo[repo_of(record["instance_id"])].append(record)

    sizes = {repo: len(items) for repo, items in by_repo.items()}
    plan = allocate(args.total, sizes)

    chosen: list[dict] = []
    for repo in sorted(plan):
        items = sorted(by_repo[repo], key=lambda r: r["instance_id"])
        k = plan[repo]
        n = len(items)
        for i in range(k):
            chosen.append(items[i * n // k])

    chosen.sort(key=lambda r: r["instance_id"])
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for record in chosen:
            fh.write(
                json.dumps(
                    {
                        "instance_id": record["instance_id"],
                        "problem_statement": record["problem_statement"],
                    }
                )
                + "\n"
            )

    print(f"wrote {len(chosen)} instances -> {out}")
    dist: dict[str, int] = defaultdict(int)
    for record in chosen:
        dist[repo_of(record["instance_id"])] += 1
    for repo in sorted(dist, key=lambda r: (-dist[r], r)):
        print(f"  {repo:<24} {dist[repo]:>3}  (pool {sizes[repo]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
