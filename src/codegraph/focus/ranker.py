"""Lead ranking: (IndexData, issue) -> top-k file leads.

Pure, deterministic, zero-LLM, zero-network scoring. Per-file hybrid score:

- ``0.45 * lexical``      FTS5 BM25 over symbols (``cache.search``); best (lowest)
  bm25 hit per file, inverted (bm25 is lower=better and returned negative by FTS5,
  so ``raw = max(0.0, -score)``) and min-max normalized across candidate files
  (constant 0.5 when the candidate set is degenerate).
- ``0.25 * graph``        personalized PageRank seeded from the BM25-top-20 files,
  seed weight proportional to inverted BM25 rank (``1/(rank+1)``, normalized to sum
  1), min-max normalized over candidates.
- ``0.15 * test-prox``    issue tokens matching test-file symbol names / paths
  (files whose path contains ``test``), propagated one hop to the src files they
  import via ``data.refs``, min-max normalized over candidates.
- ``0.15 * global prior`` plain PageRank over all files, min-max normalized over
  candidates.

Deterministic tie-break: ``(-score, path, best-symbol name)``. If
``index.graph.pagerank`` is unavailable, a private fallback with identical
semantics is used so ranking always works standalone.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from ..cache import Hit, IndexCache
from ..model import IndexData, Ref, Symbol

LOGGER = logging.getLogger(__name__)

# Hybrid component weights (sum to 1.0).
W_LEXICAL = 0.45
W_GRAPH = 0.25
W_TEST = 0.15
W_PRIOR = 0.15

_LEXICAL_K = 64  # BM25 hits pulled from the cache
_SEED_LIMIT = 20  # personalized-PageRank seed count (BM25-top-N files)
_WHY_MAX = 90  # hard cap for the terse `why` phrase, in characters
_DEGENERATE = 0.5  # min-max value when all candidates are equal / single
_EPS = 1e-12

# Function words only — they add BM25 noise without topic signal.
_STOPWORDS = frozenset(
    {
        "a", "all", "also", "an", "and", "any", "are", "as", "at", "be", "but", "by",
        "can", "could", "did", "do", "does", "each", "for", "from", "had", "has", "have",
        "how", "if", "in", "into", "is", "it", "its", "may", "might", "must", "not", "of",
        "on", "or", "our", "per", "shall", "should", "so", "some", "than", "that", "the",
        "their", "then", "there", "this", "to", "us", "via", "was", "we", "were", "what",
        "when", "which", "why", "will", "with", "would", "you", "your",
    }
)


@dataclass(frozen=True, slots=True)
class Lead:
    """One ranked entry of the PLAN section: a file plus its most relevant symbol."""

    path: str  # repo-relative posix path
    start_line: int
    end_line: int
    why: str = ""  # terse role description, <= 90 chars
    score: float = 0.0  # hybrid score in [0, 1]


# ---------------------------------------------------------------------------
# PageRank (lazy bridge to index.graph + private fallback)
# ---------------------------------------------------------------------------

_PAGERANK_FN: Callable[..., dict[str, float]] | None = None
_PAGERANK_RESOLVED = False


def _pagerank_fn() -> Callable[..., dict[str, float]] | None:
    """Resolve ``index.graph.pagerank`` once, lazily; None when unavailable."""
    global _PAGERANK_FN, _PAGERANK_RESOLVED
    if not _PAGERANK_RESOLVED:
        _PAGERANK_RESOLVED = True
        try:
            from ..index.graph import pagerank as fn
        except ImportError:
            LOGGER.debug("index.graph unavailable; using local PageRank fallback")
            _PAGERANK_FN = None
        else:
            _PAGERANK_FN = fn
    return _PAGERANK_FN


def _fallback_pagerank(
    files: object,
    refs: Iterable[Ref],
    damping: float = 0.85,
    iters: int = 30,
    personalization: dict[str, float] | None = None,
) -> dict[str, float]:
    """Deterministic PageRank over file nodes (same semantics as index.graph.pagerank).

    ``files`` may be a mapping of path -> FileEntry, or a sequence of paths or
    FileEntry-like objects. Edge weight = reference count; dangling mass is
    redistributed according to the personalization vector (uniform if absent).
    """
    if hasattr(files, "keys"):
        nodes = sorted(str(key) for key in files)  # type: ignore[union-attr]
    else:
        nodes = sorted(str(getattr(item, "path", item)) for item in files)  # type: ignore[union-attr]
    n = len(nodes)
    if n == 0:
        return {}
    node_set = set(nodes)

    weight: dict[tuple[str, str], int] = {}
    for ref in sorted(
        refs,
        key=lambda r: (
            str(getattr(r, "src_file", "")),
            str(getattr(r, "dst_file", "")),
            str(getattr(r, "dst_name", "")),
        ),
    ):
        src = str(getattr(ref, "src_file", ""))
        dst = str(getattr(ref, "dst_file", ""))
        if src in node_set and dst in node_set and src != dst:
            weight[(src, dst)] = weight.get((src, dst), 0) + 1

    out_weight: dict[str, int] = {}
    incoming: dict[str, list[tuple[str, int]]] = {}
    for (src, dst), w in sorted(weight.items()):
        out_weight[src] = out_weight.get(src, 0) + w
        incoming.setdefault(dst, []).append((src, w))
    for edges in incoming.values():
        edges.sort()

    if personalization:
        raw = {node: max(0.0, float(personalization.get(node, 0.0))) for node in nodes}
        z = sum(raw.values())
        pers = {node: v / z for node, v in raw.items()} if z > 0.0 else dict.fromkeys(nodes, 1.0 / n)
    else:
        pers = dict.fromkeys(nodes, 1.0 / n)

    ranks: dict[str, float] = dict.fromkeys(nodes, 1.0 / n)
    for _ in range(max(0, iters)):
        dangling = sum(ranks[node] for node in nodes if out_weight.get(node, 0) == 0)
        nxt: dict[str, float] = {}
        for node in nodes:
            inflow = sum(ranks[src] * w / out_weight[src] for src, w in incoming.get(node, ()))
            nxt[node] = (1.0 - damping) * pers[node] + damping * (dangling * pers[node] + inflow)
        ranks = nxt
    return ranks


def file_pagerank(
    files: object,
    refs: Iterable[Ref],
    damping: float = 0.85,
    iters: int = 30,
    personalization: dict[str, float] | None = None,
) -> dict[str, float]:
    """PageRank over files: ``index.graph.pagerank`` if importable, else the fallback.

    Any failure of the external implementation (signature drift, bad input shape)
    degrades to the local fallback so scoring never crashes.
    """
    fn = _pagerank_fn()
    if fn is not None:
        try:
            return {
                str(path): float(score)
                for path, score in dict(
                    fn(files, refs, damping=damping, iters=iters, personalization=personalization)
                ).items()
            }
        except Exception:
            LOGGER.debug("index.graph.pagerank failed; using fallback", exc_info=True)
    return _fallback_pagerank(
        files, refs, damping=damping, iters=iters, personalization=personalization
    )


# ---------------------------------------------------------------------------
# Issue parsing and component scorers
# ---------------------------------------------------------------------------


def issue_tokens(issue: str) -> list[str]:
    """Distinct lowercase alphanumeric tokens of *issue* (stopwords / len<3 dropped)."""
    blob = "".join(c if c.isalnum() else " " for c in (issue or "").lower())
    tokens: list[str] = []
    seen: set[str] = set()
    for tok in blob.split():
        if len(tok) < 3 or tok in _STOPWORDS or tok in seen:
            continue
        seen.add(tok)
        tokens.append(tok)
    return tokens


def _minmax(values: dict[str, float], degenerate: float = _DEGENERATE) -> dict[str, float]:
    """Min-max normalize *values* into [0, 1]; *degenerate* when hi == lo (or single)."""
    if not values:
        return {}
    lo = min(values.values())
    hi = max(values.values())
    if hi - lo <= _EPS:
        return dict.fromkeys(values, degenerate)
    return {key: (val - lo) / (hi - lo) for key, val in values.items()}


def _lexical(cache: IndexCache, tokens: list[str]) -> dict[str, Hit]:
    """Best (lowest-bm25) symbol hit per file, or {} when nothing matches."""
    if not tokens:
        return {}
    try:
        hits = cache.search(" ".join(tokens), k=_LEXICAL_K)
    except Exception:
        LOGGER.debug("cache.search failed; proceeding without lexical signal", exc_info=True)
        return []
    best: dict[str, Hit] = {}
    for hit in sorted(hits, key=lambda h: (h.score, h.file, h.name, h.line)):
        best.setdefault(hit.file, hit)
    return best


def _test_proximity(data: IndexData, tokens: list[str]) -> dict[str, float]:
    """Raw test-proximity signal: matched-token counts for test files, propagated one
    hop to the src files they import via ``data.refs`` (propagated score = source's)."""
    if not tokens:
        return {}
    test_paths = sorted(p for p in data.files if "test" in p.lower())
    if not test_paths:
        return {}
    raw: dict[str, float] = {}
    for path in test_paths:
        entry = data.files[path]
        haystack = " ".join([path.lower(), *(s.name.lower() for s in entry.symbols)])
        matched = sum(1 for tok in tokens if tok in haystack)
        if matched:
            raw[path] = float(matched)
    signal = dict(raw)
    for ref in data.refs:
        if ref.src_file in raw and ref.dst_file and ref.dst_file in data.files:
            prev = signal.get(ref.dst_file, 0.0)
            signal[ref.dst_file] = max(prev, raw[ref.src_file])
    return signal


def _enclosing_class(data: IndexData, sym: Symbol) -> str:
    """Innermost class symbol in the same file whose range contains *sym*, if any."""
    entry = data.files.get(sym.file)
    if entry is None:
        return ""
    classes = [
        c
        for c in entry.symbols
        if c.kind == "class" and c is not sym and c.line <= sym.line <= c.end_line
    ]
    if not classes:
        return ""
    classes.sort(key=lambda c: (c.end_line - c.line, c.line, c.name))
    return classes[0].name


def _why_text(data: IndexData, sym: Symbol) -> str:
    """Terse role phrase from the symbol's doc/signature plus its context, <= 90 chars."""
    enclosing = _enclosing_class(data, sym)
    context = f"{enclosing}.{sym.name}" if enclosing else sym.name
    role = " ".join(sym.doc.split()).rstrip(".") if sym.doc else ""
    if not role:
        sig = " ".join((sym.signature or "").split())
        for prefix in ("async def ", "def ", "class "):
            if sig.startswith(prefix):
                sig = sig[len(prefix) :]
                break
        role = sig.strip() or f"{sym.kind} {sym.name}"
    why = f"{role} ({context})"
    if len(why) > _WHY_MAX:
        why = why[: _WHY_MAX - 3] + "..."
    return why


def _best_symbol(data: IndexData, path: str, hit: Hit | None, tokens: list[str]) -> Symbol | None:
    """The lead symbol for *path*: the BM25-best hit's symbol, else best token overlap,
    else the earliest symbol. Ties break by earliest line, then name."""
    entry = data.files.get(path)
    if entry is None:
        return None
    syms = sorted(entry.symbols, key=lambda s: (s.line, s.name))
    if not syms:
        return None
    if hit is not None:
        named = [s for s in syms if s.name == hit.name]
        if named:
            return named[0]

    def overlap(sym: Symbol) -> int:
        return sum(1 for tok in tokens if tok in sym.name.lower())

    return min(syms, key=lambda s: (-overlap(s), s.line, s.name))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def rank(data: IndexData, cache: IndexCache, issue: str, k: int = 5) -> list[Lead]:
    """Rank candidate files for *issue* and return at most *k* leads.

    Deterministic: same inputs yield byte-identical leads. Scores are clamped to
    [0, 1]. Files without symbols are skipped as leads (no line anchor possible)
    but still participate in normalization.
    """
    if k <= 0 or not data.files:
        return []
    tokens = issue_tokens(issue)

    best_by_file = {f: h for f, h in _lexical(cache, tokens).items() if f in data.files}
    test_signal = _test_proximity(data, tokens)
    candidates = sorted(set(best_by_file) | set(test_signal))
    if not candidates:
        # No lexical and no test signal (vague issue): fall back to PageRank top-k.
        prior0 = file_pagerank(data.files, data.refs)
        candidates = sorted(data.files, key=lambda p: (-prior0.get(p, 0.0), p))[:k]

    lex_raw = {f: max(0.0, -best_by_file[f].score) for f in sorted(best_by_file)}
    lex_norm = _minmax(lex_raw)

    # Personalized PageRank seeded from the BM25-top-20 files.
    lex_order = sorted(best_by_file, key=lambda f: (best_by_file[f].score, f))
    seeds = lex_order[:_SEED_LIMIT]
    personalization: dict[str, float] | None = None
    if seeds:
        raw = {f: 1.0 / (i + 1) for i, f in enumerate(seeds)}
        z = sum(raw.values())
        personalization = {path: raw.get(path, 0.0) / z for path in data.files}
    graph = file_pagerank(data.files, data.refs, personalization=personalization)
    prior = file_pagerank(data.files, data.refs)

    graph_norm = _minmax({f: graph.get(f, 0.0) for f in candidates})
    prior_norm = _minmax({f: prior.get(f, 0.0) for f in candidates})
    test_norm = _minmax({f: test_signal.get(f, 0.0) for f in candidates})

    scored: list[tuple[str, float]] = []
    for path in candidates:
        score = (
            W_LEXICAL * lex_norm.get(path, 0.0)
            + W_GRAPH * graph_norm.get(path, 0.0)
            + W_TEST * test_norm.get(path, 0.0)
            + W_PRIOR * prior_norm.get(path, 0.0)
        )
        scored.append((path, min(1.0, max(0.0, score))))
    scored.sort(key=lambda item: (-item[1], item[0]))

    leads: list[Lead] = []
    for path, score in scored:
        if len(leads) >= k:
            break
        sym = _best_symbol(data, path, best_by_file.get(path), tokens)
        if sym is None:
            continue
        leads.append(
            Lead(
                path=path,
                start_line=sym.line,
                end_line=sym.end_line,
                why=_why_text(data, sym),
                score=score,
            )
        )
    return leads
