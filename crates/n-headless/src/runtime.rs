use crate::state::ServerContext;
use chrono::Local;
use n_core::notify::email::{
    event_email_payload_with_model, preclose_candidate_email_payload, preclose_email_payload,
    single_bar_email_payload, EventEmailKind,
};
use n_core::service::{RefreshStats, ScanResult};
use n_protocol::NewNotificationHistoryItem;
use std::sync::Arc;
use std::time::{Duration, Instant};

type QuoteBatch = Vec<(String, n_core::trader::model::ExecutionQuote)>;

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
        .filter(|(code, quote)| {
            subscriptions.contains(&code.to_uppercase())
                || subscriptions.contains(&quote.instrument_id.to_uppercase())
        })
        .cloned()
        .collect()
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

pub fn spawn_runtime_tasks(ctx: Arc<ServerContext>) {
    spawn_scheduler(ctx.clone());
    spawn_quote_poller(ctx.clone());
    spawn_live_chart_publisher(ctx.clone());
    spawn_tq_bar_event_consumer(ctx.clone());
    spawn_tq_warmup(ctx.clone());
    spawn_trader_stream(ctx.clone());
    spawn_execution(ctx.clone());
    crate::instance_runtime::spawn(ctx);
}

pub async fn register_execution_signals(
    ctx: &Arc<ServerContext>,
    signals: &[n_core::storage::entities::pattern_events::Model],
) {
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
        if let Ok(bars) = ctx.services.bars_for(code, "15m").await {
            bar_times.insert(
                code.clone(),
                bars.iter().map(|b| b.dt.to_bar_ts()).collect::<Vec<_>>(),
            );
        }
        if let Ok(rows) = n_core::storage::repo::raw_klines(&ctx.db, code).await {
            raw_bars.insert(
                code.clone(),
                rows.iter()
                    .map(n_core::service::model_to_fetch)
                    .collect::<Vec<_>>(),
            );
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
            if let Err(e) = r.engine.on_quote_batch(events).await {
                tracing::warn!("默认实例行情推进失败: {e}");
            }
        }
    });
    let quote_ctx = ctx.clone();
    tokio::spawn(async move {
        let mut cursor = 0;
        let mut stream_id = String::new();
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
            // The cursor belongs to the global bridge stream. Changing the
            // symbol set must not skip intervening ticks of existing positions.
            let tq = quote_ctx.services.data_source.tq_client().await;
            match tq.execution_events(&codes, cursor, &stream_id).await {
                Ok(batch) => {
                    let restarted = !stream_id.is_empty()
                        && batch["stream_id"]
                            .as_str()
                            .is_some_and(|id| id != stream_id);
                    if batch["gap"].as_bool().unwrap_or(false) || restarted {
                        let reason = if restarted {
                            "行情桥事件流已重启"
                        } else {
                            "行情源事件缓存已覆盖未消费数据"
                        };
                        tracing::warn!(cursor, stream_id, reason, "执行行情源需要重新同步");
                        if let Some(manager) = quote_ctx.instances.get() {
                            for r in manager.all().await {
                                if let Err(e) = r.engine.quote_gap_with_reason(reason).await {
                                    tracing::error!(
                                        "实例 {} 行情缺口保存失败: {e}",
                                        r.definition.id
                                    );
                                }
                            }
                        } else if let Err(e) = quote_ctx
                            .execution_engine
                            .quote_gap_with_reason(reason)
                            .await
                        {
                            tracing::error!("行情缺口保存失败: {e}");
                        }
                        stream_id.clear();
                        cursor = 0;
                        continue;
                    }
                    stream_id = batch["stream_id"].as_str().unwrap_or("").into();
                    cursor = batch["latest_event_id"].as_u64().unwrap_or(0);
                    // Idle/closed-session responses also prove transport health.
                    // Every instance shares this stream, including P2 and B5.
                    if let Some(manager) = quote_ctx.instances.get() {
                        for r in manager.all().await {
                            if r.engine.market_recovered().await {
                                tracing::info!(instance_id = %r.definition.id, "执行行情连接恢复，报单仍需本合约有效新盘口");
                            }
                        }
                    } else {
                        if quote_ctx.execution_engine.market_recovered().await {
                            tracing::info!("执行行情连接恢复，报单仍需本合约有效新盘口");
                        }
                    }
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
                                    if !r.quote_gap.swap(true, std::sync::atomic::Ordering::Relaxed)
                                    {
                                        tracing::warn!(instance_id = %r.definition.id, dropped_ticks = events.len(), "本实例行情消费队列已满，启动缺口保护");
                                    }
                                }
                                Err(tokio::sync::mpsc::error::TrySendError::Closed(_)) => {
                                    tracing::warn!(instance_id = %r.definition.id, "实例行情消费者已关闭");
                                }
                            }
                        }
                    }
                    if quote_ctx.instances.get().is_none() {
                        if let Err(e) = quote_ctx.execution_engine.on_quote_batch(events).await {
                            tracing::warn!("执行行情推进失败: {e}");
                        }
                    }
                }
                Err(e) => {
                    quote_ctx.execution_engine.market_unavailable().await;
                    if let Some(manager) = quote_ctx.instances.get() {
                        for r in manager.all().await {
                            r.engine.market_unavailable().await;
                        }
                    }
                    tracing::warn!("执行行情连接失败: {e}");
                    tokio::time::sleep(Duration::from_secs(3)).await;
                }
            }
        }
    });
    tokio::spawn(async move {
        let mut timer = tokio::time::interval(Duration::from_secs(1));
        timer.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        let mut next_sync = Instant::now();
        let mut history_versions = std::collections::HashMap::new();
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
            let declared = ctx.execution_engine.market_subscriptions().await;
            let mut symbols = ctx.execution_engine.active_symbols().await;
            symbols.extend(
                declared
                    .iter()
                    .filter(|s| s.timeframe.as_deref() == Some("15m"))
                    .map(|s| s.symbol.clone()),
            );
            symbols.sort();
            symbols.dedup();
            let strategy_revision = ctx.execution_engine.definition().await.revision;
            for symbol in symbols {
                let version =
                    crate::instance_runtime::market_history_revision(&ctx.db, &symbol, "15m")
                        .await
                        .ok()
                        .map(|history| (strategy_revision.clone(), history));
                if version
                    .as_ref()
                    .is_some_and(|v| history_versions.get(&symbol) == Some(v))
                {
                    continue;
                }
                let mut refreshed = true;
                if ctx.execution_engine.needs_completed_15m().await {
                    if let Ok(raw) = n_core::storage::repo::raw_klines(&ctx.db, &symbol).await {
                        let bars = raw
                            .iter()
                            .map(n_core::service::model_to_fetch)
                            .collect::<Vec<_>>();
                        if let Err(e) = crate::instance_runtime::forward_strategy_history(
                            &ctx,
                            &ctx.execution_engine,
                            &symbol,
                            &bars,
                        )
                        .await
                        {
                            refreshed = false;
                            tracing::warn!("策略独立历史推进失败: {e}");
                        }
                        if let Err(e) = ctx.execution_engine.on_completed_15m(&symbol, &bars).await
                        {
                            refreshed = false;
                            tracing::warn!("候选策略完整K线推进失败: {e}");
                        }
                    } else {
                        refreshed = false;
                    }
                }
                if let Ok(bars) = ctx.services.bars_for(&symbol, "15m").await {
                    let last = ctx.execution_engine.last_bar(&symbol).await;
                    let pending = if let Some(last) = last {
                        bars.iter()
                            .filter(|b| b.dt.to_bar_ts() > last)
                            .collect::<Vec<_>>()
                    } else {
                        bars.last().into_iter().collect::<Vec<_>>()
                    };
                    for bar in pending {
                        if let Err(e) = ctx
                            .execution_engine
                            .on_price_update(
                                &symbol,
                                bar.high,
                                bar.low,
                                bar.close,
                                &bar.dt.to_bar_ts(),
                                true,
                            )
                            .await
                        {
                            refreshed = false;
                            tracing::warn!("执行K线推进失败: {e}");
                        }
                    }
                    let times = bars.iter().map(|b| b.dt.to_bar_ts()).collect::<Vec<_>>();
                    if let Err(e) = ctx
                        .execution_engine
                        .reconcile_entry_age(&symbol, &times)
                        .await
                    {
                        refreshed = false;
                        tracing::warn!("入场预警时效核对失败: {e}");
                    }
                    if refreshed {
                        if let Some(version) = version {
                            history_versions.insert(symbol, version);
                        }
                    }
                }
            }
            for subscription in declared {
                if let Some(tf) = subscription.timeframe.filter(|tf| tf != "15m") {
                    if let Ok(bars) = ctx.services.bars_for(&subscription.symbol, &tf).await {
                        if let Some(bar) = bars.last() {
                            let _ = ctx
                                .execution_engine
                                .dispatch(n_core::trader::strategy::StrategyEvent::BarClosed {
                                    symbol: subscription.symbol.clone(),
                                    timeframe: tf,
                                    timestamp: bar.dt.to_bar_ts(),
                                    high: bar.high,
                                    low: bar.low,
                                    close: bar.close,
                                })
                                .await;
                        }
                    }
                }
            }
        }
    });
}

fn spawn_trader_stream(ctx: Arc<ServerContext>) {
    // watch coalesces bursts: a slow consumer always receives the latest snapshot.
    let (tx, mut rx) = tokio::sync::watch::channel(serde_json::Value::Null);
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
                    if let Err(e) = bridge_ctx.execution_engine.reconcile(&snapshot).await {
                        tracing::error!("柜台状态恢复失败，执行任务未完成: {e}");
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
        let mut last_sent = Instant::now();
        loop {
            // The timer only reads strategy memory; it never queries CTP or HTTP.
            tokio::select! {
                _ = ticker.tick() => {},
                changed = rx.changed() => { if changed.is_err() { break; } },
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
            snapshot["config"] =
                serde_json::to_value(ctx.execution_engine.get_config().await).unwrap_or_default();
            snapshot["managed_positions"] =
                serde_json::to_value(ctx.execution_engine.list_positions().await)
                    .unwrap_or_default();
            snapshot["conditions"] =
                serde_json::to_value(ctx.execution_engine.list_conditions().await)
                    .unwrap_or_default();
            snapshot["history"] =
                serde_json::to_value(ctx.execution_engine.get_trade_history().await)
                    .unwrap_or_default();
            snapshot["statistics"] =
                serde_json::to_value(ctx.execution_engine.get_trade_statistics().await)
                    .unwrap_or_default();
            snapshot["execution_error"] =
                serde_json::to_value(ctx.execution_engine.execution_error().await)
                    .unwrap_or_default();
            snapshot["strategy_comparison"] = ctx.execution_engine.comparison_summary().await;
            let (snapshot, delta) = publisher.update(snapshot);
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
        let mut last_scan: Option<chrono::DateTime<Local>> = None;

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

            if !startup_done {
                startup_done = true;
                if n_core::scheduler::is_trading_time(&now) {
                    if let Ok(stats) = run_refresh(&ctx).await {
                        last_refresh = Some(now);
                        let seq = ctx.next_event_seq();
                        ctx.realtime_hub
                            .broadcast(
                                seq,
                                "data.updated",
                                serde_json::to_value(&stats).unwrap_or_default(),
                            )
                            .await;
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
                        ctx.realtime_hub
                            .broadcast(
                                seq,
                                "data.updated",
                                serde_json::to_value(&stats).unwrap_or_default(),
                            )
                            .await;
                    }
                }
                n_core::scheduler::SchedulerAction::Scan => {
                    let _ = run_refresh(&ctx).await;
                    last_refresh = Some(now);
                    if let Ok(scan_res) = run_scan(&ctx).await {
                        last_scan = Some(now);
                        let seq = ctx.next_event_seq();
                        ctx.realtime_hub
                            .broadcast(
                                seq,
                                "scan.completed",
                                serde_json::to_value(&scan_res).unwrap_or_default(),
                            )
                            .await;
                    }
                }
                n_core::scheduler::SchedulerAction::RefreshAndScan => {
                    if let Ok(stats) = run_refresh(&ctx).await {
                        last_refresh = Some(now);
                        let seq = ctx.next_event_seq();
                        ctx.realtime_hub
                            .broadcast(
                                seq,
                                "data.updated",
                                serde_json::to_value(&stats).unwrap_or_default(),
                            )
                            .await;
                    }
                    if let Ok(scan_res) = run_scan(&ctx).await {
                        last_scan = Some(now);
                        let seq = ctx.next_event_seq();
                        ctx.realtime_hub
                            .broadcast(
                                seq,
                                "scan.completed",
                                serde_json::to_value(&scan_res).unwrap_or_default(),
                            )
                            .await;
                    }
                }
            }
        }
    });
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

async fn run_scan(ctx: &Arc<ServerContext>) -> anyhow::Result<ScanResult> {
    let t0 = Instant::now();
    tracing::info!("🔍 扫描触发 | {}", Local::now().format("%H:%M:%S"));
    let res = ctx.services.run_scan().await?;
    register_execution_signals(ctx, &res.new_warnings).await;
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
        };
        if let Ok(saved) = n_core::storage::repo::insert_notification_history(&ctx.db, &item).await
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
        };
        if let Ok(saved) = n_core::storage::repo::insert_notification_history(&ctx.db, &item).await
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

    // 收盘后对账结算 (机制一与机制二)
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

    Ok(res)
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
        if !cfg.email.enabled || !cfg.email.sendable() {
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
        if !cfg.email.enabled || !cfg.email.sendable() {
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
            if ctx.is_shutting_down.load(std::sync::atomic::Ordering::Relaxed) { break; }
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
                if !n_core::scheduler::is_symbol_trading_time(&symbol, &Local::now()) { continue; }
                let ctx = ctx.clone();
                let bridge_port = config.data_source.bridge_port;
                jobs.spawn(async move {
                    use n_core::fetch::MarketDataSource;
                    let tq = ctx.services.data_source.tq_client().await;
                    let requested_at = Instant::now();
                    match tokio::time::timeout(Duration::from_millis(1000), tq.fetch_minute_raw(&symbol, "5m", 300)).await {
                        Ok(Ok(raw)) => {
                            let current = ctx.services.config().await;
                            if current.data_source.primary_source != "tqsdk"
                                || current.data_source.bridge_port != bridge_port
                                || !ctx.services.data_source.tq_is_available()
                            { return; }
                            let now = Local::now();
                            let bars = ctx.services.live_chart.publish_after(&symbol, raw.klines, now.naive_local(), requested_at);
                            for tf in n_core::derive::Timeframe::supported() {
                                let bar = bars.iter().find(|bar| bar.timeframe == tf.as_str());
                                let payload = serde_json::json!({
                                    "symbol": symbol, "timeframe": tf.as_str(), "bar": bar,
                                    "updated_at": now.timestamp_millis(),
                                });
                                ctx.realtime_hub.broadcast_kline(
                                    ctx.next_event_seq(), "kline.partial", &symbol, tf.as_str(), payload,
                                ).await;
                            }
                        }
                        _ => tracing::debug!(symbol, "后台实时柱更新未就绪，旧结果按新鲜度自动过期"),
                    }
                });
            }
            while let Some(result) = jobs.join_next().await {
                if let Err(error) = result { tracing::warn!(%error, "后台实时柱任务失败"); }
            }
        }
    });
}

fn spawn_quote_poller(ctx: Arc<ServerContext>) {
    tokio::spawn(async move {
        loop {
            let interval_ms = ctx.services.config().await.quote.poll_interval_ms;
            tokio::time::sleep(Duration::from_millis(interval_ms)).await;
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

            let contracts: Vec<String> = ctx.trader_snapshot.read().await["broker_positions"]
                .as_array()
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
                    // Normal price updates maintain all seeded periods without SQL or bridge reads.
                    let cfg = ctx.services.config().await;
                    let updated_bars = if cfg.data_source.primary_source == "tqsdk"
                        && ctx.services.data_source.tq_is_available()
                    {
                        ctx.services.live_chart.update_prices(&snapshots, Local::now().naive_local())
                    } else { Vec::new() };
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

                    for bar in updated_bars {
                        let sym = bar.symbol.clone();
                        let tf = bar.timeframe.clone();
                        ctx.realtime_hub.broadcast_kline(
                            ctx.next_event_seq(), "kline.partial", &sym, &tf,
                            serde_json::json!({ "symbol": sym, "timeframe": tf, "bar": bar }),
                        ).await;
                    }

                    // 检测入场价命中
                    if let Ok(hits) = ctx.services.entry_trigger_hits(&snapshots).await {
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

                    // 检测关键区域手工画线
                    if let Ok(alerts) = ctx.services.manual_level_alerts(&snapshots).await {
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
                            spawn_preclose_emails(ctx.clone(), updates);
                        }
                        Err(error) => tracing::warn!("收盘前预检测轮询失败: {error:#}"),
                        _ => {}
                    }

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
                            spawn_preclose_candidate_emails(ctx.clone(), updates);
                        }
                        Err(error) => tracing::warn!("临时未收盘推演轮询失败: {error:#}"),
                        _ => {}
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
                    tracing::info!(
                        "🔔 [FAST_PATH] 收到天勤闭合K线: {} {} {}",
                        ev.symbol,
                        ev.period,
                        ev.bar_end
                    );
                    let seq = ctx.next_event_seq();
                    let closed_dto = n_protocol::dto::KlineDto {
                        symbol: ev.symbol.clone(),
                        timeframe: ev.period.clone(),
                        ts: ev.bar_end.clone(),
                        open: ev.kline.open,
                        high: ev.kline.high,
                        low: ev.kline.low,
                        close: ev.kline.close,
                        volume: ev.kline.volume,
                        hold: ev.kline.hold,
                        source: "tqsdk_closed".to_string(),
                        rollover: false,
                    };
                    let payload = serde_json::json!({
                        "symbol": ev.symbol,
                        "timeframe": ev.period,
                        "bar": closed_dto,
                        "event": ev,
                    });
                    ctx.realtime_hub
                        .broadcast_kline(seq, "kline.closed", &ev.symbol, &ev.period, payload)
                        .await;
                }
            }
        }
    });
}

fn spawn_tq_warmup(ctx: Arc<ServerContext>) {
    tokio::spawn(async move {
        tokio::time::sleep(Duration::from_millis(800)).await;
        let cfg = ctx.services.config().await;
        if cfg.data_source.primary_source != "tqsdk" || !ctx.services.data_source.tq_is_available()
        {
            return;
        }
        let tq = ctx.services.data_source.tq_client().await;
        if let Ok(symbols) = n_core::storage::repo::list_symbols(&ctx.db, true).await {
            let codes: Vec<String> = symbols.into_iter().map(|s| s.code).collect();
            if !codes.is_empty() {
                tracing::info!(
                    "🔥 正在为 {} 个活跃品种预热天勤 5m K 线订阅...",
                    codes.len()
                );
                let _ = tq.subscribe_klines(&codes, "5m", 300).await;
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

    fn quote(code: &str, price: f64) -> (String, n_core::trader::model::ExecutionQuote) {
        (
            code.into(),
            n_core::trader::model::ExecutionQuote {
                instrument_id: code.into(),
                exchange_id: "SHFE".into(),
                underlying_symbol: String::new(),
                last: price,
                bid: price - 1.0,
                ask: price + 1.0,
                tick: 1.0,
                lower: 1.0,
                upper: 2000.0,
                timestamp: chrono::Utc::now().timestamp(),
            },
        )
    }

    #[tokio::test]
    async fn drains_full_queue_without_discarding_intermediate_ticks() {
        let (tx, mut rx) = tokio::sync::mpsc::channel(64);
        let first = vec![quote("pb2611", 1000.0)];
        for price in 0..64 {
            tx.try_send(vec![quote("pb2611", 990.0 + price as f64)])
                .unwrap();
        }
        let events = drain_quote_batches(&mut rx, first);
        assert_eq!(events.len(), 65);
        assert_eq!(events[0].1.last, 1000.0);
        assert_eq!(events[1].1.last, 990.0);
        assert_eq!(events[64].1.last, 1053.0);
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
            selected.iter().map(|(_, q)| q.last).collect::<Vec<_>>(),
            vec![1000.0, 990.0]
        );
        assert!(filter_quote_events(&events, &Default::default()).is_empty());
        let mut alias = quote("pb2611", 1001.0);
        alias.0 = "PB0".into();
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
