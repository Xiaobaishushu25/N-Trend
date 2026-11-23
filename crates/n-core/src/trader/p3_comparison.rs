//! Forward comparison of two P3 runner exits. This book owns no broker orders.
use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use super::candidates::{self, p3, Fees};
use super::execution::{ConditionOrder, ManagedPosition, TradeRecord};
use super::model::ExecutionQuote;
use super::policy::{align_protection, protection_reason};
use super::trailing::{TRAIL_ACTIVATE_R, TRAIL_DISTANCE_R};
use crate::analyze::outcome::{NO_FOLLOW_BAR, NO_FOLLOW_MFE_R, TIME_HORIZON_BARS};
use crate::fetch::kline::Kline;
use crate::session::SessionCalendar;

const VERSION: &str = "p3-runner-paired-v1";
const ATR_FACTOR: f64 = 2.;
const ATR_PERIOD: usize = 14;

#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
#[serde(default)]
pub(super) struct P3RunnerBook {
    pub enabled: bool,
    pub started_at: Option<i64>,
    pub enabled_since: Option<i64>,
    pairs: BTreeMap<String, Pair>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
struct Pair {
    position: ManagedPosition,
    market_symbol: String,
    registered_at: i64,
    opened_at: i64,
    fees: Fees,
    multiplier: f64,
    peak_r: f64,
    followed_at: Option<i64>,
    last_quote_at: Option<i64>,
    last_bar_at: Option<i64>,
    bars_held: usize,
    atr: Option<(i64, f64)>,
    flat_boundary: Option<(i64, String)>,
    pending_exit: Option<(i64, String)>,
    first_fill: Option<Fill>,
    common_exit: Option<Fill>,
    runners: [Runner; 2],
    invalid: Option<String>,
    interventions: BTreeMap<String, Intervention>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
struct Runner {
    stop: f64,
    fill: Option<Fill>,
    peak_r: f64,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
struct Fill {
    at: i64,
    price: f64,
    reason: String,
    net_yuan: f64,
    realized_r: f64,
    giveback_r: f64,
    atr_at: Option<i64>,
    atr: Option<f64>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
struct Intervention {
    at: String,
    price: f64,
    lots: i32,
    reason: String,
    broker_fill: bool,
}

impl P3RunnerBook {
    pub fn is_empty(&self) -> bool {
        self.started_at.is_none()
    }

    pub fn register(
        &mut self,
        condition: &ConditionOrder,
        position: &ManagedPosition,
        exchange: &str,
        fill_at: Option<i64>,
        now: i64,
    ) {
        if !self.enabled || position.volume != 2 || self.pairs.contains_key(&position.id) {
            return;
        }
        let (Some(frozen), Some(fill_at)) = (&condition.frozen, fill_at) else {
            return;
        };
        if frozen.definition.strategy_id != p3::ID
            || frozen.definition.parameters["exit_mode"] != "hybrid_s1"
            || self.enabled_since.is_none_or(|started| fill_at < started)
            || exchange.is_empty()
            || !position.risk.is_finite()
            || position.risk <= 0.
        {
            return;
        }
        let Ok(opened_at) = candidates::parse_time(&position.open_ts) else {
            return;
        };
        if self
            .enabled_since
            .is_some_and(|started| opened_at < started)
        {
            return;
        }
        let Ok(params) =
            serde_json::from_value::<p3::P3Parameters>(frozen.definition.parameters.clone())
        else {
            return;
        };
        let Ok(strategy) = p3::P3Strategy::new(params) else {
            return;
        };
        let Ok(fees) =
            serde_json::from_value::<Fees>(frozen.plan.evidence["candidate"]["fees"].clone())
        else {
            return;
        };
        if !fees.valid() {
            return;
        }
        let market_symbol = if exchange == "CZCE" {
            format!("{exchange}.{}", position.instrument_id.to_ascii_uppercase())
        } else {
            format!("{exchange}.{}", position.instrument_id.to_ascii_lowercase())
        };
        let flat_boundary = strategy
            .comparison_flat_boundary(&position.instrument_id, opened_at)
            .map(|(at, reason)| (at, reason.to_owned()));
        let runner = Runner {
            stop: position.initial_stop_price,
            fill: None,
            peak_r: 0.,
        };
        self.pairs.insert(
            position.id.clone(),
            Pair {
                position: position.clone(),
                market_symbol,
                registered_at: now.max(fill_at),
                opened_at,
                fees,
                multiplier: crate::precision::effective_multiplier(&position.instrument_id, ""),
                peak_r: position.max_mfe_r,
                followed_at: (position.max_mfe_r >= NO_FOLLOW_MFE_R).then_some(now),
                last_quote_at: None,
                last_bar_at: None,
                bars_held: 0,
                atr: None,
                flat_boundary,
                pending_exit: None,
                first_fill: None,
                common_exit: None,
                runners: [runner.clone(), runner],
                invalid: None,
                interventions: BTreeMap::new(),
            },
        );
    }

    pub fn contracts(&self) -> BTreeSet<&str> {
        self.pairs
            .values()
            .filter(|pair| pair.active())
            .map(|pair| pair.position.instrument_id.as_str())
            .collect()
    }

    pub fn history_symbols(&self) -> BTreeSet<String> {
        self.pairs
            .values()
            .filter(|pair| pair.active())
            .map(|pair| pair.market_symbol.clone())
            .collect()
    }

    pub fn invalidate_active(&mut self, reason: &str) {
        for pair in self.pairs.values_mut().filter(|pair| pair.active()) {
            pair.invalid = Some(reason.into());
        }
    }

    pub fn invalidate_entry(&mut self, id: &str, reason: &str) {
        if let Some(pair) = self.pairs.get_mut(id).filter(|pair| pair.active()) {
            pair.invalid = Some(reason.into());
        }
    }

    pub fn record_intervention(&mut self, record: &TradeRecord) {
        if !record.is_manual
            && record.exit_reason != "external_manual"
            && !record.exit_reason.starts_with("manual")
        {
            return;
        }
        if let Some(pair) = self.pairs.get_mut(&record.condition_order_id) {
            pair.interventions.insert(
                record.id.clone(),
                Intervention {
                    at: record.exit_ts.clone(),
                    price: record.exit_price,
                    lots: record.volume,
                    reason: record.exit_reason.clone(),
                    broker_fill: !record.id.starts_with("sync_broker:")
                        && !record.id.starts_with("manual_close:"),
                },
            );
        }
    }

    pub fn remove_reconciled_interventions(&mut self, id: &str) {
        if let Some(pair) = self.pairs.get_mut(id) {
            pair.interventions
                .retain(|_, event| event.broker_fill || event.reason != "external_manual");
        }
    }

    pub fn repair_intervention_time(&mut self, trade_id: &str, timestamp: &str) {
        if let Some(event) = self
            .pairs
            .values_mut()
            .find_map(|pair| pair.interventions.get_mut(trade_id))
        {
            event.at = timestamp.to_owned();
        }
    }

    pub fn on_quote(&mut self, quote: &ExecutionQuote, now: i64) -> bool {
        if !quote.valid_at(now) {
            return false;
        }
        let mut observed = false;
        for pair in self.pairs.values_mut().filter(|pair| {
            pair.active()
                && pair
                    .position
                    .instrument_id
                    .eq_ignore_ascii_case(&quote.instrument_id)
        }) {
            observed |= pair.on_quote(quote);
        }
        observed
    }

    pub fn on_history(&mut self, symbol: &str, raw: &[Kline], cutoff: i64) -> bool {
        let Ok(bars) = candidates::completed_bars(raw, cutoff) else {
            return false;
        };
        let mut changed = false;
        for pair in self
            .pairs
            .values_mut()
            .filter(|pair| pair.active() && pair.market_symbol == symbol)
        {
            changed |= pair.on_bars(&bars);
        }
        changed
    }

    pub fn summary(&self, eligible: bool) -> Value {
        let mut paired = 0;
        let mut interrupted = 0;
        let mut active = 0;
        let mut no_runner = 0;
        let mut unavailable = 0;
        let mut delta = 0.;
        let mut interrupted_delta = 0.;
        let mut recent = self.pairs.values().collect::<Vec<_>>();
        for pair in &recent {
            if pair.active() {
                active += 1;
            } else if pair.invalid.is_some() {
                unavailable += 1;
            } else if let Some(difference) = pair.delta() {
                if pair.interventions.is_empty() {
                    paired += 1;
                    delta += difference;
                } else {
                    interrupted += 1;
                    interrupted_delta += difference;
                }
            } else {
                no_runner += 1;
            }
        }
        recent.sort_by_key(|pair| std::cmp::Reverse(pair.registered_at));
        json!({"version": VERSION, "eligible": eligible, "enabled": self.enabled, "started_at": self.started_at,
            "paired": paired, "active": active, "unavailable": unavailable, "no_runner": no_runner, "delta_yuan": delta,
            "interrupted_pairs": interrupted, "interrupted_delta_yuan": interrupted_delta,
            "recent": recent.into_iter().take(24).map(|pair| json!({
                "id": pair.position.id, "event_id": pair.position.event_id, "instrument": pair.position.instrument_id,
                "started_at": pair.registered_at, "first_fill": pair.first_fill, "common_exit": pair.common_exit,
                "active": pair.active(), "invalid": pair.invalid, "atr": pair.atr,
                "base": pair.runners[0].fill, "dynamic": pair.runners[1].fill, "delta_yuan": pair.delta(),
                "interventions": pair.interventions.values().collect::<Vec<_>>()
            })).collect::<Vec<_>>()})
    }
}

impl Pair {
    fn active(&self) -> bool {
        self.invalid.is_none()
            && self.common_exit.is_none()
            && self.runners.iter().any(|runner| runner.fill.is_none())
    }

    fn delta(&self) -> Option<f64> {
        Some(self.runners[1].fill.as_ref()?.net_yuan - self.runners[0].fill.as_ref()?.net_yuan)
    }

    fn on_bars(&mut self, bars: &[Kline]) -> bool {
        let mut previous_close: Option<f64> = None;
        let mut ranges = std::collections::VecDeque::with_capacity(ATR_PERIOD);
        let mut changed = false;
        for bar in bars {
            let Ok(at) = candidates::parse_time(&bar.datetime) else {
                continue;
            };
            if let Some(close) = previous_close {
                if ranges.len() == ATR_PERIOD {
                    ranges.pop_front();
                }
                ranges.push_back(
                    (bar.high - bar.low)
                        .max((bar.high - close).abs())
                        .max((bar.low - close).abs()),
                );
            }
            previous_close = Some(bar.close);
            if ranges.len() == ATR_PERIOD && self.atr.is_none_or(|(last, _)| at > last) {
                let atr = ranges.iter().sum::<f64>() / ATR_PERIOD as f64;
                if atr.is_finite() && atr > 0. {
                    self.atr = Some((at, atr));
                    changed = true;
                }
            }
            if at < self.opened_at || self.last_bar_at.is_some_and(|last| at <= last) {
                continue;
            }
            self.last_bar_at = Some(at);
            self.bars_held += 1;
            changed = true;
            if self.pending_exit.is_none() {
                let reason = if self.bars_held >= TIME_HORIZON_BARS {
                    Some("time_exit")
                } else if self.bars_held == NO_FOLLOW_BAR
                    && self.followed_at.is_none_or(|time| time > at)
                {
                    Some("no_follow")
                } else {
                    None
                };
                self.pending_exit = reason.map(|reason| (at, reason.into()));
            }
        }
        changed
    }

    fn on_quote(&mut self, q: &ExecutionQuote) -> bool {
        if q.timestamp < self.registered_at || self.last_quote_at.is_some_and(|at| q.timestamp < at)
        {
            return false;
        }
        let mut changed = self.last_quote_at != Some(q.timestamp);
        self.last_quote_at = Some(q.timestamp);
        let sign = if self.position.direction == "up" {
            1.
        } else {
            -1.
        };
        let fill_price = self.position.fill_price;
        let risk = self.position.risk;
        let r = (q.last - fill_price) * sign / risk;
        changed |= r > self.peak_r;
        self.peak_r = self.peak_r.max(r);
        if self.followed_at.is_none() && r >= NO_FOLLOW_MFE_R {
            self.followed_at = Some(q.timestamp);
            changed = true;
        }
        let forced = [&self.flat_boundary, &self.pending_exit]
            .into_iter()
            .flatten()
            .filter(|(at, _)| q.timestamp >= *at)
            .min_by_key(|(at, _)| *at)
            .map(|(_, reason)| reason.as_str());
        let atr = self.atr.filter(|(at, _)| *at <= q.timestamp);
        let price = if sign > 0. { q.bid } else { q.ask };
        let can_fill = price.is_finite() && price >= q.lower && price <= q.upper;
        let make_fill = |reason: &str, peak_r: f64, lots: i32| {
            let realized_r = (price - fill_price) * sign / risk;
            Fill {
                at: q.timestamp,
                price,
                reason: reason.into(),
                realized_r,
                net_yuan: (realized_r * risk * self.multiplier
                    - self.fees.bound(fill_price, price, self.multiplier))
                    * lots as f64,
                giveback_r: (peak_r - realized_r).max(0.),
                atr_at: atr.map(|(at, _)| at),
                atr: atr.map(|(_, value)| value),
            }
        };
        if self.first_fill.is_none() {
            let stop_hit = (q.last - self.position.initial_stop_price) * sign <= 0.;
            if let Some(reason) = forced.or(stop_hit.then_some("stop")) {
                changed = true;
                if can_fill {
                    self.common_exit = Some(make_fill(reason, self.peak_r, 2));
                } else {
                    self.invalid = Some("unavailable_close_book".into());
                }
            } else if (q.last - self.position.target_price) * sign >= 0. {
                changed = true;
                if !can_fill {
                    self.invalid = Some("unavailable_close_book".into());
                    return true;
                }
                let reason =
                    if ((self.position.target_price - fill_price) * sign - risk).abs() <= 1e-6 {
                        p3::FIRST_1R
                    } else {
                        p3::FIRST_S1
                    };
                self.first_fill = Some(make_fill(reason, self.peak_r, 1));
                for runner in &mut self.runners {
                    runner.stop = align_protection(fill_price, sign, q.tick);
                    runner.peak_r = r;
                }
            }
            return changed;
        }
        let latest_due_bar = q.timestamp - q.timestamp.rem_euclid(900) - 900;
        let usable_atr = atr.filter(|(at, _)| {
            *at >= latest_due_bar
                || !SessionCalendar::get(&self.position.instrument_id)
                    .is_in_trading_time_naive(&candidates::local(latest_due_bar - 1).naive_local())
        });
        let Some((_, atr_value)) = usable_atr
            .filter(|_| self.peak_r >= TRAIL_ACTIVATE_R)
            .or_else(|| (self.peak_r < TRAIL_ACTIVATE_R || forced.is_some()).then_some((0, 0.)))
        else {
            self.invalid = Some("atr_unavailable".into());
            return true;
        };
        let distances = [
            TRAIL_DISTANCE_R,
            TRAIL_DISTANCE_R.max(ATR_FACTOR * atr_value / risk),
        ];
        for (runner, distance) in self
            .runners
            .iter_mut()
            .zip(distances)
            .filter(|(lane, _)| lane.fill.is_none())
        {
            let stop = p3::runner_stop(&self.position, q, self.peak_r, runner.stop, distance);
            changed |= r > runner.peak_r || stop != runner.stop;
            runner.peak_r = runner.peak_r.max(r);
            runner.stop = stop;
            let stop_hit = (q.last - runner.stop) * sign <= 0.;
            if let Some(reason) = forced.or_else(|| {
                stop_hit.then(|| protection_reason(true, fill_price, runner.stop, sign, q.tick))
            }) {
                changed = true;
                if can_fill {
                    runner.fill = Some(make_fill(reason, runner.peak_r, 1));
                } else {
                    self.invalid = Some("unavailable_close_book".into());
                    break;
                }
            }
        }
        changed
    }
}
