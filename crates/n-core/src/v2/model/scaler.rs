use serde::{Deserialize, Serialize};
use crate::v2::dataset::DatasetRow;

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct StandardScaler {
    pub feature_names: Vec<String>,
    pub means: Vec<f64>,
    pub stds: Vec<f64>,
}

impl StandardScaler {
    pub fn fit(rows: &[DatasetRow], feature_names: &[String]) -> Self {
        let n = feature_names.len();
        let mut means = vec![0.0; n];
        let mut stds = vec![1.0; n];
        if rows.is_empty() { return Self { feature_names: feature_names.to_vec(), means, stds }; }
        for (j, name) in feature_names.iter().enumerate() {
            let vals: Vec<f64> = rows.iter().filter_map(|r| get_feature(r, name)).collect();
            if vals.is_empty() { continue; }
            let m = vals.iter().sum::<f64>() / vals.len() as f64;
            let var = vals.iter().map(|v| (v - m).powi(2)).sum::<f64>() / vals.len() as f64;
            let s = var.sqrt().max(1e-9);
            means[j] = m;
            stds[j] = s;
        }
        Self { feature_names: feature_names.to_vec(), means, stds }
    }
    pub fn transform_row(&self, row: &DatasetRow) -> Vec<f64> {
        self.feature_names.iter().enumerate().map(|(j, name)| {
            if let Some(v) = get_feature(row, name) {
                (v - self.means[j]) / self.stds[j]
            } else { 0.0 }
        }).collect()
    }
    pub fn transform_slice(&self, feats: &[f64]) -> Vec<f64> {
        feats.iter().enumerate().map(|(j, v)| (v - self.means[j]) / self.stds[j]).collect()
    }
    pub fn transform_features(&self, features: &[f64]) -> Vec<f64> { self.transform_slice(features) }
}

pub fn get_feature(row: &DatasetRow, name: &str) -> Option<f64> {
    match name {
        "direction_num" => Some(if row.direction == "up" { 1.0 } else { -1.0 }),
        "a_move_atr" => Some(row.a_move_atr),
        "b_move_atr" => Some(row.b_move_atr),
        "a_speed" => Some(row.a_speed),
        "retracement" => Some(row.retracement),
        "setup_quality" => Some(row.setup_quality),
        "a_move" => Some(row.a_move),
        "b_move" => Some(row.b_move),
        "warning_volume_ratio" => row.warning_volume_ratio,
        "trigger_close_overshoot_r" => row.trigger_close_overshoot_r,
        "trigger_close_location" => row.trigger_close_location,
        "trigger_body_atr" => row.trigger_body_atr,
        "trigger_volume_ratio" => row.trigger_volume_ratio,
        "trigger_wick_atr" => row.trigger_wick_atr,
        "internal_swing_margin_r" => row.internal_swing_margin_r,
        "chase_distance_r" => row.chase_distance_r,
        "trend_gap_60" => row.trend_gap_60,
        "trend_slope_60" => row.trend_slope_60,
        "trend_strength_60" => row.trend_strength_60,
        "trend_alignment_60" => row.trend_alignment_60,
        "trend_10d" => row.trend_10d,
        "trend_alignment_10d" => row.trend_alignment_10d,
        "range_position_10d" => row.range_position_10d,
        "mr_position_10d" => row.mr_position_10d,
        "distance_ma10_dir" => row.distance_ma10_dir,
        "trend_position_interaction" => row.trend_position_interaction,
        _ => None,
    }
}

pub fn row_to_vec(row: &DatasetRow, feature_names: &[String]) -> Vec<Option<f64>> {
    feature_names.iter().map(|n| get_feature(row, n)).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::v2::dataset::DatasetRow;
    fn mk_row(a_atr: f64, ret: f64) -> DatasetRow {
        DatasetRow { event_id: "x".into(), symbol: "RB".into(), direction: "up".into(), setup_quality: 3.0, a_move: 10.0, b_move: 5.0, a_move_atr: a_atr, b_move_atr: 1.0, a_speed: 2.0, retracement: ret, warning_volume_ratio: Some(1.0), trigger_close_overshoot_r: Some(0.2), trigger_close_location: Some(0.5), trigger_body_atr: Some(1.0), trigger_volume_ratio: Some(1.0), trigger_wick_atr: Some(0.3), internal_swing_margin_r: Some(0.2), chase_distance_r: Some(0.1), missing_mask: 0, label_win: 1, r_multiple: Some(1.0), is_1r_aux_win: Some(true), trigger_bar_ts: Some("2024-01-01 10:00:00".into()), exit_ts: Some("2024-01-01 11:00:00".into()), schema_version: crate::v2::FEATURE_SCHEMA_VERSION.into(), trend_gap_60:None, trend_slope_60:None, trend_strength_60:None, trend_alignment_60:None, trend_10d:None, trend_alignment_10d:None, range_position_10d:None, mr_position_10d:None, distance_ma10_dir:None, trend_position_interaction:None, context_as_of_ts:None, context_last_60m_ts:None, context_last_daily_day:None, crossed_rollover_10d:false }
    }
    #[test]
    fn scaler_mean_std() {
        let rows = vec![mk_row(2.0, 0.4), mk_row(4.0, 0.6), mk_row(6.0, 0.5)];
        let sc = StandardScaler::fit(&rows, &["a_move_atr".into(), "retracement".into()]);
        assert!((sc.means[0] - 4.0).abs() < 1e-9);
        assert!((sc.means[1] - 0.5).abs() < 1e-9);
        assert!(sc.stds[0] > 1.0);
        let v = sc.transform_row(&rows[0]);
        assert!(v[0] < 0.0);
    }
    #[test]
    fn missing_fills_zero() {
        let rows = vec![mk_row(2.0, 0.4)];
        let sc = StandardScaler::fit(&rows, &["trigger_volume_ratio".into()]);
        let mut r = mk_row(2.0, 0.4);
        r.trigger_volume_ratio = None;
        let v = sc.transform_row(&r);
        assert_eq!(v[0], 0.0);
    }
}
