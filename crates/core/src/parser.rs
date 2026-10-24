//! Tree-sitter symbol/reference extraction (port of `index/parser.py`).
//!
//! Public surface:
//! - [`parse_files`] — parallel extraction over a scanned file list.
//! - [`resolve_refs`] — parent-side resolution of unresolved references against
//!   the global symbol-name index (plus Go import-path resolution).
//!
//! Design notes:
//! - Files are parsed in chunks with rayon; grammars are compiled once per process.
//! - Any failure (unsupported language, broken parse) degrades to a regex fallback
//!   extractor so a build NEVER fails (hard invariant #4).
//! - Python/TS/JS/Rust imports are resolved against the filesystem; Go imports are
//!   resolved by [`resolve_refs`] (they need go.mod plus the global file list).

use std::collections::{HashMap, HashSet, VecDeque};
use std::path::Path;
use std::sync::OnceLock;

use rayon::prelude::*;
use tree_sitter::{Node, Parser, Query, QueryCursor, StreamingIterator};

use crate::model::{Ref, Symbol};
use crate::scanner::ScannedFile;

/// Hard cap on references extracted per source file (perf guard).
pub const MAX_REFS_PER_FILE: usize = 2000;

/// Files per worker task (amortizes dispatch overhead).
pub const CHUNK_SIZE: usize = 32;

/// Below this file count, parse sequentially (pool setup costs more than it saves).
pub const INPROC_THRESHOLD: usize = 64;

const DOC_LIMIT: usize = 100;
const SIG_LIMIT: usize = 160;

// ---------------------------------------------------------------------------
// grammar + query definitions (mirrors of the Python query sources)
// ---------------------------------------------------------------------------

const DEF_QUERIES: &[(&str, &str)] = &[
    ("py", "(function_definition) @def\n(class_definition) @def"),
    (
        "ts",
        "(function_declaration) @def\n(class_declaration) @def\n(method_definition) @def\n(lexical_declaration (variable_declarator value: [(arrow_function) (function_expression)])) @def\n(interface_declaration) @def",
    ),
    (
        "js",
        "(function_declaration) @def\n(class_declaration) @def\n(method_definition) @def\n(lexical_declaration (variable_declarator value: [(arrow_function) (function_expression)])) @def\n(generator_function_declaration) @def",
    ),
    (
        "go",
        "(function_declaration) @def\n(method_declaration) @def\n(type_spec) @def",
    ),
    (
        "rs",
        "(function_item) @def\n(struct_item) @def\n(enum_item) @def\n(trait_item) @def",
    ),
];

const IMP_QUERIES: &[(&str, &str)] = &[
    (
        "py",
        "(import_statement) @imp\n(import_from_statement) @imp",
    ),
    ("ts", "(import_statement) @imp\n(export_statement) @imp"),
    ("js", "(import_statement) @imp\n(export_statement) @imp"),
    ("go", "(import_declaration) @imp"),
    ("rs", "(use_declaration) @imp"),
];

const IDENT_QUERIES: &[(&str, &str)] = &[
    ("py", "(identifier) @id"),
    (
        "ts",
        "(identifier) @id\n(property_identifier) @id\n(shorthand_property_identifier) @id",
    ),
    (
        "js",
        "(identifier) @id\n(property_identifier) @id\n(shorthand_property_identifier) @id",
    ),
    ("go", "(identifier) @id"),
    ("rs", "(identifier) @id\n(type_identifier) @id"),
];

/// Grammar name for a language key (`.tsx` files use the tsx grammar).
fn grammar_for(lang: &str, is_tsx: bool) -> Option<&'static str> {
    match lang {
        "py" => Some("python"),
        "ts" if is_tsx => Some("tsx"),
        "ts" => Some("typescript"),
        "js" => Some("javascript"),
        "go" => Some("go"),
        "rs" => Some("rust"),
        _ => None,
    }
}

/// tree-sitter [`Language`] for a grammar name (None → regex fallback).
fn ts_language(grammar: &str) -> Option<tree_sitter::Language> {
    let lang_fn = match grammar {
        "python" => tree_sitter_python::LANGUAGE,
        "typescript" => tree_sitter_typescript::LANGUAGE_TYPESCRIPT,
        "tsx" => tree_sitter_typescript::LANGUAGE_TSX,
        "javascript" => tree_sitter_javascript::LANGUAGE,
        "go" => tree_sitter_go::LANGUAGE,
        "rust" => tree_sitter_rust::LANGUAGE,
        _ => return None,
    };
    Some(tree_sitter::Language::from(lang_fn))
}

/// Compiled queries for one language, built once and shared across threads.
struct LangQueries {
    def: Option<Query>,
    imp: Option<Query>,
    id: Option<Query>,
}

impl LangQueries {
    fn new(lang: &str) -> Self {
        let compile = |table: &[(&str, &str)]| -> Option<Query> {
            let src = table.iter().find(|(l, _)| *l == lang)?.1;
            let grammar = grammar_for(lang, false)?;
            let ts_lang = ts_language(grammar)?;
            // Any grammar/query mismatch must degrade to regex, never crash.
            Query::new(&ts_lang, src).ok()
        };
        LangQueries {
            def: compile(DEF_QUERIES),
            imp: compile(IMP_QUERIES),
            id: compile(IDENT_QUERIES),
        }
    }
}

static QUERIES: OnceLock<HashMap<&'static str, LangQueries>> = OnceLock::new();

fn queries() -> &'static HashMap<&'static str, LangQueries> {
    QUERIES.get_or_init(|| {
        ["py", "ts", "js", "go", "rs"]
            .iter()
            .map(|lang| (*lang, LangQueries::new(lang)))
            .collect()
    })
}

// Per-thread parser cache (Parser is neither Send nor Sync).
thread_local! {
    static PARSERS: std::cell::RefCell<HashMap<&'static str, Parser>> =
        std::cell::RefCell::new(HashMap::new());
}

fn parse_with(grammar: &'static str, src: &[u8]) -> Option<tree_sitter::Tree> {
    PARSERS.with(|cell| {
        let mut map = cell.borrow_mut();
        let parser = map.entry(grammar).or_insert_with(|| {
            let mut p = Parser::new();
            if let Some(lang) = ts_language(grammar) {
                let _ = p.set_language(&lang);
            }
            p
        });
        parser.parse(src, None)
    })
}

// ---------------------------------------------------------------------------
// text helpers
// ---------------------------------------------------------------------------

/// Decode a node's source text (Python `errors="replace"` semantics).
fn txt_lossy(node: Node, src: &[u8]) -> String {
    String::from_utf8_lossy(&src[node.start_byte()..node.end_byte()]).into_owned()
}

fn collapse(text: &str) -> String {
    text.split_whitespace().collect::<Vec<_>>().join(" ")
}

fn clip(text: &str, limit: usize) -> String {
    if limit == 0 {
        return String::new();
    }
    if text.chars().count() <= limit {
        text.to_string()
    } else {
        let head: String = text.chars().take(limit - 3).collect();
        format!("{head}...")
    }
}

fn first_line(node: Node, src: &[u8]) -> String {
    let text = txt_lossy(node, src);
    let line = text.split('\n').next().unwrap_or("");
    collapse(line)
        .trim_end_matches([' ', '{', ';', ':'])
        .to_string()
}

fn param_text(node: Node, src: &[u8]) -> String {
    match node.child_by_field_name("parameters") {
        Some(p) => collapse(&txt_lossy(p, src)),
        None => String::new(),
    }
}

fn params_or_empty(node: Node, src: &[u8]) -> String {
    let p = param_text(node, src);
    if p.is_empty() {
        "()".to_string()
    } else {
        p
    }
}

/// Node text truncated at `inner.end_byte` (for signature prefixes).
///
/// Mirrors the Python reference exactly: the byte-derived length is applied as a
/// *character* slice of the decoded text (`_txt(...)[:end]`).
fn up_to(node: Node, inner: Node, src: &[u8]) -> String {
    let end = inner
        .end_byte()
        .min(node.end_byte())
        .saturating_sub(node.start_byte());
    let text = txt_lossy(node, src);
    let prefix: String = text.chars().take(end).collect();
    collapse(&prefix)
}

fn has_child(node: Node, kind: &str) -> bool {
    children_of(node).iter().any(|c| c.kind() == kind)
}

/// Collect a node's children (named + anonymous) into a Vec.
///
/// tree-sitter 0.25's `Node::children` requires a caller-supplied cursor; this
/// convenience allocates a fresh one per call (fine for the small subtrees we
/// inspect).
fn children_of<'a>(node: Node<'a>) -> Vec<Node<'a>> {
    let mut cursor = node.walk();
    node.children(&mut cursor).collect()
}

// ---------------------------------------------------------------------------
// signatures + docs
// ---------------------------------------------------------------------------

fn sig_py(node: Node, src: &[u8]) -> String {
    if node.kind() == "class_definition" {
        let name = node
            .child_by_field_name("name")
            .map(|n| txt_lossy(n, src))
            .unwrap_or_default();
        // superclasses text already includes the surrounding parens
        let bases = node
            .child_by_field_name("superclasses")
            .map(|n| collapse(&txt_lossy(n, src)))
            .unwrap_or_default();
        return clip(&format!("class {name}{bases}"), SIG_LIMIT);
    }
    let name = node
        .child_by_field_name("name")
        .map(|n| txt_lossy(n, src))
        .unwrap_or_default();
    let params = params_or_empty(node, src);
    let ret_text = node
        .child_by_field_name("return_type")
        .map(|n| collapse(&txt_lossy(n, src)))
        .unwrap_or_default();
    let prefix = if has_child(node, "async") {
        "async def"
    } else {
        "def"
    };
    let mut sig = format!("{prefix} {name}{params}");
    if !ret_text.is_empty() {
        sig = format!("{sig} -> {ret_text}");
    }
    clip(&sig, SIG_LIMIT)
}

fn sig_ts(node: Node, src: &[u8]) -> String {
    let kind = node.kind();
    if kind == "class_declaration" || kind == "interface_declaration" {
        return clip(&first_line(node, src), SIG_LIMIT);
    }
    if kind == "method_definition" {
        let name = node
            .child_by_field_name("name")
            .map(|n| txt_lossy(n, src))
            .unwrap_or_default();
        return clip(&format!("{name}{}", params_or_empty(node, src)), SIG_LIMIT);
    }
    if kind == "lexical_declaration" || kind == "variable_declaration" {
        let decl = children_of(node)
            .into_iter()
            .find(|c| c.kind() == "variable_declarator");
        if let Some(decl) = decl {
            if let Some(val) = decl.child_by_field_name("value") {
                if let Some(params) = val.child_by_field_name("parameters") {
                    return clip(&up_to(decl, params, src), SIG_LIMIT);
                }
            }
        }
        return clip(&first_line(node, src), SIG_LIMIT);
    }
    let name = node
        .child_by_field_name("name")
        .map(|n| txt_lossy(n, src))
        .unwrap_or_default();
    let prefix = if has_child(node, "async") {
        "async "
    } else {
        ""
    };
    clip(
        &format!("{prefix}function {name}{}", params_or_empty(node, src)),
        SIG_LIMIT,
    )
}

fn sig_go(node: Node, src: &[u8]) -> String {
    if node.kind() == "type_spec" {
        return clip(&format!("type {}", first_line(node, src)), SIG_LIMIT);
    }
    if let Some(params) = node.child_by_field_name("parameters") {
        return clip(&up_to(node, params, src), SIG_LIMIT);
    }
    clip(&first_line(node, src), SIG_LIMIT)
}

fn sig_rs(node: Node, src: &[u8]) -> String {
    if matches!(
        node.kind(),
        "struct_item" | "enum_item" | "trait_item" | "type_item"
    ) {
        return clip(&first_line(node, src), SIG_LIMIT);
    }
    if let Some(params) = node.child_by_field_name("parameters") {
        return clip(&up_to(node, params, src), SIG_LIMIT);
    }
    clip(&first_line(node, src), SIG_LIMIT)
}

fn sig_for(lang: &str, node: Node, src: &[u8]) -> String {
    match lang {
        "py" => sig_py(node, src),
        "ts" | "js" => sig_ts(node, src),
        "go" => sig_go(node, src),
        "rs" => sig_rs(node, src),
        _ => clip(&first_line(node, src), SIG_LIMIT),
    }
}

fn doc_py(node: Node, src: &[u8]) -> String {
    let body = match node.child_by_field_name("body") {
        Some(b) if b.child_count() > 0 => b,
        _ => return String::new(),
    };
    let mut first = match body.child(0) {
        Some(c) => c,
        None => return String::new(),
    };
    if first.kind() == "expression_statement" && first.child_count() > 0 {
        if let Some(inner) = first.child(0) {
            if inner.kind() == "string" {
                first = inner;
            }
        }
    }
    if first.kind() != "string" {
        return String::new();
    }
    let content = match children_of(first)
        .into_iter()
        .find(|c| c.kind() == "string_content")
    {
        Some(c) => c,
        None => return String::new(),
    };
    let text = txt_lossy(content, src);
    let text = text.trim();
    if text.is_empty() {
        return String::new();
    }
    let first_line_text = text.lines().next().unwrap_or("").trim();
    clip(first_line_text, DOC_LIMIT)
}

/// Doc from a comment node directly above the definition (TS/JS/Go/Rust).
fn doc_comment(node: Node, src: &[u8]) -> String {
    let mut target = node;
    if let Some(parent) = target.parent() {
        if parent.kind() == "export_statement" {
            target = parent;
        }
    }
    let sib = match target.prev_named_sibling() {
        Some(s) if s.kind() == "comment" => s,
        _ => return String::new(),
    };
    // allow one blank line of slack
    if (sib.end_position().row as i64) < node.start_position().row as i64 - 2 {
        return String::new();
    }
    for line in txt_lossy(sib, src).lines() {
        let mut cleaned = line.trim().to_string();
        // `^/\*+` — a leading slash followed by one or more stars
        if cleaned.starts_with('/') {
            let stars = cleaned[1..].bytes().take_while(|&b| b == b'*').count();
            if stars > 0 {
                cleaned = cleaned[1 + stars..].to_string();
            }
        }
        // `\*/$`
        if cleaned.ends_with("*/") {
            cleaned.truncate(cleaned.len() - 2);
        }
        let cleaned = cleaned.trim_start_matches('*').trim();
        if !cleaned.is_empty() && !cleaned.starts_with('@') {
            return clip(cleaned, DOC_LIMIT);
        }
    }
    String::new()
}

// ---------------------------------------------------------------------------
// kinds
// ---------------------------------------------------------------------------

fn kind_py(node: Node) -> &'static str {
    if node.kind() == "class_definition" {
        return "class";
    }
    let mut parent = node.parent();
    while let Some(p) = parent {
        if p.kind() == "class_definition" {
            return "method";
        }
        if p.kind() == "module" {
            break;
        }
        parent = p.parent();
    }
    "def"
}

fn kind_rs(node: Node) -> &'static str {
    if matches!(node.kind(), "struct_item" | "enum_item" | "trait_item") {
        return "class";
    }
    let mut parent = node.parent();
    while let Some(p) = parent {
        if p.kind() == "impl_item" {
            return "method";
        }
        parent = p.parent();
    }
    "def"
}

fn kind_go(node: Node) -> &'static str {
    if node.kind() == "type_spec" {
        return "class";
    }
    if node.kind() == "method_declaration" {
        "method"
    } else {
        "def"
    }
}

fn kind_ts(node: Node) -> &'static str {
    match node.kind() {
        "class_declaration" | "interface_declaration" => "class",
        "method_definition" => "method",
        _ => "def",
    }
}

// ---------------------------------------------------------------------------
// import extraction (filesystem resolution where the file location suffices)
// ---------------------------------------------------------------------------

fn unquote(text: &str) -> String {
    let t = text.trim();
    if t.len() >= 2 {
        let b = t.as_bytes();
        if b[0] == b[b.len() - 1] && (b[0] == b'"' || b[0] == b'\'') {
            return t[1..t.len() - 1].to_string();
        }
    }
    t.to_string()
}

/// Repo-relative python module file for a dotted-path prefix, or "".
fn py_file(root: &Path, prefix: &str) -> String {
    for cand in [format!("{prefix}.py"), format!("{prefix}/__init__.py")] {
        if !cand.is_empty() && root.join(&cand).is_file() {
            return cand;
        }
    }
    String::new()
}

/// `app/core` → `['app/core', 'app', '']` (nearest import root first).
fn py_ancestors(rel_dir: &str) -> Vec<String> {
    let parts: Vec<&str> = if rel_dir.is_empty() {
        vec![]
    } else {
        rel_dir.split('/').collect()
    };
    (0..=parts.len())
        .rev()
        .map(|i| parts[..i].join("/"))
        .collect()
}

fn posix_join(a: &str, b: &str) -> String {
    if a.is_empty() {
        b.to_string()
    } else {
        format!("{a}/{b}")
    }
}

fn parent_dir(path: &str) -> String {
    match path.rfind('/') {
        Some(i) => path[..i].to_string(),
        None => String::new(),
    }
}

/// Repo-relative path prefix for a python module ("" when not in the repo).
///
/// `level > 0` means a relative import (1 = current package). Absolute modules
/// are searched from the file's directory up to the repo root, nearest first.
fn py_module_prefix(root: &Path, relpath: &str, level: usize, module: &str) -> String {
    let rel_dir = parent_dir(relpath);
    if level > 0 {
        let ancestors = py_ancestors(&rel_dir);
        let base = &ancestors[(level - 1).min(ancestors.len() - 1)];
        return if module.is_empty() {
            base.clone()
        } else {
            posix_join(base, &module.replace('.', "/"))
        };
    }
    let module_rel = module.replace('.', "/");
    for anc in py_ancestors(&rel_dir) {
        let prefix = posix_join(&anc, &module_rel);
        if !py_file(root, &prefix).is_empty() || root.join(&prefix).is_dir() {
            return prefix;
        }
    }
    String::new()
}

fn py_import_refs(
    root: &Path,
    relpath: &str,
    node: Node,
    src: &[u8],
) -> Vec<(String, String, u32)> {
    let line = node.start_position().row as u32 + 1;
    if node.kind() == "import_statement" {
        let mut refs = Vec::new();
        for child in children_of(node) {
            if child.kind() != "dotted_name" {
                continue;
            }
            let module = txt_lossy(child, src);
            let prefix = py_module_prefix(root, relpath, 0, &module);
            let mut target = if prefix.is_empty() {
                String::new()
            } else {
                py_file(root, &prefix)
            };
            if target.is_empty() {
                if let Some(i) = prefix.rfind('/') {
                    target = py_file(root, &prefix[..i]);
                }
            }
            refs.push((module, target, line));
        }
        return refs;
    }

    // import_from_statement
    let mut level = 0usize;
    let mut module = String::new();
    if let Some(mod_node) = node.child_by_field_name("module_name") {
        if mod_node.kind() == "relative_import" {
            let prefix_node = children_of(mod_node)
                .into_iter()
                .find(|c| c.kind() == "import_prefix");
            let dots = prefix_node
                .map(|n| txt_lossy(n, src))
                .unwrap_or_else(|| ".".into());
            level = dots.matches('.').count().max(1);
            let dn = children_of(mod_node)
                .into_iter()
                .find(|c| c.kind() == "dotted_name");
            module = dn.map(|n| txt_lossy(n, src)).unwrap_or_default();
        } else {
            module = txt_lossy(mod_node, src);
        }
    }
    // names after the `import` keyword, breadth-first through nested lists
    let mut names: Vec<String> = Vec::new();
    let mut stack: VecDeque<Node> = children_of(node).into_iter().collect();
    let mut seen_import_kw = false;
    while let Some(child) = stack.pop_front() {
        if child.kind() == "import" {
            seen_import_kw = true;
            continue;
        }
        if !seen_import_kw {
            continue;
        }
        match child.kind() {
            "dotted_name" => names.push(txt_lossy(child, src)),
            "aliased_import" => {
                if let Some(dn) = child.child_by_field_name("name") {
                    names.push(txt_lossy(dn, src));
                }
            }
            "wildcard_import" => names.push("*".to_string()),
            "parenthesized_import_list" | "import_list" => {
                for c in children_of(child).into_iter().rev() {
                    stack.push_front(c);
                }
            }
            _ => {}
        }
    }
    let prefix = py_module_prefix(root, relpath, level, &module);
    let mut refs = Vec::new();
    for name in names {
        let mut target = String::new();
        if !prefix.is_empty() {
            let sub = py_file(root, &format!("{prefix}/{}", name.replace('.', "/")));
            target = if sub.is_empty() {
                py_file(root, &prefix)
            } else {
                sub
            };
        }
        refs.push((name, target, line));
    }
    refs
}

/// POSIX `normpath`: resolve `.`/`..` segments lexically.
fn normpath(p: &str) -> String {
    let abs = p.starts_with('/');
    let mut parts: Vec<&str> = Vec::new();
    for seg in p.split('/') {
        match seg {
            "" | "." => {}
            ".." => match parts.last() {
                Some(&"..") | None => {
                    if !abs {
                        parts.push("..");
                    }
                }
                Some(_) => {
                    parts.pop();
                }
            },
            s => parts.push(s),
        }
    }
    let mut out = parts.join("/");
    if abs {
        out = format!("/{out}");
    }
    if out.is_empty() {
        out = ".".to_string();
    }
    out
}

/// Resolve a TS/JS import specifier to a repo-relative file, or "".
fn ts_resolve(root: &Path, relpath: &str, spec: &str) -> String {
    if !(spec.starts_with("./") || spec.starts_with("../") || spec.starts_with('/')) {
        return String::new();
    }
    let rel_dir = parent_dir(relpath);
    let base = spec.strip_prefix('/').unwrap_or(spec);
    let joined = normpath(&posix_join(&rel_dir, base));
    if joined.starts_with("..") {
        return String::new();
    }
    let mut candidates: Vec<String> = Vec::new();
    let basename = joined.rsplit('/').next().unwrap_or("");
    if basename.contains('.') {
        candidates.push(joined.clone());
    }
    for ext in [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"] {
        candidates.push(format!("{joined}{ext}"));
        candidates.push(format!("{joined}/index{ext}"));
    }
    for cand in candidates {
        if !cand.is_empty() && root.join(&cand).is_file() {
            return cand;
        }
    }
    String::new()
}

fn ts_module_base(spec: &str) -> String {
    let trimmed = spec.trim_end_matches('/');
    let mut base = trimmed.rsplit('/').next().unwrap_or(trimmed);
    if base.is_empty() {
        base = spec;
    }
    for ext in [".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"] {
        if let Some(stripped) = base.strip_suffix(ext) {
            return stripped.to_string();
        }
    }
    base.to_string()
}

/// Names referenced by an import/export clause (module-side names).
fn ts_clause_names(node: Node, src: &[u8]) -> Vec<String> {
    let mut names = Vec::new();
    let mut stack: VecDeque<Node> = children_of(node).into_iter().collect();
    while let Some(child) = stack.pop_front() {
        match child.kind() {
            "import_clause" | "export_clause" | "named_imports" | "named_exports" => {
                for c in children_of(child).into_iter().rev() {
                    stack.push_front(c);
                }
            }
            "import_specifier" | "export_specifier" => {
                if let Some(n) = child.child_by_field_name("name") {
                    names.push(txt_lossy(n, src));
                }
            }
            "namespace_import" => {
                let alias = children_of(child)
                    .into_iter()
                    .find(|c| c.kind() == "identifier");
                names.push(alias.map(|n| txt_lossy(n, src)).unwrap_or_default());
            }
            _ => {}
        }
    }
    names
}

fn ts_import_refs(
    root: &Path,
    relpath: &str,
    node: Node,
    src: &[u8],
) -> Vec<(String, String, u32)> {
    let src_node = match node.child_by_field_name("source") {
        Some(n) if n.kind() == "string" => n,
        _ => return vec![],
    };
    let line = node.start_position().row as u32 + 1;
    let spec = unquote(&txt_lossy(src_node, src));
    let target = ts_resolve(root, relpath, &spec);
    let mut names: Vec<String> = ts_clause_names(node, src)
        .into_iter()
        .filter(|n| !n.is_empty())
        .collect();
    if names.is_empty() {
        names.push(ts_module_base(&spec));
    }
    names
        .into_iter()
        .map(|n| (n, target.clone(), line))
        .collect()
}

/// Go import paths as unresolved raw refs (parent resolves via go.mod + file list).
fn go_import_refs(node: Node, src: &[u8]) -> Vec<(String, String, u32)> {
    let mut refs = Vec::new();
    let mut stack = vec![node];
    while let Some(n) = stack.pop() {
        if n.kind() == "import_spec" {
            if let Some(path_node) = n.child_by_field_name("path") {
                let line = n.start_position().row as u32 + 1;
                refs.push((unquote(&txt_lossy(path_node, src)), String::new(), line));
            }
        } else {
            stack.extend(children_of(n));
        }
    }
    refs
}

/// Flatten `crate::util::{alpha, beta}` into `['crate::util::alpha', ...]`.
fn rs_flatten_use(text: &str) -> Vec<String> {
    let flat = text.replace('{', "::").replace('}', "");
    // collapse runs of ':' into "::" (re.sub(r":{2,}", "::", flat))
    let mut collapsed = String::with_capacity(flat.len());
    let mut colons = 0usize;
    for c in flat.chars() {
        if c == ':' {
            colons += 1;
        } else {
            if colons > 0 {
                collapsed.push_str("::");
                colons = 0;
            }
            collapsed.push(c);
        }
    }
    if colons > 0 {
        collapsed.push_str("::");
    }
    collapsed
        .split(',')
        .map(|p| p.trim().to_string())
        .filter(|p| !p.is_empty())
        .collect()
}

/// Resolve a rust `use` path against crate/self/super conventions.
fn rs_resolve(root: &Path, relpath: &str, base_segs: &[&str], leaf: &str) -> String {
    let crate_root = if root.join("src").is_dir() { "src" } else { "" };
    let rel_dir = parent_dir(relpath);
    if base_segs.is_empty() {
        return String::new();
    }
    let head = base_segs[0];
    let prefix: String = match head {
        "crate" => {
            let mut parts: Vec<&str> = Vec::new();
            if !crate_root.is_empty() {
                parts.push(crate_root);
            }
            parts.extend_from_slice(&base_segs[1..]);
            parts.join("/")
        }
        "self" => {
            let mut parts: Vec<&str> = Vec::new();
            if !rel_dir.is_empty() {
                parts.push(&rel_dir);
            }
            parts.extend_from_slice(&base_segs[1..]);
            parts.join("/")
        }
        "super" => {
            let mut ups = 0usize;
            for seg in base_segs {
                if *seg == "super" {
                    ups += 1;
                } else {
                    break;
                }
            }
            let rest = &base_segs[ups..];
            let mut d = rel_dir.clone();
            for _ in 0..ups {
                d = parent_dir(&d);
            }
            let mut parts: Vec<String> = Vec::new();
            if !d.is_empty() {
                parts.push(d);
            }
            parts.extend(rest.iter().map(|s| (*s).to_string()));
            parts.join("/")
        }
        _ => return String::new(), // external crate — cannot map into the repo
    };
    let candidates: Vec<String> = if leaf.is_empty() || leaf == "*" {
        vec![format!("{prefix}.rs"), format!("{prefix}/mod.rs")]
    } else {
        vec![
            format!("{prefix}/{leaf}.rs"),
            format!("{prefix}/{leaf}/mod.rs"),
            format!("{prefix}.rs"),
            format!("{prefix}/mod.rs"),
        ]
    };
    for cand in candidates {
        if !cand.is_empty() && root.join(&cand).is_file() {
            return cand;
        }
    }
    String::new()
}

fn rs_use_refs(root: &Path, relpath: &str, node: Node, src: &[u8]) -> Vec<(String, String, u32)> {
    let line = node.start_position().row as u32 + 1;
    let arg = match children_of(node).into_iter().find(|c| c.kind() != "use") {
        Some(a) => a,
        None => return vec![],
    };
    let mut refs = Vec::new();
    for path in rs_flatten_use(&txt_lossy(arg, src)) {
        let path = strip_as_alias(&path);
        let segs: Vec<&str> = path.split("::").filter(|s| !s.is_empty()).collect();
        if segs.is_empty() {
            continue;
        }
        let leaf = segs[segs.len() - 1];
        let target = rs_resolve(root, relpath, &segs[..segs.len() - 1], leaf);
        refs.push((leaf.to_string(), target, line));
    }
    refs
}

/// `re.sub(r"\s+as\s+\w+$", "", path).strip()`
fn strip_as_alias(path: &str) -> String {
    static AS_RE: OnceLock<Option<regex::Regex>> = OnceLock::new();
    let re = AS_RE.get_or_init(|| regex::Regex::new(r"\s+as\s+\w+$").ok());
    match re.as_ref() {
        Some(r) => r.replace(path, "").trim().to_string(),
        None => path.trim().to_string(),
    }
}

fn import_refs(
    root: &Path,
    relpath: &str,
    lang: &str,
    node: Node,
    src: &[u8],
) -> Vec<(String, String, u32)> {
    match lang {
        "py" => py_import_refs(root, relpath, node, src),
        "ts" | "js" => ts_import_refs(root, relpath, node, src),
        "go" => go_import_refs(node, src),
        "rs" => rs_use_refs(root, relpath, node, src),
        _ => vec![],
    }
}

// ---------------------------------------------------------------------------
// regex fallback extractor (broken parses, unsupported languages)
// ---------------------------------------------------------------------------

struct Fallback {
    regex: regex::Regex,
    kind: &'static str, // "def" | "class" | "auto_def" | "auto_class" | "go_func" | "c_func"
}

fn fb(pattern: String, kind: &'static str) -> Option<Fallback> {
    // A malformed pattern must degrade that one extractor, never crash.
    regex::Regex::new(&pattern)
        .ok()
        .map(|regex| Fallback { regex, kind })
}

const C_IDENT: &str = r"[A-Za-z_$][A-Za-z0-9_$]*";

fn fallbacks() -> &'static HashMap<&'static str, Vec<Fallback>> {
    static FALLBACKS: OnceLock<HashMap<&'static str, Vec<Fallback>>> = OnceLock::new();
    FALLBACKS.get_or_init(|| {
        let mut map: HashMap<&'static str, Vec<Fallback>> = HashMap::new();
        map.insert(
            "py",
            vec![
                fb(
                    format!(r"(?m)^[ \t]*(?:async[ \t]+)?def[ \t]+({C_IDENT})"),
                    "auto_def",
                ),
                fb(format!(r"(?m)^[ \t]*class[ \t]+({C_IDENT})"), "auto_class"),
            ]
            .into_iter()
            .flatten()
            .collect(),
        );
        map.insert(
            "ts",
            vec![
                fb(
                    format!(
                        r"(?m)^[ \t]*(?:export[ \t]+)?(?:default[ \t]+)?(?:async[ \t]+)?function[ \t]*\*?[ \t]*({C_IDENT})"
                    ),
                    "def",
                ),
                fb(
                    format!(
                        r"(?m)^[ \t]*(?:export[ \t]+)?(?:default[ \t]+)?(?:abstract[ \t]+)?class[ \t]+({C_IDENT})"
                    ),
                    "class",
                ),
                fb(
                    format!(
                        r"(?m)^[ \t]*(?:export[ \t]+)?(?:const|let|var)[ \t]+({C_IDENT})[ \t]*=[ \t]*(?:async[ \t]*)?(?:function\b|\([^)]*\)[ \t]*=>|[A-Za-z_$][\w$]*[ \t]*=>)"
                    ),
                    "def",
                ),
            ]
            .into_iter()
            .flatten()
            .collect(),
        );
        map.insert(
            "go",
            vec![
                fb(
                    r"(?m)^func(?:[ \t]+\([^)]+\))?[ \t]+([A-Za-z_]\w*)".to_string(),
                    "go_func",
                ),
                fb(
                    r"(?m)^type[ \t]+([A-Za-z_]\w*)[ \t]+(?:struct|interface)\b".to_string(),
                    "class",
                ),
            ]
            .into_iter()
            .flatten()
            .collect(),
        );
        map.insert(
            "rs",
            vec![
                fb(
                    r"(?m)^[ \t]*(?:pub(?:\([^)]*\))?[ \t]+)?fn[ \t]+([A-Za-z_]\w*)".to_string(),
                    "def",
                ),
                fb(
                    r"(?m)^[ \t]*(?:pub(?:\([^)]*\))?[ \t]+)?(?:struct|enum|trait)[ \t]+([A-Za-z_]\w*)".to_string(),
                    "class",
                ),
            ]
            .into_iter()
            .flatten()
            .collect(),
        );
        map.insert(
            "c",
            vec![
                fb(
                    r"(?m)^[ \t]*(?:[A-Za-z_][\w \t*]*?[ \t*])?([A-Za-z_]\w*)[ \t]*\([^;{}()]*\)[ \t]*\{".to_string(),
                    "c_func",
                ),
                fb(
                    r"(?m)^[ \t]*(?:typedef[ \t]+)?struct[ \t]+([A-Za-z_]\w*)".to_string(),
                    "class",
                ),
            ]
            .into_iter()
            .flatten()
            .collect(),
        );
        map
    })
}

const C_KEYWORDS: &[&str] = &[
    "if", "for", "while", "switch", "return", "else", "do", "sizeof", "goto", "case",
];

/// Python block end: last line before the next non-blank line at column <= indent.
fn fallback_end_line(lines: &[&str], idx: usize) -> usize {
    let Some(line) = lines.get(idx) else {
        return idx + 1;
    };
    let indent = line.len() - line.trim_start_matches([' ', '\t']).len();
    let mut j = idx + 1;
    while j < lines.len() {
        let stripped = lines[j].trim();
        if !stripped.is_empty() {
            let cur = lines[j].len() - lines[j].trim_start_matches([' ', '\t']).len();
            if cur <= indent {
                return j; // next block starts at line j+1 (1-based); block ends at j
            }
        }
        j += 1;
    }
    lines.len()
}

fn regex_symbols(src: &[u8], lang: &str, relpath: &str) -> Vec<Symbol> {
    let fallbacks = match fallbacks().get(lang) {
        Some(f) => f,
        None => return vec![],
    };
    let text = String::from_utf8_lossy(src).into_owned();
    let lines: Vec<&str> = text.lines().collect();
    let mut symbols: Vec<Symbol> = Vec::new();
    for fbk in fallbacks {
        for m in fbk.regex.captures_iter(&text) {
            let Some(whole) = m.get(0) else { continue };
            let Some(name_m) = m.get(1) else { continue };
            let name = &text[name_m.start()..name_m.end()];
            if name.is_empty() || C_KEYWORDS.contains(&name) {
                continue;
            }
            let line = text[..name_m.start()].matches('\n').count() + 1;
            let raw = &text[whole.start()..whole.end()];
            let kind: &str = match fbk.kind {
                "auto_def" | "auto_class" => {
                    let indent_len = raw.len() - raw.trim_start_matches([' ', '\t']).len();
                    if indent_len > 0 {
                        "method"
                    } else {
                        fbk.kind.trim_start_matches("auto_")
                    }
                }
                "go_func" => {
                    // re.match(r"func[ \t]+\(", raw)
                    match raw.get(4..) {
                        Some(after) => {
                            let ws = after.trim_start_matches([' ', '\t']);
                            if ws.len() < after.len() && ws.starts_with('(') {
                                "method"
                            } else {
                                "def"
                            }
                        }
                        None => "def",
                    }
                }
                "c_func" => "def",
                other => other,
            };
            let mut end_line = line;
            if lang == "py" {
                end_line = fallback_end_line(&lines, line - 1);
            }
            let sig = clip(
                collapse(raw).trim_end_matches([' ', '{', ';', ':']),
                SIG_LIMIT,
            );
            symbols.push(Symbol {
                file: relpath.to_string(),
                name: name.to_string(),
                kind: kind.to_string(),
                line: line as u32,
                end_line: line.max(end_line) as u32,
                signature: sig,
                doc: String::new(),
            });
        }
    }
    symbols.sort_by(|a, b| (a.line, a.name.as_str()).cmp(&(b.line, b.name.as_str())));
    symbols
}

// ---------------------------------------------------------------------------
// per-file extraction
// ---------------------------------------------------------------------------

fn extract_symbols(
    lang: &str,
    q: &LangQueries,
    root_node: Node,
    src: &[u8],
    relpath: &str,
) -> Vec<Symbol> {
    let Some(query) = &q.def else {
        return vec![];
    };
    let mut symbols: Vec<Symbol> = Vec::new();
    let mut cursor = QueryCursor::new();
    let mut captures = cursor.captures(query, root_node, src);
    while let Some((m, cap_idx)) = captures.next() {
        let node = m.captures[*cap_idx].node;
        let mut name_node = node.child_by_field_name("name");
        if name_node.is_none()
            && (node.kind() == "lexical_declaration" || node.kind() == "variable_declaration")
        {
            let decl = children_of(node)
                .into_iter()
                .find(|c| c.kind() == "variable_declarator");
            name_node = decl.and_then(|d| d.child_by_field_name("name"));
        }
        let Some(name_node) = name_node else { continue };
        if lang == "go" && node.kind() == "type_spec" {
            let is_anchor = children_of(node)
                .into_iter()
                .any(|c| c.kind() == "struct_type" || c.kind() == "interface_type");
            if !is_anchor {
                continue; // plain alias `type X = Y` — not a definition anchor
            }
        }
        let name = txt_lossy(name_node, src);
        if name.is_empty() {
            continue;
        }
        let kind = match lang {
            "py" => kind_py(node).to_string(),
            "ts" | "js" => kind_ts(node).to_string(),
            "rs" => kind_rs(node).to_string(),
            _ => kind_go(node).to_string(),
        };
        let doc = if lang == "py" {
            doc_py(node, src)
        } else {
            doc_comment(node, src)
        };
        symbols.push(Symbol {
            file: relpath.to_string(),
            name,
            kind,
            line: node.start_position().row as u32 + 1,
            end_line: node.end_position().row as u32 + 1,
            signature: sig_for(lang, node, src),
            doc,
        });
    }
    symbols
}

fn is_plausible_name(name: &str) -> bool {
    let n = name.chars().count();
    if !(2..=80).contains(&n) {
        return false;
    }
    let mut chars = name.chars();
    let first = match chars.next() {
        Some(c) => c,
        None => return false,
    };
    if !(first.is_ascii_alphabetic() || first == '_' || first == '$') {
        return false;
    }
    chars.all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '$')
}

fn extract_refs(
    root: &Path,
    relpath: &str,
    lang: &str,
    q: &LangQueries,
    root_node: Node,
    src: &[u8],
) -> Vec<Ref> {
    // Import refs first (highest value), then identifier occurrences, capped.
    let mut raws: Vec<(String, String, u32)> = Vec::new();
    if let Some(imp) = &q.imp {
        let mut cursor = QueryCursor::new();
        let mut captures = cursor.captures(imp, root_node, src);
        'outer: while let Some((m, cap_idx)) = captures.next() {
            let node = m.captures[*cap_idx].node;
            for raw in import_refs(root, relpath, lang, node, src) {
                raws.push(raw);
                if raws.len() >= MAX_REFS_PER_FILE {
                    break 'outer;
                }
            }
        }
    }
    if let Some(idq) = &q.id {
        if raws.len() < MAX_REFS_PER_FILE {
            let mut cursor = QueryCursor::new();
            let mut captures = cursor.captures(idq, root_node, src);
            while let Some((m, cap_idx)) = captures.next() {
                let node = m.captures[*cap_idx].node;
                let name = txt_lossy(node, src);
                if !is_plausible_name(&name) {
                    continue;
                }
                raws.push((name, String::new(), node.start_position().row as u32 + 1));
                if raws.len() >= MAX_REFS_PER_FILE {
                    break;
                }
            }
        }
    }
    let mut out: Vec<Ref> = Vec::new();
    let mut seen: HashSet<(String, String, u32)> = HashSet::new();
    for (dst_name, dst_file, line) in raws {
        let key = (dst_name.clone(), dst_file.clone(), line);
        if seen.insert(key) {
            out.push(Ref {
                src_file: relpath.to_string(),
                dst_name,
                dst_file,
                line,
            });
        }
    }
    out
}

/// Parse one file: tree-sitter extraction, unioned with regex fallback on errors.
fn parse_one(root: &Path, file: &ScannedFile) -> (String, Vec<Symbol>, Vec<Ref>) {
    let relpath = file.rel.as_str();
    let lang = file.lang.as_str();
    let src = match std::fs::read(&file.abs) {
        Ok(b) => b,
        Err(_) => return (lang.to_string(), vec![], vec![]),
    };
    let mut symbols: Vec<Symbol> = Vec::new();
    let mut refs: Vec<Ref> = Vec::new();
    let mut parsed_ok = false;
    let mut has_error = false;
    let grammar = grammar_for(lang, file.rel.ends_with(".tsx"));
    if let (Some(g), Some(q)) = (grammar, queries().get(lang)) {
        if let Some(tree) = parse_with(g, &src) {
            has_error = tree.root_node().has_error();
            let root_node = tree.root_node();
            symbols = extract_symbols(lang, q, root_node, &src, relpath);
            refs = extract_refs(root, relpath, lang, q, root_node, &src);
            parsed_ok = true;
        }
    }
    if !parsed_ok || has_error {
        let known: HashSet<(String, u32)> =
            symbols.iter().map(|s| (s.name.clone(), s.line)).collect();
        let spans: Vec<(u32, u32)> = symbols.iter().map(|s| (s.line, s.end_line)).collect();
        for sym in regex_symbols(&src, lang, relpath) {
            if known.contains(&(sym.name.clone(), sym.line)) {
                continue;
            }
            if spans.iter().any(|&(a, b)| a <= sym.line && sym.line <= b) {
                continue;
            }
            symbols.push(sym);
        }
        crate::model::sort_symbols(&mut symbols);
    }
    (lang.to_string(), symbols, refs)
}

/// Parse `files` in parallel; returns relpath → (lang, symbols, refs).
///
/// Uses rayon for large file sets and sequential parsing for small inputs. The
/// per-file result is deterministic regardless of scheduling.
pub fn parse_files(
    root: &str,
    files: &[ScannedFile],
    workers: usize,
) -> HashMap<String, (String, Vec<Symbol>, Vec<Ref>)> {
    if files.is_empty() {
        return HashMap::new();
    }
    let root_path = std::path::PathBuf::from(root);
    let parse_chunk = |chunk: &[ScannedFile]| -> Vec<(String, String, Vec<Symbol>, Vec<Ref>)> {
        chunk
            .iter()
            .map(|f| {
                let (lang, symbols, refs) = parse_one(&root_path, f);
                (f.rel.clone(), lang, symbols, refs)
            })
            .collect()
    };

    let mut results: HashMap<String, (String, Vec<Symbol>, Vec<Ref>)> = HashMap::new();
    if files.len() <= INPROC_THRESHOLD {
        for chunk in files.chunks(CHUNK_SIZE) {
            for (rel, lang, symbols, refs) in parse_chunk(chunk) {
                results.insert(rel, (lang, symbols, refs));
            }
        }
        return results;
    }

    // Parallel path: explicit worker count when requested, rayon default otherwise.
    let chunks: Vec<&[ScannedFile]> = files.chunks(CHUNK_SIZE).collect();
    type ChunkResult = Vec<(String, String, Vec<Symbol>, Vec<Ref>)>;
    let parsed: Vec<ChunkResult> = if let Ok(pool) = rayon::ThreadPoolBuilder::new()
        .num_threads(workers.clamp(1, 32))
        .build()
    {
        pool.install(|| chunks.par_iter().map(|c| parse_chunk(c)).collect())
    } else {
        chunks.iter().map(|c| parse_chunk(c)).collect()
    };
    for chunk_result in parsed {
        for (rel, lang, symbols, refs) in chunk_result {
            results.insert(rel, (lang, symbols, refs));
        }
    }
    results
}

// ---------------------------------------------------------------------------
// parent-side reference resolution
// ---------------------------------------------------------------------------

/// Heuristic: dotted / path-shaped / rust-path names survive when unresolved.
fn looks_like_module_path(name: &str) -> bool {
    if name.chars().count() <= 1 || name.chars().any(|c| c.is_whitespace()) {
        return false;
    }
    name.contains('.') || name.contains("::") || name.contains('/')
}

/// Deterministic candidate: same directory first, then lexicographic path.
fn pick_candidate(candidates: &[String], src_file: &str, src_dir: &str) -> String {
    let mut best = String::new();
    let mut best_key: Option<(u8, &str)> = None;
    for cand in candidates {
        let cand = cand.as_str();
        if cand == src_file {
            continue;
        }
        let key = (if parent_dir(cand) == src_dir { 0u8 } else { 1 }, cand);
        if best_key.is_none_or(|bk| key < bk) {
            best = cand.to_string();
            best_key = Some(key);
        }
    }
    best
}

/// Map every directory containing .go files to its sorted list of .go files.
fn go_dirs(paths: &HashSet<String>) -> HashMap<String, Vec<String>> {
    let mut dirs: HashMap<String, Vec<String>> = HashMap::new();
    for p in paths {
        if p.ends_with(".go") {
            dirs.entry(parent_dir(p)).or_default().push(p.clone());
        }
    }
    for list in dirs.values_mut() {
        list.sort();
    }
    dirs
}

/// Map a Go import path onto the repo files of the target package directory.
fn resolve_go_import(
    import_path: &str,
    go_module: &str,
    go_dir_map: &HashMap<String, Vec<String>>,
) -> Vec<String> {
    let mut candidates: Vec<String> = Vec::new();
    if !go_module.is_empty() {
        if let Some(stripped) = import_path.strip_prefix(&format!("{go_module}/")) {
            candidates.push(stripped.to_string());
        }
    }
    let parts: Vec<&str> = import_path.split('/').collect();
    if parts.len() >= 2 {
        candidates.push(parts[parts.len() - 2..].join("/"));
    }
    candidates.push(import_path.to_string());
    for cand in &candidates {
        if let Some(files) = go_dir_map.get(cand) {
            return files.clone();
        }
    }
    for cand in &candidates {
        let mut matches: Vec<&String> = go_dir_map
            .keys()
            .filter(|d| d.ends_with(&format!("/{cand}")))
            .collect();
        matches.sort();
        if let Some(best) = matches.into_iter().min_by_key(|d| (d.len(), *d)) {
            return go_dir_map[best].clone();
        }
    }
    vec![]
}

/// Resolve unresolved refs and return the deterministic global ref list.
///
/// - Already-resolved refs are kept when their target still exists in `all_paths`.
/// - Unresolved refs are matched against `name_index` (defined symbol names).
/// - Module-path-shaped leftovers (external `os.path`, rust crate paths, go
///   packages that map nowhere) are preserved with `dst_file=""`; other misses
///   are dropped as noise.
/// - Self-file edges are dropped; output sorted by `(src, line, name, dst)`.
pub fn resolve_refs(
    refs_by_file: &HashMap<String, Vec<Ref>>,
    name_index: &HashMap<String, Vec<String>>,
    all_paths: &HashSet<String>,
    lang_of: &HashMap<String, String>,
    go_module: &str,
) -> Vec<Ref> {
    fn emit(
        seen: &mut HashSet<(String, String, String, u32)>,
        out: &mut Vec<Ref>,
        src: &str,
        name: &str,
        dst: &str,
        line: u32,
    ) {
        let key = (src.to_string(), name.to_string(), dst.to_string(), line);
        if seen.insert(key) {
            out.push(Ref {
                src_file: src.to_string(),
                dst_name: name.to_string(),
                dst_file: dst.to_string(),
                line,
            });
        }
    }

    let path_set = all_paths;
    let go_dir_map = go_dirs(path_set);
    let mut out: Vec<Ref> = Vec::new();
    let mut seen: HashSet<(String, String, String, u32)> = HashSet::new();

    let mut src_files: Vec<&String> = refs_by_file.keys().collect();
    src_files.sort();
    for src_file in src_files {
        let src_dir = parent_dir(src_file);
        for r in &refs_by_file[src_file] {
            if !r.dst_file.is_empty() {
                if r.dst_file == *src_file || !path_set.contains(&r.dst_file) {
                    continue;
                }
                emit(
                    &mut seen,
                    &mut out,
                    src_file,
                    &r.dst_name,
                    &r.dst_file,
                    r.line,
                );
                continue;
            }
            let mut targets: Vec<String> = Vec::new();
            if lang_of.get(src_file).map(|s| s.as_str()) == Some("go") {
                targets = resolve_go_import(&r.dst_name, go_module, &go_dir_map)
                    .into_iter()
                    .filter(|t| *t != *src_file && path_set.contains(t))
                    .collect();
            } else if let Some(cands) = name_index.get(&r.dst_name) {
                let pick = pick_candidate(cands, src_file, &src_dir);
                if !pick.is_empty() && path_set.contains(&pick) {
                    targets = vec![pick];
                }
            }
            if !targets.is_empty() {
                for target in targets {
                    emit(&mut seen, &mut out, src_file, &r.dst_name, &target, r.line);
                }
            } else if looks_like_module_path(&r.dst_name) {
                emit(&mut seen, &mut out, src_file, &r.dst_name, "", r.line);
            }
        }
    }
    out.sort_by(|a, b| {
        (
            a.src_file.as_str(),
            a.line,
            a.dst_name.as_str(),
            a.dst_file.as_str(),
        )
            .cmp(&(
                b.src_file.as_str(),
                b.line,
                b.dst_name.as_str(),
                b.dst_file.as_str(),
            ))
    });
    out
}

/// Read the module path declared in `<root>/go.mod` ("" when absent).
pub fn read_go_module(root: &str) -> String {
    static MODULE: OnceLock<Option<regex::Regex>> = OnceLock::new();
    let text = match std::fs::read_to_string(Path::new(root).join("go.mod")) {
        Ok(t) => t,
        Err(_) => return String::new(),
    };
    let re = MODULE.get_or_init(|| regex::Regex::new(r"(?m)^\s*module\s+(\S+)\s*$").ok());
    match re.as_ref() {
        Some(r) => r
            .captures(&text)
            .and_then(|c| c.get(1))
            .map(|m| m.as_str().to_string())
            .unwrap_or_default(),
        None => String::new(),
    }
}
