//! trailmark-core: the Rust port of the Python reference implementation's core
//! (scanner, tree-sitter parser, PageRank graph, SQLite/FTS5 cache, AGENTS.md
//! block emitter) plus the build/update orchestration.

pub mod cache;
pub mod emit;
pub mod fmt;
pub mod graph;
pub mod index;
pub mod model;
pub mod parser;
pub mod scanner;

pub use cache::{Hit, IndexCache, TrailmarkError};
pub use emit::{read_block, remove_block, render, write_block, AGENTS_FILENAME};
pub use fmt::{byte_len, estimate_tokens, py_splitlines, truncate, wrap, wrap_default};
pub use graph::{pagerank, pagerank_pair};
pub use index::{build, norm_langs, update};
pub use model::{FileEntry, IndexData, Ref, Symbol, BLOCK_END, BLOCK_START, DEFAULT_BUDGET_BYTES};
pub use parser::{parse_files, read_go_module, resolve_refs, MAX_REFS_PER_FILE};
pub use scanner::{scan, ScannedFile, DEFAULT_LANGS};

/// Languages indexed when the caller does not restrict them, as owned strings.
pub fn default_langs() -> Vec<String> {
    DEFAULT_LANGS.iter().map(|s| s.to_string()).collect()
}
