//! Ranking: the FTS5 query, cosine nearest neighbours, and rank fusion.

use std::collections::HashMap;

/// Reciprocal rank fusion's damping constant; 60 is the value from the
/// original paper and the usual default.
const RRF_K: f64 = 60.0;

/// An FTS5 query matching any of the query's words, or `None` when it has none
/// worth matching. Every word is quoted, so nothing in it is read as FTS5
/// syntax (`AND`, `NEAR`, `*`, a column filter).
pub(crate) fn fts_query(query: &str) -> Option<String> {
    let words: Vec<String> = query
        .split(|c: char| !c.is_alphanumeric())
        .filter(|w| w.chars().count() >= 2)
        .map(|w| format!("\"{}\"", w.to_lowercase()))
        .collect();
    (!words.is_empty()).then(|| words.join(" OR "))
}

/// Scaled to unit length, so cosine similarity is a dot product.
pub(crate) fn normalized(mut v: Vec<f32>) -> Vec<f32> {
    let norm = v.iter().map(|x| x * x).sum::<f32>().sqrt();
    if norm > 0.0 {
        v.iter_mut().for_each(|x| *x /= norm);
    }
    v
}

pub(crate) fn to_blob(v: &[f32]) -> Vec<u8> {
    v.iter().flat_map(|x| x.to_le_bytes()).collect()
}

fn from_blob(b: &[u8]) -> impl Iterator<Item = f32> + '_ {
    b.chunks_exact(4)
        .map(|c| f32::from_le_bytes([c[0], c[1], c[2], c[3]]))
}

/// The `n` ids whose vectors are closest to `q`, best first. A brute-force
/// scan: a collection is hundreds of passages, not millions.
pub(crate) fn nearest(q: &[f32], all: &[(i64, Vec<u8>)], n: usize) -> Vec<i64> {
    let mut scored: Vec<(f32, i64)> = all
        .iter()
        .filter(|(_, b)| b.len() == q.len() * 4)
        .map(|(id, b)| (from_blob(b).zip(q).map(|(a, b)| a * b).sum(), *id))
        .collect();
    scored.sort_by(|a, b| b.0.total_cmp(&a.0));
    scored.truncate(n);
    scored.into_iter().map(|(_, id)| id).collect()
}

/// Reciprocal rank fusion of several best-first lists: the top `k` ids with
/// their fused scores, best first. Ties go to the id seen first.
pub(crate) fn fuse(lists: &[Vec<i64>], k: usize) -> Vec<(i64, f64)> {
    let mut score: HashMap<i64, (f64, usize)> = HashMap::new();
    let mut seen = 0;
    for list in lists {
        for (rank, id) in list.iter().enumerate() {
            let e = score.entry(*id).or_insert_with(|| {
                seen += 1;
                (0.0, seen)
            });
            e.0 += 1.0 / (RRF_K + rank as f64 + 1.0);
        }
    }
    let mut out: Vec<(i64, f64, usize)> =
        score.into_iter().map(|(id, (s, o))| (id, s, o)).collect();
    out.sort_by(|a, b| b.1.total_cmp(&a.1).then(a.2.cmp(&b.2)));
    out.truncate(k);
    out.into_iter().map(|(id, s, _)| (id, s)).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn query_syntax_is_never_passed_through() {
        assert_eq!(
            fts_query("NEAR(a b) AND title:x* OR \"y\"").unwrap(),
            "\"near\" OR \"and\" OR \"title\" OR \"or\""
        );
        assert_eq!(fts_query("a ! ?"), None);
    }

    #[test]
    fn agreement_between_rankings_wins() {
        // 2 is second in both lists; 1 and 3 each top one list only.
        let fused = fuse(&[vec![1, 2], vec![3, 2]], 3);
        assert_eq!(fused[0].0, 2);
        assert_eq!(fused.len(), 3);
    }

    #[test]
    fn nearest_is_by_cosine() {
        let a = to_blob(&normalized(vec![1.0, 0.0]));
        let b = to_blob(&normalized(vec![0.6, 0.8]));
        let wrong_dims = to_blob(&[1.0, 0.0, 0.0]);
        let q = normalized(vec![0.0, 1.0]);
        assert_eq!(
            nearest(&q, &[(1, a), (2, b), (3, wrong_dims)], 5),
            vec![2, 1]
        );
    }
}
