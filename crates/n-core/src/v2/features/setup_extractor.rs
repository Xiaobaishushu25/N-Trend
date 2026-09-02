use serde::{Deserialize, Serialize};

/// Setup raw features — frozen at Warning K close
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct SetupFeatures {
    pub a_move: f64,
    pub b_move: f64,
    pub a_bars: i64,
    pub b_bars: i64,
    pub retracement: f64,
    /// avg a speed = a_move / a_bars
    pub a_speed: f64,
    /// a_move / ATR(S1)
    pub a_move_atr: f64,
    /// b_move / ATR(S2)
    pub b_move_atr: f64,
    pub grade: String,
    pub level: String,
    pub direction: String,
    /// number of strong a-leg bars (relaxed count)
    pub a_strong_count: i64,
    /// Setup quality 0-5 (display only, NOT used as P(win) proxy)
    pub setup_quality: f64,
    /// trend60 snapshot JSON-serializable summary
    pub trend60_state: String,
    /// warning raw features
    pub warning_close_location: Option<f64>,
    pub warning_body_atr: Option<f64>,
    pub warning_wick_ratio: Option<f64>,
    pub warning_volume_ratio: Option<f64>,
    /// direction normalized flag
    pub normalized: bool,
    pub missing_mask: u32,
}

use crate::analyze::model::{Bar, Dir, NPattern, Trend60};

/// Extract setup features from a detected N pattern.
/// This function is the SINGLE source of truth for both Live and Replay paths.
pub fn extract_setup_features(
    pattern: &NPattern,
    bars15: &[Bar],
    atr15: &[Option<f64>],
    trend60: &Trend60,
    setup_quality: f64,
) -> SetupFeatures {
    let atr_s1 = atr15.get(pattern.s1.index).and_then(|x| *x).unwrap_or(1.0);
    let atr_s2 = atr15.get(pattern.s2.index).and_then(|x| *x).unwrap_or(1.0);
    let a_speed = if pattern.a_bars > 0 { pattern.a_move / pattern.a_bars as f64 } else { 0.0 };
    // warning bar is s2 bar itself (end of b leg)
    let wb = bars15.get(pattern.s2.index);
    let (close_loc, body_atr, wick_ratio, vol_ratio) = if let Some(b) = wb {
        let range = (b.high - b.low).max(1e-9);
        let cl = (b.close - b.low) / range;
        let body = (b.close - b.open).abs();
        let ba = if atr_s2 > 1e-9 { body / atr_s2 } else { 0.0 };
        let upper = b.high - b.open.max(b.close);
        let lower = b.open.min(b.close) - b.low;
        let wick = if body > 1e-9 { upper.max(lower) / body } else { 0.0 };
        // volume ratio vs simple 20-bar mean
        let start = pattern.s2.index.saturating_sub(20);
        let mean_vol: f64 = bars15[start..pattern.s2.index].iter().map(|x| x.volume).sum::<f64>() / 20.0_f64.max(1.0);
        let vr = if mean_vol > 1e-9 { b.volume / mean_vol } else { 1.0 };
        (Some(cl), Some(ba), Some(wick), Some(vr))
    } else { (None, None, None, None) };

    let mut mask = 0u32;
    if close_loc.is_none() { mask |= 1; }
    if body_atr.is_none() { mask |= 2; }
    if atr_s1.is_nan() || atr_s1 <= 0.0 { mask |= 4; }

    SetupFeatures {
        a_move: pattern.a_move,
        b_move: pattern.b_move,
        a_bars: pattern.a_bars as i64,
        b_bars: pattern.b_bars as i64,
        retracement: pattern.retracement,
        a_speed,
        a_move_atr: if atr_s1 > 1e-9 { pattern.a_move / atr_s1 } else { 0.0 },
        b_move_atr: if atr_s2 > 1e-9 { pattern.b_move / atr_s2 } else { 0.0 },
        grade: format!("{:?}", pattern.grade),
        level: pattern.level.to_string(),
        direction: match pattern.dir { Dir::Up => "up".to_string(), Dir::Down => "down".to_string() },
        a_strong_count: 0, // filled by caller if available
        setup_quality,
        trend60_state: format!("{:?}", trend60),
        warning_close_location: close_loc,
        warning_body_atr: body_atr,
        warning_wick_ratio: wick_ratio,
        warning_volume_ratio: vol_ratio,
        normalized: false,
        missing_mask: mask,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::analyze::model::{Bar, DT, Dir, Grade, NPattern, Swing, Trend60};

    fn bar(o: f64, h: f64, l: f64, c: f64) -> Bar {
        Bar { dt: DT { year: 2024, month: 1, day: 1, hour: 10, minute: 0 }, open: o, high: h, low: l, close: c, volume: 1000.0, hold: 0.0, rollover: false }
    }
    #[test]
    fn setup_speed_computed() {
        let p = NPattern { level: "fine", dir: Dir::Up, s0: Swing{index:0, price:10.0, is_high:false}, s1: Swing{index:5, price:20.0, is_high:true}, s2: Swing{index:8, price:15.0, is_high:false}, a_move:10.0, b_move:5.0, a_bars:6, b_bars:3, retracement:0.5, grade: Grade::A, hard_failure:false, a_too_long:false, b_too_long:false, b_fast:false, b_weakening:false, b_weakening_ratio:None, a_strong_trend:0, b_strong_reverse:0, c_move:0.0, c_bars:0, c_extended:false, c_hard_failure:false };
        let bars = vec![bar(10.0,11.0,9.0,10.5); 10];
        let atr = vec![Some(1.0); 10];
        let t60 = Trend60 { direction: "UP".to_string(), ma20: 0.0, slope: 0.0, price_vs_ma: 0.0, higher_highs: false, higher_lows: false, lower_highs: false, lower_lows: false };
        let f = extract_setup_features(&p, &bars, &atr, &t60, 3.5);
        assert!((f.a_speed - 1.666).abs() < 0.01);
    }
}

