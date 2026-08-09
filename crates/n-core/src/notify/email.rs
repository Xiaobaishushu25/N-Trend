//! 邮件通知（SMTP）。桌面通知由前端监听信号事件后弹出，不在此模块。

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};

pub const DEFAULT_RECIPIENT: &str = "2055761346@qq.com";

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct EmailSettings {
    pub enabled: bool,
    pub to: String,
    pub from: String,
    pub smtp_host: String,
    pub smtp_port: u16,
    pub smtp_user: String,
    pub smtp_password: String,
}

impl Default for EmailSettings {
    fn default() -> Self {
        Self {
            enabled: true,
            to: DEFAULT_RECIPIENT.to_string(),
            from: String::new(),
            smtp_host: "smtp.qq.com".to_string(),
            smtp_port: 465,
            smtp_user: String::new(),
            smtp_password: String::new(),
        }
    }
}

impl EmailSettings {
    /// 是否具备实际发送条件（有授权码且非占位符）。
    pub fn sendable(&self) -> bool {
        !self.smtp_password.trim().is_empty()
            && !self.smtp_password.contains("在此填写")
            && !self.from.trim().is_empty()
            && !self.to.trim().is_empty()
    }
}

/// 发送扫描摘要邮件；未启用或未配置授权码时静默跳过并返回 Ok。
pub fn send_summary(subject: &str, body: &str, s: &EmailSettings) -> Result<()> {
    if !s.enabled {
        return Ok(());
    }
    if !s.sendable() {
        tracing::warn!("邮件未配置 SMTP 授权码/发件人，已跳过发送");
        return Ok(());
    }

    use lettre::message::Mailbox;
    use lettre::transport::smtp::authentication::Credentials;
    use lettre::transport::smtp::client::{Tls, TlsParameters};
    use lettre::{Message, SmtpTransport, Transport};

    let from = s.from.parse::<Mailbox>().context("发件人邮箱格式错误")?;
    let mut builder = Message::builder().from(from).subject(subject.to_string());
    for address in s.to.split(',').map(str::trim).filter(|a| !a.is_empty()) {
        let mailbox = address
            .parse::<Mailbox>()
            .with_context(|| format!("收件人邮箱格式错误 '{address}'"))?;
        builder = builder.to(mailbox);
    }
    let email = builder.body(body.to_string()).context("构造邮件内容失败")?;

    let tls_params = TlsParameters::new(s.smtp_host.clone())
        .with_context(|| format!("创建TLS参数失败: {}", s.smtp_host))?;
    let tls = match s.smtp_port {
        465 => Tls::Wrapper(tls_params),
        25 => Tls::None,
        _ => Tls::Required(tls_params),
    };
    let mailer = SmtpTransport::builder_dangerous(&s.smtp_host)
        .port(s.smtp_port)
        .tls(tls)
        .credentials(Credentials::new(
            s.smtp_user.clone(),
            s.smtp_password.clone(),
        ))
        .build();

    mailer
        .send(&email)
        .with_context(|| format!("SMTP发送失败: {}", s.to))?;
    tracing::info!("已发送综合结论邮件: {} -> {}", s.from, s.to);
    Ok(())
}

/// 生成扫描邮件的主题与正文（纯文本）。
pub fn scan_email_payload(summary: &str) -> (String, String) {
    let subject = format!("N趋势扫描 {}", crate::analyze::time::now_minute());
    (subject, summary.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_settings_not_sendable_without_password() {
        let s = EmailSettings::default();
        assert!(s.enabled);
        assert!(!s.sendable());
    }

    #[test]
    fn sendable_requires_password_and_addresses() {
        let mut s = EmailSettings::default();
        s.smtp_password = "authcode".to_string();
        s.from = "a@b.com".to_string();
        s.to = "c@d.com".to_string();
        assert!(s.sendable());
    }
}
