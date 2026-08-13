//! 应用配置：JSON 文件持久化 + 默认值 + 旧版 DB 设置迁移。
//!
//! 配置只存于 `<app_data_dir>/config.json` 单个文件，不写入数据库；
//! 数据库 settings 表仅保留调度/补全等运行时键。

use std::collections::HashMap;
use std::fs::OpenOptions;
use std::io::Write;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use sea_orm::DatabaseConnection;
use serde::{Deserialize, Serialize};

use crate::notify::email::EmailSettings;
use crate::scheduler::SchedulerConfig;
use crate::storage::repo;

/// 全部支持的K线周期（顺序即展示顺序）。
pub const DEFAULT_TIMEFRAMES: [&str; 7] = ["5m", "15m", "30m", "60m", "120m", "240m", "1d"];

/// 旧版 DB 设置表中属于“配置”的键；迁移成功后整体删除，仅保留运行时键。
const LEGACY_KEYS: [&str; 16] = [
    "refresh_interval_secs",
    "scan_interval_secs",
    "trading_only",
    "request_interval_ms",
    "minutely_budget",
    "backfill_count",
    "incremental_count",
    "auto_start_scheduler",
    "log_level",
    "email.enabled",
    "email.to",
    "email.from",
    "email.smtp_host",
    "email.smtp_port",
    "email.smtp_user",
    "email.smtp_password",
];

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct Config {
    /// 应用级开关（启动行为）
    pub app_config: AppConfig,
    /// 定时任务（复用调度器配置结构）
    pub scheduler: SchedulerConfig,
    /// 数据抓取限速与回填
    pub fetch: FetchConfig,
    /// 实时行情轮询与限速
    pub quote: QuoteConfig,
    /// 邮件通知
    pub email: EmailSettings,
    /// 通知开关
    pub notify: NotifyConfig,
    /// 日志
    pub log: LogConfig,
    /// 界面细节
    pub ui: UiConfig,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            app_config: AppConfig::default(),
            scheduler: SchedulerConfig::default(),
            fetch: FetchConfig::default(),
            quote: QuoteConfig::default(),
            email: EmailSettings::default(),
            notify: NotifyConfig::default(),
            log: LogConfig::default(),
            ui: UiConfig::default(),
        }
    }
}

impl Config {
    /// 加载配置：优先读 JSON 文件；缺失/损坏时从旧版 DB 设置迁移并持久化。
    ///
    /// 损坏文件先改名为 `config.json.bak` 再重建；迁移成功后才删除旧配置键，
    /// 失败时旧键保留，保证可回退。
    pub async fn load(path: &Path, db: &DatabaseConnection) -> Result<Self> {
        if path.exists() {
            match std::fs::read_to_string(path) {
                Ok(text) => match serde_json::from_str::<Config>(&text) {
                    Ok(config) => return Ok(config),
                    Err(e) => {
                        let bak = backup_path(path);
                        tracing::warn!("配置文件解析失败({e})，已备份到 {}", bak.display());
                        let _ = std::fs::rename(path, &bak);
                    }
                },
                Err(e) => {
                    tracing::warn!("读取配置文件失败({e})，尝试重建");
                }
            }
        }
        let legacy = repo::all_settings(db).await?;
        let config = Config::from_legacy_map(&legacy);
        config.save(path)?;
        let keys: Vec<String> = LEGACY_KEYS.iter().map(|s| s.to_string()).collect();
        repo::delete_settings(db, &keys).await?;
        Ok(config)
    }

    /// 写 JSON 配置文件（缺失目录自动创建；内容小，写入频率低）。
    pub fn save(&self, path: &Path) -> Result<()> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)
                .with_context(|| format!("创建配置目录失败: {}", parent.display()))?;
        }
        let mut file = OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .open(path)
            .with_context(|| format!("打开配置文件失败: {}", path.display()))?;
        let json = serde_json::to_string_pretty(self).context("序列化配置失败")?;
        file.write_all(json.as_bytes())
            .with_context(|| format!("写入配置文件失败: {}", path.display()))?;
        Ok(())
    }

    /// 由旧版 DB settings 表构建配置（缺失键用默认值）。
    fn from_legacy_map(map: &HashMap<String, String>) -> Self {
        let d = Config::default();
        Config {
            app_config: AppConfig {
                logic_version: d.app_config.logic_version.clone(),
                auto_start_scheduler: get_bool(
                    map,
                    "auto_start_scheduler",
                    d.app_config.auto_start_scheduler,
                ),
            },
            scheduler: SchedulerConfig {
                refresh_interval_secs: get_u64(
                    map,
                    "refresh_interval_secs",
                    d.scheduler.refresh_interval_secs,
                ),
                scan_interval_secs: get_u64(
                    map,
                    "scan_interval_secs",
                    d.scheduler.scan_interval_secs,
                ),
                trading_only: get_bool(map, "trading_only", d.scheduler.trading_only),
            },
            fetch: FetchConfig {
                request_interval_ms: get_u64(
                    map,
                    "request_interval_ms",
                    d.fetch.request_interval_ms,
                ),
                minutely_budget: get_usize(map, "minutely_budget", d.fetch.minutely_budget),
                backfill_count: get_usize(map, "backfill_count", d.fetch.backfill_count),
                incremental_count: get_usize(map, "incremental_count", d.fetch.incremental_count),
            },
            quote: QuoteConfig::default(),
            email: EmailSettings {
                enabled: get_bool(map, "email.enabled", d.email.enabled),
                to: get_str(map, "email.to", &d.email.to),
                from: get_str(map, "email.from", &d.email.from),
                smtp_host: get_str(map, "email.smtp_host", &d.email.smtp_host),
                smtp_port: get_u16(map, "email.smtp_port", d.email.smtp_port),
                smtp_user: get_str(map, "email.smtp_user", &d.email.smtp_user),
                smtp_password: get_str(map, "email.smtp_password", &d.email.smtp_password),
            },
            notify: NotifyConfig::default(),
            log: LogConfig {
                level: get_str(map, "log_level", &d.log.level),
            },
            ui: UiConfig::default(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct NotifyConfig {
    /// 局内新形态通知：扫描发现新的即将触发形态时弹卡片通知
    pub in_app_new_pattern: bool,
    /// 新形态通知的最低形态评分：低于该阈值的即将触发形态不提醒
    pub new_pattern_min_score: f64,
    /// 局内触发价通知：实时行情触及形态入场价时弹右下角通知（持久，需手动关闭）
    pub in_app_entry_trigger: bool,
    /// 系统级触发价通知：入场价提醒同时发送系统通知
    pub system_entry_trigger: bool,
}

impl Default for NotifyConfig {
    fn default() -> Self {
        Self {
            in_app_new_pattern: true,
            new_pattern_min_score: 0.0,
            in_app_entry_trigger: true,
            system_entry_trigger: false,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct AppConfig {
    /// 应用启动时自动运行定时任务
    pub auto_start_scheduler: bool,
    /// 交易信号分析版本：1 = 保留原逻辑（默认），2 = 严格N字 + 箱体
    #[serde(default = "default_logic_version")]
    pub logic_version: String,
}

impl Default for AppConfig {
    fn default() -> Self {
        Self {
            auto_start_scheduler: true,
            logic_version: default_logic_version(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct FetchConfig {
    /// 单请求最小间隔（毫秒）
    pub request_interval_ms: u64,
    /// 每分钟请求预算
    pub minutely_budget: usize,
    /// 首抓/回补根数
    pub backfill_count: usize,
    /// 增量抓取根数
    pub incremental_count: usize,
}

impl Default for FetchConfig {
    fn default() -> Self {
        Self {
            request_interval_ms: 400,
            minutely_budget: 60,
            backfill_count: 1000,
            incremental_count: 10,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct QuoteConfig {
    /// 实时行情轮询间隔（毫秒）
    pub poll_interval_ms: u64,
    /// 实时行情单请求最小间隔（毫秒）
    pub request_interval_ms: u64,
    /// 实时行情每分钟请求预算
    pub minutely_budget: usize,
}

impl Default for QuoteConfig {
    fn default() -> Self {
        Self {
            poll_interval_ms: 3000,
            request_interval_ms: 200,
            minutely_budget: 120,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct LogConfig {
    /// 日志级别：trace/debug/info/warn/error（重启后生效）
    pub level: String,
}

impl Default for LogConfig {
    fn default() -> Self {
        Self {
            level: "info".to_string(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct UiConfig {
    /// 自选表格行情跳动闪烁时长（毫秒）
    pub flash_ms: u64,
    /// 顶栏呼吸灯保持时长（毫秒）
    pub breathe_hold_ms: u64,
    /// K线图最小间距（像素）
    pub min_bar_spacing: u64,
    /// 点击进入K线图时默认展示的K线根数（从最新一根往前数）
    pub chart_display_bars: u64,
    /// K线图默认向左移动距离（根），即默认视图右侧留出的空白上限
    pub chart_right_gap: u64,
    /// 启用的K线周期（空时按全部处理）
    pub timeframes: Vec<String>,
    /// 上次打开的分组表格（null=全部品种），应用启动后恢复
    pub last_group_id: Option<i64>,
}

impl Default for UiConfig {
    fn default() -> Self {
        Self {
            flash_ms: 900,
            breathe_hold_ms: 5000,
            min_bar_spacing: 8,
            chart_display_bars: 140,
            chart_right_gap: 10,
            timeframes: DEFAULT_TIMEFRAMES.iter().map(|s| s.to_string()).collect(),
            last_group_id: None,
        }
    }
}

/// 首次运行兼容导入旧 email.toml（文件不存在时返回默认设置）。
pub fn import_email_toml(path: &Path) -> Result<EmailSettings> {
    if !path.exists() {
        return Ok(EmailSettings::default());
    }
    let text = std::fs::read_to_string(path)
        .with_context(|| format!("无法读取邮件配置 {}", path.display()))?;

    #[derive(Debug, Default, Deserialize)]
    #[serde(default)]
    struct FileConfig {
        smtp: FileSmtp,
        mail: FileMail,
    }
    #[derive(Debug, Default, Deserialize)]
    #[serde(default)]
    struct FileSmtp {
        host: String,
        port: Option<u16>,
        user: String,
        password: String,
        from: String,
    }
    #[derive(Debug, Default, Deserialize)]
    #[serde(default)]
    struct FileMail {
        to: Vec<String>,
        enabled: Option<bool>,
    }

    let cfg: FileConfig =
        toml::from_str(&text).with_context(|| format!("解析邮件配置失败 {}", path.display()))?;

    let mut s = EmailSettings::default();
    if !cfg.smtp.host.trim().is_empty() {
        s.smtp_host = cfg.smtp.host;
    }
    if let Some(port) = cfg.smtp.port {
        s.smtp_port = port;
    }
    let first_recipient = cfg
        .mail
        .to
        .first()
        .cloned()
        .unwrap_or_else(|| EmailSettings::default().to);
    s.smtp_user = if cfg.smtp.user.trim().is_empty() {
        first_recipient.clone()
    } else {
        cfg.smtp.user
    };
    s.smtp_password = cfg.smtp.password;
    s.from = if cfg.smtp.from.trim().is_empty() {
        s.smtp_user.clone()
    } else {
        cfg.smtp.from
    };
    if !cfg.mail.to.is_empty() {
        s.to = cfg.mail.to.join(",");
    }
    if let Some(enabled) = cfg.mail.enabled {
        s.enabled = enabled;
    }
    Ok(s)
}

fn backup_path(path: &Path) -> PathBuf {
    let mut name = path
        .file_name()
        .map(|s| s.to_os_string())
        .unwrap_or_default();
    name.push(".bak");
    path.with_file_name(name)
}

fn get_str(map: &HashMap<String, String>, key: &str, default: &str) -> String {
    map.get(key).cloned().unwrap_or_else(|| default.to_string())
}

fn get_bool(map: &HashMap<String, String>, key: &str, default: bool) -> bool {
    map.get(key).and_then(|v| v.parse().ok()).unwrap_or(default)
}

fn get_u64(map: &HashMap<String, String>, key: &str, default: u64) -> u64 {
    map.get(key).and_then(|v| v.parse().ok()).unwrap_or(default)
}

fn get_usize(map: &HashMap<String, String>, key: &str, default: usize) -> usize {
    map.get(key).and_then(|v| v.parse().ok()).unwrap_or(default)
}

fn get_u16(map: &HashMap<String, String>, key: &str, default: u16) -> u16 {
    map.get(key).and_then(|v| v.parse().ok()).unwrap_or(default)
}

fn default_logic_version() -> String {
    "1".to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_roundtrip_through_json() {
        let config = Config::default();
        let json = serde_json::to_string(&config).unwrap();
        let back: Config = serde_json::from_str(&json).unwrap();
        assert_eq!(back.app_config.auto_start_scheduler, true);
        assert_eq!(back.app_config.logic_version, "1");
        assert_eq!(back.scheduler.refresh_interval_secs, 300);
        assert_eq!(back.scheduler.scan_interval_secs, 900);
        assert_eq!(back.scheduler.trading_only, true);
        assert_eq!(back.fetch.request_interval_ms, 400);
        assert_eq!(back.fetch.minutely_budget, 60);
        assert_eq!(back.fetch.backfill_count, 1000);
        assert_eq!(back.fetch.incremental_count, 10);
        assert_eq!(back.quote.poll_interval_ms, 3000);
        assert_eq!(back.quote.request_interval_ms, 200);
        assert_eq!(back.quote.minutely_budget, 120);
        assert_eq!(back.notify.in_app_new_pattern, true);
        assert_eq!(back.notify.new_pattern_min_score, 0.0);
        assert_eq!(back.notify.in_app_entry_trigger, true);
        assert_eq!(back.notify.system_entry_trigger, false);
        assert_eq!(back.log.level, "info");
        assert_eq!(back.ui.flash_ms, 900);
        assert_eq!(back.ui.breathe_hold_ms, 5000);
        assert_eq!(back.ui.min_bar_spacing, 8);
        assert_eq!(back.ui.chart_display_bars, 140);
        assert_eq!(back.ui.chart_right_gap, 10);
        assert_eq!(
            back.ui.timeframes,
            DEFAULT_TIMEFRAMES
                .iter()
                .map(|s| s.to_string())
                .collect::<Vec<_>>()
        );
        assert!(back.ui.last_group_id.is_none());
    }

    #[test]
    fn empty_json_uses_defaults() {
        let config: Config = serde_json::from_str("{}").unwrap();
        assert_eq!(config.app_config.auto_start_scheduler, true);
        assert_eq!(config.app_config.logic_version, "1");
        assert_eq!(config.fetch.request_interval_ms, 400);
        assert_eq!(config.quote.poll_interval_ms, 3000);
        assert_eq!(config.log.level, "info");
        assert_eq!(config.notify.new_pattern_min_score, 0.0);
    }

    #[test]
    fn partial_json_keeps_unknown_fields_out() {
        let json = r#"{"scheduler":{"refresh_interval_secs":600}}"#;
        let config: Config = serde_json::from_str(json).unwrap();
        assert_eq!(config.scheduler.refresh_interval_secs, 600);
        assert_eq!(config.scheduler.scan_interval_secs, 900);
        assert_eq!(config.app_config.auto_start_scheduler, true);
    }

    #[test]
    fn notify_threshold_roundtrips_through_json() {
        let mut config = Config::default();
        config.notify.new_pattern_min_score = 3.5;
        let json = serde_json::to_string(&config).unwrap();
        let back: Config = serde_json::from_str(&json).unwrap();
        assert_eq!(back.notify.new_pattern_min_score, 3.5);
    }

    #[tokio::test]
    async fn migrates_legacy_db_settings_and_keeps_runtime_keys() {
        let db = crate::storage::connect(std::path::Path::new(":memory:"))
            .await
            .unwrap();
        let mut map = HashMap::new();
        map.insert("refresh_interval_secs".into(), "600".into());
        map.insert("scan_interval_secs".into(), "1800".into());
        map.insert("trading_only".into(), "false".into());
        map.insert("request_interval_ms".into(), "500".into());
        map.insert("minutely_budget".into(), "45".into());
        map.insert("backfill_count".into(), "500".into());
        map.insert("incremental_count".into(), "5".into());
        map.insert("auto_start_scheduler".into(), "false".into());
        map.insert("log_level".into(), "debug".into());
        map.insert("email.enabled".into(), "false".into());
        map.insert("email.to".into(), "a@b.com".into());
        map.insert("email.from".into(), "c@d.com".into());
        map.insert("email.smtp_host".into(), "smtp.test".into());
        map.insert("email.smtp_port".into(), "587".into());
        map.insert("email.smtp_user".into(), "user".into());
        map.insert("email.smtp_password".into(), "secret".into());
        // 运行时键：迁移后必须保留
        map.insert(
            "scheduler_last_refresh".into(),
            "2026-08-06 09:00:00".into(),
        );
        map.insert("names_enriched".into(), "1".into());
        repo::set_settings(&db, &map).await.unwrap();

        let dir =
            std::env::temp_dir().join(format!("ntrend-config-test-{}-migrate", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("config.json");

        let config = Config::load(&path, &db).await.unwrap();
        assert_eq!(config.scheduler.refresh_interval_secs, 600);
        assert_eq!(config.scheduler.scan_interval_secs, 1800);
        assert_eq!(config.scheduler.trading_only, false);
        assert_eq!(config.fetch.request_interval_ms, 500);
        assert_eq!(config.fetch.minutely_budget, 45);
        assert_eq!(config.fetch.backfill_count, 500);
        assert_eq!(config.fetch.incremental_count, 5);
        assert_eq!(config.app_config.auto_start_scheduler, false);
        assert_eq!(config.app_config.logic_version, "1");
        assert_eq!(config.log.level, "debug");
        assert_eq!(config.email.enabled, false);
        assert_eq!(config.email.to, "a@b.com");
        assert_eq!(config.email.smtp_password, "secret");
        assert_eq!(config.quote.poll_interval_ms, 3000);

        // 配置键已删除，运行时键保留
        let rest = repo::all_settings(&db).await.unwrap();
        assert!(!rest.contains_key("refresh_interval_secs"));
        assert!(!rest.contains_key("email.smtp_password"));
        assert_eq!(
            rest.get("scheduler_last_refresh").map(String::as_str),
            Some("2026-08-06 09:00:00")
        );
        assert_eq!(rest.get("names_enriched").map(String::as_str), Some("1"));
        assert!(path.exists());

        let _ = std::fs::remove_dir_all(&dir);
    }

    #[tokio::test]
    async fn corrupt_config_is_backed_up_and_recreated() {
        let db = crate::storage::connect(std::path::Path::new(":memory:"))
            .await
            .unwrap();
        let dir =
            std::env::temp_dir().join(format!("ntrend-config-test-{}-corrupt", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("config.json");
        std::fs::write(&path, "{ not json }").unwrap();

        let config = Config::load(&path, &db).await.unwrap();
        assert_eq!(config.log.level, "info");
        assert!(path.with_file_name("config.json.bak").exists());
        assert!(path.exists());

        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn imports_email_toml_defaults_when_missing() {
        let s = import_email_toml(Path::new("__missing_email.toml")).unwrap();
        assert_eq!(s.to, EmailSettings::default().to);
    }
}
