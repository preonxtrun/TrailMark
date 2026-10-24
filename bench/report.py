"""Paired control-vs-treatment statistics for bench campaigns.

Pure python + numpy — no docker, no swebench imports. Feed it ``records.jsonl``
(optionally with grading results) and get a deterministic markdown table. All reductions
iterate sorted keys and seed the bootstrap RNG, so identical inputs always render an
identical report (SPEC invariant #1).

Usage:
    python -m bench.report --out runs/demo [--bootstrap 10000] [--seed 0]

Metrics per arm: mean $/task, mean tokens/task, mean turns, cache-read share, and
resolve% when grading results are present (``graded.json`` / ``graded.<arm>.json`` —
see :func:`load_graded`). The paired section reports treatment − control deltas per
instance (seed-averaged) with a seeded percentile-bootstrap 95% CI over instances.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

CONTROL_ARM = "control"
TREATMENT_ARM = "treatment"
_TOKEN_KEYS = ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens")
_METRIC_ORDER = ("cost_usd", "tokens", "turns")  # fixed bootstrap RNG consumption order


def load_records(path: str | Path) -> list[dict]:
    """Load a records.jsonl file; blank/malformed lines are skipped with a warning."""
    records: list[dict] = []
    with Path(path).open(encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("skipping malformed record at %s:%d", path, lineno)
                continue
            if isinstance(record, dict):
                records.append(record)
    return records


def _as_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    return None


def load_graded(out_dir: str | Path) -> dict[str, dict[str, bool]]:
    """Load grading results into ``{arm: {instance_id: resolved}}``.

    Recognized files in the campaign output dir:
    - ``graded.<arm>.json`` — ``{instance_id: resolved_bool}`` for that arm,
    - ``graded.json`` — either ``{instance_id: resolved_bool}`` (a single map, stored
      under the ``"*"`` key and applied to any arm) or ``{arm: {instance_id: bool}}``.
    Unrecognized shapes are skipped with a warning.
    """
    out = Path(out_dir)
    result: dict[str, dict[str, bool]] = {}
    single = out / "graded.json"
    if single.is_file():
        try:
            data = json.loads(single.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, dict):
                    arm_map = {
                        str(iid): bool(v) for iid, v in value.items() if _as_bool(v) is not None
                    }
                    if arm_map:
                        result[str(key)] = arm_map
                elif _as_bool(value) is not None:
                    result.setdefault("*", {})[str(key)] = bool(_as_bool(value))
        else:
            logger.warning("unrecognized graded.json shape in %s", out)
    for path in sorted(out.glob("graded.*.json")):
        arm = path.name[len("graded.") : -len(".json")]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            arm_map = {str(iid): bool(v) for iid, v in data.items() if _as_bool(v) is not None}
            if arm_map:
                result[arm] = arm_map
    return result


def _usage_bucket(record: Mapping[str, object], key: str) -> int:
    usage = record.get("usage")
    if not isinstance(usage, Mapping):
        return 0
    value = usage.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _record_tokens(record: Mapping[str, object]) -> int | None:
    usage = record.get("usage")
    if not isinstance(usage, Mapping):
        return None
    try:
        return sum(int(usage.get(key) or 0) for key in _TOKEN_KEYS)
    except (TypeError, ValueError):
        return None


def _seed_mean(records: Sequence[Mapping[str, object]], key: str) -> float | None:
    """Mean of a numeric field across seeds (non-numeric/missing values excluded)."""
    values = [
        record[key]
        for record in records
        if isinstance(record.get(key), (int, float)) and not isinstance(record.get(key), bool)
    ]
    return sum(values) / len(values) if values else None


def _seed_mean_tokens(records: Sequence[Mapping[str, object]]) -> float | None:
    values = [_record_tokens(record) for record in records]
    values = [value for value in values if value is not None]
    return sum(values) / len(values) if values else None


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _round(value: float | None, digits: int = 6) -> float | None:
    return None if value is None else round(float(value), digits)


def _bootstrap_ci(
    values: Sequence[float], bootstrap: int, rng: np.random.Generator
) -> list[float] | None:
    """Seeded percentile bootstrap 95% CI over instance-level deltas (deterministic)."""
    if len(values) < 2 or bootstrap <= 0:
        return None
    arr = np.asarray(values, dtype=float)
    idx = rng.integers(0, arr.size, size=(int(bootstrap), arr.size))
    means = arr[idx].mean(axis=1)
    lo, hi = (float(x) for x in np.percentile(means, [2.5, 97.5]))
    return [round(lo, 6), round(hi, 6)]


def paired_report(
    records: Sequence[Mapping[str, object]],
    bootstrap: int = 10_000,
    seed: int = 0,
    graded: Mapping[str, Mapping[str, bool]] | None = None,
) -> dict[str, object]:
    """Compute the paired control-vs-treatment report (deterministic).

    - per arm: instance-level seed-averaged means for $/task, tokens/task, turns; the
      aggregate cache-read share; and resolve% when grading data covers the arm.
    - paired (treatment − control): per-instance seed-averaged deltas over instances
      present in BOTH arms, with a seeded percentile-bootstrap 95% CI over instances for
      cost/tokens/turns (resolve deltas are reported without a CI).
    - ``graded`` maps arm → {instance_id: resolved}; the ``"*"`` key (from a single-map
      ``graded.json``) applies to every arm.
    """
    grade_map = graded or {}
    tree: dict[str, dict[str, list[Mapping[str, object]]]] = {}
    for record in records:
        arm = record.get("arm")
        instance_id = record.get("instance_id")
        if not isinstance(arm, str) or not isinstance(instance_id, str):
            continue
        tree.setdefault(arm, {}).setdefault(instance_id, []).append(record)
    if not tree:
        raise ValueError("no usable records (each record needs 'arm' and 'instance_id')")
    arms = sorted(tree)
    control = CONTROL_ARM if CONTROL_ARM in arms else arms[0]
    treatment = (
        TREATMENT_ARM
        if TREATMENT_ARM in arms and TREATMENT_ARM != control
        else (arms[1] if len(arms) > 1 else None)
    )

    per_arm: dict[str, dict[str, object]] = {}
    for arm in arms:
        instances = tree[arm]
        costs = [c for c in (_seed_mean(rs, "cost_usd") for rs in instances.values()) if c is not None]
        tokens = [t for t in (_seed_mean_tokens(rs) for rs in instances.values()) if t is not None]
        turns = [t for t in (_seed_mean(rs, "turns") for rs in instances.values()) if t is not None]
        cache_read = sum(_usage_bucket(r, "cache_read_tokens") for rs in instances.values() for r in rs)
        total = sum(
            _usage_bucket(r, key) for rs in instances.values() for r in rs for key in _TOKEN_KEYS
        )
        grades = grade_map.get(arm) or grade_map.get("*") or {}
        graded_ids = sorted(set(instances) & set(grades))
        n_resolved = sum(1 for iid in graded_ids if grades[iid])
        per_arm[arm] = {
            "n_instances": len(instances),
            "n_records": sum(len(rs) for rs in instances.values()),
            "mean_cost_usd": _round(_mean(costs)),
            "mean_tokens": _round(_mean(tokens)),
            "mean_turns": _round(_mean(turns)),
            "cache_read_share": _round(cache_read / total) if total else None,
            "resolve_rate": _round(n_resolved / len(graded_ids)) if graded_ids else None,
            "n_graded": len(graded_ids),
            "n_resolved": n_resolved,
        }

    paired: dict[str, object] = {
        "instances": 0,
        "delta_cost_usd": None,
        "ci95_cost_usd": None,
        "delta_tokens": None,
        "ci95_tokens": None,
        "delta_turns": None,
        "ci95_turns": None,
        "delta_resolve_rate": None,
    }
    if treatment is not None:
        common = sorted(set(tree[control]) & set(tree[treatment]))
        deltas: dict[str, list[float]] = {"cost_usd": [], "tokens": [], "turns": []}
        resolve_deltas: list[float] = []
        grades_c = grade_map.get(control) or grade_map.get("*") or {}
        grades_t = grade_map.get(treatment) or grade_map.get("*") or {}
        for instance_id in common:
            rs_c, rs_t = tree[control][instance_id], tree[treatment][instance_id]
            for metric, getter in (
                ("cost_usd", lambda rs: _seed_mean(rs, "cost_usd")),
                ("tokens", _seed_mean_tokens),
                ("turns", lambda rs: _seed_mean(rs, "turns")),
            ):
                value_c, value_t = getter(rs_c), getter(rs_t)
                if value_c is not None and value_t is not None:
                    deltas[metric].append(value_t - value_c)
            if instance_id in grades_c and instance_id in grades_t:
                resolve_deltas.append(float(bool(grades_t[instance_id])) - float(bool(grades_c[instance_id])))
        rng = np.random.default_rng(seed)  # one RNG, consumed in fixed metric order
        paired["instances"] = len(common)
        paired["delta_cost_usd"] = _round(_mean(deltas["cost_usd"]))
        paired["ci95_cost_usd"] = _bootstrap_ci(deltas["cost_usd"], bootstrap, rng)
        paired["delta_tokens"] = _round(_mean(deltas["tokens"]))
        paired["ci95_tokens"] = _bootstrap_ci(deltas["tokens"], bootstrap, rng)
        paired["delta_turns"] = _round(_mean(deltas["turns"]))
        paired["ci95_turns"] = _bootstrap_ci(deltas["turns"], bootstrap, rng)
        paired["delta_resolve_rate"] = _round(_mean(resolve_deltas))

    return {
        "n_records": len(records),
        "control_arm": control,
        "treatment_arm": treatment,
        "per_arm": per_arm,
        "paired": paired,
        "bootstrap": int(bootstrap),
        "seed": int(seed),
    }


# --------------------------------------------------------------------------
# markdown rendering
# --------------------------------------------------------------------------


def _money(value: object) -> str:
    return "—" if value is None else f"${float(value):.4f}"


def _money_delta(value: object) -> str:
    return "—" if value is None else f"{float(value):+.4f}"


def _money_ci(value: object) -> str:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return "—"
    return f"[{float(value[0]):+.4f}, {float(value[1]):+.4f}]"


def _num(value: object) -> str:
    return "—" if value is None else f"{float(value):.1f}"


def _num_delta(value: object) -> str:
    return "—" if value is None else f"{float(value):+.1f}"


def _num_ci(value: object) -> str:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return "—"
    return f"[{float(value[0]):+.1f}, {float(value[1]):+.1f}]"


def _pct(value: object) -> str:
    return "—" if value is None else f"{float(value) * 100:.1f}%"


def _pct_delta(value: object) -> str:
    return "—" if value is None else f"{float(value) * 100:+.1f}%"


def render_markdown(report: Mapping[str, object]) -> str:
    """Render a :func:`paired_report` dict as a deterministic markdown table."""
    per_arm = report.get("per_arm") if isinstance(report.get("per_arm"), Mapping) else {}
    paired = report.get("paired") if isinstance(report.get("paired"), Mapping) else {}
    control = str(report.get("control_arm", CONTROL_ARM))
    treatment = report.get("treatment_arm")
    ctrl = per_arm.get(control, {}) if isinstance(per_arm.get(control), Mapping) else {}
    trt = (
        per_arm.get(treatment, {})
        if treatment and isinstance(per_arm.get(treatment), Mapping)
        else {}
    )
    rows = [
        (
            "$/task",
            _money(ctrl.get("mean_cost_usd")),
            _money(trt.get("mean_cost_usd")),
            _money_delta(paired.get("delta_cost_usd")),
            _money_ci(paired.get("ci95_cost_usd")),
        ),
        (
            "tokens/task",
            _num(ctrl.get("mean_tokens")),
            _num(trt.get("mean_tokens")),
            _num_delta(paired.get("delta_tokens")),
            _num_ci(paired.get("ci95_tokens")),
        ),
        (
            "turns/task",
            _num(ctrl.get("mean_turns")),
            _num(trt.get("mean_turns")),
            _num_delta(paired.get("delta_turns")),
            _num_ci(paired.get("ci95_turns")),
        ),
        (
            "cache-read share",
            _pct(ctrl.get("cache_read_share")),
            _pct(trt.get("cache_read_share")),
            "—",
            "—",
        ),
        (
            "resolve %",
            _pct(ctrl.get("resolve_rate")),
            _pct(trt.get("resolve_rate")),
            _pct_delta(paired.get("delta_resolve_rate")),
            "—",
        ),
    ]
    lines = [
        "# trailmark bench — paired report",
        "",
        (
            f"bootstrap={report.get('bootstrap')} seed={report.get('seed')} "
            f"paired_instances={paired.get('instances', 0)} records={report.get('n_records', 0)}"
        ),
        "",
        "| metric | control | treatment | Δ (treatment − control) | 95% CI |",
        "|---|---:|---:|---:|---:|",
    ]
    lines += [f"| {name} | {cv} | {tv} | {dv} | {ci} |" for name, cv, tv, dv, ci in rows]
    lines += [
        "",
        (
            f"arms: control={control} treatment={treatment or '—'} "
            "(cache-read share and resolve% are aggregate ratios; only cost/tokens/turns "
            "deltas carry a bootstrap CI)"
        ),
    ]
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bench.report", description="Paired control-vs-treatment stats + markdown table."
    )
    parser.add_argument("--out", required=True, help="campaign output dir (reads records.jsonl inside)")
    parser.add_argument("--bootstrap", type=int, default=10_000, help="bootstrap resamples")
    parser.add_argument("--seed", type=int, default=0, help="bootstrap RNG seed (deterministic)")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        force=True,
    )
    out_dir = Path(args.out)
    records = load_records(out_dir / "records.jsonl")
    if not records:
        logger.error("no records found in %s", out_dir / "records.jsonl")
        return 2
    report = paired_report(records, bootstrap=args.bootstrap, seed=args.seed, graded=load_graded(out_dir))
    print(render_markdown(report))  # markdown output is the product here
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
