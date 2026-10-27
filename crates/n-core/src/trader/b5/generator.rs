//! Causal B5 extraction. No fills, outcomes or global research selectors.
use super::*;
use crate::analyze::{
    event::{self, WarningCandidate},
    model::{Bar, Dir, DT},
};
use crate::storage::entities::pattern_events;

pub fn bars(raw: &[Kline], tf: crate::derive::Timeframe, rolls: &[RolloverRecord]) -> Vec<Bar> {
    let last = raw.last().map(|b| b.datetime.as_str()).unwrap_or("");
    let mut result: Vec<_> = crate::derive::aggregate_endpoints(raw, tf)
        .into_iter()
        .filter(|b| b.datetime.as_str() <= last)
        .filter_map(|b| {
            Some(Bar {
                dt: DT::from_bar_ts(&b.datetime)?,
                open: b.open,
                high: b.high,
                low: b.low,
                close: b.close,
                volume: b.volume,
                hold: b.hold,
                rollover: false,
            })
        })
        .collect();
    for r in rolls
        .iter()
        .filter(|r| r.confirmed && r.ts.as_str() <= last)
    {
        if let Some(b) = result.iter_mut().find(|b| b.dt.to_bar_ts() >= r.ts) {
            b.rollover = true;
        }
    }
    result
}

/// Advance the SAME service similarity state before applying B5 eligibility.
pub fn extract(
    symbol: &str,
    all: &[Bar],
    tick: f64,
    from: usize,
    prior: &mut Vec<pattern_events::Model>,
) -> Result<Vec<WarningCandidate>> {
    let source = event::selected_warnings(symbol, all, tick, true, from);
    let mut kept = vec![];
    for w in from..all.len() {
        let prefix = &all[..=w];
        prior.retain(|e| {
            prefix
                .iter()
                .rposition(|b| b.dt.to_bar_ts() == e.warning_ts)
                .is_some_and(|i| w - i <= crate::analyze::outcome::DEDUP_WARNING_BARS)
        });
        let mut current: Vec<_> = source.iter().filter(|c| c.warning_index == w).collect();
        current.sort_by_key(|c| if c.direction == Dir::Down { 0 } else { 1 });
        for c in current {
            if crate::service::has_similar_warning(prefix, c, prior)? {
                continue;
            }
            let sign = if c.direction == Dir::Up { 1. } else { -1. };
            let price = |i: usize| if sign > 0. { all[i].low } else { all[i].high };
            let ts = all[w].dt.to_bar_ts();
            prior.push(pattern_events::Model {
                id: 0,
                symbol: symbol.into(),
                direction: if sign > 0. { "up" } else { "down" }.into(),
                grade: c.grade.clone(),
                level: c.level.into(),
                s0_ts: all[c.s0_index].dt.to_bar_ts(),
                s0_price: price(c.s0_index),
                s1_ts: all[c.s1_index].dt.to_bar_ts(),
                s1_price: if sign > 0. {
                    all[c.s1_index].high
                } else {
                    all[c.s1_index].low
                },
                s2_ts: all[c.s2_index].dt.to_bar_ts(),
                s2_price: price(c.s2_index),
                a_move: c.a_move,
                b_move: c.b_move,
                a_bars: c.a_bars as i64,
                b_bars: c.b_bars as i64,
                retracement: c.retracement,
                warning_ts: ts.clone(),
                detected_at: ts.clone(),
                warning_kind: c.warning_kind.into(),
                entry_score: c.entry_score,
                entry_score_dims: "{}".into(),
                entry: c.entry,
                stop: c.stop,
                target: c.target,
                risk: c.risk,
                rr: c.rr,
                state: "pending".into(),
                last_advance_ts: None,
                trigger_ts: None,
                trigger_bar_ts: None,
                trigger_price: None,
                trigger_score: None,
                trigger_volume_ratio: None,
                overshoot_r: None,
                hold_score: None,
                hold_score_history: "[]".into(),
                outcome: None,
                exit_reason: None,
                exit_ts: None,
                exit_price: None,
                r_multiple: None,
                mfe_r: None,
                mae_r: None,
                created_at: ts.clone(),
                updated_at: ts,
            });
            kept.push(c.clone());
        }
        for e in prior.iter_mut() {
            crate::service::advance_event_model(e, prefix);
        }
    }
    Ok(kept)
}

pub fn grid(price: f64, tick: f64, up: bool) -> f64 {
    if up {
        (price / tick - 1e-9).ceil() * tick
    } else {
        (price / tick + 1e-9).floor() * tick
    }
}

/// Frozen prior12 target transform: chronological strict pivots can only tighten.
pub fn local_target(c: &WarningCandidate, completed: &[Kline], ts: &str, tick: f64) -> f64 {
    let sign = if c.direction == Dir::Up { 1. } else { -1. };
    let entry = grid(c.entry, tick, sign > 0.);
    let mut target = grid(c.target, tick, sign < 0.);
    let end = completed.partition_point(|b| b.datetime.as_str() < ts);
    for w in completed[end.saturating_sub(12)..end].windows(3) {
        if candidates::parse_time(&w[1].datetime).unwrap()
            - candidates::parse_time(&w[0].datetime).unwrap()
            != 900
            || candidates::parse_time(&w[2].datetime).unwrap()
                - candidates::parse_time(&w[1].datetime).unwrap()
                != 900
        {
            continue;
        }
        let pivot = if sign > 0. {
            w[1].high > w[0].high && w[1].high > w[2].high
        } else {
            w[1].low < w[0].low && w[1].low < w[2].low
        };
        let price = if sign > 0. { w[1].high } else { w[1].low };
        if pivot && (price - entry) * sign > 0. && (target - price) * sign > 0. {
            target = price;
        }
    }
    grid(target, tick, sign < 0.)
}
