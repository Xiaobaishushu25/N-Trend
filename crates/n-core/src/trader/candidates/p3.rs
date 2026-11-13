//! P3: shared N admission/lifecycle, monetary sizing and explicit exit modes.
use super::*;
use crate::trader::policy::{align_protection, protection_reason};
use crate::trader::{
    model::{is_actual_futures_contract, ExecutionQuote},
    ExecutionConfig,
};

#[path = "p3/risk_budget.rs"]
mod risk_budget;
use risk_budget::{BudgetSettings, RiskBudget};

pub const ID: &str = "n_p3_v1";
pub const VERSION: &str = "1";
pub const EXIT: &str = "p3_exit_v1";
pub const SIZING: &str = "risk_budget_v1";
const FIRST_S1: &str = "p3_first_s1";

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ExitMode {
    FixedS1,
    HybridS1,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct P3Parameters {
    pub fees: FeeProfile,
    pub calendar: HolidayCalendar,
    pub risk_budget_yuan: f64,
    pub max_lots: i32,
    pub exit_mode: ExitMode,
    /// Explicit account/product deadlines may be earlier than the delivery month.
    #[serde(default)]
    pub entry_cutoffs: BTreeMap<String, String>,
}
impl P3Parameters {
    pub fn reference_snapshot() -> Self {
        let mut p = Parameters::reference_snapshot();
        p.calendar.id = format!("p3-10m-{}", p.calendar.id);
        for window in &mut p.calendar.windows {
            let cutoff = label(parse_time(&window.last_close).unwrap() - 600);
            window.block_from = cutoff.clone();
            window.flat_at = cutoff;
        }
        Self {
            fees: p.fees,
            calendar: p.calendar,
            risk_budget_yuan: 600.,
            max_lots: 2,
            exit_mode: ExitMode::FixedS1,
            entry_cutoffs: BTreeMap::new(),
        }
    }
    fn common(&self) -> Parameters {
        Parameters {
            fees: self.fees.clone(),
            calendar: self.calendar.clone(),
        }
    }
    fn budget(&self) -> Result<BudgetSettings> {
        BudgetSettings::new(self.risk_budget_yuan, self.max_lots)
    }
    pub fn validate(&self) -> Result<()> {
        self.common().validate()?;
        self.budget()?;
        for (instrument, timestamp) in &self.entry_cutoffs {
            ensure!(
                is_actual_futures_contract(instrument),
                "P3合约截止配置必须使用实际月份合约"
            );
            ensure!(
                instrument == &instrument.to_uppercase(),
                "P3合约截止配置必须使用大写合约编号"
            );
            parse_time(timestamp)?;
        }
        Ok(())
    }
}

pub struct P3Strategy {
    base: CandidateStrategy,
    budget: BudgetSettings,
    exit_mode: ExitMode,
    entry_cutoffs: BTreeMap<String, String>,
}
impl P3Strategy {
    pub fn new(p: P3Parameters) -> Result<Self> {
        p.validate()?;
        Ok(Self {
            base: CandidateStrategy::with_min_score(p.common(), 2.5)?,
            budget: p.budget()?,
            exit_mode: p.exit_mode,
            entry_cutoffs: p.entry_cutoffs,
        })
    }
    #[cfg(test)]
    pub(crate) fn with_test_calendar_time(mut self, timestamp: i64) -> Self {
        self.base = self.base.with_test_calendar_time(timestamp);
        self
    }
    fn contract_month(instrument: &str, now: i64) -> Option<(i32, u32)> {
        if !is_actual_futures_contract(instrument) {
            return None;
        }
        let prefix = SessionCalendar::contract_prefix(instrument);
        let digits = &instrument[prefix.len()..];
        let day = SessionCalendar::get(instrument).trading_day(&local(now).naive_local());
        let month = digits[digits.len() - 2..].parse::<u32>().ok()?;
        if !(1..=12).contains(&month) {
            return None;
        }
        let year = if digits.len() == 4 {
            2000 + digits[..2].parse::<i32>().ok()?
        } else {
            let digit = digits[..1].parse::<i32>().ok()?;
            (day.year() - 1..=day.year() + 8).find(|year| year % 10 == digit)?
        };
        Some((year, month))
    }
    fn expiry_cutoff(&self, instrument: &str, now: i64) -> Option<i64> {
        let (year, month) = Self::contract_month(instrument, now)?;
        let first = chrono::NaiveDate::from_ymd_opt(year, month, 1)?;
        for days in 1..=31 {
            let day = first - Duration::days(days);
            if !SessionCalendar::is_trading_day(&day) {
                continue;
            }
            let close = shanghai()
                .from_local_datetime(&day.and_hms_opt(15, 0, 0)?)
                .single()?
                .timestamp();
            if self.base.parameters.calendar.windows.iter().any(|h| {
                parse_time(&h.last_close).is_ok_and(|c| close > c)
                    && parse_time(&h.reopen).is_ok_and(|r| close < r)
            }) {
                continue;
            }
            return Some(close - 600);
        }
        None
    }
    fn forced_flat_reason(&self, instrument: &str, now: i64) -> Option<&'static str> {
        if parse_time(&self.base.parameters.calendar.coverage_end).is_ok_and(|end| now >= end - 600)
        {
            Some("pre_calendar_end")
        } else if self
            .expiry_cutoff(instrument, now)
            .is_some_and(|cut| now >= cut)
        {
            Some("contract_expiry")
        } else {
            None
        }
    }
    fn contract_block(&self, instrument: &str, now: i64) -> Option<&'static str> {
        let Some((year, month)) = Self::contract_month(instrument, now) else {
            return Some("invalid_actual_contract");
        };
        let day = SessionCalendar::get(instrument).trading_day(&local(now).naive_local());
        if (year, month) <= (day.year(), day.month()) {
            return Some("delivery_month");
        }
        if let Some(reason) = self.forced_flat_reason(instrument, now) {
            return Some(if reason == "contract_expiry" {
                "contract_expiry_window"
            } else {
                "calendar_coverage_ending"
            });
        }
        self.entry_cutoffs
            .get(&instrument.to_uppercase())
            .filter(|cut| parse_time(cut).is_ok_and(|t| now >= t))
            .map(|_| "contract_entry_cutoff")
    }
}
impl Strategy for P3Strategy {
    fn entry_constraints(&self) -> EntryConstraints {
        EntryConstraints {
            entry_timeout_seconds: Some(3),
            ..self.base.entry_constraints()
        }
    }
    fn check_entry(
        &self,
        plan: &EntryPlan,
        q: &ExecutionQuote,
        fill: f64,
        now: i64,
    ) -> EntryDecision {
        if let Some(reason) = self.contract_block(&plan.instrument_id, self.base.calendar_time(now))
        {
            return EntryDecision::Reject(reason.into());
        }
        let dt = local(self.base.calendar_time(now)).naive_local();
        let session = SessionCalendar::get(&plan.instrument_id);
        if !session.is_in_trading_time_naive(&dt)
            || (dt.second() > 0 && session.is_session_close(dt.hour(), dt.minute()))
        {
            return EntryDecision::Wait("outside_trading_session".into());
        }
        self.base.check_entry(plan, q, fill, now)
    }
    fn on_event(
        &self,
        event: &StrategyEvent,
        c: &StrategyContext<'_>,
        state: &mut Value,
    ) -> Result<Vec<TradeIntent>> {
        let intents = self.base.on_event(event, c, state)?;
        let mut result = vec![];
        for intent in intents {
            let TradeIntent::Open(mut plan) = intent else {
                result.push(intent);
                continue;
            };
            let snapshot: Snapshot = serde_json::from_value(plan.evidence["candidate"].clone())?;
            let reason = if SessionCalendar::contract_prefix(&snapshot.symbol)
                != SessionCalendar::contract_prefix(&snapshot.instrument)
            {
                Some("contract_metadata_mismatch")
            } else {
                self.contract_block(
                    &snapshot.instrument,
                    self.base.calendar_time(plan.created_at),
                )
            };
            let lots = risk_budget::initial_volume(&snapshot, &self.budget)?;
            if reason.is_some() || lots == 0 {
                let mut audit: State = serde_json::from_value(state.clone())?;
                if let Some(decision) = audit.decisions.get_mut(&plan.signal_id) {
                    decision["reason"] = json!(reason.unwrap_or("risk_budget_exceeded"));
                    decision["values"]["risk_budget_yuan"] = json!(self.budget.yuan());
                    decision["values"]["min_score"] = json!(2.5);
                }
                audit.pending.remove(&plan.signal_id);
                *state = serde_json::to_value(audit)?;
                continue;
            }
            plan.requested_volume = lots;
            plan.reason = "P3含成本金额预算与结构S1".into();
            plan.evidence["p3"] = json!({"risk_budget_yuan":self.budget.yuan(),
                "max_lots":self.budget.max_lots,"approved_lots":lots,"exit_mode":self.exit_mode});
            result.push(TradeIntent::Open(plan));
        }
        let event_time = match event {
            StrategyEvent::Quote(q) => Some(self.base.calendar_time(q.timestamp)),
            StrategyEvent::Timer { timestamp } => Some(self.base.calendar_time(*timestamp)),
            _ => None,
        };
        if let Some(now) = event_time {
            for condition in c.conditions.iter().filter(|o| {
                matches!(
                    o.state,
                    ConditionState::Pending
                        | ConditionState::WaitingConfirm
                        | ConditionState::Triggered
                        | ConditionState::Working
                        | ConditionState::Unknown
                        | ConditionState::PartFilled
                )
            }) {
                let roll = matches!(event, StrategyEvent::Quote(_))
                    && c.quotes.get(&condition.symbol).is_some_and(|q| {
                        !q.instrument_id
                            .eq_ignore_ascii_case(&condition.instrument_id)
                    });
                if let Some(reason) = self
                    .forced_flat_reason(&condition.instrument_id, now)
                    .or(roll.then_some("contract_rollover"))
                {
                    result.push(TradeIntent::CancelEntry {
                        condition_id: condition.id.clone(),
                        reason: reason.into(),
                    });
                }
            }
            for p in c.positions.iter().filter(|p| !p.is_closed && p.volume > 0) {
                if let Some(reason) = self.forced_flat_reason(&p.instrument_id, now) {
                    result.push(TradeIntent::Close {
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
        Ok(result)
    }
}

fn mode(parameters: &Value) -> Result<ExitMode> {
    Ok(serde_json::from_value(parameters["exit_mode"].clone())?)
}
struct P3Exit;
impl ExitPolicy for P3Exit {
    fn target_on_fill(&self, _: &ManagedPosition, plan: &EntryPlan) -> Option<f64> {
        plan.target_price
    }
    fn on_quote(&self, p: &ManagedPosition, q: &ExecutionQuote, parameters: &Value) -> ExitUpdate {
        let sign = if p.direction == "up" { 1. } else { -1. };
        let Ok(mode) = mode(parameters) else {
            return ExitUpdate {
                stop: p.current_stop_price,
                close: Some(("invalid_configuration".into(), None)),
            };
        };
        let mut stop = p.current_stop_price;
        if mode == ExitMode::HybridS1 && p.lot1_closed {
            let peak = p.max_mfe_r.max((q.last - p.fill_price) * sign / p.risk);
            let candidate = if peak >= 1.6 {
                p.fill_price + sign * (peak - 0.8) * p.risk
            } else {
                p.fill_price
            };
            let candidate = align_protection(candidate, sign, q.tick);
            stop = if sign > 0. {
                stop.max(candidate)
            } else {
                stop.min(candidate)
            };
        }
        let close = if (q.last - stop) * sign <= 0. {
            Some((
                protection_reason(p.lot1_closed, p.fill_price, stop, sign, q.tick).into(),
                None,
            ))
        } else if !p.lot1_closed && (q.last - p.target_price) * sign >= 0. {
            if mode == ExitMode::HybridS1 && p.volume == 2 {
                Some((FIRST_S1.into(), Some(1)))
            } else {
                Some(("target_s1".into(), None))
            }
        } else {
            None
        };
        ExitUpdate { stop, close }
    }
    fn on_partial_close(
        &self,
        p: &ManagedPosition,
        reason: &str,
        filled: i32,
        parameters: &Value,
        quote: Option<&ExecutionQuote>,
    ) -> PartialExitUpdate {
        let first = mode(parameters).is_ok_and(|m| m == ExitMode::HybridS1)
            && reason == FIRST_S1
            && filled == 1;
        let sign = if p.direction == "up" { 1. } else { -1. };
        let be = quote
            .filter(|q| {
                q.instrument_id.eq_ignore_ascii_case(&p.instrument_id)
                    && q.tick.is_finite()
                    && q.tick > 0.
            })
            .map_or(p.fill_price, |q| {
                align_protection(p.fill_price, sign, q.tick)
            });
        PartialExitUpdate {
            stop: if first { be } else { p.current_stop_price },
            first_target_completed: p.lot1_closed || first,
        }
    }
}

pub fn definition(parameters: P3Parameters) -> StrategyDefinition {
    StrategyDefinition {
        strategy_id: ID.into(),
        code_version: VERSION.into(),
        revision: "1".into(),
        exit_policy: EXIT.into(),
        sizing_policy: SIZING.into(),
        parameters: serde_json::to_value(parameters).unwrap(),
    }
}
pub fn validate_definition(d: &StrategyDefinition) -> Result<()> {
    if d.strategy_id != ID {
        ensure!(
            d.exit_policy != EXIT && d.sizing_policy != SIZING,
            "P3退出与金额预算模块必须配套P3策略使用"
        );
    }
    if d.strategy_id == ID {
        ensure!(
            d.code_version == VERSION && d.exit_policy == EXIT && d.sizing_policy == SIZING,
            "P3必须使用配套金额预算和退出模块"
        );
        serde_json::from_value::<P3Parameters>(d.parameters.clone())?.validate()?;
    }
    Ok(())
}
pub fn register(r: &mut StrategyRegistry) {
    r.register_strategy(ID, VERSION, |p| {
        Ok(Box::new(P3Strategy::new(serde_json::from_value(
            p.clone(),
        )?)?))
    });
    r.register_sizing(SIZING, Arc::new(RiskBudget));
    r.register_exit(EXIT, Arc::new(P3Exit));
}
pub fn preset() -> Value {
    json!({"label":"P3 · 600元预算 / S1与分批追踪","definition":definition(P3Parameters::reference_snapshot())})
}

#[cfg(test)]
#[path = "p3/tests.rs"]
mod tests;
