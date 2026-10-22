use crate::analyze::model::Bar;
use crate::derive::rollover::RolloverRecord;
use crate::derive::{aggregate, Timeframe};
use crate::fetch::kline::Kline;
use crate::v2::features::{
    extract_market_context, MarketContextSnapshot, SetupFeatures, TriggerFeatures,
};
use crate::v2::{EXECUTION_VERSION, FEATURE_SCHEMA_VERSION, PATTERN_LOGIC_VERSION};
use anyhow::Result;
use serde::{Deserialize, Serialize};

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

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
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
    #[serde(default)]
    pub evaluation: Option<crate::v3::replay::Evaluation>,
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
    pub fn new(config: ReplayConfig) -> Self {
        Self { config }
    }
    pub fn default_engine() -> Self {
        Self::new(ReplayConfig::default())
    }

    /// Aggregate raw 5m klines to 15m/60m bars (reuses derive::aggregate)
    pub fn aggregate_bars(&self, raw5m: &[Kline]) -> (Vec<Bar>, Vec<Bar>) {
        let last = raw5m.last().map(|b| b.datetime.as_str()).unwrap_or("");
        let bars15_k = aggregate(raw5m, Timeframe::M15)
            .into_iter()
            .filter(|b| b.datetime.as_str() <= last)
            .collect::<Vec<_>>();
        let bars60_k = aggregate(raw5m, Timeframe::M60)
            .into_iter()
            .filter(|b| b.datetime.as_str() <= last)
            .collect::<Vec<_>>();
        (kline_to_bar(&bars15_k), kline_to_bar(&bars60_k))
    }

    pub fn aggregate_with_rollovers(
        &self,
        raw: &[Kline],
        rollovers: &[RolloverRecord],
    ) -> (Vec<Bar>, Vec<Bar>) {
        let (mut m15, mut m60) = self.aggregate_bars(raw);
        for list in [&mut m15, &mut m60] {
            for r in rollovers.iter().filter(|r| r.confirmed) {
                if let Some(b) = list.iter_mut().find(|b| b.dt.to_bar_ts() >= r.ts) {
                    b.rollover = true;
                }
            }
        }
        (m15, m60)
    }

    /// Replay history using the same forward warning extractor as live scans.
    /// Returns deduped events (symbol+s0/s1/s2+direction earliest warning wins).
    pub fn replay_history(
        &self,
        symbol: &str,
        raw5m: &[Kline],
        tick_size: f64,
    ) -> Result<Vec<ReplayEvent>> {
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
        let (bars15, bars60) = self.aggregate_with_rollovers(raw5m, rollovers);
        let daily_bars = kline_to_bar(&aggregate(raw5m, Timeframe::Day));
        if bars15.len() < 30 {
            return Ok(vec![]);
        }
        let candidates = crate::analyze::event::replay_warnings(symbol, &bars15, tick_size);
        let mut events: Vec<ReplayEvent> = Vec::with_capacity(candidates.len());
        for candidate in candidates {
            let is_long = candidate.direction == crate::analyze::model::Dir::Up;
            let direction = if is_long { "up" } else { "down" };
            let warning_bar = &bars15[candidate.warning_index];
            let warning_extreme = if is_long {
                warning_bar.high
            } else {
                warning_bar.low
            };
            let s0_price = if is_long {
                bars15[candidate.s0_index].low
            } else {
                bars15[candidate.s0_index].high
            };
            let s1_price = if is_long {
                bars15[candidate.s1_index].high
            } else {
                bars15[candidate.s1_index].low
            };
            let s2_price = if is_long {
                bars15[candidate.s2_index].low
            } else {
                bars15[candidate.s2_index].high
            };
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
                a_speed: if candidate.a_bars > 0 {
                    candidate.a_move / candidate.a_bars as f64
                } else {
                    0.0
                },
                a_move_atr: if a_atr > 1e-9 {
                    candidate.a_move / a_atr
                } else {
                    0.0
                },
                b_move_atr: if b_atr > 1e-9 {
                    candidate.b_move / b_atr
                } else {
                    0.0
                },
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
                warning_wick_ratio: Some(if warning_body > 1e-9 {
                    warning_upper.max(warning_lower) / warning_body
                } else {
                    0.0
                }),
                warning_volume_ratio: crate::analyze::outcome::vol_ratio_at(
                    &bars15,
                    candidate.warning_index,
                ),
                normalized: false,
                missing_mask: 0,
            };
            let warning_ts = warning_bar.dt.to_bar_ts();
            events.push(ReplayEvent {
                symbol: symbol.into(),
                direction: direction.into(),
                grade: candidate.grade.clone(),
                level: candidate.level.to_string(),
                s0_ts: bars15[candidate.s0_index].dt.to_bar_ts(),
                s1_ts: bars15[candidate.s1_index].dt.to_bar_ts(),
                s2_ts: bars15[candidate.s2_index].dt.to_bar_ts(),
                s0_price,
                s1_price,
                s2_price,
                a_move: candidate.a_move,
                b_move: candidate.b_move,
                a_bars: candidate.a_bars as i64,
                b_bars: candidate.b_bars as i64,
                retracement: candidate.retracement,
                warning_ts,
                warning_kind: candidate.warning_kind.to_string(),
                entry: candidate.entry,
                stop: candidate.stop,
                target: candidate.target,
                risk: candidate.risk,
                rr: candidate.rr,
                warning_extreme,
                tick_size,
                trigger_level: candidate.entry,
                setup_features: sf,
                trigger_features: None,
                trigger_bar_ts: None,
                entry_ts: None,
                entry_price: None,
                outcome: None,
                schema_version: FEATURE_SCHEMA_VERSION.into(),
                market_context: None,
                evaluation: None,
                pattern_version: PATTERN_LOGIC_VERSION.into(),
                execution_version: EXECUTION_VERSION.into(),
            });
        }
        // Context is frozen at WARNING close, never at the future trigger close.
        for ev in &mut events {
            ev.market_context = extract_market_context(
                symbol,
                &ev.warning_ts,
                &ev.direction,
                &bars15,
                &bars60,
                &daily_bars,
                rollovers,
            );
            let result = crate::v3::replay::evaluate(ev, raw5m, rollovers, false, 1.0);
            ev.entry_ts = result.entry_ts.clone();
            ev.entry_price = result.fill;
            ev.trigger_bar_ts = result.entry_ts.clone();
            // Gross display only. Supervised NET labels are built by the v3 cost runner.
            if result.status == "closed" {
                let r = result.gross_points / (result.risk * result.volume as f64).max(1e-9);
                ev.outcome = Some(ReplayOutcome {
                    outcome: if r > 0.0 { "win" } else { "loss" }.into(),
                    exit_reason: result.exit_reason.clone().unwrap_or_default(),
                    exit_ts: result.exit_ts.clone().unwrap(),
                    exit_price: result.final_price.unwrap(),
                    r_multiple: r,
                    mfe_r: Some(result.mfe_r),
                    mae_r: Some(result.mae_r),
                    is_1r_aux_win: None,
                });
            }
            ev.evaluation = Some(result);
        }
        Ok(events)
    }
}

fn kline_to_bar(klines: &[Kline]) -> Vec<Bar> {
    klines
        .iter()
        .map(|k| {
            let dt = crate::analyze::model::DT::from_bar_ts(&k.datetime).unwrap_or(
                crate::analyze::model::DT {
                    year: 2024,
                    month: 1,
                    day: 1,
                    hour: 0,
                    minute: 0,
                },
            );
            Bar {
                dt,
                open: k.open,
                high: k.high,
                low: k.low,
                close: k.close,
                volume: k.volume,
                hold: k.hold,
                rollover: false,
            }
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn empty_is_empty() {
        assert!(ReplayEngine::default_engine()
            .replay_history("RB", &[], 1.)
            .unwrap()
            .is_empty());
    }
}
