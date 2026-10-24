//! Hash-stable marked-block writer for AGENTS.md (port of `emit/agents_md.py`).
//!
//! The managed block sits between [`BLOCK_START`]/[`BLOCK_END`] markers. Content
//! outside the markers is never modified. Writes are no-ops when the body is
//! unchanged (cache-friendliness).

use std::fs;
use std::io;
use std::path::Path;

use crate::model::{BLOCK_END, BLOCK_START};

/// The AGENTS.md file name.
pub const AGENTS_FILENAME: &str = "AGENTS.md";

/// Render the full marked block (markers + body).
pub fn render(body: &str) -> String {
    format!("{BLOCK_START}\n{}\n{BLOCK_END}", body.trim_end())
}

/// Return the current block body (without markers) or `None` if absent.
pub fn read_block(path: &Path) -> Option<String> {
    let text = fs::read_to_string(path).ok()?;
    let start = text.find(BLOCK_START)?;
    let end = text.find(BLOCK_END)?;
    if end < start {
        return None;
    }
    let body = &text[start + BLOCK_START.len()..end];
    Some(body.trim_matches('\n').to_string())
}

/// Insert/replace the managed block at the top of `path`. Returns true if changed.
///
/// - Creates the file (and parents) if missing.
/// - Idempotent: writing an identical body is a no-op.
pub fn write_block(path: &Path, body: &str) -> io::Result<bool> {
    let block = render(body);
    let existing = match fs::read_to_string(path) {
        Ok(text) => text,
        Err(e) if e.kind() == io::ErrorKind::NotFound => {
            if let Some(parent) = path.parent() {
                fs::create_dir_all(parent)?;
            }
            fs::write(path, format!("{block}\n"))?;
            return Ok(true);
        }
        Err(e) => return Err(e),
    };

    let new_text = match (existing.find(BLOCK_START), existing.find(BLOCK_END)) {
        (Some(start), Some(end)) if end >= start => {
            let mut out = String::with_capacity(existing.len() + block.len());
            out.push_str(&existing[..start]);
            out.push_str(&block);
            out.push_str(&existing[end + BLOCK_END.len()..]);
            out
        }
        _ => format!("{block}\n\n{existing}"),
    };
    if new_text == existing {
        return Ok(false);
    }
    fs::write(path, new_text)?;
    Ok(true)
}

/// Strip the managed block from `path`. Returns true if the file changed.
pub fn remove_block(path: &Path) -> io::Result<bool> {
    let existing = match fs::read_to_string(path) {
        Ok(text) => text,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(false),
        Err(e) => return Err(e),
    };
    let (start, end) = match (existing.find(BLOCK_START), existing.find(BLOCK_END)) {
        (Some(s), Some(e)) if e >= s => (s, e),
        _ => return Ok(false),
    };
    let new_text = existing[..start].to_string() + &existing[end + BLOCK_END.len()..];
    let new_text = new_text.trim_start_matches('\n');
    if !new_text.trim().is_empty() {
        fs::write(path, new_text)?;
    } else {
        fs::remove_file(path)?;
    }
    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmpdir(name: &str) -> std::path::PathBuf {
        let d = std::env::temp_dir().join(format!(
            "cg_emit_{name}_{}_{:?}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_nanos())
                .unwrap_or(0)
        ));
        fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn render_wraps_body_with_markers() {
        let out = render("RULES\n- x");
        assert_eq!(
            out,
            "<!-- codegraph:start v1 -->\nRULES\n- x\n<!-- codegraph:end -->"
        );
        // trailing newline in body is trimmed
        assert_eq!(render("body\n"), render("body"));
    }

    #[test]
    fn write_block_creates_replaces_and_is_idempotent() {
        let dir = tmpdir("write");
        let path = dir.join("AGENTS.md");
        assert!(write_block(&path, "v1").unwrap());
        assert_eq!(fs::read_to_string(&path).unwrap(), render("v1") + "\n");
        assert!(
            !write_block(&path, "v1").unwrap(),
            "identical body is a no-op"
        );
        assert!(write_block(&path, "v2").unwrap());
        assert_eq!(fs::read_to_string(&path).unwrap(), render("v2") + "\n");

        // content outside markers is preserved; the block stays at the top
        fs::write(
            &path,
            format!("preamble\n{}\nbody\n{}\nfooter\n", BLOCK_START, BLOCK_END),
        )
        .unwrap();
        assert!(write_block(&path, "v3").unwrap());
        let text = fs::read_to_string(&path).unwrap();
        assert!(text.starts_with("preamble\n"));
        assert!(text.contains("v3"));
        assert!(text.ends_with("footer\n"));
        assert_eq!(read_block(&path).unwrap(), "v3");
    }

    #[test]
    fn read_block_returns_none_when_absent_or_unmarked() {
        let dir = tmpdir("read");
        assert!(read_block(&dir.join("nope.md")).is_none());
        let p = dir.join("plain.md");
        fs::write(&p, "no markers here").unwrap();
        assert!(read_block(&p).is_none());
    }

    #[test]
    fn remove_block_unlinks_when_empty_and_strips_when_not() {
        let dir = tmpdir("remove");
        let p = dir.join("AGENTS.md");
        fs::write(&p, format!("{}\nbody\n{}\n", BLOCK_START, BLOCK_END)).unwrap();
        assert!(remove_block(&p).unwrap());
        assert!(!p.exists(), "empty remainder unlinks the file");

        let q = dir.join("doc.md");
        fs::write(
            &q,
            format!("{}\nbody\n{}\nkeep me\n", BLOCK_START, BLOCK_END),
        )
        .unwrap();
        assert!(remove_block(&q).unwrap());
        assert_eq!(fs::read_to_string(&q).unwrap(), "keep me\n");
        assert!(!remove_block(&q).unwrap());
    }
}
