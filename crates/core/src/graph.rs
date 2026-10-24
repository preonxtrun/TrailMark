//! File-level PageRank over the reference graph (port of `index/graph.py`).
//!
//! Edges are file→file with weight = number of references. The iteration count
//! is fixed (no early convergence exit) and all input arrays are built in sorted
//! order, so the result is byte-identical across runs for the same inputs (hard
//! invariant #1). Floating-point summation mirrors numpy's pairwise summation
//! (`np.bincount`/`.sum()`) so scores match the Python reference bit-for-bit.

use std::collections::HashMap;

use crate::model::IndexData;

/// One item in the sorted edge array: (src index, dst index, weight).
type EdgeItem = (usize, usize, f64);

/// numpy's `pairwise_sum` (loops.c.src): sequential below 8 elements, 8
/// unrolled accumulators up to PW_BLOCKSIZE=128, recursive halving above.
fn np_sum(a: &[f64]) -> f64 {
    let n = a.len();
    if n < 8 {
        let mut res = 0.0f64;
        for x in a {
            res += x;
        }
        res
    } else if n <= 128 {
        let mut r = [0.0f64; 8];
        for (k, slot) in r.iter_mut().enumerate() {
            *slot = a[k];
        }
        let lim = n - n % 8;
        let mut i = 8;
        while i < lim {
            for k in 0..8 {
                r[k] += a[i + k];
            }
            i += 8;
        }
        let mut res = ((r[0] + r[1]) + (r[2] + r[3])) + ((r[4] + r[5]) + (r[6] + r[7]));
        // non-multiple-of-8 rest
        while i < n {
            res += a[i];
            i += 1;
        }
        res
    } else {
        let mut n2 = n / 2;
        n2 -= n2 % 8;
        np_sum(&a[..n2]) + np_sum(&a[n2..])
    }
}

/// `np.bincount(idx, weights=w, minlength=n)`: sequential accumulation per bin
/// in array order.
fn bincount(idx: &[usize], weights: &[f64], n: usize) -> Vec<f64> {
    let mut out = vec![0.0f64; n];
    for (i, &j) in idx.iter().enumerate() {
        out[j] += weights[i];
    }
    out
}

/// Compute file-level PageRank scores summing to ~1.0.
///
/// * `damping`: damping factor (teleport probability is `1 - damping`).
/// * `iters`: fixed power-iteration count.
/// * `personalization`: optional `{file: weight}` seed vector (used by focus);
///   missing keys get weight 0. An all-zero/empty mapping falls back to uniform.
///
/// Dangling nodes (no out-edges) and unreachable mass are redistributed uniformly
/// every iteration, exactly like the numpy reference implementation.
pub fn pagerank(
    data: &IndexData,
    damping: f64,
    iters: u32,
    personalization: Option<&HashMap<String, f64>>,
) -> HashMap<String, f64> {
    let base: Vec<&str> = data.files.keys().map(|k| k.as_str()).collect();
    let mut edge_weight: HashMap<(&str, &str), i64> = HashMap::new();
    let mut extra: Vec<&str> = Vec::new();
    for r in &data.refs {
        if r.src_file.is_empty() || r.dst_file.is_empty() {
            continue; // unresolved refs carry no file edge
        }
        *edge_weight
            .entry((r.src_file.as_str(), r.dst_file.as_str()))
            .or_insert(0) += 1;
        if !base.binary_search(&r.src_file.as_str()).is_ok() {
            extra.push(r.src_file.as_str());
        }
        if !base.binary_search(&r.dst_file.as_str()).is_ok() {
            extra.push(r.dst_file.as_str());
        }
    }
    // nodes = sorted(set(file keys) | extra endpoints)
    let mut nodes: Vec<&str> = base.clone();
    nodes.extend(extra);
    nodes.sort_unstable();
    nodes.dedup();
    let n = nodes.len();
    if n == 0 {
        return HashMap::new();
    }
    let index: HashMap<&str, usize> = nodes
        .iter()
        .enumerate()
        .map(|(i, p)| (*p, i))
        .collect();

    let pvec: Vec<f64> = match personalization.filter(|p| !p.is_empty()) {
        Some(pers) => {
            let raw: Vec<f64> = nodes
                .iter()
                .map(|p| {
                    let v = pers.get(*p).copied().unwrap_or(0.0);
                    if v.is_nan() || v < 0.0 {
                        0.0
                    } else {
                        v
                    }
                })
                .collect();
            let total = np_sum(&raw);
            if total > 0.0 {
                raw.iter().map(|v| v / total).collect()
            } else {
                vec![1.0 / n as f64; n]
            }
        }
        None => vec![1.0 / n as f64; n],
    };

    if edge_weight.is_empty() {
        return nodes
            .iter()
            .enumerate()
            .map(|(i, p)| ((*p).to_string(), pvec[i]))
            .collect();
    }

    // Sorted edge arrays → deterministic float summation order. (src, dst) is
    // unique per edge, so the weight never participates in the comparison —
    // exactly like the Python `sorted((src, dst, w))` on unique pairs.
    let mut items: Vec<EdgeItem> = edge_weight
        .iter()
        .map(|(&(s, d), &w)| (index[s], index[d], w as f64))
        .collect();
    items.sort_by(|a, b| (a.0, a.1).cmp(&(b.0, b.1)));

    let src_idx: Vec<usize> = items.iter().map(|e| e.0).collect();
    let dst_idx: Vec<usize> = items.iter().map(|e| e.1).collect();
    let weights: Vec<f64> = items.iter().map(|e| e.2).collect();
    let out_weight = bincount(&src_idx, &weights, n);
    let flow_weight: Vec<f64> = items
        .iter()
        .map(|e| e.2 / out_weight[e.0])
        .collect();
    let dangling: Vec<bool> = out_weight.iter().map(|&w| w == 0.0).collect();

    let mut rank = pvec.clone();
    for _ in 0..iters.max(0) {
        let scaled: Vec<f64> = src_idx
            .iter()
            .zip(&flow_weight)
            .map(|(&s, &w)| rank[s] * w)
            .collect();
        let flow = bincount(&dst_idx, &scaled, n);
        let dangling_vals: Vec<f64> = rank
            .iter()
            .zip(&dangling)
            .filter_map(|(&v, &d)| if d { Some(v) } else { None })
            .collect();
        let dangling_mass = np_sum(&dangling_vals);
        let dm = dangling_mass / n as f64;
        let teleport = 1.0 - damping;
        rank = flow
            .iter()
            .zip(&pvec)
            .map(|(&f, &p)| damping * f + damping * dm + teleport * p)
            .collect();
    }

    let total = np_sum(&rank);
    let final_rank: Vec<f64> = if total > 0.0 {
        rank.iter().map(|v| v / total).collect()
    } else {
        rank
    };
    nodes
        .iter()
        .zip(final_rank)
        .map(|(p, s)| ((*p).to_string(), s))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::{FileEntry, Ref};
    use std::collections::BTreeMap;

    fn data(files: &[&str], refs: &[(&str, &str)]) -> IndexData {
        let mut map = BTreeMap::new();
        for f in files {
            map.insert(
                (*f).to_string(),
                FileEntry {
                    path: (*f).to_string(),
                    lang: "py".into(),
                    ..Default::default()
                },
            );
        }
        IndexData {
            root: "/r".into(),
            files: map,
            refs: refs
                .iter()
                .map(|(s, d)| Ref {
                    src_file: (*s).to_string(),
                    dst_name: "x".into(),
                    dst_file: (*d).to_string(),
                    line: 1,
                })
                .collect(),
        }
    }

    #[test]
    fn pagerank_sums_to_one_and_orders_hot_files_first() {
        let d = data(
            &["core/api.py", "app/a.py", "app/b.py"],
            &[("app/a.py", "core/api.py"), ("app/b.py", "core/api.py")],
        );
        let pr = pagerank(&d, 0.85, 30, None);
        let total: f64 = pr.values().sum();
        assert!((total - 1.0).abs() < 1e-9, "sum={total}");
        assert!(pr["core/api.py"] > pr["app/a.py"]);
        assert_eq!(pr["app/a.py"], pr["app/b.py"], "symmetric inputs tie");
    }

    #[test]
    fn pagerank_is_deterministic_and_handles_dangling() {
        let d = data(
            &["a.py", "b.py", "c.py"],
            &[("a.py", "b.py"), ("a.py", "c.py")],
        );
        let p1 = pagerank(&d, 0.85, 30, None);
        let p2 = pagerank(&d, 0.85, 30, None);
        let mut k1: Vec<(String, u64)> =
            p1.iter().map(|(k, v)| (k.clone(), v.to_bits())).collect();
        let mut k2: Vec<(String, u64)> =
            p2.iter().map(|(k, v)| (k.clone(), v.to_bits())).collect();
        k1.sort();
        k2.sort();
        assert_eq!(k1, k2, "bit-identical across runs");
        let total: f64 = p1.values().sum();
        assert!((total - 1.0).abs() < 1e-9);
    }

    #[test]
    fn pagerank_personalization_seeds_weights() {
        let d = data(&["a.py", "b.py"], &[]);
        let mut pers = HashMap::new();
        pers.insert("b.py".to_string(), 1.0);
        let pr = pagerank(&d, 0.85, 30, Some(&pers));
        assert!(pr["b.py"] > pr["a.py"], "seeded node must dominate");
        let total: f64 = pr.values().sum();
        assert!((total - 1.0).abs() < 1e-9);
    }

    #[test]
    fn np_sum_matches_numpy_pairwise_shape() {
        // simple invariants: identity, monotonic blocks
        let v: Vec<f64> = (1..=100).map(|i| i as f64).collect();
        let expected: f64 = 5050.0;
        assert_eq!(np_sum(&v), expected);
        assert_eq!(np_sum(&[]), 0.0);
        assert_eq!(np_sum(&[1.5]), 1.5);
        let seven: Vec<f64> = vec![0.1; 7];
        assert!((np_sum(&seven) - 0.7).abs() < 1e-12);
    }
}
