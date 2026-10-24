//! Deterministic text helpers (port of `emit/fmt.py` + `util/tokens.py`).
//!
//! Budgets are enforced in UTF-8 bytes. All helpers are pure functions of their
//! inputs: no locale, no randomness, no floats.

/// UTF-8 byte length (the enforced budget unit).
#[inline]
pub fn byte_len(text: &str) -> usize {
    text.len()
}

/// Cheap deterministic token estimate (~4 chars/token for mixed code+prose).
///
/// Mirrors `util.tokens.estimate_tokens`: `max(1, (len + 3) // 4)` in characters.
pub fn estimate_tokens(text: &str) -> usize {
    text.chars().count().div_ceil(4).max(1)
}

/// Shorten `text` to at most `max_bytes` UTF-8 bytes, ellipsizing when cut.
///
/// Never splits a code point; returns the text unchanged when it already fits.
/// Below the ellipsis' own byte size the result is a hard cut without marker.
pub fn truncate(text: &str, max_bytes: usize) -> String {
    if max_bytes == 0 {
        return String::new();
    }
    if text.len() <= max_bytes {
        return text.to_string();
    }
    let ellipsis = "\u{2026}"; // 3 bytes in UTF-8
    let (tail, limit) = if max_bytes >= ellipsis.len() {
        (ellipsis, max_bytes - ellipsis.len())
    } else {
        ("", max_bytes)
    };
    let mut end = limit.min(text.len());
    while end > 0 && !text.is_char_boundary(end) {
        end -= 1;
    }
    format!("{}{}", &text[..end], tail)
}

/// Greedy deterministic word wrap at `width` bytes.
///
/// The first line starts with `bullet`; continuation lines start with `indent`.
/// Words longer than the available width are never split mid-word.
pub fn wrap(text: &str, width: usize, indent: &str, bullet: &str) -> String {
    let words: Vec<&str> = text.split_whitespace().collect();
    if words.is_empty() {
        return String::new();
    }
    let mut lines: Vec<String> = Vec::new();
    let mut cur = bullet.to_string();
    let mut has_word = false;
    for word in words {
        let cand = if has_word {
            format!("{cur} {word}")
        } else {
            format!("{cur}{word}")
        };
        if has_word && byte_len(&cand) > width {
            lines.push(cur);
            cur = format!("{indent}{word}");
        } else {
            cur = cand;
            has_word = true;
        }
    }
    lines.push(cur);
    lines.join("\n")
}

/// [`wrap`] with the codegraph defaults (`indent="  "`, `bullet="- "`).
pub fn wrap_default(text: &str, width: usize) -> String {
    wrap(text, width, "  ", "- ")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn truncate_by_bytes_never_splits_codepoints() {
        assert_eq!(truncate("hello", 10), "hello");
        assert_eq!(truncate("hello", 4), "h\u{2026}");
        assert_eq!(truncate("h\u{e9}llo", 3), "\u{2026}"); // limit=0 → ellipsis only (Python parity)
        assert_eq!(truncate("hello", 2), "he"); // below ellipsis size: hard cut
        assert_eq!(truncate("hello", 0), "");
        assert_eq!(byte_len(&truncate("hello", 4)), 4);
    }

    #[test]
    fn wrap_is_greedy_and_byte_measured() {
        let text = "aaa bbb ccc ddd";
        let out = wrap_default(text, 10);
        assert_eq!(out, "- aaa bbb\n  ccc ddd");
        assert_eq!(wrap_default("", 10), "");
        // long single word is never split
        let out = wrap_default("xx yy zzzzzzzzzzzz", 9);
        assert_eq!(out, "- xx yy\n  zzzzzzzzzzzz");
    }

    #[test]
    fn estimate_tokens_matches_python() {
        assert_eq!(estimate_tokens(""), 1); // max(1, ...)
        assert_eq!(estimate_tokens("abcd"), 1);
        assert_eq!(estimate_tokens("abcde"), 2);
        assert_eq!(estimate_tokens("abcdefgh"), 2);
    }
}
