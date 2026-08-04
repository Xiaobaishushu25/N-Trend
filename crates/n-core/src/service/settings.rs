//! 应用设置：DB 持久化 + 默认值 + 首次运行兼容导入 email.toml。

use std::collections::HashMap;
use std::path::Path;

use anyhow::{Context, Result};
use sea_orm::DatabaseConnection;
use serde::{Deserialize, Serialize};

use crate::notify::email::EmailSettings;
use crate::storage::repo;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Settings {
    pub refresh_interval_secs: u64,
    pub scan_interval_secs: u64,
    pub trading_only: bool,
    pub request_interval_ms: u64,
    pub minutely_budget: usize,
    pub backfill_count: usize,
    pub incremental_count: usize,
    pub auto_start_scheduler: bool,
    pub log_level: String,
    pub email: EmailSettings,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            refresh_interval_secs: 300,
            scan_interval_secs: 900,
            trading_only: true,
            request_interval_ms: 400,
            minutely_budget: 60,
            backfill_count: 1000,
            incremental_count: 10,
            auto_start_scheduler: true,
            log_level: "info".to_string(),
            email: EmailSettings::default(),
        }
    }
}

impl Settings {
    pub async fn load(db: &DatabaseConnection) -> Result<Self> {
        let map = repo::all_settings(db).await?;
        let d = Settings::default();
        Ok(Settings {
            refresh_interval_secs: get_u64(&map, "refresh_interval_secs", d.refresh_interval_secs),
            scan_interval_secs: get_u64(&map, "scan_interval_secs", d.scan_interval_secs),
            trading_only: get_bool(&map, "trading_only", d.trading_only),
            request_interval_ms: get_u64(&map, "request_interval_ms", d.request_interval_ms),
            minutely_budget: get_usize(&map, "minutely_budget", d.minutely_budget),
            backfill_count: get_usize(&map, "backfill_count", d.backfill_count),
            incremental_count: get_usize(&map, "incremental_count", d.incremental_count),
            auto_start_scheduler: get_bool(&map, "auto_start_scheduler", d.auto_start_scheduler),
            log_level: get_str(&map, "log_level", &d.log_level),
            email: EmailSettings {
                enabled: get_bool(&map, "email.enabled", d.email.enabled),
                to: get_str(&map, "email.to", &d.email.to),
                from: get_str(&map, "email.from", &d.email.from),
                smtp_host: get_str(&map, "email.smtp_host", &d.email.smtp_host),
                smtp_port: get_u16(&map, "email.smtp_port", d.email.smtp_port),
                smtp_user: get_str(&map, "email.smtp_user", &d.email.smtp_user),
                smtp_password: get_str(&map, "email.smtp_password", &d.email.smtp_password),
            },
        })
    }

    pub async fn save(&self, db: &DatabaseConnection) -> Result<()> {
        let mut map = HashMap::new();
        put(&mut map, "refresh_interval_secs", &self.refresh_interval_secs.to_string());
        put(&mut map, "scan_interval_secs", &self.scan_interval_secs.to_string());
        put(&mut map, "trading_only", &self.trading_only.to_string());
        put(&mut map, "request_interval_ms", &self.request_interval_ms.to_string());
        put(&mut map, "minutely_budget", &self.minutely_budget.to_string());
        put(&mut map, "backfill_count", &self.backfill_count.to_string());
        put(&mut map, "incremental_count", &self.incremental_count.to_string());
        put(&mut map, "auto_start_scheduler", &self.auto_start_scheduler.to_string());
        put(&mut map, "log_level", &self.log_level);
        put(&mut map, "email.enabled", &self.email.enabled.to_string());
        put(&mut map, "email.to", &self.email.to);
        put(&mut map, "email.from", &self.email.from);
        put(&mut map, "email.smtp_host", &self.email.smtp_host);
        put(&mut map, "email.smtp_port", &self.email.smtp_port.to_string());
        put(&mut map, "email.smtp_user", &self.email.smtp_user);
        put(&mut map, "email.smtp_password", &self.email.smtp_password);
        repo::set_settings(db, &map).await
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

fn put(map: &mut HashMap<String, String>, key: &str, value: &str) {
    map.insert(key.to_string(), value.to_string());
}

fn get_str(map: &HashMap<String, String>, key: &str, default: &str) -> String {
    map.get(key).cloned().unwrap_or_else(|| default.to_string())
}

fn get_bool(map: &HashMap<String, String>, key: &str, default: bool) -> bool {
    map.get(key)
        .and_then(|v| v.parse().ok())
        .unwrap_or(default)
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn imports_email_toml_defaults_when_missing() {
        let s = import_email_toml(Path::new("__missing_email.toml")).unwrap();
        assert_eq!(s.to, EmailSettings::default().to);
    }
}
