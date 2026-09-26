use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct AppConfigDto {
    #[serde(alias = "autoStartScheduler")]
    pub auto_start_scheduler: bool,
    #[serde(alias = "logicVersion")]
    pub logic_version: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct SchedulerConfigDto {
    #[serde(alias = "refreshIntervalSecs")]
    pub refresh_interval_secs: u64,
    #[serde(alias = "scanIntervalSecs")]
    pub scan_interval_secs: u64,
    #[serde(alias = "tradingOnly")]
    pub trading_only: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct FetchConfigDto {
    #[serde(alias = "requestIntervalMs")]
    pub request_interval_ms: u64,
    #[serde(alias = "minutelyBudget")]
    pub minutely_budget: usize,
    #[serde(alias = "backfillCount")]
    pub backfill_count: usize,
    #[serde(alias = "incrementalCount")]
    pub incremental_count: usize,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct QuoteConfigDto {
    #[serde(alias = "pollIntervalMs")]
    pub poll_interval_ms: u64,
    #[serde(alias = "requestIntervalMs")]
    pub request_interval_ms: u64,
    #[serde(alias = "minutelyBudget")]
    pub minutely_budget: usize,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct NotifyConfigDto {
    #[serde(alias = "inAppNewPattern")]
    pub in_app_new_pattern: bool,
    #[serde(alias = "newPatternMinScore")]
    pub new_pattern_min_score: f64,
    #[serde(alias = "inAppEntryTrigger")]
    pub in_app_entry_trigger: bool,
    #[serde(alias = "systemEntryTrigger")]
    pub system_entry_trigger: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct PrecloseConfigDto {
    #[serde(alias = "schemaVersion")]
    pub schema_version: u32,
    pub enabled: bool,
    #[serde(alias = "leadSecs")]
    pub lead_secs: u64,
    #[serde(alias = "horizonMinutes")]
    pub horizon_minutes: u64,
    #[serde(alias = "inAppNotify")]
    pub in_app_notify: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct LogConfigDto {
    pub level: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct DataSourceConfigDto {
    #[serde(alias = "primarySource")]
    pub primary_source: String,
    #[serde(alias = "fallbackEnabled")]
    pub fallback_enabled: bool,
    #[serde(alias = "tqAccount")]
    pub tq_account: String,
    #[serde(default, alias = "tqPasswordConfigured")]
    pub tq_password_configured: bool,
    #[serde(alias = "bridgePort")]
    pub bridge_port: u16,
    #[serde(alias = "autoSpawnBridge")]
    pub auto_spawn_bridge: bool,
    #[serde(alias = "pythonPath")]
    pub python_path: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct EmailSettingsDto {
    pub enabled: bool,
    pub to: String,
    pub from: String,
    #[serde(alias = "smtpHost")]
    pub smtp_host: String,
    #[serde(alias = "smtpPort")]
    pub smtp_port: u16,
    #[serde(alias = "smtpUser")]
    pub smtp_user: String,
    #[serde(default, alias = "smtpPasswordConfigured")]
    pub smtp_password_configured: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct ServerSettingsDto {
    #[serde(alias = "config_revision")]
    pub config_revision: i64,
    #[serde(alias = "app_config")]
    pub app_config: AppConfigDto,
    pub scheduler: SchedulerConfigDto,
    pub fetch: FetchConfigDto,
    pub quote: QuoteConfigDto,
    pub notify: NotifyConfigDto,
    pub preclose: PrecloseConfigDto,
    pub log: LogConfigDto,
    #[serde(alias = "data_source")]
    pub data_source: DataSourceConfigDto,
    pub email: EmailSettingsDto,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct ServerSettingsUpdate {
    #[serde(alias = "config_revision")]
    pub config_revision: i64,
    #[serde(default, alias = "app_config")]
    pub app_config: Option<AppConfigDto>,
    #[serde(default)]
    pub scheduler: Option<SchedulerConfigDto>,
    #[serde(default)]
    pub fetch: Option<FetchConfigDto>,
    #[serde(default)]
    pub quote: Option<QuoteConfigDto>,
    #[serde(default)]
    pub notify: Option<NotifyConfigDto>,
    #[serde(default)]
    pub preclose: Option<PrecloseConfigDto>,
    #[serde(default)]
    pub log: Option<LogConfigDto>,
    #[serde(default, alias = "data_source")]
    pub data_source: Option<DataSourceUpdateDto>,
    #[serde(default)]
    pub email: Option<EmailSettingsUpdateDto>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct DataSourceUpdateDto {
    #[serde(alias = "primarySource")]
    pub primary_source: String,
    #[serde(alias = "fallbackEnabled")]
    pub fallback_enabled: bool,
    #[serde(alias = "tqAccount")]
    pub tq_account: String,
    #[serde(default, alias = "tqPassword")]
    pub tq_password: Option<String>,
    #[serde(alias = "bridgePort")]
    pub bridge_port: u16,
    #[serde(alias = "autoSpawnBridge")]
    pub auto_spawn_bridge: bool,
    #[serde(alias = "pythonPath")]
    pub python_path: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct EmailSettingsUpdateDto {
    pub enabled: bool,
    pub to: String,
    pub from: String,
    #[serde(alias = "smtpHost")]
    pub smtp_host: String,
    #[serde(alias = "smtpPort")]
    pub smtp_port: u16,
    #[serde(alias = "smtpUser")]
    pub smtp_user: String,
    #[serde(default, alias = "smtpPassword")]
    pub smtp_password: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct ConfigApplyResult {
    pub applied: Vec<String>,
    pub restart_required: Vec<String>,
    pub rejected: Vec<String>,
    pub new_revision: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct ClientLocalSettings {
    #[serde(alias = "server_url")]
    pub server_url: String,
    #[serde(alias = "device_id")]
    pub device_id: String,
    #[serde(alias = "device_name")]
    pub device_name: String,
    pub theme: String,
    #[serde(alias = "chart_display_bars")]
    pub chart_display_bars: usize,
    #[serde(alias = "chart_right_gap")]
    pub chart_right_gap: usize,
    #[serde(alias = "min_bar_spacing")]
    pub min_bar_spacing: f64,
    pub timeframes: Vec<String>,
    #[serde(alias = "last_group_id")]
    pub last_group_id: Option<i64>,
    #[serde(alias = "desktop_notification_enabled")]
    pub desktop_notification_enabled: bool,
    #[serde(alias = "backup_dir")]
    pub backup_dir: Option<String>,
    #[serde(alias = "last_backup_date")]
    pub last_backup_date: Option<String>,
}

impl Default for ClientLocalSettings {
    fn default() -> Self {
        Self {
            server_url: "http://127.0.0.1:8081".to_string(),
            device_id: String::new(),
            device_name: "Desktop PC".to_string(),
            theme: "dark".to_string(),
            chart_display_bars: 200,
            chart_right_gap: 15,
            min_bar_spacing: 6.0,
            timeframes: vec![
                "5m".to_string(),
                "15m".to_string(),
                "30m".to_string(),
                "1h".to_string(),
                "2h".to_string(),
                "4h".to_string(),
                "1d".to_string(),
            ],
            last_group_id: None,
            desktop_notification_enabled: true,
            backup_dir: None,
            last_backup_date: None,
        }
    }
}
