use std::sync::{atomic::Ordering, Arc};
use std::time::{Duration, Instant};

use anyhow::Result;
use chrono::{DateTime, Local, NaiveDateTime, Timelike};
use n_core::scheduler::{
    expected_5m_close, symbol_scan_due_at, symbol_scan_settled, CloseScanMode,
};
use n_core::storage::{repo, scan_jobs};
use scan_jobs::{Job, Next, Work};
use tokio::sync::Notify;

use crate::state::ServerContext;

const BAR_FORMAT: &str = "%Y-%m-%d %H:%M:%S";

pub(super) fn spawn(ctx: Arc<ServerContext>, wake: Arc<Notify>, mode: CloseScanMode) {
    tracing::info!(
        ?mode,
        scan_workers = mode.workers(),
        "[SCAN_QUEUE] runtime configured"
    );
    tokio::spawn(schedule(ctx.clone(), wake.clone()));
    tokio::spawn(async move {
        while let Err(error) = scan_jobs::recover(&ctx.db).await {
            if ctx.is_shutting_down.load(Ordering::Relaxed) {
                return;
            }
            tracing::error!("恢复闭合扫描队列失败: {error:#}");
            tokio::time::sleep(Duration::from_secs(1)).await;
        }
        // Market requests use a separate consumer so missing data does not
        // occupy a scanner. SQLite claims enforce one active job per symbol.
        tokio::spawn(consume(ctx.clone(), wake.clone(), Work::Repair, mode));
        for _ in 1..mode.workers() {
            tokio::spawn(consume(ctx.clone(), wake.clone(), Work::Scan, mode));
        }
        consume(ctx, wake, Work::Scan, mode).await;
    });
}

async fn consume(ctx: Arc<ServerContext>, wake: Arc<Notify>, work: Work, mode: CloseScanMode) {
    while !ctx.is_shutting_down.load(Ordering::Relaxed) {
        let job = match scan_jobs::claim(&ctx.db, work, Local::now().timestamp()).await {
            Ok(Some(job)) => job,
            Ok(None) => {
                let _ = tokio::time::timeout(Duration::from_millis(500), wake.notified()).await;
                continue;
            }
            Err(error) => {
                tracing::error!(?work, "领取闭合扫描任务失败: {error:#}");
                tokio::time::sleep(Duration::from_secs(1)).await;
                continue;
            }
        };
        let started = Instant::now();
        let next = match process(&ctx, &job, work, mode).await {
            Ok(next) => next,
            Err(error) => {
                let retry_at = Local::now().timestamp() + (1 << job.attempt.clamp(1, 8));
                tracing::warn!(
                    ?work, symbol = %job.symbol, bar_end = %job.bar_end, attempt = job.attempt, retry_at,
                    "闭合扫描任务将重试: {error:#}"
                );
                match work {
                    Work::Scan => Next::ScanAt(retry_at),
                    Work::Repair => Next::RepairAt(retry_at),
                }
            }
        };
        while let Err(error) = scan_jobs::finish(&ctx.db, &job, &next).await {
            if ctx.is_shutting_down.load(Ordering::Relaxed) {
                return;
            }
            tracing::error!(symbol = %job.symbol, bar_end = %job.bar_end, "保存闭合扫描任务状态失败: {error:#}");
            tokio::time::sleep(Duration::from_secs(1)).await;
        }
        tracing::debug!(
            ?work, symbol = %job.symbol, bar_end = %job.bar_end, attempt = job.attempt,
            elapsed_ms = started.elapsed().as_millis(),
            "[SCAN_QUEUE] work acknowledged"
        );
        wake.notify_waiters();
    }
}

async fn process(
    ctx: &Arc<ServerContext>,
    job: &Job,
    work: Work,
    mode: CloseScanMode,
) -> Result<Next> {
    let bar = NaiveDateTime::parse_from_str(&job.bar_end, BAR_FORMAT)?;
    let enabled = repo::list_symbols(&ctx.db, true)
        .await?
        .into_iter()
        .any(|row| row.code == job.symbol);
    if !enabled || !expected_5m_close(&job.symbol, bar) {
        return Ok(Next::Skipped);
    }
    let now = Local::now();
    let due = symbol_scan_due_at(&job.symbol, bar);
    let repair_at = now.timestamp() + (due - now.naive_local()).num_seconds().max(0);

    if matches!(work, Work::Repair) {
        if now.naive_local() < due {
            return Ok(Next::RepairAt(repair_at.max(now.timestamp() + 1)));
        }
        ctx.services
            .repair_closed_scan_inputs(&job.symbol, &job.bar_end)
            .await?;
        return Ok(Next::ScanAt(Local::now().timestamp()));
    }

    if !ctx
        .services
        .closed_scan_inputs_ready(&job.symbol, &job.bar_end)
        .await?
    {
        return Ok(Next::RepairAt(repair_at));
    }
    let started = Instant::now();
    let (result, skipped) = ctx
        .services
        .scan_queued_15m_symbol(&job.symbol, &job.bar_end, mode)
        .await?;
    if let Some(res) = result {
        let _publication = ctx.scan_publication.lock().await;
        super::publish_scan_signals(ctx, &res).await;
        ctx.realtime_hub
            .broadcast(
                ctx.next_event_seq(),
                "scan.symbol_completed",
                serde_json::json!({
                    "symbol": job.symbol,
                    "bar_end": job.bar_end,
                    "result": res,
                }),
            )
            .await;
        *ctx.last_scan.write().await = Some(Local::now().naive_local());
    }
    tracing::info!(
        symbol = %job.symbol, bar_end = %job.bar_end, attempt = job.attempt, skipped,
        queued_ms = (now.timestamp() - job.queued_at).max(0) * 1000,
        scan_ms = started.elapsed().as_millis(),
        "[SCAN_QUEUE] symbol scan finished"
    );
    Ok(Next::Done)
}

async fn schedule(ctx: Arc<ServerContext>, wake: Arc<Notify>) {
    let mut ticker = tokio::time::interval(Duration::from_secs(5));
    ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    let mut published = None;
    while !ctx.is_shutting_down.load(Ordering::Relaxed) {
        ticker.tick().await;
        if !ctx.scheduler_running.load(Ordering::Relaxed) {
            continue;
        }
        if let Err(error) = reconcile_rounds(&ctx, &wake, Local::now(), &mut published).await {
            tracing::warn!("定时闭合扫描调度失败: {error:#}");
        }
    }
}

async fn reconcile_rounds(
    ctx: &Arc<ServerContext>,
    wake: &Notify,
    now: DateTime<Local>,
    published: &mut Option<NaiveDateTime>,
) -> Result<()> {
    let wall = now.naive_local();
    let grid = wall
        .date()
        .and_hms_opt(wall.hour(), wall.minute() / 15 * 15, 0)
        .expect("valid 15-minute grid");
    let symbols = repo::list_symbols(&ctx.db, true).await?;
    // Recover the preceding grid even if startup missed its normal timer.
    // Older unacknowledged work remains in the durable queue.
    for target in [grid - chrono::Duration::minutes(15), grid] {
        let expected: Vec<_> = symbols
            .iter()
            .map(|row| row.code.as_str())
            .filter(|symbol| expected_5m_close(symbol, target))
            .collect();
        let settled: Vec<_> = expected
            .iter()
            .copied()
            .filter(|symbol| symbol_scan_settled(symbol, target, wall))
            .collect();
        if settled.is_empty() {
            continue;
        }
        let bar_end = target.format(BAR_FORMAT).to_string();
        if scan_jobs::enqueue_scheduled(&ctx.db, &settled, &bar_end, now.timestamp()).await? {
            wake.notify_waiters();
        }
        if settled.len() != expected.len()
            || published.is_some_and(|bar| bar >= target)
            || scan_jobs::remaining_for_bar(&ctx.db, &bar_end).await? != 0
        {
            continue;
        }
        {
            let _publication = ctx.scan_publication.lock().await;
            let snapshot = ctx
                .services
                .scheduled_scan_snapshot(expected.len() as i64)
                .await?;
            ctx.realtime_hub
                .broadcast(
                    ctx.next_event_seq(),
                    "scan.completed",
                    serde_json::to_value(&snapshot)?,
                )
                .await;
            *ctx.last_scan.write().await = Some(Local::now().naive_local());
        }
        super::reconcile_preclose(ctx).await;
        *published = Some(target);
        tracing::info!(
            bar_end,
            scanned = expected.len(),
            "[SCAN_QUEUE] scheduled round complete"
        );
        let cutoff = (target - chrono::Duration::days(7))
            .format(BAR_FORMAT)
            .to_string();
        scan_jobs::prune_completed(&ctx.db, &cutoff).await?;
    }
    Ok(())
}
