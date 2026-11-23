//! Forward observations of the P3 single-lot exit near its first target.
use std::collections::{BTreeMap, BTreeSet, HashMap};

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use super::{ExecutionState, ManagedPosition, TradeRecord};
use crate::{
    fetch::kline::Kline,
    session::{SessionCalendar, TradingSessionSpec},
    trader::{
        candidates::{self, p3, Fees},
        model::ExecutionQuote,
        ConditionOrder,
    },
};

const VERSION: &str = "p3-single-stall-v1";
const BAR: i64 = 15 * 60;
const RAW_BAR: i64 = 5 * 60;
const ARM_R: f64 = 0.85;
const STALLED_BARS: u8 = 2;
const QUOTE_WINDOW: i64 = 60;
const BAR_GRACE: i64 = 120;
const EPSILON: f64 = 1e-8;

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(transparent)]
pub(super) struct P3StallBook(BTreeMap<String, Observation>);

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
struct Observation {
    market_symbol: String,
    instrument: String,
    event_id: Option<i64>,
    opened_at: i64,
    registered_at: i64,
    entry: f64,
    risk: f64,
    target: f64,
    stop: f64,
    s1: f64,
    sign: f64,
    fees: Fees,
    multiplier: f64,
    last_bar_at: i64,
    peak_r: f64,
    entry_slice_peak_r: f64,
    stale_bars: u8,
    signal: Option<Signal>,
    reference: Option<Reference>,
    target_seen: bool,
    invalid: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
struct Signal {
    bar_at: i64,
    confirmed_at: i64,
    close: f64,
    peak_r: f64,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
struct Reference {
    quote_at: i64,
    opposite_price: f64,
    one_tick_price: f64,
    two_tick_price: f64,
    one_tick_net_r: f64,
    two_tick_net_r: f64,
}

impl P3StallBook {
    pub(super) fn is_empty(&self) -> bool {
        self.0.is_empty()
    }

    pub(super) fn register(
        &mut self,
        condition: &ConditionOrder,
        position: &ManagedPosition,
        exchange: &str,
        fill_at: Option<i64>,
        now: i64,
    ) {
        if position.volume != 1 {
            return;
        }
        let (Some(frozen), Some(opened_at)) = (&condition.frozen, fill_at) else {
            return;
        };
        let Some(s1) = frozen.plan.target_price else {
            return;
        };
        let sign = match position.direction.as_str() {
            "up" => 1.,
            "down" => -1.,
            _ => return,
        };
        let risk = position.risk;
        let s1_distance = (s1 - position.fill_price) * sign;
        if self.0.contains_key(&position.id)
            || frozen.definition.strategy_id != p3::ID
            || frozen.definition.exit_policy != p3::EXIT
            || position.is_manual
            || position.lot1_closed
            || exchange.is_empty()
            || !risk.is_finite()
            || risk <= 0.
            || !(s1_distance > 0. && s1_distance < risk - EPSILON)
            || ((position.target_price - position.fill_price) * sign - risk).abs() > EPSILON
            || candidates::parse_time(&position.open_ts).ok() != Some(opened_at)
        {
            return;
        }
        let Ok(fees) =
            serde_json::from_value::<Fees>(frozen.plan.evidence["candidate"]["fees"].clone())
        else {
            return;
        };
        let multiplier = crate::precision::effective_multiplier(&position.instrument_id, "");
        if !fees.valid() || !multiplier.is_finite() || multiplier <= 0. {
            return;
        }
        let exchange = exchange.to_ascii_uppercase();
        let contract = if exchange == "CZCE" {
            position.instrument_id.to_ascii_uppercase()
        } else {
            position.instrument_id.to_ascii_lowercase()
        };
        self.0.insert(
            position.id.clone(),
            Observation {
                market_symbol: format!("{exchange}.{contract}"),
                instrument: position.instrument_id.clone(),
                event_id: position.event_id,
                opened_at,
                registered_at: now.max(opened_at),
                entry: position.fill_price,
                risk,
                target: position.target_price,
                stop: position.initial_stop_price,
                s1,
                sign,
                fees,
                multiplier,
                last_bar_at: opened_at - opened_at.rem_euclid(BAR),
                peak_r: 0.,
                entry_slice_peak_r: 0.,
                stale_bars: 0,
                signal: None,
                reference: None,
                target_seen: false,
                invalid: None,
            },
        );
    }

    pub(super) fn invalidate(&mut self, id: &str, reason: &'static str) {
        if let Some(row) = self.0.get_mut(id) {
            row.invalid.get_or_insert_with(|| reason.into());
        }
    }

    pub(super) fn interrupt(
        &mut self,
        positions: &HashMap<String, ManagedPosition>,
        reason: &'static str,
    ) {
        for (id, row) in &mut self.0 {
            if row.watching() && positions.get(id).is_some_and(|p| !p.is_closed) {
                row.invalid = Some(reason.into());
            }
        }
    }

    fn summary(&self, state: &ExecutionState) -> Value {
        let actual: HashMap<_, _> = state
            .history
            .iter()
            .filter(|h| {
                self.0
                    .get(&h.condition_order_id)
                    .is_some_and(|row| row.signal.is_some())
            })
            .map(|h| (h.condition_order_id.as_str(), h))
            .collect();
        let mut rows = self.0.iter().collect::<Vec<_>>();
        rows.sort_unstable_by_key(|(id, row)| (std::cmp::Reverse(row.opened_at), *id));
        let (mut active, mut triggered, mut paired, mut unavailable, mut target_later) =
            (0, 0, 0, 0, 0);
        let mut delta = 0.;
        let mut recent = Vec::new();
        for (id, row) in rows {
            let open = state
                .positions
                .get(id)
                .is_some_and(|p| !p.is_closed && p.volume == 1);
            let exit = (!open).then(|| actual.get(id.as_str()).copied()).flatten();
            let actual_net = exit.and_then(|h| row.actual_net_r(h));
            let pair = row
                .reference
                .as_ref()
                .zip(actual_net)
                .filter(|_| row.invalid.is_none())
                .map(|(quote, actual)| quote.one_tick_net_r - actual);
            let reached_target =
                pair.is_some() && exit.is_some_and(|h| h.exit_reason == "target_1r");
            let unavailable_reason = row.invalid.as_deref().or_else(|| {
                (!open && row.signal.is_some() && pair.is_none()).then_some(
                    if row.reference.is_none() {
                        "closed_before_quote"
                    } else {
                        "actual_fill_unverified"
                    },
                )
            });
            active += usize::from(open && !row.target_seen && unavailable_reason.is_none());
            triggered += usize::from(row.signal.is_some());
            unavailable += usize::from(unavailable_reason.is_some());
            if let Some(difference) = pair {
                paired += 1;
                delta += difference;
                target_later += usize::from(reached_target);
            }
            if recent.len() < 24 {
                recent.push(json!({
                    "id": id, "instrument": row.instrument, "event_id": row.event_id,
                    "open": open, "target_seen": row.target_seen,
                    "opened_at": row.opened_at, "s1": row.s1, "target": row.target,
                    "signal": row.signal, "reference": row.reference, "unavailable": unavailable_reason,
                    "actual": exit.map(|h| json!({
                        "price": h.exit_price, "at": h.exit_ts, "reason": h.exit_reason, "net_r": actual_net
                    })),
                    "delta_net_r": pair, "target_later": reached_target
                }));
            }
        }
        json!({"version": VERSION, "observed": self.0.len(), "active": active,
            "triggered": triggered, "paired": paired, "unavailable": unavailable,
            "target_later": target_later, "delta_net_r": delta, "recent": recent})
    }
}

impl ExecutionState {
    pub(super) fn p3_stall_summary(&self) -> Value {
        self.p3_stall.summary(self)
    }

    pub(super) fn p3_stall_history_symbols(&self) -> BTreeSet<String> {
        self.p3_stall
            .0
            .iter()
            .filter_map(|(id, row)| {
                (row.watching() && self.p3_stall_can_observe(id)).then(|| row.market_symbol.clone())
            })
            .collect()
    }

    fn p3_stall_can_observe(&self, id: &str) -> bool {
        !self.closes.contains_key(id)
            && self
                .positions
                .get(id)
                .is_some_and(|p| !p.is_closed && p.volume == 1)
    }

    pub(super) fn on_p3_stall_history(&mut self, symbol: &str, raw: &[Kline], now: i64) -> bool {
        let ids = self
            .p3_stall
            .0
            .iter()
            .filter_map(|(id, row)| {
                (row.watching() && row.market_symbol == symbol && self.p3_stall_can_observe(id))
                    .then(|| id.clone())
            })
            .collect::<Vec<_>>();
        if ids.is_empty() {
            return false;
        }
        let bars = candidates::completed_bars(raw, now);
        let mut changed = false;
        for id in ids {
            let row = self.p3_stall.0.get_mut(&id).unwrap();
            changed |= match &bars {
                Ok(bars) => row.on_bars(bars, raw, now),
                Err(_) => {
                    row.invalid = Some("invalid_history".into());
                    true
                }
            };
        }
        changed
    }

    pub(super) fn on_p3_stall_quotes(
        &mut self,
        quotes: &HashMap<String, ExecutionQuote>,
        now: i64,
    ) -> bool {
        let mut changed = false;
        let (positions, closes) = (&self.positions, &self.closes);
        for (id, row) in &mut self.p3_stall.0 {
            if row.watching()
                && !closes.contains_key(id)
                && positions
                    .get(id)
                    .is_some_and(|p| !p.is_closed && p.volume == 1)
            {
                if let Some(q) = quotes
                    .values()
                    .filter(|q| row.accepts_quote(q, now))
                    .min_by_key(|q| q.timestamp)
                {
                    changed |= row.on_quote(q, now);
                }
            }
        }
        changed
    }
}

impl Observation {
    fn watching(&self) -> bool {
        self.invalid.is_none() && self.reference.is_none() && !self.target_seen
    }

    fn r(&self, price: f64) -> f64 {
        (price - self.entry) * self.sign / self.risk
    }

    fn net_r(&self, price: f64) -> f64 {
        self.r(price)
            - self.fees.bound(self.entry, price, self.multiplier) / (self.risk * self.multiplier)
    }

    fn actual_net_r(&self, h: &TradeRecord) -> Option<f64> {
        let at = candidates::parse_time(&h.exit_ts).ok()?;
        let observed = self.reference.as_ref()?.quote_at;
        (at >= observed
            && h.volume == 1
            && h.exit_price.is_finite()
            && h.exit_price > 0.
            && (h.fill_price - self.entry).abs() <= EPSILON
            && h.instrument_id.eq_ignore_ascii_case(&self.instrument)
            && !h.id.starts_with("sync_broker:")
            && !h.id.starts_with("manual_close:"))
        .then(|| self.net_r(h.exit_price))
    }

    fn peak(&self, bar: &Kline) -> f64 {
        self.r(if self.sign > 0. { bar.high } else { bar.low })
    }

    fn on_bars(&mut self, bars: &[Kline], raw: &[Kline], now: i64) -> bool {
        if let Some(signal) = &self.signal {
            if now > signal.bar_at + QUOTE_WINDOW {
                self.invalid = Some("quote_timeout".into());
                return true;
            }
            return false;
        }
        let session = SessionCalendar::get(&self.instrument);
        let mut changed = false;
        for bar in bars {
            let at = candidates::parse_time(&bar.datetime).expect("validated aggregate timestamp");
            if at <= self.last_bar_at || !is_session_bar(&session, at) {
                continue;
            }
            if has_session_bar(&session, self.last_bar_at + BAR, at - BAR) {
                self.invalid = Some("bar_gap".into());
                return true;
            }
            self.last_bar_at = at;
            changed = true;
            let complete_holding_bar = at - BAR >= self.opened_at;
            let (peak, stop_seen) = if complete_holding_bar {
                (self.peak(bar), self.stop_seen(bar))
            } else {
                raw.iter()
                    .filter_map(|b| {
                        let end = candidates::parse_time(&b.datetime).ok()?;
                        (end <= at && end - RAW_BAR >= self.opened_at).then_some(b)
                    })
                    .fold((self.entry_slice_peak_r, false), |(peak, stopped), b| {
                        (peak.max(self.peak(b)), stopped || self.stop_seen(b))
                    })
            };
            if stop_seen {
                self.invalid = Some("real_exit_boundary".into());
                return true;
            }
            if peak > self.peak_r + EPSILON {
                self.peak_r = peak;
                self.stale_bars = 0;
            } else if complete_holding_bar {
                self.stale_bars = self.stale_bars.saturating_add(1);
            }
            if self.peak_r >= 1. - EPSILON {
                self.target_seen = true;
                return true;
            }
            if self.peak_r >= ARM_R - EPSILON
                && self.stale_bars >= STALLED_BARS
                && (bar.close - self.s1) * self.sign < -EPSILON
            {
                self.signal = Some(Signal {
                    bar_at: at,
                    confirmed_at: now,
                    close: bar.close,
                    peak_r: self.peak_r,
                });
                if now > at + QUOTE_WINDOW {
                    self.invalid = Some("late_bar".into());
                }
                return true;
            }
        }
        if has_session_bar(&session, self.last_bar_at + BAR, now - BAR_GRACE - 1) {
            self.invalid = Some("bar_gap".into());
            return true;
        }
        changed
    }

    fn stop_seen(&self, bar: &Kline) -> bool {
        let adverse = if self.sign > 0. { bar.low } else { bar.high };
        (adverse - self.stop) * self.sign <= 0.
    }

    fn accepts_quote(&self, q: &ExecutionQuote, now: i64) -> bool {
        q.valid_at(now)
            && q.timestamp >= self.registered_at
            && self
                .signal
                .as_ref()
                .is_none_or(|signal| q.timestamp >= signal.confirmed_at)
            && q.instrument_id.eq_ignore_ascii_case(&self.instrument)
            && self
                .market_symbol
                .split_once('.')
                .is_some_and(|(exchange, _)| exchange.eq_ignore_ascii_case(&q.exchange_id))
    }

    fn on_quote(&mut self, q: &ExecutionQuote, now: i64) -> bool {
        let Some(signal) = &self.signal else {
            let r = self.r(q.last);
            if q.timestamp
                < self.opened_at + (RAW_BAR - self.opened_at.rem_euclid(RAW_BAR)) % RAW_BAR
                && r > self.entry_slice_peak_r
            {
                self.entry_slice_peak_r = r;
                return true;
            }
            return false;
        };
        if now > signal.bar_at + QUOTE_WINDOW {
            self.invalid = Some("quote_timeout".into());
            return true;
        }
        if (q.last - self.stop) * self.sign <= 0. || (q.last - self.target) * self.sign >= 0. {
            self.invalid = Some("real_exit_boundary".into());
            return true;
        }
        let book = if self.sign > 0. { q.bid } else { q.ask };
        if !book.is_finite() || !(q.lower..=q.upper).contains(&book) {
            return false;
        }
        let one = (book - self.sign * q.tick).clamp(q.lower, q.upper);
        let two = (book - self.sign * 2. * q.tick).clamp(q.lower, q.upper);
        self.reference = Some(Reference {
            quote_at: q.timestamp,
            opposite_price: book,
            one_tick_price: one,
            two_tick_price: two,
            one_tick_net_r: self.net_r(one),
            two_tick_net_r: self.net_r(two),
        });
        true
    }
}

fn is_session_bar(session: &TradingSessionSpec, end: i64) -> bool {
    session.is_in_trading_time_naive(&candidates::local(end - 1).naive_local())
}

fn has_session_bar(session: &TradingSessionSpec, start: i64, end: i64) -> bool {
    (start..=end)
        .step_by(BAR as usize)
        .any(|at| is_session_bar(session, at))
}
