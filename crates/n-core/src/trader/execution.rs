use std::collections::HashMap;
use anyhow::Result;
use tokio::sync::RwLock;

use crate::analyze::model::Dir;
use crate::analyze::outcome::{
    NO_FOLLOW_BAR, NO_FOLLOW_MFE_R, PENDING_BARS, TIME_HORIZON_BARS, TRAIL_START_R, TRAIL_STEP_R,
};
use super::client::CtpTraderClient;
use super::model::*;

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PositionTier {
    /// 大仓位：2手，评分 >= 3.5，采用阶梯追踪止盈 (1.0R锁保本，0.8R步长上移)
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
    pub trailing_grade: usize,
    pub current_stop_price: f64,
    pub lot1_closed: bool,
    pub exit_reason: Option<String>,
    pub exit_price: Option<f64>,
    pub is_closed: bool,
    pub realized_pnl_pts: Option<f64>,
    pub r_multiple: Option<f64>,
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
    pub r_multiple: f64,
    pub max_mfe_r: f64,
    pub max_mae_r: f64,
    pub bars_held: usize,
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
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct ExecutionConfig {
    pub mode: ExecutionMode,
    pub max_risk_percent: f64,
    pub default_large_volume: i32,
    pub default_small_volume: i32,
    pub daily_loss_limit_r: f64,
}

impl Default for ExecutionConfig {
    fn default() -> Self {
        Self {
            mode: ExecutionMode::SemiAuto,
            max_risk_percent: 1.0,
            default_large_volume: 2,
            default_small_volume: 1,
            daily_loss_limit_r: 3.0,
        }
    }
}

pub struct ExecutionEngine {
    client: CtpTraderClient,
    config: RwLock<ExecutionConfig>,
    config_path: Option<std::path::PathBuf>,
    // key: condition_order_id
    conditions: RwLock<HashMap<String, ConditionOrder>>,
    // key: position_id
    positions: RwLock<HashMap<String, ManagedPosition>>,
    // historical closed trade records
    trade_history: RwLock<Vec<TradeRecord>>,
    // daily cumulated realized loss R
    daily_cumulated_loss_r: RwLock<f64>,
}

impl ExecutionEngine {
    pub fn new(client: CtpTraderClient, config: ExecutionConfig) -> Self {
        Self::with_config_path(client, config, None)
    }

    pub fn with_config_path(
        client: CtpTraderClient,
        config: ExecutionConfig,
        config_path: Option<std::path::PathBuf>,
    ) -> Self {
        Self {
            client,
            config: RwLock::new(config),
            config_path,
            conditions: RwLock::new(HashMap::new()),
            positions: RwLock::new(HashMap::new()),
            trade_history: RwLock::new(Vec::new()),
            daily_cumulated_loss_r: RwLock::new(0.0),
        }
    }

    pub async fn get_config(&self) -> ExecutionConfig {
        self.config.read().await.clone()
    }

    pub async fn update_config(&self, cfg: ExecutionConfig) {
        *self.config.write().await = cfg.clone();
        if let Some(ref path) = self.config_path {
            if let Ok(json) = serde_json::to_string_pretty(&cfg) {
                let _ = std::fs::write(path, json);
                tracing::info!("💾 交易执行配置已成功持久化至磁盘: {}", path.display());
            }
        }
    }

    pub async fn list_conditions(&self) -> Vec<ConditionOrder> {
        self.conditions.read().await.values().cloned().collect()
    }

    pub async fn list_positions(&self) -> Vec<ManagedPosition> {
        self.positions.read().await.values().cloned().collect()
    }

    pub async fn get_trade_history(&self) -> Vec<TradeRecord> {
        self.trade_history.read().await.clone()
    }

    /// 汇总统计所有已了结的历史交易指标
    pub async fn get_trade_statistics(&self) -> TradeStatistics {
        let history = self.trade_history.read().await;
        if history.is_empty() {
            return TradeStatistics::default();
        }

        let mut win_count = 0;
        let mut loss_count = 0;
        let mut total_r = 0.0;
        let mut gross_gain_r = 0.0;
        let mut gross_loss_r = 0.0;
        let mut large_count = 0;
        let mut small_count = 0;
        let mut reasons: HashMap<String, usize> = HashMap::new();

        for t in history.iter() {
            total_r += t.r_multiple;
            if t.r_multiple > 0.0 {
                win_count += 1;
                gross_gain_r += t.r_multiple;
            } else if t.r_multiple < 0.0 {
                loss_count += 1;
                gross_loss_r += t.r_multiple.abs();
            }

            match t.tier {
                PositionTier::Large => large_count += 1,
                PositionTier::Small => small_count += 1,
            }

            *reasons.entry(t.exit_reason.clone()).or_insert(0) += 1;
        }

        let total = history.len();
        let win_rate = if total > 0 { (win_count as f64 / total as f64) * 100.0 } else { 0.0 };
        let avg_r = if total > 0 { total_r / total as f64 } else { 0.0 };
        let pf = if gross_loss_r > 0.0 { gross_gain_r / gross_loss_r } else { gross_gain_r };

        TradeStatistics {
            total_trades: total,
            win_trades: win_count,
            loss_trades: loss_count,
            win_rate_pct: win_rate,
            total_r,
            avg_r,
            profit_factor: pf,
            large_trades_count: large_count,
            small_trades_count: small_count,
            exit_reason_breakdown: reasons,
        }
    }

    /// 注册一笔来自形态分析的突破条件单（根据评分自动分流大仓 2 手 vs 小仓 1 手）
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
        let risk = (entry - stop).abs();
        if risk <= 0.0 {
            anyhow::bail!("风险空间非正，拒绝注册条件单");
        }
        let cfg = self.config.read().await;

        // 根据评分判定大仓位（>=3.5分）或小仓位（2.5 ~ 3.5分）
        let tier = if score >= 3.5 {
            PositionTier::Large
        } else {
            PositionTier::Small
        };

        let volume = match tier {
            PositionTier::Large => cfg.default_large_volume.max(1),
            PositionTier::Small => cfg.default_small_volume.max(1),
        };

        let id = uuid::Uuid::new_v4().to_string();
        let dir_str = match dir {
            Dir::Up => "up",
            Dir::Down => "down",
        };
        let tier_label = match tier {
            PositionTier::Large => "大仓位(2手/阶梯止盈)",
            PositionTier::Small => "小仓位(1手/1:1止盈)",
        };
        let order = ConditionOrder {
            id: id.clone(),
            event_id,
            symbol: symbol.to_string(),
            instrument_id: instrument_id.to_string(),
            direction: dir_str.to_string(),
            tier,
            entry_price: entry,
            stop_price: stop,
            target_price: target,
            risk,
            score,
            volume,
            warning_bar_ts: warning_ts.to_string(),
            bars_waited: 0,
            max_wait_bars: PENDING_BARS,
            state: ConditionState::Pending,
            created_at: chrono::Local::now().timestamp(),
            note: format!("评分: {:.2} ({})，等待突破入场", score, tier_label),
        };
        self.conditions.write().await.insert(id.clone(), order);
        tracing::info!(
            "✅ [条件单注册] ID: {} | 品种: {} ({}) | 分档: {} | 手数: {} | 触发买点: {:.1} | 止损: {:.1}",
            id, symbol, instrument_id, tier_label, volume, entry, stop
        );
        Ok(id)
    }

    /// 撤销指定的未触发条件单
    pub async fn cancel_condition(&self, id: &str) -> Result<bool> {
        let mut guard = self.conditions.write().await;
        if let Some(ord) = guard.get_mut(id) {
            if ord.state == ConditionState::Pending || ord.state == ConditionState::WaitingConfirm {
                ord.state = ConditionState::Canceled;
                ord.note = "用户或系统主动撤销".to_string();
                return Ok(true);
            }
        }
        Ok(false)
    }

    /// 盘中价格触碰或新 K 线产生时的核心撮合推进函数
    pub async fn on_price_update(
        &self,
        symbol: &str,
        high: f64,
        low: f64,
        close: f64,
        bar_ts: &str,
        is_new_bar: bool,
    ) -> Result<()> {
        let mode = self.config.read().await.mode;
        let loss_r = *self.daily_cumulated_loss_r.read().await;
        let loss_limit = self.config.read().await.daily_loss_limit_r;

        // 1. 检查日亏损熔断
        let is_circuit_broken = loss_r >= loss_limit;

        // 2. 检查待触发的条件单
        let mut to_fill: Vec<(ConditionOrder, f64)> = Vec::new();
        {
            let mut cond_guard = self.conditions.write().await;
            for ord in cond_guard.values_mut() {
                if ord.symbol != symbol || ord.state != ConditionState::Pending {
                    continue;
                }
                if is_new_bar {
                    ord.bars_waited += 1;
                    if ord.bars_waited > ord.max_wait_bars {
                        ord.state = ConditionState::Expired;
                        ord.note = format!("预警后超过 {} 根 K 线未触发，已过时失效", ord.max_wait_bars);
                        continue;
                    }
                }
                if is_circuit_broken {
                    ord.note = "单日累计亏损已达熔断阈值，暂停新开仓".to_string();
                    continue;
                }

                let touched = match ord.direction.as_str() {
                    "up" => high >= ord.entry_price,
                    "down" => low <= ord.entry_price,
                    _ => false,
                };

                if touched {
                    let fill_price = ord.entry_price;
                    match mode {
                        ExecutionMode::MonitorOnly => {
                            ord.state = ConditionState::Filled;
                            ord.note = format!("模拟撮合成交 @ {:.1}", fill_price);
                            to_fill.push((ord.clone(), fill_price));
                        }
                        ExecutionMode::SemiAuto => {
                            ord.state = ConditionState::WaitingConfirm;
                            ord.note = format!("行情已触碰买点 {:.1}，等待人工点击确认开仓", ord.entry_price);
                        }
                        ExecutionMode::FullAuto => {
                            ord.state = ConditionState::Triggered;
                            ord.note = "突破触发，正在向 CTP 发送开仓指令".to_string();
                            let dir_ctp = match ord.direction.as_str() {
                                "up" => "buy",
                                "down" => "sell",
                                _ => "buy",
                            };
                            let req = OrderInsertRequest {
                                instrument_id: ord.instrument_id.clone(),
                                direction: dir_ctp.to_string(),
                                offset: "open".to_string(),
                                price: fill_price,
                                volume: ord.volume,
                                exchange_id: None,
                                order_ref: None,
                            };
                            if let Ok(res) = self.client.insert_order(&req).await {
                                if res.success {
                                    ord.state = ConditionState::Filled;
                                    to_fill.push((ord.clone(), fill_price));
                                } else {
                                    ord.note = format!("CTP拒绝开仓: {}", res.msg);
                                }
                            }
                        }
                    }
                }
            }
        }

        // 为成功触发成交的订单创建持仓追踪实体
        let mut newly_opened_ids = std::collections::HashSet::new();
        for (ord, fill) in to_fill {
            let pos_id = uuid::Uuid::new_v4().to_string();
            let managed = ManagedPosition {
                id: pos_id.clone(),
                condition_order_id: ord.id.clone(),
                symbol: ord.symbol.clone(),
                instrument_id: ord.instrument_id.clone(),
                direction: ord.direction.clone(),
                tier: ord.tier,
                fill_price: fill,
                initial_stop_price: ord.stop_price,
                target_price: ord.target_price,
                risk: ord.risk,
                volume: ord.volume,
                open_ts: bar_ts.to_string(),
                bars_held: 0,
                max_mfe_r: 0.0,
                max_mae_r: 0.0,
                trailing_grade: 0,
                current_stop_price: ord.stop_price,
                lot1_closed: false,
                exit_reason: None,
                exit_price: None,
                is_closed: false,
                realized_pnl_pts: None,
                r_multiple: None,
            };
            self.positions.write().await.insert(pos_id.clone(), managed);
            newly_opened_ids.insert(pos_id.clone());
            tracing::info!(
                "🎉 [开仓成交建立持仓] ID: {} | 合约: {} | 方向: {} | 仓位类型: {:?} ({}手) | 开仓价: {:.1} | 初始止损: {:.1}",
                pos_id, ord.instrument_id, ord.direction, ord.tier, ord.volume, fill, ord.stop_price
            );
        }

        // 3. 推进已持有的仓位 (差异化平仓出场)
        let mut to_close: Vec<(String, String, f64, String, i32)> = Vec::new(); // (pos_id, inst, price, reason, volume)
        let mut split_lot1_records: Vec<TradeRecord> = Vec::new();
        {
            let mut pos_guard = self.positions.write().await;
            for pos in pos_guard.values_mut() {
                if pos.symbol != symbol || pos.is_closed || newly_opened_ids.contains(&pos.id) {
                    continue;
                }
                if is_new_bar {
                    pos.bars_held += 1;
                }
                let is_long = pos.direction == "up";
                let fill = pos.fill_price;
                let risk = pos.risk.max(1e-9);

                // 更新 MFE / MAE
                let mfe = if is_long { (high - fill) / risk } else { (fill - low) / risk };
                let mae = if is_long { (low - fill) / risk } else { (fill - high) / risk };
                pos.max_mfe_r = pos.max_mfe_r.max(mfe);
                pos.max_mae_r = pos.max_mae_r.min(mae);

                // 出场规则 1: 硬性初始止损 (仅在第一手未平仓保本前生效)
                if !pos.lot1_closed && pos.trailing_grade == 0 {
                    let hit_stop = if is_long { low <= pos.initial_stop_price } else { high >= pos.initial_stop_price };
                    if hit_stop {
                        to_close.push((pos.id.clone(), pos.instrument_id.clone(), pos.initial_stop_price, "stop".to_string(), pos.volume));
                        continue;
                    }
                }

                // 出场规则 2-A: 小仓位 (1手) -> 1:1 盈亏比固定止盈
                if pos.tier == PositionTier::Small && pos.max_mfe_r >= 1.0 {
                    let target_1r = if is_long { fill + risk } else { fill - risk };
                    to_close.push((pos.id.clone(), pos.instrument_id.clone(), target_1r, "target_1r".to_string(), pos.volume));
                    continue;
                }

                // 出场规则 2-B: 大仓位 (2手) -> 第一手 1.0R 平仓保本 + 第二手止损移至成本线并阶梯追踪
                if pos.tier == PositionTier::Large {
                    // 第 1 步：浮盈首次触及 1.0R，第一手直接平仓保本，第二手止损移动到成本线 (fill)
                    if !pos.lot1_closed && pos.max_mfe_r >= TRAIL_START_R {
                        pos.lot1_closed = true;
                        pos.volume -= 1; // 剩余 1 手
                        pos.current_stop_price = fill; // 关键：止损立即移动到开仓成本线，保本！
                        pos.trailing_grade = 1;
                        let target_1r = if is_long { fill + risk } else { fill - risk };

                        // 记录第一手的平仓流水
                        let pnl_pts = if is_long { target_1r - fill } else { fill - target_1r };
                        let r_mult = pnl_pts / risk;
                        let rec1 = TradeRecord {
                            id: format!("{}_lot1", pos.id),
                            condition_order_id: pos.condition_order_id.clone(),
                            symbol: pos.symbol.clone(),
                            instrument_id: pos.instrument_id.clone(),
                            direction: pos.direction.clone(),
                            tier: pos.tier,
                            volume: 1,
                            fill_price: fill,
                            exit_price: target_1r,
                            risk: pos.risk,
                            initial_stop_price: pos.initial_stop_price,
                            target_price: pos.target_price,
                            open_ts: pos.open_ts.clone(),
                            exit_ts: bar_ts.to_string(),
                            exit_reason: "target_1r".to_string(),
                            realized_pnl_pts: pnl_pts,
                            r_multiple: r_mult,
                            max_mfe_r: pos.max_mfe_r,
                            max_mae_r: pos.max_mae_r,
                            bars_held: pos.bars_held,
                        };
                        split_lot1_records.push(rec1);

                        // 若为全自动模式，向 CTP 发送 1 手平仓单
                        if mode == ExecutionMode::FullAuto {
                            let close_dir = if is_long { "sell" } else { "buy" };
                            let req = OrderInsertRequest {
                                instrument_id: pos.instrument_id.clone(),
                                direction: close_dir.to_string(),
                                offset: "auto".to_string(),
                                price: target_1r,
                                volume: 1,
                                exchange_id: None,
                                order_ref: None,
                            };
                            let _ = self.client.insert_order(&req).await;
                        }

                        tracing::info!(
                            "🎉 [大仓分批止盈] {} 浮盈达到 1.0R，第一手平仓 1 手锁定保本；第二手止损已自动移至开仓成本线 {:.1}",
                            pos.instrument_id, fill
                        );
                    }

                    // 第 2 步：第二手继续进行阶梯追踪止盈 (基准线从成本线开始向上抬升)
                    if pos.lot1_closed && pos.volume > 0 {
                        let mut g = pos.trailing_grade;
                        loop {
                            let next_g = g + 1;
                            let next_r = TRAIL_STEP_R * next_g as f64; // e.g. 1.6R, 2.4R...
                            if pos.max_mfe_r >= next_r {
                                g = next_g;
                            } else {
                                break;
                            }
                        }
                        if g > pos.trailing_grade {
                            pos.trailing_grade = g;
                            let lock_r = TRAIL_STEP_R * (g - 1) as f64; // 0.8R, 1.6R...
                            pos.current_stop_price = if is_long { fill + lock_r * risk } else { fill - lock_r * risk };
                            tracing::info!(
                                "📈 [第二手阶梯锁盈抬升] {} 浮盈达到档位 {}，止损线上移至 {:.1}",
                                pos.instrument_id, g, pos.current_stop_price
                            );
                        }

                        // 检查是否回踩跌破最新止损线 (成本线或锁定线)
                        let fell_back = if is_long { low <= pos.current_stop_price } else { high >= pos.current_stop_price };
                        if fell_back {
                            let reason = if pos.trailing_grade > 1 { "trailing" } else { "breakeven" };
                            to_close.push((
                                pos.id.clone(),
                                pos.instrument_id.clone(),
                                pos.current_stop_price,
                                reason.to_string(),
                                pos.volume,
                            ));
                            continue;
                        }
                    }
                }

                // 出场规则 3: 第 5 根 Bar 收盘无跟随出场 (统一适用)
                if is_new_bar && pos.bars_held == NO_FOLLOW_BAR && pos.max_mfe_r < NO_FOLLOW_MFE_R {
                    to_close.push((pos.id.clone(), pos.instrument_id.clone(), close, "no_follow".to_string(), pos.volume));
                    continue;
                }

                // 出场规则 4: 触及原形态目标位 S1 前高前低
                let hit_target = if is_long { high >= pos.target_price } else { low <= pos.target_price };
                if hit_target {
                    to_close.push((pos.id.clone(), pos.instrument_id.clone(), pos.target_price, "target_s1".to_string(), pos.volume));
                    continue;
                }

                // 出场规则 5: 持仓 60 根 Bar 超时强制离场 (统一适用)
                if is_new_bar && pos.bars_held >= TIME_HORIZON_BARS {
                    to_close.push((pos.id.clone(), pos.instrument_id.clone(), close, "time_exit".to_string(), pos.volume));
                    continue;
                }
            }
        }

        // 保存大仓第一手分批平仓流水
        for rec in split_lot1_records {
            self.trade_history.write().await.push(rec);
        }

        // 执行平仓
        for (pos_id, inst, exit_price, reason, volume) in to_close {
            self.execute_close_position(&pos_id, &inst, exit_price, &reason, bar_ts, volume).await?;
        }

        Ok(())
    }

    /// 执行平仓动作并将明细落入历史交易记录
    pub async fn execute_close_position(
        &self,
        pos_id: &str,
        instrument_id: &str,
        exit_price: f64,
        reason: &str,
        exit_ts: &str,
        volume: i32,
    ) -> Result<()> {
        let mode = self.config.read().await.mode;
        let mut pos_guard = self.positions.write().await;
        let Some(pos) = pos_guard.get_mut(pos_id) else {
            return Ok(());
        };
        if pos.is_closed {
            return Ok(());
        }

        let is_long = pos.direction == "up";
        let close_dir = if is_long { "sell" } else { "buy" };

        if mode == ExecutionMode::FullAuto {
            let req = OrderInsertRequest {
                instrument_id: instrument_id.to_string(),
                direction: close_dir.to_string(),
                offset: "auto".to_string(),
                price: exit_price,
                volume,
                exchange_id: None,
                order_ref: None,
            };
            let _ = self.client.insert_order(&req).await;
        }

        let risk = pos.risk.max(1e-9);
        let pnl_pts = if is_long { exit_price - pos.fill_price } else { pos.fill_price - exit_price };
        let r_mult = pnl_pts / risk;

        pos.is_closed = true;
        pos.volume = 0;
        pos.exit_price = Some(exit_price);
        pos.exit_reason = Some(reason.to_string());
        pos.realized_pnl_pts = Some(pnl_pts);
        pos.r_multiple = Some(r_mult);

        if r_mult < 0.0 {
            *self.daily_cumulated_loss_r.write().await += r_mult.abs();
        }

        // 保存历史交易记录
        let record = TradeRecord {
            id: if pos.lot1_closed { format!("{}_lot2", pos.id) } else { pos.id.clone() },
            condition_order_id: pos.condition_order_id.clone(),
            symbol: pos.symbol.clone(),
            instrument_id: pos.instrument_id.clone(),
            direction: pos.direction.clone(),
            tier: pos.tier,
            volume,
            fill_price: pos.fill_price,
            exit_price,
            risk: pos.risk,
            initial_stop_price: pos.initial_stop_price,
            target_price: pos.target_price,
            open_ts: pos.open_ts.clone(),
            exit_ts: exit_ts.to_string(),
            exit_reason: reason.to_string(),
            realized_pnl_pts: pnl_pts,
            r_multiple: r_mult,
            max_mfe_r: pos.max_mfe_r,
            max_mae_r: pos.max_mae_r,
            bars_held: pos.bars_held,
        };
        self.trade_history.write().await.push(record);

        tracing::info!(
            "🔔 [持仓了结] ID: {} | 合约: {} | 分档: {:?} ({}手) | 原因: {} | 平仓价: {:.1} | 盈亏点数: {:+.1} | 收益: {:+.2}R",
            pos_id, instrument_id, pos.tier, volume, reason, exit_price, pnl_pts, r_mult
        );
        Ok(())
    }

    /// 半自动模式下人工手动确认开仓
    pub async fn confirm_open(&self, condition_id: &str) -> Result<bool> {
        let mut cond_guard = self.conditions.write().await;
        let Some(ord) = cond_guard.get_mut(condition_id) else {
            return Ok(false);
        };
        if ord.state != ConditionState::WaitingConfirm && ord.state != ConditionState::Pending {
            return Ok(false);
        }

        let dir_ctp = match ord.direction.as_str() {
            "up" => "buy",
            "down" => "sell",
            _ => "buy",
        };
        let req = OrderInsertRequest {
            instrument_id: ord.instrument_id.clone(),
            direction: dir_ctp.to_string(),
            offset: "open".to_string(),
            price: ord.entry_price,
            volume: ord.volume,
            exchange_id: None,
            order_ref: None,
        };
        let res = self.client.insert_order(&req).await?;
        if res.success {
            ord.state = ConditionState::Filled;
            ord.note = "人工确认开仓，报单已提交柜台".to_string();
            // 建立持仓
            let pos_id = uuid::Uuid::new_v4().to_string();
            let managed = ManagedPosition {
                id: pos_id.clone(),
                condition_order_id: ord.id.clone(),
                symbol: ord.symbol.clone(),
                instrument_id: ord.instrument_id.clone(),
                direction: ord.direction.clone(),
                tier: ord.tier,
                fill_price: ord.entry_price,
                initial_stop_price: ord.stop_price,
                target_price: ord.target_price,
                risk: ord.risk,
                volume: ord.volume,
                open_ts: chrono::Local::now().format("%Y-%m-%d %H:%M:%S").to_string(),
                bars_held: 0,
                max_mfe_r: 0.0,
                max_mae_r: 0.0,
                trailing_grade: 0,
                current_stop_price: ord.stop_price,
                lot1_closed: false,
                exit_reason: None,
                exit_price: None,
                is_closed: false,
                realized_pnl_pts: None,
                r_multiple: None,
            };
            self.positions.write().await.insert(pos_id, managed);
            return Ok(true);
        }
        Ok(false)
    }

    /// 重置单日亏损累计计数器（每日开盘时调用）
    pub async fn reset_daily_circuit_breaker(&self) {
        *self.daily_cumulated_loss_r.write().await = 0.0;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn create_test_engine() -> ExecutionEngine {
        let client = CtpTraderClient::for_port(8766);
        let config = ExecutionConfig {
            mode: ExecutionMode::MonitorOnly,
            max_risk_percent: 1.0,
            default_large_volume: 2,
            default_small_volume: 1,
            daily_loss_limit_r: 3.0,
        };
        ExecutionEngine::new(client, config)
    }

    #[tokio::test]
    async fn test_large_position_2_lots_trailing_stop() {
        let engine = create_test_engine();
        // 评分 4.2 >= 3.5 -> 判定为大仓位 (2手，阶梯止盈)
        let cond_id = engine
            .register_condition(
                Some(1),
                "PB0",
                "pb2611",
                Dir::Up,
                16800.0,
                16700.0, // Risk = 100
                17100.0,
                4.2,
                "2026-10-16 10:00:00",
            )
            .await
            .unwrap();

        let conditions = engine.list_conditions().await;
        let c = conditions.iter().find(|x| x.id == cond_id).unwrap();
        assert_eq!(c.tier, PositionTier::Large);
        assert_eq!(c.volume, 2);

        // 触碰 16800 触发开仓
        engine
            .on_price_update("PB0", 16805.0, 16780.0, 16802.0, "2026-10-16 10:15:00", true)
            .await
            .unwrap();

        let positions = engine.list_positions().await;
        assert_eq!(positions[0].volume, 2);
        assert_eq!(positions[0].tier, PositionTier::Large);

        // 1.0R reached (high 16905 >= 16900): 第一手直接平仓 1 手锁定 1.0R 保本，第二手止损移动到成本线 16800
        engine
            .on_price_update("PB0", 16905.0, 16820.0, 16890.0, "2026-10-16 10:30:00", true)
            .await
            .unwrap();
        let p = &engine.list_positions().await[0];
        assert_eq!(p.volume, 1);
        assert!(p.lot1_closed);
        assert_eq!(p.trailing_grade, 1);
        assert_eq!(p.current_stop_price, 16800.0);

        // 检查第一手平仓流水
        let mid_history = engine.get_trade_history().await;
        assert_eq!(mid_history.len(), 1);
        assert_eq!(mid_history[0].volume, 1);
        assert_eq!(mid_history[0].exit_reason, "target_1r");
        assert!((mid_history[0].r_multiple - 1.0).abs() < 1e-4);

        // 1.8R reached (high 16985 >= 16960): 第二手阶梯追踪锁 0.8R (16880)
        engine
            .on_price_update("PB0", 16985.0, 16890.0, 16970.0, "2026-10-16 10:45:00", true)
            .await
            .unwrap();
        let p = &engine.list_positions().await[0];
        assert_eq!(p.trailing_grade, 2);
        assert_eq!(p.current_stop_price, 16880.0);

        // 回落到 16875 -> 触及锁定线 16880 止盈平仓
        engine
            .on_price_update("PB0", 16920.0, 16875.0, 16878.0, "2026-10-16 11:00:00", true)
            .await
            .unwrap();

        let history = engine.get_trade_history().await;
        assert_eq!(history.len(), 2);
        // 第一手: +1.0R 锁定保本
        assert_eq!(history[0].volume, 1);
        assert_eq!(history[0].tier, PositionTier::Large);
        assert_eq!(history[0].exit_reason, "target_1r");
        assert!((history[0].r_multiple - 1.0).abs() < 1e-4);
        // 第二手: +0.8R 阶梯追踪了结
        assert_eq!(history[1].volume, 1);
        assert_eq!(history[1].tier, PositionTier::Large);
        assert_eq!(history[1].exit_reason, "trailing");
        assert!((history[1].r_multiple - 0.8).abs() < 1e-4);

        let stats = engine.get_trade_statistics().await;
        assert_eq!(stats.total_trades, 2);
        assert_eq!(stats.win_trades, 2);
        assert_eq!(stats.win_rate_pct, 100.0);
        assert!((stats.total_r - 1.8).abs() < 1e-4);
    }

    #[tokio::test]
    async fn test_large_position_lot1_win_lot2_breakeven() {
        let engine = create_test_engine();
        let _cond_id = engine
            .register_condition(
                Some(1),
                "PB0",
                "pb2611",
                Dir::Up,
                16800.0,
                16700.0, // Risk = 100
                17100.0,
                4.0,
                "2026-10-16 10:00:00",
            )
            .await
            .unwrap();

        // 触碰 16800 触发开仓 (2手)
        engine
            .on_price_update("PB0", 16805.0, 16780.0, 16802.0, "2026-10-16 10:15:00", true)
            .await
            .unwrap();

        // 1.0R reached (high 16905 >= 16900): 第一手止盈 1.0R，第二手止损移动到成本线 16800
        engine
            .on_price_update("PB0", 16905.0, 16820.0, 16890.0, "2026-10-16 10:30:00", true)
            .await
            .unwrap();

        let positions = engine.list_positions().await;
        assert_eq!(positions[0].volume, 1);
        assert_eq!(positions[0].current_stop_price, 16800.0);

        // 随后行情未进一步上涨，直接大幅回落至 16790 -> 跌破成本线 16800！
        // 注意：不应该打到原硬止损 16700，而是在 16800 成本线直接平仓，reason 为 "breakeven"
        engine
            .on_price_update("PB0", 16850.0, 16790.0, 16795.0, "2026-10-16 10:45:00", true)
            .await
            .unwrap();

        let history = engine.get_trade_history().await;
        assert_eq!(history.len(), 2);
        // 第一手: +1.0R 止盈锁定
        assert_eq!(history[0].exit_reason, "target_1r");
        assert!((history[0].r_multiple - 1.0).abs() < 1e-4);
        // 第二手: 成本线保本出场，R = 0.0，原因 breakeven
        assert_eq!(history[1].exit_reason, "breakeven");
        assert!((history[1].r_multiple - 0.0).abs() < 1e-4);
        assert_eq!(history[1].exit_price, 16800.0);

        let stats = engine.get_trade_statistics().await;
        assert_eq!(stats.total_trades, 2);
        assert_eq!(stats.win_trades, 1); // 1手盈利，1手平局
        assert!((stats.total_r - 1.0).abs() < 1e-4); // 整体净赚 1.0R
    }

    #[tokio::test]
    async fn test_small_position_1_lot_fixed_1r_tp() {
        let engine = create_test_engine();
        // 评分 2.8 < 3.5 -> 判定为小仓位 (1手，1:1盈亏比止盈)
        let cond_id = engine
            .register_condition(
                Some(2),
                "JD0",
                "jd2609",
                Dir::Up,
                3500.0,
                3400.0, // Risk = 100
                3700.0,
                2.8,
                "2026-10-16 10:00:00",
            )
            .await
            .unwrap();

        let conditions = engine.list_conditions().await;
        let c = conditions.iter().find(|x| x.id == cond_id).unwrap();
        assert_eq!(c.tier, PositionTier::Small);
        assert_eq!(c.volume, 1);

        // 触碰 3500 触发开仓
        engine
            .on_price_update("JD0", 3505.0, 3490.0, 3502.0, "2026-10-16 10:15:00", true)
            .await
            .unwrap();

        // 价格达到 3605 (已达到 1.0R，即 3500 + 100 = 3600) -> 立即 1:1 止盈平仓！
        engine
            .on_price_update("JD0", 3605.0, 3510.0, 3590.0, "2026-10-16 10:30:00", true)
            .await
            .unwrap();

        let history = engine.get_trade_history().await;
        assert_eq!(history.len(), 1);
        assert_eq!(history[0].volume, 1);
        assert_eq!(history[0].tier, PositionTier::Small);
        assert_eq!(history[0].exit_reason, "target_1r");
        assert!((history[0].r_multiple - 1.0).abs() < 1e-4);
        assert_eq!(history[0].exit_price, 3600.0);

        let stats = engine.get_trade_statistics().await;
        assert_eq!(stats.small_trades_count, 1);
        assert_eq!(stats.win_rate_pct, 100.0);
        assert!((stats.total_r - 1.0).abs() < 1e-4);
    }
}
