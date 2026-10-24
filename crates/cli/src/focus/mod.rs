//! FOCUS subsystem (port of `focus/__init__.py`): (IndexData, issue) → ranked
//! leads → PLAN section + outlines.

pub mod outlines;
pub mod plan;
pub mod ranker;

pub use outlines::{generate_for, generate_top, outline_posix, outline_text};
pub use plan::render_plan;
pub use ranker::{issue_tokens, rank, Lead};
