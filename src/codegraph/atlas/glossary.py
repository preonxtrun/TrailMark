"""Vocabulary extraction for the GLOSSARY section.

Terms are snake/camel sub-tokens of symbol names and signatures with
``len >= 5`` and ``freq >= 3`` minus a small stopword list. A term's score is
``freq * log(1 + distinct_files)`` — used for ordering only, never emitted
(invariant #1: no floats in output). The top ≤ 12 terms are emitted as::

    - <term> = <short meaning> (<path>:<line>)

where the meaning is the exemplar symbol's doc first line, or its
``kind + signature`` when undocumented.
"""

from __future__ import annotations

import math
import re
from collections import Counter

from ..emit.fmt import truncate
from ..model import IndexData, Symbol

#: Maximum number of glossary rows.
MAX_TERMS = 12
#: Minimum sub-token length and total frequency for a term to qualify.
MIN_LEN = 5
MIN_FREQ = 3
#: Display caps (bytes).
MEANING_CAP = 48

#: Small stopword list for code identifiers (len >= 5 noise words plus keywords).
STOPWORDS: frozenset[str] = frozenset(
    {
        "about", "after", "again", "assert", "before", "check", "class", "default",
        "entry", "except", "false", "field", "input", "item", "items", "kwargs",
        "object", "optional", "output", "param", "params", "print", "raise", "result",
        "results", "return", "self", "should", "string", "super", "value", "values",
        "verify", "while",
    }
)

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SUBTOKEN = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")


def tokenize(text: str) -> list[str]:
    """Split snake/camel identifiers into lowercase sub-tokens.

    Keeps only tokens with ``len >= MIN_LEN`` that are not in :data:`STOPWORDS`.
    """
    out: list[str] = []
    for ident in _IDENT.findall(text):
        for part in ident.split("_"):
            for sub in _SUBTOKEN.findall(part):
                word = sub.lower()
                if len(word) >= MIN_LEN and word not in STOPWORDS:
                    out.append(word)
    return out


def build_glossary(data: IndexData, max_terms: int = MAX_TERMS) -> list[str]:
    """GLOSSARY entry rows for *data* (deterministic; empty when nothing qualifies)."""
    freq: dict[str, int] = {}
    file_sets: dict[str, set[str]] = {}
    # term -> (hits in symbol, file, line, name, symbol) exemplar:
    # most occurrences first, ties broken by (path, line, name) — invariant #1.
    best: dict[str, tuple[int, str, int, str, Symbol]] = {}
    for sym in data.symbols():
        tokens = tokenize(f"{sym.name} {sym.signature}")
        if not tokens:
            continue
        for word, n in Counter(tokens).items():
            freq[word] = freq.get(word, 0) + n
            file_sets.setdefault(word, set()).add(sym.file)
            cur = best.get(word)
            if cur is None or n > cur[0] or (
                n == cur[0] and (sym.file, sym.line, sym.name) < (cur[1], cur[2], cur[3])
            ):
                best[word] = (n, sym.file, sym.line, sym.name, sym)

    scored = [
        (f * math.log(1 + len(file_sets[term])), f, term)
        for term, f in freq.items()
        if f >= MIN_FREQ
    ]
    scored.sort(key=lambda item: (-item[0], -item[1], item[2]))

    rows: list[str] = []
    for _score, _f, term in scored[:max_terms]:
        _n, path, line, _name, sym = best[term]
        rows.append(f"- {term} = {_meaning(sym)} ({path}:{line})")
    return rows


def _meaning(sym: Symbol) -> str:
    """Short meaning: doc first line, else ``kind + signature`` (byte-capped)."""
    doc = " ".join((sym.doc or "").split())
    if doc:
        return truncate(doc, MEANING_CAP)
    base = " ".join(f"{sym.kind} {sym.signature}".split()) or sym.kind
    return truncate(base, MEANING_CAP)
