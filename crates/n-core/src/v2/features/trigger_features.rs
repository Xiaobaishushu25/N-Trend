use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct TriggerFeatures {
    /// trigger bar close time string
    pub trigger_bar_ts: String,
    /// close overshoot vs trigger level in R units
    pub close_overshoot_r: Option<f64>,
    /// close location within bar [0,1]
    pub close_location: Option<f64>,
    /// body size / ATR
    pub body_atr: Option<f64>,
    /// volume ratio vs 20-bar mean
    pub volume_ratio: Option<f64>,
    /// OI ratio (if available)
    pub oi_ratio: Option<f64>,
    /// internal swing margin in R
    pub internal_swing_margin_r: Option<f64>,
    /// wick size / ATR
    pub wick_atr: Option<f64>,
    /// chase distance from trigger level in R
    pub chase_distance_r: Option<f64>,
    /// raw close price
    pub close_price: f64,
    /// raw trigger level
    pub trigger_level: f64,
    /// risk per contract (entry - stop abs)
    pub risk: f64,
    pub missing_mask: u32,
}

use crate::analyze::model::Bar;

/// Extract trigger features at Trigger K close.
/// Must be called ONLY after Trigger K has fully closed (no intra-bar peeking).
pub fn extract_trigger_features(
    trigger_bar: &Bar,
    trigger_level: f64,
    risk: f64,
    atr: Option<f64>,
    volume_ratio: Option<f64>,
    oi_ratio: Option<f64>,
    internal_swing_margin: Option<f64>,
) -> TriggerFeatures {
    let atr = atr.unwrap_or(1.0).max(1e-9);
    let range = (trigger_bar.high - trigger_bar.low).max(1e-9);
    let close_location = (trigger_bar.close - trigger_bar.low) / range;
    let body = (trigger_bar.close - trigger_bar.open).abs();
    let body_atr = body / atr;
    let wick = (trigger_bar.high - trigger_bar.open.max(trigger_bar.close)).max(trigger_bar.open.min(trigger_bar.close) - trigger_bar.low);
    let wick_atr = wick / atr;
    let overshoot_r = if risk.abs() > 1e-9 { (trigger_bar.close - trigger_level) / risk } else { 0.0 };
    // For short, overshoot should be negative direction; we keep signed — normalization handles it
    let chase_r = overshoot_r.abs();
    let swing_r = internal_swing_margin.map(|v| if risk.abs()>1e-9 { v / risk } else { 0.0 });

    let mut mask = 0u32;
    if atr <= 0.0 { mask |= 1; }
    if volume_ratio.is_none() { mask |= 2; }
    if oi_ratio.is_none() { mask |= 4; }

    TriggerFeatures {
        trigger_bar_ts: trigger_bar.dt.to_bar_ts(),
        close_overshoot_r: Some(overshoot_r),
        close_location: Some(close_location),
        body_atr: Some(body_atr),
        volume_ratio,
        oi_ratio,
        internal_swing_margin_r: swing_r,
        wick_atr: Some(wick_atr),
        chase_distance_r: Some(chase_r),
        close_price: trigger_bar.close,
        trigger_level,
        risk,
        missing_mask: mask,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::analyze::model::{Bar, DT};
    #[test]
    fn trigger_close_location() {
        let b = Bar{ dt: DT{year:2024, month:1, day:1, hour:10, minute:15}, open:10.0, high:12.0, low:10.0, close:12.0, volume:1000.0, hold:0.0, rollover:false };
        let f = extract_trigger_features(&b, 11.0, 1.0, Some(1.0), Some(1.5), None, Some(0.5));
        assert!((f.close_location.unwrap() - 1.0).abs() < 1e-9);
        assert!((f.close_overshoot_r.unwrap() - 1.0).abs() < 1e-9);
    }
}
