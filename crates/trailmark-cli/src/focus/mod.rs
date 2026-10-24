//! FOCUS subsystem (port of `focus/__init__.py`): (IndexData, issue) → ranked
//! leads → PLAN section + outlines.

pub mod outlines;
pub mod plan;
pub mod ranker;

pub use outlines::{generate_for, generate_top, outline_posix, outline_text};
pub use plan::render_plan;
pub use ranker::{issue_tokens, rank, rank_and_prior, Lead};

/// G-imp4: contracts+snippets need headroom — each contracted lead displaces an
/// early full-file read (the highest replay-damage content in a run).
///
/// Port of `focus.cli_focus.effective_budget_for`:
/// `max(budget, 2048 + 2200 * min(3, n_leads))` when leads exist, else the
/// budget unchanged.
pub fn effective_budget_for(budget: usize, n_leads: usize) -> usize {
    if n_leads == 0 {
        budget
    } else {
        budget.max(2048 + 2200 * n_leads.min(3))
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn effective_budget_matches_python_formula() {
        use super::effective_budget_for;
        assert_eq!(effective_budget_for(2048, 0), 2048);
        assert_eq!(effective_budget_for(2048, 1), 4248);
        assert_eq!(effective_budget_for(2048, 2), 6448);
        assert_eq!(effective_budget_for(2048, 3), 8648);
        assert_eq!(effective_budget_for(2048, 5), 8648); // min(3, 5)
        assert_eq!(effective_budget_for(10_000, 3), 10_000); // max()
    }
}
