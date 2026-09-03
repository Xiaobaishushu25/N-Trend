//! Point-in-time market context.
//!
//! Every value in this module is calculated from bars whose close timestamp
//! is no later than `as_of_ts`.  The daily features deliberately use the ten
//! completed trading days before the event's trading day; the current daily
//! bar is never used.

use chrono::{NaiveDate, NaiveDateTime};
use serde::{Deserialize, Serialize};

use crate::analyze::indicators;
use crate::analyze::model::Bar;
use crate::derive::{rollover::RolloverRecord, trading_day};

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct MarketContextSnapshot {
    pub as_of_ts: String,
    pub latest_15m_close_ts: Option<String>,
    pub latest_60m_close_ts: Option<String>,
    pub latest_daily_trading_day: Option<String>,
    pub trend_gap_60: Option<f64>,
    pub trend_slope_60: Option<f64>,
    pub adx_60: Option<f64>,
    pub trend_strength_60: Option<f64>,
    pub trend_alignment_60: Option<f64>,
    pub trend_10d: Option<f64>,
    pub trend_alignment_10d: Option<f64>,
    pub range_position_10d: Option<f64>,
    pub mr_position_10d: Option<f64>,
    pub distance_ma10_dir: Option<f64>,
    pub trend_position_interaction: Option<f64>,
    pub crossed_rollover_10d: bool,
}

/// Extract the causal context for the latest 15m bar at or before `as_of_ts`.
/// `bars60` and `daily_bars` are expected to carry bucket-end timestamps.
pub fn extract_market_context(
    _symbol: &str,
    as_of_ts: &str,
    direction: &str,
    bars15: &[Bar],
    bars60: &[Bar],
    daily_bars: &[Bar],
    rollovers: &[RolloverRecord],
) -> Option<MarketContextSnapshot> {
    let as_of = parse_dt(as_of_ts)?;
    let current_day = trading_day(as_of);
    let eligible15: Vec<&Bar> = bars15.iter().filter(|b| dt(b) <= as_of).collect();
    let latest15 = eligible15.last().copied()?;
    // A 60m bar is usable only after its bucket-end timestamp.  This is the
    // closed-bar policy; no partial 60m bar is synthesized here.
    let eligible60: Vec<&Bar> = bars60.iter().filter(|b| dt(b) <= as_of).collect();
    let prior_daily: Vec<&Bar> = daily_bars
        .iter()
        .filter(|b| {
            let d = date(b);
            d < current_day && dt(b) <= as_of
        })
        .collect();
    let latest_daily = prior_daily.last().copied();
    let crossed_rollover_10d = prior_daily
        .iter()
        .rev()
        .take(10)
        .map(|b| date(b))
        .any(|day| rollovers.iter().any(|r| {
            parse_dt(&r.ts).map(|ts| trading_day(ts) == day).unwrap_or(false)
        }));

    let mut snapshot = MarketContextSnapshot {
        as_of_ts: as_of_ts.to_string(),
        latest_15m_close_ts: Some(latest15.dt.to_bar_ts()),
        latest_60m_close_ts: eligible60.last().map(|b| b.dt.to_bar_ts()),
        latest_daily_trading_day: latest_daily.map(|b| date(b).to_string()),
        trend_gap_60: None,
        trend_slope_60: None,
        adx_60: None,
        trend_strength_60: None,
        trend_alignment_60: None,
        trend_10d: None,
        trend_alignment_10d: None,
        range_position_10d: None,
        mr_position_10d: None,
        distance_ma10_dir: None,
        trend_position_interaction: None,
        crossed_rollover_10d,
    };

    let side = if direction == "down" || direction == "short" { -1.0 } else { 1.0 };
    if eligible60.len() >= 60 {
        let closed: Vec<Bar> = eligible60.into_iter().cloned().collect();
        let atr = indicators::atr(&closed, 20);
        let i = closed.len() - 1;
        if let Some(a) = atr[i].filter(|v| *v > 0.0) {
            let ema20 = ema(&closed, 20);
            let ema60 = ema(&closed, 60);
            if let (Some(e20), Some(e60)) = (ema20[i], ema60[i]) {
                let gap = (e20 - e60) / a;
                snapshot.trend_gap_60 = Some(gap);
                snapshot.trend_alignment_60 = Some(side * gap);
                if i >= 4 {
                    snapshot.trend_slope_60 = Some((e20 - ema20[i - 4].unwrap_or(e20)) / a);
                }
            }
            let adx_value = adx(&closed, 14).get(i).and_then(|v| *v);
            snapshot.adx_60 = adx_value;
            snapshot.trend_strength_60 = adx_value.map(|v| v / 100.0);
        }
    }

    // A rollover-crossing window is excluded from all ten-day context
    // features for this first causal experiment.
    if !crossed_rollover_10d && prior_daily.len() >= 11 {
        let ten = &prior_daily[prior_daily.len() - 10..];
        let first = prior_daily[prior_daily.len() - 11].close;
        let last = ten[9].close;
        let daily_atr = daily_atr(ten);
        if daily_atr > 0.0 {
            let trend = (last - first) / daily_atr;
            snapshot.trend_10d = Some(trend);
            snapshot.trend_alignment_10d = Some(side * trend);
        }
        let high = ten.iter().map(|b| b.high).fold(f64::NEG_INFINITY, f64::max);
        let low = ten.iter().map(|b| b.low).fold(f64::INFINITY, f64::min);
        if high.is_finite() && low.is_finite() && high > low {
            let position = (latest15.close - low) / (high - low);
            snapshot.range_position_10d = Some(position);
            snapshot.mr_position_10d = Some(side * (0.5 - position));
            let ma10 = ten.iter().map(|b| b.close).sum::<f64>() / 10.0;
            snapshot.distance_ma10_dir = Some(side * (latest15.close - ma10) / daily_atr);
        }
    }
    snapshot.trend_position_interaction = snapshot
        .trend_strength_60
        .zip(snapshot.mr_position_10d)
        .map(|(strength, mr)| strength * mr);
    Some(snapshot)
}

fn parse_dt(s: &str) -> Option<NaiveDateTime> {
    NaiveDateTime::parse_from_str(s, "%Y-%m-%d %H:%M:%S")
        .or_else(|_| NaiveDateTime::parse_from_str(s, "%Y-%m-%d %H:%M"))
        .ok()
}

fn dt(bar: &Bar) -> NaiveDateTime {
    NaiveDateTime::parse_from_str(&bar.dt.to_bar_ts(), "%Y-%m-%d %H:%M:%S").unwrap()
}

fn date(bar: &Bar) -> NaiveDate {
    dt(bar).date()
}

fn ema(bars: &[Bar], period: usize) -> Vec<Option<f64>> {
    let mut out = vec![None; bars.len()];
    if bars.len() < period || period == 0 { return out; }
    let mut value = bars[..period].iter().map(|b| b.close).sum::<f64>() / period as f64;
    out[period - 1] = Some(value);
    let alpha = 2.0 / (period as f64 + 1.0);
    for i in period..bars.len() {
        value = alpha * bars[i].close + (1.0 - alpha) * value;
        out[i] = Some(value);
    }
    out
}

fn daily_atr(bars: &[&Bar]) -> f64 {
    if bars.is_empty() { return 0.0; }
    let mut total = 0.0;
    for (i, bar) in bars.iter().enumerate() {
        let tr = if i == 0 { bar.high - bar.low } else {
            let prev = bars[i - 1].close;
            (bar.high - bar.low).max((bar.high - prev).abs()).max((bar.low - prev).abs())
        };
        total += tr;
    }
    total / bars.len() as f64
}

fn adx(bars: &[Bar], period: usize) -> Vec<Option<f64>> {
    let n = bars.len();
    let mut out = vec![None; n];
    if period == 0 || n < period * 2 { return out; }
    let mut tr = vec![0.0; n];
    let mut plus = vec![0.0; n];
    let mut minus = vec![0.0; n];
    for i in 1..n {
        tr[i] = (bars[i].high - bars[i].low)
            .max((bars[i].high - bars[i - 1].close).abs())
            .max((bars[i].low - bars[i - 1].close).abs());
        let up = bars[i].high - bars[i - 1].high;
        let down = bars[i - 1].low - bars[i].low;
        if up > down && up > 0.0 { plus[i] = up; }
        if down > up && down > 0.0 { minus[i] = down; }
    }
    let mut dx = vec![None; n];
    for i in period..n {
        let start = i + 1 - period;
        let atr = tr[start..=i].iter().sum::<f64>();
        if atr <= 0.0 { continue; }
        let pdi = 100.0 * plus[start..=i].iter().sum::<f64>() / atr;
        let mdi = 100.0 * minus[start..=i].iter().sum::<f64>() / atr;
        let denom = pdi + mdi;
        if denom > 0.0 { dx[i] = Some(100.0 * (pdi - mdi).abs() / denom); }
    }
    for i in (period * 2 - 1)..n {
        let values: Vec<f64> = dx[i + 1 - period..=i].iter().filter_map(|v| *v).collect();
        if values.len() == period { out[i] = Some(values.iter().sum::<f64>() / period as f64); }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::analyze::model::DT;

    fn bars(n: usize, scale: f64) -> Vec<Bar> {
        (0..n).map(|i| Bar { dt: DT { year: 2024, month: 1, day: 1 + (i / 24) as i32, hour: (i % 24) as i32, minute: 0 }, open: 100.0 + i as f64 * scale, high: 101.0 + i as f64 * scale, low: 99.0 + i as f64 * scale, close: 100.0 + i as f64 * scale, volume: 1.0, hold: 1.0, rollover: false }).collect()
    }

    #[test]
    fn future_mutation_and_deletion_do_not_change_snapshot() {
        let b15 = bars(500, 0.1);
        let b60 = bars(500, 0.2);
        let daily = bars(30 * 24, 0.5);
        let as_of = b15[400].dt.to_bar_ts();
        let before = extract_market_context("RB0", &as_of, "up", &b15, &b60, &daily, &[]).unwrap();
        let mut mutated = b15.clone();
        for bar in mutated.iter_mut().skip(401) { bar.high *= 10.0; bar.low *= 10.0; bar.close *= 10.0; }
        let after_mutation = extract_market_context("RB0", &as_of, "up", &mutated, &b60, &daily, &[]).unwrap();
        assert_eq!(before, after_mutation);
        let deleted = extract_market_context("RB0", &as_of, "up", &b15[..=400], &b60, &daily, &[]).unwrap();
        assert_eq!(before, deleted);
    }

    #[test]
    fn future_60m_bar_is_not_visible() {
        let b15 = bars(500, 0.1);
        let b60 = bars(500, 0.2);
        let daily = bars(30 * 24, 0.5);
        let as_of = b15[400].dt.to_bar_ts();
        let mut mutated = b60.clone();
        for bar in mutated.iter_mut().skip(401) { bar.close *= 10.0; }
        let a = extract_market_context("RB0", &as_of, "up", &b15, &b60, &daily, &[]).unwrap();
        let b = extract_market_context("RB0", &as_of, "up", &b15, &mutated, &daily, &[]).unwrap();
        assert_eq!(a, b);
    }

    #[test]
    fn rollover_crossing_disables_previous_ten_day_features() {
        let b15 = bars(500, 0.1);
        let b60 = bars(500, 0.2);
        let daily = bars(30 * 24, 0.5);
        let rollover = RolloverRecord {
            symbol: "RB0".into(), ts: "2024-01-15 21:00:00".into(),
            from_contract: "RB2405".into(), to_contract: "RB2409".into(), confirmed: true,
        };
        let snapshot = extract_market_context("RB0", &b15[400].dt.to_bar_ts(), "up", &b15, &b60, &daily, &[rollover]).unwrap();
        assert!(snapshot.crossed_rollover_10d);
        assert!(snapshot.trend_10d.is_none());
        assert!(snapshot.range_position_10d.is_none());
        assert!(snapshot.mr_position_10d.is_none());
    }
}
