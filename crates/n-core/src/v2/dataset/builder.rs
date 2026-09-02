use serde::{Deserialize, Serialize};
use crate::v2::replay::ReplayEvent;
use crate::v2::features::normalized::normalize_direction;

/// One training row — Setup + Trigger raw features + label
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct DatasetRow {
    pub event_id: String,
    pub symbol: String,
    pub direction: String,
    pub setup_quality: f64,
    pub a_move: f64,
    pub b_move: f64,
    pub a_move_atr: f64,
    pub b_move_atr: f64,
    pub a_speed: f64,
    pub retracement: f64,
    pub warning_volume_ratio: Option<f64>,
    pub trigger_close_overshoot_r: Option<f64>,
    pub trigger_close_location: Option<f64>,
    pub trigger_body_atr: Option<f64>,
    pub trigger_volume_ratio: Option<f64>,
    pub trigger_wick_atr: Option<f64>,
    pub internal_swing_margin_r: Option<f64>,
    pub chase_distance_r: Option<f64>,
    pub missing_mask: u32,
    pub label_win: i32, // 1 win 0 loss
    pub r_multiple: Option<f64>,
    pub is_1r_aux_win: Option<bool>,
    pub trigger_bar_ts: Option<String>,
    pub exit_ts: Option<String>,
    pub schema_version: String,
}

#[derive(Clone, Debug)]
pub struct DatasetHash(pub String);

pub struct DatasetBuilder {
    pub feature_whitelist: Vec<String>,
}

impl DatasetBuilder {
    pub fn new(whitelist: Vec<String>) -> Self { Self { feature_whitelist: whitelist } }
    pub fn default_whitelist() -> Vec<String> {
        vec!["a_move_atr","b_move_atr","a_speed","retracement","warning_volume_ratio","trigger_close_overshoot_r","trigger_close_location","trigger_body_atr","trigger_volume_ratio","trigger_wick_atr","internal_swing_margin_r","chase_distance_r"].into_iter().map(|s| s.to_string()).collect()
    }
    /// Build rows from replay events — only events with trigger+outcome
    pub fn build(&self, events: Vec<ReplayEvent>) -> Vec<DatasetRow> {
        let mut rows = Vec::new();
        for ev in events {
            if ev.trigger_features.is_none() || ev.outcome.is_none() { continue; }
            let tf = ev.trigger_features.clone().unwrap();
            let out = ev.outcome.clone().unwrap();
            // clone for normalization
            let mut sf = ev.setup_features.clone();
            let mut tf2 = tf.clone();
            normalize_direction(&mut sf, Some(&mut tf2));
            let row = DatasetRow {
                event_id: format!("{}|{}|{}", ev.symbol, ev.s0_ts, ev.s2_ts),
                symbol: ev.symbol.clone(), direction: sf.direction.clone(), setup_quality: sf.setup_quality,
                a_move: sf.a_move, b_move: sf.b_move, a_move_atr: sf.a_move_atr, b_move_atr: sf.b_move_atr, a_speed: sf.a_speed, retracement: sf.retracement,
                warning_volume_ratio: sf.warning_volume_ratio,
                trigger_close_overshoot_r: tf2.close_overshoot_r, trigger_close_location: tf2.close_location,
                trigger_body_atr: tf2.body_atr, trigger_volume_ratio: tf2.volume_ratio, trigger_wick_atr: tf2.wick_atr,
                internal_swing_margin_r: tf2.internal_swing_margin_r, chase_distance_r: tf2.chase_distance_r,
                missing_mask: sf.missing_mask | tf2.missing_mask,
                label_win: if out.outcome=="win" {1} else {0}, r_multiple: Some(out.r_multiple), is_1r_aux_win: out.is_1r_aux_win,
                trigger_bar_ts: ev.trigger_bar_ts.clone(), exit_ts: Some(out.exit_ts.clone()), schema_version: ev.schema_version.clone(),
            };
            // whitelist drop: if whitelist non-empty, we keep row but caller can filter columns on export
            rows.push(row);
        }
        // time sort for walk-forward stability
        rows.sort_by(|a,b| a.trigger_bar_ts.cmp(&b.trigger_bar_ts));
        rows
    }
    /// blake3 hash over (features + labels + config)
    pub fn hash(&self, rows: &[DatasetRow]) -> DatasetHash {
        let mut hasher = blake3::Hasher::new();
        hasher.update(self.feature_whitelist.join(",").as_bytes());
        for r in rows {
            let s = format!("{}|{}|{}|{}|{}|{}|{}", r.event_id, r.a_move_atr, r.retracement, r.trigger_close_overshoot_r.unwrap_or(0.0), r.label_win, r.missing_mask, r.schema_version);
            hasher.update(s.as_bytes());
        }
        DatasetHash(hasher.finalize().to_hex().to_string())
    }
    /// Filter rows by missing policy — discard if ATR missing bit set
    pub fn filter_missing(rows: Vec<DatasetRow>) -> (Vec<DatasetRow>, usize) {
        let mut kept = Vec::new();
        let mut dropped = 0;
        for r in rows {
            if (r.missing_mask & 4) != 0 { dropped += 1; continue; }
            kept.push(r);
        }
        (kept, dropped)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn hash_is_deterministic() {
        let b = DatasetBuilder::new(DatasetBuilder::default_whitelist());
        let rows = vec![DatasetRow{ event_id:"a".into(), symbol:"RB".into(), direction:"up".into(), setup_quality:3.5, a_move:10.0,b_move:5.0,a_move_atr:5.0,b_move_atr:2.0,a_speed:1.6,retracement:0.5, warning_volume_ratio:Some(1.2), trigger_close_overshoot_r:Some(0.3), trigger_close_location:Some(0.8), trigger_body_atr:Some(1.0), trigger_volume_ratio:Some(1.5), trigger_wick_atr:Some(0.2), internal_swing_margin_r:Some(0.4), chase_distance_r:Some(0.3), missing_mask:0, label_win:1, r_multiple:Some(1.5), is_1r_aux_win:Some(true), trigger_bar_ts:Some("2024-01-01 10:15:00".into()), exit_ts:Some("2024-01-01 11:00:00".into()), schema_version:"v2.1".into()}];
        let h1 = b.hash(&rows);
        let h2 = b.hash(&rows);
        assert_eq!(h1.0, h2.0);
    }
}
