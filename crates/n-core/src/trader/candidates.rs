//! Frozen P2/P3 decisions on the common multi-account executor.
//! Live book execution is versioned separately from research-v5 OHLC replay.
use super::{strategy::*, ConditionState, ManagedPosition};
use crate::{
    fetch::kline::Kline,
    session::{NightSessionType, SessionCalendar},
};
use anyhow::{ensure, Context, Result};
use chrono::{Datelike, Duration, FixedOffset, NaiveDateTime, TimeZone, Timelike};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, BTreeSet},
    sync::Arc,
};

pub const P2: &str = "n_p2";
pub const P3: &str = "n_p3";
pub const CODE_VERSION: &str = "1";
pub const EXECUTION_VERSION: &str = "candidate-book-v1";
pub const ONE_LOT: &str = "candidate_one_lot_v1";

fn shanghai() -> FixedOffset {
    FixedOffset::east_opt(28800).unwrap()
}
pub fn parse_time(s: &str) -> Result<i64> {
    let text = if s.len() == 17 && s.as_bytes().get(8) == Some(&b' ') {
        NaiveDateTime::parse_from_str(s, "%Y%m%d %H:%M:%S")?
    } else {
        NaiveDateTime::parse_from_str(s, "%Y-%m-%d %H:%M:%S")?
    };
    Ok(shanghai()
        .from_local_datetime(&text)
        .single()
        .context("invalid Shanghai timestamp")?
        .timestamp())
}
fn local(ts: i64) -> chrono::DateTime<FixedOffset> {
    chrono::DateTime::from_timestamp(ts, 0)
        .unwrap()
        .with_timezone(&shanghai())
}
pub(crate) fn label(ts: i64) -> String {
    local(ts).format("%Y-%m-%d %H:%M:%S").to_string()
}
/// Correct midnight bucket: 00:00 remains the preceding interval's endpoint.
fn bucket(ts: i64) -> String {
    let dt = local(ts);
    let secs = dt.num_seconds_from_midnight() as i64;
    label(ts - secs + ((secs + 899) / 900) * 900)
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct FeeRule {
    pub fixed_yuan_per_lot: f64,
    pub notional_rate: f64,
}
impl FeeRule {
    fn charge(&self, price: f64, mult: f64) -> f64 {
        self.fixed_yuan_per_lot + self.notional_rate * price * mult
    }
    fn valid(&self) -> bool {
        [self.fixed_yuan_per_lot, self.notional_rate]
            .iter()
            .all(|x| x.is_finite() && *x >= 0.)
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Fees {
    pub open: Option<FeeRule>,
    pub close_today: Option<FeeRule>,
    pub close_yesterday: Option<FeeRule>,
    pub roundtrip_fixed_yuan_per_lot: Option<f64>,
}
impl Fees {
    pub(crate) fn bound(&self, fill: f64, close: f64, mult: f64) -> f64 {
        self.roundtrip_fixed_yuan_per_lot.unwrap_or_else(|| {
            self.open.as_ref().unwrap().charge(fill, mult)
                + self
                    .close_today
                    .as_ref()
                    .unwrap()
                    .charge(close, mult)
                    .max(self.close_yesterday.as_ref().unwrap().charge(close, mult))
        })
    }
    fn valid(&self) -> bool {
        [&self.open, &self.close_today, &self.close_yesterday]
            .iter()
            .all(|r| r.as_ref().is_none_or(FeeRule::valid))
            && self
                .roundtrip_fixed_yuan_per_lot
                .is_none_or(|x| x.is_finite() && x >= 0.)
            && (self.roundtrip_fixed_yuan_per_lot.is_some()
                || [&self.open, &self.close_today, &self.close_yesterday]
                    .iter()
                    .all(|r| r.is_some()))
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct FeeProfile {
    pub id: String,
    pub products: BTreeMap<String, Fees>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct HolidayWindow {
    pub name: String,
    pub block_from: String,
    pub flat_at: String,
    pub last_close: String,
    pub reopen: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct HolidayCalendar {
    pub id: String,
    pub coverage_start: String,
    pub coverage_end: String,
    pub windows: Vec<HolidayWindow>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Parameters {
    pub fees: FeeProfile,
    pub calendar: HolidayCalendar,
}
impl Parameters {
    /// Explicit UI preset, never a silently fetched or mutable broker fee schedule.
    pub fn reference_snapshot() -> Self {
        Self {
            fees: serde_json::from_str(include_str!("candidates/reference-fees-20261022.json"))
                .unwrap(),
            calendar: serde_json::from_str(include_str!(
                "candidates/reference-holidays-2026-jul-oct.json"
            ))
            .unwrap(),
        }
    }
    pub fn validate(&self) -> Result<()> {
        ensure!(
            !self.fees.id.is_empty() && !self.fees.products.is_empty(),
            "P2/P3必须冻结费用版本与费率"
        );
        ensure!(
            self.fees.products.values().all(Fees::valid),
            "P2/P3费率无效"
        );
        ensure!(!self.calendar.id.is_empty(), "P2/P3必须声明日历版本");
        let first = parse_time(&self.calendar.coverage_start)?;
        let last = parse_time(&self.calendar.coverage_end)?;
        ensure!(first < last, "日历覆盖区间无效");
        let mut end = first;
        for h in &self.calendar.windows {
            let (b, f, c, r) = (
                parse_time(&h.block_from)?,
                parse_time(&h.flat_at)?,
                parse_time(&h.last_close)?,
                parse_time(&h.reopen)?,
            );
            ensure!(
                end <= b && b < f && f < c && c < r && r <= last,
                "长假窗口顺序或覆盖范围无效"
            );
            end = r;
        }
        Ok(())
    }
    pub(crate) fn fee(&self, symbol: &str, instrument: &str) -> Option<&Fees> {
        self.fees
            .products
            .get(&instrument.to_uppercase())
            .or_else(|| {
                self.fees
                    .products
                    .get(&SessionCalendar::contract_prefix(symbol))
            })
    }
    pub(crate) fn block(&self, symbol: &str, now: i64) -> Option<&'static str> {
        if now < parse_time(&self.calendar.coverage_start).ok()?
            || now > parse_time(&self.calendar.coverage_end).ok()?
        {
            return Some("calendar_uncovered");
        }
        if self.calendar.windows.iter().any(|h| {
            now >= parse_time(&h.block_from).unwrap() && now < parse_time(&h.reopen).unwrap()
        }) {
            return Some("filtered_holiday_window");
        }
        weekend_windows(symbol, now)
            .iter()
            .any(|(cut, reopen)| now >= *cut && now < *reopen)
            .then_some("filtered_weekend_window")
    }
    pub(crate) fn flat_reason(&self, symbol: &str, opened: i64, now: i64) -> Option<&'static str> {
        if self.calendar.windows.iter().any(|h| {
            opened < parse_time(&h.reopen).unwrap() && now >= parse_time(&h.flat_at).unwrap()
        }) {
            return Some("pre_holiday");
        }
        weekend_windows(symbol, now)
            .iter()
            .any(|(cut, reopen)| opened < *reopen && now >= *cut)
            .then_some("pre_weekend")
    }
}
/// Last weekly cutoff plus this week's cutoff; overdue positions stay exposed until a confirmed fill.
fn weekend_windows(symbol: &str, now: i64) -> Vec<(i64, i64)> {
    let today = local(now).date_naive();
    let monday = today - Duration::days(today.weekday().num_days_from_monday() as i64);
    let night = match SessionCalendar::contract_prefix(symbol).as_str() {
        "SF" | "SM" => NightSessionType::None,
        _ => SessionCalendar::get(symbol).night_type,
    };
    [-1, 0]
        .into_iter()
        .map(|weeks| {
            let friday = monday + Duration::days(4 + weeks * 7);
            let (day, hour, min) = match night {
                NightSessionType::None => (friday, 15, 0),
                NightSessionType::Close2300 => (friday, 23, 0),
                NightSessionType::Close2330 => (friday, 23, 30),
                NightSessionType::Close0100 => (friday + Duration::days(1), 1, 0),
                NightSessionType::Close0230 => (friday + Duration::days(1), 2, 30),
            };
            let close = shanghai()
                .from_local_datetime(&day.and_hms_opt(hour, min, 0).unwrap())
                .single()
                .unwrap()
                .timestamp();
            let reopen = shanghai()
                .from_local_datetime(&(friday + Duration::days(3)).and_hms_opt(9, 0, 0).unwrap())
                .single()
                .unwrap()
                .timestamp();
            (close - 600, reopen)
        })
        .collect()
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct WarningContext {
    pub as_of: String,
    pub atr14: Option<f64>,
    pub complete: bool,
}
/// Build only complete triples from the prefix, with no future bucket or Wilder smoothing.
pub fn completed_bars(raw: &[Kline], cutoff: i64) -> Result<Vec<Kline>> {
    let mut triples: BTreeMap<String, Vec<&Kline>> = BTreeMap::new();
    let mut previous = None;
    for b in raw {
        let ts = parse_time(&b.datetime)?;
        ensure!(previous.is_none_or(|p| p < ts), "原始行情时间重复或乱序");
        previous = Some(ts);
        ensure!(
            local(ts).second() == 0 && local(ts).minute() % 5 == 0,
            "原始行情不是5分钟结束标签"
        );
        if ts > cutoff {
            break;
        }
        ensure!(
            [b.open, b.high, b.low, b.close]
                .iter()
                .all(|v| v.is_finite())
                && b.low <= b.open.min(b.close)
                && b.high >= b.open.max(b.close)
                && b.low <= b.high,
            "原始行情价格无效"
        );
        triples.entry(bucket(ts)).or_default().push(b);
    }
    let mut result = vec![];
    for (end, bs) in triples {
        if bs.len() != 3 || bs[2].datetime != end {
            continue;
        }
        let high = bs.iter().map(|b| b.high).fold(f64::NEG_INFINITY, f64::max);
        let low = bs.iter().map(|b| b.low).fold(f64::INFINITY, f64::min);
        result.push(Kline {
            datetime: end,
            open: bs[0].open,
            high,
            low,
            close: bs[2].close,
            volume: bs.iter().map(|b| b.volume).sum(),
            hold: bs[2].hold,
        });
    }
    Ok(result)
}
pub fn warning_context(raw: &[Kline], warning_ts: &str) -> Result<Option<WarningContext>> {
    let bars = completed_bars(raw, parse_time(warning_ts)?)?;
    let mut tr = Vec::new();
    let mut close: Option<f64> = None;
    for b in &bars {
        tr.push(close.map_or(b.high - b.low, |c| {
            (b.high - b.low)
                .max((b.high - c).abs())
                .max((b.low - c).abs())
        }));
        close = Some(b.close);
    }
    Ok(bars.last().map(|b| WarningContext {
        complete: b.datetime == warning_ts,
        as_of: b.datetime.clone(),
        atr14: (tr.len() >= 14).then(|| tr[tr.len() - 14..].iter().sum::<f64>() / 14.),
    }))
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct Snapshot {
    key: String,
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
    atr14: f64,
    fees: Fees,
    fee_version: String,
    calendar_version: String,
}
impl Snapshot {
    fn sign(&self) -> f64 {
        if self.direction == Direction::Long {
            1.
        } else {
            -1.
        }
    }
    fn target(&self, fill: f64, cap: bool) -> f64 {
        if !cap {
            return self.target;
        }
        fill + self.sign()
            * ((self.target - fill) * self.sign()).min(2. * (fill - self.stop) * self.sign())
    }
    fn net_space(&self, fill: f64, cap: bool) -> bool {
        let target = self.target(fill, cap) - self.sign() * self.tick;
        let stop = self.stop - self.sign() * self.tick;
        let reward = (target - fill) * self.sign() * self.multiplier
            - self.fees.bound(fill, target, self.multiplier);
        let loss = (fill - stop) * self.sign() * self.multiplier
            + self.fees.bound(fill, stop, self.multiplier);
        loss > 0. && reward + 1e-7 >= loss
    }
}
#[derive(Default, Serialize, Deserialize)]
struct Clock {
    buckets: BTreeMap<String, f64>,
}
#[derive(Default, Serialize, Deserialize)]
#[serde(default)]
struct State {
    seen: BTreeSet<String>,
    decisions: BTreeMap<String, Value>,
    pending: BTreeMap<String, Clock>,
    holding: BTreeMap<String, Clock>,
}
impl State {
    fn record(&mut self, key: &str, reason: &str, values: Value) {
        self.seen.insert(key.into());
        self.decisions
            .insert(key.into(), json!({"reason":reason,"values":values}));
    }
}

pub struct CandidateStrategy {
    parameters: Parameters,
    cap: bool,
    #[cfg(test)]
    calendar_time_override: Option<i64>,
}
impl CandidateStrategy {
    pub fn new(parameters: Parameters, cap: bool) -> Result<Self> {
        parameters.validate()?;
        Ok(Self {
            parameters,
            cap,
            #[cfg(test)]
            calendar_time_override: None,
        })
    }
    #[cfg(test)]
    pub(crate) fn with_test_calendar_time(mut self, timestamp: i64) -> Self {
        self.calendar_time_override = Some(timestamp);
        self
    }
    fn calendar_time(&self, now: i64) -> i64 {
        #[cfg(test)]
        if let Some(timestamp) = self.calendar_time_override {
            return timestamp;
        }
        now
    }
    fn signal(
        &self,
        p: &Value,
        c: &StrategyContext<'_>,
        state: &mut State,
    ) -> Result<Vec<TradeIntent>> {
        if !c.allow_entries {
            return Ok(vec![]);
        }
        let now = p["created_at_epoch"]
            .as_i64()
            .unwrap_or_else(|| chrono::Utc::now().timestamp());
        let warn = p["warning_ts"].as_str().unwrap_or("");
        if parse_time(warn).is_ok_and(|t| t > now) {
            return Ok(vec![]);
        }
        let key = [
            "symbol",
            "direction",
            "s0_ts",
            "s1_ts",
            "s2_ts",
            "warning_ts",
        ]
        .map(|k| p[k].as_str().unwrap_or(""))
        .join("|");
        if state.seen.contains(&key) {
            return Ok(vec![]);
        }
        let values = json!({"id":p["id"],"symbol":p["symbol"],"instrument_id":p["instrument_id"],
            "direction":p["direction"],"warning_ts":p["warning_ts"],"created_at_epoch":now,
            "entry":p["entry"],"stop":p["stop"],"target":p["target"],"risk":p["risk"],
            "score":p["entry_score"],"tick":p["tick_size"],"multiplier":p["multiplier"],"warning_context":p["warning_context"],
            "fee_version":self.parameters.fees.id,"calendar_version":self.parameters.calendar.id,"execution_version":EXECUTION_VERSION});
        let snapshot = (|| -> Result<Snapshot> {
            let number = |k: &str| {
                p[k].as_f64()
                    .filter(|v| v.is_finite())
                    .with_context(|| format!("missing finite {k}"))
            };
            ensure!(
                ["symbol", "s0_ts", "s1_ts", "s2_ts", "instrument_id"]
                    .iter()
                    .all(|k| p[*k].as_str().is_some_and(|s| !s.is_empty())),
                "signal identity missing"
            );
            parse_time(warn)?;
            let direction = match p["direction"].as_str() {
                Some("up") => Direction::Long,
                Some("down") => Direction::Short,
                _ => anyhow::bail!("invalid direction"),
            };
            let (entry, stop, target, risk, tick, multiplier, score) = (
                number("entry")?,
                number("stop")?,
                number("target")?,
                number("risk")?,
                number("tick_size")?,
                number("multiplier")?,
                number("entry_score")?,
            );
            let sign = if direction == Direction::Long {
                1.
            } else {
                -1.
            };
            ensure!(
                tick > 0.
                    && multiplier > 0.
                    && risk > 0.
                    && entry > 0.
                    && stop > 0.
                    && target > 0.
                    && (entry - stop) * sign > 0.
                    && (target - entry) * sign > 0.
                    && ((entry - stop) * sign - risk).abs() < 1e-6,
                "invalid geometry"
            );
            let symbol = p["symbol"].as_str().unwrap();
            let instrument = p["instrument_id"].as_str().unwrap();
            let fees = self
                .parameters
                .fee(symbol, instrument)
                .context("fee_unavailable")?
                .clone();
            let atr = p["warning_context"]["atr14"]
                .as_f64()
                .filter(|v| v.is_finite() && *v > 0.)
                .unwrap_or(0.);
            Ok(Snapshot {
                key: key.clone(),
                symbol: symbol.into(),
                instrument: instrument.into(),
                direction,
                warning_ts: warn.into(),
                available_at: now,
                entry,
                stop,
                target,
                risk,
                tick,
                multiplier,
                score,
                atr14: atr,
                fees,
                fee_version: self.parameters.fees.id.clone(),
                calendar_version: self.parameters.calendar.id.clone(),
            })
        })();
        let s = match snapshot {
            Ok(s) => s,
            Err(e) => {
                state.record(&key, &format!("invalid_input:{e}"), values);
                return Ok(vec![]);
            }
        };
        let forecast = s.entry + s.sign() * s.tick;
        let friction = 2. * s.tick * s.multiplier
            + s.fees
                .bound(forecast, s.stop - s.sign() * s.tick, s.multiplier);
        let reason = if s.score < 3. {
            Some("score_below_min")
        } else if s.risk / s.tick + 1e-6 < 10. {
            Some("risk_ticks_below_min")
        } else if friction > 0.20 * s.risk * s.multiplier + 1e-7 {
            Some("friction_above20")
        } else if p["warning_context"]["as_of"].as_str() != Some(warn)
            || p["warning_context"]["complete"] != true
            || s.atr14 <= 0.
        {
            Some("atr_unavailable")
        } else if !(0.75..=2.5).contains(&(s.risk / s.atr14)) {
            Some("atr_out_of_range")
        } else if !s.net_space(forecast, self.cap) {
            Some("filtered_net_rr_plan")
        } else {
            self.parameters.block(&s.symbol, self.calendar_time(now))
        };
        let values = json!({"warning":values,"snapshot":s,"friction_yuan":friction,"risk_atr":s.risk/s.atr14});
        if let Some(reason) = reason {
            state.record(&key, reason, values);
            return Ok(vec![]);
        }
        state.record(&key, "accepted_warning", values);
        state.pending.insert(key.clone(), Clock::default());
        Ok(vec![TradeIntent::Open(EntryPlan {
            signal_id: key,
            symbol: s.symbol.clone(),
            instrument_id: s.instrument.clone(),
            direction: s.direction,
            reference_price: s.entry,
            trigger: EntryTrigger::Breakout { price: s.entry },
            stop_price: Some(s.stop),
            target_price: Some(s.target),
            risk_per_unit: s.risk,
            requested_volume: 1,
            created_at: now,
            expires_at: None,
            reason: "P2/P3条件单，固定一手与原结构止损".into(),
            evidence: json!({"id":p["id"],"entry_score":s.score,"warning_ts":s.warning_ts,
                "candidate":s,"execution_version":EXECUTION_VERSION,"source_evidence":p.get("source_evidence").unwrap_or(p)}),
        })])
    }
}
impl Strategy for CandidateStrategy {
    fn entry_constraints(&self) -> EntryConstraints {
        EntryConstraints {
            one_per_symbol: true,
            next_quote: true,
            terminal_rejections: true,
            complete_15m: true,
        }
    }
    fn check_entry(
        &self,
        plan: &EntryPlan,
        q: &super::model::ExecutionQuote,
        fill: f64,
        now: i64,
    ) -> EntryDecision {
        let Ok(s) = serde_json::from_value::<Snapshot>(plan.evidence["candidate"].clone()) else {
            return EntryDecision::Reject("invalid_candidate_snapshot".into());
        };
        if !q.instrument_id.eq_ignore_ascii_case(&s.instrument) || (q.tick - s.tick).abs() > 1e-7 {
            return EntryDecision::Reject("contract_metadata_mismatch".into());
        }
        if !q.valid_at(now)
            || !(0..=2).contains(&(now - q.timestamp))
            || q.timestamp < s.available_at
            || !q.bid.is_finite()
            || !q.ask.is_finite()
            || q.bid < q.lower
            || q.ask > q.upper
            || q.bid > q.ask
        {
            return EntryDecision::Wait("unavailable_quote".into());
        }
        if let Some(reason) = self.parameters.block(&s.symbol, self.calendar_time(now)) {
            return EntryDecision::Reject(reason.into());
        }
        if !fill.is_finite()
            || (fill - s.entry) * s.sign() > (0.20 * s.risk).max(2. * s.tick) + 1e-6
        {
            return EntryDecision::Reject("unfilled".into());
        }
        if (s.target - fill) * s.sign() <= 0. {
            return EntryDecision::Reject("invalid_target".into());
        }
        if !s.net_space(fill, self.cap) {
            return EntryDecision::Reject("filtered_net_rr_fill".into());
        }
        if (fill - s.stop) * s.sign() / s.tick + 1e-6 < 10. {
            return EntryDecision::Reject("filtered_actual_risk".into());
        }
        EntryDecision::Allow
    }
    fn on_event(
        &self,
        event: &StrategyEvent,
        c: &StrategyContext<'_>,
        value: &mut Value,
    ) -> Result<Vec<TradeIntent>> {
        let mut state: State = if value.is_null() {
            State::default()
        } else {
            serde_json::from_value(value.clone())?
        };
        let mut actions = vec![];
        match event {
            StrategyEvent::Signal { source, payload } if source == N_STRATEGY => {
                actions = self.signal(payload, c, &mut state)?
            }
            StrategyEvent::Quote(q) => {
                for cond in c
                    .conditions
                    .iter()
                    .filter(|o| o.instrument_id.eq_ignore_ascii_case(&q.instrument_id))
                {
                    if !matches!(
                        cond.state,
                        ConditionState::Pending
                            | ConditionState::WaitingConfirm
                            | ConditionState::Triggered
                            | ConditionState::Working
                            | ConditionState::Unknown
                    ) || c.positions.iter().any(|p| p.id == cond.id && !p.is_closed)
                    {
                        continue;
                    }
                    if q.timestamp < cond.created_at
                        || parse_time(&cond.warning_bar_ts).map_or(true, |t| q.timestamp <= t)
                    {
                        continue;
                    }
                    let clock = state
                        .pending
                        .entry(cond.origin.signal_id.clone())
                        .or_default();
                    clock.buckets.entry(bucket(q.timestamp)).or_insert(0.);
                    if clock.buckets.len() > 12 {
                        actions.push(TradeIntent::CancelEntry {
                            condition_id: cond.id.clone(),
                            reason: "untriggered:12_buckets".into(),
                        });
                    }
                }
                for p in c.positions.iter().filter(|p| {
                    !p.is_closed && p.instrument_id.eq_ignore_ascii_case(&q.instrument_id)
                }) {
                    if parse_time(&p.open_ts).is_ok_and(|t| q.timestamp >= t) {
                        let r = (q.last - p.fill_price)
                            * if p.direction == "up" { 1. } else { -1. }
                            / p.risk;
                        let mfe = state
                            .holding
                            .entry(p.id.clone())
                            .or_default()
                            .buckets
                            .entry(bucket(q.timestamp))
                            .or_insert(0.);
                        *mfe = mfe.max(r);
                    }
                }
            }
            StrategyEvent::BarClosed {
                symbol,
                timeframe,
                timestamp,
                ..
            } if timeframe == "candidate15m" => {
                for cond in c.conditions.iter().filter(|o| {
                    o.symbol == *symbol
                        && matches!(
                            o.state,
                            ConditionState::Pending
                                | ConditionState::Triggered
                                | ConditionState::WaitingConfirm
                                | ConditionState::Working
                                | ConditionState::Unknown
                        )
                        && !c.positions.iter().any(|p| p.id == o.id && !p.is_closed)
                }) {
                    if timestamp > &cond.warning_bar_ts {
                        let clock = state
                            .pending
                            .entry(cond.origin.signal_id.clone())
                            .or_default();
                        clock.buckets.entry(timestamp.clone()).or_insert(0.);
                        if clock.buckets.len() > 12 {
                            actions.push(TradeIntent::CancelEntry {
                                condition_id: cond.id.clone(),
                                reason: "untriggered:12_buckets".into(),
                            });
                        }
                    }
                }
                for p in c
                    .positions
                    .iter()
                    .filter(|p| !p.is_closed && p.symbol == *symbol)
                {
                    if c.conditions
                        .iter()
                        .any(|o| o.id == p.id && o.state == ConditionState::Closing)
                    {
                        continue;
                    }
                    let start = bucket(parse_time(&p.open_ts)?);
                    if timestamp < &start {
                        continue;
                    }
                    let clock = state.holding.entry(p.id.clone()).or_default();
                    clock.buckets.entry(start).or_insert(0.);
                    clock.buckets.entry(timestamp.clone()).or_insert(0.);
                    let completed = clock
                        .buckets
                        .range(..=timestamp.clone())
                        .collect::<Vec<_>>();
                    let mfe = completed.iter().map(|(_, r)| **r).fold(0., f64::max);
                    let reason = if completed.len() >= 60 {
                        Some("time_exit")
                    } else if completed.len() == 5 && mfe < 0.5 {
                        Some("no_follow")
                    } else {
                        None
                    };
                    if let Some(reason) = reason {
                        actions.push(TradeIntent::Close {
                            position_id: p.id.clone(),
                            direction: if p.direction == "up" {
                                Direction::Long
                            } else {
                                Direction::Short
                            },
                            volume: None,
                            reason: reason.into(),
                        });
                    }
                }
            }
            StrategyEvent::Timer { timestamp } => {
                for p in c.positions.iter().filter(|p| !p.is_closed && p.volume > 0) {
                    if c.conditions
                        .iter()
                        .any(|o| o.id == p.id && o.state == ConditionState::Closing)
                    {
                        continue;
                    }
                    if let Some(reason) =
                        self.parameters
                            .flat_reason(&p.symbol, parse_time(&p.open_ts)?, *timestamp)
                    {
                        actions.push(TradeIntent::Close {
                            position_id: p.id.clone(),
                            direction: if p.direction == "up" {
                                Direction::Long
                            } else {
                                Direction::Short
                            },
                            volume: None,
                            reason: reason.into(),
                        });
                    }
                }
                for cond in c.conditions.iter().filter(|o| {
                    matches!(
                        o.state,
                        ConditionState::Pending
                            | ConditionState::Triggered
                            | ConditionState::WaitingConfirm
                            | ConditionState::Working
                            | ConditionState::Unknown
                    ) && !c.positions.iter().any(|p| p.id == o.id && !p.is_closed)
                }) {
                    if let Some(reason) = self.parameters.block(&cond.symbol, *timestamp) {
                        actions.push(TradeIntent::CancelEntry {
                            condition_id: cond.id.clone(),
                            reason: reason.into(),
                        });
                    }
                }
            }
            _ => {}
        }
        *value = serde_json::to_value(state)?;
        Ok(actions)
    }
}
struct OneLot;
impl SizingPolicy for OneLot {
    fn volume(&self, p: &EntryPlan, _: &super::ExecutionConfig, _: &Value) -> Result<i32> {
        ensure!(p.requested_volume == 1, "P2/P3只允许固定一手");
        Ok(1)
    }
}
struct FixedExit {
    cap: bool,
}
impl ExitPolicy for FixedExit {
    fn target_on_fill(&self, p: &ManagedPosition, plan: &EntryPlan) -> Option<f64> {
        let s: Snapshot = serde_json::from_value(plan.evidence["candidate"].clone()).ok()?;
        Some(s.target(p.fill_price, self.cap))
    }
    fn on_quote(
        &self,
        p: &ManagedPosition,
        q: &super::model::ExecutionQuote,
        _: &Value,
    ) -> ExitUpdate {
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
pub fn definition(id: &str, parameters: Parameters) -> StrategyDefinition {
    StrategyDefinition {
        strategy_id: id.into(),
        code_version: CODE_VERSION.into(),
        revision: "1".into(),
        exit_policy: if id == P3 {
            "p3_fixed_exit_v1"
        } else {
            "p2_fixed_exit_v1"
        }
        .into(),
        sizing_policy: ONE_LOT.into(),
        parameters: serde_json::to_value(parameters).unwrap(),
    }
}
pub fn validate_definition(d: &StrategyDefinition) -> Result<()> {
    if matches!(d.strategy_id.as_str(), P2 | P3) {
        ensure!(
            d.code_version == CODE_VERSION
                && d.sizing_policy == ONE_LOT
                && d.exit_policy
                    == if d.strategy_id == P3 {
                        "p3_fixed_exit_v1"
                    } else {
                        "p2_fixed_exit_v1"
                    },
            "P2/P3必须使用对应固定目标退出和固定一手模块"
        );
    }
    Ok(())
}
pub fn register(r: &mut StrategyRegistry) {
    r.register_strategy(P2, CODE_VERSION, |p| {
        Ok(Box::new(CandidateStrategy::new(
            serde_json::from_value(p.clone())?,
            false,
        )?))
    });
    r.register_strategy(P3, CODE_VERSION, |p| {
        Ok(Box::new(CandidateStrategy::new(
            serde_json::from_value(p.clone())?,
            true,
        )?))
    });
    r.register_exit("p2_fixed_exit_v1", Arc::new(FixedExit { cap: false }));
    r.register_exit("p3_fixed_exit_v1", Arc::new(FixedExit { cap: true }));
    r.register_sizing(ONE_LOT, Arc::new(OneLot));
}
pub fn presets() -> Value {
    json!([{"label":"P2 · ATR过滤 / 原S1目标","definition":definition(P2,Parameters::reference_snapshot())},
        {"label":"P3 · ATR过滤 / 2R目标上限","definition":definition(P3,Parameters::reference_snapshot())}])
}

#[cfg(test)]
#[path = "candidates/tests.rs"]
mod tests;
