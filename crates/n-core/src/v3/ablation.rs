//! Offline diagnostic replay. No database, model registration, broker or production mutation.
//! Two monotone OHLC paths are compared, never averaged or chosen by profitability.
//! Agreement is a sensitivity check, not proof against every possible intrabar oscillation.
use crate::{
    analyze::outcome::{NO_FOLLOW_MFE_R, PENDING_BARS, TIME_HORIZON_BARS},
    derive::{rollover::RolloverRecord, Timeframe},
    fetch::kline::Kline,
    trader::{policy::on_price, ExecutionConfig, PositionTier},
    v2::replay::ReplayEvent,
    v3::replay::Evaluation,
};
use chrono::{Datelike, NaiveDateTime, TimeZone, Timelike};
use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Policy {
    OriginalS1,
    /// One lot, retain S1, move stop to entry after reaching 1R. Fees still apply.
    S1ProtectOneR,
    FixedOneR,
    LegacyStaircase,
    ActualTier,
}
#[derive(Clone, Copy, Debug, Serialize, Deserialize)]
pub struct Experiment {
    pub policy: Policy,
    /// None disables no-follow. Five matches live counting; six diagnoses old tc+5.
    pub no_follow_bar: Option<usize>,
    pub slip_ticks: f64,
    pub fee_ticks: f64,
}
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EntryMode {
    #[default]
    Touch,
    CloseConfirmNextOpen,
    BreakoutRetestQuarter,
}
#[derive(Clone, Copy, Debug, Default, Serialize, Deserialize)]
#[serde(default)]
pub struct ResearchRules {
    /// Offline fixed-policy score studies only. ActualTier always keeps admission.
    pub disable_admission_for_fixed_policy: bool,
    pub entry_mode: EntryMode,
    pub target_cap_r: Option<f64>,
    pub fast_fail_bucket: Option<usize>,
    pub pending_buckets: Option<usize>,
    pub min_actual_risk_ticks: Option<f64>,
    pub flat_before_long_break: bool,
    pub skip_prebreak_entries: bool,
    /// Keep only positions currently at/above this gross R before a long break.
    pub exit_before_break_below_r: Option<f64>,
    pub flat_before_weekend: bool,
    pub max_holding_buckets: Option<usize>,
}
/// Published holiday schedule, supplied explicitly by an offline caller.
/// String timestamps use the same fixed Shanghai local representation as bars.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct HolidayWindow {
    pub name: String,
    pub block_from: String,
    pub flat_at: String,
    pub last_close: String,
    pub reopen: String,
}
impl ResearchRules {
    fn target(self, ev: &ReplayEvent, fill: f64, sign: f64) -> f64 {
        self.target_cap_r.map_or(ev.target, |cap| {
            let distance = ((ev.target - fill) * sign).min((fill - ev.stop) * sign * cap);
            fill + sign * distance
        })
    }
    fn expected_entry(self, ev: &ReplayEvent, slip: f64, sign: f64) -> f64 {
        ev.entry + sign * slip * ev.tick_size
            - if self.entry_mode == EntryMode::BreakoutRetestQuarter {
                sign * ev.risk * 0.25
            } else {
                0.
            }
    }
}
fn pre_long_break(symbol: &str, ts: &str) -> bool {
    let Ok(t) = NaiveDateTime::parse_from_str(ts, "%Y-%m-%d %H:%M:%S") else {
        return false;
    };
    let Some(close) = crate::session::SessionCalendar::next_session_close(
        symbol,
        &(t - chrono::Duration::seconds(1)),
    ) else {
        return false;
    };
    if !(0..=10).contains(&(close - t).num_minutes()) {
        return false;
    }
    let after = close + chrono::Duration::minutes(30);
    chrono::Local
        .from_local_datetime(&after)
        .single()
        .is_some_and(|local| !crate::session::SessionCalendar::is_trading_time(symbol, &local))
}
fn pre_weekend(symbol: &str, ts: &str) -> bool {
    use crate::session::spec::NightSessionType;
    let Ok(t) = NaiveDateTime::parse_from_str(ts, "%Y-%m-%d %H:%M:%S") else {
        return false;
    };
    let Some(close) = crate::session::SessionCalendar::next_session_close(
        symbol,
        &(t - chrono::Duration::seconds(1)),
    ) else {
        return false;
    };
    if !(0..=10).contains(&(close - t).num_minutes()) {
        return false;
    }
    // Offline archive-specific correction: the existing default calendar falls
    // SF/SM through to23:00, but the frozen archive has day sessions only.
    // Their published contracts specify day hours; night starts require a separate
    // notice. Do not alter the shared/live calendar or old replay controls here.
    let night = match crate::session::SessionCalendar::contract_prefix(symbol).as_str() {
        "SF" | "SM" => NightSessionType::None,
        _ => crate::session::SessionCalendar::get(symbol).night_type,
    };
    let expected = match night {
        NightSessionType::None => (chrono::Weekday::Fri, 15, 0),
        NightSessionType::Close2300 => (chrono::Weekday::Fri, 23, 0),
        NightSessionType::Close2330 => (chrono::Weekday::Fri, 23, 30),
        NightSessionType::Close0100 => (chrono::Weekday::Sat, 1, 0),
        NightSessionType::Close0230 => (chrono::Weekday::Sat, 2, 30),
    };
    (close.weekday(), close.hour(), close.minute()) == expected
}
#[derive(Clone, Copy, Debug, Default, Serialize, Deserialize)]
pub struct FeeRule {
    pub fixed_yuan_per_lot: f64,
    pub notional_rate: f64,
}
impl FeeRule {
    fn charge(self, price: f64, multiplier: f64) -> f64 {
        self.fixed_yuan_per_lot + self.notional_rate * price * multiplier
    }
}
#[derive(Clone, Copy, Debug, Serialize, Deserialize)]
pub struct EntryGuard {
    pub min_net_reward_loss: f64,
    /// Frozen forecast; execution stress must not tighten the planned gate.
    pub assumed_slip_ticks: f64,
    pub open: FeeRule,
    pub close_today: FeeRule,
    pub close_yesterday: FeeRule,
    pub roundtrip_fixed_override: Option<f64>,
}
impl EntryGuard {
    pub fn commission_bound(self, entry: f64, exit: f64, multiplier: f64) -> f64 {
        self.roundtrip_fixed_override.unwrap_or_else(|| {
            self.open.charge(entry, multiplier)
                + self
                    .close_today
                    .charge(exit, multiplier)
                    .max(self.close_yesterday.charge(exit, multiplier))
        })
    }
    /// Use only the proposed fill and frozen target/stop, never future excursion.
    pub fn allows(self, ev: &ReplayEvent, fill: f64, _ex: Experiment, multiplier: f64) -> bool {
        self.allows_target(ev, fill, ev.target, multiplier)
    }
    pub fn allows_target(self, ev: &ReplayEvent, fill: f64, target: f64, multiplier: f64) -> bool {
        let sign = if ev.direction == "up" { 1. } else { -1. };
        let target_fill = target - sign * self.assumed_slip_ticks * ev.tick_size;
        let stop_fill = ev.stop - sign * self.assumed_slip_ticks * ev.tick_size;
        let reward = (target_fill - fill) * sign * multiplier
            - self.commission_bound(fill, target_fill, multiplier);
        let loss = (fill - stop_fill) * sign * multiplier
            + self.commission_bound(fill, stop_fill, multiplier);
        loss > 0. && reward + 1e-7 >= self.min_net_reward_loss * loss
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Disagreement {
    pub ts: String,
    pub open_high_low_close: Evaluation,
    pub open_low_high_close: Evaluation,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Fill {
    pub ts: String,
    pub price: f64,
    pub volume: i32,
    pub offset: String,
    pub reason: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ResultRow {
    pub evaluation: Evaluation,
    pub disagreement: Option<Disagreement>,
    pub fills: Vec<Fill>,
}
#[derive(Clone)]
struct State {
    e: Evaluation,
    remaining: i32,
    first: bool,
    stop: f64,
    peak: f64,
    tier: PositionTier,
    target: f64,
    fills: Vec<Fill>,
    guard: Option<EntryGuard>,
    multiplier: f64,
    rules: ResearchRules,
    breakout_at: Option<String>,
    confirmed_at: Option<String>,
    holidays: Vec<HolidayWindow>,
}
impl State {
    fn terminal(&self) -> bool {
        !matches!(self.e.status.as_str(), "pending" | "open")
    }
    fn finish(&mut self, status: &str, ts: &str) {
        self.e.status = status.into();
        self.e.exit_ts = Some(ts.into());
    }
    fn realize(&mut self, price: f64, qty: i32, reason: &str, sign: f64, ts: &str) {
        self.fills.push(Fill {
            ts: ts.into(),
            price,
            volume: qty,
            offset: "close".into(),
            reason: reason.into(),
        });
        self.e.gross_points += (price - self.e.fill.unwrap()) * sign * qty as f64;
        self.remaining -= qty;
        self.e.final_price = Some(price);
        self.e.exit_reason = Some(reason.into());
        if self.remaining == 0 {
            self.finish("closed", ts);
        }
    }
    fn enter(&mut self, observed: f64, ev: &ReplayEvent, ex: Experiment, sign: f64, ts: &str) {
        if self.rules.flat_before_weekend && pre_weekend(&ev.symbol, ts) {
            self.finish("filtered_weekend_window", ts);
            return;
        }
        // Bars are labelled by interval end: conservatively block the complete
        // interval ending at block_from. Reopening fills are not blocked.
        if self
            .holidays
            .iter()
            .any(|h| ts >= h.block_from.as_str() && ts <= h.reopen.as_str())
        {
            self.finish("filtered_holiday_window", ts);
            return;
        }
        if (self.rules.flat_before_long_break
            || self.rules.skip_prebreak_entries
            || self.rules.exit_before_break_below_r.is_some())
            && pre_long_break(&ev.symbol, ts)
        {
            self.finish("filtered_close_window", ts);
            return;
        }
        let fill = observed + sign * ex.slip_ticks * ev.tick_size;
        if (fill - ev.entry) * sign > (ev.risk * 0.2).max(2. * ev.tick_size) + 1e-6 {
            self.finish("unfilled", ts);
            return;
        }
        // The S1 experiment rejects an order whose target is already behind its
        // proposed fill. This is a pre-entry decision, never a simulated fill.
        if matches!(ex.policy, Policy::OriginalS1 | Policy::S1ProtectOneR)
            && (ev.target - fill) * sign <= 0.
        {
            self.finish("invalid_target", ts);
            return;
        }
        if self.guard.is_some_and(|g| {
            !g.allows_target(ev, fill, self.rules.target(ev, fill, sign), self.multiplier)
        }) {
            self.finish("filtered_net_rr_fill", ts);
            return;
        }
        if self
            .rules
            .min_actual_risk_ticks
            .is_some_and(|n| (fill - ev.stop) * sign / ev.tick_size + 1e-6 < n)
        {
            self.finish("filtered_actual_risk", ts);
            return;
        }
        self.e.fill = Some(fill);
        self.e.risk = (fill - ev.stop) * sign;
        self.e.entry_ts = Some(ts.into());
        self.e.status = "open".into();
        self.fills.push(Fill {
            ts: ts.into(),
            price: fill,
            volume: self.e.volume,
            offset: "open".into(),
            reason: "entry".into(),
        });
        // Actual execution preserves a valid S1; preset 1R follows the actual fill.
        self.target = if (ev.target - ev.entry) * sign <= ev.risk + 1e-6 {
            fill + sign * self.e.risk
        } else {
            ev.target
        };
        if self.e.risk <= 0. {
            self.finish("invalidated", ts);
        }
    }
    fn excursion(&mut self, price: f64, sign: f64) {
        let r = (price - self.e.fill.unwrap()) * sign / self.e.risk;
        self.peak = self.peak.max(r);
        self.e.mfe_r = self.e.mfe_r.max(r);
        self.e.mae_r = self.e.mae_r.min(r);
    }
    fn stop_reason(&self) -> &'static str {
        if !self.first {
            "stop"
        } else {
            "protection"
        }
    }
    fn favorable(&mut self, end: f64, ev: &ReplayEvent, ex: Experiment, sign: f64, ts: &str) {
        let fill = self.e.fill.unwrap();
        let one = fill + sign * self.e.risk;
        let slip = sign * ex.slip_ticks * ev.tick_size;
        if matches!(ex.policy, Policy::OriginalS1 | Policy::S1ProtectOneR)
            || ex.policy == Policy::FixedOneR
            || ex.policy == Policy::ActualTier && self.tier == PositionTier::Small
        {
            let target = if matches!(ex.policy, Policy::OriginalS1 | Policy::S1ProtectOneR) {
                self.rules.target(ev, fill, sign)
            } else {
                one
            };
            if (end - target) * sign >= -1e-9 {
                self.excursion(target, sign);
                self.realize(
                    target - slip,
                    self.remaining,
                    if matches!(ex.policy, Policy::OriginalS1 | Policy::S1ProtectOneR) {
                        "target_s1"
                    } else {
                        "target_1r"
                    },
                    sign,
                    ts,
                );
                return;
            }
        } else if ex.policy == Policy::ActualTier {
            if !self.first && (end - one) * sign >= -1e-9 {
                self.excursion(one, sign);
                self.realize(one - slip, 1.min(self.remaining), "target_1r", sign, ts);
                self.first = true;
                self.stop = fill;
                if self.terminal() {
                    return;
                }
            }
            if self.first
                && (self.target - fill) * sign > self.e.risk + 1e-6
                && (end - self.target) * sign >= -1e-9
            {
                self.excursion(self.target, sign);
                self.realize(self.target - slip, self.remaining, "target_s1", sign, ts);
                return;
            }
        }
        self.excursion(end, sign);
        if ex.policy == Policy::S1ProtectOneR && self.peak + 1e-9 >= 1. {
            self.first = true;
            self.stop = fill;
        } else if ex.policy == Policy::ActualTier && self.first {
            self.stop = on_price(
                self.tier,
                fill,
                self.e.risk,
                sign,
                end,
                self.target,
                self.stop,
                self.peak,
                true,
            )
            .stop;
        } else if ex.policy == Policy::LegacyStaircase && self.peak + 1e-9 >= 1. {
            self.first = true;
            let grade = ((self.peak + 1e-9) / 0.8).floor().max(1.) as usize;
            let lock = if grade == 1 { 1. } else { grade as f64 * 0.8 };
            let candidate = fill + sign * lock * self.e.risk;
            if (candidate - self.stop) * sign > 0. {
                self.stop = candidate;
            }
        }
    }
    fn open(&mut self, price: f64, ev: &ReplayEvent, ex: Experiment, sign: f64, ts: &str) {
        if self.terminal() {
            return;
        }
        if self.e.fill.is_none() {
            if (price - ev.stop) * sign <= 0. {
                self.finish("invalidated", ts);
                return;
            }
            match self.rules.entry_mode {
                EntryMode::Touch => {
                    if (price - ev.entry) * sign >= 0. {
                        self.enter(price, ev, ex, sign, ts);
                    }
                }
                EntryMode::CloseConfirmNextOpen => {
                    if self.confirmed_at.as_deref().is_some_and(|at| at < ts) {
                        self.enter(price, ev, ex, sign, ts);
                    } else if (price - ev.target) * sign >= 0. {
                        self.finish("missed_target", ts);
                    }
                }
                EntryMode::BreakoutRetestQuarter => {
                    if (price - ev.target) * sign >= 0. {
                        self.finish("missed_target", ts);
                    } else if self.breakout_at.as_deref().is_some_and(|at| at < ts)
                        && (price - (ev.entry - sign * ev.risk * 0.25)) * sign <= 0.
                    {
                        self.enter(price, ev, ex, sign, ts);
                    } else if (price - ev.entry) * sign >= 0. {
                        self.breakout_at.get_or_insert_with(|| ts.into());
                    }
                }
            }
        }
        if self.terminal() || self.e.fill.is_none() {
            return;
        }
        if (price - self.stop) * sign <= 0. {
            self.excursion(price, sign);
            self.realize(
                price - sign * ex.slip_ticks * ev.tick_size,
                self.remaining,
                self.stop_reason(),
                sign,
                ts,
            );
        } else {
            self.favorable(price, ev, ex, sign, ts);
        }
    }
    fn segment(
        &mut self,
        from: f64,
        to: f64,
        ev: &ReplayEvent,
        ex: Experiment,
        sign: f64,
        ts: &str,
    ) {
        if self.terminal() {
            return;
        }
        let favorable = (to - from) * sign >= 0.;
        if self.e.fill.is_none() {
            match self.rules.entry_mode {
                EntryMode::Touch => {
                    if favorable && (to - ev.entry) * sign >= 0. {
                        self.enter(ev.entry, ev, ex, sign, ts);
                    } else if !favorable && (to - ev.stop) * sign <= 0. {
                        self.finish("invalidated", ts);
                    }
                }
                EntryMode::CloseConfirmNextOpen => {
                    if !favorable && (to - ev.stop) * sign <= 0. {
                        self.finish("invalidated", ts);
                    } else if favorable && (to - ev.target) * sign >= 0. {
                        self.finish("missed_target", ts);
                    }
                }
                EntryMode::BreakoutRetestQuarter => {
                    let retest = ev.entry - sign * ev.risk * 0.25;
                    if favorable {
                        if (to - ev.entry) * sign >= 0. {
                            self.breakout_at.get_or_insert_with(|| ts.into());
                        }
                        if (to - ev.target) * sign >= 0. {
                            self.finish("missed_target", ts);
                        }
                    } else if self.breakout_at.as_deref().is_some_and(|at| at < ts)
                        && (to - retest) * sign <= 0.
                    {
                        self.enter(retest, ev, ex, sign, ts);
                    } else if (to - ev.stop) * sign <= 0. {
                        self.finish("invalidated", ts);
                    }
                }
            }
        }
        if self.terminal() || self.e.fill.is_none() {
            return;
        }
        if favorable {
            self.favorable(to, ev, ex, sign, ts);
        } else if (to - self.stop) * sign <= 0. {
            self.excursion(self.stop, sign);
            self.realize(
                self.stop - sign * ex.slip_ticks * ev.tick_size,
                self.remaining,
                self.stop_reason(),
                sign,
                ts,
            );
        } else {
            self.excursion(to, sign);
        }
    }
}
fn near(a: f64, b: f64) -> bool {
    (a - b).abs() < 1e-7
}
fn agree(a: &State, b: &State) -> bool {
    a.e.status == b.e.status
        && a.e.entry_ts == b.e.entry_ts
        && a.e.exit_ts == b.e.exit_ts
        && a.e.exit_reason == b.e.exit_reason
        && a.remaining == b.remaining
        && a.e.fill == b.e.fill
        && (a.terminal() || a.breakout_at == b.breakout_at && a.confirmed_at == b.confirmed_at)
        && near(a.e.gross_points, b.e.gross_points)
        && a.fills.len() == b.fills.len()
        && a.fills.iter().zip(&b.fills).all(|(x, y)| {
            x.ts == y.ts
                && near(x.price, y.price)
                && x.volume == y.volume
                && x.offset == y.offset
                && x.reason == y.reason
        })
        && (a.terminal() || a.first == b.first && near(a.stop, b.stop) && near(a.peak, b.peak))
}
pub fn evaluate(
    ev: &ReplayEvent,
    raw: &[Kline],
    rolls: &[RolloverRecord],
    config: &ExecutionConfig,
    ex: Experiment,
    multiplier: f64,
) -> ResultRow {
    evaluate_guarded(ev, raw, rolls, config, ex, multiplier, None)
}
#[allow(clippy::too_many_arguments)]
pub fn evaluate_guarded(
    ev: &ReplayEvent,
    raw: &[Kline],
    rolls: &[RolloverRecord],
    config: &ExecutionConfig,
    ex: Experiment,
    multiplier: f64,
    guard: Option<EntryGuard>,
) -> ResultRow {
    evaluate_research(
        ev,
        raw,
        rolls,
        config,
        ex,
        multiplier,
        guard,
        ResearchRules::default(),
    )
}
#[allow(clippy::too_many_arguments)]
pub fn evaluate_research(
    ev: &ReplayEvent,
    raw: &[Kline],
    rolls: &[RolloverRecord],
    config: &ExecutionConfig,
    ex: Experiment,
    multiplier: f64,
    guard: Option<EntryGuard>,
    rules: ResearchRules,
) -> ResultRow {
    evaluate_research_with_holidays(ev, raw, rolls, config, ex, multiplier, guard, rules, &[])
}
#[allow(clippy::too_many_arguments)]
pub fn evaluate_research_with_holidays(
    ev: &ReplayEvent,
    raw: &[Kline],
    rolls: &[RolloverRecord],
    config: &ExecutionConfig,
    ex: Experiment,
    multiplier: f64,
    guard: Option<EntryGuard>,
    rules: ResearchRules,
    holidays: &[HolidayWindow],
) -> ResultRow {
    let large = ex.policy == Policy::ActualTier && ev.setup_features.setup_quality >= 3.5;
    let volume = if large {
        config.default_large_volume.max(1)
    } else if ex.policy == Policy::ActualTier {
        config.default_small_volume.max(1)
    } else {
        1
    };
    let mut state = State {
        e: Evaluation {
            status: "pending".into(),
            volume,
            ..Default::default()
        },
        remaining: volume,
        first: false,
        stop: ev.stop,
        peak: 0.,
        tier: if large {
            PositionTier::Large
        } else {
            PositionTier::Small
        },
        target: ev.target,
        fills: Vec::new(),
        guard,
        multiplier,
        rules,
        breakout_at: None,
        confirmed_at: None,
        holidays: holidays.to_vec(),
    };
    if !(rules.disable_admission_for_fixed_policy && ex.policy != Policy::ActualTier)
        && (ev.setup_features.setup_quality < 2.5
            || ev.risk / ev.tick_size + 1e-6 < config.min_entry_risk_ticks as f64)
    {
        state.e.status = "filtered".into();
        return ResultRow {
            evaluation: state.e,
            disagreement: None,
            fills: state.fills,
        };
    }
    let sign = if ev.direction == "up" { 1. } else { -1. };
    if guard.is_some_and(|g| {
        let proposed = rules.expected_entry(ev, g.assumed_slip_ticks, sign);
        !g.allows_target(ev, proposed, rules.target(ev, proposed, sign), multiplier)
    }) {
        state.e.status = "filtered_net_rr_plan".into();
        return ResultRow {
            evaluation: state.e,
            disagreement: None,
            fills: state.fills,
        };
    }
    let mut previous: Option<NaiveDateTime> = None;
    let mut last_bucket = String::new();
    let mut waited = 0;
    let mut held = 0;
    let mut disagreement = None;
    for bar in raw.iter().filter(|b| b.datetime > ev.warning_ts) {
        let Ok(dt) = NaiveDateTime::parse_from_str(&bar.datetime, "%Y-%m-%d %H:%M:%S") else {
            state.finish("missing_data", &bar.datetime);
            break;
        };
        // No quote at the predeclared liquidation time means unknown execution,
        // not a fictional fill at the post-holiday open (or a later bar).
        if state.e.fill.is_some()
            && holidays.iter().any(|h| {
                state
                    .e
                    .entry_ts
                    .as_deref()
                    .is_some_and(|t| t < h.flat_at.as_str())
                    && bar.datetime > h.flat_at
                    && previous
                        .is_some_and(|p| p.format("%Y-%m-%d %H:%M:%S").to_string() < h.flat_at)
            })
        {
            state.finish("missing_data", &bar.datetime);
            break;
        }
        if let Some(prior) = previous {
            let gap = (dt - prior).num_minutes();
            if gap > 5 && gap <= 180 {
                let missing = prior + chrono::Duration::minutes(5);
                if let Some(local) = chrono::Local.from_local_datetime(&missing).single() {
                    if crate::session::SessionCalendar::is_trading_time(&ev.symbol, &local) {
                        state.finish("missing_data", &bar.datetime);
                        break;
                    }
                }
            }
        }
        previous = Some(dt);
        let bucket = crate::derive::current_bucket_end_ts(dt, Timeframe::M15);
        if bucket != last_bucket {
            last_bucket = bucket.clone();
            if state.e.fill.is_none() {
                waited += 1;
            } else {
                held += 1;
            }
        }
        if rolls
            .iter()
            .any(|r| r.confirmed && r.ts > ev.warning_ts && r.ts <= bar.datetime)
        {
            state.finish("rollover", &bar.datetime);
            break;
        }
        if state.e.fill.is_none() && waited > rules.pending_buckets.unwrap_or(PENDING_BARS) {
            state.finish("untriggered", &bar.datetime);
            break;
        }
        let had_fill = state.e.fill.is_some();
        let mut paths = [state.clone(), state.clone()];
        for (index, path) in paths.iter_mut().enumerate() {
            let vertices = if index == 0 {
                [bar.open, bar.high, bar.low, bar.close]
            } else {
                [bar.open, bar.low, bar.high, bar.close]
            };
            path.open(bar.open, ev, ex, sign, &bar.datetime);
            for segment in vertices.windows(2) {
                path.segment(segment[0], segment[1], ev, ex, sign, &bar.datetime);
            }
        }
        if !agree(&paths[0], &paths[1]) {
            disagreement = Some(Disagreement {
                ts: bar.datetime.clone(),
                open_high_low_close: paths[0].e.clone(),
                open_low_high_close: paths[1].e.clone(),
            });
            state.finish("ambiguous", &bar.datetime);
            break;
        }
        state = paths[0].clone();
        if !had_fill && state.e.fill.is_some() {
            held = 1;
        }
        if state.terminal() {
            break;
        }
        if state.e.fill.is_some() && holidays.iter().any(|h| bar.datetime == h.flat_at) {
            state.realize(
                bar.close - sign * ex.slip_ticks * ev.tick_size,
                state.remaining,
                "pre_holiday",
                sign,
                &bar.datetime,
            );
            break;
        }
        if state.e.fill.is_some()
            && rules.flat_before_weekend
            && pre_weekend(&ev.symbol, &bar.datetime)
        {
            state.realize(
                bar.close - sign * ex.slip_ticks * ev.tick_size,
                state.remaining,
                "pre_weekend",
                sign,
                &bar.datetime,
            );
            break;
        }
        if state.e.fill.is_some()
            && (rules.flat_before_long_break
                || rules
                    .exit_before_break_below_r
                    .is_some_and(|r| (bar.close - state.e.fill.unwrap()) * sign / state.e.risk < r))
            && pre_long_break(&ev.symbol, &bar.datetime)
        {
            state.realize(
                bar.close - sign * ex.slip_ticks * ev.tick_size,
                state.remaining,
                "pre_long_break",
                sign,
                &bar.datetime,
            );
            break;
        }
        if state.e.fill.is_none()
            && rules.entry_mode == EntryMode::CloseConfirmNextOpen
            && (bar.close - ev.entry) * sign >= 0.
        {
            state.confirmed_at = Some(bar.datetime.clone());
        }
        if state.e.fill.is_some() && bar.datetime == bucket {
            let reason = if held >= rules.max_holding_buckets.unwrap_or(TIME_HORIZON_BARS) {
                Some("time_exit")
            } else if rules.fast_fail_bucket.is_some_and(|n| held == n)
                && state.peak < NO_FOLLOW_MFE_R
                && (bar.close - ev.entry) * sign < 0.
            {
                Some("failed_breakout")
            } else if ex.no_follow_bar.is_some_and(|n| held == n) && state.peak < NO_FOLLOW_MFE_R {
                Some("no_follow")
            } else {
                None
            };
            if let Some(reason) = reason {
                state.realize(
                    bar.close - sign * ex.slip_ticks * ev.tick_size,
                    state.remaining,
                    reason,
                    sign,
                    &bar.datetime,
                );
                break;
            }
        }
    }
    if state.e.status == "open" {
        state.e.status = "unfinished".into();
    }
    if state.e.status == "closed" {
        let net = state.e.gross_points * multiplier
            - ex.fee_ticks * ev.tick_size * multiplier * volume as f64;
        state.e.net_amount = Some(net);
        state.e.net_r = Some(net / (state.e.risk * multiplier * volume as f64));
    }
    ResultRow {
        evaluation: state.e,
        disagreement,
        fills: state.fills,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn weekend_rule_uses_the_last_symbol_session_and_not_friday_day_close_for_night_symbols() {
        assert!(!pre_weekend("EG0", "2026-07-24 14:55:00"));
        assert!(pre_weekend("EG0", "2026-07-24 22:50:00"));
        assert!(pre_weekend("JD0", "2026-07-24 14:50:00"));
        assert!(pre_weekend("SF0", "2026-07-24 14:50:00"));
        assert!(pre_weekend("SM0", "2026-07-24 14:50:00"));
        assert!(pre_weekend("PB0", "2026-07-25 00:50:00"));
        assert!(!pre_weekend("EG0", "2026-07-23 22:50:00"));
    }
    #[test]
    fn weekend_rule_exits_at_observed_close_and_shorter_holding_limit_counts_completed_buckets() {
        let mut e = event(false, 3.);
        e.warning_ts = "2026-07-24 22:30:00".into();
        e.symbol = "EG0".into();
        let mut bars = Vec::new();
        for minute in [35, 40, 45, 50] {
            let mut b = bar(50, 100., 104., 99., 102.);
            b.datetime = format!("2026-07-24 22:{minute}:00");
            bars.push(b);
        }
        let r = research(
            &e,
            &bars,
            ResearchRules {
                flat_before_weekend: true,
                ..Default::default()
            },
        );
        assert_eq!(r.evaluation.exit_reason.as_deref(), Some("pre_weekend"));
        assert_eq!(r.evaluation.exit_ts.as_deref(), Some("2026-07-24 22:50:00"));
        assert_eq!(r.evaluation.final_price, Some(102.));
        let bars: Vec<_> = (50..=75)
            .step_by(5)
            .map(|m| bar(m, 100., 104., 99., 102.))
            .collect();
        let r = research(
            &event(false, 3.),
            &bars,
            ResearchRules {
                max_holding_buckets: Some(2),
                ..Default::default()
            },
        );
        assert_eq!(r.evaluation.exit_reason.as_deref(), Some("time_exit"));
        assert_eq!(r.evaluation.exit_ts.as_deref(), Some("2026-01-05 10:15:00"));
    }
    fn scheduled(e: &ReplayEvent, bars: &[Kline], holidays: &[HolidayWindow]) -> ResultRow {
        evaluate_research_with_holidays(
            e,
            bars,
            &[],
            &ExecutionConfig::default(),
            Experiment {
                policy: Policy::OriginalS1,
                no_follow_bar: None,
                slip_ticks: 1.,
                fee_ticks: 0.,
            },
            10.,
            None,
            ResearchRules::default(),
            holidays,
        )
    }
    fn holiday() -> HolidayWindow {
        HolidayWindow {
            name: "unit-test holiday".into(),
            block_from: "2026-01-05 14:30:00".into(),
            flat_at: "2026-01-05 14:45:00".into(),
            last_close: "2026-01-05 15:00:00".into(),
            reopen: "2026-01-08 09:00:00".into(),
        }
    }
    #[test]
    fn holiday_schedule_flattens_winners_and_losers_but_not_ordinary_lunch() {
        let mut e = event(false, 3.);
        e.warning_ts = "2026-01-05 14:15:00".into();
        for close in [97., 104.] {
            let bars = [
                bar(320, 100., 104., 96., 102.),
                bar(345, 102., 105., 96., close),
            ];
            // Fill the missing regular bars so missing-data detection is meaningful.
            let mut full = vec![bars[0].clone()];
            for minute in [325, 330, 335, 340] {
                full.push(bar(minute, 102., 104., 96., 102.));
            }
            full.push(bars[1].clone());
            let r = scheduled(&e, &full, &[holiday()]);
            assert_eq!(r.evaluation.exit_reason.as_deref(), Some("pre_holiday"));
            assert_eq!(r.evaluation.exit_ts.as_deref(), Some("2026-01-05 14:45:00"));
            assert_eq!(r.evaluation.final_price, Some(close - 1.));
        }
        let mut e = event(false, 3.);
        e.warning_ts = "2026-01-05 11:15:00".into();
        let r = scheduled(
            &e,
            &[
                bar(140, 100., 104., 99., 102.),
                bar(145, 102., 104., 101., 103.),
                bar(150, 103., 104., 102., 103.),
            ],
            &[holiday()],
        );
        assert_eq!(r.evaluation.status, "unfinished");
    }
    #[test]
    fn holiday_blocks_new_orders_and_missing_liquidation_quote_stays_unknown() {
        let mut e = event(false, 3.);
        e.warning_ts = "2026-01-05 14:25:00".into();
        let r = scheduled(&e, &[bar(330, 100., 104., 99., 102.)], &[holiday()]);
        assert_eq!(r.evaluation.status, "filtered_holiday_window");
        assert!(r.fills.is_empty());
        e.warning_ts = "2026-01-05 14:15:00".into();
        let mut later = bar(20, 140., 145., 135., 142.);
        later.datetime = "2026-01-08 09:05:00".into();
        let r = scheduled(&e, &[bar(320, 100., 104., 99., 102.), later], &[holiday()]);
        assert_eq!(r.evaluation.status, "missing_data");
        assert_eq!(r.fills.len(), 1);
        assert!(r.evaluation.net_amount.is_none());
    }
    #[test]
    fn holiday_does_not_grant_an_entry_during_the_reopening_gap() {
        let mut e = event(false, 3.);
        e.warning_ts = "2026-01-05 14:25:00".into();
        let mut b = bar(20, 100., 105., 99., 104.);
        b.datetime = "2026-01-08 09:05:00".into();
        let r = scheduled(&e, &[b], &[holiday()]);
        assert_eq!(
            r.evaluation.entry_ts.as_deref(),
            Some("2026-01-08 09:05:00")
        );
        // It is a new post-reopen fill, not a held position credited with a gap.
        assert_eq!(r.evaluation.fill, Some(101.));
    }
    #[test]
    fn long_break_flattening_uses_declared_calendar_not_future_bars() {
        assert!(!pre_long_break("RB0", "2026-01-05 10:10:00"));
        assert!(pre_long_break("RB0", "2026-01-05 11:20:00"));
        assert!(pre_long_break("RB0", "2026-01-05 14:50:00"));
        assert!(!pre_long_break("RB0", "2026-01-05 14:45:00"));
        let mut e = event(false, 3.);
        e.warning_ts = "2026-01-05 14:30:00".into();
        let r = research(
            &e,
            &[
                bar(335, 100., 104., 99., 102.),
                bar(340, 102., 104., 101., 103.),
                bar(345, 103., 104., 101., 102.),
                bar(350, 102., 104., 101., 102.),
            ],
            ResearchRules {
                flat_before_long_break: true,
                ..Default::default()
            },
        );
        assert_eq!(r.evaluation.exit_reason.as_deref(), Some("pre_long_break"));
        assert_eq!(r.evaluation.exit_ts.as_deref(), Some("2026-01-05 14:50:00"));
    }
    #[test]
    fn selective_break_exit_keeps_progressing_positions_and_closes_unproven_ones() {
        let mut e = event(false, 3.);
        e.warning_ts = "2026-01-05 14:30:00".into();
        let bars = [
            bar(335, 100., 104., 99., 102.),
            bar(340, 102., 104., 101., 103.),
            bar(345, 103., 104., 101., 102.),
            bar(350, 102., 104., 101., 102.),
        ];
        let r = research(
            &e,
            &bars,
            ResearchRules {
                exit_before_break_below_r: Some(0.5),
                ..Default::default()
            },
        );
        assert_eq!(r.evaluation.exit_reason.as_deref(), Some("pre_long_break"));
        let bars = [
            bar(335, 100., 104., 99., 102.),
            bar(340, 102., 106., 101., 105.),
            bar(345, 105., 107., 104., 106.),
            bar(350, 106., 108., 105., 107.),
        ];
        let r = research(
            &e,
            &bars,
            ResearchRules {
                exit_before_break_below_r: Some(0.5),
                ..Default::default()
            },
        );
        assert_eq!(r.evaluation.status, "unfinished");
        assert_eq!(r.fills.len(), 1);
        let r = research(
            &e,
            &[bar(350, 100., 104., 99., 102.)],
            ResearchRules {
                skip_prebreak_entries: true,
                ..Default::default()
            },
        );
        assert_eq!(r.evaluation.status, "filtered_close_window");
        assert!(r.fills.is_empty());
    }
    fn research(e: &ReplayEvent, bars: &[Kline], rules: ResearchRules) -> ResultRow {
        evaluate_research(
            e,
            bars,
            &[],
            &ExecutionConfig::default(),
            Experiment {
                policy: Policy::OriginalS1,
                no_follow_bar: None,
                slip_ticks: 0.,
                fee_ticks: 0.,
            },
            10.,
            None,
            rules,
        )
    }
    #[test]
    fn confirmation_waits_for_next_bar_open_not_the_confirming_close() {
        let r = research(
            &event(false, 3.),
            &[
                bar(50, 99., 105., 98., 104.),
                bar(55, 102., 108., 101., 106.),
            ],
            ResearchRules {
                entry_mode: EntryMode::CloseConfirmNextOpen,
                ..Default::default()
            },
        );
        assert_eq!(
            r.evaluation.entry_ts.as_deref(),
            Some("2026-01-05 09:55:00")
        );
        assert_eq!(r.evaluation.fill, Some(102.));
        assert_eq!(r.fills.len(), 1);
    }
    #[test]
    fn retest_cannot_arm_and_fill_in_the_same_five_minute_bar() {
        let r = research(
            &event(false, 3.),
            &[bar(50, 99., 105., 96., 103.)],
            ResearchRules {
                entry_mode: EntryMode::BreakoutRetestQuarter,
                ..Default::default()
            },
        );
        assert!(r.evaluation.fill.is_none());
        assert!(r.fills.is_empty());
        let r = research(
            &event(false, 3.),
            &[bar(50, 99., 105., 96., 103.), bar(55, 99., 99., 97., 99.)],
            ResearchRules {
                entry_mode: EntryMode::BreakoutRetestQuarter,
                ..Default::default()
            },
        );
        assert_eq!(r.evaluation.fill, Some(97.5));
        assert_eq!(
            r.evaluation.entry_ts.as_deref(),
            Some("2026-01-05 09:55:00")
        );
    }
    #[test]
    fn retest_must_not_shrink_actual_risk_below_frozen_minimum() {
        let r = research(
            &event(false, 3.),
            &[bar(50, 99., 105., 98., 103.), bar(55, 99., 101., 97., 99.)],
            ResearchRules {
                entry_mode: EntryMode::BreakoutRetestQuarter,
                min_actual_risk_ticks: Some(10.),
                ..Default::default()
            },
        );
        assert_eq!(r.evaluation.status, "filtered_actual_risk");
        assert!(r.fills.is_empty());
    }
    #[test]
    fn capped_target_uses_actual_fill_risk_and_reduces_s1_only() {
        let r = research(
            &event(false, 3.),
            &[
                bar(50, 100., 105., 99., 104.),
                bar(55, 104., 118., 103., 117.),
            ],
            ResearchRules {
                target_cap_r: Some(1.5),
                ..Default::default()
            },
        );
        assert_eq!(r.evaluation.status, "closed");
        assert_eq!(r.evaluation.final_price, Some(115.));
        assert!(near(r.evaluation.gross_points, 15.));
    }
    #[test]
    fn untouched_s1_before_retest_is_a_missed_opportunity_never_a_win() {
        let r = research(
            &event(false, 3.),
            &[
                bar(50, 100., 105., 99., 104.),
                bar(55, 135., 136., 134., 135.),
            ],
            ResearchRules {
                entry_mode: EntryMode::BreakoutRetestQuarter,
                ..Default::default()
            },
        );
        assert_eq!(r.evaluation.status, "missed_target");
        assert!(r.evaluation.net_amount.is_none());
        assert!(r.fills.is_empty());
    }
    fn guard() -> EntryGuard {
        EntryGuard {
            min_net_reward_loss: 1.,
            assumed_slip_ticks: 1.,
            open: FeeRule::default(),
            close_today: FeeRule::default(),
            close_yesterday: FeeRule::default(),
            roundtrip_fixed_override: Some(2.),
        }
    }
    #[test]
    fn net_space_gate_includes_both_sides_slippage_and_commission() {
        let mut e = event(false, 3.);
        e.target = 114.;
        let ex = Experiment {
            policy: Policy::OriginalS1,
            no_follow_bar: Some(5),
            slip_ticks: 1.,
            fee_ticks: 0.,
        };
        assert!(!guard().allows(&e, 101., ex, 10.));
        e.target = 116.;
        assert!(guard().allows(&e, 101., ex, 10.));
        assert!(!guard().allows(&e, 102., ex, 10.));
    }
    #[test]
    fn execution_stress_does_not_reselect_the_warning_space_gate() {
        let mut e = event(false, 3.);
        e.target = 116.;
        let r = evaluate_guarded(
            &e,
            &[bar(50, 100., 105., 99., 101.)],
            &[],
            &ExecutionConfig::default(),
            Experiment {
                policy: Policy::OriginalS1,
                no_follow_bar: Some(5),
                slip_ticks: 2.,
                fee_ticks: 0.,
            },
            10.,
            Some(guard()),
        );
        // Forecast stays 1 tick, so warning passes. Actual 2-tick proposed fill
        // can then fail causally, before any position is booked.
        assert_eq!(r.evaluation.status, "filtered_net_rr_fill");
        assert!(r.fills.is_empty());
    }
    #[test]
    fn commission_bound_uses_worse_close_and_actual_turnover() {
        let g = EntryGuard {
            open: FeeRule {
                fixed_yuan_per_lot: 0.,
                notional_rate: 0.0001,
            },
            close_today: FeeRule::default(),
            close_yesterday: FeeRule {
                fixed_yuan_per_lot: 2.,
                notional_rate: 0.0001,
            },
            roundtrip_fixed_override: None,
            ..guard()
        };
        assert!(near(g.commission_bound(100., 120., 10.), 2.22));
    }
    #[test]
    fn gap_rechecks_space_before_assigning_any_fill() {
        let mut e = event(false, 3.);
        e.target = 116.;
        let r = evaluate_guarded(
            &e,
            &[bar(50, 101., 103., 100., 102.)],
            &[],
            &ExecutionConfig::default(),
            Experiment {
                policy: Policy::OriginalS1,
                no_follow_bar: Some(5),
                slip_ticks: 1.,
                fee_ticks: 0.,
            },
            10.,
            Some(guard()),
        );
        assert_eq!(r.evaluation.status, "filtered_net_rr_fill");
        assert!(r.evaluation.fill.is_none());
        assert!(r.fills.is_empty());
    }
    #[test]
    fn one_r_protection_retains_one_lot_and_never_claims_cost_free_breakeven() {
        for short in [false, true] {
            let e = event(short, 3.);
            let sign = if short { -1. } else { 1. };
            let prices = [
                (100., 105., 99., 104.),
                (105., 112., 104., 110.),
                (105., 108., 99., 100.),
            ];
            let bars: Vec<_> = prices
                .iter()
                .enumerate()
                .map(|(i, &(o, h, l, c))| {
                    if short {
                        bar(50 + i as u32 * 5, 200. - o, 200. - l, 200. - h, 200. - c)
                    } else {
                        bar(50 + i as u32 * 5, o, h, l, c)
                    }
                })
                .collect();
            let r = evaluate(
                &e,
                &bars,
                &[],
                &ExecutionConfig::default(),
                Experiment {
                    policy: Policy::S1ProtectOneR,
                    no_follow_bar: None,
                    slip_ticks: 0.,
                    fee_ticks: 0.2,
                },
                10.,
            );
            assert_eq!(r.evaluation.volume, 1);
            assert_eq!(r.evaluation.status, "closed");
            assert_eq!(r.evaluation.exit_reason.as_deref(), Some("protection"));
            assert!(near(r.evaluation.gross_points, 0.));
            assert!(r.evaluation.net_amount.unwrap() < 0.);
            assert!(near((r.fills[1].price - r.fills[0].price) * sign, 0.));
        }
    }
    fn event(short: bool, score: f64) -> ReplayEvent {
        let sign = if short { -1. } else { 1. };
        let mut e = ReplayEvent {
            symbol: "RB0".into(),
            direction: if short { "down" } else { "up" }.into(),
            warning_ts: "2026-01-05 09:45:00".into(),
            entry: 100.,
            stop: 100. - sign * 10.,
            target: 100. + sign * 30.,
            risk: 10.,
            tick_size: 1.,
            ..Default::default()
        };
        e.setup_features.setup_quality = score;
        e
    }
    fn bar(minute: u32, o: f64, h: f64, l: f64, c: f64) -> Kline {
        Kline {
            datetime: format!("2026-01-05 {:02}:{:02}:00", 9 + minute / 60, minute % 60),
            open: o,
            high: h,
            low: l,
            close: c,
            volume: 1.,
            hold: 0.,
        }
    }
    fn run(
        e: &ReplayEvent,
        bars: &[Kline],
        policy: Policy,
        no_follow_bar: Option<usize>,
    ) -> ResultRow {
        evaluate(
            e,
            bars,
            &[],
            &ExecutionConfig::default(),
            Experiment {
                policy,
                no_follow_bar,
                slip_ticks: 0.,
                fee_ticks: 0.,
            },
            10.,
        )
    }
    #[test]
    fn ambiguous_paths_are_not_assigned_a_loss_or_averaged() {
        let r = run(
            &event(false, 3.),
            &[bar(50, 95., 112., 89., 105.)],
            Policy::FixedOneR,
            None,
        );
        assert_eq!(r.evaluation.status, "ambiguous");
        assert!(r.evaluation.net_amount.is_none());
        assert!(r.disagreement.is_some());
    }
    #[test]
    fn fill_ledger_retains_each_partial_exit_and_reconciles_gross_profit() {
        let bars = [
            bar(50, 100., 105., 99., 104.),
            bar(55, 105., 111., 104., 110.),
            bar(60, 105., 108., 99., 100.),
        ];
        let r = run(&event(false, 4.), &bars, Policy::ActualTier, None);
        assert_eq!(r.evaluation.status, "closed");
        assert_eq!(r.fills.len(), 3);
        assert_eq!(r.fills[0].volume, 2);
        assert_eq!(r.fills[0].offset, "open");
        assert_eq!(r.fills[1].volume, 1);
        assert_eq!(r.fills[2].volume, 1);
        assert_ne!(r.fills[1].ts, r.fills[2].ts);
        let points: f64 = r.fills[1..]
            .iter()
            .map(|f| (f.price - r.fills[0].price) * f.volume as f64)
            .sum();
        assert!(near(points, r.evaluation.gross_points));
    }
    #[test]
    fn partial_exit_cash_is_summed_and_fees_are_charged_once_per_initial_lot() {
        let bars = [
            bar(50, 100., 105., 99., 104.),
            bar(55, 105., 111., 104., 110.),
            bar(60, 105., 108., 99., 100.),
        ];
        let e = event(false, 4.);
        let r = evaluate(
            &e,
            &bars,
            &[],
            &ExecutionConfig::default(),
            Experiment {
                policy: Policy::ActualTier,
                no_follow_bar: None,
                slip_ticks: 0.,
                fee_ticks: 2.5,
            },
            10.,
        )
        .evaluation;
        assert_eq!(r.status, "closed");
        assert!(near(r.gross_points, 10.));
        assert!(near(r.net_amount.unwrap(), 50.));
        assert!(near(r.net_r.unwrap(), 0.25));
    }
    #[test]
    fn mirrored_prices_give_identical_profit() {
        let bars = [
            bar(50, 100., 105., 99., 104.),
            bar(55, 104., 112., 103., 111.),
        ];
        let mirror = bars
            .iter()
            .map(|b| Kline {
                open: 200. - b.open,
                high: 200. - b.low,
                low: 200. - b.high,
                close: 200. - b.close,
                ..b.clone()
            })
            .collect::<Vec<_>>();
        for policy in [
            Policy::OriginalS1,
            Policy::FixedOneR,
            Policy::LegacyStaircase,
            Policy::ActualTier,
        ] {
            let a = run(&event(false, 3.), &bars, policy, None);
            let b = run(&event(true, 3.), &mirror, policy, None);
            assert_eq!(a.evaluation.status, b.evaluation.status);
            assert!(near(a.evaluation.gross_points, b.evaluation.gross_points));
        }
    }
    #[test]
    fn no_follow_fires_only_on_fifth_completed_bucket_and_can_be_disabled() {
        let bars = (0..18)
            .map(|i| bar(50 + i * 5, 100., 104., 99., 101.))
            .collect::<Vec<_>>();
        let e = event(false, 3.);
        let a = run(&e, &bars, Policy::FixedOneR, Some(5)).evaluation;
        assert_eq!(a.exit_ts.as_deref(), Some("2026-01-05 11:00:00"));
        assert_eq!(a.exit_reason.as_deref(), Some("no_follow"));
        assert_eq!(
            run(&e, &bars, Policy::FixedOneR, None).evaluation.status,
            "unfinished"
        );
        assert_eq!(
            run(&e, &bars, Policy::FixedOneR, Some(6))
                .evaluation
                .exit_ts
                .as_deref(),
            Some("2026-01-05 11:15:00")
        );
    }
    #[test]
    fn original_s1_and_one_r_answer_different_questions() {
        let mut e = event(false, 3.);
        e.target = 105.;
        e.rr = 0.5;
        let bars = [bar(50, 100., 106., 99., 105.)];
        assert_eq!(
            run(&e, &bars, Policy::OriginalS1, None).evaluation.net_r,
            Some(0.5)
        );
        assert_eq!(
            run(&e, &bars, Policy::FixedOneR, None).evaluation.status,
            "unfinished"
        );
    }
    #[test]
    fn actual_policy_calls_shared_continuous_trailing() {
        let bars = [
            bar(50, 100., 105., 99., 104.),
            bar(55, 105., 111., 104., 110.),
            bar(60, 110., 117., 110., 116.),
            bar(65, 115., 116., 107., 108.),
        ];
        let r = run(&event(false, 4.), &bars, Policy::ActualTier, None).evaluation;
        assert_eq!(r.status, "closed");
        assert!(near(r.gross_points, 19.));
    }
    #[test]
    fn staircase_does_not_invent_high_before_low_order() {
        let r = run(
            &event(false, 3.),
            &[bar(50, 100., 115., 99., 114.)],
            Policy::LegacyStaircase,
            None,
        );
        assert_eq!(r.evaluation.status, "ambiguous");
    }
    #[test]
    fn old_profitable_label_is_not_an_input_to_replay() {
        let mut e = event(false, 3.);
        let bars = [bar(50, 100., 104., 99., 101.)];
        let before = run(&e, &bars, Policy::FixedOneR, None);
        e.evaluation = Some(Evaluation {
            status: "closed".into(),
            net_amount: Some(999999.),
            ..Default::default()
        });
        assert_eq!(
            serde_json::to_value(before).unwrap(),
            serde_json::to_value(run(&e, &bars, Policy::FixedOneR, None)).unwrap()
        );
    }
    #[test]
    fn lunch_break_does_not_count_as_held_bars() {
        let mut e = event(false, 3.);
        e.warning_ts = "2026-01-05 11:15:00".into();
        let minutes = [
            140, 145, 150, 275, 280, 285, 290, 295, 300, 305, 310, 315, 320, 325, 330,
        ];
        let bars = minutes
            .iter()
            .map(|m| bar(*m, 100., 104., 99., 101.))
            .collect::<Vec<_>>();
        let r = run(&e, &bars, Policy::FixedOneR, Some(5)).evaluation;
        assert_eq!(r.exit_ts.as_deref(), Some("2026-01-05 14:30:00"));
    }
    #[test]
    fn stop_gap_and_fee_do_not_silently_cap_losses_at_one_r() {
        let bars = [bar(50, 100., 104., 99., 103.), bar(55, 85., 87., 80., 84.)];
        let r = run(&event(false, 3.), &bars, Policy::FixedOneR, None).evaluation;
        assert_eq!(r.net_r, Some(-1.5));
    }
    #[test]
    fn all_policies_apply_same_predeclared_gate() {
        let e = event(false, 2.4);
        for policy in [
            Policy::OriginalS1,
            Policy::FixedOneR,
            Policy::LegacyStaircase,
            Policy::ActualTier,
        ] {
            assert_eq!(
                run(&e, &[bar(50, 100., 112., 99., 111.)], policy, None)
                    .evaluation
                    .status,
                "filtered"
            );
        }
    }
    #[test]
    fn score_study_bypass_is_explicit_and_never_disables_actual_tier_admission() {
        let mut e = event(false, 2.4);
        e.stop = 98.;
        e.risk = 2.;
        let bars = [bar(50, 100., 112., 99., 111.)];
        let rules = ResearchRules {
            disable_admission_for_fixed_policy: true,
            ..Default::default()
        };
        let ex = Experiment {
            policy: Policy::FixedOneR,
            no_follow_bar: None,
            slip_ticks: 0.,
            fee_ticks: 0.,
        };
        assert_eq!(
            evaluate(&e, &bars, &[], &ExecutionConfig::default(), ex, 10.)
                .evaluation
                .status,
            "filtered"
        );
        assert_eq!(
            evaluate_research(
                &e,
                &bars,
                &[],
                &ExecutionConfig::default(),
                ex,
                10.,
                None,
                rules
            )
            .evaluation
            .status,
            "closed"
        );
        let actual = Experiment {
            policy: Policy::ActualTier,
            ..ex
        };
        assert_eq!(
            evaluate_research(
                &e,
                &bars,
                &[],
                &ExecutionConfig::default(),
                actual,
                10.,
                None,
                rules
            )
            .evaluation
            .status,
            "filtered"
        );
    }
    #[test]
    fn truncated_trade_is_not_used_as_a_negative_label() {
        let r = run(
            &event(false, 3.),
            &[bar(50, 100., 104., 99., 103.)],
            Policy::FixedOneR,
            None,
        )
        .evaluation;
        assert_eq!(r.status, "unfinished");
        assert!(r.net_r.is_none());
    }
    #[test]
    fn target_already_passed_at_entry_is_a_rejected_order_not_a_fill() {
        let mut e = event(false, 3.);
        e.target = 101.;
        let r = run(
            &e,
            &[bar(50, 102., 104., 101., 103.)],
            Policy::OriginalS1,
            None,
        )
        .evaluation;
        assert_eq!(r.status, "invalid_target");
        assert!(r.fill.is_none());
        assert!(r.entry_ts.is_none());
        assert!(r.net_amount.is_none());
    }
}
