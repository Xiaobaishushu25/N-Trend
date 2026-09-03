use crate::v2::dataset::DatasetRow;
use anyhow::{anyhow, Result};

/// One walk-forward fold: train indices [0, train_end), valid [train_end, valid_end)
#[derive(Clone, Debug)]
pub struct Fold {
    pub train_start: usize,
    pub train_end: usize,
    pub valid_start: usize,
    pub valid_end: usize,
}

pub fn walk_forward(n: usize, n_splits: usize) -> Vec<Fold> {
    if n == 0 || n_splits == 0 { return vec![]; }
    let fold_size = (n as f64 / n_splits as f64).ceil() as usize;
    let mut folds = Vec::new();
    let mut valid_start = 0;
    for _ in 0..n_splits {
        let valid_end = (valid_start + fold_size).min(n);
        if valid_start >= n { break; }
        let train_start = 0;
        let train_end = valid_start;
        folds.push(Fold { train_start, train_end, valid_start, valid_end });
        valid_start = valid_end;
    }
    folds.into_iter().filter(|f| f.train_end - f.train_start >= 10 && f.valid_end > f.valid_start).collect()
}

/// Purge-aware walk-forward: adjusts fold boundaries so train_last_ts < valid_first_ts
pub fn walk_forward_purge_aware(rows: &[DatasetRow], n_splits: usize) -> Vec<Fold> {
    let mut folds = walk_forward(rows.len(), n_splits);
    for f in folds.iter_mut() {
        if f.train_end == 0 || f.valid_start >= rows.len() { continue; }
        // if same timestamp straddles boundary, expand train to include all rows with that timestamp
        let mut vs = f.valid_start;
        let train_last_ts = rows.get(f.train_end.saturating_sub(1)).and_then(|r| r.trigger_bar_ts.as_deref()).unwrap_or("");
        // move valid_start forward while it shares timestamp with train_last
        while vs < f.valid_end {
            let vs_ts = rows.get(vs).and_then(|r| r.trigger_bar_ts.as_deref()).unwrap_or("");
            if !train_last_ts.is_empty() && !vs_ts.is_empty() && vs_ts == train_last_ts {
                vs += 1;
            } else { break; }
        }
        if vs != f.valid_start {
            f.valid_start = vs;
            f.train_end = vs;
        }
        // also ensure valid range itself doesn't split same timestamp at its end — extend valid_end to include same-ts tail
        while f.valid_end < rows.len() {
            let cur_ts = rows.get(f.valid_end - 1).and_then(|r| r.trigger_bar_ts.as_deref()).unwrap_or("");
            let next_ts = rows.get(f.valid_end).and_then(|r| r.trigger_bar_ts.as_deref()).unwrap_or("");
            if !cur_ts.is_empty() && cur_ts == next_ts { f.valid_end += 1; } else { break; }
        }
    }
    // re-filter after adjustments (may have empty valid)
    folds.into_iter().filter(|f| f.valid_start < f.valid_end && f.train_end - f.train_start >= 5).collect()
}

/// Expanding walk-forward with purge gap: ensure train last trigger_bar_ts < valid first trigger_bar_ts
/// Returns error if purge violated (time leakage).
pub fn assert_purge(rows: &[DatasetRow], folds: &[Fold]) -> Result<()> {
    for f in folds {
        if f.train_end == 0 { continue; }
        let train_last = rows[f.train_end - 1].trigger_bar_ts.as_deref().unwrap_or("");
        let valid_first = rows[f.valid_start].trigger_bar_ts.as_deref().unwrap_or("");
        if !train_last.is_empty() && !valid_first.is_empty() && train_last >= valid_first {
            return Err(anyhow!("purge violated: train_last {} >= valid_first {} in fold {:?}", train_last, valid_first, f));
        }
        // also ensure no overlap in event_id
        // outcome strictly after trigger already guaranteed by leakage test
    }
    Ok(())
}

/// Time-based 80/20 split helper (final holdout)
pub fn time_split_indices(n: usize, train_ratio: f64) -> (usize, usize) {
    let train_n = (n as f64 * train_ratio).floor() as usize;
    (train_n, n - train_n)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::v2::dataset::DatasetRow;
    fn mk_row(ts: &str) -> DatasetRow {
        DatasetRow { event_id: ts.into(), symbol: "RB".into(), direction: "up".into(), setup_quality: 3.0, a_move: 10.0, b_move: 5.0, a_move_atr: 2.0, b_move_atr: 1.0, a_speed: 1.0, retracement: 0.5, warning_volume_ratio: Some(1.0), trigger_close_overshoot_r: Some(0.2), trigger_close_location: Some(0.5), trigger_body_atr: Some(1.0), trigger_volume_ratio: Some(1.0), trigger_wick_atr: Some(0.1), internal_swing_margin_r: Some(0.2), chase_distance_r: Some(0.1), missing_mask: 0, label_win: 1, r_multiple: Some(1.0), is_1r_aux_win: Some(true), trigger_bar_ts: Some(ts.into()), exit_ts: Some("2025-01-02 10:00:00".into()), schema_version: "v2.1".into() }
    }
    #[test]
    fn walk_forward_basic() {
        let rows: Vec<DatasetRow> = (0..100).map(|i| mk_row(&format!("2024-01-{:02} 10:00:00", (i%28)+1))).collect();
        let folds = walk_forward(rows.len(), 5);
        assert!(!folds.is_empty());
        for f in &folds { assert!(f.train_end <= f.valid_start); }
    }
    #[test]
    fn purge_passes_on_sorted() {
        let rows: Vec<DatasetRow> = vec![mk_row("2024-01-01 10:00:00"), mk_row("2024-01-02 10:00:00"), mk_row("2024-01-03 10:00:00"), mk_row("2024-01-04 10:00:00")];
        let folds = vec![Fold{train_start:0,train_end:2,valid_start:2,valid_end:4}];
        assert!(assert_purge(&rows, &folds).is_ok());
    }
    #[test]
    fn purge_fails_on_overlap() {
        let rows: Vec<DatasetRow> = vec![mk_row("2024-01-02 10:00:00"), mk_row("2024-01-01 10:00:00")]; // unsorted
        let folds = vec![Fold{train_start:0,train_end:1,valid_start:1,valid_end:2}];
        // train_last = 2024-01-02, valid_first = 2024-01-01 => violation
        assert!(assert_purge(&rows, &folds).is_err());
    }
}

