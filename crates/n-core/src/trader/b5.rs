//! Six-product B5 on the live book executor. Frozen research decisions, versioned
//! quote execution; no global selector, replay labels, network or broker access.
use super::{candidates, model::ExecutionQuote, strategy::*, ConditionState, ManagedPosition};
use crate::{
    derive::{rollover::RolloverRecord, Timeframe},
    fetch::kline::Kline,
};
use anyhow::{ensure, Result};
use chrono::Timelike;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, BTreeSet},
    sync::Arc,
};
mod generator;

pub const ID: &str = "n_b5_six";
pub const VERSION: &str = "1";
pub const EXIT: &str = "b5_structural_exit_v1";
pub const EXECUTION_VERSION: &str = "b5-six-book-v1";
pub const SYMBOLS: [&str; 6] = ["EG0", "PP0", "V0", "JD0", "SR0", "CJ0"];

#[derive(Clone, Debug, Serialize, Deserialize)]
struct Snapshot {
    key: String,
    cluster: String,
    symbol: String,
    instrument: String,
    direction: Direction,
    warning_ts: String,
    available_at: i64,
    entry: f64,
    stop: f64,
    target: f64,
    risk: f64,
    tick: f64,
    multiplier: f64,
    score: f64,
    hour: Option<f64>,
    confirm_minute: bool,
    body_net: f64,
    fees: candidates::Fees,
}
impl Snapshot {
    fn sign(&self) -> f64 {
        if self.direction == Direction::Long {
            1.
        } else {
            -1.
        }
    }
    fn net_space(&self, fill: f64) -> bool {
        let target = self.target - self.sign() * self.tick;
        let stop = self.stop - self.sign() * self.tick;
        let reward = (target - fill) * self.sign() * self.multiplier
            - self.fees.bound(fill, target, self.multiplier);
        let loss = (fill - stop) * self.sign() * self.multiplier
            + self.fees.bound(fill, stop, self.multiplier);
        loss > 0. && reward + 1e-7 >= loss
    }
}
#[derive(Clone, Default, Serialize, Deserialize)]
struct Minute {
    start: i64,
    first: i64,
    last: i64,
    close: f64,
}
#[derive(Default, Serialize, Deserialize)]
#[serde(default)]
struct State {
    raw_cursor: BTreeMap<String, String>,
    cursor: BTreeMap<String, String>,
    dedup: BTreeMap<String, Vec<crate::storage::entities::pattern_events::Model>>,
    decisions: BTreeMap<String, Value>,
    status: BTreeMap<String, String>,
    minutes: BTreeMap<String, Minute>,
    confirmed: BTreeMap<String, i64>,
    released: BTreeMap<String, i64>,
    lifecycle: Value,
}
impl State {
    fn record(&mut self, s: &Snapshot, reason: &str) {
        let friction = 2. * s.tick * s.multiplier
            + s.fees.bound(
                s.entry + s.sign() * s.tick,
                s.stop - s.sign() * s.tick,
                s.multiplier,
            );
        let space = (s.target - s.entry) * s.sign() * s.multiplier;
        self.decisions.insert(s.key.clone(),json!({"reason":reason,"values":{
            "symbol":s.symbol,"direction":if s.sign()>0.{"up"}else{"down"},"warning_ts":s.warning_ts,
            "entry":s.entry,"stop":s.stop,"target":s.target,"risk":s.risk,"score":s.score,
            "created_at_epoch":s.available_at,"tick":s.tick,"multiplier":s.multiplier,
            "snapshot":s,"min_score":2.5,"friction_yuan":friction,
            "cost_to_gross_space":if space>0.{Some(friction/space)}else{None},
            "a_body_net_ex_s0":s.body_net,"execution_version":EXECUTION_VERSION}}));
        while self.decisions.len() > 1000 {
            let key = self
                .decisions
                .iter()
                .min_by_key(|(_, v)| v["values"]["created_at_epoch"].as_i64().unwrap_or(0))
                .map(|(k, _)| k.clone())
                .unwrap();
            self.decisions.remove(&key);
        }
    }
}

pub struct B5 {
    parameters: candidates::Parameters,
}
impl B5 {
    fn entry_block(&self, s: &Snapshot, now: i64) -> Option<&'static str> {
        if candidates::parse_time(&self.parameters.calendar.coverage_end)
            .is_ok_and(|end| now >= end - 600)
        {
            return Some("calendar_coverage_ending");
        }
        if s.hour.is_some_and(|h| h <= -1.) && pre_long_break(&s.symbol, now) {
            return Some("hour_counter_pre_long_break");
        }
        self.parameters.block(&s.symbol, now)
    }
    fn new(parameters: candidates::Parameters) -> Result<Self> {
        parameters.validate()?;
        ensure!(
            SYMBOLS.iter().all(|s| parameters.fee(s, "").is_some()),
            "六品种B5必须配置六个品种的冻结费率"
        );
        Ok(Self { parameters })
    }
    fn history(
        &self,
        symbol: &str,
        input: &[Kline],
        rolls: &[RolloverRecord],
        now: i64,
        c: &StrategyContext<'_>,
        state: &mut State,
    ) -> Result<Vec<TradeIntent>> {
        if !SYMBOLS.contains(&symbol) {
            return Ok(vec![]);
        }
        let raw: Vec<_> = input
            .iter()
            .filter(|b| candidates::parse_time(&b.datetime).is_ok_and(|t| t <= now))
            .cloned()
            .collect();
        let Some(last) = raw.last() else {
            return Ok(vec![]);
        };
        let completed = candidates::completed_bars(&raw, now)?; // validates timestamps and geometry
        let all = generator::bars(&raw, Timeframe::M15, rolls);
        if all.len() < 30 {
            state.status.insert(symbol.into(), "warming_up".into());
            return Ok(vec![]);
        }
        // The quote map key is the requested main-continuous symbol. Tq's
        // underlying_symbol describes its actual member, not "EG0"/"JD0".
        let quote = c
            .quotes
            .get(symbol)
            .filter(|q| q.valid_at(now))
            .or_else(|| {
                c.quotes
                    .values()
                    .filter(|q| q.underlying_symbol.eq_ignore_ascii_case(symbol) && q.valid_at(now))
                    .max_by_key(|q| q.timestamp)
            });
        let Some(q) = quote else {
            state
                .status
                .insert(symbol.into(), "waiting_current_contract_quote".into());
            return Ok(vec![]);
        };
        let product = crate::session::SessionCalendar::contract_prefix(symbol);
        if crate::session::SessionCalendar::contract_prefix(&q.instrument_id) != product {
            return Ok(vec![]);
        }
        let old = state.cursor.get(symbol).cloned();
        if state
            .raw_cursor
            .get(symbol)
            .is_some_and(|t| t >= &last.datetime)
        {
            return Ok(vec![]);
        }
        let from = old.as_ref().map_or(all.len().saturating_sub(64), |t| {
            all.partition_point(|b| b.dt.to_bar_ts() <= *t)
        });
        let warnings = generator::extract(
            symbol,
            &all,
            q.tick,
            from,
            state.dedup.entry(symbol.into()).or_default(),
        )?;
        state
            .raw_cursor
            .insert(symbol.into(), last.datetime.clone());
        state
            .cursor
            .insert(symbol.into(), all.last().unwrap().dt.to_bar_ts());
        state.status.insert(
            symbol.into(),
            if old.is_none() {
                "initialized_no_historical_entries"
            } else {
                "ready"
            }
            .into(),
        );
        let hourly = generator::bars(&raw, Timeframe::M60, rolls);
        let daily = generator::bars(&raw, Timeframe::Day, rolls);
        let multiplier = match product.as_str() {
            "EG" | "JD" | "SR" => 10.,
            _ => 5.,
        };
        let mut actions = vec![];
        for w in warnings {
            let ts = all[w.warning_index].dt.to_bar_ts();
            if old.is_none() || !c.allow_entries {
                continue;
            } // initialize dedup, never backfill trades
            let sign = if w.direction == crate::analyze::model::Dir::Up {
                1.
            } else {
                -1.
            };
            let entry = generator::grid(w.entry, q.tick, sign > 0.);
            let stop = generator::grid(w.stop, q.tick, sign < 0.);
            let target = generator::local_target(&w, &completed, &ts, q.tick);
            let hour = crate::v2::features::extract_market_context(
                symbol,
                &ts,
                if sign > 0. { "up" } else { "down" },
                &all,
                &hourly,
                &daily,
                rolls,
            )
            .and_then(|v| v.trend_alignment_60);
            let s0 = all[w.s0_index].dt.to_bar_ts();
            let s1 = all[w.s1_index].dt.to_bar_ts();
            let cluster = format!(
                "{symbol}|{}|{s0}|{s1}",
                if sign > 0. { "up" } else { "down" }
            );
            let key = format!("{cluster}|{}|{ts}", all[w.s2_index].dt.to_bar_ts());
            let s = Snapshot {
                key: key.clone(),
                cluster,
                symbol: symbol.into(),
                instrument: q.instrument_id.clone(),
                direction: if sign > 0. {
                    Direction::Long
                } else {
                    Direction::Short
                },
                warning_ts: ts.clone(),
                available_at: now,
                entry,
                stop,
                target,
                risk: (entry - stop) * sign,
                tick: q.tick,
                multiplier,
                score: w.entry_score,
                hour,
                confirm_minute: hour.is_some_and(|h| h <= 0.) && w.warning_kind != "cumulative",
                body_net: sign
                    * all[w.s0_index + 1..=w.s1_index]
                        .iter()
                        .map(|b| b.close - b.open)
                        .sum::<f64>(),
                fees: self
                    .parameters
                    .fee(symbol, &q.instrument_id)
                    .unwrap()
                    .clone(),
            };
            let reason = self.plan_reason(&s, now, completed.iter().any(|b| b.datetime == ts));
            state.record(&s, reason.unwrap_or("accepted_warning"));
            if reason.is_some() {
                continue;
            }
            actions.push(TradeIntent::Open(EntryPlan{signal_id:key,symbol:s.symbol.clone(),instrument_id:s.instrument.clone(),
                direction:s.direction,reference_price:s.entry,trigger:EntryTrigger::Breakout{price:s.entry},
                stop_price:Some(s.stop),target_price:Some(s.target),risk_per_unit:s.risk,requested_volume:1,
                created_at:now,expires_at:None,reason:"六品种B5 · 原结构目标 / 固定一手".into(),
                evidence:json!({"entry_score":s.score,"warning_ts":ts,"b5":s,"execution_version":EXECUTION_VERSION})}));
        }
        Ok(actions)
    }
    fn plan_reason(&self, s: &Snapshot, now: i64, complete: bool) -> Option<&'static str> {
        let friction = 2. * s.tick * s.multiplier
            + s.fees.bound(
                s.entry + s.sign() * s.tick,
                s.stop - s.sign() * s.tick,
                s.multiplier,
            );
        let space = (s.target - s.entry) * s.sign();
        if s.score < 2.5 {
            Some("score_below_min")
        } else if !complete {
            Some("incomplete_warning_bar")
        } else if !s.risk.is_finite() || s.risk / s.tick + 1e-6 < 10. {
            Some("risk_ticks_below_min")
        } else if friction > 0.2 * s.risk * s.multiplier + 1e-7 {
            Some("friction_above20")
        } else if space <= 0. || friction / (space * s.multiplier) > 0.10 + 1e-12 {
            Some("target_cost_above10")
        } else if s.body_net <= 0. {
            Some("a_body_net_nonpositive")
        } else if !s.net_space(s.entry + s.sign() * s.tick) {
            Some("filtered_net_rr_plan")
        } else if candidates::parse_time(&s.warning_ts).map_or(true, |t| now - t > 120 || now < t) {
            Some("stale_warning")
        } else {
            self.entry_block(s, now)
        }
    }
}

fn snapshot(c: &super::execution::ConditionOrder) -> Option<Snapshot> {
    serde_json::from_value(c.frozen.as_ref()?.plan.evidence["b5"].clone()).ok()
}
fn terminal(s: ConditionState) -> bool {
    matches!(
        s,
        ConditionState::Closed
            | ConditionState::Canceled
            | ConditionState::Expired
            | ConditionState::Invalidated
            | ConditionState::Rejected
    )
}
fn sector(s: &str) -> &'static str {
    if matches!(s, "EG0" | "PP0" | "V0") {
        "chemical"
    } else {
        "agricultural"
    }
}
fn pre_long_break(symbol: &str, now: i64) -> bool {
    let Ok(t) = chrono::NaiveDateTime::parse_from_str(&candidates::label(now), "%Y-%m-%d %H:%M:%S")
    else {
        return false;
    };
    crate::session::SessionCalendar::next_session_close(symbol, &(t - chrono::Duration::seconds(1)))
        .is_some_and(|close| {
            (0..=600).contains(&(close - t).num_seconds())
                && (close.hour(), close.minute()) != (10, 15)
        })
}

impl Strategy for B5 {
    fn uses_history(&self) -> bool {
        true
    }
    fn entry_constraints(&self) -> EntryConstraints {
        EntryConstraints {
            one_per_symbol: true,
            next_quote: true,
            terminal_rejections: true,
            complete_15m: true,
        }
    }
    fn subscriptions(&self) -> Vec<MarketSubscription> {
        SYMBOLS
            .iter()
            .map(|s| MarketSubscription {
                symbol: (*s).into(),
                timeframe: Some("15m".into()),
            })
            .collect()
    }
    fn handles_event(&self, e: &StrategyEvent) -> bool {
        !matches!(e, StrategyEvent::Signal { .. })
    }
    fn check_portfolio(&self, plan: &EntryPlan, p: &EntryPortfolio<'_>, now: i64) -> EntryDecision {
        let Ok(s) = serde_json::from_value::<Snapshot>(plan.evidence["b5"].clone()) else {
            return EntryDecision::Reject("invalid_b5_snapshot".into());
        };
        let minute = now / 60 * 60;
        let live = |o: &super::execution::ConditionOrder| {
            let open = p
                .positions
                .get(&o.id)
                .is_some_and(|v| !v.is_closed && v.volume > 0);
            let unresolved = p.orders.values().any(|r| {
                r.condition_id == o.id
                    && (!matches!(
                        r.phase,
                        super::execution::OrderPhase::Terminal
                            | super::execution::OrderPhase::Rejected
                    ) || r.filled < r.reported_filled)
            });
            if open || unresolved {
                return true;
            }
            if o.trigger_ts.is_none() {
                return false;
            }
            if !terminal(o.state) {
                return true;
            }
            let exit = p
                .history
                .iter()
                .filter(|t| t.condition_order_id == o.id)
                .filter_map(|t| candidates::parse_time(&t.exit_ts).ok())
                .max()
                .or_else(|| p.state["released"][&o.id].as_i64());
            exit.is_none_or(|t| t >= minute)
        };
        if p.conditions
            .values()
            .any(|o| o.origin.signal_id != plan.signal_id && o.symbol == s.symbol && live(o))
        {
            return EntryDecision::Reject("blocked_symbol_slot".into());
        }
        let stops = p
            .history
            .iter()
            .filter(|t| {
                t.exit_reason == "stop"
                    && candidates::parse_time(&t.exit_ts).is_ok_and(|v| v < minute)
            })
            .filter_map(|t| p.conditions.get(&t.condition_order_id))
            .filter_map(snapshot)
            .filter(|v| v.cluster == s.cluster)
            .map(|v| v.key)
            .collect::<BTreeSet<_>>()
            .len();
        if stops >= 2 {
            return EntryDecision::Reject("cluster_two_realized_stops".into());
        }
        if s.hour.is_some_and(|h| h <= 0.) {
            let count = p
                .conditions
                .values()
                .filter(|o| o.origin.signal_id != plan.signal_id && live(o))
                .filter_map(snapshot)
                .filter(|v| {
                    v.direction == s.direction
                        && sector(&v.symbol) == sector(&s.symbol)
                        && v.hour.is_some_and(|h| h <= 0.)
                })
                .count();
            if count >= 2 {
                return EntryDecision::Reject("sector_counter_slots_full".into());
            }
        }
        if s.confirm_minute {
            let Some(at) = p.state["confirmed"][&s.key].as_i64() else {
                return EntryDecision::Wait("waiting_completed_1m_close".into());
            };
            if now < at {
                return EntryDecision::Wait("waiting_next_minute".into());
            }
            if now >= at + 60 {
                return EntryDecision::Reject("confirmation_fill_window_missed".into());
            }
        }
        EntryDecision::Allow
    }
    fn check_entry(
        &self,
        plan: &EntryPlan,
        q: &ExecutionQuote,
        fill: f64,
        now: i64,
    ) -> EntryDecision {
        let Ok(s) = serde_json::from_value::<Snapshot>(plan.evidence["b5"].clone()) else {
            return EntryDecision::Reject("invalid_b5_snapshot".into());
        };
        if !q.instrument_id.eq_ignore_ascii_case(&s.instrument) || (q.tick - s.tick).abs() > 1e-7 {
            return EntryDecision::Reject("contract_metadata_mismatch".into());
        }
        if !q.valid_at(now)
            || !(0..=2).contains(&(now - q.timestamp))
            || q.timestamp <= s.available_at
            || !q.bid.is_finite()
            || !q.ask.is_finite()
            || q.bid > q.ask
            || q.bid < q.lower
            || q.ask > q.upper
        {
            return EntryDecision::Wait("unavailable_quote".into());
        }
        if let Some(r) = self.entry_block(&s, now) {
            return EntryDecision::Reject(r.into());
        }
        if !fill.is_finite() || (fill - s.entry) * s.sign() > (0.2 * s.risk).max(2. * s.tick) + 1e-6
        {
            return EntryDecision::Reject("unfilled".into());
        }
        if (s.target - fill) * s.sign() <= 0. {
            return EntryDecision::Reject("invalid_target".into());
        }
        if (fill - s.stop) * s.sign() / s.tick + 1e-6 < 10. {
            return EntryDecision::Reject("filtered_actual_risk".into());
        }
        if !s.net_space(fill) {
            return EntryDecision::Reject("filtered_net_rr_fill".into());
        }
        EntryDecision::Allow
    }
    fn on_event(
        &self,
        e: &StrategyEvent,
        c: &StrategyContext<'_>,
        value: &mut Value,
    ) -> Result<Vec<TradeIntent>> {
        let mut state: State = if value.is_null() {
            State::default()
        } else {
            serde_json::from_value(value.clone())?
        };
        let mut actions = vec![];
        match e {
            StrategyEvent::History5m {
                symbol,
                bars,
                rollovers,
                timestamp,
            } => actions = self.history(symbol, bars, rollovers, *timestamp, c, &mut state)?,
            StrategyEvent::Quote(q) => {
                let m = q.timestamp / 60 * 60;
                let prev = state.minutes.get(&q.instrument_id).cloned();
                for o in c.conditions.iter().filter(|o| {
                    o.instrument_id.eq_ignore_ascii_case(&q.instrument_id) && !terminal(o.state)
                }) {
                    let Some(s) = snapshot(o) else {
                        continue;
                    };
                    if s.confirm_minute {
                        if let Some(ref prev) = prev {
                            if prev.start + 60 == m
                                && prev.first <= prev.start
                                && q.timestamp - prev.last <= 15
                                && prev.start >= s.available_at
                                && (prev.close - s.entry) * s.sign() >= 0.
                            {
                                state.confirmed.entry(s.key.clone()).or_insert(m);
                            }
                        }
                        if (q.last - s.target) * s.sign() >= 0.
                            && !c.positions.iter().any(|p| p.id == o.id && !p.is_closed)
                        {
                            actions.push(TradeIntent::CancelEntry {
                                condition_id: o.id.clone(),
                                reason: "missed_target_before_confirmation".into(),
                            });
                        }
                    }
                }
                if let Some(ref prev) = prev {
                    if prev.start + 60 == m
                        && prev.first <= prev.start
                        && q.timestamp - prev.last <= 15
                    {
                        for p in c.positions.iter().filter(|p| {
                            !p.is_closed
                                && p.volume > 0
                                && p.instrument_id.eq_ignore_ascii_case(&q.instrument_id)
                        }) {
                            let Some(s) = c
                                .conditions
                                .iter()
                                .find(|o| o.id == p.id)
                                .and_then(snapshot)
                            else {
                                continue;
                            };
                            if s.hour.is_some_and(|h| h <= -1.)
                                && pre_long_break(&s.symbol, m)
                                && candidates::parse_time(&p.open_ts).is_ok_and(|t| t <= prev.start)
                                && (prev.close - p.fill_price) * s.sign() < 0.
                            {
                                actions.push(TradeIntent::Close {
                                    position_id: p.id.clone(),
                                    direction: s.direction,
                                    volume: None,
                                    reason: "session_failure".into(),
                                });
                            }
                        }
                    }
                }
                if prev.as_ref().is_none_or(|v| q.timestamp >= v.last) {
                    let item = state.minutes.entry(q.instrument_id.clone()).or_default();
                    if item.start != m {
                        *item = Minute {
                            start: m,
                            first: q.timestamp,
                            last: q.timestamp,
                            close: q.last,
                        };
                    } else {
                        item.last = q.timestamp;
                        item.close = q.last;
                    }
                }
            }
            StrategyEvent::Timer { timestamp } => {
                for o in c
                    .conditions
                    .iter()
                    .filter(|o| terminal(o.state) && o.trigger_ts.is_some())
                {
                    state.released.entry(o.id.clone()).or_insert(*timestamp);
                }
                // The configured calendar boundary is explicit, never silently extended.
                let end = candidates::parse_time(&self.parameters.calendar.coverage_end)?;
                if *timestamp >= end - 600 {
                    for o in c.conditions.iter().filter(|o| {
                        !terminal(o.state)
                            && !c.positions.iter().any(|p| p.id == o.id && !p.is_closed)
                    }) {
                        actions.push(TradeIntent::CancelEntry {
                            condition_id: o.id.clone(),
                            reason: "calendar_coverage_ending".into(),
                        });
                    }
                    for p in c.positions.iter().filter(|p| !p.is_closed && p.volume > 0) {
                        actions.push(TradeIntent::Close {
                            position_id: p.id.clone(),
                            direction: if p.direction == "up" {
                                Direction::Long
                            } else {
                                Direction::Short
                            },
                            volume: None,
                            reason: "calendar_coverage_ending".into(),
                        });
                    }
                }
            }
            _ => {}
        }
        // Share the established observed-bucket lifecycle, without consuming old N signals.
        if matches!(
            e,
            StrategyEvent::Quote(_) | StrategyEvent::BarClosed { .. } | StrategyEvent::Timer { .. }
        ) {
            let lifecycle = candidates::CandidateStrategy::new(self.parameters.clone(), false)?;
            actions.extend(lifecycle.on_event(e, c, &mut state.lifecycle)?);
        }
        // Avoid two independent exit reasons issuing duplicate controls on one event.
        let mut controlled = BTreeSet::new();
        actions.retain(|a| match a {
            TradeIntent::Close { position_id, .. } => {
                controlled.insert(format!("close:{position_id}"))
            }
            TradeIntent::CancelEntry { condition_id, .. } => {
                controlled.insert(format!("cancel:{condition_id}"))
            }
            _ => true,
        });
        *value = serde_json::to_value(state)?;
        Ok(actions)
    }
}

struct B5Exit;
impl ExitPolicy for B5Exit {
    fn target_on_fill(&self, _: &ManagedPosition, p: &EntryPlan) -> Option<f64> {
        Some(
            serde_json::from_value::<Snapshot>(p.evidence["b5"].clone())
                .ok()?
                .target,
        )
    }
    fn on_quote(&self, p: &ManagedPosition, q: &ExecutionQuote, _: &Value) -> ExitUpdate {
        let sign = if p.direction == "up" { 1. } else { -1. };
        ExitUpdate {
            stop: p.initial_stop_price,
            close: if (q.last - p.initial_stop_price) * sign <= 0. {
                Some(("stop".into(), None))
            } else if (q.last - p.target_price) * sign >= 0. {
                Some(("target_s1".into(), None))
            } else {
                None
            },
        }
    }
}
pub fn definition() -> StrategyDefinition {
    let mut p = candidates::Parameters::reference_snapshot();
    p.fees.products.retain(|s, _| {
        SYMBOLS
            .iter()
            .any(|v| crate::session::SessionCalendar::contract_prefix(v) == *s)
    });
    // No national holiday after October in the 2026 schedule; stop before the unconfigured 2027 boundary.
    p.calendar.id = "b5-live-calendar-2026-through-dec30-v1".into();
    p.calendar.coverage_end = "2026-12-30 15:00:00".into();
    StrategyDefinition {
        strategy_id: ID.into(),
        code_version: VERSION.into(),
        revision: "1".into(),
        exit_policy: EXIT.into(),
        sizing_policy: candidates::ONE_LOT.into(),
        parameters: serde_json::to_value(p).unwrap(),
    }
}
pub fn preset() -> Value {
    json!({"label":"六品种 B5 · EG / PP / PVC / JD / SR / CJ","definition":definition()})
}
pub fn validate_definition(d: &StrategyDefinition) -> Result<()> {
    if d.strategy_id == ID {
        ensure!(
            d.code_version == VERSION
                && d.exit_policy == EXIT
                && d.sizing_policy == candidates::ONE_LOT,
            "六品种B5必须使用对应结构退出与固定一手"
        );
    }
    Ok(())
}
pub fn register(r: &mut StrategyRegistry) {
    r.register_strategy(ID, VERSION, |v| {
        Ok(Box::new(B5::new(serde_json::from_value(v.clone())?)?))
    });
    r.register_exit(EXIT, Arc::new(B5Exit));
}

#[cfg(test)]
mod tests;
