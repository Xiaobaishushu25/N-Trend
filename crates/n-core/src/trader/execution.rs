use anyhow::Result;
use std::collections::HashMap;
use std::collections::HashSet;
use std::path::{Path, PathBuf};
use tokio::sync::{Mutex, RwLock};

use super::client::CtpTraderClient;
use super::model::*;
use super::overnight::{append_snapshot_jsonl, check_pre_close_checkpoint, OvernightSnapshot};
use super::trailing::continuous_stop_price;
use crate::analyze::model::Dir;
use crate::analyze::outcome::{
    NO_FOLLOW_BAR, NO_FOLLOW_MFE_R, PENDING_BARS, TIME_HORIZON_BARS, TRAIL_START_R,
};

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PositionTier {
    /// 大仓位：2手，评分 >= 3.5；首手1R兑现，次手1.6R起按最高浮盈回撤0.8R追踪。
    Large,
    /// 小仓位：1手，评分 2.5 ~ 3.5，采用 1:1 盈亏比固定止盈 (达1.0R即止盈)
    Small,
}

impl PositionTier {
    pub fn default_volume(&self) -> i32 {
        match self {
            PositionTier::Large => 2,
            PositionTier::Small => 1,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ConditionState {
    Pending,
    WaitingConfirm,
    Triggered,
    Filled,
    Expired,
    Canceled,
    Invalidated,
    Working,
    PartFilled,
    Closing,
    Closed,
    Rejected,
    Unknown,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct ConditionOrder {
    pub id: String,
    pub event_id: Option<i64>,
    pub symbol: String,
    pub instrument_id: String,
    pub direction: String, // "up" / "down"
    pub tier: PositionTier,
    pub entry_price: f64,
    pub stop_price: f64,
    pub target_price: f64,
    pub risk: f64,
    pub score: f64,
    pub volume: i32,
    pub warning_bar_ts: String,
    #[serde(default)]
    pub trigger_ts: Option<String>,
    pub bars_waited: usize,
    pub max_wait_bars: usize,
    pub state: ConditionState,
    pub created_at: i64,
    pub note: String,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct ManagedPosition {
    pub id: String,
    pub condition_order_id: String,
    pub symbol: String,
    pub instrument_id: String,
    pub direction: String, // "up" / "down"
    pub tier: PositionTier,
    pub fill_price: f64,
    pub initial_stop_price: f64,
    pub target_price: f64,
    pub risk: f64,
    pub volume: i32,
    pub open_ts: String,
    pub bars_held: usize,
    pub max_mfe_r: f64,
    pub max_mae_r: f64,
    /// Legacy wire field: 0 = initial stop, 1 = breakeven, >= 2 = trailing active.
    pub trailing_grade: usize,
    pub current_stop_price: f64,
    pub lot1_closed: bool,
    pub exit_reason: Option<String>,
    pub exit_price: Option<f64>,
    pub is_closed: bool,
    pub realized_pnl_pts: Option<f64>,
    pub r_multiple: Option<f64>,
    #[serde(default)]
    pub event_id: Option<i64>,
    #[serde(default)]
    pub planned_entry_price: Option<f64>,
    #[serde(default)]
    pub score: Option<f64>,
    #[serde(default)]
    pub warning_bar_ts: Option<String>,
    #[serde(default)]
    pub trigger_ts: Option<String>,
    #[serde(default)]
    pub order_insert_ts: Option<String>,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct TradeRecord {
    pub id: String,
    pub condition_order_id: String,
    pub symbol: String,
    pub instrument_id: String,
    pub direction: String,
    pub tier: PositionTier,
    pub volume: i32,
    pub fill_price: f64,
    pub exit_price: f64,
    pub risk: f64,
    pub initial_stop_price: f64,
    pub target_price: f64,
    pub open_ts: String,
    pub exit_ts: String,
    pub exit_reason: String, // "stop", "trailing", "target_1r", "no_follow", "target_s1", "time_exit", "manual"
    pub realized_pnl_pts: f64,
    #[serde(default)]
    pub realized_pnl_amount: f64,
    pub r_multiple: f64,
    pub max_mfe_r: f64,
    pub max_mae_r: f64,
    pub bars_held: usize,
    #[serde(default)]
    pub event_id: Option<i64>,
    #[serde(default)]
    pub planned_entry_price: Option<f64>,
    #[serde(default)]
    pub score: Option<f64>,
    #[serde(default)]
    pub warning_bar_ts: Option<String>,
    #[serde(default)]
    pub trigger_ts: Option<String>,
    #[serde(default)]
    pub order_insert_ts: Option<String>,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize, Default)]
pub struct TradeStatistics {
    pub total_trades: usize,
    pub win_trades: usize,
    pub loss_trades: usize,
    pub win_rate_pct: f64,
    pub total_r: f64,
    pub avg_r: f64,
    pub profit_factor: f64,
    pub large_trades_count: usize,
    pub small_trades_count: usize,
    pub exit_reason_breakdown: HashMap<String, usize>,
    #[serde(default)]
    pub total_pnl: f64,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct ExecutionConfig {
    pub mode: ExecutionMode,
    pub max_risk_percent: f64,
    pub default_large_volume: i32,
    pub default_small_volume: i32,
    pub daily_loss_limit_r: f64,
    #[serde(default = "default_wait")]
    pub order_wait_seconds: u64,
    #[serde(default = "default_slippage")]
    pub slippage_ticks: u32,
    #[serde(default = "default_max_slippage")]
    pub max_slippage_ticks: u32,
    #[serde(default)]
    pub max_exit_slippage_ticks: u32,
    /// Reject new entries whose structural stop is too narrow for trading costs.
    #[serde(default = "default_min_entry_risk_ticks")]
    pub min_entry_risk_ticks: u32,
}

impl Default for ExecutionConfig {
    fn default() -> Self {
        Self {
            mode: ExecutionMode::SemiAuto,
            max_risk_percent: 1.0,
            default_large_volume: 2,
            default_small_volume: 1,
            daily_loss_limit_r: 3.0,
            order_wait_seconds: default_wait(),
            slippage_ticks: default_slippage(),
            max_slippage_ticks: default_max_slippage(),
            max_exit_slippage_ticks: 0,
            min_entry_risk_ticks: default_min_entry_risk_ticks(),
        }
    }
}

fn default_wait() -> u64 {
    3
}
fn default_min_entry_risk_ticks() -> u32 {
    // User-selected live lane; baseline remains a paired shadow observation.
    10
}
fn default_slippage() -> u32 {
    2
}
fn default_max_slippage() -> u32 {
    20
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize, PartialEq)]
pub enum OrderPhase {
    Unknown,
    Working,
    CancelPending,
    Terminal,
    Rejected,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct ExecutionOrder {
    pub request: OrderInsertRequest,
    pub condition_id: String,
    pub reason: Option<String>,
    pub phase: OrderPhase,
    pub reported_filled: i32,
    pub filled: i32,
    pub action_at: i64,
    pub trading_day: String,
    pub note: String,
    #[serde(default)]
    pub cancel_attempts: u32,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub funds_at_submit: Option<EntryFunds>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub entry_rules: Option<EntryRuleSnapshot>,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct EntryRuleSnapshot {
    pub version: String,
    pub config: ExecutionConfig,
    pub planned_risk_ticks: f64,
    pub comparison_run_id: Option<String>,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct EntryFunds {
    available: f64,
    margin: f64,
    updated_at: f64,
}

impl EntryFunds {
    fn fresh(&self, now: i64) -> bool {
        self.available.is_finite()
            && self.margin.is_finite()
            && self.margin >= 0.0
            && self.updated_at.is_finite()
            && (-1.0..=60.0).contains(&(now as f64 - self.updated_at))
    }
}

#[derive(Clone, serde::Serialize, serde::Deserialize)]
struct EntryFundingWait {
    order_ref: String,
    checkpoint: Option<EntryFunds>,
    required_available: Option<f64>,
}

fn insufficient_funds(note: &str) -> bool {
    note.contains("[31]") || note.starts_with("31:") || note.contains("资金不足")
}

fn missing_funds(note: &str) -> Option<f64> {
    // The final bracket contains the broker's approximate shortfall, not error 31.
    let amount = note.rsplit_once("资金[")?.1.split(']').next()?;
    amount
        .parse::<f64>()
        .ok()
        .filter(|n| n.is_finite() && *n > 0.0)
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
struct CloseTask {
    reason: String,
    target: i32,
    filled: i32,
    reference: f64,
    attempts: u32,
    all: bool,
}

#[derive(Clone, Default, serde::Serialize, serde::Deserialize)]
#[serde(default)]
struct ExecutionState {
    conditions: HashMap<String, ConditionOrder>,
    positions: HashMap<String, ManagedPosition>,
    history: Vec<TradeRecord>,
    orders: HashMap<String, ExecutionOrder>,
    closes: HashMap<String, CloseTask>,
    seen_trades: HashSet<String>,
    aborted_entries: HashSet<String>,
    authorized_entries: HashSet<String>,
    last_ref: u64,
    last_bars: HashMap<String, String>,
    account: Option<String>,
    trading_day: String,
    loss_r: f64,
    #[serde(default)]
    overnight_snapshots: HashMap<String, OvernightSnapshot>,
    #[serde(default)]
    recorded_checkpoints: HashSet<String>,
    entry_funding_waits: HashMap<String, EntryFundingWait>,
    comparison: super::comparison::ComparisonBook,
    #[serde(skip)]
    entry_funds: Option<EntryFunds>,
    #[serde(skip)]
    broker_ready: bool,
    #[serde(skip)]
    broker_connected: bool,
    #[serde(skip)]
    market_error: Option<String>,
    #[serde(skip)]
    error: Option<String>,
    #[serde(skip)]
    persistence_error: Option<String>,
    #[serde(skip)]
    last_action_at: i64,
    #[serde(skip)]
    quotes: HashMap<String, ExecutionQuote>,
    #[serde(skip)]
    entry_symbols_ready: HashSet<String>,
}

pub struct ExecutionEngine {
    client: CtpTraderClient,
    config: RwLock<ExecutionConfig>,
    config_path: Option<PathBuf>,
    state_path: Option<PathBuf>,
    state: Mutex<ExecutionState>,
    drive_lock: Mutex<()>,
    _state_lock: Option<std::fs::File>,
}

fn atomic_json(path: &Path, value: &impl serde::Serialize) -> Result<()> {
    use std::io::Write;
    let temp = path.with_extension("tmp");
    let mut file = std::fs::File::create(&temp)?;
    file.write_all(&serde_json::to_vec(value)?)?;
    file.sync_all()?;
    std::fs::rename(temp, path)?;
    Ok(())
}

impl ExecutionEngine {
    pub fn new(client: CtpTraderClient, config: ExecutionConfig) -> Self {
        Self::with_config_path(client, config, None)
    }

    pub fn with_config_path(
        client: CtpTraderClient,
        config: ExecutionConfig,
        config_path: Option<PathBuf>,
    ) -> Self {
        let state_path = config_path
            .as_ref()
            .map(|p| p.with_file_name("trader_execution.json"));
        let mut lock_error = None;
        let state_lock = state_path.as_ref().and_then(|p| {
            let lock_path = p.with_extension("lock");
            match std::fs::OpenOptions::new()
                .read(true)
                .write(true)
                .create(true)
                .truncate(false)
                .open(lock_path)
            {
                Ok(file) => match file.try_lock() {
                    Ok(()) => Some(file),
                    Err(e) => {
                        lock_error = Some(format!("执行状态已由其他进程占用，禁止交易: {e}"));
                        None
                    }
                },
                Err(e) => {
                    lock_error = Some(format!("无法锁定执行状态，禁止交易: {e}"));
                    None
                }
            }
        });
        let mut state = match state_path.as_ref().filter(|p| p.exists()) {
            Some(path) => match std::fs::read(path)
                .map_err(anyhow::Error::from)
                .and_then(|data| {
                    serde_json::from_slice::<ExecutionState>(&data).map_err(Into::into)
                }) {
                Ok(state) => state,
                Err(e) => ExecutionState {
                    persistence_error: Some(format!("执行状态加载失败，禁止报单: {e}")),
                    ..Default::default()
                },
            },
            None => ExecutionState::default(),
        };
        if lock_error.is_some() {
            state.persistence_error = lock_error;
        }
        Self {
            client,
            config: RwLock::new(config),
            config_path,
            state_path,
            state: Mutex::new(state),
            drive_lock: Mutex::new(()),
            _state_lock: state_lock,
        }
    }

    fn save(&self, state: &mut ExecutionState) -> Result<()> {
        if let Some(error) = &state.persistence_error {
            anyhow::bail!("{error}");
        }
        if let Some(path) = &self.state_path {
            if let Err(e) = state
                .comparison
                .archive_completed(&path.with_file_name("strategy_comparison.jsonl"))
            {
                let message = format!("策略对比归档失败，完整记录暂存执行状态文件: {e}");
                if state.comparison.archive_error.as_ref() != Some(&message) {
                    tracing::warn!("{message}");
                }
                state.comparison.archive_error = Some(message);
            } else {
                state.comparison.archive_error = None;
            }
            if let Err(e) = atomic_json(path, state) {
                state.persistence_error = Some(format!("执行状态保存失败，禁止报单: {e}"));
                anyhow::bail!("{}", state.persistence_error.as_ref().unwrap());
            }
        }
        Ok(())
    }

    pub async fn get_config(&self) -> ExecutionConfig {
        self.config.read().await.clone()
    }
    pub async fn update_config(&self, cfg: ExecutionConfig) -> Result<()> {
        if let Some(path) = &self.config_path {
            atomic_json(path, &cfg)?;
        }
        *self.config.write().await = cfg;
        Ok(())
    }
    pub async fn list_conditions(&self) -> Vec<ConditionOrder> {
        self.state
            .lock()
            .await
            .conditions
            .values()
            .cloned()
            .collect()
    }
    pub async fn list_positions(&self) -> Vec<ManagedPosition> {
        self.state
            .lock()
            .await
            .positions
            .values()
            .cloned()
            .collect()
    }
    pub async fn list_orders(&self) -> Vec<ExecutionOrder> {
        let mut orders = self
            .state
            .lock()
            .await
            .orders
            .values()
            .cloned()
            .collect::<Vec<_>>();
        orders.sort_by_key(|o| {
            o.request
                .order_ref
                .as_deref()
                .unwrap_or("")
                .parse::<u64>()
                .unwrap_or(0)
        });
        orders
    }
    pub async fn get_trade_history(&self) -> Vec<TradeRecord> {
        self.state.lock().await.history.clone()
    }

    pub async fn comparison_summary(&self) -> serde_json::Value {
        self.state.lock().await.comparison.summary()
    }

    pub async fn comparison_details(&self) -> serde_json::Value {
        serde_json::json!({"active_and_aggregate": &self.state.lock().await.comparison,
            "archive_file":self.state_path.as_ref().map(|p|p.with_file_name("strategy_comparison.jsonl")),
            "archive_dedup_key":"archive_id"})
    }

    pub async fn execution_error(&self) -> Option<String> {
        let s = self.state.lock().await;
        s.persistence_error
            .clone()
            .or_else(|| s.error.clone())
            .or_else(|| s.market_error.clone())
    }

    fn set_entry_note(s: &mut ExecutionState, id: &str, note: String) -> bool {
        if let Some(cond) = s.conditions.get_mut(id) {
            if cond.note != note {
                cond.note = note;
                return true;
            }
        }
        false
    }

    /// Persist the capital wait once per rejection, including legacy saved orders.
    /// Replayed counter snapshots must not move the recovery threshold each time.
    fn refresh_entry_funding_waits(s: &mut ExecutionState) -> bool {
        let mut latest: HashMap<String, &ExecutionOrder> = HashMap::new();
        for order in s.orders.values().filter(|o| o.reason.is_none()) {
            let old = latest.entry(order.condition_id.clone()).or_insert(order);
            if (order.action_at, &order.request.order_ref) > (old.action_at, &old.request.order_ref)
            {
                *old = order;
            }
        }
        let mut changed = false;
        for (id, order) in latest {
            if order.phase != OrderPhase::Rejected || !insufficient_funds(&order.note) {
                continue;
            }
            let order_ref = order.request.order_ref.clone().unwrap_or_default();
            if s.entry_funding_waits
                .get(&id)
                .is_some_and(|w| w.order_ref == order_ref && w.checkpoint.is_some())
            {
                continue;
            }
            let checkpoint = order
                .funds_at_submit
                .clone()
                .or_else(|| s.entry_funds.clone());
            let required_available = checkpoint
                .as_ref()
                .zip(missing_funds(&order.note))
                .map(|(funds, shortfall)| funds.available.max(0.0) + shortfall + 0.01);
            if checkpoint.is_none() && s.entry_funding_waits.contains_key(&id) {
                continue;
            }
            s.entry_funding_waits.insert(
                id,
                EntryFundingWait {
                    order_ref,
                    checkpoint,
                    required_available,
                },
            );
            changed = true;
        }
        changed
    }

    fn entry_funding_note(s: &ExecutionState, id: &str, now: i64) -> Option<String> {
        let Some(funds) = s.entry_funds.as_ref().filter(|f| f.fresh(now)) else {
            return Some("等待柜台最新资金同步，暂缓新开仓".into());
        };
        if funds.available <= 0.0 {
            return Some("可用资金不足，暂缓新开仓，等待资金恢复".into());
        }
        let Some(wait) = s.entry_funding_waits.get(id) else {
            return None;
        };
        let fresh = wait
            .checkpoint
            .as_ref()
            .is_some_and(|old| funds.updated_at > old.updated_at);
        let recovered = if let Some(required) = wait.required_available {
            funds.available >= required
        } else {
            // Without a numeric broker shortfall, a quote/P&L fluctuation is not
            // evidence that capital was released. Wait for a new margin release.
            wait.checkpoint.as_ref().is_some_and(|old| {
                funds.margin + 0.01 < old.margin && funds.available > old.available + 0.01
            })
        };
        if fresh && recovered {
            None
        } else {
            Some(match wait.required_available {
                Some(required) => format!(
                    "资金不足，暂缓新开仓；可用{:.2}，等待至少{:.2}及新的资金确认",
                    funds.available, required
                ),
                None => "资金不足，暂缓新开仓；等待柜台确认保证金释放".into(),
            })
        }
    }

    pub fn overnight_tracking_path(&self) -> Option<PathBuf> {
        self.config_path
            .as_ref()
            .map(|p| p.with_file_name("overnight_tracking.jsonl"))
    }

    pub fn overnight_attribution_path(&self) -> Option<PathBuf> {
        self.config_path
            .as_ref()
            .map(|p| p.with_file_name("overnight_attribution.jsonl"))
    }

    pub async fn get_overnight_snapshots(&self) -> Vec<OvernightSnapshot> {
        self.state
            .lock()
            .await
            .overnight_snapshots
            .values()
            .cloned()
            .collect()
    }

    fn check_overnight_checkpoints(&self, s: &mut ExecutionState) {
        let now = chrono::Local::now();
        let tracking_path = self.overnight_tracking_path();
        let open_positions: Vec<ManagedPosition> = s
            .positions
            .values()
            .filter(|p| !p.is_closed)
            .cloned()
            .collect();

        for pos in open_positions {
            if let Some(checkpoint_type) = check_pre_close_checkpoint(&pos.symbol, &now, 2) {
                let checkpoint_key = format!("{}:{}:{:?}", pos.id, s.trading_day, checkpoint_type);
                if !s.recorded_checkpoints.contains(&checkpoint_key) {
                    if let Some(quote) = s
                        .quotes
                        .get(&pos.instrument_id.to_uppercase())
                        .filter(|q| q.valid())
                    {
                        let snap =
                            OvernightSnapshot::create(&pos, quote, checkpoint_type, &s.trading_day);
                        tracing::info!(
                            "🌙 [影子跟踪] 记录收盘前2分钟快照: 合约={} 方向={} 最新={:.2} 假设平仓价={:.2} 盈亏={:+.2}点({:+.2}R)",
                            snap.instrument_id,
                            snap.direction,
                            snap.checkpoint_last,
                            snap.hypo_exit_price,
                            snap.hypo_pnl_pts,
                            snap.hypo_pnl_r,
                        );
                        if let Some(ref path) = tracking_path {
                            if let Err(e) = append_snapshot_jsonl(path, &snap) {
                                tracing::warn!("写入隔夜快照失败: {e}");
                            }
                        }
                        s.recorded_checkpoints.insert(checkpoint_key);
                        s.overnight_snapshots.insert(pos.id.clone(), snap);
                    }
                }
            }
        }
    }

    fn update_overnight_next_open(&self, s: &mut ExecutionState) {
        let now = chrono::Local::now();
        let now_ts = now.timestamp();
        let tracking_path = self.overnight_tracking_path();

        for (pos_id, snap) in s.overnight_snapshots.iter_mut() {
            if snap.next_open_price.is_none() && now_ts - snap.created_at >= 180 {
                if let Some(_pos) = s.positions.get(pos_id).filter(|p| !p.is_closed) {
                    if let Some(quote) = s
                        .quotes
                        .get(&snap.instrument_id.to_uppercase())
                        .filter(|q| q.valid())
                    {
                        let open_ts = now.format("%Y-%m-%d %H:%M:%S").to_string();
                        snap.update_next_open(quote.last, &open_ts);
                        tracing::info!(
                            "🌅 [影子跟踪] 记录次时段开盘跳空: 合约={} 开盘价={:.2} 跳空点数={:+.2} 跳空R={:+.2}R 击穿止损={:?}",
                            snap.instrument_id,
                            snap.next_open_price.unwrap_or_default(),
                            snap.gap_pts.unwrap_or_default(),
                            snap.gap_r.unwrap_or_default(),
                            snap.gap_penetrated_stop.unwrap_or_default(),
                        );
                        if let Some(ref path) = tracking_path {
                            let _ = append_snapshot_jsonl(path, snap);
                        }
                    }
                }
            }
        }
    }

    pub async fn get_trade_statistics(&self) -> TradeStatistics {
        let s = self.state.lock().await;
        let mut stats = TradeStatistics::default();
        let (mut wins, mut losses) = (0.0, 0.0);
        for t in &s.history {
            stats.total_trades += 1;
            stats.total_r += t.r_multiple;
            stats.total_pnl += t.realized_pnl_amount;
            if t.r_multiple > 0.0 {
                stats.win_trades += 1;
                wins += t.r_multiple;
            }
            if t.r_multiple < 0.0 {
                stats.loss_trades += 1;
                losses -= t.r_multiple;
            }
            if t.tier == PositionTier::Large {
                stats.large_trades_count += 1;
            } else {
                stats.small_trades_count += 1;
            }
            *stats
                .exit_reason_breakdown
                .entry(t.exit_reason.clone())
                .or_default() += 1;
        }
        if stats.total_trades > 0 {
            stats.win_rate_pct = stats.win_trades as f64 / stats.total_trades as f64 * 100.0;
            stats.avg_r = stats.total_r / stats.total_trades as f64;
        }
        stats.profit_factor = if losses > 0.0 { wins / losses } else { wins };
        stats
    }

    pub async fn register_condition(
        &self,
        event_id: Option<i64>,
        symbol: &str,
        instrument_id: &str,
        dir: Dir,
        entry: f64,
        stop: f64,
        target: f64,
        score: f64,
        warning_ts: &str,
    ) -> Result<String> {
        self.register_condition_with_evidence(
            event_id,
            symbol,
            instrument_id,
            dir,
            entry,
            stop,
            target,
            score,
            warning_ts,
            None,
        )
        .await
    }

    pub async fn register_condition_with_evidence(
        &self,
        event_id: Option<i64>,
        symbol: &str,
        instrument_id: &str,
        dir: Dir,
        entry: f64,
        stop: f64,
        target: f64,
        score: f64,
        warning_ts: &str,
        evidence: Option<serde_json::Value>,
    ) -> Result<String> {
        let long = dir == Dir::Up;
        anyhow::ensure!(
            entry.is_finite()
                && stop.is_finite()
                && target.is_finite()
                && entry > 0.0
                && stop > 0.0
                && (if long { stop < entry } else { stop > entry }),
            "入场/止损价格不合法"
        );
        let risk = (entry - stop).abs();
        anyhow::ensure!(risk > 0.0, "止损空间必须大于0");
        anyhow::ensure!(score.is_finite() && score >= 2.5, "评分低于执行门槛");
        anyhow::ensure!(
            is_actual_futures_contract(instrument_id),
            "必须使用实际交易合约"
        );
        let cfg = self.get_config().await;
        let mut s = self.state.lock().await;
        let direction = if long { "up" } else { "down" };
        // This key survives process restarts and repeated scans of the same signal.
        if let Some(ord) = s.conditions.values().find(|o| {
            o.symbol == symbol
                && o.direction == direction
                && (if event_id.is_some() {
                    o.event_id == event_id
                } else {
                    o.warning_bar_ts == warning_ts
                })
        }) {
            return Ok(ord.id.clone());
        }
        let tier = if score >= 3.5 {
            PositionTier::Large
        } else {
            PositionTier::Small
        };
        let target_space = if long { target - entry } else { entry - target };
        // 目标空间过小判定：若目标空间小于等于止损空间（即 RR <= 1.0，含 S1 倒挂或触碰 entry 的情形）
        // 此时不以原形态 S1 目标点位作为止盈，而是将止盈预设在 1:1 盈亏比（1.0R）上
        let (effective_target, is_preset_1r) = if target_space <= risk {
            let one_r_target = if long { entry + risk } else { entry - risk };
            (one_r_target, true)
        } else {
            (target, false)
        };
        let id = uuid::Uuid::new_v4().to_string();
        s.conditions.insert(
            id.clone(),
            ConditionOrder {
                id: id.clone(),
                event_id,
                symbol: symbol.into(),
                instrument_id: instrument_id.into(),
                direction: direction.into(),
                tier,
                entry_price: entry,
                stop_price: stop,
                target_price: effective_target,
                risk,
                score,
                volume: if tier == PositionTier::Large {
                    cfg.default_large_volume.max(1)
                } else {
                    cfg.default_small_volume.max(1)
                },
                warning_bar_ts: warning_ts.into(),
                trigger_ts: None,
                bars_waited: 0,
                max_wait_bars: PENDING_BARS,
                state: ConditionState::Pending,
                created_at: chrono::Utc::now().timestamp(),
                note: if is_preset_1r {
                    format!("目标空间<1R(S1:{:.1})，止盈预设1:1", target)
                } else {
                    "等待实际合约触价".into()
                },
            },
        );
        let condition = s.conditions[&id].clone();
        s.comparison
            .register(&condition, &cfg, condition.created_at);
        if let Some(evidence) = evidence {
            s.comparison.attach_evidence(&id, evidence);
        }
        self.save(&mut s)?;
        Ok(id)
    }

    pub async fn cancel_condition(&self, id: &str) -> Result<bool> {
        let mut s = self.state.lock().await;
        if !s.conditions.get(id).is_some_and(|o| {
            !matches!(
                o.state,
                ConditionState::Filled | ConditionState::Closing | ConditionState::Closed
            )
        }) {
            return Ok(false);
        }
        s.aborted_entries.insert(id.into());
        if let Some(o) = s.conditions.get_mut(id) {
            o.state = ConditionState::Canceled;
            o.note = "已取消后续开仓，正在处理剩余委托".into();
        }
        self.save(&mut s)?;
        Ok(true)
    }

    pub async fn confirm_open(&self, id: &str) -> Result<bool> {
        let mut s = self.state.lock().await;
        if !s
            .conditions
            .get(id)
            .is_some_and(|o| o.state == ConditionState::WaitingConfirm)
            || s.aborted_entries.contains(id)
        {
            return Ok(false);
        }
        s.authorized_entries.insert(id.into());
        let o = s.conditions.get_mut(id).unwrap();
        o.state = ConditionState::Triggered;
        if o.trigger_ts.is_none() {
            o.trigger_ts = Some(chrono::Local::now().format("%Y-%m-%d %H:%M:%S").to_string());
        }
        o.note = "已确认，等待有效盘口提交委托；尚未成交".into();
        self.save(&mut s)?;
        Ok(true)
    }

    /// Complete counter snapshots are replayable. Only trade records change holdings.
    pub async fn reconcile(&self, snapshot: &serde_json::Value) -> Result<()> {
        let mut guard = self.state.lock().await;
        let mut next = guard.clone();
        match self.reconcile_snapshot(&mut next, snapshot) {
            Ok(()) => {
                *guard = next;
                drop(guard);
                self.on_quotes(HashMap::new()).await
            }
            Err(e) => {
                guard.broker_ready = false;
                guard.error = Some(format!("柜台成交恢复失败，暂停新报单: {e}"));
                Err(e)
            }
        }
    }

    fn reconcile_snapshot(
        &self,
        s: &mut ExecutionState,
        snapshot: &serde_json::Value,
    ) -> Result<()> {
        let h = &snapshot["health"];
        s.broker_connected = h["logged_in"].as_bool().unwrap_or(false)
            && h["settlement_confirmed"]
                .as_bool()
                .unwrap_or_else(|| h["ready"].as_bool().unwrap_or(false))
            && snapshot["bridge_online"].as_bool().unwrap_or(true);
        s.broker_ready = h["ready"].as_bool().unwrap_or(false)
            && snapshot["bridge_online"].as_bool().unwrap_or(true);
        if h["user_id"].as_str().unwrap_or("").is_empty() {
            return Ok(());
        }
        let account = format!(
            "{}:{}",
            h["broker_id"].as_str().unwrap_or(""),
            h["user_id"].as_str().unwrap_or("")
        );
        if s.account.as_ref().is_some_and(|old| old != &account) {
            s.broker_ready = false;
            s.broker_connected = false;
            s.error = Some("柜台账户与持久化执行状态不一致，禁止交易".into());
            return Ok(());
        }
        s.account = Some(account);
        if let (Some(available), Some(margin), Some(updated_at)) = (
            snapshot["account"]["available"].as_f64(),
            snapshot["account"]["curr_margin"]
                .as_f64()
                .or_else(|| snapshot["account"]["margin"].as_f64()),
            snapshot["account"]["updated_at"].as_f64(),
        ) {
            if available.is_finite()
                && margin.is_finite()
                && updated_at.is_finite()
                && s.entry_funds
                    .as_ref()
                    .is_none_or(|old| updated_at >= old.updated_at)
            {
                s.entry_funds = Some(EntryFunds {
                    available,
                    margin,
                    updated_at,
                });
            }
        }
        let day = h["trading_day"].as_str().unwrap_or("").to_string();
        if !day.is_empty() && h["logged_in"].as_bool().unwrap_or(false) && s.trading_day != day {
            s.trading_day = day.clone();
            s.loss_r = 0.0;
        }
        for raw in snapshot["orders"].as_array().into_iter().flatten() {
            let r = raw["order_ref"].as_str().unwrap_or("");
            if let Some(order) = s.orders.get_mut(r) {
                if !raw["instrument_id"]
                    .as_str()
                    .unwrap_or("")
                    .eq_ignore_ascii_case(&order.request.instrument_id)
                {
                    continue;
                }
                if let Some(qty) = raw["volume_total_original"].as_i64().filter(|qty| *qty > 0) {
                    order.request.volume = qty as i32;
                }
                order.reported_filled = order
                    .reported_filled
                    .max(raw["volume_traded"].as_i64().unwrap_or(0) as i32);
                let status = raw["status"].as_str().unwrap_or("");
                let phase = match status {
                    "all_traded" | "canceled" | "part_traded_canceled" => OrderPhase::Terminal,
                    "rejected" => OrderPhase::Rejected,
                    _ => OrderPhase::Working,
                };
                if order.phase != OrderPhase::Terminal
                    && (order.phase != OrderPhase::CancelPending
                        || phase == OrderPhase::Terminal
                        || phase == OrderPhase::Rejected)
                {
                    order.phase = phase;
                }
                order.note = raw["status_msg"].as_str().unwrap_or("").into();
            }
        }
        let mut trades = snapshot["trades"]
            .as_array()
            .into_iter()
            .flatten()
            .map(|raw| serde_json::from_value::<TraderTradeRecord>(raw.clone()))
            .collect::<std::result::Result<Vec<_>, _>>()?;
        trades.sort_by_key(|t| {
            (
                t.trade_date.clone(),
                t.trade_time.clone(),
                t.offset != "open",
                t.order_ref.parse::<u64>().unwrap_or(0),
                t.trade_id.clone(),
            )
        });
        for trade in trades {
            let fill_day = if trade.trading_day.is_empty() {
                &trade.trade_date
            } else {
                &trade.trading_day
            };
            let key = format!(
                "{}:{}:{}:{}:{}",
                fill_day, trade.exchange_id, trade.instrument_id, trade.direction, trade.trade_id
            );
            let Some(order) = s.orders.get(&trade.order_ref).cloned() else {
                continue;
            };
            if !trade
                .instrument_id
                .eq_ignore_ascii_case(&order.request.instrument_id)
                || s.seen_trades.contains(&key)
            {
                continue;
            }
            anyhow::ensure!(
                trade.volume > 0 && trade.price.is_finite() && trade.price > 0.0,
                "柜台成交记录不合法"
            );
            let expected_dir = &order.request.direction;
            anyhow::ensure!(
                &trade.direction == expected_dir
                    && ((trade.offset == "open") == order.reason.is_none()),
                "成交回报与委托方向不一致"
            );
            anyhow::ensure!(
                order.filled + trade.volume <= order.request.volume,
                "成交数量超过该委托计划数量"
            );
            if order.reason.is_some() {
                anyhow::ensure!(
                    s.positions
                        .get(&order.condition_id)
                        .is_some_and(|p| p.volume >= trade.volume),
                    "平仓成交缺少对应开仓成交，等待完整快照"
                );
            }
            s.seen_trades.insert(key.clone());
            let ord = s.orders.get_mut(&trade.order_ref).unwrap();
            ord.filled += trade.volume;
            if ord.filled >= ord.request.volume {
                ord.phase = OrderPhase::Terminal;
            }
            let cond = s.conditions[&order.condition_id].clone();
            if order.reason.is_none() {
                let order_insert_ts = s.orders.get(&trade.order_ref).map(|o| {
                    chrono::DateTime::from_timestamp(o.action_at, 0)
                        .map(|dt| {
                            dt.with_timezone(&chrono::Local)
                                .format("%Y-%m-%d %H:%M:%S")
                                .to_string()
                        })
                        .unwrap_or_else(|| {
                            chrono::Local::now().format("%Y-%m-%d %H:%M:%S").to_string()
                        })
                });
                let pos = s
                    .positions
                    .entry(cond.id.clone())
                    .or_insert_with(|| ManagedPosition {
                        id: cond.id.clone(),
                        condition_order_id: cond.id.clone(),
                        symbol: cond.symbol.clone(),
                        instrument_id: cond.instrument_id.clone(),
                        direction: cond.direction.clone(),
                        tier: cond.tier,
                        fill_price: trade.price,
                        initial_stop_price: cond.stop_price,
                        target_price: cond.target_price,
                        risk: cond.risk,
                        volume: 0,
                        open_ts: format!("{} {}", trade.trade_date, trade.trade_time),
                        bars_held: 0,
                        max_mfe_r: 0.0,
                        max_mae_r: 0.0,
                        trailing_grade: 0,
                        current_stop_price: cond.stop_price,
                        lot1_closed: false,
                        exit_reason: None,
                        exit_price: None,
                        is_closed: false,
                        realized_pnl_pts: None,
                        r_multiple: None,
                        event_id: cond.event_id,
                        planned_entry_price: Some(cond.entry_price),
                        score: Some(cond.score),
                        warning_bar_ts: Some(cond.warning_bar_ts.clone()),
                        trigger_ts: cond.trigger_ts.clone(),
                        order_insert_ts: order_insert_ts.clone(),
                    });
                if pos.order_insert_ts.is_none() {
                    pos.order_insert_ts = order_insert_ts;
                }
                if pos.trigger_ts.is_none() {
                    pos.trigger_ts = cond.trigger_ts.clone();
                }
                let qty = pos.volume + trade.volume;
                pos.fill_price = (pos.fill_price * pos.volume as f64
                    + trade.price * trade.volume as f64)
                    / qty as f64;
                pos.volume = qty;
                pos.risk = (pos.fill_price - cond.stop_price).abs().max(1e-9);
                pos.is_closed = false;
                let sign = if pos.direction == "up" { 1.0 } else { -1.0 };
                let target_space = (cond.target_price - cond.entry_price) * sign;
                if target_space <= cond.risk + 1e-6 {
                    pos.target_price = pos.fill_price + sign * pos.risk;
                }
                // Stop exits include fills racing an entry cancellation. A partial
                // profit-taking task still closes only its requested first lot.
                if let Some(task) = s.closes.get_mut(&cond.id) {
                    if task.all {
                        task.target += trade.volume;
                    }
                } else if (cond.direction == "up" && trade.price <= cond.stop_price)
                    || (cond.direction == "down" && trade.price >= cond.stop_price)
                {
                    Self::latch_exit(s, &cond.id, "stop", cond.stop_price, true);
                }
            } else if let Some(pos) = s.positions.get_mut(&cond.id) {
                anyhow::ensure!(
                    trade.volume <= pos.volume,
                    "平仓成交超过托管数量，需要核对柜台持仓"
                );
                let points =
                    (trade.price - pos.fill_price) * if pos.direction == "up" { 1.0 } else { -1.0 };
                pos.volume -= trade.volume;
                pos.is_closed = pos.volume == 0;
                pos.exit_price = Some(trade.price);
                pos.exit_reason = order.reason.clone();
                let r = points / pos.risk.max(1e-9);
                let mult = crate::precision::effective_multiplier(&pos.instrument_id, "");
                let pnl_amount = points * mult * trade.volume as f64;
                let rec = TradeRecord {
                    id: key,
                    condition_order_id: cond.id.clone(),
                    symbol: pos.symbol.clone(),
                    instrument_id: pos.instrument_id.clone(),
                    direction: pos.direction.clone(),
                    tier: pos.tier,
                    volume: trade.volume,
                    fill_price: pos.fill_price,
                    exit_price: trade.price,
                    risk: pos.risk,
                    initial_stop_price: pos.initial_stop_price,
                    target_price: pos.target_price,
                    open_ts: pos.open_ts.clone(),
                    exit_ts: format!("{} {}", trade.trade_date, trade.trade_time),
                    exit_reason: order.reason.clone().unwrap(),
                    realized_pnl_pts: points,
                    realized_pnl_amount: pnl_amount,
                    r_multiple: r,
                    max_mfe_r: pos.max_mfe_r,
                    max_mae_r: pos.max_mae_r,
                    bars_held: pos.bars_held,
                    event_id: cond.event_id.or(pos.event_id),
                    planned_entry_price: Some(cond.entry_price).or(pos.planned_entry_price),
                    score: Some(cond.score).or(pos.score),
                    warning_bar_ts: Some(cond.warning_bar_ts.clone())
                        .or_else(|| pos.warning_bar_ts.clone()),
                    trigger_ts: cond.trigger_ts.clone().or_else(|| pos.trigger_ts.clone()),
                    order_insert_ts: pos.order_insert_ts.clone(),
                };
                if r < 0.0 && *fill_day == s.trading_day {
                    s.loss_r -= r * trade.volume as f64;
                }
                if let Some(task) = s.closes.get_mut(&cond.id) {
                    task.filled += trade.volume;
                }
                if let Some(snap) = s.overnight_snapshots.get_mut(&cond.id) {
                    snap.finalize_actual_exit(
                        rec.exit_price,
                        &rec.exit_ts,
                        &rec.exit_reason,
                        rec.realized_pnl_pts,
                        rec.r_multiple,
                    );
                    tracing::info!(
                        "📊 [影子跟踪] 隔夜持仓归因完成: 合约={} 真实出场={:.2}({:+.2}R) 假设平仓={:.2}({:+.2}R) => 隔夜超额收益={:+.2}点 ({:+.2}R)",
                        snap.instrument_id,
                        snap.actual_exit_price.unwrap_or_default(),
                        snap.actual_realized_pnl_r.unwrap_or_default(),
                        snap.hypo_exit_price,
                        snap.hypo_pnl_r,
                        snap.overnight_delta_pts.unwrap_or_default(),
                        snap.overnight_delta_r.unwrap_or_default(),
                    );
                    if let Some(path) = self.overnight_attribution_path() {
                        if let Err(e) = append_snapshot_jsonl(&path, snap) {
                            tracing::warn!("写入隔夜归因记录失败: {e}");
                        }
                    }
                }
                s.history.push(rec);
            }
        }
        // A terminal order may precede its trade callbacks: wait for every reported fill.
        for id in s.conditions.keys().cloned().collect::<Vec<_>>() {
            let open_filled: i32 = s
                .orders
                .values()
                .filter(|o| o.condition_id == id && o.reason.is_none())
                .map(|o| o.filled)
                .sum();
            let closing = s.closes.contains_key(&id);
            let pos = s.positions.get(&id);
            let orders_settled = s.orders.values().filter(|o| o.condition_id == id).all(|o| {
                matches!(o.phase, OrderPhase::Terminal | OrderPhase::Rejected)
                    && o.filled >= o.reported_filled
            });
            let phase = if closing && pos.is_some_and(|p| p.is_closed) && orders_settled {
                Some(ConditionState::Closed)
            } else if closing && pos.is_none() && orders_settled {
                Some(ConditionState::Canceled)
            } else if closing {
                Some(ConditionState::Closing)
            } else if open_filled >= s.conditions[&id].volume {
                Some(ConditionState::Filled)
            } else if open_filled > 0 {
                Some(ConditionState::PartFilled)
            } else if s
                .orders
                .values()
                .any(|o| o.condition_id == id && o.phase == OrderPhase::Working)
            {
                Some(ConditionState::Working)
            } else {
                None
            };
            if let Some(phase) = phase {
                s.conditions.get_mut(&id).unwrap().state = phase;
            }
        }
        // Never rewrite strategy quantities from an ambiguous aggregate snapshot.
        // A lower authoritative quantity freezes submissions until reconciliation.
        s.error = None;
        let positions_at = h["positions_updated_at"].as_f64().unwrap_or(0.0) as i64;
        let newest_fill = snapshot["last_trade_at"].as_f64().unwrap_or(0.0) as i64;
        if positions_at > newest_fill && !h["sync_pending"].as_bool().unwrap_or(true) {
            let mut expected: HashMap<(String, String), i32> = HashMap::new();
            for p in s.positions.values().filter(|p| !p.is_closed) {
                *expected
                    .entry((
                        p.instrument_id.to_lowercase(),
                        if p.direction == "up" { "buy" } else { "sell" }.into(),
                    ))
                    .or_default() += p.volume;
            }
            for ((inst, dir), qty) in expected {
                let actual: i32 = snapshot["broker_positions"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .filter(|p| {
                        p["instrument_id"]
                            .as_str()
                            .unwrap_or("")
                            .eq_ignore_ascii_case(&inst)
                            && p["direction"] == dir
                    })
                    .map(|p| p["position"].as_i64().unwrap_or(0) as i32)
                    .sum();
                if actual < qty {
                    s.error = Some(format!(
                        "{inst} 柜台持仓 {actual} 小于托管持仓 {qty}，暂停报单等待核对"
                    ));
                }
            }
        }
        self.save(s)
    }

    pub async fn requested_contracts(&self) -> Vec<String> {
        let s = self.state.lock().await;
        s.conditions
            .values()
            .filter(|o| {
                !matches!(
                    o.state,
                    ConditionState::Expired
                        | ConditionState::Closed
                        | ConditionState::Canceled
                        | ConditionState::Invalidated
                )
            })
            .map(|o| o.instrument_id.clone())
            .chain(
                s.positions
                    .values()
                    .filter(|p| !p.is_closed)
                    .map(|p| p.instrument_id.clone()),
            )
            .chain(s.comparison.contracts())
            .collect::<std::collections::BTreeSet<_>>()
            .into_iter()
            .collect()
    }

    pub async fn last_bar(&self, symbol: &str) -> Option<String> {
        self.state.lock().await.last_bars.get(symbol).cloned()
    }

    /// Reconcile pending-signal age before permitting entries after a restart.
    /// Existing exits remain available while bar history is unavailable.
    pub async fn reconcile_entry_age(&self, symbol: &str, bars: &[String]) -> Result<()> {
        if bars.is_empty() {
            return Ok(());
        }
        let mut s = self.state.lock().await;
        let mut expired = Vec::new();
        for o in s.conditions.values_mut().filter(|o| o.symbol == symbol) {
            if matches!(
                o.state,
                ConditionState::Pending
                    | ConditionState::WaitingConfirm
                    | ConditionState::Triggered
                    | ConditionState::Rejected
            ) {
                o.bars_waited = o.bars_waited.max(
                    bars.iter()
                        .filter(|ts| ts.as_str() > o.warning_bar_ts.as_str())
                        .count(),
                );
                if o.bars_waited > o.max_wait_bars {
                    o.state = ConditionState::Expired;
                    expired.push(o.id.clone());
                }
            }
        }
        s.aborted_entries.extend(expired);
        self.save(&mut s)?;
        s.entry_symbols_ready.insert(symbol.into());
        Ok(())
    }

    pub async fn active_symbols(&self) -> Vec<String> {
        let s = self.state.lock().await;
        s.conditions
            .values()
            .filter(|o| {
                !matches!(
                    o.state,
                    ConditionState::Expired
                        | ConditionState::Closed
                        | ConditionState::Canceled
                        | ConditionState::Invalidated
                )
            })
            .map(|o| o.symbol.clone())
            .chain(
                s.positions
                    .values()
                    .filter(|p| !p.is_closed)
                    .map(|p| p.symbol.clone()),
            )
            .collect::<std::collections::BTreeSet<_>>()
            .into_iter()
            .collect()
    }

    pub async fn registered_signals(&self) -> HashSet<i64> {
        self.state
            .lock()
            .await
            .conditions
            .values()
            .filter_map(|o| o.event_id)
            .collect()
    }

    pub async fn market_unavailable(&self) {
        let mut s = self.state.lock().await;
        if s.market_error.is_none() {
            s.comparison.mark_quote_gap();
        }
        s.quotes.clear();
        s.market_error = Some("执行行情连接中断，暂停新报单并保留未完成任务".into());
    }

    pub async fn quote_gap(&self) -> Result<()> {
        let mut s = self.state.lock().await;
        s.comparison.mark_quote_gap();
        let ids = s
            .orders
            .values()
            .map(|o| o.condition_id.clone())
            .collect::<HashSet<_>>();
        for id in ids {
            let reference = s.conditions[&id].stop_price;
            Self::latch_exit(&mut s, &id, "market_gap", reference, true);
        }
        s.error = Some("执行行情发生跳号，已终止补仓并启动风险退出".into());
        self.save(&mut s)
    }

    pub async fn on_quote_batch(&self, events: Vec<(String, ExecutionQuote)>) -> Result<()> {
        // Observe every tick, then submit using the newest book, not a queued old tick.
        let guard = self.drive_lock.lock().await;
        for (code, quote) in events {
            self.on_quotes([(code, quote)].into_iter().collect())
                .await?;
        }
        drop(guard);
        self.drive().await
    }

    pub async fn on_quotes(&self, quotes: HashMap<String, ExecutionQuote>) -> Result<()> {
        let mode = self.get_config().await.mode;
        let mut s = self.state.lock().await;
        if quotes.values().any(|q| q.valid()) {
            s.market_error = None;
        }
        s.quotes.extend(quotes);
        let state = &mut *s;
        state
            .comparison
            .advance(&state.quotes, chrono::Utc::now().timestamp());
        let entries = s.conditions.values().cloned().collect::<Vec<_>>();
        for cond in entries {
            let Some(q) = s
                .quotes
                .get(&cond.instrument_id.to_uppercase())
                .cloned()
                .filter(|q| q.valid() && q.instrument_id.eq_ignore_ascii_case(&cond.instrument_id))
            else {
                continue;
            };
            if cond.state == ConditionState::Pending
                && !s.aborted_entries.contains(&cond.id)
                && s.entry_symbols_ready.contains(&cond.symbol)
            {
                let hit = if cond.direction == "up" {
                    q.last >= cond.entry_price
                } else {
                    q.last <= cond.entry_price
                };
                if hit {
                    let o = s.conditions.get_mut(&cond.id).unwrap();
                    if o.trigger_ts.is_none() {
                        o.trigger_ts =
                            Some(chrono::Local::now().format("%Y-%m-%d %H:%M:%S").to_string());
                    }
                    o.state = if mode == ExecutionMode::SemiAuto {
                        ConditionState::WaitingConfirm
                    } else {
                        ConditionState::Triggered
                    };
                    o.note = if mode == ExecutionMode::MonitorOnly {
                        "触价，仅监控，不提交委托"
                    } else {
                        "触价，等待提交委托"
                    }
                    .into();
                    if mode == ExecutionMode::FullAuto {
                        s.authorized_entries.insert(cond.id.clone());
                    }
                }
            }
            // A stop also terminates an unfilled or partly filled entry.
            let entry_started = s
                .orders
                .values()
                .any(|o| o.condition_id == cond.id && o.reason.is_none());
            let stop_hit = if cond.direction == "up" {
                q.last <= cond.stop_price
            } else {
                q.last >= cond.stop_price
            };
            let exposed = s.positions.get(&cond.id).is_some_and(|p| p.volume > 0)
                || s.orders.values().any(|o| {
                    o.condition_id == cond.id
                        && o.reason.is_none()
                        && (!matches!(o.phase, OrderPhase::Terminal | OrderPhase::Rejected)
                            || o.filled < o.reported_filled)
                });
            if !exposed
                && s.authorized_entries.contains(&cond.id)
                && !s.aborted_entries.contains(&cond.id)
                && stop_hit
            {
                s.aborted_entries.insert(cond.id.clone());
                let o = s.conditions.get_mut(&cond.id).unwrap();
                o.state = ConditionState::Canceled;
                o.note = "报单前行情已越过止损，终止本次入场".into();
            }
            if entry_started && exposed && stop_hit {
                Self::latch_exit(&mut s, &cond.id, "stop", cond.stop_price, true);
            }
            let Some(mut pos) = s.positions.get(&cond.id).cloned().filter(|p| !p.is_closed) else {
                continue;
            };
            let sign = if pos.direction == "up" { 1.0 } else { -1.0 };
            let r = (q.last - pos.fill_price) * sign / pos.risk.max(1e-9);
            pos.max_mfe_r = pos.max_mfe_r.max(r);
            pos.max_mae_r = pos.max_mae_r.min(r);
            if !s.closes.contains_key(&cond.id) {
                if pos.tier == PositionTier::Small && r >= 1.0 {
                    Self::latch_exit(&mut s, &cond.id, "target_1r", q.last, true);
                } else if pos.tier == PositionTier::Large && !pos.lot1_closed && r >= TRAIL_START_R
                {
                    Self::latch_exit(&mut s, &cond.id, "target_1r", q.last, false);
                } else if pos.lot1_closed {
                    pos.current_stop_price = continuous_stop_price(
                        pos.fill_price,
                        pos.risk,
                        sign,
                        pos.max_mfe_r,
                        pos.current_stop_price,
                    );
                    if (pos.current_stop_price - pos.fill_price) * sign > 0.0 {
                        pos.trailing_grade = pos.trailing_grade.max(2);
                    }
                    if (q.last - pos.current_stop_price) * sign <= 0.0 {
                        Self::latch_exit(
                            &mut s,
                            &cond.id,
                            if pos.trailing_grade > 1 {
                                "trailing"
                            } else {
                                "breakeven"
                            },
                            pos.current_stop_price,
                            true,
                        );
                    } else if (q.last - pos.target_price) * sign >= 0.0 {
                        let target_r =
                            (pos.target_price - pos.fill_price) * sign / pos.risk.max(1e-9);
                        if target_r > 1.0 {
                            Self::latch_exit(&mut s, &cond.id, "target_s1", q.last, true);
                        }
                    }
                }
            } else if s.closes[&cond.id].reason == "target_1r"
                && pos.tier == PositionTier::Large
                && s.closes[&cond.id].filled >= s.closes[&cond.id].target
            {
                pos.lot1_closed = true;
                pos.current_stop_price = pos.fill_price;
                pos.trailing_grade = 1;
                s.closes.remove(&cond.id);
            }
            s.positions.insert(cond.id, pos);
        }
        self.save(&mut s)
    }

    fn latch_exit(s: &mut ExecutionState, id: &str, reason: &str, reference: f64, all: bool) {
        s.aborted_entries.insert(id.into());
        let qty = s.positions.get(id).map(|p| p.volume).unwrap_or(0);
        let task = s.closes.entry(id.into()).or_insert(CloseTask {
            reason: reason.into(),
            target: 0,
            filled: 0,
            reference,
            attempts: 0,
            all,
        });
        task.target = task
            .target
            .max(task.filled + if all { qty } else { qty.min(1) });
        if all {
            task.reason = reason.into();
            task.reference = reference;
            task.all = true;
        }
        if let Some(o) = s.conditions.get_mut(id) {
            o.state = ConditionState::Closing;
            o.note = format!("退出任务已锁定: {reason}");
        }
    }

    /// Drive one serialized broker action per two seconds, across every strategy.
    pub async fn drive(&self) -> Result<()> {
        let _drive = self.drive_lock.lock().await;
        let cfg = self.get_config().await;
        let now = chrono::Utc::now().timestamp();
        let mut s = self.state.lock().await;
        let state = &mut *s;
        if state.comparison.advance(&state.quotes, now) {
            self.save(&mut s)?;
        }
        self.check_overnight_checkpoints(&mut s);
        self.update_overnight_next_open(&mut s);
        if cfg.mode == ExecutionMode::MonitorOnly {
            return Ok(());
        }
        if !s.broker_connected || s.persistence_error.is_some() || now - s.last_action_at < 2 {
            return Ok(());
        }
        // 解除长时间处于 CancelPending 且已重试多次的死锁报单，防止阻塞驱动循环与其它品种交易
        let mut unblocked_any = false;
        for (r, o) in s.orders.iter_mut() {
            if o.phase == OrderPhase::CancelPending
                && o.cancel_attempts >= 3
                && now - o.action_at >= 15
            {
                tracing::warn!(
                    "⚠️ 报单 {} ({}) 处于 CancelPending 且已重试 {} 次超时未获柜台终态回报，将其标记为 Rejected 避免阻塞其它品种交易",
                    r,
                    o.request.instrument_id,
                    o.cancel_attempts
                );
                o.phase = OrderPhase::Rejected;
                o.note = "撤单多次未获回报，标记终止".to_string();
                unblocked_any = true;
            }
        }
        if unblocked_any {
            self.save(&mut s)?;
        }

        let wait = cfg.order_wait_seconds.max(2) as i64;
        // Cancel a timed-out live order before replacing it. Terminal != all fills received.
        let cancel = s
            .orders
            .iter()
            .filter(|(_, o)| {
                let abort = o.reason.is_none() && s.aborted_entries.contains(&o.condition_id);
                let reprice = s
                    .quotes
                    .get(&o.request.instrument_id.to_uppercase())
                    .filter(|q| q.valid())
                    .and_then(|q| {
                        q.order_price(
                            o.request.direction == "buy",
                            o.request.price,
                            cfg.slippage_ticks,
                            0,
                        )
                    })
                    .is_some_and(|price| {
                        if o.request.direction == "buy" {
                            price > o.request.price
                        } else {
                            price < o.request.price
                        }
                    });
                (o.phase == OrderPhase::Working
                    && (abort || (now - o.action_at >= wait && reprice)))
                    || (o.phase == OrderPhase::CancelPending
                        && o.cancel_attempts < 3
                        && now - o.action_at >= wait * (o.cancel_attempts as i64 + 1))
            })
            .min_by_key(|(_, o)| {
                (
                    o.reason.is_none() && !s.aborted_entries.contains(&o.condition_id),
                    o.action_at,
                )
            })
            .map(|(r, o)| (r.clone(), o.clone()));
        if let Some((r, order)) = cancel {
            let o_mut = s.orders.get_mut(&r).unwrap();
            o_mut.phase = OrderPhase::CancelPending;
            o_mut.cancel_attempts += 1;
            o_mut.action_at = now;
            s.last_action_at = now;
            self.save(&mut s)?;
            drop(s);
            let result = self
                .client
                .cancel_order(
                    &r,
                    &order.request.instrument_id,
                    order.request.exchange_id.as_deref(),
                )
                .await;
            if let Err(e) = result {
                tracing::warn!("撤单结果待柜台确认: {e}");
            }
            return Ok(());
        }
        if !s.broker_ready || s.error.is_some() || s.market_error.is_some() {
            return Ok(());
        }
        let mut entry_state_changed = Self::refresh_entry_funding_waits(&mut s);
        let mut conditions = s.conditions.values().cloned().collect::<Vec<_>>();
        conditions.sort_by_key(|o| (!s.closes.contains_key(&o.id), o.created_at, o.id.clone()));
        let mut candidate = None;
        for cond in conditions {
            let blocked = s.orders.values().any(|o| {
                o.condition_id == cond.id
                    && (!matches!(o.phase, OrderPhase::Terminal | OrderPhase::Rejected)
                        || o.filled < o.reported_filled)
            });
            if blocked {
                continue;
            }
            let Some(q) = s
                .quotes
                .get(&cond.instrument_id.to_uppercase())
                .cloned()
                .filter(|q| q.valid())
            else {
                continue;
            };
            let filled: i32 = s
                .orders
                .values()
                .filter(|o| o.condition_id == cond.id && o.reason.is_none())
                .map(|o| o.filled)
                .sum();
            let exit = s.closes.get(&cond.id).cloned();
            let (qty, reason, reference, attempts) = if let Some(task) = &exit {
                (
                    task.target - task.filled,
                    Some(task.reason.clone()),
                    task.reference,
                    task.attempts,
                )
            } else {
                if cfg.mode == ExecutionMode::MonitorOnly
                    || !s.entry_symbols_ready.contains(&cond.symbol)
                    || !s.authorized_entries.contains(&cond.id)
                    || s.aborted_entries.contains(&cond.id)
                // 暂时停用日内亏损熔断限制（后续如需重新启用，解开下方注释即可）：
                // || (cfg.daily_loss_limit_r > 0.0 && s.loss_r >= cfg.daily_loss_limit_r)
                {
                    continue;
                }
                let risk_ticks = cond.risk / q.tick;
                if risk_ticks + 1e-6 < cfg.min_entry_risk_ticks as f64 {
                    entry_state_changed |= Self::set_entry_note(
                        &mut s,
                        &cond.id,
                        format!(
                            "入场筛选：止损距离{risk_ticks:.2} tick小于{} tick，暂缓新开仓",
                            cfg.min_entry_risk_ticks,
                        ),
                    );
                    continue;
                }
                if let Some(note) = Self::entry_funding_note(&s, &cond.id, now) {
                    entry_state_changed |= Self::set_entry_note(&mut s, &cond.id, note);
                    continue;
                }
                (cond.volume - filled, None, cond.entry_price, 0)
            };
            if qty <= 0 {
                continue;
            }
            let buy = if reason.is_none() {
                cond.direction == "up"
            } else {
                cond.direction != "up"
            };
            let max_ticks = if reason.is_some() {
                cfg.max_exit_slippage_ticks
            } else {
                // 入场最大允许滑点：max(止损空间 * 0.2, 2 * tick)
                let max_entry_slippage = (cond.risk * 0.2).max(2.0 * q.tick);
                let market_price = if buy { q.ask } else { q.bid };
                let market_price = if market_price.is_finite() && market_price > 0.0 {
                    market_price
                } else {
                    q.last
                };
                let overshot = if buy {
                    market_price > cond.entry_price + max_entry_slippage + 1e-6
                } else {
                    market_price < cond.entry_price - max_entry_slippage - 1e-6
                };
                if overshot {
                    let limit_desc = if buy { "上限" } else { "下限" };
                    let max_p = if buy {
                        cond.entry_price + max_entry_slippage
                    } else {
                        cond.entry_price - max_entry_slippage
                    };
                    if let Some(o) = s.conditions.get_mut(&cond.id) {
                        o.note = format!(
                            "盘口{:.1}超入场滑点限制({}{:.1})，暂缓报单",
                            market_price, limit_desc, max_p
                        );
                    }
                    continue;
                }
                (max_entry_slippage / q.tick).round() as u32
            };
            let ticks = cfg.slippage_ticks.saturating_add(attempts).min(20);
            let Some(price) = q.order_price(buy, reference, ticks, max_ticks) else {
                continue;
            };
            s.last_ref = (s.last_ref + 1).max(chrono::Utc::now().timestamp() as u64 * 100);
            let r = s.last_ref.to_string();
            let req = OrderInsertRequest {
                instrument_id: cond.instrument_id.clone(),
                direction: if buy { "buy" } else { "sell" }.into(),
                offset: if reason.is_none() { "open" } else { "auto" }.into(),
                price,
                volume: qty,
                exchange_id: Some(q.exchange_id.clone()),
                order_ref: Some(r.clone()),
            };
            let entry_rules = reason.is_none().then(|| EntryRuleSnapshot {
                version: "live-entry-funding-v1".into(),
                config: cfg.clone(),
                planned_risk_ticks: cond.risk / q.tick,
                comparison_run_id: s
                    .comparison
                    .runs
                    .iter()
                    .find(|r| r.signals.contains_key(&cond.id))
                    .map(|r| r.definition.id.clone()),
            });
            candidate = Some((r, req, cond.id.clone(), reason, entry_rules));
            break;
        }
        let Some((r, req, id, reason, entry_rules)) = candidate else {
            if entry_state_changed {
                self.save(&mut s)?;
            }
            return Ok(());
        };
        let day = s.trading_day.clone();
        let funds_at_submit = if reason.is_none() {
            s.entry_funds.clone()
        } else {
            None
        };
        s.orders.insert(
            r.clone(),
            ExecutionOrder {
                request: req.clone(),
                condition_id: id.clone(),
                reason: reason.clone(),
                phase: OrderPhase::Unknown,
                reported_filled: 0,
                filled: 0,
                action_at: now,
                trading_day: day,
                note: "已持久化提交意图，等待柜台回报".into(),
                cancel_attempts: 0,
                funds_at_submit,
                entry_rules,
            },
        );
        if reason.is_none() {
            s.entry_funding_waits.remove(&id);
        }
        if let Some(task) = s.closes.get_mut(&id) {
            task.attempts += 1;
        }
        s.last_action_at = now;
        self.save(&mut s)?;
        drop(s);
        let result = self.client.insert_order(&req).await;
        let mut s = self.state.lock().await;
        let order = s.orders.get_mut(&r).unwrap();
        if order.phase == OrderPhase::Unknown {
            match result {
                Ok(res) if !res.success => {
                    order.phase = OrderPhase::Rejected;
                    order.note = res.msg;
                }
                Ok(res) => {
                    order.note = res.msg;
                } // Submission success is not a broker order acknowledgement.
                Err(e) => {
                    order.note = format!("提交结果未知，等待查询或回报核实，禁止盲目重报: {e}");
                }
            }
        }
        if reason.is_none() {
            let phase = if order.phase == OrderPhase::Rejected {
                ConditionState::Rejected
            } else {
                ConditionState::Unknown
            };
            let note = order.note.clone();
            if let Some(cond) = s.conditions.get_mut(&id) {
                if !matches!(
                    cond.state,
                    ConditionState::Filled
                        | ConditionState::PartFilled
                        | ConditionState::Closing
                        | ConditionState::Closed
                ) {
                    cond.state = phase;
                    cond.note = note;
                }
            }
        }
        Self::refresh_entry_funding_waits(&mut s);
        self.save(&mut s)
    }

    pub async fn on_price_update(
        &self,
        symbol: &str,
        _high: f64,
        _low: f64,
        _close: f64,
        bar_ts: &str,
        is_new_bar: bool,
    ) -> Result<()> {
        if !is_new_bar {
            return Ok(());
        } // Execution uses actual-contract quote events, never historical OHLC touches.
        let mut s = self.state.lock().await;
        if s.last_bars
            .get(symbol)
            .is_some_and(|last| last.as_str() >= bar_ts)
        {
            return Ok(());
        }
        s.comparison.on_bar(symbol, bar_ts);
        let first = s.last_bars.insert(symbol.into(), bar_ts.into()).is_none();
        // Startup establishes a baseline; old bars must not advance a recovered holding.
        if first {
            return self.save(&mut s);
        }
        let mut expired = Vec::new();
        for o in s.conditions.values_mut().filter(|o| {
            o.symbol == symbol
                && !matches!(
                    o.state,
                    ConditionState::Filled
                        | ConditionState::Closing
                        | ConditionState::Closed
                        | ConditionState::Canceled
                        | ConditionState::Expired
                        | ConditionState::Invalidated
                )
        }) {
            if bar_ts > o.warning_bar_ts.as_str() {
                o.bars_waited += 1;
            }
            if o.bars_waited > o.max_wait_bars {
                o.state = ConditionState::Expired;
                expired.push(o.id.clone());
            }
        }
        s.aborted_entries.extend(expired);
        let ids = s
            .positions
            .values()
            .filter(|p| p.symbol == symbol && !p.is_closed)
            .map(|p| p.id.clone())
            .collect::<Vec<_>>();
        for id in ids {
            let p = s.positions.get_mut(&id).unwrap();
            p.bars_held += 1;
            let reason = if p.bars_held >= TIME_HORIZON_BARS {
                Some("time_exit")
            } else if p.bars_held == NO_FOLLOW_BAR && p.max_mfe_r < NO_FOLLOW_MFE_R {
                Some("no_follow")
            } else {
                None
            };
            let reference = p.fill_price;
            if let Some(reason) = reason {
                Self::latch_exit(&mut s, &id, reason, reference, true);
            }
        }
        self.save(&mut s)
    }
}

#[cfg(test)]
#[path = "execution_tests.rs"]
mod tests;
