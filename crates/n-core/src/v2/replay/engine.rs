use anyhow::Result;
use serde::{Deserialize, Serialize};
use crate::analyze::model::Bar;
use crate::derive::{aggregate, Timeframe};
use crate::fetch::kline::Kline;
use crate::derive::rollover::RolloverRecord;
use crate::v2::features::{extract_market_context, extract_trigger_features, MarketContextSnapshot, SetupFeatures, TriggerFeatures};
use crate::v2::{FEATURE_SCHEMA_VERSION, PATTERN_LOGIC_VERSION, EXECUTION_VERSION};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ReplayConfig {
    pub trigger_timeout_bars: usize,
    pub stop_target_lookahead: usize,
}

impl Default for ReplayConfig {
    fn default() -> Self {
        Self {
            // Keep the historical replay window equal to the live pending
            // window.  A larger replay-only timeout changes the population of
            // triggered events and makes the backtest incomparable to live.
            trigger_timeout_bars: crate::analyze::outcome::PENDING_BARS,
            stop_target_lookahead: crate::analyze::outcome::TIME_HORIZON_BARS,
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ReplayEvent {
    pub symbol: String,
    pub direction: String,
    pub grade: String,
    pub level: String,
    pub s0_ts: String,
    pub s1_ts: String,
    pub s2_ts: String,
    pub s0_price: f64,
    pub s1_price: f64,
    pub s2_price: f64,
    pub a_move: f64,
    pub b_move: f64,
    pub a_bars: i64,
    pub b_bars: i64,
    pub retracement: f64,
    pub warning_ts: String,
    pub warning_kind: String,
    pub entry: f64,
    pub stop: f64,
    pub target: f64,
    pub risk: f64,
    pub rr: f64,
    /// Warning candle extreme used by the trigger rule; distinct from entry.
    pub warning_extreme: f64,
    pub tick_size: f64,
    pub trigger_level: f64,
    pub setup_features: SetupFeatures,
    pub trigger_features: Option<TriggerFeatures>,
    pub trigger_bar_ts: Option<String>,
    pub entry_ts: Option<String>,
    pub entry_price: Option<f64>,
    pub outcome: Option<ReplayOutcome>,
    pub market_context: Option<MarketContextSnapshot>,
    pub schema_version: String,
    pub pattern_version: String,
    pub execution_version: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ReplayOutcome {
    pub outcome: String, // win / loss
    pub exit_reason: String,
    pub exit_ts: String,
    pub exit_price: f64,
    pub r_multiple: f64,
    pub mfe_r: Option<f64>,
    pub mae_r: Option<f64>,
    pub is_1r_aux_win: Option<bool>,
}

/// Minimal replay engine — single source of truth for feature computation
pub struct ReplayEngine {
    pub config: ReplayConfig,
}

impl ReplayEngine {
    pub fn new(config: ReplayConfig) -> Self { Self { config } }
    pub fn default_engine() -> Self { Self::new(ReplayConfig::default()) }

    /// Aggregate raw 5m klines to 15m/60m bars (reuses derive::aggregate)
    pub fn aggregate_bars(&self, raw5m: &[Kline]) -> (Vec<Bar>, Vec<Bar>) {
        let bars15_k = aggregate(raw5m, Timeframe::M15);
        let bars60_k = aggregate(raw5m, Timeframe::M60);
        (kline_to_bar(&bars15_k), kline_to_bar(&bars60_k))
    }

    /// Replay history using the same forward warning extractor as live scans.
    /// Returns deduped events (symbol+s0/s1/s2+direction earliest warning wins).
    pub fn replay_history(&self, symbol: &str, raw5m: &[Kline], tick_size: f64) -> Result<Vec<ReplayEvent>> {
        self.replay_history_with_rollovers(symbol, raw5m, tick_size, &[])
    }

    /// Replay with the point-in-time rollover records known at the event
    /// cutoff.  The three-argument method remains a safe empty-marker helper
    /// for callers that do not have contract metadata.
    pub fn replay_history_with_rollovers(
        &self,
        symbol: &str,
        raw5m: &[Kline],
        tick_size: f64,
        rollovers: &[RolloverRecord],
    ) -> Result<Vec<ReplayEvent>> {
        let (bars15, bars60) = self.aggregate_bars(raw5m);
        let daily_bars = kline_to_bar(&aggregate(raw5m, Timeframe::Day));
        if bars15.len() < 30 { return Ok(vec![]); }
        let candidates = crate::analyze::event::replay_warnings(symbol, &bars15, tick_size);
        let mut events: Vec<ReplayEvent> = Vec::with_capacity(candidates.len());
        for candidate in candidates {
            let is_long = candidate.direction == crate::analyze::model::Dir::Up;
            let direction = if is_long { "up" } else { "down" };
            let warning_bar = &bars15[candidate.warning_index];
            let warning_extreme = if is_long { warning_bar.high } else { warning_bar.low };
            let s0_price = if is_long { bars15[candidate.s0_index].low } else { bars15[candidate.s0_index].high };
            let s1_price = if is_long { bars15[candidate.s1_index].high } else { bars15[candidate.s1_index].low };
            let s2_price = if is_long { bars15[candidate.s2_index].low } else { bars15[candidate.s2_index].high };
            let atr = crate::analyze::indicators::atr(&bars15[..=candidate.warning_index], 20);
            let a_atr = atr.get(candidate.s1_index).and_then(|x| *x).unwrap_or(0.0);
            let b_atr = atr.get(candidate.s2_index).and_then(|x| *x).unwrap_or(0.0);
            let warning_range = (warning_bar.high - warning_bar.low).max(1e-9);
            let warning_body = (warning_bar.close - warning_bar.open).abs();
            let warning_upper = warning_bar.high - warning_bar.open.max(warning_bar.close);
            let warning_lower = warning_bar.open.min(warning_bar.close) - warning_bar.low;
            let sf = SetupFeatures {
                a_move: candidate.a_move,
                b_move: candidate.b_move,
                a_bars: candidate.a_bars as i64,
                b_bars: candidate.b_bars as i64,
                retracement: candidate.retracement,
                a_speed: if candidate.a_bars > 0 { candidate.a_move / candidate.a_bars as f64 } else { 0.0 },
                a_move_atr: if a_atr > 1e-9 { candidate.a_move / a_atr } else { 0.0 },
                b_move_atr: if b_atr > 1e-9 { candidate.b_move / b_atr } else { 0.0 },
                grade: candidate.grade.clone(),
                level: candidate.level.to_string(),
                direction: direction.to_string(),
                a_strong_count: 0,
                setup_quality: candidate.entry_score,
                // replay_warnings is also the live event generator.  Keep
                // the exact snapshot it computed instead of recomputing a
                // different 60m approximation here.
                trend60_state: candidate.trend_state.clone(),
                warning_close_location: Some((warning_bar.close - warning_bar.low) / warning_range),
                warning_body_atr: Some(warning_body / b_atr.max(1e-9)),
                warning_wick_ratio: Some(if warning_body > 1e-9 { warning_upper.max(warning_lower) / warning_body } else { 0.0 }),
                warning_volume_ratio: crate::analyze::outcome::vol_ratio_at(&bars15, candidate.warning_index),
                normalized: false,
                missing_mask: 0,
            };
            let warning_ts = warning_bar.dt.to_bar_ts();
            events.push(ReplayEvent {
                symbol: symbol.into(), direction: direction.into(), grade: candidate.grade.clone(), level: candidate.level.to_string(),
                s0_ts: bars15[candidate.s0_index].dt.to_bar_ts(), s1_ts: bars15[candidate.s1_index].dt.to_bar_ts(), s2_ts: bars15[candidate.s2_index].dt.to_bar_ts(),
                s0_price, s1_price, s2_price, a_move: candidate.a_move, b_move: candidate.b_move,
                a_bars: candidate.a_bars as i64, b_bars: candidate.b_bars as i64, retracement: candidate.retracement,
                warning_ts, warning_kind: candidate.warning_kind.to_string(), entry: candidate.entry, stop: candidate.stop,
                target: candidate.target, risk: candidate.risk, rr: candidate.rr, warning_extreme, tick_size,
                trigger_level: candidate.entry, setup_features: sf, trigger_features: None, trigger_bar_ts: None,
                entry_ts: None, entry_price: None, outcome: None, schema_version: FEATURE_SCHEMA_VERSION.into(),
                market_context: None,
                pattern_version: PATTERN_LOGIC_VERSION.into(), execution_version: EXECUTION_VERSION.into(),
            });
        }
        // second pass: for each event, use the same entry-touch trigger and
        // holding rules as the live event state machine.
        for ev in events.iter_mut() {
            let warn_idx_opt = bars15.iter().position(|b| bar_ts_matches(&b.dt.to_bar_ts(), &ev.warning_ts));
            let s2_idx_opt = bars15.iter().position(|b| bar_ts_matches(&b.dt.to_bar_ts(), &ev.s2_ts));
            let Some(start_idx) = warn_idx_opt.or(s2_idx_opt) else { continue; };
            let is_long = ev.direction == "up";
            let mut trigger_idx: Option<usize> = None;
            let end = (start_idx + 1 + self.config.trigger_timeout_bars).min(bars15.len());
            for j in start_idx + 1..end {
                let b = &bars15[j];
                let touched = if is_long { b.high >= ev.entry } else { b.low <= ev.entry };
                if touched { trigger_idx = Some(j); break; }
            }
            if let Some(ti) = trigger_idx {
                let tb = &bars15[ti];
                let risk = ev.risk.abs().max(1e-9);
                let atr = crate::analyze::indicators::atr(&bars15, 20).get(ti).and_then(|x| *x);
                let vol_ratio = crate::analyze::outcome::vol_ratio_at(&bars15, ti);
                let tf = extract_trigger_features(tb, ev.entry, risk, atr, vol_ratio, None, None);
                ev.trigger_features = Some(tf.clone());
                ev.trigger_bar_ts = Some(tb.dt.to_bar_ts());
                let mut fill = ev.entry;
                if ti > 0 && !bars15[ti - 1].rollover {
                    let prev_close = bars15[ti - 1].close;
                    let cur_open = tb.open;
                    let gap = if is_long { prev_close < ev.entry && cur_open > ev.entry } else { prev_close > ev.entry && cur_open < ev.entry };
                    if gap { fill = cur_open; }
                }
                ev.entry_ts = Some(tb.dt.to_bar_ts());
                ev.entry_price = Some(fill);
                ev.market_context = extract_market_context(
                    symbol,
                    &tb.dt.to_bar_ts(),
                    &ev.direction,
                    &bars15,
                    &bars60,
                    &daily_bars,
                    rollovers,
                );

                let mut exit: Option<(usize, f64, &str)> = None;
                let base_tp = if is_long { fill + risk } else { fill - risk };
                let look = (ti + crate::analyze::outcome::TIME_HORIZON_BARS).min(bars15.len());
                let mut mfe = 0.0_f64;
                let mut mae = 0.0_f64;
                let mut trail_grade: Option<usize> = None;
                for k in ti..look {
                    let b = &bars15[k];
                    if b.rollover {
                        exit = None;
                        break;
                    }
                    let mfe_contrib = if is_long { (b.high - fill) / risk } else { (fill - b.low) / risk };
                    let mae_contrib = if is_long { (b.low - fill) / risk } else { (fill - b.high) / risk };
                    mfe = mfe.max(mfe_contrib);
                    mae = mae.min(mae_contrib);
                    let hit_stop = if is_long { b.low <= ev.stop } else { b.high >= ev.stop };
                    let reached_tp1 = if is_long { b.high >= base_tp } else { b.low <= base_tp };
                    if hit_stop && reached_tp1 {
                        match resolve_intrabar_exit(raw5m, &b.dt, ev.stop, base_tp, is_long) {
                            IntrabarExit::Stop => { exit = Some((k, ev.stop, "stop")); break; }
                            IntrabarExit::Ambiguous => break,
                            IntrabarExit::Target => {}
                        }
                    } else if hit_stop {
                        exit = Some((k, ev.stop, "stop"));
                        break;
                    }
                    if trail_grade.is_none() && reached_tp1 { trail_grade = Some(1); }
                    if let Some(mut grade) = trail_grade {
                        loop {
                            let next_grade = grade + 1;
                            let next_price = if is_long { fill + trail_r(next_grade) * risk } else { fill - trail_r(next_grade) * risk };
                            let next_hit = if is_long { b.high >= next_price } else { b.low <= next_price };
                            if !next_hit { break; }
                            grade = next_grade;
                        }
                        trail_grade = Some(grade);
                        let trail_price = if is_long { fill + trail_r(grade) * risk } else { fill - trail_r(grade) * risk };
                        let fell_back = if is_long { b.low <= trail_price } else { b.high >= trail_price };
                        if fell_back { exit = Some((k, trail_price, "target")); break; }
                    }
                    if k - ti + 1 >= crate::analyze::outcome::NO_FOLLOW_BAR && mfe < crate::analyze::outcome::NO_FOLLOW_MFE_R {
                        exit = Some((k, b.close, "no_follow"));
                        break;
                    }
                    if k == ti + crate::analyze::outcome::TIME_HORIZON_BARS - 1 {
                        exit = Some((k, b.close, "time_exit"));
                        break;
                    }
                }
                if let Some((ei, ep, reason)) = exit {
                    let r_mult = if is_long { (ep - fill) / risk } else { (fill - ep) / risk };
                    let outcome = if r_mult > 0.0 { "win" } else { "loss" };
                    ev.outcome = Some(ReplayOutcome {
                        outcome: outcome.into(), exit_reason: reason.into(), exit_ts: bars15[ei].dt.to_bar_ts(), exit_price: ep,
                        r_multiple: r_mult, mfe_r: Some(mfe), mae_r: Some(mae), is_1r_aux_win: Some(mfe >= 1.0),
                    });
                }
            }
        }
        Ok(events)
    }
}

fn trail_r(grade: usize) -> f64 {
    if grade == 1 { 1.0 } else { crate::analyze::outcome::TRAIL_STEP_R * grade as f64 }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum IntrabarExit { Stop, Target, Ambiguous }

/// Resolve a same-15m-bar stop/target collision in chronological 5m order.
/// A missing or intrinsically ambiguous 5m sequence is deliberately treated
/// as unknown so DatasetBuilder excludes it from supervised training labels.
fn resolve_intrabar_exit(raw5m: &[Kline], bucket: &crate::analyze::model::DT, stop: f64, target: f64, is_long: bool) -> IntrabarExit {
    for k in raw5m {
        let Some(dt) = crate::analyze::model::DT::from_bar_ts(&k.datetime) else { continue; };
        if dt.year != bucket.year || dt.month != bucket.month || dt.day != bucket.day || dt.hour != bucket.hour || dt.minute / 15 != bucket.minute / 15 {
            continue;
        }
        let hit_stop = if is_long { k.low <= stop } else { k.high >= stop };
        let hit_target = if is_long { k.high >= target } else { k.low <= target };
        match (hit_stop, hit_target) {
            (true, true) => return IntrabarExit::Ambiguous,
            (true, false) => return IntrabarExit::Stop,
            (false, true) => return IntrabarExit::Target,
            (false, false) => {}
        }
    }
    IntrabarExit::Ambiguous
}

fn bar_ts_matches(bar_ts: &str, dto_ts: &str) -> bool {
    // dto ts is "YYYY-MM-DD HH:MM" without seconds, bar_ts is "YYYY-MM-DD HH:MM:SS"
    bar_ts == dto_ts || bar_ts.starts_with(dto_ts) || format!("{}:00", dto_ts) == bar_ts
}

fn kline_to_bar(klines: &[Kline]) -> Vec<Bar> {
    klines.iter().map(|k| {
        let dt = crate::analyze::model::DT::from_bar_ts(&k.datetime).unwrap_or(crate::analyze::model::DT{year:2024, month:1, day:1, hour:0, minute:0});
        Bar{ dt, open: k.open, high: k.high, low: k.low, close: k.close, volume: k.volume, hold: k.hold, rollover: false }
    }).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn empty_raw_returns_empty() {
        let eng = ReplayEngine::default_engine();
        let v = eng.replay_history("test", &[], 1.0).unwrap();
        assert!(v.is_empty());
    }

    fn kline(ts: &str, high: f64, low: f64) -> Kline {
        Kline { datetime: ts.into(), open: 100.0, high, low, close: 100.0, volume: 1.0, hold: 0.0 }
    }

    #[test]
    fn same_15m_bar_is_resolved_by_5m_order() {
        let bucket = crate::analyze::model::DT::from_bar_ts("2025-01-01 10:00:00").unwrap();
        let raw = vec![kline("2025-01-01 10:00:00", 101.0, 99.0), kline("2025-01-01 10:05:00", 102.0, 100.5)];
        assert_eq!(resolve_intrabar_exit(&raw, &bucket, 99.5, 101.5, true), IntrabarExit::Stop);
        assert_eq!(resolve_intrabar_exit(&raw, &bucket, 98.5, 100.5, true), IntrabarExit::Target);
    }

    #[test]
    fn same_5m_bar_is_ambiguous() {
        let bucket = crate::analyze::model::DT::from_bar_ts("2025-01-01 10:00:00").unwrap();
        let raw = vec![kline("2025-01-01 10:00:00", 102.0, 98.0)];
        assert_eq!(resolve_intrabar_exit(&raw, &bucket, 99.5, 101.5, true), IntrabarExit::Ambiguous);
    }
}







