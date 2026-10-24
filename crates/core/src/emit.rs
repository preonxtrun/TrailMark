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

    let start = existing.find(BLOCK_START);
    let end = existing.find(BLOCK_END);
    // malformed markers (one without its pair, or end before start): refuse to
    // guess — we would never be able to restore the file faithfully
    if start.is_some() != end.is_some() || (start.is_some() && end.unwrap() < start.unwrap()) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!(
                "{}: malformed codegraph block (marker without its pair) — fix or remove the \
                 file manually; refusing to guess",
                path.display()
            ),
        ));
    }
    let new_text = match (start, end) {
        (Some(start), Some(end)) => {
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
///
/// Never deletes the file: an empty remainder is written back as an empty file
/// (byte-identical restoration, even when the file existed empty before).
/// Dangling markers (one without its pair) are cleaned up as best effort.
pub fn remove_block(path: &Path) -> io::Result<bool> {
    let existing = match fs::read_to_string(path) {
        Ok(text) => text,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(false),
        Err(e) => return Err(e),
    };
    let (start, end) = match (existing.find(BLOCK_START), existing.find(BLOCK_END)) {
        (Some(s), Some(e)) if e >= s => (s, e),
        _ => {
            // dangling markers: one without its pair — clean them up if present
            let stripped = strip_dangling(&existing);
            if stripped != existing {
                fs::write(path, stripped)?;
                return Ok(true);
            }
            return Ok(false);
        }
    };
    let new_text = existing[..start].to_string() + &existing[end + BLOCK_END.len()..];
    if new_text != existing {
        fs::write(path, new_text)?;
        return Ok(true);
    }
    Ok(false)
}

/// Remove marker occurrences when the file has one marker without its pair.
///
/// A half-pasted block (start without end, or vice versa) must never cause a
/// second block to be inserted; the dangling marker is removed so exactly one
/// well-formed block always exists.
fn strip_dangling(text: &str) -> String {
    let has_start = text.contains(BLOCK_START);
    let has_end = text.contains(BLOCK_END);
    if has_start != has_end {
        return text.replace(BLOCK_START, "").replace(BLOCK_END, "");
    }
    text.to_string()
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
    fn remove_block_strips_and_never_unlinks() {
        let dir = tmpdir("remove");
        let p = dir.join("AGENTS.md");
        fs::write(&p, format!("{}\nbody\n{}\n", BLOCK_START, BLOCK_END)).unwrap();
        assert!(remove_block(&p).unwrap());
        // empty remainder is written back (byte-identical restoration), never unlinked
        assert!(p.exists());
        assert_eq!(fs::read_to_string(&p).unwrap(), "\n");
        // removing again with no markers is a no-op
        assert!(!remove_block(&p).unwrap());

        let q = dir.join("doc.md");
        fs::write(
            &q,
            format!("{}\nbody\n{}\nkeep me\n", BLOCK_START, BLOCK_END),
        )
        .unwrap();
        assert!(remove_block(&q).unwrap());
        assert_eq!(fs::read_to_string(&q).unwrap(), "\nkeep me\n");
        assert!(!remove_block(&q).unwrap());
    }

    #[test]
    fn write_block_refuses_malformed_markers() {
        let dir = tmpdir("malformed");
        let p = dir.join("AGENTS.md");
        fs::write(&p, format!("start without end: {}\n", BLOCK_START)).unwrap();
        assert!(
            write_block(&p, "v").is_err(),
            "one marker without its pair must refuse"
        );
        fs::write(&p, format!("end without start: {}\n", BLOCK_END)).unwrap();
        assert!(write_block(&p, "v").is_err());
        fs::write(
            &p,
            format!("{}\nx\n{}\n{}\n", BLOCK_END, BLOCK_START, BLOCK_END),
        )
        .unwrap();
        assert!(
            write_block(&p, "v").is_err(),
            "end before start must refuse"
        );

        // dangling markers are cleaned up by remove_block
        let q = dir.join("dangling.md");
        fs::write(&q, format!("keep\n{}\n", BLOCK_START)).unwrap();
        assert!(remove_block(&q).unwrap());
        assert_eq!(fs::read_to_string(&q).unwrap(), "keep\n\n");
    }

    #[test]
    fn strip_dangling_removes_only_unpaired_markers() {
        // no markers → unchanged
        assert_eq!(strip_dangling("plain text"), "plain text");
        // paired markers → unchanged
        let paired = format!("a {BLOCK_START} b {BLOCK_END} c");
        assert_eq!(strip_dangling(&paired), paired);
        // dangling start / dangling end → markers removed
        let dangling_start = format!("a {BLOCK_START} b");
        assert_eq!(strip_dangling(&dangling_start), "a  b");
        let dangling_end = format!("a {BLOCK_END} b");
        assert_eq!(strip_dangling(&dangling_end), "a  b");
    }
}
