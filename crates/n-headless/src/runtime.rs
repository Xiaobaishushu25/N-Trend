use crate::state::ServerContext;
use chrono::Local;
use n_core::notify::email::{event_email_payload_with_model, single_bar_email_payload, EventEmailKind};
use n_core::service::{RefreshStats, ScanResult};
use n_protocol::NewNotificationHistoryItem;
use std::sync::Arc;
use std::time::{Duration, Instant};

pub fn spawn_runtime_tasks(ctx: Arc<ServerContext>) {
    spawn_scheduler(ctx.clone());
    spawn_quote_poller(ctx.clone());
    spawn_tq_bar_event_consumer(ctx.clone());
}

fn spawn_scheduler(ctx: Arc<ServerContext>) {
    tokio::spawn(async move {
        let mut ticker = tokio::time::interval(Duration::from_secs(15));
        ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        let mut startup_done = false;
        let mut last_trading_status: Option<n_core::session::TradingStatus> = None;
        let mut last_refresh: Option<chrono::DateTime<Local>> = None;
        let mut last_scan: Option<chrono::DateTime<Local>> = None;

        loop {
            ticker.tick().await;
            if ctx.is_shutting_down.load(std::sync::atomic::Ordering::Relaxed) {
                break;
            }
            if !ctx.scheduler_running.load(std::sync::atomic::Ordering::Relaxed) {
                continue;
            }

            let now = Local::now();
            let cfg = ctx.services.config().await.scheduler.clone();

            let current_status = n_core::session::SessionCalendar::current_trading_status(&now);
            if last_trading_status != Some(current_status) {
                match current_status {
                    n_core::session::TradingStatus::Trading => {
                        tracing::info!("☀️ [服务端调度器] 交易时段已开启，恢复常规行情刷新与扫描调度");
                    }
                    n_core::session::TradingStatus::HolidayOff => {
                        tracing::info!("🌙 [服务端调度器] 当前处于法定节假日休市时段，进入静默休眠");
                    }
                    n_core::session::TradingStatus::PreHolidayNightOff => {
                        tracing::info!("🌙 [服务端调度器] 当前处于节假日前夕（今晚无夜盘），进入静默休眠");
                    }
                    n_core::session::TradingStatus::WeekendOff => {
                        tracing::info!("🌙 [服务端调度器] 当前处于周末休市时段，进入静默休眠");
                    }
                    n_core::session::TradingStatus::DailyIntermission => {
                        tracing::info!("🌙 [服务端调度器] 当前处于日常非交易时段，进入静默休眠");
                    }
                }
                last_trading_status = Some(current_status);
            }

            if !startup_done {
                startup_done = true;
                if n_core::scheduler::is_trading_time(&now) {
                    if let Ok(stats) = run_refresh(&ctx).await {
                        last_refresh = Some(now);
                        let seq = ctx.next_event_seq();
                        ctx.realtime_hub.broadcast(seq, "data.updated", serde_json::to_value(&stats).unwrap_or_default()).await;
                    }
                    continue;
                }
            }

            let action = n_core::scheduler::next_action(now, &cfg, last_refresh, last_scan);
            match action {
                n_core::scheduler::SchedulerAction::None => {}
                n_core::scheduler::SchedulerAction::Refresh => {
                    if let Ok(stats) = run_refresh(&ctx).await {
                        last_refresh = Some(now);
                        let seq = ctx.next_event_seq();
                        ctx.realtime_hub.broadcast(seq, "data.updated", serde_json::to_value(&stats).unwrap_or_default()).await;
                    }
                }
                n_core::scheduler::SchedulerAction::Scan => {
                    let _ = run_refresh(&ctx).await;
                    last_refresh = Some(now);
                    if let Ok(scan_res) = run_scan(&ctx).await {
                        last_scan = Some(now);
                        let seq = ctx.next_event_seq();
                        ctx.realtime_hub.broadcast(seq, "scan.completed", serde_json::to_value(&scan_res).unwrap_or_default()).await;
                    }
                }
                n_core::scheduler::SchedulerAction::RefreshAndScan => {
                    if let Ok(stats) = run_refresh(&ctx).await {
                        last_refresh = Some(now);
                        let seq = ctx.next_event_seq();
                        ctx.realtime_hub.broadcast(seq, "data.updated", serde_json::to_value(&stats).unwrap_or_default()).await;
                    }
                    if let Ok(scan_res) = run_scan(&ctx).await {
                        last_scan = Some(now);
                        let seq = ctx.next_event_seq();
                        ctx.realtime_hub.broadcast(seq, "scan.completed", serde_json::to_value(&scan_res).unwrap_or_default()).await;
                    }
                }
            }
        }
    });
}

async fn run_refresh(ctx: &Arc<ServerContext>) -> anyhow::Result<RefreshStats> {
    let t0 = Instant::now();
    tracing::info!("🔄 刷新触发 | {}", Local::now().format("%H:%M:%S"));
    match ctx.services.refresh_data().await {
        Ok(s) => {
            *ctx.last_refresh.write().await = Some(Local::now().naive_local());
            tracing::info!(
                "✓ 刷新完成 {}ms | 成功 {} 失败 {}",
                t0.elapsed().as_millis(),
                s.succeeded,
                s.failures
            );
            Ok(s)
        }
        Err(e) => {
            tracing::error!("刷新失败: {e}");
            Err(e)
        }
    }
}

async fn run_scan(ctx: &Arc<ServerContext>) -> anyhow::Result<ScanResult> {
    let t0 = Instant::now();
    tracing::info!("🔍 扫描触发 | {}", Local::now().format("%H:%M:%S"));
    let res = ctx.services.run_scan().await?;
    *ctx.last_scan.write().await = Some(Local::now().naive_local());

    tracing::info!(
        "✓ 扫描完成 {}ms | 扫描 {} 活跃 {} 新预警 {} 新触发 {}",
        t0.elapsed().as_millis(),
        res.scanned,
        res.active_count,
        res.new_warnings.len(),
        res.newly_triggered.len()
    );

    // 将新预警和新触发持久化到服务端的 notification_history 并广播
    for w in &res.new_warnings {
        let item = NewNotificationHistoryItem {
            kind: "warning".to_string(),
            title: Some(format!("{} 出现新形态预警", w.symbol)),
            content: format!("{} {} 级形态 (评分: {:.1})", w.direction, w.level, w.entry_score),
            signal: Some(n_protocol::dto::NotificationSignal {
                code: w.symbol.clone(),
                name: w.symbol.clone(),
                direction: w.direction.clone(),
                level: w.level.clone(),
                grade: w.grade.clone(),
                score: w.entry_score,
                entry: w.entry,
                stop: w.stop,
                target: w.target,
                time: Some(w.warning_ts.clone()),
            }),
            entry_trigger: None,
            single_bar: None,
            manual_level: None,
        };
        if let Ok(saved) = n_core::storage::repo::insert_notification_history(&ctx.db, &item).await {
            let seq = ctx.next_event_seq();
            ctx.realtime_hub.broadcast(seq, "notification.created", serde_json::to_value(&saved).unwrap_or_default()).await;
        }
    }

    for t in &res.newly_triggered {
        let item = NewNotificationHistoryItem {
            kind: "trigger".to_string(),
            title: Some(format!("{} 形态入场触发", t.symbol)),
            content: format!("{} {} 级形态触达入场价: {}", t.direction, t.level, t.entry),
            signal: Some(n_protocol::dto::NotificationSignal {
                code: t.symbol.clone(),
                name: t.symbol.clone(),
                direction: t.direction.clone(),
                level: t.level.clone(),
                grade: t.grade.clone(),
                score: t.entry_score,
                entry: t.entry,
                stop: t.stop,
                target: t.target,
                time: t.trigger_bar_ts.clone(),
            }),
            entry_trigger: None,
            single_bar: None,
            manual_level: None,
        };
        if let Ok(saved) = n_core::storage::repo::insert_notification_history(&ctx.db, &item).await {
            let seq = ctx.next_event_seq();
            ctx.realtime_hub.broadcast(seq, "notification.created", serde_json::to_value(&saved).unwrap_or_default()).await;
        }
    }

    // 邮件发送
    let cfg = ctx.services.config().await;
    let min_score = cfg.notify.new_pattern_min_score;
    if cfg.email.enabled && cfg.email.sendable() {
        let triggered_ids: std::collections::HashSet<i64> =
            res.newly_triggered.iter().map(|e| e.id).collect();
        let mut emails = Vec::new();
        for e in res
            .new_warnings
            .iter()
            .filter(|e| e.entry_score >= min_score && !triggered_ids.contains(&e.id))
        {
            emails.push((EventEmailKind::Warning, e));
        }
        for e in res
            .newly_triggered
            .iter()
            .filter(|e| e.entry_score >= min_score)
        {
            emails.push((EventEmailKind::Trigger, e));
        }
        for (kind, e) in emails {
            let win_rate = if kind == EventEmailKind::Trigger {
                n_core::v2::prediction::champion_win_rate(&ctx.db, e.id)
                    .await
                    .ok()
                    .flatten()
            } else {
                None
            };
            let model = win_rate.as_ref().map(|rate| (rate.model_id.as_str(), rate.p_win));
            let (subject, body) = event_email_payload_with_model(kind, e, model);
            let email_cfg = cfg.email.clone();
            tokio::task::spawn_blocking(move || {
                let _ = n_core::notify::email::send_summary(&subject, &body, &email_cfg);
            });
        }
        for sb in &res.single_bars {
            let (subject, body) = single_bar_email_payload(sb);
            let email_cfg = cfg.email.clone();
            tokio::task::spawn_blocking(move || {
                let _ = n_core::notify::email::send_summary(&subject, &body, &email_cfg);
            });
        }
    }

    Ok(res)
}

fn spawn_quote_poller(ctx: Arc<ServerContext>) {
    tokio::spawn(async move {
        loop {
            let interval_ms = ctx.services.config().await.quote.poll_interval_ms;
            tokio::time::sleep(Duration::from_millis(interval_ms)).await;
            if ctx.is_shutting_down.load(std::sync::atomic::Ordering::Relaxed) {
                break;
            }

            let now = Local::now();
            if !n_core::scheduler::is_trading_time(&now) {
                continue;
            }

            match ctx.services.realtime_quotes().await {
                Ok(snapshots) => {
                    let seq = ctx.next_event_seq();
                    ctx.realtime_hub.broadcast(seq, "quote.updated", serde_json::to_value(&snapshots).unwrap_or_default()).await;

                    // 检测入场价命中
                    if let Ok(hits) = ctx.services.entry_trigger_hits(&snapshots).await {
                        if !hits.is_empty() {
                            let seq = ctx.next_event_seq();
                            ctx.realtime_hub.broadcast(seq, "entry.triggered", serde_json::to_value(&hits).unwrap_or_default()).await;
                        }
                    }

                    // 检测关键区域手工画线
                    if let Ok(alerts) = ctx.services.manual_level_alerts(&snapshots).await {
                        if !alerts.is_empty() {
                            let seq = ctx.next_event_seq();
                            ctx.realtime_hub.broadcast(seq, "manual_level.triggered", serde_json::to_value(&alerts).unwrap_or_default()).await;
                        }
                    }
                }
                Err(e) => {
                    tracing::warn!("实时行情轮询失败: {e}");
                }
            }
        }
    });
}

fn spawn_tq_bar_event_consumer(ctx: Arc<ServerContext>) {
    tokio::spawn(async move {
        let mut is_first = true;
        let mut after_id = 0u64;
        loop {
            tokio::time::sleep(Duration::from_secs(3)).await;
            if ctx.is_shutting_down.load(std::sync::atomic::Ordering::Relaxed) {
                break;
            }

            let cfg = ctx.services.config().await;
            if cfg.data_source.primary_source != "tqsdk" || !ctx.services.data_source.tq_is_available() {
                continue;
            }

            let now = Local::now();
            if !n_core::scheduler::is_trading_time(&now) {
                // 非交易时段（如周末、夜盘收盘后）无行情 Tick 与闭合 K 线，静默休眠
                tokio::time::sleep(Duration::from_secs(30)).await;
                continue;
            }

            if is_first {
                is_first = false;
                tokio::time::sleep(Duration::from_secs(30)).await;
            }

            let tq = ctx.services.data_source.tq_client().await;
            if let Ok(response) = tq.poll_closed_bar_events(after_id, 25).await {
                for ev in response.events {
                    if ev.event_id > after_id {
                        after_id = ev.event_id;
                    }
                    tracing::info!("🔔 [FAST_PATH] 收到天勤闭合K线: {} {} {}", ev.symbol, ev.period, ev.bar_end);
                    let seq = ctx.next_event_seq();
                    ctx.realtime_hub.broadcast_kline(
                        seq,
                        "kline.closed",
                        &ev.symbol,
                        &ev.period,
                        serde_json::to_value(&ev).unwrap_or_default(),
                    ).await;
                }
            }
        }
    });
}
