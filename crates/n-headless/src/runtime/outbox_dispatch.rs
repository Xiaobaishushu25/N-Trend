//! R4a durable publication worker. No trading signal depends solely on the
//! volatile ScanResult.new_warnings vector surviving process shutdown.
use super::{register_execution_signals, registered_event_ids};
use crate::state::ServerContext;
use anyhow::{anyhow, Context, Result};
use chrono::{Local, NaiveDateTime};
use n_core::notify::email::{event_email_payload_with_model, EventEmailKind};
use n_core::storage::{entities::pattern_events, outbox, repo};
use n_protocol::{dto::NotificationSignal, NewNotificationHistoryItem};
use std::{
    sync::Arc,
    time::{Duration, Instant},
};

const EXECUTION_MAX_AGE_SECS: i64 = 2 * 60 * 60;
const EMAIL_MAX_AGE_SECS: i64 = 12 * 60 * 60;

fn age_seconds(event: &pattern_events::Model) -> Option<i64> {
    NaiveDateTime::parse_from_str(&event.warning_ts, "%Y-%m-%d %H:%M:%S")
        .ok()
        .map(|dt| (Local::now().naive_local() - dt).num_seconds())
}

fn notice(event: &pattern_events::Model, kind: &str) -> NewNotificationHistoryItem {
    let is_trigger = kind == "trigger";
    NewNotificationHistoryItem {
        kind: kind.to_string(),
        title: Some(if is_trigger {
            format!("{} 形态入场触发", event.symbol)
        } else {
            format!("{} 出现新形态预警", event.symbol)
        }),
        content: if is_trigger {
            format!(
                "{} {} 级形态触达入场价: {}",
                event.direction, event.level, event.entry
            )
        } else {
            format!(
                "{} {} 级形态 (评分: {:.1})",
                event.direction, event.level, event.entry_score
            )
        },
        signal: Some(NotificationSignal {
            code: event.symbol.clone(),
            name: event.symbol.clone(),
            direction: event.direction.clone(),
            level: event.level.clone(),
            grade: event.grade.clone(),
            score: event.entry_score,
            entry: event.entry,
            stop: event.stop,
            target: event.target,
            time: if is_trigger {
                event.trigger_bar_ts.clone()
            } else {
                Some(event.warning_ts.clone())
            },
        }),
        entry_trigger: None,
        single_bar: None,
        manual_level: None,
    }
}

enum DeliveryResult {
    Done,
    Skipped,
}

async fn deliver_registration(
    ctx: &Arc<ServerContext>,
    e: &pattern_events::Model,
) -> Result<DeliveryResult> {
    if e.entry_score < 2.5 || !matches!(e.direction.as_str(), "up" | "down") {
        return Ok(DeliveryResult::Skipped);
    }
    let Some(age) = age_seconds(e) else {
        return Ok(DeliveryResult::Skipped);
    };
    if age < -60 || age > EXECUTION_MAX_AGE_SECS || e.state != "pending" {
        // NEVER place a stale order after a prolonged network/server outage.
        return Ok(DeliveryResult::Skipped);
    }
    let not_before = repo::get_setting(&ctx.db, "execution_signal_not_before")
        .await?
        .unwrap_or_else(|| "0".into())
        .parse::<i64>()
        .context("执行预警时间界限无效")?;
    let warning_epoch = NaiveDateTime::parse_from_str(&e.warning_ts, "%Y-%m-%d %H:%M:%S")?
        .and_local_timezone(Local)
        .single()
        .ok_or_else(|| anyhow!("预警时间本地时区歧义"))?
        .timestamp();
    if warning_epoch < not_before {
        return Ok(DeliveryResult::Skipped);
    }

    let enabled = repo::list_symbols(&ctx.db, true)
        .await?
        .iter()
        .any(|s| s.code == e.symbol);
    if !enabled {
        return Ok(DeliveryResult::Skipped);
    }
    // Existing engine conditions are the authoritative idempotency key. The
    // registration function and the fallback ticker share a single mutex.
    register_execution_signals(ctx, std::slice::from_ref(e)).await;
    let Some(manager) = ctx.instances.get() else {
        let engine = &ctx.execution_engine;
        let strategy_id = engine.definition().await.strategy_id;
        let registered = registered_event_ids(engine.list_conditions().await, &strategy_id);
        return if registered.contains(&e.id) {
            Ok(DeliveryResult::Done)
        } else {
            Err(anyhow!("默认交易实例尚未完成条件注册"))
        };
    };
    let mut missing = Vec::new();
    for runtime in manager.all().await {
        if runtime.definition.id != "default" && warning_epoch < runtime.definition.created_at {
            continue;
        }
        let strategy_id = runtime.engine.definition().await.strategy_id;
        if strategy_id != n_core::trader::strategy::N_STRATEGY {
            continue;
        }
        if runtime.engine.paused().await {
            missing.push(runtime.definition.id.clone());
            continue;
        }
        let registered = registered_event_ids(runtime.engine.list_conditions().await, &strategy_id);
        if !registered.contains(&e.id) {
            missing.push(runtime.definition.id.clone());
        }
    }
    if missing.is_empty() {
        Ok(DeliveryResult::Done)
    } else {
        Err(anyhow!("交易条件尚未在全部适用实例持久化: {missing:?}"))
    }
}

async fn deliver(ctx: &Arc<ServerContext>, task: &outbox::Delivery) -> Result<DeliveryResult> {
    let Some(e) = repo::pattern_event_by_id(&ctx.db, task.event_id).await? else {
        // Deleted/merged events must never be resurrected by an old task.
        return Ok(DeliveryResult::Skipped);
    };
    // A pattern_events INSERT commits before freeze_event and before the R3
    // scan cursor. Never deliver a newly inserted warning from a half-finished
    // scan: the durable cursor is the commit barrier for the entire scan.
    if matches!(
        task.action.as_str(),
        outbox::NOTICE_WARNING | outbox::EMAIL_WARNING | outbox::REGISTER_WARNING
    ) {
        let key = format!(
            "r3_scan_cursor_v1:{}:{}",
            n_core::v2::version::EVENT_LOGIC_VERSION,
            e.symbol
        );
        let finished = repo::get_setting(&ctx.db, &key)
            .await?
            .and_then(|raw| serde_json::from_str::<serde_json::Value>(&raw).ok())
            .and_then(|v| v["last_bar_end"].as_str().map(str::to_owned))
            .is_some_and(|last| last >= e.warning_ts);
        if !finished {
            if age_seconds(&e).is_some_and(|age| age > EMAIL_MAX_AGE_SECS) {
                return Ok(DeliveryResult::Skipped);
            }
            return Err(anyhow!("R3 扫描游标未完成，暂缓发布 {}", e.symbol));
        }
    }
    match task.action.as_str() {
        outbox::NOTICE_WARNING | outbox::NOTICE_TRIGGER => {
            let kind = if task.action == outbox::NOTICE_WARNING {
                "warning"
            } else {
                "trigger"
            };
            if let Some(saved) = outbox::persist_notice(&ctx.db, task, &notice(&e, kind)).await? {
                ctx.realtime_hub
                    .broadcast(
                        ctx.next_event_seq(),
                        "notification.created",
                        serde_json::to_value(saved).unwrap_or_default(),
                    )
                    .await;
            }
            Ok(DeliveryResult::Done)
        }
        outbox::EMAIL_WARNING | outbox::EMAIL_TRIGGER => {
            let cfg = ctx.services.config().await;
            if !cfg.email.enabled
                || !cfg.email.sendable()
                || e.entry_score < cfg.notify.new_pattern_min_score
            {
                return Ok(DeliveryResult::Skipped);
            }
            if age_seconds(&e).is_none_or(|age| age < -60 || age > EMAIL_MAX_AGE_SECS) {
                return Ok(DeliveryResult::Skipped);
            }
            if task.action == outbox::EMAIL_WARNING && e.trigger_ts.is_some() {
                // A warning immediately followed by its trigger gets only the
                // trigger email (same policy as the previous scan publisher).
                return Ok(DeliveryResult::Skipped);
            }
            let kind = if task.action == outbox::EMAIL_WARNING {
                EventEmailKind::Warning
            } else {
                EventEmailKind::Trigger
            };
            let win_rate = if kind == EventEmailKind::Trigger {
                n_core::v2::prediction::champion_win_rate(&ctx.db, e.id)
                    .await
                    .ok()
                    .flatten()
            } else {
                None
            };
            let model = win_rate
                .as_ref()
                .map(|rate| (rate.model_id.as_str(), rate.p_win));
            let (subject, body) = event_email_payload_with_model(kind, &e, model);
            let email_cfg = cfg.email.clone();
            tokio::task::spawn_blocking(move || {
                n_core::notify::email::send_summary(&subject, &body, &email_cfg)
            })
            .await
            .context("SMTP 工作线程失败")??;
            Ok(DeliveryResult::Done)
        }
        outbox::REGISTER_WARNING => deliver_registration(ctx, &e).await,
        other => Err(anyhow!("未知 signal delivery action: {other}")),
    }
}

pub(super) fn spawn(ctx: Arc<ServerContext>) {
    // SMTP may block for seconds. Email must never hold up registration/alerts.
    for emails in [false, true] {
        let ctx = ctx.clone();
        tokio::spawn(async move {
            loop {
                if ctx
                    .is_shutting_down
                    .load(std::sync::atomic::Ordering::Relaxed)
                {
                    break;
                }
                let task = match outbox::claim_next(&ctx.db, emails).await {
                    Ok(Some(task)) => task,
                    Ok(None) => {
                        tokio::time::sleep(Duration::from_millis(500)).await;
                        continue;
                    }
                    Err(error) => {
                        tracing::error!("[R4A_OUTBOX] 领取任务失败: {error:#}");
                        tokio::time::sleep(Duration::from_secs(2)).await;
                        continue;
                    }
                };
                let started = Instant::now();
                let result = deliver(&ctx, &task).await;
                match result {
                    Ok(DeliveryResult::Done) => {
                        // persist_notice already ACKed within its own transaction.
                        if let Err(error) = outbox::complete(&ctx.db, &task, "done").await {
                            tracing::error!(event_id=task.event_id, action=%task.action,
                            "[R4A_OUTBOX] 完成标记失败: {error:#}");
                        } else {
                            tracing::info!(event_id=task.event_id,action=%task.action,
                            attempts=task.attempts, delivery_ms=started.elapsed().as_millis(),
                            "[R4A_OUTBOX] 投递完成");
                        }
                    }
                    Ok(DeliveryResult::Skipped) => {
                        if let Err(error) = outbox::complete(&ctx.db, &task, "skipped").await {
                            tracing::error!("[R4A_OUTBOX] 跳过标记失败: {error:#}");
                        }
                    }
                    Err(error) => {
                        tracing::warn!(event_id=task.event_id,action=%task.action,attempts=task.attempts,
                        "[R4A_OUTBOX] 投递失败，排队重试: {error:#}");
                        if let Err(mark_error) =
                            outbox::retry(&ctx.db, &task, &format!("{error:#}")).await
                        {
                            tracing::error!("[R4A_OUTBOX] 退避状态写入失败: {mark_error:#}");
                        }
                    }
                }
            }
        });
    }
}
