//! Performance targets (SPEC invariant #6 / deliverable PERF TARGETS):
//!
//! - cold build < 5s on a synthetic 4001-file repo
//! - warm update (1 changed file) < 100ms
//! - focus < 50ms
//!
//! The repo mirrors the Python suite's 4001-file fixture. Targets are asserted
//! strictly in release builds; debug builds only assert generous ceilings so
//! `cargo test` stays green everywhere. Measured numbers are printed.

use std::fs;
use std::path::{Path, PathBuf};
use std::time::Instant;

use codegraph_cli::atlas::assemble;
use codegraph_cli::focus::rank;
use codegraph_core::{build, update, IndexCache};

fn scratch(tag: &str) -> PathBuf {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let dir = std::env::temp_dir().join(format!("cg_perf_{tag}_{}_{nanos}", std::process::id()));
    fs::create_dir_all(&dir).expect("perf scratch dir");
    dir
}

/// 40 packages × (50 modules + 50 siblings) + 1 core file = 4001 files.
fn build_big_repo(root: &Path) {
    fs::create_dir_all(root.join("core")).unwrap();
    fs::write(
        root.join("core/api.py"),
        "def shared_api(x):\n    \"\"\"Shared hot module.\"\"\"\n    return x + 1\n",
    )
    .unwrap();
    for p in 0..40 {
        let pkg = root.join(format!("pkg{p:02}"));
        fs::create_dir_all(&pkg).unwrap();
        for f in 0..50 {
            fs::write(
                pkg.join(format!("mod{f:02}.py")),
                format!(
                    "\"\"\"Module {p}.{f}.\"\"\"\nfrom core.api import shared_api\nfrom .sibling{f:02} import sibling_{f}\n\ndef fn_{p}_{f}(x):\n    \"\"\"Compute {p}.{f}.\"\"\"\n    return shared_api(x) + sibling_{f}(x)\n\nclass Thing{p}_{f}:\n    \"\"\"Thing {p}.{f}.\"\"\"\n\n    def run(self, v):\n        return fn_{p}_{f}(v)\n"
                ),
            )
            .unwrap();
            fs::write(
                pkg.join(format!("sibling{f:02}.py")),
                format!("def sibling_{f}(x):\n    return x * 2\n"),
            )
            .unwrap();
        }
    }
}

#[test]
fn perf_targets_on_4001_files() {
    let release = !cfg!(debug_assertions);
    let root = scratch("repo");
    build_big_repo(&root);

    let langs = codegraph_core::default_langs();
    let root_str = root.to_string_lossy().to_string();
    let cache = IndexCache::new(&root.join(".codegraph"));

    // --- cold build ---------------------------------------------------------
    let t0 = Instant::now();
    let data = build(&root_str, Some(&langs), 0, Some(&cache));
    let cold = t0.elapsed();
    assert_eq!(data.files.len(), 4001);
    println!(
        "[perf] cold build ({} files, {} symbols, {} refs): {:.3}s",
        data.files.len(),
        data.symbols().len(),
        data.refs.len(),
        cold.as_secs_f64()
    );
    if release {
        assert!(cold.as_secs_f64() < 5.0, "cold build target <5s: {cold:?}");
    } else {
        assert!(cold.as_secs_f64() < 60.0, "debug ceiling: {cold:?}");
    }

    // --- warm update (touch exactly one file) --------------------------------
    let victim = root.join("pkg00/mod00.py");
    let text = fs::read_to_string(&victim).unwrap();
    let stamp = std::time::SystemTime::now() + std::time::Duration::from_secs(5);
    fs::write(&victim, format!("{text}\ndef extra():\n    return 1\n")).unwrap();
    let epoch = std::time::SystemTime::UNIX_EPOCH;
    let future = stamp.duration_since(epoch).unwrap();
    set_mtime(&victim, future.as_secs() as i64, future.subsec_nanos());

    let t1 = Instant::now();
    let updated = update(&root_str, Some(&langs), 0, Some(&cache));
    let warm = t1.elapsed();
    assert!(
        updated.files["pkg00/mod00.py"]
            .symbols
            .iter()
            .any(|s| s.name == "extra"),
        "warm update must reparse the changed file"
    );
    assert_eq!(updated.files.len(), 4001, "no files lost");
    println!(
        "[perf] warm update (1 changed file): {:.3}ms",
        warm.as_secs_f64() * 1e3
    );
    if release {
        assert!(
            warm.as_secs_f64() < 0.1,
            "warm update target <100ms: {warm:?}"
        );
    } else {
        assert!(warm.as_secs_f64() < 5.0, "debug ceiling: {warm:?}");
    }

    // --- focus (cache load + rank + assemble + write) ------------------------
    let issue = "fix the shared_api computation for pkg00 module extra";
    let t2 = Instant::now();
    let cached = cache.load().expect("cache present");
    let leads = rank(&cached, &cache, issue, 5);
    let body = assemble(&cached, Some(issue), "generic", 2048);
    assert!(!body.is_empty());
    let focus = t2.elapsed();
    println!(
        "[perf] focus (load+rank+assemble, {} leads, {} B body): {:.3}ms",
        leads.len(),
        body.len(),
        focus.as_secs_f64() * 1e3
    );
    if release {
        assert!(focus.as_secs_f64() < 0.05, "focus target <50ms: {focus:?}");
    } else {
        assert!(focus.as_secs_f64() < 5.0, "debug ceiling: {focus:?}");
    }

    fs::remove_dir_all(&root).ok();
}

/// `os.utime` equivalent (seconds + nanos since the epoch).
fn set_mtime(path: &Path, secs: i64, nanos: u32) {
    let st = std::time::SystemTime::UNIX_EPOCH
        .checked_add(std::time::Duration::new(secs as u64, nanos))
        .expect("valid timestamp");
    let file = fs::OpenOptions::new().append(true).open(path).unwrap();
    file.set_times(std::fs::FileTimes::new().set_modified(st))
        .unwrap();
}

#[test]
fn warm_update_profile() {
    let root = scratch("prof");
    build_big_repo(&root);
    let langs = codegraph_core::default_langs();
    let root_str = root.to_string_lossy().to_string();
    let cache = IndexCache::new(&root.join(".codegraph"));
    let data = build(&root_str, Some(&langs), 0, Some(&cache));
    assert_eq!(data.files.len(), 4001);

    // component timings
    let t = Instant::now();
    let cached = cache.load().unwrap();
    println!("[prof] cache.load: {:.3}ms ({} files, {} refs)", t.elapsed().as_secs_f64()*1e3, cached.files.len(), cached.refs.len());

    let t = Instant::now();
    let scanned = codegraph_core::scan(&root_str, &langs);
    println!("[prof] scan: {:.3}ms ({} files)", t.elapsed().as_secs_f64()*1e3, scanned.len());

    let victim = root.join("pkg00/mod00.py");
    let text = fs::read_to_string(&victim).unwrap();
    fs::write(&victim, format!("{text}\ndef extra():\n    return 1\n")).unwrap();
    let future = (std::time::SystemTime::now() + std::time::Duration::from_secs(5))
        .duration_since(std::time::UNIX_EPOCH).unwrap();
    set_mtime(&victim, future.as_secs() as i64, future.subsec_nanos());

    let t = Instant::now();
    let m = cache.file_mtimes();
    println!("[prof] file_mtimes: {:.3}ms ({} entries)", t.elapsed().as_secs_f64()*1e3, m.len());

    let t = Instant::now();
    let updated = update(&root_str, Some(&langs), 0, Some(&cache));
    println!("[prof] full update: {:.3}ms ({} files, {} refs)", t.elapsed().as_secs_f64()*1e3, updated.files.len(), updated.refs.len());

    // second update with NO changes: isolates load+scan+resolve floor
    let t = Instant::now();
    let _ = update(&root_str, Some(&langs), 0, Some(&cache));
    println!("[prof] no-op update: {:.3}ms", t.elapsed().as_secs_f64()*1e3);

    // cache write ops on 1 changed file (as update() performs them)
    let entry = cached.files.get("pkg00/mod00.py").cloned().unwrap();
    let refs = cached.refs.iter().filter(|r| r.src_file == "pkg00/mod00.py").cloned().collect::<Vec<_>>();
    for _ in 0..3 {
        let t = Instant::now();
        cache.remove_files(&["pkg00/mod00.py".to_string()]).unwrap();
        cache.upsert_files(std::slice::from_ref(&entry), &refs).unwrap();
        println!("[prof] remove+upsert (1 file): {:.3}ms", t.elapsed().as_secs_f64()*1e3);
    }

    // resolve floor: resolve_refs over all cached refs
    let t = Instant::now();
    let name_idx: std::collections::HashMap<String, Vec<String>> = {
        use std::collections::BTreeSet;
        let mut idx: std::collections::HashMap<String, BTreeSet<String>> = std::collections::HashMap::new();
        for e in cached.files.values() {
            for sym in &e.symbols { idx.entry(sym.name.clone()).or_default().insert(e.path.clone()); }
        }
        idx.into_iter().map(|(k, v)| (k, v.into_iter().collect())).collect()
    };
    println!("[prof] name_index build: {:.3}ms", t.elapsed().as_secs_f64()*1e3);
    let by_file: std::collections::HashMap<String, Vec<codegraph_core::Ref>> = {
        let mut m: std::collections::HashMap<String, Vec<codegraph_core::Ref>> = std::collections::HashMap::new();
        for r in &cached.refs { m.entry(r.src_file.clone()).or_default().push(r.clone()); }
        m
    };
    let t = Instant::now();
    let resolved = codegraph_core::resolve_refs(&by_file, &name_idx, &cached.files.keys().cloned().collect(), &cached.files.iter().map(|(k,v)|(k.clone(),v.lang.clone())).collect(), "");
    println!("[prof] resolve_refs ({} in, {} out): {:.3}ms", cached.refs.len(), resolved.len(), t.elapsed().as_secs_f64()*1e3);

    fs::remove_dir_all(&root).ok();
}
