use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum ExecutionMode {
    /// 仅监控/模拟：不向柜台发送任何真实开平仓委托
    #[serde(alias = "dry_run", alias = "DryRun", alias = "MonitorOnly", alias = "monitor_only")]
    MonitorOnly,
    /// 半自动：触发突破时弹窗提示，待人工确认后才报单；成交后平仓全自动
    #[serde(alias = "semi_auto", alias = "SemiAuto")]
    SemiAuto,
    /// 全自动：触发后严格按交易规则无人值守全自动报单开平仓
    #[serde(alias = "full_auto", alias = "FullAuto")]
    FullAuto,
}

impl Default for ExecutionMode {
    fn default() -> Self {
        Self::SemiAuto
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TraderHealth {
    pub service: String,
    pub healthy: bool,
    pub pid: Option<u32>,
    pub uptime: f64,
    pub connected: bool,
    pub authenticated: bool,
    pub logged_in: bool,
    pub ready: bool,
    pub trading_day: Option<String>,
    pub broker_id: Option<String>,
    pub user_id: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct TraderAccount {
    pub balance: f64,
    pub available: f64,
    pub curr_margin: f64,
    pub close_profit: f64,
    pub position_profit: f64,
    pub updated_at: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TraderPosition {
    pub instrument_id: String,
    pub direction: String, // "buy" or "sell"
    pub position: i32,
    pub yd_position: i32,
    pub today_position: i32,
    pub open_cost: f64,
    pub position_profit: f64,
    pub margin: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OrderInsertRequest {
    pub instrument_id: String,
    pub direction: String, // "buy" or "sell"
    #[serde(default = "default_offset")]
    pub offset: String, // "open", "close", "close_today", "auto"
    pub price: f64,
    pub volume: i32,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub exchange_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub order_ref: Option<String>,
}

fn default_offset() -> String {
    "auto".to_string()
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OrderInsertResponse {
    pub success: bool,
    #[serde(default)]
    pub order_ref: String,
    pub msg: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OrderCancelRequest {
    pub order_ref: String,
    pub instrument_id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub exchange_id: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OrderCancelResponse {
    pub success: bool,
    pub msg: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TraderOrderRecord {
    pub order_ref: String,
    pub instrument_id: String,
    pub exchange_id: String,
    pub direction: String,
    pub offset: String,
    pub limit_price: f64,
    pub volume_total_original: i32,
    pub volume_traded: i32,
    pub volume_total: i32,
    pub status: String, // "all_traded", "queueing", "canceled", "part_traded"
    pub status_msg: String,
    pub insert_time: String,
    pub update_time: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TraderTradeRecord {
    pub trade_id: String,
    pub order_ref: String,
    pub instrument_id: String,
    pub exchange_id: String,
    pub direction: String,
    pub offset: String,
    pub price: f64,
    pub volume: i32,
    pub trade_time: String,
    pub trade_date: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct UpdateEvent {
    pub id: u64,
    #[serde(rename = "type")]
    pub event_type: String,
    pub data: serde_json::Value,
    pub ts: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct UpdatesResponse {
    pub events: Vec<UpdateEvent>,
    pub last_id: u64,
}
