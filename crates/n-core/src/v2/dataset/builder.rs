use serde::{Deserialize, Serialize};
use crate::v2::replay::ReplayEvent;
use crate::v2::features::normalized::normalize_direction;
use crate::v2::model::scaler::get_feature;

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
    // Point-in-time market context. RR is intentionally not included yet.
    pub trend_gap_60: Option<f64>,
    pub trend_slope_60: Option<f64>,
    pub trend_strength_60: Option<f64>,
    pub trend_alignment_60: Option<f64>,
    pub trend_10d: Option<f64>,
    pub trend_alignment_10d: Option<f64>,
    pub range_position_10d: Option<f64>,
    pub mr_position_10d: Option<f64>,
    pub distance_ma10_dir: Option<f64>,
    pub trend_position_interaction: Option<f64>,
    // Audit fields make the causal cutoff inspectable in every dataset row.
    pub context_as_of_ts: Option<String>,
    pub context_last_60m_ts: Option<String>,
    pub context_last_daily_day: Option<String>,
    pub crossed_rollover_10d: bool,
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

    pub fn context_whitelist() -> Vec<String> {
        vec![
            "trend_gap_60", "trend_slope_60", "trend_strength_60", "trend_alignment_60",
            "trend_10d", "trend_alignment_10d", "range_position_10d", "mr_position_10d",
            "distance_ma10_dir", "trend_position_interaction",
        ].into_iter().map(str::to_string).collect()
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
                trend_gap_60: ev.market_context.as_ref().and_then(|c| c.trend_gap_60),
                trend_slope_60: ev.market_context.as_ref().and_then(|c| c.trend_slope_60),
                trend_strength_60: ev.market_context.as_ref().and_then(|c| c.trend_strength_60),
                trend_alignment_60: ev.market_context.as_ref().and_then(|c| c.trend_alignment_60),
                trend_10d: ev.market_context.as_ref().and_then(|c| c.trend_10d),
                trend_alignment_10d: ev.market_context.as_ref().and_then(|c| c.trend_alignment_10d),
                range_position_10d: ev.market_context.as_ref().and_then(|c| c.range_position_10d),
                mr_position_10d: ev.market_context.as_ref().and_then(|c| c.mr_position_10d),
                distance_ma10_dir: ev.market_context.as_ref().and_then(|c| c.distance_ma10_dir),
                trend_position_interaction: ev.market_context.as_ref().and_then(|c| c.trend_position_interaction),
                context_as_of_ts: ev.market_context.as_ref().map(|c| c.as_of_ts.clone()),
                context_last_60m_ts: ev.market_context.as_ref().and_then(|c| c.latest_60m_close_ts.clone()),
                context_last_daily_day: ev.market_context.as_ref().and_then(|c| c.latest_daily_trading_day.clone()),
                crossed_rollover_10d: ev.market_context.as_ref().map(|c| c.crossed_rollover_10d).unwrap_or(false),
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
            let feature_values = self.feature_whitelist.iter().map(|name| {
                get_feature(r, name).map(|v| v.to_string()).unwrap_or_else(|| "NULL".to_string())
            }).collect::<Vec<_>>().join(",");
            let s = format!("{}|{}|{}|{}|{}|{}|{}|{}", r.event_id, feature_values, r.label_win, r.missing_mask, r.schema_version, r.context_as_of_ts.as_deref().unwrap_or(""), r.context_last_60m_ts.as_deref().unwrap_or(""), r.context_last_daily_day.as_deref().unwrap_or(""));
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
        let rows = vec![DatasetRow{ event_id:"a".into(), symbol:"RB".into(), direction:"up".into(), setup_quality:3.5, a_move:10.0,b_move:5.0,a_move_atr:5.0,b_move_atr:2.0,a_speed:1.6,retracement:0.5, warning_volume_ratio:Some(1.2), trigger_close_overshoot_r:Some(0.3), trigger_close_location:Some(0.8), trigger_body_atr:Some(1.0), trigger_volume_ratio:Some(1.5), trigger_wick_atr:Some(0.2), internal_swing_margin_r:Some(0.4), chase_distance_r:Some(0.3), missing_mask:0, label_win:1, r_multiple:Some(1.5), is_1r_aux_win:Some(true), trigger_bar_ts:Some("2024-01-01 10:15:00".into()), exit_ts:Some("2024-01-01 11:00:00".into()), schema_version:"v2.1".into(), trend_gap_60:None, trend_slope_60:None, trend_strength_60:None, trend_alignment_60:None, trend_10d:None, trend_alignment_10d:None, range_position_10d:None, mr_position_10d:None, distance_ma10_dir:None, trend_position_interaction:None, context_as_of_ts:None, context_last_60m_ts:None, context_last_daily_day:None, crossed_rollover_10d:false}];
        let h1 = b.hash(&rows);
        let h2 = b.hash(&rows);
        assert_eq!(h1.0, h2.0);
    }
}
