//! Index subsystem (port of `index/__init__.py`): scan → parse → resolve → persist.
//!
//! Public API (SPEC module signatures):
//! - [`build`]  — full index of the repository tree.
//! - [`update`] — mtime+size incremental index: unchanged files are reused from
//!   the cache without re-reading them, deleted files are dropped, changed/new
//!   files are reparsed.

use std::collections::{BTreeMap, HashMap, HashSet};

use crate::cache::IndexCache;
use crate::model::{sort_symbols, FileEntry, IndexData, Ref, Symbol};
use crate::parser::{parse_files, read_go_module, resolve_refs};
use crate::scanner::{self, ScannedFile};

/// Normalize a language list (default: all default languages).
pub fn norm_langs(langs: Option<&[String]>) -> Vec<String> {
    match langs {
        None => scanner::DEFAULT_LANGS.iter().map(|s| s.to_string()).collect(),
        Some(list) => {
            let mut out: Vec<String> = list
                .iter()
                .map(|l| l.trim().to_lowercase())
                .filter(|l| !l.is_empty())
                .collect();
            out.sort();
            out.dedup();
            out
        }
    }
}

/// Map every defined symbol name to the sorted list of files defining it.
fn name_index(files: &BTreeMap<String, FileEntry>) -> HashMap<String, Vec<String>> {
    let mut idx: HashMap<String, std::collections::BTreeSet<String>> = HashMap::new();
    for entry in files.values() {
        for sym in &entry.symbols {
            idx.entry(sym.name.clone())
                .or_default()
                .insert(entry.path.clone());
        }
    }
    idx.into_iter()
        .map(|(k, v)| (k, v.into_iter().collect()))
        .collect()
}

/// Build a complete IndexData from scanned files plus parse results.
fn assemble(
    root_str: &str,
    scanned: &[ScannedFile],
    parses: &HashMap<String, (String, Vec<Symbol>, Vec<Ref>)>,
) -> IndexData {
    let mut files: BTreeMap<String, FileEntry> = BTreeMap::new();
    let mut refs_by_file: HashMap<String, Vec<Ref>> = HashMap::new();
    let mut lang_of: HashMap<String, String> = HashMap::new();
    for f in scanned {
        let (_p_lang, mut symbols, refs) = parses
            .get(&f.rel)
            .cloned()
            .unwrap_or_else(|| (f.lang.clone(), Vec::new(), Vec::new()));
        sort_symbols(&mut symbols);
        files.insert(
            f.rel.clone(),
            FileEntry {
                path: f.rel.clone(),
                lang: f.lang.clone(),
                mtime: f.mtime,
                size: f.size,
                symbols,
            },
        );
        refs_by_file.insert(f.rel.clone(), refs);
        lang_of.insert(f.rel.clone(), f.lang.clone());
    }
    let refs = resolve_refs(
        &refs_by_file,
        &name_index(&files),
        &files.keys().cloned().collect::<HashSet<_>>(),
        &lang_of,
        &read_go_module(root_str),
    );
    IndexData {
        root: root_str.to_string(),
        files,
        refs,
    }
}

/// Fully index the repository at `root` and persist to `cache` when given.
///
/// * `langs` — languages to index (None → all defaults).
/// * `workers` — parser worker count; 0 → rayon default.
/// * `cache` — optional [`IndexCache`] the result is saved to.
///
/// Returns the complete IndexData (files sorted by path, refs sorted).
pub fn build(
    root: &str,
    langs: Option<&[String]>,
    workers: usize,
    cache: Option<&IndexCache>,
) -> IndexData {
    let root_str = scanner::resolve_root(root);
    let langs_list = norm_langs(langs);
    let scanned = scanner::scan(&root_str, &langs_list);
    let parses = parse_files(&root_str, &scanned, workers);
    let data = assemble(&root_str, &scanned, &parses);
    if let Some(cache) = cache {
        if let Err(e) = cache.save(&data) {
            eprintln!("codegraph: cache save failed: {e}");
        }
    }
    data
}

/// Incrementally index `root`: reuse unchanged files, reparse only deltas.
///
/// A file is reprocessed when it is new, or its `(mtime, size, lang)` differs
/// from the cached entry; files absent from the current scan are dropped. When
/// no usable cache exists this falls back to a full [`build`].
pub fn update(
    root: &str,
    langs: Option<&[String]>,
    workers: usize,
    cache: Option<&IndexCache>,
) -> IndexData {
    let root_str = scanner::resolve_root(root);
    let langs_list = norm_langs(langs);
    let Some(cache) = cache else {
        return build(&root_str, Some(&langs_list), workers, None);
    };
    let cached = match cache.load() {
        Some(c) if c.root == root_str => c,
        _ => {
            return build(&root_str, Some(&langs_list), workers, Some(cache));
        }
    };

    let scanned = scanner::scan(&root_str, &langs_list);
    let scanned_rels: HashSet<&str> = scanned.iter().map(|f| f.rel.as_str()).collect();

    let mut changed: Vec<&ScannedFile> = Vec::new();
    let mut keep: BTreeMap<String, FileEntry> = BTreeMap::new();
    for f in &scanned {
        match cached.files.get(&f.rel) {
            Some(old) if old.mtime == f.mtime && old.size == f.size && old.lang == f.lang => {
                keep.insert(f.rel.clone(), old.clone());
            }
            _ => changed.push(f),
        }
    }
    let removed: Vec<String> = cached
        .files
        .keys()
        .filter(|rel| !scanned_rels.contains(rel.as_str()))
        .cloned()
        .collect();

    let changed_owned: Vec<ScannedFile> = changed.iter().map(|f| (*f).clone()).collect();
    let parses = parse_files(&root_str, &changed_owned, workers);

    let mut new_files: BTreeMap<String, FileEntry> = BTreeMap::new();
    let mut new_refs: HashMap<String, Vec<Ref>> = HashMap::new();
    for f in &changed_owned {
        let (_p_lang, mut symbols, refs) = parses
            .get(&f.rel)
            .cloned()
            .unwrap_or_else(|| (f.lang.clone(), Vec::new(), Vec::new()));
        sort_symbols(&mut symbols);
        new_files.insert(
            f.rel.clone(),
            FileEntry {
                path: f.rel.clone(),
                lang: f.lang.clone(),
                mtime: f.mtime,
                size: f.size,
                symbols,
            },
        );
        new_refs.insert(f.rel.clone(), refs);
    }

    let mut files = keep;
    for (k, v) in &new_files {
        files.insert(k.clone(), v.clone());
    }

    // Cached refs survive for unchanged files; stale targets (deleted/renamed)
    // are filtered, unresolved module-path refs are re-matched against the
    // fresh index.
    let mut cached_refs: HashMap<String, Vec<Ref>> = HashMap::new();
    for r in &cached.refs {
        if files.contains_key(&r.src_file) && !new_files.contains_key(&r.src_file) {
            cached_refs
                .entry(r.src_file.clone())
                .or_default()
                .push(r.clone());
        }
    }
    for (k, v) in &new_refs {
        cached_refs.insert(k.clone(), v.clone());
    }
    let refs = resolve_refs(
        &cached_refs,
        &name_index(&files),
        &files.keys().cloned().collect::<HashSet<_>>(),
        &files
            .iter()
            .map(|(k, v)| (k.clone(), v.lang.clone()))
            .collect(),
        &read_go_module(&root_str),
    );

    // Persist the delta: removed + changed files are deleted, then the changed
    // files are re-inserted with their (freshly parsed) refs.
    if !removed.is_empty() || !new_files.is_empty() {
        let mut paths: Vec<String> = removed;
        paths.extend(new_files.keys().cloned());
        if let Err(e) = cache.remove_files(&paths) {
            eprintln!("codegraph: cache removal failed: {e}");
        }
    }
    if !new_files.is_empty() {
        let entries: Vec<FileEntry> = new_files.values().cloned().collect();
        let mut flat_refs: Vec<Ref> = Vec::new();
        for rel in new_files.keys() {
            if let Some(list) = new_refs.get(rel) {
                flat_refs.extend(list.iter().cloned());
            }
        }
        if let Err(e) = cache.upsert_files(&entries, &flat_refs) {
            eprintln!("codegraph: cache upsert failed: {e}");
        }
    }

    IndexData {
        root: root_str,
        files,
        refs,
    }
}
