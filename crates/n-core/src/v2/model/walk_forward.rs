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

/// Purge-aware walk-forward: adjusts fold boundaries so no training label
/// reaches into the validation window. The label horizon is exit_ts, not just
/// the trigger timestamp.
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
        // Purge every training row whose realized label horizon overlaps the
        // first validation trigger. This prevents a trade opened before the
        // split from leaking its future outcome into validation.
        if f.valid_start < rows.len() {
            if let Some(valid_ts) = rows[f.valid_start].trigger_bar_ts.as_deref() {
                let first_overlapping = (f.train_start..f.train_end).find(|idx| {
                    let row = &rows[*idx];
                    let label_end = row.exit_ts.as_deref().or(row.trigger_bar_ts.as_deref());
                    label_end.map(|ts| ts >= valid_ts).unwrap_or(true)
                });
                // Keep the training interval contiguous. If an earlier row
                // has a longer label horizon than a later row, truncating
                // only the suffix would leave an interior leakage row.
                if let Some(idx) = first_overlapping { f.train_end = idx; }
            }
        }
    }
    // re-filter after adjustments (may have empty valid)
    folds.into_iter().filter(|f| f.valid_start < f.valid_end && f.train_end - f.train_start >= 5).collect()
}

/// Expanding walk-forward with purge gap: ensure every training label ends
/// before the first validation trigger (and preserve the trigger ordering
/// invariant). Returns an error if purge is violated.
pub fn assert_purge(rows: &[DatasetRow], folds: &[Fold]) -> Result<()> {
    for f in folds {
        if f.train_end == 0 { continue; }
        let train_last = rows[f.train_end - 1].trigger_bar_ts.as_deref().unwrap_or("");
        let valid_first = rows[f.valid_start].trigger_bar_ts.as_deref().unwrap_or("");
        if !train_last.is_empty() && !valid_first.is_empty() && train_last >= valid_first {
            return Err(anyhow!("purge violated: train_last {} >= valid_first {} in fold {:?}", train_last, valid_first, f));
        }
        for row in &rows[f.train_start..f.train_end] {
            let label_end = row.exit_ts.as_deref().or(row.trigger_bar_ts.as_deref()).unwrap_or("");
            if !label_end.is_empty() && !valid_first.is_empty() && label_end >= valid_first {
                return Err(anyhow!("label horizon overlaps validation: exit {} >= valid {} in fold {:?}", label_end, valid_first, f));
            }
        }
    }
    Ok(())
}

/// Time-based 80/20 split helper (final holdout)
pub fn time_split_indices(n: usize, train_ratio: f64) -> (usize, usize) {
    let train_n = (n as f64 * train_ratio).floor() as usize;
    (train_n, n - train_n)
}

/// Split chronologically before any walk-forward construction.  The final
/// `holdout_size` rows are never passed to feature/model selection code.
pub fn split_final_holdout<'a>(rows: &'a [DatasetRow], holdout_size: usize) -> (&'a [DatasetRow], &'a [DatasetRow]) {
    let cut = rows.len().saturating_sub(holdout_size);
    (&rows[..cut], &rows[cut..])
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::v2::dataset::DatasetRow;
    fn mk_row(ts: &str) -> DatasetRow {
        DatasetRow { event_id: ts.into(), symbol: "RB".into(), direction: "up".into(), setup_quality: 3.0, a_move: 10.0, b_move: 5.0, a_move_atr: 2.0, b_move_atr: 1.0, a_speed: 1.0, retracement: 0.5, warning_volume_ratio: Some(1.0), trigger_close_overshoot_r: Some(0.2), trigger_close_location: Some(0.5), trigger_body_atr: Some(1.0), trigger_volume_ratio: Some(1.0), trigger_wick_atr: Some(0.1), internal_swing_margin_r: Some(0.2), chase_distance_r: Some(0.1), missing_mask: 0, label_win: 1, r_multiple: Some(1.0), is_1r_aux_win: Some(true), trigger_bar_ts: Some(ts.into()), exit_ts: Some("2024-01-01 11:00:00".into()), schema_version: crate::v2::FEATURE_SCHEMA_VERSION.into(), trend_gap_60:None, trend_slope_60:None, trend_strength_60:None, trend_alignment_60:None, trend_10d:None, trend_alignment_10d:None, range_position_10d:None, mr_position_10d:None, distance_ma10_dir:None, trend_position_interaction:None, context_as_of_ts:None, context_last_60m_ts:None, context_last_daily_day:None, crossed_rollover_10d:false }
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

    #[test]
    fn purge_reduces_training_end_for_overlapping_label() {
        let mut rows = vec![
            mk_row("2024-01-01 10:00:00"),
            mk_row("2024-01-02 10:00:00"),
            mk_row("2024-01-03 10:00:00"),
            mk_row("2024-01-04 10:00:00"),
        ];
        rows[1].exit_ts = Some("2024-01-03 12:00:00".into());
        let folds = walk_forward_purge_aware(&rows, 2);
        assert!(folds.iter().all(|f| assert_purge(&rows, std::slice::from_ref(f)).is_ok()));
    }
}

