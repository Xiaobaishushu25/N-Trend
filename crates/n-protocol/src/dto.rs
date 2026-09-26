use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct SymbolDto {
    pub code: String,
    pub name: String,
    pub variety: String,
    pub exchange: String,
    pub node: String,
    pub watchlist: bool,
    pub enabled: bool,
    pub tick_size: f64,
    #[serde(default)]
    pub is_followed: bool,
    pub created_at: String,
    pub updated_at: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ContractSuggestionDto {
    pub code: String,
    pub name: String,
    pub variety: String,
    pub exchange: String,
    pub node: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct GroupDto {
    pub id: i64,
    pub name: String,
    pub sort_index: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct KlineDto {
    pub symbol: String,
    pub timeframe: String,
    pub ts: String,
    pub open: f64,
    pub high: f64,
    pub low: f64,
    pub close: f64,
    pub volume: f64,
    pub hold: f64,
    pub source: String,
    pub rollover: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ChartKlineResponse {
    pub rows: Vec<KlineDto>,
    pub status: String,
    pub message: String,
    #[serde(default, skip_serializing_if = "Option::is_none", alias = "latestClosedTs")]
    pub latest_closed_ts: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none", alias = "seriesRevision")]
    pub series_revision: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none", alias = "serverTime")]
    pub server_time: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none", alias = "hasMore")]
    pub has_more: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none", alias = "nextBefore")]
    pub next_before: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none", alias = "fullReloadRequired")]
    pub full_reload_required: Option<bool>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct TrendPointDto {
    pub ts: String,
    pub value: f64,
    pub direction: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct MarketSnapshot {
    pub code: String,
    pub latest: Option<f64>,
    pub change_pct: Option<f64>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ManualLevelDto {
    pub id: i64,
    pub symbol: String,
    pub timeframe: String,
    pub name: String,
    pub start_ts: String,
    pub end_ts: Option<String>,
    pub zone_low: f64,
    pub zone_high: f64,
    pub role: String,
    pub role_override: String,
    pub role_confidence: f64,
    pub status: String,
    pub monitor_enabled: bool,
    pub current_phase: String,
    pub last_event_ts: Option<String>,
    pub created_at: String,
    pub updated_at: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ManualLevelInput {
    pub symbol: String,
    pub timeframe: String,
    pub name: String,
    pub start_ts: String,
    pub end_ts: Option<String>,
    pub zone_low: f64,
    pub zone_high: f64,
    pub role_override: String,
    pub monitor_enabled: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ManualLevelEventDto {
    pub id: i64,
    pub level_id: i64,
    pub symbol: String,
    pub timeframe: String,
    pub event_type: String,
    pub role: Option<String>,
    pub bar_ts: Option<String>,
    pub price: Option<f64>,
    pub reason: String,
    pub phase: String,
    pub role_confidence: f64,
    pub volume_ratio: Option<f64>,
    pub created_at: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ManualLevelAlert {
    pub level_id: i64,
    pub symbol: String,
    pub timeframe: String,
    pub name: String,
    pub event_type: String,
    pub role: String,
    pub role_confidence: f64,
    pub bar_ts: Option<String>,
    pub price: Option<f64>,
    pub reason: String,
    pub phase: String,
    pub volume_ratio: Option<f64>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct PatternEventDto {
    pub id: i64,
    pub symbol: String,
    pub timeframe: String,
    pub logic_version: i64,
    pub warning_kind: String,
    pub warning_ts: String,
    pub warning_bar_idx: i64,
    pub direction: String,
    pub level: String,
    pub grade: String,
    pub entry_score: f64,
    pub target_price: f64,
    pub stop_price: f64,
    pub entry_price: f64,
    pub is_triggered: bool,
    pub trigger_bar_ts: Option<String>,
    pub trigger_price: Option<f64>,
    pub hit_time: Option<String>,
    pub status: String,
    pub is_closed: bool,
    pub exit_bar_ts: Option<String>,
    pub exit_price: Option<f64>,
    pub outcome: Option<String>,
    pub r_multiple: Option<f64>,
    pub mae: Option<f64>,
    pub mfe: Option<f64>,
    pub bars_held: Option<i64>,
    pub user_opened: Option<bool>,
    pub notes: Option<String>,
    pub created_at: String,
    pub updated_at: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct PrecloseSignalDto {
    pub id: i64,
    pub symbol: String,
    pub timeframe: String,
    pub schema_version: i64,
    pub signal_day: String,
    pub trigger_bar_ts: String,
    pub direction: String,
    pub status: String,
    pub score: f64,
    pub created_at: String,
    pub updated_at: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct PrecloseCandidateDto {
    pub id: i64,
    pub symbol: String,
    pub timeframe: String,
    pub schema_version: i64,
    pub signal_day: String,
    pub trigger_bar_ts: String,
    pub direction: String,
    pub status: String,
    pub score: f64,
    pub created_at: String,
    pub updated_at: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct SingleBarAlert {
    pub symbol: String,
    pub name: String,
    pub label: String,
    pub kind: String,
    pub time: String,
    pub price: f64,
    pub trigger_bar_ts: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct EntryTriggerHit {
    pub event_id: i64,
    pub symbol: String,
    pub name: String,
    pub direction: String,
    pub entry_score: f64,
    pub entry_price: f64,
    pub latest_price: f64,
    pub time: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct SymbolFailure {
    pub symbol: String,
    pub reason: String,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
pub struct RefreshStats {
    pub succeeded: usize,
    pub failures: usize,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ScanResult {
    pub scanned: i64,
    pub active_count: i64,
    pub summary: String,
    pub signals: Vec<PatternEventDto>,
    pub new_warnings: Vec<PatternEventDto>,
    pub newly_triggered: Vec<PatternEventDto>,
    pub failed: Vec<SymbolFailure>,
    pub single_bars: Vec<SingleBarAlert>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
pub struct OutcomeRefresh {
    pub updated: usize,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct OutcomeDetail {
    pub event_id: i64,
    pub symbol: String,
    pub logic_version: String,
    pub warning_kind: String,
    pub warning_ts: String,
    pub detected_at: String,
    pub direction: String,
    pub level: String,
    pub grade: String,
    pub entry_score: f64,
    pub trigger_bar_ts: Option<String>,
    pub trigger_price: Option<f64>,
    pub hit_time: Option<String>,
    pub target_price: f64,
    pub stop_price: f64,
    pub outcome: Option<String>,
    pub exit_price: Option<f64>,
    pub exit_bar_ts: Option<String>,
    pub r_multiple: Option<f64>,
    pub mae: Option<f64>,
    pub mfe: Option<f64>,
    pub bars_held: Option<i64>,
    pub notes: Option<String>,
    pub user_opened: Option<bool>,
    pub model_id: Option<String>,
    pub model_p_win: Option<f64>,
    pub model_pred_label: Option<i64>,
    pub features_json: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ReviewStats {
    pub total: usize,
    pub win_count: usize,
    pub loss_count: usize,
    pub scratch_count: usize,
    pub open_count: usize,
    pub win_rate: f64,
    pub profit_factor: f64,
    pub avg_r: f64,
    pub expectancy_r: f64,
    pub max_drawdown_r: f64,
    pub total_r: f64,
    #[serde(default)]
    pub items: Vec<OutcomeDetail>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
pub struct RecentOutcomeFilters {
    pub symbol: Option<String>,
    pub version: Option<String>,
    pub direction: Option<String>,
    pub level: Option<String>,
    pub grade: Option<String>,
    pub score_min: Option<f64>,
    pub score_max: Option<f64>,
    pub outcome: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ReviewSignalDetail {
    pub event_id: i64,
    pub symbol: String,
    pub name: String,
    pub timeframe: String,
    pub direction: String,
    pub level: String,
    pub grade: String,
    pub score: f64,
    pub entry_price: f64,
    pub stop_price: f64,
    pub target_price: f64,
    pub warning_ts: String,
    pub trigger_bar_ts: Option<String>,
    pub trigger_price: Option<f64>,
    pub exit_bar_ts: Option<String>,
    pub exit_price: Option<f64>,
    pub outcome: Option<String>,
    pub r_multiple: Option<f64>,
    pub created_at: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct SignalAnnotationDto {
    pub id: i64,
    pub event_id: i64,
    pub content: String,
    pub created_at: String,
    pub updated_at: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct SignalDecisionDto {
    pub event_id: i64,
    pub opened: bool,
    pub updated_at: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct SignalUserData {
    pub annotations: Vec<SignalAnnotationDto>,
    pub decision: Option<SignalDecisionDto>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct SchedulerStatus {
    pub running: bool,
    pub last_refresh: Option<String>,
    pub last_scan: Option<String>,
    pub active_data_source: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct AppInfo {
    pub name: String,
    pub version: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct MetaDto {
    pub api_version: String,
    pub server_version: String,
    pub min_client_version: String,
    pub server_time: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ServerStatusDto {
    pub tq_available: bool,
    pub db_available: bool,
    pub last_refresh: Option<String>,
    pub last_scan: Option<String>,
    pub quote_delay_ms: Option<u64>,
    pub symbol_count: usize,
    pub uptime_secs: u64,
    pub active_connections: usize,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct NotificationSignal {
    pub code: String,
    pub name: String,
    pub direction: String,
    pub level: String,
    pub grade: String,
    pub score: f64,
    pub entry: f64,
    pub stop: f64,
    pub target: f64,
    pub time: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct NotificationSingleBar {
    pub symbol: String,
    pub name: String,
    pub label: String,
    pub kind: String,
    pub time: String,
    pub price: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct NotificationEntryTrigger {
    pub symbol: String,
    pub name: String,
    pub direction: String,
    pub entry: f64,
    pub latest: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct NotificationHistoryItem {
    pub id: u64,
    pub created_at: String,
    pub kind: String,
    pub title: Option<String>,
    pub content: String,
    pub signal: Option<NotificationSignal>,
    pub entry_trigger: Option<NotificationEntryTrigger>,
    pub single_bar: Option<NotificationSingleBar>,
    pub manual_level: Option<ManualLevelAlert>,
    #[serde(default)]
    pub read_at: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct NewNotificationHistoryItem {
    pub kind: String,
    pub title: Option<String>,
    pub content: String,
    pub signal: Option<NotificationSignal>,
    pub entry_trigger: Option<NotificationEntryTrigger>,
    #[serde(default)]
    pub single_bar: Option<NotificationSingleBar>,
    #[serde(default)]
    pub manual_level: Option<ManualLevelAlert>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct V2ModelRow {
    pub model_id: String,
    pub name: String,
    pub status: String,
    pub is_champion: bool,
    pub accuracy: f64,
    pub precision: f64,
    pub recall: f64,
    pub f1: f64,
    pub created_at: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct V2PredictionRow {
    pub event_id: i64,
    pub model_id: String,
    pub p_win: f64,
    pub pred_label: i64,
    pub created_at: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct V2ReportBundle {
    pub summary: serde_json::Value,
}
