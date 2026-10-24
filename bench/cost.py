"""Usage and cost extraction for headless coding-CLI outputs (bench harness).

bench/ is a standalone package and imports nothing from ``src/trailmark``: the measurement
harness must stay decoupled from the optimization layer it evaluates.

Parsers are defensive by design — unexpected CLI output must never raise. Malformed lines
are ignored; when non-empty output yields no usage at all, token counts fall back to a
characters/4 estimate (``Usage.estimated=True``) priced with ``DEFAULT_PRICE``. Each
parser documents the exact field paths it reads and its reduction rules, and a small
handcrafted sample for every CLI lives in ``bench/fixtures/`` so adapters are unit-tested
without docker.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_PRICE",
    "PRICES",
    "Usage",
    "cost_usd",
    "parse",
    "parse_claude",
    "parse_codex",
    "parse_opencode",
    "price_for",
]

#: (input, cache-read, cache-write, output) USD per 1M tokens, keyed by model-id substring.
#: List prices as of early 2026 — spot-check before a real campaign, or override per call
#: with ``cost_usd(usage, prices=...)``. The longest matching key wins (alphabetical
#: tie-break, deterministic).
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-opus": (15.0, 1.50, 18.75, 75.0),
    "claude-sonnet-4": (3.0, 0.30, 3.75, 15.0),
    "claude-haiku": (0.80, 0.08, 1.00, 4.0),
    "gpt-5": (1.25, 0.125, 0.0, 10.0),
    "gpt-4.1": (2.0, 0.50, 0.0, 8.0),
    "gpt-4o": (2.50, 1.25, 0.0, 10.0),
    "o3": (2.0, 0.50, 0.0, 8.0),
    "o4-mini": (1.10, 0.275, 0.0, 4.40),
    "qwen": (0.50, 0.10, 0.0, 1.60),
    "kimi": (0.60, 0.15, 0.0, 2.50),
    "glm": (0.60, 0.11, 0.0, 2.20),
    "deepseek": (0.27, 0.07, 0.0, 1.10),
}

#: Fallback price for models matching no PRICES key (in, cache-read, cache-write, out).
DEFAULT_PRICE: tuple[float, float, float, float] = (3.0, 0.30, 3.75, 15.0)

#: (input, cache_read, cache_write, output) token quadruple.
Tokens = tuple[int, int, int, int]


@dataclass
class Usage:
    """Token/cost accounting for ONE headless agent run.

    ``turns`` counts assistant events/messages; ``reported_cost_usd`` is whatever cost
    figure the CLI itself printed (None when absent) and is informational only — billing
    math always uses :func:`cost_usd` (token math). ``estimated`` marks char/4 fallbacks
    produced when a CLI printed no parseable usage.
    """

    model: str | None = None
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    turns: int = 0
    reported_cost_usd: float | None = None
    estimated: bool = False

    @property
    def total_tokens(self) -> int:
        """All billable token buckets summed."""
        return (
            self.input_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
            + self.output_tokens
        )

    def as_dict(self) -> dict[str, object]:
        """JSON-safe dict (records.jsonl schema)."""
        return asdict(self)


def _int(value: object) -> int | None:
    """Best-effort int coercion; bools and non-numeric types are rejected."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _pick(source: Mapping[str, object], *keys: str) -> int:
    """First integer value among ``keys`` (0 when none present)."""
    for key in keys:
        value = _int(source.get(key))
        if value is not None:
            return value
    return 0


def _add(a: Tokens, b: Tokens) -> Tokens:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2], a[3] + b[3])


def _iter_json(lines: Sequence[str]):
    """Yield parsed JSON objects; blank/malformed lines are skipped (debug-logged)."""
    for line in lines:
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            logger.debug("ignoring non-JSON line: %.120s", line)
            continue
        if isinstance(obj, dict):
            yield obj


def _unparsed_chars(lines: Sequence[str]) -> int:
    """Characters in lines that did not parse as JSON (input for char/4 estimation)."""
    total = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            json.loads(line)
        except json.JSONDecodeError:
            total += len(line)
    return total


def _estimated_usage(chars: int) -> Usage:
    """Char/4 fallback with a 75/25 input/output split (documented heuristic)."""
    tokens = max(0, chars // 4)
    inp = int(tokens * 0.75)
    return Usage(model=None, input_tokens=inp, output_tokens=tokens - inp, estimated=True)


def _apply_estimate(usage: Usage, chars: int) -> None:
    """Fill ``usage`` with the char/4 estimate when nothing parseable was found."""
    if chars <= 0:
        return
    fallback = _estimated_usage(chars)
    usage.input_tokens = fallback.input_tokens
    usage.output_tokens = fallback.output_tokens
    usage.estimated = True


# --------------------------------------------------------------------------
# claude (claude-code `claude -p --output-format stream-json`)
# --------------------------------------------------------------------------


def _claude_usage(obj: Mapping[str, object]) -> Tokens | None:
    """Read usage from ``message.usage`` or top-level ``usage`` (claude stream-json)."""
    message = obj.get("message")
    sources = [
        message.get("usage") if isinstance(message, Mapping) else None,
        obj.get("usage"),
    ]
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        inp = _int(source.get("input_tokens"))
        if inp is None:
            continue
        return (
            inp,
            _pick(source, "cache_read_input_tokens", "cached_input_tokens"),
            _pick(source, "cache_creation_input_tokens", "cache_write_input_tokens"),
            _pick(source, "output_tokens", "completion_tokens"),
        )
    return None


def _claude_model(obj: Mapping[str, object]) -> str | None:
    """Model id from ``message.model`` or top-level ``model``."""
    message = obj.get("message")
    for source in (message, obj):
        if isinstance(source, Mapping):
            model = source.get("model")
            if isinstance(model, str) and model:
                return model
    return None


def parse_claude(lines: Sequence[str]) -> Usage:
    """Parse ``claude -p --output-format stream-json`` JSONL (claude-code >= 1.0).

    Field-path assumptions:
    - assistant events: ``{"type":"assistant","message":{"model":...,"usage":{"input_tokens",
      "cache_creation_input_tokens","cache_read_input_tokens","output_tokens"}}}``.
    - a final ``{"type":"result",...,"usage":{...},"total_cost_usd":<float>}`` whose usage
      is the CUMULATIVE session total. Assistant events are SUMMED; the result event is
      used only for ``total_cost_usd`` and as a fallback when no assistant event carried
      usage (prevents double counting).
    - model id at ``message.model`` or top-level ``model`` (last non-empty wins).
    - ``turns`` = number of ``type=="assistant"`` events.

    Non-JSON output degrades to a char/4 estimate (``Usage.estimated``); never raises on
    unexpected lines.
    """
    usage = Usage()
    assistant: Tokens | None = None
    result: Tokens | None = None
    for obj in _iter_json(lines):
        model = _claude_model(obj)
        if model:
            usage.model = model
        kind = obj.get("type")
        found = _claude_usage(obj)
        if kind == "assistant":
            usage.turns += 1
            if found is not None:
                assistant = found if assistant is None else _add(assistant, found)
        elif kind == "result":
            if found is not None:
                result = found
            cost_value = obj.get("total_cost_usd")
            if isinstance(cost_value, (int, float)) and not isinstance(cost_value, bool):
                usage.reported_cost_usd = float(cost_value)
    total = assistant or result
    if total is not None:
        (
            usage.input_tokens,
            usage.cache_read_tokens,
            usage.cache_write_tokens,
            usage.output_tokens,
        ) = total
    else:
        _apply_estimate(usage, _unparsed_chars(lines))
    return usage


# --------------------------------------------------------------------------
# codex (`codex exec --json`) and opencode (`opencode run`) — defensive scanning
# --------------------------------------------------------------------------


def _usage_shape(obj: Mapping[str, object]) -> Tokens | None:
    """Recognize usage-shaped dicts across the CLIs.

    - shape A/B (claude, codex, openai): ``input_tokens`` plus optional
      ``cache_read_input_tokens``/``cached_input_tokens``,
      ``cache_creation_input_tokens``/``cache_write_input_tokens``,
      ``output_tokens``/``completion_tokens``. Codex-style ``reasoning_output_tokens``
      are billed as output, so they are added into the output bucket when present.
    - shape C (opencode): ``input`` + ``output`` (ints) with optional
      ``cache.read``/``cache.write``.
    """
    inp = _int(obj.get("input_tokens"))
    if inp is not None:
        return (
            inp,
            _pick(obj, "cache_read_input_tokens", "cached_input_tokens"),
            _pick(obj, "cache_creation_input_tokens", "cache_write_input_tokens"),
            _pick(obj, "output_tokens", "completion_tokens")
            + _pick(obj, "reasoning_output_tokens"),
        )
    if "input" in obj and "output" in obj:
        inp2 = _int(obj.get("input"))
        out2 = _int(obj.get("output"))
        if inp2 is None or out2 is None:
            return None
        cache = obj.get("cache")
        cache = cache if isinstance(cache, Mapping) else {}
        return (
            inp2,
            _int(cache.get("read")) or 0,
            _int(cache.get("write")) or 0,
            out2,
        )
    return None


def _scan_usages(
    obj: Mapping[str, object], path: tuple[str, ...] = ()
) -> list[tuple[tuple[str, ...], Tokens]]:
    """Depth-first scan for usage-shaped dicts; returns (ancestor-key-path, tokens)."""
    found: list[tuple[tuple[str, ...], Tokens]] = []
    shaped = _usage_shape(obj)
    if shaped is not None:
        found.append((path, shaped))
    for key, value in obj.items():
        child_path = (*path, str(key).lower())
        if isinstance(value, Mapping):
            found.extend(_scan_usages(value, child_path))
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, Mapping):
                    found.extend(_scan_usages(item, child_path))
    return found


def _reduce_usages(found: Sequence[tuple[tuple[str, ...], Tokens]]) -> Tokens:
    """Fold scanned usages into one session total.

    - dicts under an ancestor key containing ``last_token_usage`` are SKIPPED (they are
      per-turn deltas that would double count cumulative totals),
    - dicts under an ancestor key containing ``total`` (e.g. codex ``total_token_usage``)
      are CUMULATIVE snapshots → per-bucket running max,
    - everything else is per-event usage → summed,
    - if any cumulative snapshot exists it represents the session total and per-event sums
      are discarded (avoids double counting when codex mixes event styles).

    A usage object's own ``total_tokens`` FIELD does not make it cumulative; only an
    ancestor KEY like ``total_token_usage`` does.
    """
    events: Tokens = (0, 0, 0, 0)
    totals: Tokens | None = None
    for path, tokens in found:
        joined = ".".join(path)
        if "last_token_usage" in joined:
            continue
        if "total" in joined:
            totals = tokens if totals is None else (
                max(totals[0], tokens[0]),
                max(totals[1], tokens[1]),
                max(totals[2], tokens[2]),
                max(totals[3], tokens[3]),
            )
        else:
            events = _add(events, tokens)
    return totals if totals is not None else events


def _collect_dicts(node: object, out: list[Mapping[str, object]]) -> None:
    """Pre-order collection of every dict (deterministic insertion order)."""
    if isinstance(node, Mapping):
        out.append(node)
        for value in node.values():
            _collect_dicts(value, out)
    elif isinstance(node, list):
        for item in node:
            _collect_dicts(item, out)


def _assistant_turns(objs: Sequence[Mapping[str, object]]) -> int:
    """Count assistant messages: ``role=="assistant"`` dicts or codex agent_message items."""
    dicts: list[Mapping[str, object]] = []
    for obj in objs:
        _collect_dicts(obj, dicts)
    return sum(
        1 for d in dicts if d.get("role") == "assistant" or d.get("type") == "agent_message"
    )


def _reported_cost(objs: Sequence[Mapping[str, object]]) -> float | None:
    """Sum numeric ``cost`` fields (opencode reports one per assistant message)."""
    dicts: list[Mapping[str, object]] = []
    for obj in objs:
        _collect_dicts(obj, dicts)
    total = 0.0
    seen = False
    for d in dicts:
        value = d.get("cost")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            total += float(value)
            seen = True
    return total if seen else None


def _generic_model(objs: Sequence[Mapping[str, object]]) -> str | None:
    """Last non-empty model id found under ``model``/``modelID``/``model_id`` keys."""
    dicts: list[Mapping[str, object]] = []
    for obj in objs:
        _collect_dicts(obj, dicts)
    model: str | None = None
    for d in dicts:
        for key in ("model", "modelID", "model_id"):
            value = d.get(key)
            if isinstance(value, str) and value:
                model = value
    return model


def _parse_generic(lines: Sequence[str]) -> Usage:
    """Shared defensive scanner used by the codex/opencode/unknown-CLI parsers."""
    objs = list(_iter_json(lines))
    found_per_obj = [_scan_usages(obj) for obj in objs]
    flat = [entry for found in found_per_obj for entry in found]
    total = _reduce_usages(flat)
    usage = Usage()
    (
        usage.input_tokens,
        usage.cache_read_tokens,
        usage.cache_write_tokens,
        usage.output_tokens,
    ) = total
    usage.turns = _assistant_turns(objs) or sum(1 for found in found_per_obj if found)
    usage.model = _generic_model(objs)
    usage.reported_cost_usd = _reported_cost(objs)
    if not flat:
        _apply_estimate(usage, _unparsed_chars(lines))
    return usage


def parse_codex(lines: Sequence[str]) -> Usage:
    """Parse ``codex exec --json`` JSONL events (codex-cli JSON output mode), defensively.

    Two real-world event styles are handled:

    - event style: ``{"type":"turn.completed","usage":{"input_tokens","cached_input_tokens",
      "output_tokens","reasoning_output_tokens","total_tokens",...}}`` — one per turn, each
      usage is PER-TURN, so events are SUMMED.
    - message style (older experimental JSON): ``{"msg":{"type":"token_count","info":{
      "total_token_usage":{...},"last_token_usage":{...}}}}`` — ``total_token_usage`` is
      CUMULATIVE for the session → per-bucket running max; ``last_token_usage`` is a
      per-turn delta and is SKIPPED (it would double count).
    - if both styles appear in one stream, cumulative totals win and per-event sums are
      discarded.
    - a usage object's own ``total_tokens`` FIELD does not make it cumulative; only an
      ancestor KEY like ``total_token_usage`` does.
    - reasoning tokens are assumed billed as output (codex reports them separately, so
      ``reasoning_output_tokens`` is added into the output bucket).
    - ``turns`` = agent_message items (fallback: number of usage-bearing events).
    - model id from ``model``/``modelID``/``model_id`` fields when the version prints them.

    Non-JSON output degrades to a char/4 estimate (``Usage.estimated``); never raises.
    """
    return _parse_generic(lines)


def parse_opencode(lines: Sequence[str], model: str | None = None) -> Usage:
    """Parse ``opencode run --format json`` JSONL — verified against opencode v2.1.4.

    Verified event shape (one JSON object per line)::

        {"type":"step_start"|"text"|"tool"|"step_finish", "timestamp":...,
         "sessionID":"ses_..", "part":{...}}

    Each assistant step emits one ``step_finish`` whose ``part`` carries PER-STEP
    usage: ``{"type":"step-finish","reason":"tool-calls"|"stop","cost":<float>,
    "tokens":{"input":..,"output":..,"reasoning":..,"cache":{"read":..,"write":..}}}``.
    Usages are SUMMED across steps (input includes re-read context per request).
    ``reasoning`` tokens are billed as output (provider convention, mirrors the
    codex adapter). ``turns`` counts ``step_finish`` events. The stream carries no
    model id → *model* is injected by the caller (``run.py --model``).
    ``part.cost`` (0 for models without public pricing) is summed into
    ``reported_cost_usd``. Falls back to the generic defensive scanner (older
    storage shapes / plain text) when no ``step_finish`` events are found.
    Never raises on unexpected lines.
    """
    objs = list(_iter_json(lines))
    input_tokens = cache_read = cache_write = output_tokens = turns = 0
    reported = 0.0
    found = False
    for obj in objs:
        if not isinstance(obj, dict) or obj.get("type") != "step_finish":
            continue
        part = obj.get("part")
        tokens = part.get("tokens") if isinstance(part, dict) else None
        if not isinstance(tokens, dict) or not tokens:
            continue
        found = True
        input_tokens += int(tokens.get("input") or 0)
        output_tokens += int(tokens.get("output") or 0) + int(tokens.get("reasoning") or 0)
        cache = tokens.get("cache")
        cache = cache if isinstance(cache, dict) else {}
        cache_read += int(cache.get("read") or 0)
        cache_write += int(cache.get("write") or 0)
        turns += 1
        cost = part.get("cost") if isinstance(part, dict) else None
        if isinstance(cost, (int, float)):
            reported += float(cost)
    if not found:
        usage = _parse_generic(lines)
        if model and not usage.model:
            usage.model = model
        return usage
    return Usage(
        model=model,
        input_tokens=input_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        output_tokens=output_tokens,
        turns=turns,
        reported_cost_usd=reported if reported else None,
    )


def parse(cli: str, lines: Sequence[str], model: str | None = None) -> Usage:
    """Dispatch to the adapter for ``cli``; unknown CLIs get the defensive generic scanner."""
    if cli == "claude":
        return parse_claude(lines)
    if cli == "codex":
        return parse_codex(lines)
    if cli == "opencode":
        return parse_opencode(lines, model=model)
    logger.debug("no adapter for cli %r — using the generic defensive scanner", cli)
    return _parse_generic(lines)


def price_for(
    model: str | None,
    prices: Mapping[str, tuple[float, float, float, float]] | None = None,
) -> tuple[float, float, float, float]:
    """Price quadruple for a model id: longest substring key wins, else DEFAULT_PRICE."""
    table = PRICES if prices is None else prices
    if not model:
        return DEFAULT_PRICE
    model_id = model.lower()
    matches = sorted(
        (key for key in table if key.lower() in model_id),
        key=lambda key: (-len(key), key),
    )
    return table[matches[0]] if matches else DEFAULT_PRICE


def cost_usd(
    usage: Usage,
    prices: Mapping[str, tuple[float, float, float, float]] | None = None,
) -> float:
    """Token-math cost in USD: in*p_in + cr*p_cr + cw*p_cw + out*p_out (per-1M prices)."""
    p_in, p_cache_read, p_cache_write, p_out = price_for(usage.model, prices)
    return (
        usage.input_tokens * p_in
        + usage.cache_read_tokens * p_cache_read
        + usage.cache_write_tokens * p_cache_write
        + usage.output_tokens * p_out
    ) / 1_000_000
