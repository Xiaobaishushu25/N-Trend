//! 邮件通知（SMTP）。桌面通知由前端监听信号事件后弹出，不在此模块。

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};

use crate::storage::entities::{pattern_events, preclose_candidates, preclose_signals};

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

    // 信号类通知
    pub signal_warning_enabled: bool,
    pub signal_trigger_enabled: bool,
    pub signal_preclose_enabled: bool,

    // 实盘类通知
    pub trade_open_enabled: bool,
    pub trade_close_enabled: bool,
    pub trade_instances: Vec<String>,
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
            signal_warning_enabled: true,
            signal_trigger_enabled: true,
            signal_preclose_enabled: true,
            trade_open_enabled: true,
            trade_close_enabled: true,
            trade_instances: vec!["default".to_string()],
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

    tracing::info!(
        "[SEND_SUMMARY] subject repr={:?} to={:?} host={}:{} len={}",
        subject,
        s.to,
        s.smtp_host,
        s.smtp_port,
        subject.len()
    );
    let from = s.from.parse::<Mailbox>().context("发件人邮箱格式错误")?;
    let mut builder = Message::builder().from(from).subject(subject.to_string());
    for address in s.to.split(',').map(str::trim).filter(|a| !a.is_empty()) {
        let mailbox = address
            .parse::<Mailbox>()
            .with_context(|| format!("收件人邮箱格式错误 '{address}'"))?;
        builder = builder.to(mailbox);
    }
    let email = builder.body(body.to_string()).context("构造邮件内容失败")?;
    if let Some(h) = email.headers().get_raw("Subject") {
        tracing::info!("[SEND_SUMMARY] built Subject header raw={:?}", h);
    } else {
        tracing::warn!("[SEND_SUMMARY] no Subject header after build");
    }

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
    event_email_payload_with_model(kind, e, None)
}

/// 触发邮件可附带冠军模型胜率；预警邮件传 None。
pub fn event_email_payload_with_model(
    kind: EventEmailKind,
    e: &pattern_events::Model,
    model_win_rate: Option<(&str, f64)>,
) -> (String, String) {
    let kind_label = match kind {
        EventEmailKind::Warning => "预警",
        EventEmailKind::Trigger => "触发",
    };
    let is_high_win_rate = matches!(kind, EventEmailKind::Trigger)
        && model_win_rate.map(|(_, p)| p >= 0.5).unwrap_or(false);
    let high_win_prefix = if is_high_win_rate {
        "【高胜率形态触发】"
    } else {
        ""
    };
    let subject = format!(
        "{high_win_prefix}N趋势{kind_label}【{symbol}】{dir} {grade} {score:.2}分",
        symbol = e.symbol,
        dir = event_dir_label(e),
        grade = e.grade,
        score = e.entry_score,
    );
    tracing::info!(
        "[MAIL_SUBJECT] kind={} symbol={} high_win_rate={} subject={}",
        kind_label,
        e.symbol,
        is_high_win_rate,
        subject
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
        match model_win_rate {
            Some((model_id, p_win)) => body.push_str(&format!(
                "\n冠军模型胜率：{:.1}%（{}）",
                p_win.clamp(0.0, 1.0) * 100.0,
                model_id
            )),
            None => body.push_str("\n冠军模型胜率：暂无可用结果"),
        }
    }

    (subject, body)
}

/// 收盘前预检测沿用正式信号模板，并补充预检测时点、预计收盘和参考现价。
pub fn preclose_email_payload(
    signal: &preclose_signals::Model,
    parent: &pattern_events::Model,
) -> (String, String) {
    let subject = format!(
        "N趋势收盘前预检测【{}】{} {} {:.2}分",
        signal.symbol,
        event_dir_label(parent),
        parent.grade,
        parent.entry_score,
    );
    let (_, event_body) = event_email_payload(EventEmailKind::Warning, parent);
    let body = format!(
        "预检测时间：{}\n预计收盘：{}\n参考现价：{:.1}\n来源正式候选：#{}\n观察窗口：{}分钟\n\n{}\n\n说明：这是收盘前预检测提醒，仅供提前关注；最终状态以收盘结算和正式触发为准。",
        signal.emitted_at,
        signal.session_close_ts,
        signal.reference_price,
        signal.parent_event_id,
        signal.horizon_minutes,
        event_body,
    );
    (subject, body)
}

/// 临时未收盘候选邮件：不依赖正式 pattern_event，明确标注为待收盘确认。
pub fn preclose_candidate_email_payload(
    candidate: &preclose_candidates::Model,
) -> (String, String) {
    let direction = if candidate.direction == "up" {
        "预做多"
    } else {
        "预做空"
    };
    let subject = format!(
        "N趋势临时未收盘扫描【{}】{} {} {:.2}分",
        candidate.symbol, direction, candidate.grade, candidate.entry_score,
    );
    let body = format!(
        "扫描时间：{}\n预计收盘：{}\n参考现价：{:.1}\n临时预警K线：{}\n形态方向：{} {}\n入场价：{:.1}\n止损价：{:.1}\n目标价：{:.1}\n风险收益比：{:.2}\n\n说明：这是基于未收盘15m K线的临时预判，不写入正式信号；收盘后的最终15m K线满足正式预警条件时才确认，否则失效。",
        candidate.emitted_at,
        candidate.session_close_ts,
        candidate.reference_price,
        candidate.warning_ts,
        direction,
        candidate.level,
        candidate.entry,
        candidate.stop,
        candidate.target,
        candidate.rr,
    );
    (subject, body)
}

pub fn single_bar_email_payload(alert: &crate::analyze::model::SingleBarAlert) -> (String, String) {
    let kind_label = if alert.kind == "hammer" { "锤" } else { "针" };
    let subject = format!(
        "N趋势{}·15m【{}】{}",
        kind_label, alert.symbol, alert.trigger_bar_ts
    );
    let body = format!(
        "品种：{}\n形态：{}·15m ({})\n触发K线：{}\n有效期至：{}\n收盘价：{:.1}\n最高：{:.1}  最低：{:.1}\n时间框架：15m\n\n说明：光头/光脚长影线，影线≥1.5倍实体且≥40%振幅且≥0.7ATR，反向影线≤5%振幅，实体≥25%振幅。下一根K线内有效。",
        alert.symbol,
        kind_label,
        alert.kind,
        alert.trigger_bar_ts,
        alert.expire_bar_ts,
        alert.price,
        alert.high,
        alert.low,
    );
    (subject, body)
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LiveTradeOpenNotice {
    pub instance_id: String,
    pub instance_name: String,
    pub symbol: String,
    pub instrument_id: String,
    pub signal_id: Option<i64>,
    pub direction: String, // "up" or "down"
    pub volume: i32,
    pub fill_price: f64,
    pub planned_entry_price: f64,
    pub slippage_pts: f64, // >0: 不利滑点, <0: 有利滑点
    pub slippage_amount: f64, // 金额（正数）
    pub target_price: f64,
    pub target_pts: f64,
    pub target_amount: f64,
    pub stop_price: f64,
    pub stop_pts: f64,
    pub stop_amount: f64,
    pub rr: f64,
    pub trade_time: String,
    pub account: Option<crate::trader::TraderAccount>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LiveTradeCloseNotice {
    pub instance_id: String,
    pub instance_name: String,
    pub symbol: String,
    pub instrument_id: String,
    pub signal_id: Option<i64>,
    pub direction: String, // "up" or "down"
    pub volume: i32,
    pub fill_price: f64, // entry fill price
    pub exit_price: f64, // exit fill price
    pub planned_exit_price: Option<f64>,
    pub exit_reason: String,
    pub slippage_pts: Option<f64>,
    pub slippage_amount: Option<f64>,
    pub realized_pnl_pts: f64,
    pub realized_pnl_amount: f64,
    pub planned_pnl_amount: Option<f64>,
    pub pnl_diff_amount: Option<f64>, // realized - planned
    pub r_multiple: f64,
    pub bars_held: i64,
    pub exit_ts: String,
    pub account: Option<crate::trader::TraderAccount>,
}

pub fn format_exit_reason(reason: &str) -> &'static str {
    match reason {
        "target" => "目标止盈 (Target 1.0R)",
        "stop" => "触发止损 (Stop Loss)",
        "trailing" => "移动止盈/保本 (Trailing Stop)",
        "breakeven" => "保本平仓 (Breakeven)",
        "late_entry_fill" => "迟到成交退出 (Late Fill Exit)",
        "external_manual" => "手动/外部平仓 (Manual Close)",
        s if s.starts_with("manual") => "手动平仓 (Manual Close)",
        s if s.starts_with("session") || s.starts_with("overnight") => "时段风控平仓 (Session Risk)",
        _ => "策略平仓",
    }
}

pub fn format_exit_reason_short(reason: &str) -> &'static str {
    match reason {
        "target" => "目标止盈",
        "stop" => "触发止损",
        "trailing" => "移动止盈",
        "breakeven" => "保本平仓",
        "late_entry_fill" => "迟到成交退出",
        "external_manual" => "外部平仓",
        s if s.starts_with("manual") => "手动平仓",
        s if s.starts_with("session") || s.starts_with("overnight") => "风控平仓",
        _ => "策略平仓",
    }
}

fn fmt_account_section(account: Option<&crate::trader::TraderAccount>) -> String {
    if let Some(acc) = account {
        let margin_ratio = if acc.balance > 0.0 {
            (acc.curr_margin / acc.balance * 100.0).clamp(0.0, 100.0)
        } else {
            0.0
        };
        format!(
            "■ 账户资产概况\n\
             • 动态权益：{:.2} 元\n\
             • 可用资金：{:.2} 元\n\
             • 占用保证金：{:.2} 元（保证金占用率 {:.1}%）\n\
             • 当日平仓盈亏：{:+.2} 元",
            acc.balance, acc.available, acc.curr_margin, margin_ratio, acc.close_profit
        )
    } else {
        "■ 账户资产概况\n• 暂未获取到账户资金快照".to_string()
    }
}

pub fn trade_open_email_payload(n: &LiveTradeOpenNotice) -> (String, String) {
    let dir = if n.direction == "up" { "多" } else { "空" };
    let dir_label = if n.direction == "up" { "买入做多" } else { "卖出做空" };
    let sig_str = n
        .signal_id
        .map(|id| format!("#{id}"))
        .unwrap_or_else(|| "无信号关联".to_string());
    let stop_str = if n.stop_amount > 0.0 {
        format!(" 止损亏损 -{:.2}元", n.stop_amount)
    } else {
        String::new()
    };
    let subject = format!(
        "【实盘开仓】{} · {} {} {}手 @{:.1}{}",
        n.instance_name, n.symbol, dir, n.volume, n.fill_price, stop_str
    );

    let slippage_desc = if n.slippage_pts.abs() < 1e-4 {
        "0.00 点（无滑点）".to_string()
    } else if n.slippage_pts < 0.0 {
        format!(
            "有利滑点 {:.2} 点（合 {:.2} 元，入场价格更优）",
            n.slippage_pts.abs(),
            n.slippage_amount
        )
    } else {
        format!(
            "不利滑点 {:.2} 点（合 {:.2} 元，存在跳空或追价滑点）",
            n.slippage_pts,
            n.slippage_amount
        )
    };

    let now_str = crate::analyze::time::now_minute();
    let account_sec = fmt_account_section(n.account.as_ref());

    let body = format!(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\
         【实盘开仓提醒】N-Trend Automated Execution\n\
         ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\
         ■ 基本信息\n\
         • 交易实例：{}（{}）\n\
         • 交易标的：{}（{}）\n\
         • 信号编号：{}\n\
         • 开仓方向：{}\n\
         • 成交数量：{} 手\n\
         • 成交时间：{}\n\n\
         ■ 价格与滑点\n\
         • 成交入场价：{:.2}\n\
         • 原定入场价：{:.2}\n\
         • 入场滑点：{}\n\n\
         ■ 目标与风控\n\
         • 目标价位：{:.2}（{:.2} 点，合 {:.2} 元）\n\
         • 止损价位：{:.2}（{:.2} 点，合 {:.2} 元）\n\
         • 风险收益比：{:.2}R\n\n\
         {}\n\n\
         ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\
         系统记录时间：{}",
        n.instance_name,
        n.instance_id,
        n.symbol,
        n.instrument_id,
        sig_str,
        dir_label,
        n.volume,
        n.trade_time,
        n.fill_price,
        n.planned_entry_price,
        slippage_desc,
        n.target_price,
        n.target_pts,
        n.target_amount,
        n.stop_price,
        n.stop_pts,
        n.stop_amount,
        n.rr,
        account_sec,
        now_str,
    );

    (subject, body)
}

pub fn trade_close_email_payload(n: &LiveTradeCloseNotice) -> (String, String) {
    let reason_short = format_exit_reason_short(&n.exit_reason);
    let reason_full = format_exit_reason(&n.exit_reason);
    let subject = format!(
        "【实盘平仓】{} · {} {} {:+.2}元 ({:+.1}点)",
        n.instance_name, n.symbol, reason_short, n.realized_pnl_amount, n.realized_pnl_pts
    );

    let sig_str = n
        .signal_id
        .map(|id| format!("#{id}"))
        .unwrap_or_else(|| "无信号关联".to_string());

    let planned_exit_desc = n
        .planned_exit_price
        .map(|p| format!("{p:.2}"))
        .unwrap_or_else(|| "随市/动态价格".to_string());

    let slippage_desc = match (n.slippage_pts, n.slippage_amount) {
        (Some(pts), Some(amt)) => {
            if pts.abs() < 1e-4 {
                "0.00 点（无滑点）".to_string()
            } else if pts < 0.0 {
                format!("有利滑点 {:.2} 点（合 {:.2} 元，平仓价格更优）", pts.abs(), amt)
            } else {
                format!("不利滑点 {:.2} 点（合 {:.2} 元，平仓滑点磨损）", pts, amt)
            }
        }
        _ => "无基准参考".to_string(),
    };

    let planned_pnl_desc = n
        .planned_pnl_amount
        .map(|amt| format!("{amt:+.2} 元"))
        .unwrap_or_else(|| "跟随市场动态退出".to_string());

    let diff_desc = match n.pnl_diff_amount {
        Some(diff) if diff > 0.01 => {
            format!("比原定计划多赚 {:.2} 元（实际成交价格更优！）", diff)
        }
        Some(diff) if diff < -0.01 => {
            format!("比原定计划少赚/多亏 {:.2} 元（滑点磨损）", diff.abs())
        }
        Some(_) => "与原定计划盈亏完全一致（无滑点）".to_string(),
        None => "非固定目标平仓，按实际执行结算".to_string(),
    };

    let now_str = crate::analyze::time::now_minute();
    let account_sec = fmt_account_section(n.account.as_ref());

    let body = format!(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\
         【实盘平仓提醒】N-Trend Automated Execution\n\
         ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\
         ■ 基本信息\n\
         • 交易实例：{}（{}）\n\
         • 交易标的：{}（{}）\n\
         • 关联信号：{}\n\
         • 平仓原因：{}\n\
         • 平仓数量：{} 手\n\
         • 持仓时长：{} 根K线\n\
         • 平仓时间：{}\n\n\
         ■ 成交价格与滑点\n\
         • 入场均价：{:.2}\n\
         • 平仓成交价：{:.2}\n\
         • 预期平仓价：{}\n\
         • 平仓滑点：{}\n\n\
         ■ 盈亏统计与收益对比\n\
         • 实际盈亏：{:+.2} 点，合 {:+.2} 元（{:+.2}R）\n\
         • 原定计划盈亏：{}\n\
         • 收益对比：{}\n\n\
         {}\n\n\
         ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\
         系统记录时间：{}",
        n.instance_name,
        n.instance_id,
        n.symbol,
        n.instrument_id,
        sig_str,
        reason_full,
        n.volume,
        n.bars_held,
        n.exit_ts,
        n.fill_price,
        n.exit_price,
        planned_exit_desc,
        slippage_desc,
        n.realized_pnl_pts,
        n.realized_pnl_amount,
        n.r_multiple,
        planned_pnl_desc,
        diff_desc,
        account_sec,
        now_str,
    );

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
        let (subject_no_model, body) = event_email_payload(EventEmailKind::Trigger, &e);
        assert!(!subject_no_model.contains("高胜率形态触发"));
        assert!(body.contains("触发时间：2026-08-14 13:45"));
        assert!(body.contains("触发价：4217.0"));
        assert!(body.contains("追价深度：0.02R"));
        assert!(body.contains("触发K线质量：3.90"));
        assert!(body.contains("当前持仓评分：4.10"));
        assert!(body.contains("冠军模型胜率：暂无可用结果"));

        let (subject_low, _) = event_email_payload_with_model(
            EventEmailKind::Trigger,
            &e,
            Some(("logistic-v2", 0.42)),
        );
        assert!(!subject_low.contains("高胜率形态触发"));

        let (subject_high, body) = event_email_payload_with_model(
            EventEmailKind::Trigger,
            &e,
            Some(("logistic-v2", 0.684)),
        );
        assert!(subject_high.starts_with("【高胜率形态触发】"));
        assert!(subject_high.contains("N趋势触发【BU0】做多 A级 3.60分"));
        assert!(body.contains("冠军模型胜率：68.4%（logistic-v2）"));

        let preclose = preclose_signals::Model {
            id: 7,
            symbol: "BU0".to_string(),
            direction: "up".to_string(),
            session_close_ts: "2026-08-14 15:00:00".to_string(),
            emitted_at: "2026-08-14 14:57:00".to_string(),
            reference_price: 4210.0,
            parent_event_id: e.id,
            entry: e.entry,
            stop: e.stop,
            target: e.target,
            risk: e.risk,
            state: "precheck".to_string(),
            confirmed_at: None,
            invalid_reason: None,
            next_open_ts: None,
            next_open_price: None,
            gap_pct: None,
            mfe_r: None,
            mae_r: None,
            outcome: None,
            outcome_ts: None,
            horizon_minutes: 60,
            created_at: "2026-08-14 14:57:00".to_string(),
            updated_at: "2026-08-14 14:57:00".to_string(),
        };
        let (subject, body) = preclose_email_payload(&preclose, &e);
        assert!(subject.contains("收盘前预检测【BU0】做多"));
        assert!(body.contains("预检测时间：2026-08-14 14:57:00"));
        assert!(body.contains("预计收盘：2026-08-14 15:00:00"));
        assert!(body.contains("参考现价：4210.0"));
        assert!(body.contains("来源正式候选：#1"));
        assert!(body.contains("观察窗口：60分钟"));
        assert!(body.contains("入场评分：3.60"));
    }

    #[test]
    fn test_trade_open_email_payload() {
        let notice = LiveTradeOpenNotice {
            instance_id: "default".into(),
            instance_name: "默认实盘".into(),
            symbol: "rb2505".into(),
            instrument_id: "rb2505".into(),
            signal_id: Some(1042),
            direction: "up".into(),
            volume: 1,
            fill_price: 3520.0,
            planned_entry_price: 3518.0,
            slippage_pts: 2.0,
            slippage_amount: 20.0,
            target_price: 3580.0,
            target_pts: 60.0,
            target_amount: 600.0,
            stop_price: 3490.0,
            stop_pts: 30.0,
            stop_amount: 300.0,
            rr: 2.0,
            trade_time: "2026-11-20 09:30:00".into(),
            account: Some(crate::trader::TraderAccount {
                balance: 100_000.0,
                available: 85_000.0,
                curr_margin: 15_000.0,
                close_profit: 0.0,
                position_profit: 0.0,
                updated_at: 0.0,
            }),
        };

        let (subject, body) = trade_open_email_payload(&notice);
        assert_eq!(
            subject,
            "【实盘开仓】默认实盘 · rb2505 多 1手 @3520.0 止损亏损 -300.00元"
        );
        assert!(body.contains("交易标的：rb2505"));
        assert!(body.contains("成交入场价：3520.00"));
        assert!(body.contains("原定入场价：3518.00"));
        assert!(body.contains("不利滑点 2.00 点（合 20.00 元"));
        assert!(body.contains("目标价位：3580.00（60.00 点，合 600.00 元）"));
        assert!(body.contains("止损价位：3490.00（30.00 点，合 300.00 元）"));
        assert!(body.contains("动态权益：100000.00 元"));
        assert!(body.contains("可用资金：85000.00 元"));
        assert!(body.contains("占用保证金：15000.00 元（保证金占用率 15.0%）"));
    }

    #[test]
    fn test_trade_close_email_payload_target_better() {
        let notice = LiveTradeCloseNotice {
            instance_id: "default".into(),
            instance_name: "默认实盘".into(),
            symbol: "rb2505".into(),
            instrument_id: "rb2505".into(),
            signal_id: Some(1042),
            direction: "up".into(),
            volume: 1,
            fill_price: 3500.0,
            exit_price: 3555.0,
            planned_exit_price: Some(3550.0),
            exit_reason: "target".into(),
            slippage_pts: Some(-5.0),
            slippage_amount: Some(50.0),
            realized_pnl_pts: 55.0,
            realized_pnl_amount: 550.0,
            planned_pnl_amount: Some(500.0),
            pnl_diff_amount: Some(50.0),
            r_multiple: 1.1,
            bars_held: 8,
            exit_ts: "2026-11-20 11:15:00".into(),
            account: Some(crate::trader::TraderAccount {
                balance: 100_550.0,
                available: 100_550.0,
                curr_margin: 0.0,
                close_profit: 550.0,
                position_profit: 0.0,
                updated_at: 0.0,
            }),
        };

        let (subject, body) = trade_close_email_payload(&notice);
        assert_eq!(subject, "【实盘平仓】默认实盘 · rb2505 目标止盈 +550.00元 (+55.0点)");
        assert!(body.contains("平仓原因：目标止盈 (Target 1.0R)"));
        assert!(body.contains("实际盈亏：+55.00 点，合 +550.00 元"));
        assert!(body.contains("原定计划盈亏：+500.00 元"));
        assert!(body.contains("比原定计划多赚 50.00 元（实际成交价格更优！）"));
        assert!(body.contains("有利滑点 5.00 点（合 50.00 元，平仓价格更优）"));
        assert!(body.contains("动态权益：100550.00 元"));
    }
}
