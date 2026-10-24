//! Repository scanner (port of `index/scanner.py`): gitignore-aware walk, binary
//! sniffing, extension→language mapping.
//!
//! Returns flat scan records for the parser layer. Deterministic: output is
//! sorted by repo-relative posix path and never depends on filesystem iteration
//! order. Gitignore matching is delegated to the `ignore` crate configured to
//! mirror the Python reference's semantics (gitignore honored with or without a
//! `.git` directory, hidden files included, no global/parent config).

use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};

use ignore::WalkBuilder;

/// Languages indexed when the caller does not restrict `langs`.
pub const DEFAULT_LANGS: &[&str] = &["py", "ts", "js", "go", "rs"];

/// Extension → language. Anything not listed is excluded unless `txt` is requested.
pub fn ext_to_lang(ext: &str) -> Option<&'static str> {
    match ext {
        ".py" | ".pyi" => Some("py"), // .pyi stubs are python source (documented)
        ".ts" | ".tsx" => Some("ts"),
        ".js" | ".jsx" | ".mjs" | ".cjs" => Some("js"),
        ".go" => Some("go"),
        ".rs" => Some("rs"),
        ".h" | ".c" | ".cc" | ".cpp" => Some("c"),
        _ => None,
    }
}

/// Text documents indexed only when `"txt"` is explicitly in `langs`.
pub fn is_txt_ext(ext: &str) -> bool {
    matches!(ext, ".txt" | ".md" | ".rst")
}

/// Directories never descended into (spec list plus standard vendored/cache dirs).
pub const SKIP_DIRS: &[&str] = &[
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    "dist",
    "build",
    ".codegraph",
    ".hg",
    ".svn",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".eggs",
    "target",
    ".next",
];

/// Extensions assumed binary without sniffing.
fn is_binary_ext(ext: &str) -> bool {
    matches!(
        ext,
        ".png"
            | ".jpg"
            | ".jpeg"
            | ".gif"
            | ".bmp"
            | ".ico"
            | ".webp"
            | ".pdf"
            | ".zip"
            | ".tar"
            | ".gz"
            | ".tgz"
            | ".bz2"
            | ".xz"
            | ".7z"
            | ".rar"
            | ".jar"
            | ".woff"
            | ".woff2"
            | ".ttf"
            | ".otf"
            | ".eot"
            | ".mp3"
            | ".mp4"
            | ".avi"
            | ".mov"
            | ".wav"
            | ".flac"
            | ".ogg"
            | ".exe"
            | ".dll"
            | ".so"
            | ".dylib"
            | ".a"
            | ".o"
            | ".obj"
            | ".lib"
            | ".pyc"
            | ".pyo"
            | ".class"
            | ".wasm"
            | ".db"
            | ".sqlite"
            | ".sqlite3"
            | ".bin"
            | ".iso"
            | ".img"
            | ".deb"
            | ".rpm"
            | ".whl"
            | ".parquet"
    )
}

/// Files larger than this are skipped (vendored/minified blobs, lockfiles).
pub const MAX_FILE_BYTES: u64 = 1_500_000;

const SNIFF_BYTES: usize = 8192;

/// One scanned file: repo-relative path, absolute path, mtime, size, language.
#[derive(Clone, Debug, PartialEq)]
pub struct ScannedFile {
    /// Repo-relative posix path.
    pub rel: String,
    /// Absolute path on disk.
    pub abs: PathBuf,
    /// Modification time (seconds since epoch, like Python `st_mtime`).
    pub mtime: f64,
    /// Size in bytes.
    pub size: u64,
    /// Language key (`"py"`, `"ts"`, ...).
    pub lang: String,
}

/// Map a file extension (with leading dot) to a language, honoring `langs`.
pub fn lang_for(ext: &str, langs: &HashSet<String>) -> Option<String> {
    match ext_to_lang(ext) {
        Some(lang) if langs.contains(lang) => Some(lang.to_string()),
        _ if is_txt_ext(ext) && langs.contains("txt") => Some("txt".to_string()),
        _ => None,
    }
}

/// Python's `os.path.splitext` semantics for a bare file name.
///
/// Returns the extension *with* the leading dot, or `None` for names whose only
/// dots are leading (`.py`, `..foo`) or when there is no dot.
fn split_ext(name: &str) -> Option<&str> {
    let dot = name.rfind('.')?;
    if name[..dot].chars().all(|c| c == '.') {
        None
    } else {
        Some(&name[dot..])
    }
}

/// Null-byte sniff of the first 8KB. Unreadable files count as binary.
fn looks_binary(path: &Path) -> bool {
    use std::io::Read;
    let mut file = match fs::File::open(path) {
        Ok(f) => f,
        Err(_) => return true,
    };
    let mut buf = [0u8; SNIFF_BYTES];
    let mut filled = 0usize;
    loop {
        match file.read(&mut buf[filled..]) {
            Ok(0) => break,
            Ok(n) => {
                filled += n;
                if filled >= SNIFF_BYTES {
                    break;
                }
            }
            Err(_) => return true,
        }
    }
    buf[..filled].contains(&0)
}

/// Absolute, lexically normalized version of `root` (like `Path.resolve()`).
pub fn resolve_root(root: &str) -> String {
    let path = Path::new(root);
    if let Ok(canon) = path.canonicalize() {
        return canon.to_string_lossy().to_string();
    }
    // Python's resolve() works on non-existent paths: absolutize lexically.
    let abs = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()
            .unwrap_or_else(|_| PathBuf::from("."))
            .join(path)
    };
    let mut out: Vec<std::borrow::Cow<'_, str>> = Vec::new();
    for comp in abs.components() {
        match comp {
            std::path::Component::CurDir => {}
            std::path::Component::ParentDir => {
                out.pop();
            }
            other => out.push(other.as_os_str().to_string_lossy()),
        }
    }
    let joined = out.join("/");
    if joined.is_empty() {
        "/".to_string()
    } else {
        joined
    }
}

/// Walk `root` and return indexable files sorted by repo-relative path.
///
/// Respects `.gitignore` files (root and nested, negation supported, with or
/// without a `.git` directory), skips VCS/vendored/cache directories, and drops
/// binary files by extension plus a null-byte sniff of the first 8KB.
/// Unreadable roots yield an empty list (never fails; invariant #4).
pub fn scan(root: &str, langs: &[String]) -> Vec<ScannedFile> {
    let langs_set: HashSet<String> = langs.iter().cloned().collect();
    let root_path = PathBuf::from(resolve_root(root));
    let mut out: Vec<ScannedFile> = Vec::new();

    let walker = WalkBuilder::new(&root_path)
        .hidden(false) // the Python scanner walks hidden files
        .require_git(false) // .gitignore applies even without a .git directory
        .git_ignore(true)
        .git_global(false)
        .git_exclude(false)
        .ignore(false) // no .ignore files in the Python reference
        .parents(false) // no gitignores outside the repo root
        .threads(1)
        .filter_entry(|e| {
            // prune hard-skip directories (and their contents) during the walk
            if e.depth() == 0 {
                return true;
            }
            if e.file_type().is_some_and(|t| t.is_dir()) {
                !skip_dir(&e.file_name().to_string_lossy())
            } else {
                true
            }
        })
        .build();

    for entry in walker {
        let entry = match entry {
            Ok(e) => e,
            Err(_) => continue, // unreadable entries are skipped, never fatal
        };
        if entry.depth() == 0 {
            continue; // the root itself is not a candidate file
        }
        // only plain files (symlinks / special files are skipped, like os.stat
        // with follow_symlinks=False in the Python reference)
        if !entry.file_type().is_some_and(|t| t.is_file()) {
            continue;
        }
        let path = entry.path().to_path_buf();
        let rel = match path.strip_prefix(&root_path) {
            Ok(r) => r.to_string_lossy().replace('\\', "/"),
            Err(_) => continue,
        };
        let name = entry.file_name().to_string_lossy().to_string();
        let ext_lower = split_ext(&name).map_or("", |e| e).to_lowercase();
        let lang = match lang_for(&ext_lower, &langs_set) {
            Some(l) => l,
            None => continue,
        };
        let meta = match fs::metadata(&path) {
            Ok(m) => m,
            Err(_) => continue,
        };
        let size = meta.len();
        if size > MAX_FILE_BYTES {
            continue;
        }
        if is_binary_ext(&ext_lower) || looks_binary(&path) {
            continue;
        }
        let mtime = systemtime_to_f64(meta.modified().ok());
        out.push(ScannedFile {
            rel,
            abs: path,
            mtime,
            size,
            lang,
        });
    }

    out.sort_by(|a, b| a.rel.cmp(&b.rel));
    out
}

/// Convert a file's modified time into Python-`st_mtime`-style float seconds.
pub fn systemtime_to_f64(st: Option<std::time::SystemTime>) -> f64 {
    match st {
        Some(t) => match t.duration_since(std::time::UNIX_EPOCH) {
            Ok(d) => d.as_secs() as f64 + f64::from(d.subsec_nanos()) / 1e9,
            Err(_) => 0.0,
        },
        None => 0.0,
    }
}

/// Prune the hard SKIP_DIRS list during the walk (independent of gitignore).
pub fn skip_dir(name: &str) -> bool {
    SKIP_DIRS.contains(&name)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn split_ext_of(name: &str) -> String {
        split_ext(name).unwrap_or("").to_string()
    }

    #[test]
    fn split_ext_matches_python_semantics() {
        assert_eq!(split_ext_of("a.py"), ".py");
        assert_eq!(split_ext_of("a.tar.gz"), ".gz");
        assert_eq!(split_ext_of("a..b"), ".b");
        assert_eq!(split_ext_of("noext"), "");
        assert_eq!(split_ext_of(".py"), ""); // hidden file: no extension
        assert_eq!(split_ext_of("..foo"), "");
        assert_eq!(split_ext_of(".hidden.md"), ".md");
    }

    #[test]
    fn lang_for_respects_requested_langs() {
        let all: HashSet<String> = ["py", "ts", "js", "go", "rs", "txt"]
            .iter()
            .map(|s| s.to_string())
            .collect();
        assert_eq!(lang_for(".py", &all).unwrap(), "py");
        assert_eq!(lang_for(".pyi", &all).unwrap(), "py"); // caller lowercases (Python parity)
        assert_eq!(lang_for(".tsx", &all).unwrap(), "ts");
        assert_eq!(lang_for(".md", &all).unwrap(), "txt");
        assert_eq!(lang_for(".md", &HashSet::from(["py".to_string()])), None);
        assert_eq!(lang_for(".cpp", &all), None); // c not requested
        assert_eq!(
            lang_for(".h", &HashSet::from(["c".to_string()])).unwrap(),
            "c"
        );
    }

    #[test]
    fn scan_skips_dirs_gitignored_and_binary_files() {
        let base = std::env::temp_dir().join(format!("cg_scan_{}", std::process::id()));
        let root = base.join("repo");
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(root.join("src/pkg")).unwrap();
        fs::create_dir_all(root.join("node_modules")).unwrap();
        fs::create_dir_all(root.join("vendor")).unwrap();
        fs::write(root.join(".gitignore"), "vendor/\n*.ignored.py\n").unwrap();
        fs::write(root.join("src/pkg/a.py"), "def a():\n    pass\n").unwrap();
        fs::write(root.join("src/b.ts"), "export const b = 1;\n").unwrap();
        fs::write(root.join("src/c.ignored.py"), "def c():\n    pass\n").unwrap();
        fs::write(root.join("node_modules/x.js"), "var x=1;\n").unwrap();
        fs::write(root.join("vendor/v.py"), "def v():\n    pass\n").unwrap();
        fs::write(root.join("src/blob.py"), [0u8, 1, 2, 0]).unwrap();
        let langs: Vec<String> = DEFAULT_LANGS.iter().map(|s| s.to_string()).collect();
        let files = scan(root.to_str().unwrap(), &langs);
        let rels: Vec<&str> = files.iter().map(|f| f.rel.as_str()).collect();
        assert_eq!(rels, vec!["src/b.ts", "src/pkg/a.py"]);
        assert!(files.iter().all(|f| f.size > 0 && f.mtime > 0.0));
        let _ = fs::remove_dir_all(&base);
    }

    #[test]
    fn scan_missing_root_is_empty() {
        let langs: Vec<String> = DEFAULT_LANGS.iter().map(|s| s.to_string()).collect();
        assert!(scan("/nonexistent/repo/path", &langs).is_empty());
    }
}
