mod outbox_dispatch;
mod scan_queue;

use crate::state::ServerContext;
use chrono::{Local, Timelike};
use n_core::notify::email::{
    event_email_payload_with_model, preclose_candidate_email_payload, preclose_email_payload,
    single_bar_email_payload, EventEmailKind,
};
use n_core::scheduler::CloseScanMode;
use n_core::service::{RefreshStats, ScanResult};
use n_core::trader::model::{ExecutionQuote, ReceivedExecutionQuote};
use n_protocol::NewNotificationHistoryItem;
use std::sync::Arc;
use std::time::{Duration, Instant};

type QuoteBatch = Vec<ReceivedExecutionQuote>;

/// Drain only the batches already waiting; retain every tick and its order.
pub(crate) fn drain_quote_batches(
    rx: &mut tokio::sync::mpsc::Receiver<QuoteBatch>,
    mut events: QuoteBatch,
) -> QuoteBatch {
    let waiting = rx.len();
    for _ in 0..waiting {
        if let Ok(batch) = rx.try_recv() {
            events.extend(batch);
        } else {
            break;
        }
    }
    events
}

fn filter_quote_events(
    events: &QuoteBatch,
    subscriptions: &std::collections::HashSet<String>,
) -> QuoteBatch {
    events
        .iter()
        .filter(|event| {
            subscriptions.contains(&event.symbol.to_uppercase())
                || subscriptions.contains(&event.quote.instrument_id.to_uppercase())
        })
        .cloned()
        .collect()
}

async fn handle_quote_gap(quote_ctx: &ServerContext, reason: &str) {
    if let Some(manager) = quote_ctx.instances.get() {
        for r in manager.all().await {
            if let Err(e) = r.engine.quote_gap_with_reason(reason).await {
                tracing::error!("实例 {} 行情缺口保存失败: {e}", r.definition.id);
            }
        }
    } else if let Err(e) = quote_ctx.execution_engine.quote_gap_with_reason(reason).await {
        tracing::error!("行情缺口保存失败: {e}");
    }
}

async fn handle_market_recovered(quote_ctx: &ServerContext) {
    if let Some(manager) = quote_ctx.instances.get() {
        for r in manager.all().await {
            if r.engine.market_recovered().await {
                tracing::info!(
                    instance_id = %r.definition.id,
                    "执行行情连接恢复，报单仍需本合约有效新盘口"
                );
            }
        }
    } else if quote_ctx.execution_engine.market_recovered().await {
        tracing::info!("执行行情连接恢复，报单仍需本合约有效新盘口");
    }
}

async fn handle_market_unavailable(quote_ctx: &ServerContext) {
    quote_ctx.execution_engine.market_unavailable().await;
    if let Some(manager) = quote_ctx.instances.get() {
        for r in manager.all().await {
            r.engine.market_unavailable().await;
        }
    }
}

async fn dispatch_quote_events(
    quote_ctx: &ServerContext,
    events: Vec<(String, ExecutionQuote)>,
    received_at: i64,
) {
    if events.is_empty() {
        return;
    }
    let events = events.into_iter().map(|(symbol, quote)| ReceivedExecutionQuote {
        symbol, quote, received_at,
    }).collect();
    if let Some(manager) = quote_ctx.instances.get() {
        for r in manager.all().await {
            let mut subscriptions = r.engine.requested_contracts().await;
            subscriptions.extend(
                r.engine
                    .market_subscriptions()
                    .await
                    .into_iter()
                    .map(|s| s.symbol),
            );
            let subscriptions = subscriptions
                .into_iter()
                .map(|s| s.to_uppercase())
                .collect();
            let events = filter_quote_events(&events, &subscriptions);
            if events.is_empty() {
                continue;
            }
            match r.quote_tx.try_send(events) {
                Ok(()) => {}
                Err(tokio::sync::mpsc::error::TrySendError::Full(events)) => {
                    if !r.quote_gap.swap(true, std::sync::atomic::Ordering::Relaxed) {
                        tracing::warn!(
                            instance_id = %r.definition.id,
                            dropped_ticks = events.len(),
                            "本实例行情消费队列已满，启动缺口保护"
                        );
                    }
                }
                Err(tokio::sync::mpsc::error::TrySendError::Closed(_)) => {
                    tracing::warn!(instance_id = %r.definition.id, "实例行情消费者已关闭");
                }
            }
        }
    }
    if quote_ctx.instances.get().is_none() {
        if let Err(e) = quote_ctx.execution_engine.on_received_quote_batch(events).await {
            tracing::warn!("执行行情推进失败: {e}");
        }
    }
}

struct SignalRegistrationTarget {
    engine: Arc<n_core::trader::ExecutionEngine>,
    created_at: Option<i64>,
    registered: std::collections::HashSet<i64>,
}

impl SignalRegistrationTarget {
    fn needs(&self, id: i64, warning_ts: &str) -> bool {
        !self.registered.contains(&id)
            && self.created_at.is_none_or(|created| {
                chrono::NaiveDateTime::parse_from_str(warning_ts, "%Y-%m-%d %H:%M:%S")
                    .ok()
                    .and_then(|d| d.and_local_timezone(Local).single())
                    .is_some_and(|d| d.timestamp() >= created)
            })
    }
}

fn registered_event_ids(
    conditions: Vec<n_core::trader::ConditionOrder>,
    strategy_id: &str,
) -> std::collections::HashSet<i64> {
    conditions
        .into_iter()
        .filter(|c| c.origin.strategy_id == strategy_id)
        .filter_map(|c| c.event_id)
        .collect()
}

pub fn spawn_runtime_tasks(ctx: Arc<ServerContext>, close_scan_mode: CloseScanMode) {
    if !n_core::storage::outbox::LEGACY_DIRECT_PUBLISH {
        outbox_dispatch::spawn(ctx.clone());
    }
    let scan_wake = Arc::new(tokio::sync::Notify::new());
    scan_queue::spawn(ctx.clone(), scan_wake.clone(), close_scan_mode);
    spawn_review_worker(ctx.clone());
    spawn_scheduler(ctx.clone());
    spawn_scan_maintenance(ctx.clone(), close_scan_mode);
    spawn_quote_poller(ctx.clone());
    spawn_live_chart_publisher(ctx.clone());
    spawn_chart_recovery_publisher(ctx.clone());
    spawn_tq_bar_event_consumer(ctx.clone(), scan_wake);
    spawn_tq_warmup(ctx.clone());
    spawn_trader_stream(ctx.clone());
    spawn_execution(ctx.clone());
    spawn_p3_shadow_history(ctx.clone());
    crate::instance_runtime::spawn(ctx);
}

fn spawn_chart_recovery_publisher(ctx: Arc<ServerContext>) {
    let mut updates = ctx.services.subscribe_chart_recovery();
    let owner = Arc::downgrade(&ctx);
    tokio::spawn(async move {
        loop {
            let update = updates.recv().await;
            let Some(ctx) = owner.upgrade() else { break };
            match update {
                Ok(update) => {
                    let symbol = update.symbol.clone();
                    ctx.realtime_hub.broadcast_chart_history(
                        ctx.next_event_seq(), &symbol, serde_json::json!(update),
                    ).await;
                }
                Err(tokio::sync::broadcast::error::RecvError::Lagged(_)) => {
                    broadcast_data_updated(&ctx, 0, 0).await;
                }
                Err(tokio::sync::broadcast::error::RecvError::Closed) => break,
            }
        }
    });
}

fn spawn_p3_shadow_history(ctx: Arc<ServerContext>) {
    use std::collections::{BTreeMap, HashMap};

    use n_core::fetch::kline::Kline;
    use n_core::session::SessionCalendar;
    use n_core::trader::ExecutionEngine;

    tokio::spawn(async move {
        let mut history = HashMap::<String, (i64, Vec<Kline>)>::new();
        let mut retry_at = HashMap::<String, Instant>::new();
        let mut interval = tokio::time::interval(Duration::from_secs(5));
        interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        let shanghai = chrono::FixedOffset::east_opt(8 * 3600).unwrap();
        loop {
            interval.tick().await;
            if ctx.is_shutting_down.load(std::sync::atomic::Ordering::Relaxed) {
                break;
            }
            let engines = if let Some(manager) = ctx.instances.get() {
                manager.all().await.into_iter().map(|r| r.engine.clone()).collect::<Vec<_>>()
            } else {
                vec![ctx.execution_engine.clone()]
            };
            let mut subscriptions = BTreeMap::<String, Vec<Arc<ExecutionEngine>>>::new();
            for engine in engines {
                for symbol in engine.p3_shadow_history_symbols().await {
                    subscriptions.entry(symbol).or_default().push(engine.clone());
                }
            }
            history.retain(|symbol, _| subscriptions.contains_key(symbol));
            retry_at.retain(|symbol, _| subscriptions.contains_key(symbol));

            let now = chrono::Utc::now().timestamp();
            let bucket = now - now.rem_euclid(900);
            let before_close = chrono::DateTime::from_timestamp(bucket - 1, 0).unwrap()
                .with_timezone(&shanghai).naive_local();
            for (symbol, watchers) in subscriptions {
                let instrument = symbol.split_once('.').map_or(symbol.as_str(), |(_, code)| code);
                let due = history.get(&symbol).is_none_or(|(loaded, _)| {
                    *loaded < bucket && SessionCalendar::get(instrument).is_in_trading_time_naive(&before_close)
                });
                if due && retry_at.get(&symbol).is_none_or(|time| Instant::now() >= *time) {
                    retry_at.insert(symbol.clone(), Instant::now() + Duration::from_secs(10));
                    let fetch = ctx.services.data_source.fetch_tq_minute(&symbol, "5m", 240);
                    if let Ok(Ok(raw)) = tokio::time::timeout(Duration::from_secs(5), fetch).await {
                        let latest = raw.last().and_then(|bar| n_core::trader::candidates::parse_time(&bar.datetime).ok());
                        if let Some(latest) = latest {
                            history.insert(symbol.clone(), (latest, raw));
                        }
                    }
                }
                let bars = history.get(&symbol).map_or(&[][..], |(_, bars)| bars.as_slice());
                for engine in watchers {
                    if let Err(error) = engine.on_p3_shadow_history(&symbol, bars).await {
                        tracing::warn!(%symbol, %error, "P3影子收盘行情保存失败");
                    }
                }
            }
        }
    });
}

fn spawn_review_worker(ctx: Arc<ServerContext>) {
    tokio::spawn(async move {
        let mut calendar_check = Instant::now() - Duration::from_secs(60);
        let mut previous = String::new();
        loop {
            if ctx
                .is_shutting_down
                .load(std::sync::atomic::Ordering::SeqCst)
            {
                break;
            }
            if calendar_check.elapsed() >= Duration::from_secs(60) {
                if let Err(e) = ctx.review.initialize().await {
                    tracing::error!("review initialize: {e:#}");
                }
                calendar_check = Instant::now();
            }
            let mut busy = false;
            match ctx.review.worker_step().await {
                Ok(Some(job)) => {
                    busy = job.status == "running";
                    let key = format!("{}:{}:{}", job.id, job.status, job.completed_symbols);
                    if key != previous {
                        previous = key;
                        let seq = ctx.next_event_seq();
                        ctx.realtime_hub.broadcast(seq,"review.changed",serde_json::json!({"job_id":job.id,"status":job.status,"completed_symbols":job.completed_symbols,"total_symbols":job.total_symbols,"revision":job.revision})).await;
                    }
                }
                Ok(None) => {}
                Err(e) => tracing::error!("review worker: {e:#}"),
            }
            tokio::time::sleep(Duration::from_millis(if busy { 100 } else { 5000 })).await;
        }
    });
}

pub async fn register_execution_signals(
    ctx: &Arc<ServerContext>,
    signals: &[n_core::storage::entities::pattern_events::Model],
) {
    // Covers both the periodic fallback and R4a delivery worker. It ensures
    // both callers re-check persisted conditions before dispatching a signal.
    let _registration_guard = ctx.execution_registration_lock.lock().await;
    // Deduplication is per instance, never global across accounts.
    let not_before =
        match n_core::storage::repo::get_setting(&ctx.db, "execution_signal_not_before").await {
            Ok(Some(value)) => match value.parse::<i64>() {
                Ok(ts) => ts,
                Err(_) => {
                    tracing::error!("执行预警时间界限无效，停止注册新入场");
                    return;
                }
            },
            Ok(None) => 0,
            Err(error) => {
                tracing::error!("无法读取执行预警时间界限: {error}");
                return;
            }
        };
    let enabled = n_core::storage::repo::list_symbols(&ctx.db, true)
        .await
        .unwrap_or_default();
    let engines = if let Some(manager) = ctx.instances.get() {
        manager
            .all()
            .await
            .into_iter()
            .map(|r| {
                let created = (r.definition.id != "default").then_some(r.definition.created_at);
                (r.engine.clone(), created)
            })
            .collect::<Vec<_>>()
    } else {
        vec![(ctx.execution_engine.clone(), None)]
    };
    let mut targets = Vec::new();
    for (engine, created_at) in engines {
        if engine.paused().await {
            continue;
        }
        let strategy_id = engine.definition().await.strategy_id;
        let registered = registered_event_ids(engine.list_conditions().await, &strategy_id);
        targets.push(SignalRegistrationTarget {
            engine,
            created_at,
            registered,
        });
    }
    let signals = signals
        .iter()
        .filter(|s| {
            s.entry_score >= 2.5
                && n_core::trader::candidates::parse_time(&s.warning_ts)
                    .is_ok_and(|ts| ts >= not_before)
                && matches!(s.direction.as_str(), "up" | "down")
                && enabled.iter().any(|e| e.code == s.symbol)
                && targets.iter().any(|t| t.needs(s.id, &s.warning_ts))
        })
        .collect::<Vec<_>>();
    if signals.is_empty() {
        return;
    }
    let tq = ctx.services.data_source.tq_client().await;
    let codes = signals
        .iter()
        .map(|s| s.symbol.clone())
        .collect::<std::collections::BTreeSet<_>>()
        .into_iter()
        .collect::<Vec<_>>();
    let Ok(quotes) = tq.execution_quotes(&codes).await else {
        return;
    };
    let mut bar_times = std::collections::HashMap::new();
    let mut raw_bars = std::collections::HashMap::new();
    for code in &codes {
        if let Ok(bars) = ctx.services.shared_bars_for(code, "15m").await {
            bar_times.insert(
                code.clone(),
                bars.iter().map(|b| b.dt.to_bar_ts()).collect::<Vec<_>>(),
            );
        }
        if let Ok(rows) = ctx.services.shared_raw_for(code).await {
            raw_bars.insert(code.clone(), rows);
        }
    }
    for signal in signals {
        let Some(times) = bar_times
            .get(&signal.symbol)
            .filter(|times| !times.is_empty())
        else {
            continue;
        };
        if times
            .iter()
            .filter(|ts| ts.as_str() > signal.warning_ts.as_str())
            .count()
            > n_core::analyze::outcome::PENDING_BARS
        {
            continue;
        }
        let Some(q) = quotes.get(&signal.symbol.to_uppercase()) else {
            continue;
        };
        let actual = if q.underlying_symbol.is_empty() {
            &q.instrument_id
        } else {
            q.underlying_symbol.rsplit('.').next().unwrap_or("")
        };
        if !n_core::trader::model::is_actual_futures_contract(actual)
            || !(0..=15).contains(&(chrono::Utc::now().timestamp() - q.timestamp))
        {
            continue;
        }
        let warning_context = raw_bars.get(&signal.symbol).and_then(|raw| {
            n_core::trader::candidates::warning_context(raw, &signal.warning_ts)
                .ok()
                .flatten()
        });
        for target in &targets {
            let engine = &target.engine;
            if !target.needs(signal.id, &signal.warning_ts) || engine.paused().await {
                continue;
            }
            let mut payload = serde_json::to_value(signal).unwrap_or_default();
            payload["source_evidence"] = payload.clone();
            payload["instrument_id"] = serde_json::json!(actual);
            payload["created_at_epoch"] = serde_json::json!(chrono::Utc::now().timestamp());
            payload["tick_size"] = serde_json::json!(q.tick);
            payload["multiplier"] = serde_json::json!(engine.broker_client().multiplier(actual));
            payload["warning_context"] = serde_json::json!(warning_context);
            if let Err(e) = engine
                .dispatch(n_core::trader::strategy::StrategyEvent::Signal {
                    source: n_core::trader::strategy::N_STRATEGY.into(),
                    payload,
                })
                .await
            {
                tracing::warn!("预警 {} 注册交易任务失败: {e}", signal.id);
            }
        }
    }
}

fn spawn_execution(ctx: Arc<ServerContext>) {
    let worker = ctx.clone();
    tokio::spawn(async move {
        let Some(manager) = worker.instances.get() else {
            return;
        };
        let Ok(r) = manager.get("default").await else {
            return;
        };
        let Some(mut rx) = r.quote_rx.lock().await.take() else {
            return;
        };
        while let Some(events) = rx.recv().await {
            let events = drain_quote_batches(&mut rx, events);
            let batch_quotes = events.len();
            let processing_started = Instant::now();
            if worker
                .is_shutting_down
                .load(std::sync::atomic::Ordering::Relaxed)
            {
                break;
            }
            if r.quote_gap
                .swap(false, std::sync::atomic::Ordering::Relaxed)
            {
                let _ = r
                    .engine
                    .quote_gap_with_reason("本实例行情消费队列已满")
                    .await;
            }
            if let Err(e) = r.engine.on_received_quote_batch(events).await {
                tracing::warn!("默认实例行情推进失败: {e}");
            }
            if processing_started.elapsed() >= Duration::from_millis(250) {
                tracing::warn!(
                    instance_id = "default",
                    batch_quotes,
                    processing_ms = processing_started.elapsed().as_millis(),
                    quote_queue_batches = rx.len(),
                    "实例行情处理较慢"
                );
            }
        }
    });
    let quote_ctx = ctx.clone();
    tokio::spawn(async move {
        let mut cursor = 0;
        let mut stream_id = String::new();
        let mut active_source = None;
        loop {
            if quote_ctx
                .is_shutting_down
                .load(std::sync::atomic::Ordering::Relaxed)
            {
                break;
            }
            let mut codes = quote_ctx.execution_engine.requested_contracts().await;
            if let Some(manager) = quote_ctx.instances.get() {
                for r in manager.all().await {
                    codes.extend(r.engine.requested_contracts().await);
                    codes.extend(
                        r.engine
                            .market_subscriptions()
                            .await
                            .into_iter()
                            .map(|s| s.symbol),
                    );
                }
            }
            codes.sort();
            codes.dedup();
            if codes.is_empty() {
                tokio::time::sleep(Duration::from_secs(1)).await;
                continue;
            }

            let quote_source = if std::env::var("NTREND_QUOTE_SOURCE")
                .map(|v| v.eq_ignore_ascii_case("ctp"))
                .unwrap_or(false)
            {
                n_core::trader::QuoteSource::Ctp
            } else if let Some(manager) = quote_ctx.instances.get() {
                let runtimes = manager.all().await;
                if runtimes.is_empty() {
                    quote_ctx.execution_engine.get_config().await.quote_source
                } else {
                    let mut any_ctp = false;
                    for r in runtimes {
                        if r.engine.get_config().await.quote_source == n_core::trader::QuoteSource::Ctp {
                            any_ctp = true;
                            break;
                        }
                    }
                    if any_ctp {
                        n_core::trader::QuoteSource::Ctp
                    } else {
                        n_core::trader::QuoteSource::Tianqin
                    }
                }
            } else {
                quote_ctx.execution_engine.get_config().await.quote_source
            };

            if active_source != Some(quote_source) {
                tracing::info!(?quote_source, "执行行情源已切换，重置事件流游标");
                active_source = Some(quote_source);
                stream_id.clear();
                cursor = 0;
            }

            match quote_source {
                n_core::trader::QuoteSource::Tianqin => {
                    let tq = quote_ctx.services.data_source.tq_client().await;
                    match tq.execution_events(&codes, cursor, &stream_id).await {
                        Ok(batch) => {
                            let received_at = chrono::Utc::now().timestamp();
                            let restarted = !stream_id.is_empty()
                                && batch["stream_id"]
                                    .as_str()
                                    .is_some_and(|id| id != stream_id);
                            if batch["gap"].as_bool().unwrap_or(false) || restarted {
                                let reason = if restarted {
                                    "天勤行情桥事件流已重启"
                                } else {
                                    "天勤行情源事件缓存已覆盖未消费数据"
                                };
                                tracing::warn!(cursor, stream_id, reason, "天勤执行行情源需要重新同步");
                                handle_quote_gap(&quote_ctx, reason).await;
                                stream_id.clear();
                                cursor = 0;
                                continue;
                            }
                            stream_id = batch["stream_id"].as_str().unwrap_or("").into();
                            cursor = batch["latest_event_id"].as_u64().unwrap_or(0);
                            handle_market_recovered(&quote_ctx).await;

                            let events: Vec<(String, n_core::trader::model::ExecutionQuote)> = batch
                                ["events"]
                                .as_array()
                                .into_iter()
                                .flatten()
                                .map(|event| {
                                    let code = event["code"].as_str().unwrap_or("");
                                    (
                                        code.to_uppercase(),
                                        n_core::fetch::tq_client::TqBridgeClient::decode_execution_quote(
                                            code,
                                            &event["quote"],
                                        ),
                                    )
                                })
                                .collect();
                            dispatch_quote_events(&quote_ctx, events, received_at).await;
                        }
                        Err(e) => {
                            handle_market_unavailable(&quote_ctx).await;
                            tracing::warn!("天勤执行行情连接失败: {e}");
                            tokio::time::sleep(Duration::from_secs(3)).await;
                        }
                    }
                }
                n_core::trader::QuoteSource::Ctp => {
                    let mut port = 8766;
                    if let Some(manager) = quote_ctx.instances.get() {
                        for r in manager.all().await {
                            if let n_core::trader::instances::AccountKind::Ctp { port: p, .. } = r.account.kind {
                                port = p;
                                break;
                            }
                        }
                    }
                    let ctp = n_core::trader::CtpTraderClient::for_port(port);
                    let _ = ctp.subscribe_market(&codes).await;
                    match ctp.fetch_market_ticks(&stream_id, cursor, 500).await {
                        Ok(batch) => {
                            let received_at = chrono::Utc::now().timestamp();
                            let restarted = !stream_id.is_empty() && batch.stream_id != stream_id;
                            if batch.gap || restarted {
                                let reason = if restarted {
                                    "CTP行情桥事件流已重启"
                                } else {
                                    "CTP行情源环形缓存已覆盖未消费数据"
                                };
                                tracing::warn!(cursor, stream_id = %batch.stream_id, reason, "CTP执行行情源需要重新同步");
                                handle_quote_gap(&quote_ctx, reason).await;
                                stream_id.clear();
                                cursor = 0;
                                continue;
                            }
                            stream_id = batch.stream_id;
                            cursor = batch.latest_seq_id;
                            handle_market_recovered(&quote_ctx).await;

                            let mut events = Vec::new();
                            for ev in batch.events {
                                match ev {
                                    n_core::trader::CtpMarketEvent::Status { ref status, .. } => {
                                        if status == "disconnected" {
                                            tracing::warn!("收到 CTP 行情前置断开事件，标记行情不可用");
                                            handle_market_unavailable(&quote_ctx).await;
                                        } else if status == "logged_in" {
                                            tracing::info!("收到 CTP 行情前置登录成功事件");
                                            handle_market_recovered(&quote_ctx).await;
                                        }
                                    }
                                    n_core::trader::CtpMarketEvent::Tick {
                                        ref instrument_id,
                                        last_price,
                                        volume,
                                        open_interest,
                                        ref trading_day,
                                        ..
                                    } => {
                                        let now_dt = chrono::Local::now().naive_local();
                                        let updated_bars = quote_ctx.services.live_chart.update_ctp_tick(
                                            instrument_id,
                                            last_price,
                                            volume,
                                            open_interest,
                                            trading_day,
                                            now_dt,
                                        );
                                        for bar in updated_bars {
                                            let sym = bar.symbol.clone();
                                            let tf = bar.timeframe.clone();
                                            quote_ctx
                                                .realtime_hub
                                                .broadcast_kline(
                                                    quote_ctx.next_event_seq(),
                                                    "kline.partial",
                                                    &sym,
                                                    &tf,
                                                    serde_json::json!({ "symbol": sym, "timeframe": tf, "bar": bar }),
                                                )
                                                .await;
                                        }

                                        if let Some(quote) = ev.to_execution_quote() {
                                            if quote.valid_at(received_at) {
                                                events.push((quote.instrument_id.to_uppercase(), quote));
                                            }
                                        }
                                    }
                                    _ => {}
                                }
                            }
                            dispatch_quote_events(&quote_ctx, events, received_at).await;
                            if cursor == 0 {
                                tokio::time::sleep(Duration::from_millis(50)).await;
                            }
                        }
                        Err(e) => {
                            handle_market_unavailable(&quote_ctx).await;
                            tracing::warn!("CTP执行行情连接失败: {e}");
                            tokio::time::sleep(Duration::from_secs(2)).await;
                        }
                    }
                }
            }
        }
    });
    tokio::spawn(async move {
        let mut timer = tokio::time::interval(Duration::from_secs(1));
        timer.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        let mut next_sync = Instant::now();
        let mut history = None;
        loop {
            timer.tick().await;
            if ctx
                .is_shutting_down
                .load(std::sync::atomic::Ordering::Relaxed)
            {
                break;
            }
            if let Err(e) = ctx
                .execution_engine
                .dispatch(n_core::trader::strategy::StrategyEvent::Timer {
                    timestamp: chrono::Utc::now().timestamp(),
                })
                .await
            {
                let _ = ctx.execution_engine.pause(true).await;
                tracing::error!("默认实例策略处理失败: {e}");
            }
            if let Err(e) = ctx.execution_engine.drive().await {
                tracing::warn!("委托维护失败: {e}");
            }
            if history.is_none() {
                if let Some(manager) = ctx.instances.get() {
                    if let Ok(runtime) = manager.get("default").await {
                        history = Some(crate::instance_runtime::DefaultHistory::spawn(
                            ctx.clone(),
                            runtime,
                        ));
                    }
                }
            }
            if let Some(history) = history.as_mut() {
                if let Err(error) = history.apply_ready(&ctx.execution_engine).await {
                    tracing::warn!(%error, "默认实例后台历史更新失败，将重试；继续维护委托");
                }
            }
            if Instant::now() < next_sync {
                continue;
            }
            next_sync = Instant::now() + Duration::from_secs(5);
            if !n_core::scheduler::is_trading_time(&Local::now()) {
                continue;
            }
            if let Ok(signals) = n_core::storage::repo::pending_execution_signals(&ctx.db).await {
                register_execution_signals(&ctx, &signals).await;
            }
        }
    });
}

fn spawn_trader_stream(ctx: Arc<ServerContext>) {
    // watch coalesces bursts: a slow consumer always receives the latest snapshot.
    let (tx, rx) = tokio::sync::watch::channel(serde_json::Value::Null);
    let bridge_ctx = ctx.clone();
    tokio::spawn(async move {
        let client = n_core::trader::CtpTraderClient::for_port(8766);
        let mut backoff = 1;
        loop {
            if bridge_ctx
                .is_shutting_down
                .load(std::sync::atomic::Ordering::Relaxed)
            {
                break;
            }
            let result: anyhow::Result<()> = async {
                let mut stream = client.event_stream().await?;
                loop {
                    let mut snapshot = stream.next_snapshot().await?;
                    // Uptime is transport metadata, not a trading state change.
                    if let Some(health) = snapshot["health"].as_object_mut() {
                        health.remove("uptime");
                    }
                    snapshot["bridge_online"] = serde_json::json!(true);
                    snapshot["source"] = serde_json::json!("server");
                    if let Some(manager) = bridge_ctx.instances.get() {
                        manager.validate_snapshot("default", &snapshot).await?;
                    }
                    let default_name = if let Some(manager) = bridge_ctx.instances.get() {
                        if let Ok(r) = manager.get("default").await {
                            let n = r.definition.name.trim();
                            if !n.is_empty() {
                                n.to_string()
                            } else {
                                "默认实盘".to_string()
                            }
                        } else {
                            "默认实盘".to_string()
                        }
                    } else {
                        "默认实盘".to_string()
                    };
                    match bridge_ctx.execution_engine.reconcile(&snapshot).await {
                        Ok(evs) => {
                            crate::instance_runtime::dispatch_live_trade_emails(&bridge_ctx, &default_name, evs);
                        }
                        Err(e) => {
                            tracing::error!("柜台状态恢复失败，执行任务未完成: {e}");
                        }
                    }
                    if let Err(e) = bridge_ctx
                        .execution_engine
                        .dispatch(n_core::trader::strategy::StrategyEvent::ExecutionUpdate(
                            snapshot.clone(),
                        ))
                        .await
                    {
                        let _ = bridge_ctx.execution_engine.pause(true).await;
                        tracing::error!("策略成交事件处理失败: {e}");
                    }
                    tx.send_replace(snapshot);
                    backoff = 1;
                    if bridge_ctx
                        .is_shutting_down
                        .load(std::sync::atomic::Ordering::Relaxed)
                    {
                        return Ok(());
                    }
                }
            }
            .await;
            if let Err(error) = result {
                tracing::warn!("CTP 快照推送中断，{} 秒后重连: {}", backoff, error);
                let mut snapshot = tx.borrow().clone();
                if snapshot.is_null() {
                    snapshot = serde_json::json!({"health": {}, "broker_positions": []});
                }
                snapshot["source"] = serde_json::json!("server");
                snapshot["bridge_online"] = serde_json::json!(false);
                snapshot["health"]["ready"] = serde_json::json!(false);
                snapshot["health"]["healthy"] = serde_json::json!(false);
                snapshot["health"]["sync_error"] =
                    serde_json::json!("桥接服务连接中断，显示最后成功快照");
                let _ = bridge_ctx.execution_engine.reconcile(&snapshot).await;
                tx.send_replace(snapshot);
            }
            tokio::time::sleep(Duration::from_secs(backoff)).await;
            backoff = (backoff * 2).min(30);
        }
    });
    tokio::spawn(async move {
        let mut ticker = tokio::time::interval(Duration::from_secs(1));
        ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        let mut publisher =
            n_protocol::trader_stream::TraderPublisher::new(uuid::Uuid::new_v4().to_string());
        let mut fields = n_core::trader::execution::ExecutionSnapshotCache::default();
        let mut calendar_warnings = std::collections::HashSet::new();
        let mut last_sent = Instant::now();
        loop {
            // Broker updates coalesce in watch; UI snapshots have a fixed
            // cadence instead of rebuilding the full archive on every update.
            ticker.tick().await;
            if rx.has_changed().is_err() {
                break;
            }
            if ctx
                .is_shutting_down
                .load(std::sync::atomic::Ordering::Relaxed)
            {
                break;
            }
            let mut snapshot = rx.borrow().clone();
            if snapshot.is_null() {
                continue;
            }
            snapshot["instance_id"] = serde_json::json!("default");
            snapshot["account_id"] = serde_json::json!("default");
            snapshot["account_kind"] = serde_json::json!("ctp");
            if let Some(manager) = ctx.instances.get() {
                if let Ok(r) = manager.get("default").await {
                    *r.snapshot.write().await = snapshot.clone();
                }
                let _ = manager.sample_equity("default", &snapshot).await;
            }
            let execution_fields = match ctx
                .execution_engine
                .snapshot_fields(&mut fields, false)
                .await
            {
                Ok(fields) => fields,
                Err(error) => {
                    tracing::warn!(%error, "默认实例快照生成失败");
                    continue;
                }
            };
            let calendar = execution_fields
                .object("strategy_diagnostics")
                .and_then(|fields| fields.get("calendar_health"));
            crate::instance_runtime::warn_calendar_coverage(
                "default",
                calendar,
                &mut calendar_warnings,
            );
            let mut snapshot =
                n_protocol::trader_stream::SharedTraderSnapshot::from_value(snapshot);
            snapshot.extend(execution_fields);
            let (snapshot, delta, _) = publisher.update_shared(snapshot, false);
            if let Some(manager) = ctx.instances.get() {
                if let Ok(r) = manager.get("default").await {
                    *r.published.write().await = snapshot.clone();
                }
            }
            // Keep the REST/bootstrap baseline fresh, including query timestamps.
            *ctx.trader_snapshot.write().await = snapshot.clone();
            if let Some(delta) = delta {
                ctx.realtime_hub
                    .broadcast_trader(ctx.next_event_seq(), &delta, &snapshot)
                    .await;
                last_sent = Instant::now();
            } else if last_sent.elapsed() >= Duration::from_secs(15) {
                ctx.realtime_hub
                    .broadcast(
                        ctx.next_event_seq(),
                        "trader.heartbeat",
                        publisher.heartbeat(),
                    )
                    .await;
                last_sent = Instant::now();
            }
        }
    });
}

fn spawn_scheduler(ctx: Arc<ServerContext>) {
    tokio::spawn(async move {
        let mut ticker = tokio::time::interval(Duration::from_secs(15));
        ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        let mut startup_done = false;
        let mut last_trading_status: Option<n_core::session::TradingStatus> = None;
        let mut last_refresh: Option<chrono::DateTime<Local>> = None;

        loop {
            ticker.tick().await;
            if ctx
                .is_shutting_down
                .load(std::sync::atomic::Ordering::Relaxed)
            {
                break;
            }
            if !ctx
                .scheduler_running
                .load(std::sync::atomic::Ordering::Relaxed)
            {
                continue;
            }

            let now = Local::now();
            let cfg = ctx.services.config().await.scheduler.clone();

            let current_status = n_core::session::SessionCalendar::current_trading_status(&now);
            if last_trading_status != Some(current_status) {
                match current_status {
                    n_core::session::TradingStatus::Trading => {
                        tracing::info!(
                            "☀️ [服务端调度器] 交易时段已开启，恢复常规行情刷新与扫描调度"
                        );
                    }
                    n_core::session::TradingStatus::HolidayOff => {
                        tracing::info!(
                            "🌙 [服务端调度器] 当前处于法定节假日休市时段，进入静默休眠"
                        );
                    }
                    n_core::session::TradingStatus::PreHolidayNightOff => {
                        tracing::info!(
                            "🌙 [服务端调度器] 当前处于节假日前夕（今晚无夜盘），进入静默休眠"
                        );
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

            let startup_refresh = !startup_done && n_core::scheduler::is_trading_time(&now);
            startup_done = true;
            if startup_refresh || n_core::scheduler::regular_refresh_due(now, &cfg, last_refresh) {
                if let Ok(stats) = run_refresh(&ctx).await {
                    last_refresh = Some(now);
                    broadcast_data_updated(&ctx, stats.succeeded, stats.failures).await;
                }
            }

        }
    });
}

fn spawn_scan_maintenance(ctx: Arc<ServerContext>, mode: CloseScanMode) {
    tokio::spawn(async move {
        let mut ticker = tokio::time::interval(Duration::from_secs(15));
        ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        let mut last_maintenance = None;
        loop {
            ticker.tick().await;
            if ctx.is_shutting_down.load(std::sync::atomic::Ordering::Relaxed) {
                break;
            }
            if !ctx.scheduler_running.load(std::sync::atomic::Ordering::Relaxed) {
                continue;
            }
            // Keep full-history maintenance away from the 15m close.
            let now = Local::now();
            let minute = now.minute() % 15;
            let grid = now.timestamp() / 900;
            if (6..10).contains(&minute)
                && last_maintenance != Some(grid)
                && n_core::scheduler::is_trading_time(&now)
            {
                match ctx.services.try_scan_maintenance(mode).await {
                    Ok(true) => last_maintenance = Some(grid),
                    Ok(false) => {}
                    Err(err) => {
                        last_maintenance = Some(grid);
                        tracing::warn!("定时扫描维护失败: {err:#}");
                    }
                }
            }
        }
    });
}

pub(crate) async fn broadcast_data_updated(ctx: &ServerContext, succeeded: usize, failures: usize) {
    let mut payload = serde_json::json!({ "succeeded": succeeded, "failures": failures });
    match n_core::storage::cache_revisions::market_history_revisions(&ctx.db).await {
        Ok(revisions) => payload["history_revisions"] = serde_json::json!(revisions),
        Err(error) => tracing::warn!(%error, "无法读取行情刷新后的历史修订号"),
    }
    ctx.realtime_hub
        .broadcast(ctx.next_event_seq(), "data.updated", payload)
        .await;
}

async fn run_refresh(ctx: &Arc<ServerContext>) -> anyhow::Result<RefreshStats> {
    let t0 = Instant::now();
    tracing::info!("🔄 刷新触发 | {}", Local::now().format("%H:%M:%S"));
    let mut strategy_symbols = Vec::new();
    if let Some(manager) = ctx.instances.get() {
        for instance in manager.all().await {
            strategy_symbols.extend(
                instance
                    .engine
                    .market_subscriptions()
                    .await
                    .into_iter()
                    .map(|s| s.symbol),
            );
        }
    }
    strategy_symbols.sort();
    strategy_symbols.dedup();
    match ctx
        .services
        .refresh_data_with_subscriptions(&strategy_symbols)
        .await
    {
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

/// Warning and trigger delivery are driven by durable Outbox rows.
async fn publish_scan_signals(ctx: &Arc<ServerContext>, res: &ScanResult) {
    // Emergency rollback switch. Never run both pathways simultaneously.
    if n_core::storage::outbox::LEGACY_DIRECT_PUBLISH {
        register_execution_signals(ctx, &res.new_warnings).await;
        // 将新预警和新触发持久化到服务端的 notification_history 并广播
        for w in &res.new_warnings {
            let item = NewNotificationHistoryItem {
                kind: "warning".to_string(),
                title: Some(format!("{} 出现新形态预警", w.symbol)),
                content: format!(
                    "{} {} 级形态 (评分: {:.1})",
                    w.direction, w.level, w.entry_score
                ),
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
                preclose: None,
            };
            if let Ok(saved) =
                n_core::storage::repo::insert_notification_history(&ctx.db, &item).await
            {
                let seq = ctx.next_event_seq();
                ctx.realtime_hub
                    .broadcast(
                        seq,
                        "notification.created",
                        serde_json::to_value(&saved).unwrap_or_default(),
                    )
                    .await;
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
                preclose: None,
            };
            if let Ok(saved) =
                n_core::storage::repo::insert_notification_history(&ctx.db, &item).await
            {
                let seq = ctx.next_event_seq();
                ctx.realtime_hub
                    .broadcast(
                        seq,
                        "notification.created",
                        serde_json::to_value(&saved).unwrap_or_default(),
                    )
                    .await;
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
                let model = win_rate
                    .as_ref()
                    .map(|rate| (rate.model_id.as_str(), rate.p_win));
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
    } // Legacy publisher, disabled while R4a Outbox is active.

    // Single-bar notices are not pattern_events and remain on the old path.
    if !n_core::storage::outbox::LEGACY_DIRECT_PUBLISH {
        let cfg = ctx.services.config().await;
        if cfg.email.enabled && cfg.email.sendable() {
            for sb in &res.single_bars {
                let (subject, body) = single_bar_email_payload(sb);
                let email_cfg = cfg.email.clone();
                tokio::task::spawn_blocking(move || {
                    let _ = n_core::notify::email::send_summary(&subject, &body, &email_cfg);
                });
            }
        }
    }

}

async fn reconcile_preclose(ctx: &Arc<ServerContext>) {
    let now_naive = Local::now().naive_local();
    match ctx.services.reconcile_preclose_signals(now_naive).await {
        Ok(updates) if !updates.is_empty() => {
            let seq = ctx.next_event_seq();
            ctx.realtime_hub
                .broadcast(
                    seq,
                    "preclose-signal",
                    serde_json::to_value(&updates).unwrap_or_default(),
                )
                .await;
        }
        Err(error) => tracing::warn!("收盘前预检测结算失败: {error:#}"),
        _ => {}
    }
    match ctx.services.reconcile_preclose_candidates(now_naive).await {
        Ok(updates) if !updates.is_empty() => {
            let seq = ctx.next_event_seq();
            ctx.realtime_hub
                .broadcast(
                    seq,
                    "preclose-candidate",
                    serde_json::to_value(&updates).unwrap_or_default(),
                )
                .await;
        }
        Err(error) => tracing::warn!("临时未收盘候选结算失败: {error:#}"),
        _ => {}
    }
}

async fn record_preclose_notifications(
    ctx: &Arc<ServerContext>,
    updates: &[n_core::storage::entities::preclose_signals::Model],
) {
    for s in updates {
        if s.state != "precheck" {
            continue;
        }
        let dir_cn = if s.direction == "up" { "做多" } else { "做空" };
        let item = NewNotificationHistoryItem {
            kind: "warning".to_string(),
            title: Some(format!("{} 收盘前预检测【{}】", s.symbol, dir_cn)),
            content: format!(
                "预计收盘：{}，参考现价：{:.1}，入场：{:.1}，止损：{:.1}",
                s.session_close_ts, s.reference_price, s.entry, s.stop
            ),
            signal: None,
            entry_trigger: None,
            single_bar: None,
            manual_level: None,
            preclose: Some(n_protocol::dto::NotificationPreclose {
                symbol: s.symbol.clone(),
                name: Some(s.symbol.clone()),
                direction: s.direction.clone(),
                session_close_ts: s.session_close_ts.clone(),
                reference_price: s.reference_price,
                kind: "formal_precheck".to_string(),
                entry_score: None,
                state: s.state.clone(),
                parent_event_id: Some(s.parent_event_id),
            }),
        };
        if let Ok(saved) =
            n_core::storage::repo::insert_notification_history(&ctx.db, &item).await
        {
            let seq = ctx.next_event_seq();
            ctx.realtime_hub
                .broadcast(
                    seq,
                    "notification.created",
                    serde_json::to_value(&saved).unwrap_or_default(),
                )
                .await;
        }
    }
}

async fn record_preclose_candidate_notifications(
    ctx: &Arc<ServerContext>,
    updates: &[n_core::storage::entities::preclose_candidates::Model],
) {
    for c in updates {
        if c.state != "provisional" || c.emitted_at != c.last_seen_at {
            continue;
        }
        let dir_cn = if c.direction == "up" { "预做多" } else { "预做空" };
        let item = NewNotificationHistoryItem {
            kind: "warning".to_string(),
            title: Some(format!("{} 未收盘临时推演【{}】", c.symbol, dir_cn)),
            content: format!(
                "推演评分：{:.2}，预计收盘：{}，参考现价：{:.1}",
                c.entry_score, c.session_close_ts, c.reference_price
            ),
            signal: None,
            entry_trigger: None,
            single_bar: None,
            manual_level: None,
            preclose: Some(n_protocol::dto::NotificationPreclose {
                symbol: c.symbol.clone(),
                name: Some(c.symbol.clone()),
                direction: c.direction.clone(),
                session_close_ts: c.session_close_ts.clone(),
                reference_price: c.reference_price,
                kind: "provisional_candidate".to_string(),
                entry_score: Some(c.entry_score),
                state: c.state.clone(),
                parent_event_id: c.parent_event_id,
            }),
        };
        if let Ok(saved) =
            n_core::storage::repo::insert_notification_history(&ctx.db, &item).await
        {
            let seq = ctx.next_event_seq();
            ctx.realtime_hub
                .broadcast(
                    seq,
                    "notification.created",
                    serde_json::to_value(&saved).unwrap_or_default(),
                )
                .await;
        }
    }
}

fn spawn_preclose_emails(
    ctx: Arc<ServerContext>,
    updates: Vec<n_core::storage::entities::preclose_signals::Model>,
) {
    let signals = updates
        .into_iter()
        .filter(|signal| signal.state == "precheck")
        .collect::<Vec<_>>();
    if signals.is_empty() {
        return;
    }
    tokio::spawn(async move {
        let cfg = ctx.services.config().await;
        if !cfg.email.enabled || !cfg.email.signal_preclose_enabled || !cfg.email.sendable() {
            return;
        }
        for signal in signals {
            let parent =
                match n_core::storage::repo::pattern_event_by_id(&ctx.db, signal.parent_event_id)
                    .await
                {
                    Ok(Some(parent)) => parent,
                    Ok(None) => {
                        tracing::warn!(
                            preclose_id = signal.id,
                            parent_event_id = signal.parent_event_id,
                            "预检测邮件对应的正式候选已不存在"
                        );
                        continue;
                    }
                    Err(error) => {
                        tracing::warn!(
                            preclose_id = signal.id,
                            "读取预检测邮件候选失败: {error:#}"
                        );
                        continue;
                    }
                };
            let (subject, body) = preclose_email_payload(&signal, &parent);
            tracing::info!(
                preclose_id = signal.id,
                symbol = signal.symbol,
                "准备发送收盘前预检测邮件"
            );
            let email_cfg = cfg.email.clone();
            tokio::task::spawn_blocking(move || {
                let _ = n_core::notify::email::send_summary(&subject, &body, &email_cfg);
            });
        }
    });
}

fn spawn_preclose_candidate_emails(
    ctx: Arc<ServerContext>,
    updates: Vec<n_core::storage::entities::preclose_candidates::Model>,
) {
    let candidates = updates
        .into_iter()
        .filter(|candidate| {
            candidate.state == "provisional" && candidate.emitted_at == candidate.last_seen_at
        })
        .collect::<Vec<_>>();
    if candidates.is_empty() {
        return;
    }
    tokio::spawn(async move {
        let cfg = ctx.services.config().await;
        if !cfg.email.enabled || !cfg.email.signal_preclose_enabled || !cfg.email.sendable() {
            return;
        }
        for candidate in candidates {
            let (subject, body) = preclose_candidate_email_payload(&candidate);
            tracing::info!(
                candidate_id = candidate.id,
                symbol = candidate.symbol,
                "准备发送临时未收盘推演邮件"
            );
            let email_cfg = cfg.email.clone();
            tokio::task::spawn_blocking(move || {
                let _ = n_core::notify::email::send_summary(&subject, &body, &email_cfg);
            });
        }
    });
}

/// Separate from quote/execution polling: bridge waits never delay those loops or chart requests.
fn spawn_live_chart_publisher(ctx: Arc<ServerContext>) {
    tokio::spawn(async move {
        loop {
            tokio::select! {
                _ = ctx.services.live_chart.wake.notified() => {},
                _ = tokio::time::sleep(Duration::from_millis(500)) => {},
            }
            if ctx
                .is_shutting_down
                .load(std::sync::atomic::Ordering::Relaxed)
            {
                break;
            }
            let config = ctx.services.config().await;
            if config.data_source.primary_source != "tqsdk"
                || !ctx.services.data_source.tq_is_available()
            {
                ctx.services.live_chart.clear();
                continue;
            }
            for symbol in ctx.realtime_hub.chart_symbols().await {
                ctx.services.live_chart.request(&symbol);
            }
            // Four bounded local bridge reads at a time; the bridge already maintains 5m serials.
            let mut jobs = tokio::task::JoinSet::new();
            for symbol in ctx.services.live_chart.due_symbols(4) {
                if !n_core::scheduler::is_symbol_trading_time(&symbol, &Local::now()) {
                    continue;
                }
                let ctx = ctx.clone();
                let bridge_port = config.data_source.bridge_port;
                jobs.spawn(async move {
                    use n_core::fetch::MarketDataSource;
                    let tq = ctx.services.data_source.tq_client().await;
                    let requested_at = Instant::now();
                    match tokio::time::timeout(
                        Duration::from_millis(1000),
                        tq.fetch_minute_raw(&symbol, "5m", 300),
                    )
                    .await
                    {
                        Ok(Ok(raw)) => {
                            let current = ctx.services.config().await;
                            if current.data_source.primary_source != "tqsdk"
                                || current.data_source.bridge_port != bridge_port
                                || !ctx.services.data_source.tq_is_available()
                            {
                                return;
                            }
                            let now = Local::now();
                            let bars = ctx.services.live_chart.publish_after(
                                &symbol,
                                raw.klines,
                                now.naive_local(),
                                requested_at,
                            );
                            for tf in n_core::derive::Timeframe::supported() {
                                let bar = bars.iter().find(|bar| bar.timeframe == tf.as_str());
                                let payload = serde_json::json!({
                                    "symbol": symbol, "timeframe": tf.as_str(), "bar": bar,
                                    "updated_at": now.timestamp_millis(),
                                });
                                ctx.realtime_hub
                                    .broadcast_kline(
                                        ctx.next_event_seq(),
                                        "kline.partial",
                                        &symbol,
                                        tf.as_str(),
                                        payload,
                                    )
                                    .await;
                            }
                        }
                        _ => {
                            tracing::debug!(symbol, "后台实时柱更新未就绪，旧结果按新鲜度自动过期")
                        }
                    }
                });
            }
            while let Some(result) = jobs.join_next().await {
                if let Err(error) = result {
                    tracing::warn!(%error, "后台实时柱任务失败");
                }
            }
        }
    });
}

type SnapshotBatch = (Instant, Vec<n_core::service::MarketSnapshot>);

/// One ordered, bounded entry worker preserves every sampled crossing. Slow
/// closed-bar analysis consumes the latest frame, with at most one scan active.
fn spawn_quote_detectors(
    ctx: Arc<ServerContext>,
) -> (
    tokio::sync::mpsc::Sender<SnapshotBatch>,
    tokio::sync::watch::Sender<Option<SnapshotBatch>>,
) {
    let (entry_tx, mut entry_rx) = tokio::sync::mpsc::channel::<SnapshotBatch>(8);
    let (slow_tx, mut slow_rx) = tokio::sync::watch::channel::<Option<SnapshotBatch>>(None);
    let entry_ctx = ctx.clone();
    tokio::spawn(async move {
        let ctx = entry_ctx;
        while let Some((received_at, snapshots)) = entry_rx.recv().await {
            if ctx
                .is_shutting_down
                .load(std::sync::atomic::Ordering::Relaxed)
            {
                break;
            }
            let queue_ms = received_at.elapsed().as_millis();
            let stage = Instant::now();
            // 检测入场价命中
            if let Ok(hits) = ctx
                .services
                .entry_trigger_hits(&snapshots)
                .await
                .map_err(|error| tracing::warn!("入场检测失败: {error:#}"))
            {
                if !hits.is_empty() {
                    let seq = ctx.next_event_seq();
                    ctx.realtime_hub
                        .broadcast(
                            seq,
                            "entry.triggered",
                            serde_json::to_value(&hits).unwrap_or_default(),
                        )
                        .await;
                }
            }

            tracing::info!(
                queue_ms,
                entry_ms = stage.elapsed().as_millis(),
                "入场检测完成"
            );
        }
    });
    tokio::spawn(async move {
        while slow_rx.changed().await.is_ok() {
            if ctx
                .is_shutting_down
                .load(std::sync::atomic::Ordering::Relaxed)
            {
                break;
            }
            let Some((received_at, snapshots)) = slow_rx.borrow_and_update().clone() else {
                continue;
            };
            let queue_ms = received_at.elapsed().as_millis();
            let stage = Instant::now();
            // 检测关键区域手工画线
            if let Ok(alerts) = ctx
                .services
                .manual_level_alerts(&snapshots)
                .await
                .map_err(|error| tracing::warn!("关键区域检测失败: {error:#}"))
            {
                if !alerts.is_empty() {
                    let seq = ctx.next_event_seq();
                    ctx.realtime_hub
                        .broadcast(
                            seq,
                            "manual_level.triggered",
                            serde_json::to_value(&alerts).unwrap_or_default(),
                        )
                        .await;
                }
            }

            let manual_ms = stage.elapsed().as_millis();
            let stage = Instant::now();
            // 收盘前预检测 (机制一)
            match ctx.services.preclose_tick(&snapshots).await {
                Ok(updates) if !updates.is_empty() => {
                    let seq = ctx.next_event_seq();
                    ctx.realtime_hub
                        .broadcast(
                            seq,
                            "preclose-signal",
                            serde_json::to_value(&updates).unwrap_or_default(),
                        )
                        .await;
                    record_preclose_notifications(&ctx, &updates).await;
                    spawn_preclose_emails(ctx.clone(), updates);
                }
                Err(error) => tracing::warn!("收盘前预检测轮询失败: {error:#}"),
                _ => {}
            }

            let preclose_ms = stage.elapsed().as_millis();
            let stage = Instant::now();
            // 临时未收盘推演扫描 (机制二)
            match ctx.services.preclose_candidate_tick(&snapshots).await {
                Ok(updates) if !updates.is_empty() => {
                    let seq = ctx.next_event_seq();
                    ctx.realtime_hub
                        .broadcast(
                            seq,
                            "preclose-candidate",
                            serde_json::to_value(&updates).unwrap_or_default(),
                        )
                        .await;
                    record_preclose_candidate_notifications(&ctx, &updates).await;
                    spawn_preclose_candidate_emails(ctx.clone(), updates);
                }
                Err(error) => tracing::warn!("临时未收盘推演轮询失败: {error:#}"),
                _ => {}
            }
            tracing::info!(
                queue_ms,
                manual_ms,
                preclose_ms,
                candidate_ms = stage.elapsed().as_millis(),
                "行情后台检测完成"
            );
        }
    });
    (entry_tx, slow_tx)
}

fn spawn_quote_poller(ctx: Arc<ServerContext>) {
    let (entry_tx, slow_tx) = spawn_quote_detectors(ctx.clone());
    tokio::spawn(async move {
        let mut period =
            Duration::from_millis(ctx.services.config().await.quote.poll_interval_ms.max(100));
        let mut ticker = tokio::time::interval(period);
        ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        loop {
            let interval_ms = ctx.services.config().await.quote.poll_interval_ms;
            let configured = Duration::from_millis(interval_ms.max(100));
            if configured != period {
                period = configured;
                ticker = tokio::time::interval_at(tokio::time::Instant::now() + period, period);
                ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
            }
            ticker.tick().await;
            if ctx
                .is_shutting_down
                .load(std::sync::atomic::Ordering::Relaxed)
            {
                break;
            }

            let now = Local::now();
            if !n_core::scheduler::is_trading_time(&now) {
                continue;
            }

            let cycle_started = Instant::now();
            let contracts: Vec<String> = ctx
                .trader_snapshot
                .read()
                .await
                .get("broker_positions")
                .and_then(serde_json::Value::as_array)
                .into_iter()
                .flatten()
                .filter_map(|p| p["instrument_id"].as_str().map(str::to_uppercase))
                .collect::<std::collections::BTreeSet<_>>()
                .into_iter()
                .collect();
            match ctx
                .services
                .realtime_quotes_with_contracts(&contracts)
                .await
            {
                Ok((snapshots, held_quotes)) => {
                    let fetch_ms = cycle_started.elapsed().as_millis();
                    // Normal price updates maintain all seeded periods without SQL or bridge reads.
                    let cfg = ctx.services.config().await;
                    let updated_bars = if cfg.data_source.primary_source == "tqsdk"
                        && ctx.services.data_source.tq_is_available()
                    {
                        ctx.services
                            .live_chart
                            .update_prices(&snapshots, Local::now().naive_local())
                    } else {
                        Vec::new()
                    };
                    let seq = ctx.next_event_seq();
                    let mut display_quotes = snapshots.clone();
                    display_quotes.extend(held_quotes);
                    ctx.realtime_hub
                        .broadcast(
                            seq,
                            "quote.updated",
                            serde_json::to_value(&display_quotes).unwrap_or_default(),
                        )
                        .await;

                    tracing::info!(
                        fetch_ms,
                        cycle_ms = cycle_started.elapsed().as_millis(),
                        interval_ms,
                        "行情采集与推送完成"
                    );
                    for bar in updated_bars {
                        let sym = bar.symbol.clone();
                        let tf = bar.timeframe.clone();
                        ctx.realtime_hub
                            .broadcast_kline(
                                ctx.next_event_seq(),
                                "kline.partial",
                                &sym,
                                &tf,
                                serde_json::json!({ "symbol": sym, "timeframe": tf, "bar": bar }),
                            )
                            .await;
                    }

                    let received_at = Instant::now();
                    slow_tx.send_replace(Some((received_at, snapshots.clone())));
                    if entry_tx.capacity() == 0 {
                        tracing::warn!("入场检测队列已满，行情采集等待有界背压");
                    }
                    if entry_tx.send((received_at, snapshots)).await.is_err() {
                        break;
                    }
                }
                Err(e) => {
                    tracing::warn!("实时行情轮询失败: {e}");
                }
            }
        }
    });
}

fn ends_fifteen_minute_bar(bar_end: &str) -> bool {
    chrono::NaiveDateTime::parse_from_str(bar_end, "%Y-%m-%d %H:%M:%S")
        .is_ok_and(|dt| dt.minute() % 15 == 0 && dt.second() == 0)
}

#[cfg(test)]
mod closed_scan_boundary_tests {
    use super::ends_fifteen_minute_bar;

    #[test]
    fn only_closed_fifteen_minute_boundaries_enqueue_scan() {
        assert!(ends_fifteen_minute_bar("2026-11-03 21:15:00"));
        assert!(ends_fifteen_minute_bar("2026-11-03 23:30:00"));
        assert!(!ends_fifteen_minute_bar("2026-11-03 21:05:00"));
        assert!(!ends_fifteen_minute_bar("2026-11-03 21:10:00"));
        assert!(!ends_fifteen_minute_bar("2026-11-03 21:15:01"));
        assert!(!ends_fifteen_minute_bar("invalid"));
    }
}

fn spawn_tq_bar_event_consumer(
    ctx: Arc<ServerContext>,
    scan_wake: Arc<tokio::sync::Notify>,
) {
    tokio::spawn(async move {
        let mut is_first = true;
        let mut after_id = 0u64;
        let mut stream_id = String::new();
        let mut poll_backoff_ms = 500u64;
        loop {
            if ctx
                .is_shutting_down
                .load(std::sync::atomic::Ordering::Relaxed)
            {
                break;
            }

            let cfg = ctx.services.config().await;
            if cfg.data_source.primary_source != "tqsdk"
                || !ctx.services.data_source.tq_is_available()
            {
                // A missing source is not a long-poll response: avoid a busy loop.
                tokio::time::sleep(Duration::from_secs(3)).await;
                continue;
            }

            if is_first {
                is_first = false;
                tokio::time::sleep(Duration::from_secs(30)).await;
            }

            let tq = ctx.services.data_source.tq_client().await;
            let poll_started = Instant::now();
            let response = match tq.poll_closed_bar_events(after_id, 25).await {
                Ok(response) => {
                    poll_backoff_ms = 500;
                    response
                }
                Err(error) => {
                    tracing::warn!("天勤闭合事件长轮询失败: {error:#}");
                    tokio::time::sleep(Duration::from_millis(poll_backoff_ms)).await;
                    poll_backoff_ms = (poll_backoff_ms * 2).min(5_000);
                    continue;
                }
            };
            let empty_response = response.events.is_empty();
            let mut persist_failed = false;
            {
                if stream_id.is_empty() {
                    stream_id = response.stream_id.clone();
                } else if stream_id != response.stream_id {
                    stream_id = response.stream_id;
                    after_id = 0;
                    // Replay the new stream from the beginning after a bridge restart.
                    tokio::time::sleep(Duration::from_millis(100)).await;
                    continue;
                }
                let enabled = match n_core::storage::repo::list_symbols(&ctx.db, true).await {
                    Ok(symbols) => symbols,
                    Err(error) => {
                        tracing::warn!("读取闭合事件启用品种失败: {error:#}");
                        tokio::time::sleep(Duration::from_millis(500)).await;
                        continue;
                    }
                };
                for ev in response.events {
                    if !enabled.iter().any(|symbol| symbol.code == ev.symbol) {
                        after_id = after_id.max(ev.event_id);
                        continue;
                    }
                    // A closure notification is a commitment that the final bar
                    // and its derived candles are already readable in the DB.
                    let store_started = Instant::now();
                    let closure = match ctx.services.store_tq_chart_closure(&ev).await {
                        Ok(update) => update,
                        Err(error) => {
                            tracing::warn!(symbol = %ev.symbol, bar_end = %ev.bar_end, "闭合K线入库或图表更新准备失败，将重试: {error:#}");
                            persist_failed = true;
                            break;
                        }
                    };
                    if ends_fifteen_minute_bar(&ev.bar_end) {
                        match n_core::storage::scan_jobs::enqueue(
                            &ctx.db, &ev.symbol, &ev.bar_end, Local::now().timestamp(), true,
                        ).await {
                            Ok(true) => scan_wake.notify_waiters(),
                            Ok(false) => {}
                            Err(error) => {
                                tracing::warn!(
                                    symbol = %ev.symbol, bar_end = %ev.bar_end, event_id = ev.event_id,
                                    "闭合扫描任务写入失败，将重试: {error:#}"
                                );
                                persist_failed = true;
                            }
                        }
                    }
                    if let Some(closure) = closure {
                        let update = ctx.services.chart_history_update(closure).await;
                        ctx.realtime_hub
                            .broadcast_chart_history(
                                ctx.next_event_seq(),
                                &ev.symbol,
                                serde_json::json!(&update),
                            )
                            .await;
                        for bar in update.bars {
                            ctx.realtime_hub.broadcast_kline(
                                ctx.next_event_seq(), "kline.closed", &ev.symbol, &bar.timeframe,
                                serde_json::json!({"symbol":ev.symbol,"timeframe":bar.timeframe,"bar":bar,
                                    "series_revision":update.series_revision}),
                            ).await;
                        }
                    }
                    if persist_failed {
                        break;
                    }
                    after_id = after_id.max(ev.event_id);
                    tracing::info!(
                        source_emitted_at = %ev.emitted_at,
                        proof = ?ev.proof,
                        event_id = ev.event_id,
                        received_to_store_ms = store_started.elapsed().as_millis(),
                        "🔔 [FAST_PATH] 收到天勤闭合K线: {} {} {}",
                        ev.symbol,
                        ev.period,
                        ev.bar_end
                    );
                }
            }
            if persist_failed {
                // Keep after_id at the preceding committed event and retry the failed one.
                tokio::time::sleep(Duration::from_millis(500)).await;
            } else if empty_response && poll_started.elapsed() < Duration::from_millis(200) {
                // Defensive throttle for a misconfigured endpoint that immediately returns empty.
                tokio::time::sleep(Duration::from_millis(200)).await;
            }
        }
    });
}

fn spawn_tq_warmup(ctx: Arc<ServerContext>) {
    tokio::spawn(async move {
        loop {
            tokio::time::sleep(Duration::from_secs(5)).await;
            if ctx
                .is_shutting_down
                .load(std::sync::atomic::Ordering::Relaxed)
            {
                break;
            }
            if ctx.services.config().await.data_source.primary_source != "tqsdk" {
                break;
            }
            if !ctx.services.data_source.tq_available_or_probe().await {
                continue;
            }
            let Ok(symbols) = n_core::storage::repo::list_symbols(&ctx.db, true).await else {
                continue;
            };
            let codes: Vec<String> = symbols.into_iter().map(|s| s.code).collect();
            if codes.is_empty() {
                break;
            }
            let tq = ctx.services.data_source.tq_client().await;
            match tq.subscribe_klines(&codes, "5m", 300).await {
                Ok(response)
                    if response.failed.is_empty() && response.subscribed.len() == codes.len() =>
                {
                    // Also works when the server starts during a break: no trading
                    // tick or scheduler refresh is required to recover the last bar.
                    let stats = ctx.services.reconcile_tq_finality(&codes).await;
                    tracing::info!(
                        "收盘订阅预热及补偿完成: 成功 {} 失败 {}",
                        stats.succeeded,
                        stats.failures
                    );
                    broadcast_data_updated(&ctx, stats.succeeded, stats.failures).await;
                    if stats.failures == 0 {
                        break;
                    }
                }
                Ok(response) => tracing::warn!(
                    "收盘订阅尚未就绪: 已订阅 {} 失败 {:?}",
                    response.subscribed.len(),
                    response.failed
                ),
                Err(error) => tracing::warn!("收盘订阅失败，将重试: {error:#}"),
            }
        }
    });
}

#[cfg(test)]
mod quote_delivery_tests {
    use super::*;

    fn registration_engine() -> Arc<n_core::trader::ExecutionEngine> {
        Arc::new(n_core::trader::ExecutionEngine::new(
            n_core::trader::CtpTraderClient::for_port(1),
            Default::default(),
        ))
    }

    #[tokio::test]
    async fn registered_signals_skip_reprocessing_without_suppressing_other_accounts_or_modules() {
        let engine = registration_engine();
        engine
            .register_condition(
                Some(42),
                "PB0",
                "pb2611",
                n_core::analyze::model::Dir::Up,
                1000.0,
                990.0,
                1200.0,
                3.6,
                "2026-10-27 10:00:00",
            )
            .await
            .unwrap();
        let conditions = engine.list_conditions().await;
        let definition = engine.definition().await;
        let first = SignalRegistrationTarget {
            engine: engine.clone(),
            created_at: None,
            registered: registered_event_ids(conditions.clone(), &definition.strategy_id),
        };
        let other = SignalRegistrationTarget {
            engine: registration_engine(),
            created_at: None,
            registered: Default::default(),
        };
        assert!(!first.needs(42, "2026-10-27 10:00:00"));
        assert!(first.needs(43, "2026-10-27 10:00:00"));
        assert!(other.needs(42, "2026-10-27 10:00:00"));
        assert!(registered_event_ids(conditions, "another_strategy").is_empty());
    }

    #[test]
    fn new_instance_registration_keeps_its_original_creation_cutoff() {
        let created =
            chrono::NaiveDateTime::parse_from_str("2026-10-27 10:00:00", "%Y-%m-%d %H:%M:%S")
                .unwrap()
                .and_local_timezone(Local)
                .single()
                .unwrap()
                .timestamp();
        let target = SignalRegistrationTarget {
            engine: registration_engine(),
            created_at: Some(created),
            registered: Default::default(),
        };
        assert!(!target.needs(42, "2026-10-27 09:59:59"));
        assert!(target.needs(42, "2026-10-27 10:00:00"));
        assert!(!target.needs(42, "invalid time"));
    }

    fn quote(code: &str, price: f64) -> ReceivedExecutionQuote {
        let received_at = chrono::Utc::now().timestamp();
        ReceivedExecutionQuote {
            symbol: code.into(),
            quote: ExecutionQuote {
                instrument_id: code.into(),
                exchange_id: "SHFE".into(),
                underlying_symbol: String::new(),
                last: price,
                bid: price - 1.0,
                ask: price + 1.0,
                tick: 1.0,
                lower: 1.0,
                upper: 2000.0,
                timestamp: received_at,
            },
            received_at,
        }
    }

    #[tokio::test]
    async fn drains_full_queue_without_discarding_intermediate_ticks() {
        let (tx, mut rx) = tokio::sync::mpsc::channel(64);
        let first = vec![quote("pb2611", 1000.0)];
        for price in 0..64 {
            let mut event = quote("pb2611", 990.0 + price as f64);
            event.received_at = price;
            tx.try_send(vec![event]).unwrap();
        }
        let events = drain_quote_batches(&mut rx, first);
        assert_eq!(events.len(), 65);
        assert_eq!(events[0].quote.last, 1000.0);
        assert_eq!(events[1].quote.last, 990.0);
        assert_eq!(events[64].quote.last, 1053.0);
        assert_eq!(events[64].received_at, 63);
        assert!(rx.is_empty());
        tx.try_send(vec![quote("pb2611", 1054.0)]).unwrap();
    }

    #[test]
    fn routes_only_subscribed_quotes_and_keeps_tick_order() {
        let events = vec![
            quote("pb2611", 1000.0),
            quote("cu2611", 1200.0),
            quote("pb2611", 990.0),
        ];
        let selected =
            filter_quote_events(&events, &std::collections::HashSet::from(["PB2611".into()]));
        assert_eq!(
            selected.iter().map(|event| event.quote.last).collect::<Vec<_>>(),
            vec![1000.0, 990.0]
        );
        assert!(filter_quote_events(&events, &Default::default()).is_empty());
        let mut alias = quote("pb2611", 1001.0);
        alias.symbol = "PB0".into();
        assert_eq!(
            filter_quote_events(
                &vec![alias],
                &std::collections::HashSet::from(["PB0".into()])
            )
            .len(),
            1
        );
    }
}
