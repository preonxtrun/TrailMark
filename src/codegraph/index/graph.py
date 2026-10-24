"""File-level PageRank over the reference graph (numpy power iteration, deterministic).

Edges are file→file with weight = number of references. The iteration count is fixed
(no early convergence exit) and all input arrays are built in sorted order, so the
result is byte-identical across runs for the same inputs (hard invariant #1).
"""

from __future__ import annotations

import numpy as np

__all__ = ["pagerank"]


def pagerank(
    files: object,
    refs: object,
    damping: float = 0.85,
    iters: int = 30,
    personalization: dict[str, float] | None = None,
) -> dict[str, float]:
    """Compute file-level PageRank scores summing to ~1.0.

    Args:
        files: iterable of file paths (a mapping is accepted and its keys are used).
        refs: iterable of objects with ``src_file`` / ``dst_file`` (usually ``Ref``).
        damping: damping factor (teleport probability is ``1 - damping``).
        iters: fixed power-iteration count; more iterations → closer to the fixed point.
        personalization: optional ``{file: weight}`` seed vector (used by focus);
            missing keys get weight 0. An all-zero/empty mapping falls back to uniform.

    Returns:
        ``{file: score}`` with scores summing to ~1.0. Dangling nodes (no out-edges)
        and unreachable mass are redistributed uniformly every iteration.
    """
    base = list(files.keys()) if hasattr(files, "keys") else list(files)
    edge_weight: dict[tuple[str, str], int] = {}
    extra: set[str] = set()
    for ref in refs:
        src = ref.src_file
        dst = ref.dst_file
        if not src or not dst:
            continue  # unresolved refs carry no file edge
        edge_weight[(src, dst)] = edge_weight.get((src, dst), 0) + 1
        extra.add(src)
        extra.add(dst)
    nodes = sorted(set(base) | extra)
    n = len(nodes)
    if n == 0:
        return {}
    index = {path: i for i, path in enumerate(nodes)}

    if personalization:
        pvec = np.array(
            [max(0.0, float(personalization.get(p, 0.0))) for p in nodes], dtype=np.float64
        )
        total = pvec.sum()
        pvec = pvec / total if total > 0 else np.full(n, 1.0 / n)
    else:
        pvec = np.full(n, 1.0 / n)

    if not edge_weight:
        return {p: float(s) for p, s in zip(nodes, pvec)}

    # Sorted edge arrays → deterministic float summation order in bincount.
    items = sorted((index[s], index[d], float(w)) for (s, d), w in edge_weight.items())
    src_idx = np.array([i for i, _, _ in items], dtype=np.int64)
    dst_idx = np.array([j for _, j, _ in items], dtype=np.int64)
    weights = np.array([w for _, _, w in items], dtype=np.float64)
    out_weight = np.bincount(src_idx, weights=weights, minlength=n)
    flow_weight = weights / out_weight[src_idx]  # row-normalized transition weights
    dangling = out_weight == 0

    rank = pvec.copy()
    for _ in range(max(0, iters)):
        flow = np.bincount(dst_idx, weights=rank[src_idx] * flow_weight, minlength=n)
        dangling_mass = rank[dangling].sum()
        rank = damping * flow + damping * (dangling_mass / n) + (1.0 - damping) * pvec

    total = rank.sum()
    if total > 0:
        rank = rank / total
    return {p: float(s) for p, s in zip(nodes, rank)}
