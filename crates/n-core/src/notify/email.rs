//! 邮件通知（SMTP）。桌面通知由前端监听信号事件后弹出，不在此模块。

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};

use crate::storage::entities::pattern_events;

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

/// 信号事件邮件的类型：预警刚识别，或触发刚命中。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EventEmailKind {
    Warning,
    Trigger,
}

fn event_dir_label(e: &pattern_events::Model) -> &'static str {
    if e.direction == "up" {
        "做多"
    } else {
        "做空"
    }
}

fn event_warning_label(e: &pattern_events::Model) -> String {
    match e.warning_kind.as_str() {
        "strong" => "强反转".to_string(),
        "engulf" => "强反转".to_string(),
        "wick" => {
            if e.direction == "up" {
                "长下影线".to_string()
            } else {
                "长上影线".to_string()
            }
        }
        // 历史记录兼容；新扫描不再产生 fast。
        "fast" => "快速反转".to_string(),
        "cumulative" => "累积反转".to_string(),
        other => other.to_string(),
    }
}

fn event_entry_note(e: &pattern_events::Model) -> String {
    if e.direction == "up" {
        "预警高点 + 1 tick".to_string()
    } else {
        "预警低点 - 1 tick".to_string()
    }
}

fn event_stop_note(e: &pattern_events::Model) -> String {
    if e.direction == "up" {
        "b段低点下方".to_string()
    } else {
        "b段高点上方".to_string()
    }
}

fn event_target_note(e: &pattern_events::Model) -> String {
    if e.direction == "up" {
        "a段高点附近".to_string()
    } else {
        "a段低点附近".to_string()
    }
}

fn event_dims(e: &pattern_events::Model) -> (Option<f64>, Option<f64>, Option<f64>) {
    let Ok(v) = serde_json::from_str::<serde_json::Value>(&e.entry_score_dims) else {
        return (None, None, None);
    };
    (
        v.get("dim_a").and_then(|x| x.as_f64()),
        v.get("dim_b").and_then(|x| x.as_f64()),
        v.get("dim_warning").and_then(|x| x.as_f64()),
    )
}

fn fmt_opt_f64(v: Option<f64>) -> String {
    match v {
        Some(x) => format!("{x:.2}"),
        None => "—".to_string(),
    }
}

/// 生成单条信号事件的邮件主题与正文（纯文本），预警与触发共用一套字段。
pub fn event_email_payload(kind: EventEmailKind, e: &pattern_events::Model) -> (String, String) {
    let kind_label = match kind {
        EventEmailKind::Warning => "预警",
        EventEmailKind::Trigger => "触发",
    };
    let subject = format!(
        "N趋势{kind_label} [{symbol}] {dir} {grade} {score:.2}分",
        symbol = e.symbol,
        dir = event_dir_label(e),
        grade = e.grade,
        score = e.entry_score,
    );

    let (dim_a, dim_b, dim_w) = event_dims(e);
    let dims_text = format!(
        "（A {} / B {} / 预警 {}）",
        fmt_opt_f64(dim_a),
        fmt_opt_f64(dim_b),
        fmt_opt_f64(dim_w),
    );
    let state_label = match e.state.as_str() {
        "pending" => "等待触发",
        "triggered" => "已触发",
        "expired" => "已失效",
        "closed" => "已平仓",
        other => other,
    };
    let trigger_condition = if e.direction == "up" {
        format!("实时价格突破 {}", e.entry)
    } else {
        format!("实时价格跌破 {}", e.entry)
    };

    let mut body = format!(
        "预警时间：{}（15m收盘）\n\
         品种：{} | 方向：{} | 等级：{}\n\
         形态：{}（{}）\n\
         入场评分：{:.2}{}\n\
         入场价：{:.1}（{}）\n\
         止损：{:.1}（{}）\n\
         目标：{:.1}（{}）\n\
         RR：{:.2}\n\
         状态：{}\n\
         触发条件：{}",
        e.warning_ts,
        e.symbol,
        event_dir_label(e),
        e.grade,
        event_warning_label(e),
        e.warning_kind,
        e.entry_score,
        dims_text,
        e.entry,
        event_entry_note(e),
        e.stop,
        event_stop_note(e),
        e.target,
        event_target_note(e),
        e.rr,
        state_label,
        trigger_condition,
    );

    if kind == EventEmailKind::Trigger {
        body.push_str(&format!(
            "\n触发时间：{}\n\
             触发价：{}\n\
             追价深度：{}R\n\
             触发K线质量：{}\n\
             当前持仓评分：{}",
            e.trigger_ts.as_deref().unwrap_or("—"),
            e.trigger_price
                .map(|p| format!("{p:.1}"))
                .unwrap_or_else(|| "—".to_string()),
            fmt_opt_f64(e.overshoot_r),
            fmt_opt_f64(e.trigger_score),
            fmt_opt_f64(e.hold_score),
        ));
    }

    (subject, body)
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

    #[test]
    fn event_email_payload_contains_required_fields() {
        let mut e = pattern_events::Model {
            id: 1,
            symbol: "BU0".to_string(),
            direction: "up".to_string(),
            grade: "A级".to_string(),
            level: "fine".to_string(),
            s0_ts: "2026-08-14 09:15".to_string(),
            s0_price: 4128.0,
            s1_ts: "2026-08-14 09:30".to_string(),
            s1_price: 4150.0,
            s2_ts: "2026-08-14 09:45".to_string(),
            s2_price: 4137.0,
            a_move: 22.0,
            b_move: 13.0,
            a_bars: 1,
            b_bars: 1,
            retracement: 0.59,
            warning_ts: "2026-08-14 11:30".to_string(),
            detected_at: "2026-08-14 11:30".to_string(),
            warning_kind: "wick".to_string(),
            entry_score: 3.6,
            entry_score_dims: r#"{"dim_a":3.8,"dim_b":3.4,"dim_warning":3.5}"#.to_string(),
            entry: 4216.0,
            stop: 4152.0,
            target: 4298.0,
            risk: 64.0,
            rr: 1.28,
            state: "pending".to_string(),
            last_advance_ts: None,
            trigger_ts: None,
            trigger_bar_ts: None,
            trigger_price: None,
            trigger_score: None,
            trigger_volume_ratio: None,
            overshoot_r: None,
            hold_score: None,
            hold_score_history: "[]".to_string(),
            outcome: None,
            exit_reason: None,
            exit_ts: None,
            exit_price: None,
            r_multiple: None,
            mfe_r: None,
            mae_r: None,
            created_at: "2026-08-14 11:30".to_string(),
            updated_at: "2026-08-14 11:30".to_string(),
        };

        let (subject, body) = event_email_payload(EventEmailKind::Warning, &e);
        assert!(subject.contains("预警"));
        assert!(body.contains("预警时间：2026-08-14 11:30"));
        assert!(body.contains("品种：BU0"));
        assert!(body.contains("方向：做多"));
        assert!(body.contains("等级：A级"));
        assert!(body.contains("形态：长下影线"));
        assert!(body.contains("入场评分：3.60"));
        assert!(body.contains("A 3.80"));
        assert!(body.contains("入场价：4216.0"));
        assert!(body.contains("止损：4152.0"));
        assert!(body.contains("目标：4298.0"));
        assert!(body.contains("等待触发"));
        assert!(body.contains("实时价格突破 4216"));

        e.state = "triggered".to_string();
        e.trigger_ts = Some("2026-08-14 13:45".to_string());
        e.trigger_price = Some(4217.0);
        e.trigger_score = Some(3.9);
        e.overshoot_r = Some(0.02);
        e.hold_score = Some(4.1);
        let (_, body) = event_email_payload(EventEmailKind::Trigger, &e);
        assert!(body.contains("触发时间：2026-08-14 13:45"));
        assert!(body.contains("触发价：4217.0"));
        assert!(body.contains("追价深度：0.02R"));
        assert!(body.contains("触发K线质量：3.90"));
        assert!(body.contains("当前持仓评分：4.10"));
    }
}
