"""Tree-sitter symbol/reference extraction with regex fallback and process parallelism.

Public surface:
- :func:`parse_files` — parallel extraction over a scanned file list (returns per-file
  ``(lang, symbols, refs)``).
- :func:`resolve_refs` — parent-side resolution of unresolved references against the
  global symbol-name index (plus Go import-path resolution).

Design notes:
- Workers are top-level functions (picklable); only paths cross the process boundary.
- Files are parsed in chunks to amortize IPC; grammars come from tree-sitter-language-pack.
- Any failure (unsupported language, broken parse, worker crash) degrades to a regex
  fallback extractor so a build NEVER fails (hard invariant #4).
- Python/TS/JS/Rust imports are resolved worker-side against the filesystem; Go imports
  are resolved parent-side (they need go.mod plus the global file list).
"""

from __future__ import annotations

import logging
import os
import posixpath
import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from ..model import Ref, Symbol

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .scanner import ScannedFile

logger = logging.getLogger(__name__)

#: Hard cap on references extracted per source file (perf guard).
MAX_REFS_PER_FILE = 2000

#: Files per worker task (amortizes IPC + process dispatch).
CHUNK_SIZE = 32

#: Below this file count, parse in-process (pool spawn costs more than it saves).
INPROC_THRESHOLD = 64

#: Default parallelism when ``workers`` is falsy.
DEFAULT_MAX_WORKERS = 8

# (relpath, lang, symbols, refs) returned per file by workers.
FileResult = tuple[str, str, list[Symbol], list[Ref]]
# (lang, symbols, refs) kept per relpath by the orchestrator.
FileParse = tuple[str, list[Symbol], list[Ref]]
# (dst_name, dst_file_or_"", line) emitted by import extractors.
RawRef = tuple[str, str, int]
# (relpath, abspath, lang) parse task for one file.
ParseTask = tuple[str, str, str]

_DOC_LIMIT = 100
_SIG_LIMIT = 160

_NAME_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")

# ---------------------------------------------------------------------------
# tree-sitter queries (compiled lazily; a bad query degrades that language to regex)
# ---------------------------------------------------------------------------

_DEF_QUERIES: dict[str, str] = {
    "py": "(function_definition) @def\n(class_definition) @def",
    "ts": (
        "(function_declaration) @def"
        "\n(class_declaration) @def"
        "\n(method_definition) @def"
        "\n(lexical_declaration (variable_declarator"
        " value: [(arrow_function) (function_expression)])) @def"
        "\n(interface_declaration) @def"
    ),
    "js": (
        "(function_declaration) @def"
        "\n(class_declaration) @def"
        "\n(method_definition) @def"
        "\n(lexical_declaration (variable_declarator"
        " value: [(arrow_function) (function_expression)])) @def"
        "\n(generator_function_declaration) @def"
    ),
    "go": "(function_declaration) @def\n(method_declaration) @def\n(type_spec) @def",
    "rs": "(function_item) @def\n(struct_item) @def\n(enum_item) @def\n(trait_item) @def",
}

_IMP_QUERIES: dict[str, str] = {
    "py": "(import_statement) @imp\n(import_from_statement) @imp",
    "ts": "(import_statement) @imp\n(export_statement) @imp",
    "js": "(import_statement) @imp\n(export_statement) @imp",
    "go": "(import_declaration) @imp",
    "rs": "(use_declaration) @imp",
}

_IDENT_QUERIES: dict[str, str] = {
    "py": "(identifier) @id",
    "ts": "(identifier) @id\n(property_identifier) @id\n(shorthand_property_identifier) @id",
    "js": "(identifier) @id\n(property_identifier) @id\n(shorthand_property_identifier) @id",
    "go": "(identifier) @id",
    "rs": "(identifier) @id\n(type_identifier) @id",
}

_GRAMMAR_BY_LANG: dict[str, str] = {
    "py": "python",
    "ts": "typescript",
    "js": "javascript",
    "go": "go",
    "rs": "rust",
}

_LANG_BY_GRAMMAR: dict[str, str] = {v: k for k, v in _GRAMMAR_BY_LANG.items()}
_LANG_BY_GRAMMAR["tsx"] = "ts"  # tsx files share the ts queries


def _grammar_for(lang: str, is_tsx: bool) -> str | None:
    """Tree-sitter grammar name for a language (``tsx`` grammar for .tsx files)."""
    grammar = _GRAMMAR_BY_LANG.get(lang)
    if grammar is None:
        return None
    if lang == "ts" and is_tsx:
        return "tsx"  # the tsx grammar is a superset and also parses plain TS
    return grammar


@lru_cache(maxsize=32)
def _compiled(grammar: str, kind: str) -> object | None:
    """Compile and cache a query cursor for (grammar, kind); None when unavailable."""
    lang = _LANG_BY_GRAMMAR.get(grammar)
    sources = {"def": _DEF_QUERIES, "imp": _IMP_QUERIES, "id": _IDENT_QUERIES}
    source = sources.get(kind, {}).get(lang) if lang else None  # type: ignore[union-attr]
    if not source:
        return None
    try:
        from tree_sitter import Query, QueryCursor
        from tree_sitter_language_pack import get_language

        return QueryCursor(Query(get_language(grammar), source))
    except Exception:  # noqa: BLE001 — any grammar/query mismatch must degrade, not crash
        logger.warning(
            "query compilation failed for grammar=%s kind=%s; regex fallback", grammar, kind
        )
        return None


@lru_cache(maxsize=32)
def _parser_for(grammar: str) -> object | None:
    """Return a cached tree-sitter Parser for the grammar (None when unsupported)."""
    try:
        from tree_sitter_language_pack import get_parser

        return get_parser(grammar)
    except Exception:  # noqa: BLE001 — missing/broken grammar must degrade, not crash
        logger.warning("no tree-sitter grammar for %s; regex fallback", grammar)
        return None


# ---------------------------------------------------------------------------
# text helpers
# ---------------------------------------------------------------------------


def _txt(node, src: bytes) -> str:
    """Decode a node's source text."""
    return src[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _collapse(text: str) -> str:
    """Collapse all whitespace runs to single spaces."""
    return " ".join(text.split())


def _clip(text: str, limit: int = _SIG_LIMIT) -> str:
    """Trim to ``limit`` chars with a deterministic ellipsis."""
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _field(node, name: str):
    """``node.child_by_field_name`` that tolerates odd trees."""
    try:
        return node.child_by_field_name(name)
    except Exception:  # noqa: BLE001 — malformed trees must never abort extraction
        return None


def _first_line(node, src: bytes) -> str:
    """First line of a node's text, collapsed, trailing brace/colon stripped."""
    end = src.find(b"\n", node.start_byte, node.end_byte)
    stop = node.end_byte if end == -1 else end
    return _collapse(src[node.start_byte : stop].decode("utf-8", errors="replace")).rstrip(" {;:")


def _param_text(node, src: bytes) -> str:
    params = _field(node, "parameters")
    if params is None:
        return ""
    return _collapse(_txt(params, src))


def _up_to(node, inner, src: bytes) -> str:
    """Node text truncated at ``inner.end_byte`` (for signature prefixes)."""
    end = min(inner.end_byte, node.end_byte) - node.start_byte
    return _collapse(_txt(node, src)[: max(0, end)])


# ---------------------------------------------------------------------------
# signatures + docs
# ---------------------------------------------------------------------------


def _sig_py(node, src: bytes) -> str:
    if node.type == "class_definition":
        name_node = _field(node, "name")
        name = _txt(name_node, src) if name_node is not None else ""
        sup = _field(node, "superclasses")
        # superclasses text already includes the surrounding parens
        bases = _collapse(_txt(sup, src)) if sup is not None else ""
        return _clip(f"class {name}{bases}")
    name_node = _field(node, "name")
    name = _txt(name_node, src) if name_node is not None else ""
    params = _param_text(node, src) or "()"
    ret = _field(node, "return_type")
    ret_text = _collapse(_txt(ret, src)) if ret is not None else ""
    prefix = "async def" if any(c.type == "async" for c in node.children) else "def"
    sig = f"{prefix} {name}{params}"
    return _clip(sig + f" -> {ret_text}" if ret_text else sig)


def _sig_ts(node, src: bytes) -> str:
    t = node.type
    if t in ("class_declaration", "interface_declaration"):
        return _clip(_first_line(node, src))
    if t == "method_definition":
        name_node = _field(node, "name")
        name = _txt(name_node, src) if name_node is not None else ""
        return _clip(f"{name}{_param_text(node, src) or '()'}")
    if t in ("lexical_declaration", "variable_declaration"):
        decl = next((c for c in node.children if c.type == "variable_declarator"), None)
        if decl is not None:
            val = _field(decl, "value")
            params = _field(val, "parameters") if val is not None else None
            if params is not None:
                return _clip(_up_to(decl, params, src))
        return _clip(_first_line(node, src))
    name_node = _field(node, "name")
    name = _txt(name_node, src) if name_node is not None else ""
    prefix = "async " if any(c.type == "async" for c in node.children) else ""
    return _clip(f"{prefix}function {name}{_param_text(node, src) or '()'}")


def _sig_go(node, src: bytes) -> str:
    if node.type == "type_spec":
        return _clip("type " + _first_line(node, src))
    params = _field(node, "parameters")
    if params is not None:
        return _clip(_up_to(node, params, src))
    return _clip(_first_line(node, src))


def _sig_rs(node, src: bytes) -> str:
    if node.type in ("struct_item", "enum_item", "trait_item", "type_item"):
        return _clip(_first_line(node, src))
    params = _field(node, "parameters")
    if params is not None:
        return _clip(_up_to(node, params, src))
    return _clip(_first_line(node, src))


def _doc_py(node, src: bytes) -> str:
    """First line of a python docstring, trimmed."""
    body = _field(node, "body")
    if body is None or not body.child_count:
        return ""
    first = body.children[0]
    if first.type == "expression_statement" and first.child_count:
        inner = first.children[0]
        if inner.type == "string":
            first = inner
    if first.type != "string":
        return ""
    content = next((c for c in first.children if c.type == "string_content"), None)
    if content is None:
        return ""
    text = _txt(content, src).strip()
    return _clip(text.splitlines()[0].strip(), _DOC_LIMIT) if text else ""


def _doc_comment(node, src: bytes) -> str:
    """Doc from a comment node directly above the definition (TS/JS/Go/Rust)."""
    target = node
    if target.parent is not None and target.parent.type == "export_statement":
        target = target.parent
    sib = getattr(target, "prev_named_sibling", None)
    if sib is None or sib.type != "comment":
        return ""
    if sib.end_point[0] < node.start_point[0] - 2:  # allow one blank line of slack
        return ""
    for line in _txt(sib, src).splitlines():
        cleaned = line.strip()
        cleaned = re.sub(r"^/\*+", "", cleaned)
        cleaned = re.sub(r"\*/$", "", cleaned)
        cleaned = cleaned.lstrip("*").strip()
        if cleaned and not cleaned.startswith("@"):
            return _clip(cleaned, _DOC_LIMIT)
    return ""


# ---------------------------------------------------------------------------
# kinds
# ---------------------------------------------------------------------------


def _kind_py(node) -> str:
    if node.type == "class_definition":
        return "class"
    parent = node.parent
    while parent is not None:
        if parent.type == "class_definition":
            return "method"
        if parent.type == "module":
            break
        parent = parent.parent
    return "def"


def _kind_rs(node) -> str:
    if node.type in ("struct_item", "enum_item", "trait_item"):
        return "class"
    parent = node.parent
    while parent is not None:
        if parent.type == "impl_item":
            return "method"
        parent = parent.parent
    return "def"


def _kind_go(node) -> str:
    if node.type == "type_spec":
        return "class"
    return "method" if node.type == "method_declaration" else "def"


_KIND_TS = {
    "class_declaration": "class",
    "interface_declaration": "class",
    "method_definition": "method",
}


# ---------------------------------------------------------------------------
# import extraction (worker-side resolution where the file location suffices)
# ---------------------------------------------------------------------------


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _py_file(root: str, prefix: str) -> str:
    """Repo-relative python module file for a dotted-path prefix, or ''."""
    for cand in (prefix + ".py", prefix + "/__init__.py"):
        if cand and os.path.isfile(os.path.join(root, cand)):
            return cand
    return ""


def _py_ancestors(rel_dir: str) -> list[str]:
    """``app/core`` → ``['app/core', 'app', '']`` (nearest import root first)."""
    parts = rel_dir.split("/") if rel_dir else []
    return ["/".join(parts[:i]) for i in range(len(parts), -1, -1)]


def _py_module_prefix(root: str, relpath: str, level: int, module: str) -> str:
    """Repo-relative path prefix for a python module ('' when not in the repo).

    ``level > 0`` means a relative import (1 = current package). Absolute modules are
    searched from the file's directory up to the repo root, nearest first.
    """
    rel_dir = posixpath.dirname(relpath)
    if level > 0:
        ancestors = _py_ancestors(rel_dir)
        base = ancestors[min(level - 1, len(ancestors) - 1)]
        return posixpath.join(base, module.replace(".", "/")) if module else base
    module_rel = module.replace(".", "/")
    for anc in _py_ancestors(rel_dir):
        prefix = posixpath.join(anc, module_rel) if anc else module_rel
        if _py_file(root, prefix) or os.path.isdir(os.path.join(root, prefix)):
            return prefix
    return ""


def _py_import_refs(root: str, relpath: str, node, src: bytes) -> list[RawRef]:
    line = node.start_point[0] + 1
    if node.type == "import_statement":
        refs: list[RawRef] = []
        for child in node.children:
            if child.type != "dotted_name":
                continue
            module = _txt(child, src)
            prefix = _py_module_prefix(root, relpath, 0, module)
            target = _py_file(root, prefix) if prefix else ""
            if not target and "/" in prefix:
                target = _py_file(root, prefix.rsplit("/", 1)[0])
            refs.append((module, target, line))
        return refs

    # import_from_statement
    mod_node = _field(node, "module_name")
    level = 0
    module = ""
    if mod_node is not None:
        if mod_node.type == "relative_import":
            prefix_node = next((c for c in mod_node.children if c.type == "import_prefix"), None)
            dots = _txt(prefix_node, src) if prefix_node is not None else "."
            level = max(1, dots.count("."))
            dn = next((c for c in mod_node.children if c.type == "dotted_name"), None)
            module = _txt(dn, src) if dn is not None else ""
        else:
            module = _txt(mod_node, src)
    names: list[str] = []
    stack = list(node.children)
    seen_import_kw = False
    while stack:
        child = stack.pop(0)
        if child.type == "import":
            seen_import_kw = True
            continue
        if not seen_import_kw:
            continue
        if child.type == "dotted_name":
            names.append(_txt(child, src))
        elif child.type == "aliased_import":
            dn = _field(child, "name")
            if dn is not None:
                names.append(_txt(dn, src))
        elif child.type == "wildcard_import":
            names.append("*")
        elif child.type in ("parenthesized_import_list", "import_list"):
            stack = list(child.children) + stack
    prefix = _py_module_prefix(root, relpath, level, module)
    refs = []
    for name in names:
        target = ""
        if prefix:
            sub = _py_file(root, f"{prefix}/{name.replace('.', '/')}")
            target = sub or _py_file(root, prefix)
        refs.append((name, target, line))
    return refs


def _ts_resolve(root: str, relpath: str, spec: str) -> str:
    """Resolve a TS/JS import specifier to a repo-relative file, or ''."""
    if not spec.startswith(("./", "../", "/")):
        return ""
    rel_dir = posixpath.dirname(relpath)
    base = spec.removeprefix("/")
    joined = posixpath.normpath(posixpath.join(rel_dir, base))
    if joined.startswith(".."):
        return ""
    candidates: list[str] = []
    if "." in posixpath.basename(joined):
        candidates.append(joined)
    for ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
        candidates.append(joined + ext)
        candidates.append(posixpath.join(joined, "index" + ext))
    for cand in candidates:
        if cand and os.path.isfile(os.path.join(root, cand)):
            return cand
    return ""


def _ts_module_base(spec: str) -> str:
    base = spec.rstrip("/").rsplit("/", 1)[-1] or spec
    return re.sub(r"\.(js|jsx|mjs|cjs|ts|tsx)$", "", base)


def _ts_clause_names(node, src: bytes) -> list[str]:
    """Names referenced by an import/export clause (module-side names)."""
    names: list[str] = []
    stack = list(node.children)
    while stack:
        child = stack.pop(0)
        if child.type in ("import_clause", "export_clause", "named_imports", "named_exports"):
            stack = list(child.children) + stack
        elif child.type in ("import_specifier", "export_specifier"):
            name_node = _field(child, "name")
            if name_node is not None:
                names.append(_txt(name_node, src))
        elif child.type == "namespace_import":
            alias = next((c for c in child.children if c.type == "identifier"), None)
            names.append(_txt(alias, src) if alias is not None else "")
    return names


def _ts_import_refs(root: str, relpath: str, node, src: bytes) -> list[RawRef]:
    src_node = _field(node, "source")
    if src_node is None or src_node.type != "string":
        return []
    line = node.start_point[0] + 1
    spec = _unquote(_txt(src_node, src))
    target = _ts_resolve(root, relpath, spec)
    names = [n for n in _ts_clause_names(node, src) if n]
    if not names:
        names = [_ts_module_base(spec)]
    return [(n, target, line) for n in names]


def _go_import_refs(node, src: bytes) -> list[RawRef]:
    """Go import paths as unresolved raw refs (parent resolves via go.mod + file list)."""
    refs: list[RawRef] = []
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type == "import_spec":
            path_node = _field(n, "path")
            if path_node is not None:
                refs.append((_unquote(_txt(path_node, src)), "", n.start_point[0] + 1))
        else:
            stack.extend(n.children)
    return refs


def _rs_flatten_use(text: str) -> list[str]:
    """Flatten ``crate::util::{alpha, beta}`` into ``['crate::util::alpha', ...]``."""
    flat = text.replace("{", "::").replace("}", "")
    flat = re.sub(r":{2,}", "::", flat)
    return [part.strip() for part in flat.split(",") if part.strip()]


def _rs_resolve(root: str, relpath: str, base_segs: list[str], leaf: str) -> str:
    """Resolve a rust ``use`` path against crate/self/super conventions."""
    crate_root = "src" if os.path.isdir(os.path.join(root, "src")) else ""
    rel_dir = posixpath.dirname(relpath)
    if not base_segs:
        return ""
    head = base_segs[0]
    prefix = ""
    if head == "crate":
        parts = ([crate_root] if crate_root else []) + base_segs[1:]
        prefix = "/".join(parts)
    elif head == "self":
        parts = ([rel_dir] if rel_dir else []) + base_segs[1:]
        prefix = "/".join(parts)
    elif head == "super":
        ups = 0
        for seg in base_segs:
            if seg == "super":
                ups += 1
            else:
                break
        rest = base_segs[ups:]
        d = rel_dir
        for _ in range(ups):
            d = posixpath.dirname(d)
        parts = ([d] if d else []) + rest
        prefix = "/".join(parts)
    else:
        return ""  # external crate — cannot map into the repo
    cands = [f"{prefix}/{leaf}.rs", f"{prefix}/{leaf}/mod.rs", f"{prefix}.rs", f"{prefix}/mod.rs"]
    if leaf in ("", "*"):
        cands = [f"{prefix}.rs", f"{prefix}/mod.rs"]
    for cand in cands:
        if cand and os.path.isfile(os.path.join(root, cand)):
            return cand
    return ""


def _rs_use_refs(root: str, relpath: str, node, src: bytes) -> list[RawRef]:
    line = node.start_point[0] + 1
    arg = None
    for child in node.children:
        if child.type == "use":
            continue
        arg = child
        break
    if arg is None:
        return []
    refs: list[RawRef] = []
    for path in _rs_flatten_use(_txt(arg, src)):
        path = re.sub(r"\s+as\s+\w+$", "", path).strip()
        segs = [s for s in path.split("::") if s]
        if not segs:
            continue
        leaf = segs[-1]
        target = _rs_resolve(root, relpath, segs[:-1], leaf)
        refs.append((leaf, target, line))
    return refs


def _import_refs(root: str, relpath: str, lang: str, node, src: bytes) -> list[RawRef]:
    if lang == "py":
        return _py_import_refs(root, relpath, node, src)
    if lang in ("ts", "js"):
        return _ts_import_refs(root, relpath, node, src)
    if lang == "go":
        return _go_import_refs(node, src)
    if lang == "rs":
        return _rs_use_refs(root, relpath, node, src)
    return []


# ---------------------------------------------------------------------------
# regex fallback extractor (broken parses, unsupported languages)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Fallback:
    """One compiled fallback pattern and how to derive kind/signature from it."""

    regex: re.Pattern[str]
    kind: str  # "def" | "class" | "auto_def" | "auto_class" | "go_func" | "c_func"


_C_IDENT = r"[A-Za-z_$][A-Za-z0-9_$]*"
_FALLBACKS: dict[str, tuple[_Fallback, ...]] = {
    "py": (
        _Fallback(
            re.compile(rf"^[ \t]*(?:async[ \t]+)?def[ \t]+({_C_IDENT})", re.MULTILINE), "auto_def"
        ),
        _Fallback(re.compile(rf"^[ \t]*class[ \t]+({_C_IDENT})", re.MULTILINE), "auto_class"),
    ),
    "ts": (
        _Fallback(
            re.compile(
                rf"^[ \t]*(?:export[ \t]+)?(?:default[ \t]+)?(?:async[ \t]+)?function"
                rf"[ \t]*\*?[ \t]*({_C_IDENT})",
                re.MULTILINE,
            ),
            "def",
        ),
        _Fallback(
            re.compile(
                rf"^[ \t]*(?:export[ \t]+)?(?:default[ \t]+)?(?:abstract[ \t]+)?class"
                rf"[ \t]+({_C_IDENT})",
                re.MULTILINE,
            ),
            "class",
        ),
        _Fallback(
            re.compile(
                rf"^[ \t]*(?:export[ \t]+)?(?:const|let|var)[ \t]+({_C_IDENT})[ \t]*=[ \t]*"
                r"(?:async[ \t]*)?(?:function\b|\([^)]*\)[ \t]*=>|[A-Za-z_$][\w$]*[ \t]*=>)",
                re.MULTILINE,
            ),
            "def",
        ),
    ),
    "go": (
        _Fallback(
            re.compile(r"^func(?:[ \t]+\([^)]+\))?[ \t]+([A-Za-z_]\w*)", re.MULTILINE), "go_func"
        ),
        _Fallback(
            re.compile(r"^type[ \t]+([A-Za-z_]\w*)[ \t]+(?:struct|interface)\b", re.MULTILINE),
            "class",
        ),
    ),
    "rs": (
        _Fallback(
            re.compile(r"^[ \t]*(?:pub(?:\([^)]*\))?[ \t]+)?fn[ \t]+([A-Za-z_]\w*)", re.MULTILINE),
            "def",
        ),
        _Fallback(
            re.compile(
                r"^[ \t]*(?:pub(?:\([^)]*\))?[ \t]+)?(?:struct|enum|trait)[ \t]+([A-Za-z_]\w*)",
                re.MULTILINE,
            ),
            "class",
        ),
    ),
    "c": (
        _Fallback(
            re.compile(
                r"^[ \t]*(?:[A-Za-z_][\w \t*]*?[ \t*])?([A-Za-z_]\w*)[ \t]*"
                r"\([^;{}()]*\)[ \t]*\{",
                re.MULTILINE,
            ),
            "c_func",
        ),
        _Fallback(
            re.compile(r"^[ \t]*(?:typedef[ \t]+)?struct[ \t]+([A-Za-z_]\w*)", re.MULTILINE),
            "class",
        ),
    ),
}

_C_KEYWORDS = frozenset(
    {"if", "for", "while", "switch", "return", "else", "do", "sizeof", "goto", "case"}
)


def _fallback_end_line(lines: list[str], idx: int) -> int:
    """Python block end: last line before the next non-blank line at column <= indent."""
    indent = len(lines[idx]) - len(lines[idx].lstrip(" \t"))
    j = idx + 1
    while j < len(lines):
        stripped = lines[j].strip()
        if stripped:
            cur = len(lines[j]) - len(lines[j].lstrip(" \t"))
            if cur <= indent:
                return j  # next block starts at line j+1 (1-based), block ends at j
        j += 1
    return len(lines)


def _regex_symbols(src: bytes, lang: str, relpath: str) -> list[Symbol]:
    """Best-effort symbol extraction with plain regexes (never raises)."""
    fallbacks = _FALLBACKS.get(lang)
    if not fallbacks:
        return []
    text = src.decode("utf-8", errors="replace")
    lines = text.splitlines()
    symbols: list[Symbol] = []
    for fb in fallbacks:
        for m in fb.regex.finditer(text):
            name = m.group(1)
            if not name or name in _C_KEYWORDS:
                continue
            line = text.count("\n", 0, m.start(1)) + 1
            raw = m.group(0)
            kind = fb.kind
            if kind in ("auto_def", "auto_class"):
                kind = (
                    "method" if (len(raw) - len(raw.lstrip(" \t"))) else kind.replace("auto_", "")
                )
            elif kind == "go_func":
                kind = "method" if re.match(r"func[ \t]+\(", raw) else "def"
            elif kind == "c_func":
                kind = "def"
            end_line = line
            if lang == "py":
                end_line = _fallback_end_line(lines, line - 1)
            sig = _clip(_collapse(raw).rstrip(" {;:"), _SIG_LIMIT)
            symbols.append(
                Symbol(
                    file=relpath,
                    name=name,
                    kind=kind,
                    line=line,
                    end_line=max(line, end_line),
                    signature=sig,
                    doc="",
                )
            )
    symbols.sort(key=lambda s: (s.line, s.name))
    return symbols


# ---------------------------------------------------------------------------
# per-file extraction
# ---------------------------------------------------------------------------


def _extract_symbols(lang: str, grammar: str, root_node, src: bytes, relpath: str) -> list[Symbol]:
    cursor = _compiled(grammar, "def")
    if cursor is None:
        return []
    sig_fn = {"py": _sig_py, "ts": _sig_ts, "js": _sig_ts, "go": _sig_go, "rs": _sig_rs}[lang]
    symbols: list[Symbol] = []
    for node in cursor.captures(root_node).get("def", []):
        name_node = _field(node, "name")
        if name_node is None and node.type in ("lexical_declaration", "variable_declaration"):
            decl = next((c for c in node.children if c.type == "variable_declarator"), None)
            name_node = _field(decl, "name") if decl is not None else None
        if name_node is None:
            continue
        if (
            lang == "go"
            and node.type == "type_spec"
            and not any(c.type in ("struct_type", "interface_type") for c in node.children)
        ):
            continue  # plain alias ``type X = Y`` — not a definition anchor
        name = _txt(name_node, src)
        if not name:
            continue
        if lang == "py":
            kind = _kind_py(node)
        elif lang in ("ts", "js"):
            kind = _KIND_TS.get(node.type, "def")
        elif lang == "rs":
            kind = _kind_rs(node)
        else:
            kind = _kind_go(node)
        doc = _doc_py(node, src) if lang == "py" else _doc_comment(node, src)
        symbols.append(
            Symbol(
                file=relpath,
                name=name,
                kind=kind,
                line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                signature=sig_fn(node, src),
                doc=doc,
            )
        )
    return symbols


def _is_plausible_name(name: str) -> bool:
    return 2 <= len(name) <= 80 and bool(_NAME_RE.match(name))


def _span(node) -> tuple[int, int]:
    """Deterministic sort key for tree-sitter capture nodes (byte span)."""
    return (node.start_byte, node.end_byte)


def _extract_refs(
    root: str, relpath: str, lang: str, grammar: str, root_node, src: bytes
) -> list[Ref]:
    """Import refs first (highest value), then identifier occurrences, capped."""
    raws: list[RawRef] = []
    imp_cursor = _compiled(grammar, "imp")
    if imp_cursor is not None:
        # captures() (dict API) returns per-parse-random order — sort by byte span
        # to restore deterministic source order (invariant #1).
        imp_nodes = sorted(imp_cursor.captures(root_node).get("imp", []), key=_span)
        for node in imp_nodes:
            for raw in _import_refs(root, relpath, lang, node, src):
                raws.append(raw)
                if len(raws) >= MAX_REFS_PER_FILE:
                    break
            if len(raws) >= MAX_REFS_PER_FILE:
                break
    # imports first (highest value), then identifier occurrences, capped
    id_cursor = _compiled(grammar, "id")
    if id_cursor is not None and len(raws) < MAX_REFS_PER_FILE:
        id_nodes = sorted(id_cursor.captures(root_node).get("id", []), key=_span)
        for node in id_nodes:
            name = _txt(node, src)
            if not _is_plausible_name(name):
                continue
            raws.append((name, "", node.start_point[0] + 1))
            if len(raws) >= MAX_REFS_PER_FILE:
                break
    out: list[Ref] = []
    seen: set[tuple[str, str, int]] = set()
    for dst_name, dst_file, line in raws:
        key = (dst_name, dst_file, line)
        if key in seen:
            continue
        seen.add(key)
        out.append(Ref(src_file=relpath, dst_name=dst_name, dst_file=dst_file, line=line))
    return out


def _parse_file(root: str, relpath: str, abspath: str, lang: str) -> FileResult:
    """Parse one file: tree-sitter extraction, unioned with regex fallback on errors."""
    try:
        with open(abspath, "rb") as fh:
            src = fh.read()
    except OSError as exc:
        logger.warning("cannot read %s: %s", relpath, exc)
        return (relpath, lang, [], [])
    symbols: list[Symbol] = []
    refs: list[Ref] = []
    parsed_ok = False
    has_error = False
    grammar = _grammar_for(lang, abspath.endswith(".tsx"))
    parser = _parser_for(grammar) if grammar else None
    def_cursor = _compiled(grammar, "def") if grammar else None
    if parser is not None and def_cursor is not None:
        try:
            tree = parser.parse(src)
            has_error = bool(tree.root_node.has_error)
            symbols = _extract_symbols(lang, grammar, tree.root_node, src, relpath)
            refs = _extract_refs(root, relpath, lang, grammar, tree.root_node, src)
            parsed_ok = True
        except Exception:
            logger.warning(
                "tree-sitter parse failed for %s; regex fallback", relpath, exc_info=True
            )
    if not parsed_ok or has_error:
        known = {(s.name, s.line) for s in symbols}
        spans = [(s.line, s.end_line) for s in symbols]
        for sym in _regex_symbols(src, lang, relpath):
            if (sym.name, sym.line) in known:
                continue
            if any(a <= sym.line <= b for a, b in spans):
                continue
            symbols.append(sym)
        symbols.sort(key=lambda s: (s.line, s.name))
    return (relpath, lang, symbols, refs)


def _parse_chunk(task: tuple[str, tuple[ParseTask, ...]]) -> list[FileResult]:
    """Worker entry point: parse a chunk of files, never raising per-file errors."""
    root, items = task
    results: list[FileResult] = []
    for relpath, abspath, lang in items:
        try:
            results.append(_parse_file(root, relpath, abspath, lang))
        except Exception:
            logger.exception("parse failed for %s — skipped", relpath)
            results.append((relpath, lang, [], []))
    return results


def _resolve_workers(workers: int, n_files: int) -> int:
    if workers and workers > 0:
        w = workers
    else:
        w = min(DEFAULT_MAX_WORKERS, os.cpu_count() or 1)
    w = max(1, min(w, 32))
    if n_files <= INPROC_THRESHOLD:
        return 1
    return w


def _chunks(files: Sequence[ScannedFile], root: str) -> list[tuple[str, tuple[ParseTask, ...]]]:
    tasks: list[ParseTask] = [(f[0], f[1], f[4]) for f in files]
    return [(root, tuple(tasks[i : i + CHUNK_SIZE])) for i in range(0, len(tasks), CHUNK_SIZE)]


def parse_files(
    root: str | Path, files: Sequence[ScannedFile], workers: int = 0
) -> dict[str, FileParse]:
    """Parse ``files`` (scanner tuples) in parallel; returns relpath → (lang, symbols, refs).

    Uses a ProcessPoolExecutor for large file sets (tree-sitter parsing is GIL-bound)
    and falls back to in-process parsing for small inputs or when the pool fails.
    """
    if not files:
        return {}
    root_str = str(Path(root).resolve())
    n_workers = _resolve_workers(workers, len(files))
    chunk_tasks = _chunks(files, root_str)
    results: dict[str, FileParse] = {}

    def consume(chunk_results: Iterable[FileResult]) -> None:
        for relpath, lang, symbols, refs in chunk_results:
            results[relpath] = (lang, symbols, refs)

    if n_workers <= 1:
        for chunk in chunk_tasks:
            consume(_parse_chunk(chunk))
        return results
    try:
        import multiprocessing

        if "fork" in multiprocessing.get_all_start_methods():
            ctx = multiprocessing.get_context("fork")
            executor: ProcessPoolExecutor = ProcessPoolExecutor(
                max_workers=n_workers, mp_context=ctx
            )
        else:
            executor = ProcessPoolExecutor(max_workers=n_workers)
        with executor:
            for chunk_results in executor.map(_parse_chunk, chunk_tasks):
                consume(chunk_results)
    except Exception:
        logger.exception("process pool failed; re-parsing in-process")
        results.clear()
        for chunk in chunk_tasks:
            consume(_parse_chunk(chunk))
    return results


# ---------------------------------------------------------------------------
# parent-side reference resolution
# ---------------------------------------------------------------------------


def _looks_like_module_path(name: str) -> bool:
    """Heuristic: dotted / path-shaped / rust-path names survive when unresolved."""
    if len(name) <= 1 or any(c.isspace() for c in name):
        return False
    return "." in name or "::" in name or "/" in name


def _pick_candidate(candidates: Sequence[str], src_file: str, src_dir: str) -> str:
    """Deterministic candidate: same directory first, then lexicographic path."""
    best = ""
    best_key: tuple[int, str] | None = None
    for cand in candidates:
        if cand == src_file:
            continue
        key = (0 if posixpath.dirname(cand) == src_dir else 1, cand)
        if best_key is None or key < best_key:
            best, best_key = cand, key
    return best


def _go_dirs(paths: Collection[str]) -> dict[str, list[str]]:
    """Map every directory containing .go files to its sorted list of .go files."""
    dirs: dict[str, list[str]] = {}
    for p in paths:
        if p.endswith(".go"):
            dirs.setdefault(posixpath.dirname(p), []).append(p)
    for files_list in dirs.values():
        files_list.sort()
    return dirs


def _resolve_go_import(
    import_path: str, go_module: str, go_dir_map: dict[str, list[str]]
) -> list[str]:
    """Map a Go import path onto the repo files of the target package directory."""
    candidates: list[str] = []
    if go_module and import_path.startswith(go_module + "/"):
        candidates.append(import_path[len(go_module) + 1 :])
    parts = import_path.split("/")
    if len(parts) >= 2:
        candidates.append("/".join(parts[-2:]))
    candidates.append(import_path)
    for cand in candidates:
        files_list = go_dir_map.get(cand)
        if files_list:
            return list(files_list)
    for cand in candidates:
        matches = sorted(d for d in go_dir_map if d.endswith("/" + cand))
        if matches:
            best = min(matches, key=lambda d: (len(d), d))
            return list(go_dir_map[best])
    return []


def resolve_refs(
    refs_by_file: Mapping[str, Sequence[Ref]],
    name_index: Mapping[str, Sequence[str]],
    all_paths: Collection[str],
    lang_of: Mapping[str, str] | None = None,
    go_module: str = "",
) -> list[Ref]:
    """Resolve unresolved refs and return the deterministic global ref list.

    - Already-resolved refs are kept when their target still exists in ``all_paths``.
    - Unresolved refs are matched against ``name_index`` (defined symbol names).
    - Module-path-shaped leftovers (external ``os.path``, rust crate paths, go
      packages that map nowhere) are preserved with ``dst_file=""``; other misses
      are dropped as noise.
    - Self-file edges are dropped; output sorted by ``(src, line, name, dst)``.
    """
    langs = lang_of or {}
    path_set = set(all_paths)
    go_dir_map = _go_dirs(path_set)
    out: list[Ref] = []
    seen: set[tuple[str, str, str, int]] = set()

    def emit(src: str, name: str, dst: str, line: int) -> None:
        key = (src, name, dst, line)
        if key not in seen:
            seen.add(key)
            out.append(Ref(src_file=src, dst_name=name, dst_file=dst, line=line))

    for src_file in sorted(refs_by_file):
        src_dir = posixpath.dirname(src_file)
        for ref in refs_by_file[src_file]:
            if ref.dst_file:
                if ref.dst_file == src_file or ref.dst_file not in path_set:
                    continue
                emit(src_file, ref.dst_name, ref.dst_file, ref.line)
                continue
            targets: list[str] = []
            if langs.get(src_file) == "go":
                targets = [
                    t
                    for t in _resolve_go_import(ref.dst_name, go_module, go_dir_map)
                    if t != src_file and t in path_set
                ]
            elif ref.dst_name in name_index:
                pick = _pick_candidate(name_index[ref.dst_name], src_file, src_dir)
                if pick and pick in path_set:
                    targets = [pick]
            if targets:
                for target in targets:
                    emit(src_file, ref.dst_name, target, ref.line)
            elif _looks_like_module_path(ref.dst_name):
                emit(src_file, ref.dst_name, "", ref.line)
    out.sort(key=lambda r: (r.src_file, r.line, r.dst_name, r.dst_file))
    return out


def read_go_module(root: str | Path) -> str:
    """Read the module path declared in ``<root>/go.mod`` ('' when absent)."""
    try:
        text = (Path(root) / "go.mod").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    m = re.search(r"(?m)^\s*module\s+(\S+)\s*$", text)
    return m.group(1) if m else ""


__all__ = [
    "CHUNK_SIZE",
    "MAX_REFS_PER_FILE",
    "FileParse",
    "FileResult",
    "parse_files",
    "read_go_module",
    "resolve_refs",
]
