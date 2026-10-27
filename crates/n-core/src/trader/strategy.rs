//! Broker-independent, statically registered strategy extension points.
use super::{model::ExecutionQuote, ExecutionConfig, ManagedPosition};
use anyhow::{ensure, Result};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{collections::BTreeMap, sync::Arc};

pub const N_STRATEGY: &str = "n_pattern";
pub const N_VERSION: &str = "1";

#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq, Eq)]
#[serde(default)]
pub struct TradeOrigin {
    pub account_id: String,
    pub instance_id: String,
    pub strategy_id: String,
    pub strategy_version: String,
    pub signal_id: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StrategyDefinition {
    pub strategy_id: String,
    pub code_version: String,
    pub revision: String,
    pub exit_policy: String,
    pub sizing_policy: String,
    #[serde(default)]
    pub parameters: Value,
}
impl Default for StrategyDefinition {
    fn default() -> Self {
        Self {
            strategy_id: N_STRATEGY.into(),
            code_version: N_VERSION.into(),
            revision: "1".into(),
            exit_policy: "n_exit_v1".into(),
            sizing_policy: "n_score_v1".into(),
            parameters: Value::Null,
        }
    }
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Direction {
    Long,
    Short,
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum EntryTrigger {
    Immediate,
    Breakout { price: f64 },
}

/// No N-pattern fields are required here. Evidence belongs to the strategy.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EntryPlan {
    pub signal_id: String,
    pub symbol: String,
    pub instrument_id: String,
    pub direction: Direction,
    pub reference_price: f64,
    pub trigger: EntryTrigger,
    pub stop_price: Option<f64>,
    pub target_price: Option<f64>,
    pub risk_per_unit: f64,
    pub requested_volume: i32,
    pub created_at: i64,
    pub expires_at: Option<i64>,
    pub reason: String,
    #[serde(default)]
    pub evidence: Value,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum TradeIntent {
    Open(EntryPlan),
    Close {
        position_id: String,
        direction: Direction,
        volume: Option<i32>,
        reason: String,
    },
    CancelEntry {
        condition_id: String,
        reason: String,
    },
    Protect {
        position_id: String,
        stop_price: f64,
    },
}

#[derive(Debug, Clone)]
pub enum StrategyEvent {
    History5m {
        symbol: String,
        bars: Vec<crate::fetch::kline::Kline>,
        rollovers: Vec<crate::derive::rollover::RolloverRecord>,
        timestamp: i64,
    },
    Signal {
        source: String,
        payload: Value,
    },
    Quote(ExecutionQuote),
    BarClosed {
        symbol: String,
        timeframe: String,
        timestamp: String,
        high: f64,
        low: f64,
        close: f64,
    },
    Timer {
        timestamp: i64,
    },
    ExecutionUpdate(Value),
}

pub struct StrategyContext<'a> {
    pub instance_id: &'a str,
    pub positions: &'a [ManagedPosition],
    pub orders: &'a [super::execution::ExecutionOrder],
    pub conditions: &'a [super::execution::ConditionOrder],
    pub account: Option<&'a super::model::TraderAccount>,
    pub quotes: &'a std::collections::HashMap<String, ExecutionQuote>,
    pub config: &'a ExecutionConfig,
    /// Old versions may manage positions but cannot create new entries.
    pub allow_entries: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MarketSubscription {
    pub symbol: String,
    pub timeframe: Option<String>,
}

/// Optional execution requirements; legacy and external strategies keep their defaults.
#[derive(Debug, Clone, Copy, Default)]
pub struct EntryConstraints {
    pub one_per_symbol: bool,
    pub next_quote: bool,
    pub terminal_rejections: bool,
    pub complete_15m: bool,
}
pub enum EntryDecision {
    Allow,
    Wait(String),
    Reject(String),
}

pub struct EntryPortfolio<'a> {
    pub conditions: &'a std::collections::HashMap<String, super::execution::ConditionOrder>,
    pub positions: &'a std::collections::HashMap<String, ManagedPosition>,
    pub orders: &'a std::collections::HashMap<String, super::execution::ExecutionOrder>,
    pub history: &'a [super::execution::TradeRecord],
    pub state: &'a Value,
}

pub trait Strategy: Send + Sync {
    fn uses_history(&self) -> bool {
        false
    }
    fn check_portfolio(
        &self,
        _plan: &EntryPlan,
        _portfolio: &EntryPortfolio<'_>,
        _now: i64,
    ) -> EntryDecision {
        EntryDecision::Allow
    }
    fn entry_constraints(&self) -> EntryConstraints {
        EntryConstraints::default()
    }
    /// Executed against the proposed limit before any broker submission.
    fn check_entry(
        &self,
        _plan: &EntryPlan,
        _quote: &ExecutionQuote,
        _fill: f64,
        _now: i64,
    ) -> EntryDecision {
        EntryDecision::Allow
    }
    fn subscriptions(&self) -> Vec<MarketSubscription> {
        vec![]
    }
    fn handles_event(&self, _event: &StrategyEvent) -> bool {
        true
    }
    /// State is JSON, persisted by the executor after processing each event.
    fn on_event(
        &self,
        event: &StrategyEvent,
        context: &StrategyContext<'_>,
        state: &mut Value,
    ) -> Result<Vec<TradeIntent>>;
}
pub trait SizingPolicy: Send + Sync {
    fn volume(&self, plan: &EntryPlan, config: &ExecutionConfig, parameters: &Value)
        -> Result<i32>;
}
pub struct ExitUpdate {
    pub stop: f64,
    pub close: Option<(String, Option<i32>)>,
}
pub trait ExitPolicy: Send + Sync {
    /// Called only after a confirmed fill. Pending conditions retain their original target.
    fn target_on_fill(&self, _position: &ManagedPosition, _plan: &EntryPlan) -> Option<f64> {
        None
    }
    fn on_quote(
        &self,
        position: &ManagedPosition,
        quote: &ExecutionQuote,
        parameters: &Value,
    ) -> ExitUpdate;
    fn on_bar(&self, _position: &ManagedPosition, _parameters: &Value) -> Option<String> {
        None
    }
}

type StrategyFactory = Arc<dyn Fn(&Value) -> Result<Box<dyn Strategy>> + Send + Sync>;
#[derive(Clone)]
pub struct StrategyRegistry {
    strategies: BTreeMap<(String, String), StrategyFactory>,
    exits: BTreeMap<String, Arc<dyn ExitPolicy>>,
    sizing: BTreeMap<String, Arc<dyn SizingPolicy>>,
}
impl Default for StrategyRegistry {
    fn default() -> Self {
        let mut r = Self {
            strategies: BTreeMap::new(),
            exits: BTreeMap::new(),
            sizing: BTreeMap::new(),
        };
        r.register_strategy(N_STRATEGY, N_VERSION, |_| Ok(Box::new(NPatternStrategy)));
        r.register_exit("n_exit_v1", Arc::new(NExitPolicy));
        r.register_exit("protect_only_v1", Arc::new(ProtectOnly));
        r.register_sizing("n_score_v1", Arc::new(NScoreSizing));
        r.register_sizing("fixed_v1", Arc::new(FixedSizing));
        super::candidates::register(&mut r);
        super::b5::register(&mut r);
        r
    }
}
impl StrategyRegistry {
    pub fn register_strategy(
        &mut self,
        id: &str,
        version: &str,
        factory: impl Fn(&Value) -> Result<Box<dyn Strategy>> + Send + Sync + 'static,
    ) {
        self.strategies
            .insert((id.into(), version.into()), Arc::new(factory));
    }
    pub fn register_exit(&mut self, id: &str, policy: Arc<dyn ExitPolicy>) {
        self.exits.insert(id.into(), policy);
    }
    pub fn register_sizing(&mut self, id: &str, policy: Arc<dyn SizingPolicy>) {
        self.sizing.insert(id.into(), policy);
    }
    pub fn strategy(&self, d: &StrategyDefinition) -> Result<Box<dyn Strategy>> {
        self.strategies
            .get(&(d.strategy_id.clone(), d.code_version.clone()))
            .ok_or_else(|| anyhow::anyhow!("未注册策略版本 {}:{}", d.strategy_id, d.code_version))?(
            &d.parameters,
        )
    }
    pub fn exit(&self, id: &str) -> Result<&dyn ExitPolicy> {
        self.exits
            .get(id)
            .map(|p| p.as_ref())
            .ok_or_else(|| anyhow::anyhow!("未注册退出模块 {id}"))
    }
    pub fn sizing(&self, id: &str) -> Result<&dyn SizingPolicy> {
        self.sizing
            .get(id)
            .map(|p| p.as_ref())
            .ok_or_else(|| anyhow::anyhow!("未注册仓位模块 {id}"))
    }
    pub fn validate(&self, d: &StrategyDefinition) -> Result<()> {
        ensure!(!d.revision.is_empty(), "策略参数版本不能为空");
        self.strategy(d)?;
        self.exit(&d.exit_policy)?;
        self.sizing(&d.sizing_policy)?;
        super::candidates::validate_definition(d)?;
        super::b5::validate_definition(d)?;
        Ok(())
    }
    pub fn catalog(&self) -> Value {
        let mut presets = super::candidates::presets()
            .as_array()
            .cloned()
            .unwrap_or_default();
        presets.push(super::b5::preset());
        serde_json::json!({"strategies":self.strategies.keys().map(|(id,v)|serde_json::json!({"id":id,"version":v})).collect::<Vec<_>>(),
            "exit_policies":self.exits.keys().collect::<Vec<_>>(), "sizing_policies":self.sizing.keys().collect::<Vec<_>>(),
            "presets":presets})
    }
}

struct NPatternStrategy;
impl Strategy for NPatternStrategy {
    fn handles_event(&self, event: &StrategyEvent) -> bool {
        matches!(event,StrategyEvent::Signal{source,..} if source==N_STRATEGY)
    }
    fn on_event(
        &self,
        event: &StrategyEvent,
        c: &StrategyContext<'_>,
        _: &mut Value,
    ) -> Result<Vec<TradeIntent>> {
        let StrategyEvent::Signal { source, payload: p } = event else {
            return Ok(vec![]);
        };
        if source != N_STRATEGY || !c.allow_entries {
            return Ok(vec![]);
        }
        let score = p["entry_score"].as_f64().unwrap_or(0.0);
        if score < 2.5 {
            return Ok(vec![]);
        }
        let direction = match p["direction"].as_str() {
            Some("up") => Direction::Long,
            Some("down") => Direction::Short,
            _ => return Ok(vec![]),
        };
        let entry = p["entry"].as_f64().unwrap_or(0.0);
        let stop = p["stop"].as_f64().unwrap_or(0.0);
        let risk = (entry - stop).abs();
        let sign = if direction == Direction::Long {
            1.0
        } else {
            -1.0
        };
        let target = p["target"].as_f64().unwrap_or(entry);
        let target = if (target - entry) * sign <= risk {
            entry + sign * risk
        } else {
            target
        };
        Ok(vec![TradeIntent::Open(EntryPlan {
            signal_id: p["id"]
                .as_i64()
                .map(|id| id.to_string())
                .unwrap_or_else(|| p["signal_id"].as_str().unwrap_or("").into()),
            symbol: p["symbol"].as_str().unwrap_or("").into(),
            instrument_id: p["instrument_id"].as_str().unwrap_or("").into(),
            direction,
            reference_price: entry,
            trigger: EntryTrigger::Breakout { price: entry },
            stop_price: Some(stop),
            target_price: Some(target),
            risk_per_unit: risk,
            requested_volume: 1,
            created_at: p["created_at_epoch"]
                .as_i64()
                .unwrap_or_else(|| chrono::Utc::now().timestamp()),
            expires_at: None,
            reason: if (p["target"].as_f64().unwrap_or(entry) - entry) * sign <= risk {
                format!(
                    "目标空间<1R(S1:{:.1})，止盈预设1:1",
                    p["target"].as_f64().unwrap_or(entry)
                )
            } else {
                "等待实际合约触价".into()
            },
            evidence: p.clone(),
        })])
    }
}
struct NScoreSizing;
impl SizingPolicy for NScoreSizing {
    fn volume(&self, p: &EntryPlan, c: &ExecutionConfig, _: &Value) -> Result<i32> {
        let score = p.evidence["entry_score"].as_f64().unwrap_or(0.0);
        ensure!(score.is_finite() && score >= 2.5, "评分低于执行门槛");
        Ok(if score >= 3.5 {
            c.default_large_volume
        } else {
            c.default_small_volume
        }
        .max(1))
    }
}
struct FixedSizing;
impl SizingPolicy for FixedSizing {
    fn volume(&self, p: &EntryPlan, _: &ExecutionConfig, _: &Value) -> Result<i32> {
        ensure!(p.requested_volume > 0, "手数必须大于0");
        Ok(p.requested_volume)
    }
}
struct NExitPolicy;
impl ExitPolicy for NExitPolicy {
    fn on_quote(&self, p: &ManagedPosition, q: &ExecutionQuote, _: &Value) -> ExitUpdate {
        let u = super::policy::on_price(
            p.tier,
            p.fill_price,
            p.risk,
            if p.direction == "up" { 1.0 } else { -1.0 },
            q.last,
            p.target_price,
            p.current_stop_price,
            p.max_mfe_r,
            p.lot1_closed,
        );
        ExitUpdate {
            stop: u.stop,
            close: u
                .intent
                .map(|i| (i.reason.into(), if i.all { None } else { Some(1) })),
        }
    }
    fn on_bar(&self, p: &ManagedPosition, _: &Value) -> Option<String> {
        use crate::analyze::outcome::{NO_FOLLOW_BAR, NO_FOLLOW_MFE_R, TIME_HORIZON_BARS};
        if p.bars_held >= TIME_HORIZON_BARS {
            Some("time_exit".into())
        } else if p.bars_held == NO_FOLLOW_BAR && p.max_mfe_r < NO_FOLLOW_MFE_R {
            Some("no_follow".into())
        } else {
            None
        }
    }
}
struct ProtectOnly;
impl ExitPolicy for ProtectOnly {
    fn on_quote(&self, p: &ManagedPosition, q: &ExecutionQuote, _: &Value) -> ExitUpdate {
        let sign = if p.direction == "up" { 1.0 } else { -1.0 };
        ExitUpdate {
            stop: p.current_stop_price,
            close: (p.current_stop_price > 0.0 && (q.last - p.current_stop_price) * sign <= 0.0)
                .then(|| ("stop".into(), None)),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FrozenPlan {
    pub definition: StrategyDefinition,
    pub execution: ExecutionConfig,
    pub plan: EntryPlan,
}
