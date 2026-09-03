use anyhow::Result;
use serde::{Deserialize, Serialize};
use crate::analyze::model::Bar;
use crate::derive::{aggregate, Timeframe};
use crate::fetch::kline::Kline;
use crate::v2::features::{extract_trigger_features, SetupFeatures, TriggerFeatures};
use crate::v2::{FEATURE_SCHEMA_VERSION, PATTERN_LOGIC_VERSION, EXECUTION_VERSION};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ReplayConfig {
    pub trigger_timeout_bars: usize,
    pub stop_target_lookahead: usize,
}

impl Default for ReplayConfig {
    fn default() -> Self { Self { trigger_timeout_bars: 48, stop_target_lookahead: 64 } }
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
    pub trigger_level: f64,
    pub setup_features: SetupFeatures,
    pub trigger_features: Option<TriggerFeatures>,
    pub trigger_bar_ts: Option<String>,
    pub entry_ts: Option<String>,
    pub entry_price: Option<f64>,
    pub outcome: Option<ReplayOutcome>,
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

    /// Replay history: detect setups via analyze_bars_v2 and scan forward for trigger.
    /// Returns deduped events (symbol+s0/s1/s2+direction earliest warning wins).
    pub fn replay_history(&self, symbol: &str, raw5m: &[Kline]) -> Result<Vec<ReplayEvent>> {
        use crate::analyze::analyze_bars_v2;
        let (bars15, bars60) = self.aggregate_bars(raw5m);
        if bars15.len() < 30 { return Ok(vec![]); }
        // We do sliding window replay: for each prefix ending at i, run analysis and collect setup
        let mut events: Vec<ReplayEvent> = Vec::new();
        let mut seen: std::collections::HashSet<String> = std::collections::HashSet::new();
        // coarse step to avoid O(n^2) blowup — step 5 bars
        let step = 5usize;
        let mut i = 30;
        while i < bars15.len() {
            let prefix15 = &bars15[..=i];
            let prefix60_len = bars60.iter().filter(|b| b.dt.to_bar_ts() <= prefix15.last().unwrap().dt.to_bar_ts()).count();
            let prefix60 = if prefix60_len > 0 { &bars60[..prefix60_len] } else { &bars60[..0] };
            if let Ok(out) = analyze_bars_v2(symbol, prefix15, prefix60, 1.0) {
                for pat in out.detail.signals.iter().filter(|p| p.active) {
                    // Use warning_ts as s2 time approximation
                    let s2_ts = pat.s2.ts.clone();
                    let key = format!("{}|{}|{}|{}|{}", symbol, pat.s0.ts, pat.s1.ts, s2_ts, pat.direction);
                    if seen.contains(&key) { continue; }
                    seen.insert(key);
                    // build setup features from pattern dto -> need NPattern; we approximate via dto fields
                    // For replay we reconstruct minimal SetupFeatures directly from dto to avoid re-parsing NPattern
                    let atr: Vec<Option<f64>> = crate::analyze::indicators::atr(prefix15, 20);
                    let t60 = crate::analyze::indicators::analyze_60m(prefix60);
                    // synthesize SetupFeatures
                    let mut sf = SetupFeatures {
                        a_move: pat.a_move, b_move: pat.b_move, a_bars: pat.a_bars as i64, b_bars: pat.b_bars as i64,
                        retracement: pat.retracement, a_speed: if pat.a_bars>0 { pat.a_move / pat.a_bars as f64 } else {0.0},
                        a_move_atr: 0.0, b_move_atr: 0.0, grade: pat.grade.clone(), level: pat.level.clone(),
                        direction: if pat.direction=="up" {"up".into()} else {"down".into()},
                        a_strong_count: 0, setup_quality: pat.score, trend60_state: format!("{:?}", t60),
                        warning_close_location: None, warning_body_atr: None, warning_wick_ratio: None, warning_volume_ratio: pat.vol_ratio,
                        normalized: false, missing_mask: 0,
                    };
                    // try to compute a_move_atr from atr at s1 index
                    if let Some(idx) = prefix15.iter().position(|b| b.dt.to_bar_ts()==pat.s1.ts) {
                        if let Some(Some(a)) = atr.get(idx) { sf.a_move_atr = pat.a_move / a.max(1e-9); }
                    }
                    let ev = ReplayEvent {
                        symbol: symbol.into(), direction: sf.direction.clone(), grade: pat.grade.clone(), level: pat.level.clone(),
                        s0_ts: pat.s0.ts.clone(), s1_ts: pat.s1.ts.clone(), s2_ts: pat.s2.ts.clone(),
                        s0_price: pat.s0.price, s1_price: pat.s1.price, s2_price: pat.s2.price,
                        a_move: pat.a_move, b_move: pat.b_move, a_bars: pat.a_bars as i64, b_bars: pat.b_bars as i64,
                        retracement: pat.retracement, warning_ts: pat.warning_ts.clone().unwrap_or_else(|| s2_ts.clone()),
                        warning_kind: pat.warning_kind.clone(), entry: pat.entry, stop: pat.stop, target: pat.target,
                        risk: pat.risk, rr: pat.rr, trigger_level: pat.entry,
                        setup_features: sf, trigger_features: None, trigger_bar_ts: None, entry_ts: None, entry_price: None, outcome: None,
                        schema_version: FEATURE_SCHEMA_VERSION.into(), pattern_version: PATTERN_LOGIC_VERSION.into(), execution_version: EXECUTION_VERSION.into(),
                    };
                    events.push(ev);
                }
            }
            i += step;
        }
        // second pass: for each event, scan forward to find trigger and outcome
        // V2 scoring trigger = close beyond warning extreme (high for up, low for down); entry = warning extreme + tick.
        // We scan from warning bar onward (fallback to s2 if warning missing) and require high/low touch + close beyond warning.
        for ev in events.iter_mut() {
            let warn_idx_opt = bars15.iter().position(|b| bar_ts_matches(&b.dt.to_bar_ts(), &ev.warning_ts));
            let s2_idx_opt = bars15.iter().position(|b| bar_ts_matches(&b.dt.to_bar_ts(), &ev.s2_ts));
            let Some(start_idx) = warn_idx_opt.or(s2_idx_opt) else { continue; };
            // warning extreme approximated as trigger_level - tick (tick ~1.0)
            let warn_level = ev.trigger_level - 1.0;
            let is_long = ev.direction == "up";
            // ensure stop/target orientation is sane for diagnostics; do not alter stored values
            let mut trigger_idx: Option<usize> = None;
            let end = (start_idx + self.config.trigger_timeout_bars).min(bars15.len().saturating_sub(1));
            if start_idx + 1 <= end {
                for j in start_idx+1..=end {
                    let b = &bars15[j];
                    let touched = if is_long {
                        b.high >= warn_level - 1e-9 && b.close > warn_level + 1e-9
                    } else {
                        b.low <= warn_level + 1e-9 && b.close < warn_level - 1e-9
                    };
                    if touched { trigger_idx = Some(j); break; }
                }
                // fallback: pure level touch without close beyond (keep some recall if scoring strict)
                if trigger_idx.is_none() {
                    for j in start_idx+1..=end {
                        let b = &bars15[j];
                        let touched2 = if is_long { b.high >= ev.trigger_level - 1e-9 } else { b.low <= ev.trigger_level + 1e-9 };
                        if touched2 { trigger_idx = Some(j); break; }
                    }
                }
                if let Some(ti) = trigger_idx {
                    let tb = &bars15[ti];
                    let risk = ev.risk.abs().max(1e-9);
                    let atr = crate::analyze::indicators::atr(&bars15, 20).get(ti).and_then(|x| *x);
                    let vol_ratio = crate::analyze::outcome::vol_ratio_at(&bars15, ti);
                    let tf = extract_trigger_features(tb, ev.trigger_level, risk, atr, vol_ratio, None, None);
                    // direction normalize for dataset consistency — store raw but builder will normalize copy
                    ev.trigger_features = Some(tf.clone());
                    ev.trigger_bar_ts = Some(tb.dt.to_bar_ts());
                    // entry is next bar open
                    if ti+1 < bars15.len() {
                        ev.entry_ts = Some(bars15[ti+1].dt.to_bar_ts());
                        ev.entry_price = Some(bars15[ti+1].open);
                    }
                    // outcome: scan until stop or target hit
                    let entry_price = ev.entry_price.unwrap_or(ev.entry);
                    let mut exit: Option<(usize, f64, &str)> = None;
                    let look = (ti+1 + self.config.stop_target_lookahead).min(bars15.len());
                    for k in ti+1..look {
                        let b = &bars15[k];
                        // check stop first
                        let hit_stop = if is_long { b.low <= ev.stop } else { b.high >= ev.stop };
                        let hit_target = if is_long { b.high >= ev.target } else { b.low <= ev.target };
                        if hit_stop { exit = Some((k, ev.stop, "stop")); break; }
                        if hit_target { exit = Some((k, ev.target, "target")); break; }
                    }
                    if let Some((ei, ep, reason)) = exit {
                        let r_mult = if is_long { (ep - entry_price)/risk } else { (entry_price - ep)/risk };
                        let outcome = if r_mult > 0.0 { "win" } else { "loss" };
                        // 1R aux: did price reach 1R before stop?
                        let mut aux_win: Option<bool> = None;
                        let one_r = if is_long { entry_price + risk } else { entry_price - risk };
                        for k in ti+1..=ei {
                            let b = &bars15[k];
                            let hit_1r = if is_long { b.high >= one_r } else { b.low <= one_r };
                            if hit_1r { aux_win = Some(true); break; }
                        }
                        if aux_win.is_none() { aux_win = Some(false); }
                        ev.outcome = Some(ReplayOutcome{
                            outcome: outcome.into(), exit_reason: reason.into(), exit_ts: bars15[ei].dt.to_bar_ts(), exit_price: ep,
                            r_multiple: r_mult, mfe_r: None, mae_r: None, is_1r_aux_win: aux_win,
                        });
                    }
                }
            }
        }
        Ok(events)
    }
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
        let v = eng.replay_history("test", &[]).unwrap();
        assert!(v.is_empty());
    }
}







