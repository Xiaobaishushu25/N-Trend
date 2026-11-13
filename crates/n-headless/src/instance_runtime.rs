//! Each account progresses in its own task; one unavailable broker never blocks another.
use crate::state::ServerContext;
use n_core::trader::{broker::BrokerClient, instances::*, strategy::StrategyEvent};
use serde_json::json;
use std::{
    collections::{HashMap, HashSet},
    sync::{atomic::Ordering, Arc},
    time::{Duration, Instant},
};
use tokio::sync::{mpsc, oneshot};

pub(crate) type HistoryRevision = Vec<(String, Option<String>, i64)>;

/// A cheap revision check avoids loading and allocating the complete history
/// every five seconds. Row counts also detect older bars being backfilled.
pub(crate) async fn market_history_revision(
    db: &n_core::sea_orm::DatabaseConnection,
    symbol: &str,
    timeframe: &str,
) -> anyhow::Result<HistoryRevision> {
    use n_core::sea_orm::{ConnectionTrait, DbBackend, Statement};
    let rows = db
        .query_all(Statement::from_sql_and_values(
            DbBackend::Sqlite,
            "SELECT timeframe, MAX(ts) AS latest, COUNT(*) AS row_count FROM klines \
         WHERE symbol=? AND timeframe IN ('5m', ?) AND (timeframe != '5m' OR source='raw') \
         GROUP BY timeframe ORDER BY timeframe",
            [symbol.into(), timeframe.into()],
        ))
        .await?;
    rows.into_iter()
        .map(|row| {
            Ok((
                row.try_get("", "timeframe")?,
                row.try_get("", "latest")?,
                row.try_get("", "row_count")?,
            ))
        })
        .collect()
}

pub fn spawn(ctx: Arc<ServerContext>) {
    tokio::spawn(async move {
        let mut started = HashSet::new();
        let mut timer = tokio::time::interval(Duration::from_secs(1));
        loop {
            timer.tick().await;
            if ctx.is_shutting_down.load(Ordering::Relaxed) {
                break;
            }
            let Some(manager) = ctx.instances.get() else {
                break;
            };
            let all = manager.all().await;
            let active = all
                .iter()
                .map(|r| r.definition.id.clone())
                .collect::<HashSet<_>>();
            let mut sidecars = ctx.ctp_sidecars.lock().await;
            let removed = sidecars
                .keys()
                .filter(|id| !active.contains(*id))
                .cloned()
                .collect::<Vec<String>>();
            for id in removed {
                if let Some(s) = sidecars.remove(&id) {
                    let s: crate::ctp_sidecar::CtpSidecar = s;
                    s.stop().await;
                }
            }
            drop(sidecars);
            for r in all {
                if r.definition.id == DEFAULT_INSTANCE || !started.insert(r.definition.id.clone()) {
                    continue;
                }
                if matches!(r.broker, BrokerClient::Disabled(_)) {
                    continue;
                }
                if let AccountKind::Ctp {
                    port,
                    broker_id,
                    user_id,
                    credential_ref: Some(reference),
                } = &manager
                    .account(&r.definition.id)
                    .await
                    .unwrap_or_else(|_| r.account.clone())
                    .kind
                {
                    let python = ctx.services.config().await.data_source.python_path;
                    let mut sidecars = ctx.ctp_sidecars.lock().await;
                    if !sidecars.contains_key(&r.definition.id) {
                        match crate::ctp_sidecar::CtpSidecar::start_account(
                            python.as_deref(),
                            *port,
                            ctx.data_dir
                                .join("ctp_credentials")
                                .join(format!("{reference}.json")),
                            ctx.data_dir
                                .join("trader_instances")
                                .join(&r.definition.id)
                                .join("flow_ctp"),
                            broker_id.clone(),
                            user_id.clone(),
                        ) {
                            Ok(s) => {
                                sidecars.insert(r.definition.id.clone(), s);
                            }
                            Err(e) => {
                                *r.error.write().await = Some(format!("CTP自动启动不可用: {e}"));
                            }
                        }
                    }
                }
                if let BrokerClient::Ctp(client) = r.broker.clone() {
                    let c = ctx.clone();
                    let account = r.clone();
                    tokio::spawn(async move {
                        let mut backoff = 1;
                        loop {
                            if c.is_shutting_down.load(Ordering::Relaxed)
                                || c.instances
                                    .get()
                                    .unwrap()
                                    .get(&account.definition.id)
                                    .await
                                    .is_err()
                            {
                                break;
                            }
                            let result: anyhow::Result<()> = async {
                                let mut stream = client.event_stream().await?;
                                loop {
                                    let mut s = stream.next_snapshot().await?;
                                    s["bridge_online"] = json!(true);
                                    let manager = c.instances.get().unwrap();
                                    manager
                                        .validate_snapshot(&account.definition.id, &s)
                                        .await?;
                                    account.engine.reconcile(&s).await?;
                                    account
                                        .engine
                                        .dispatch(StrategyEvent::ExecutionUpdate(s.clone()))
                                        .await?;
                                    *account.snapshot.write().await = s;
                                    *account.error.write().await = None;
                                    backoff = 1;
                                    if c.is_shutting_down.load(Ordering::Relaxed)
                                        || manager.get(&account.definition.id).await.is_err()
                                    {
                                        return Ok(());
                                    }
                                }
                            }
                            .await;
                            if let Err(e) = result {
                                *account.error.write().await = Some(e.to_string());
                                let mut s = account.snapshot.read().await.clone();
                                if !s.is_object() {
                                    s = json!({"health":{}})
                                }
                                s["bridge_online"] = json!(false);
                                s["health"]["ready"] = json!(false);
                                let _ = account.engine.reconcile(&s).await;
                                *account.snapshot.write().await = s;
                            }
                            tokio::time::sleep(Duration::from_secs(backoff)).await;
                            backoff = (backoff * 2).min(30);
                        }
                    });
                }
                let c = ctx.clone();
                tokio::spawn(async move {
                    run_account(c, r).await;
                });
            }
        }
        for (_, sidecar) in ctx.ctp_sidecars.lock().await.drain() {
            sidecar.stop().await;
        }
    });
}

type HistoryKey = (String, String);
type HistoryVersion = (String, HistoryRevision);

struct LoadedHistory {
    version: HistoryVersion,
    bars: Vec<n_core::analyze::model::Bar>,
    raw: Option<Vec<n_core::fetch::kline::Kline>>,
    rollovers: Vec<n_core::derive::rollover::RolloverRecord>,
}

struct HistoryMessage {
    key: HistoryKey,
    result: anyhow::Result<LoadedHistory>,
    applied: oneshot::Sender<bool>,
}

type QuoteBatch = Vec<(String, n_core::trader::model::ExecutionQuote)>;

enum AccountEvent {
    Quotes(QuoteBatch),
    History(HistoryMessage),
    Tick,
    Closed,
}

async fn next_account_event(
    timer: &mut tokio::time::Interval,
    quotes: &mut mpsc::Receiver<QuoteBatch>,
    history: &mut mpsc::Receiver<HistoryMessage>,
) -> AccountEvent {
    tokio::select! {
        _ = timer.tick() => AccountEvent::Tick,
        batch = quotes.recv() => batch.map_or(AccountEvent::Closed, AccountEvent::Quotes),
        update = history.recv(), if !history.is_closed() || !history.is_empty() =>
            update.map_or(AccountEvent::Tick, AccountEvent::History),
    }
}

/// One in-flight load and one bounded handoff per account. Database/cache reads
/// never run in the quote consumer; only that consumer applies engine updates.
struct HistoryLoader {
    updates: mpsc::Receiver<HistoryMessage>,
    task: tokio::task::JoinHandle<()>,
}
impl Drop for HistoryLoader {
    fn drop(&mut self) {
        self.task.abort();
    }
}
impl HistoryLoader {
    fn spawn(ctx: Arc<ServerContext>, r: Arc<InstanceRuntime>) -> Self {
        let (tx, updates) = mpsc::channel(1);
        let task = tokio::spawn(async move {
            let mut versions = HashMap::<HistoryKey, HistoryVersion>::new();
            let mut timer = tokio::time::interval(Duration::from_secs(5));
            timer.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
            loop {
                timer.tick().await;
                if ctx.is_shutting_down.load(Ordering::Relaxed) || tx.is_closed() {
                    break;
                }
                let mut subscriptions = r
                    .engine
                    .active_symbols()
                    .await
                    .into_iter()
                    .map(|s| (s, "15m".to_owned()))
                    .collect::<Vec<_>>();
                subscriptions.extend(
                    r.engine
                        .market_subscriptions()
                        .await
                        .into_iter()
                        .filter_map(|s| s.timeframe.map(|tf| (s.symbol, tf))),
                );
                subscriptions.sort();
                subscriptions.dedup();
                let strategy_revision = r.engine.definition().await.revision;
                for key in subscriptions {
                    let started = Instant::now();
                    let result: anyhow::Result<Option<LoadedHistory>> = async {
                        let version = (
                            strategy_revision.clone(),
                            market_history_revision(&ctx.db, &key.0, &key.1).await?,
                        );
                        if versions.get(&key) == Some(&version) {
                            return Ok(None);
                        }
                        let mut raw = None;
                        let mut rollovers = Vec::new();
                        if key.1 == "15m" && r.engine.needs_completed_15m().await {
                            raw = Some(
                                n_core::storage::repo::raw_klines(&ctx.db, &key.0)
                                    .await?
                                    .iter()
                                    .map(n_core::service::model_to_fetch)
                                    .collect(),
                            );
                            if r.engine.needs_market_history().await {
                                rollovers =
                                    n_core::storage::repo::symbol_rollovers(&ctx.db, &key.0)
                                        .await?
                                        .into_iter()
                                        .map(|v| n_core::derive::rollover::RolloverRecord {
                                            symbol: v.symbol,
                                            ts: v.ts,
                                            from_contract: v.from_contract,
                                            to_contract: v.to_contract,
                                            confirmed: v.confirmed,
                                        })
                                        .collect();
                            }
                        }
                        let bars = ctx.services.bars_for(&key.0, &key.1).await?;
                        Ok(Some(LoadedHistory {
                            version,
                            bars,
                            raw,
                            rollovers,
                        }))
                    }
                    .await;
                    if matches!(result, Ok(None)) {
                        continue;
                    }
                    let result = result.map(|v| v.expect("unchanged history already skipped"));
                    let version = result.as_ref().ok().map(|v| v.version.clone());
                    let (applied, acknowledgment) = oneshot::channel();
                    if started.elapsed() >= Duration::from_millis(250) {
                        tracing::warn!(instance_id = %r.definition.id, symbol = %key.0,
                            timeframe = %key.1, load_ms = started.elapsed().as_millis(),
                            "实例后台历史读取较慢，行情消费继续独立运行");
                    }
                    if tx
                        .send(HistoryMessage {
                            key: key.clone(),
                            result,
                            applied,
                        })
                        .await
                        .is_err()
                    {
                        return;
                    }
                    // Retry failed/stale updates: never mark an unapplied version fresh.
                    if acknowledgment.await.unwrap_or(false) {
                        if let Some(version) = version {
                            versions.insert(key, version);
                        }
                    }
                }
            }
        });
        Self { updates, task }
    }
}

/// The default account already has its own quote task. Its maintenance timer
/// uses the same bounded loader without waiting on database reads either.
pub(crate) struct DefaultHistory {
    loader: HistoryLoader,
    last_bars: HashMap<HistoryKey, String>,
}
impl DefaultHistory {
    pub(crate) fn spawn(ctx: Arc<ServerContext>, r: Arc<InstanceRuntime>) -> Self {
        Self {
            loader: HistoryLoader::spawn(ctx, r),
            last_bars: HashMap::new(),
        }
    }

    pub(crate) async fn apply_ready(
        &mut self,
        engine: &n_core::trader::ExecutionEngine,
    ) -> anyhow::Result<()> {
        let Ok(message) = self.loader.updates.try_recv() else {
            return Ok(());
        };
        let result = match message.result {
            Ok(update) => apply_history(engine, &message.key, update, &mut self.last_bars).await,
            Err(error) => Err(error),
        };
        let _ = message.applied.send(matches!(&result, Ok(true)));
        result.map(|_| ())
    }
}

async fn apply_history(
    engine: &n_core::trader::ExecutionEngine,
    key: &HistoryKey,
    update: LoadedHistory,
    last_bars: &mut HashMap<HistoryKey, String>,
) -> anyhow::Result<bool> {
    if engine.definition().await.revision != update.version.0 {
        return Ok(false);
    }
    if let Some(raw) = update.raw {
        engine
            .on_market_history(&key.0, &raw, update.rollovers)
            .await?;
        engine.on_completed_15m(&key.0, &raw).await?;
    }
    let last = match last_bars.get(key) {
        Some(last) => Some(last.clone()),
        None if key.1 == "15m" => engine.last_bar(&key.0).await,
        None => None,
    };
    let pending = match last {
        Some(last) => update
            .bars
            .iter()
            .filter(|b| b.dt.to_bar_ts() > last)
            .collect::<Vec<_>>(),
        None => update.bars.last().into_iter().collect(),
    };
    for bar in pending {
        let timestamp = bar.dt.to_bar_ts();
        if key.1 == "15m" {
            engine
                .on_price_update(&key.0, bar.high, bar.low, bar.close, &timestamp, true)
                .await?;
        } else {
            engine
                .dispatch(StrategyEvent::BarClosed {
                    symbol: key.0.clone(),
                    timeframe: key.1.clone(),
                    timestamp: timestamp.clone(),
                    high: bar.high,
                    low: bar.low,
                    close: bar.close,
                })
                .await?;
        }
        last_bars.insert(key.clone(), timestamp);
    }
    if key.1 == "15m" {
        engine
            .reconcile_entry_age(
                &key.0,
                &update
                    .bars
                    .iter()
                    .map(|b| b.dt.to_bar_ts())
                    .collect::<Vec<_>>(),
            )
            .await?;
    }
    Ok(engine.definition().await.revision == update.version.0)
}

pub(crate) fn warn_calendar_coverage(
    instance: &str,
    diagnostics: &serde_json::Value,
    previous: &mut HashSet<String>,
) {
    let mut current = HashSet::new();
    for health in diagnostics["calendar_health"]
        .as_array()
        .into_iter()
        .flatten()
    {
        if health["status"] == "ok" {
            continue;
        }
        let key = format!(
            "{}:{}:{}:{}",
            health["revision"],
            health["calendar_version"],
            health["status"],
            chrono::Utc::now().format("%Y-%m-%d")
        );
        if !previous.contains(&key) {
            tracing::warn!(instance_id = instance, calendar = %health,
                "交易日历覆盖不足或将在三天内到期，请配置新的日历版本");
        }
        current.insert(key);
    }
    *previous = current;
}

async fn run_account(ctx: Arc<ServerContext>, r: Arc<InstanceRuntime>) {
    let Some(mut rx) = r.quote_rx.lock().await.take() else {
        return;
    };
    let mut timer = tokio::time::interval(Duration::from_secs(1));
    timer.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    let mut publisher =
        n_protocol::trader_stream::TraderPublisher::new(uuid::Uuid::new_v4().to_string());
    let mut last_bars = HashMap::<(String, String), String>::new();
    let mut history = HistoryLoader::spawn(ctx.clone(), r.clone());
    let mut history_errors = HashMap::<HistoryKey, String>::new();
    let mut calendar_warnings = HashSet::new();
    let mut ticks = 0u64;
    loop {
        let (quotes, loaded) =
            match next_account_event(&mut timer, &mut rx, &mut history.updates).await {
                AccountEvent::Quotes(batch) => (Some(batch), None),
                AccountEvent::History(update) => (None, Some(update)),
                AccountEvent::Tick => (None, None),
                AccountEvent::Closed => break,
            };
        let quotes = quotes.map(|events| crate::runtime::drain_quote_batches(&mut rx, events));
        let manager = ctx.instances.get().unwrap();
        if ctx.is_shutting_down.load(Ordering::Relaxed)
            || manager.get(&r.definition.id).await.is_err()
        {
            break;
        }
        let result: anyhow::Result<()> = async {
            if r.quote_gap.swap(false, Ordering::Relaxed) {
                r.engine
                    .quote_gap_with_reason("本实例行情消费队列已满")
                    .await?;
            }
            if let Some(events) = quotes {
                if let BrokerClient::Paper(p) = &r.broker {
                    p.on_quotes(&events)?;
                    r.engine.reconcile(&p.snapshot()).await?;
                }
                r.engine.on_quote_batch(events).await?;
            }
            if let Err(e) = r
                .engine
                .dispatch(StrategyEvent::Timer {
                    timestamp: chrono::Utc::now().timestamp(),
                })
                .await
            {
                r.engine.pause(true).await?;
                tracing::error!("实例策略定时处理失败，继续执行平仓: {e}");
            }
            r.engine.drive().await?;
            if let BrokerClient::Paper(p) = &r.broker {
                let s = p.snapshot();
                r.engine.reconcile(&s).await?;
                r.engine
                    .dispatch(StrategyEvent::ExecutionUpdate(s.clone()))
                    .await?;
                *r.snapshot.write().await = s;
            }
            // Apply at most one loaded symbol between quote batches. Slow I/O
            // remains in the background; engine mutations stay serialized here.
            if let Some(message) = loaded.or_else(|| history.updates.try_recv().ok()) {
                let applied_at = Instant::now();
                let result = match message.result {
                    Ok(update) => {
                        apply_history(&r.engine, &message.key, update, &mut last_bars).await
                    }
                    Err(error) => Err(error),
                };
                let applied = match result {
                    Ok(applied) => {
                        history_errors.remove(&message.key);
                        applied
                    }
                    Err(error) => {
                        tracing::warn!(instance_id = %r.definition.id, symbol = %message.key.0,
                            error = %error, "实例历史更新失败，将重试；继续行情和风险退出");
                        history_errors.insert(message.key, error.to_string());
                        false
                    }
                };
                let _ = message.applied.send(applied);
                if applied_at.elapsed() >= Duration::from_millis(250) {
                    tracing::warn!(instance_id = %r.definition.id,
                        apply_ms = applied_at.elapsed().as_millis(), quote_queue_batches = rx.len(),
                        "实例历史应用较慢，请检查策略计算与持久化耗时");
                }
            }
            *r.runtime_error.write().await = None;
            let mut snapshot = manager.build_snapshot(&r.definition.id).await?;
            warn_calendar_coverage(
                &r.definition.id,
                &snapshot["strategy_diagnostics"],
                &mut calendar_warnings,
            );
            snapshot["runtime_diagnostics"] = json!({
                "quote_queue_batches":rx.len(),
                "history_errors":history_errors.iter().map(|(key, error)|
                    json!({"symbol":key.0,"timeframe":key.1,"error":error})).collect::<Vec<_>>()});
            if let Err(e) = manager.sample_equity(&r.definition.id, &snapshot).await {
                tracing::warn!("账户净值采样失败: {e}");
            }
            let (baseline, delta) = publisher.update(snapshot.take());
            *r.published.write().await = baseline.clone();
            if let Some(delta) = delta {
                ctx.realtime_hub
                    .broadcast(
                        ctx.next_event_seq(),
                        "trader.instance.delta",
                        json!({"instance_id":r.definition.id,"delta":delta}),
                    )
                    .await;
            }
            // The cached baseline is used for REST recovery; never share sequences across instances.
            // Store only broker data separately; the publisher owns its own monotonically increasing stream.
            if ticks % 15 == 0 {
                ctx.realtime_hub
                    .broadcast(
                        ctx.next_event_seq(),
                        "trader.instance.heartbeat",
                        json!({"instance_id":r.definition.id,"heartbeat":publisher.heartbeat()}),
                    )
                    .await;
            }
            let _ = baseline;
            Ok(())
        }
        .await;
        if let Err(e) = result {
            *r.runtime_error.write().await = Some(e.to_string());
            if let Ok(snapshot) = manager.build_snapshot(&r.definition.id).await {
                let (baseline, delta) = publisher.update(snapshot);
                *r.published.write().await = baseline;
                if let Some(delta) = delta {
                    ctx.realtime_hub
                        .broadcast(
                            ctx.next_event_seq(),
                            "trader.instance.delta",
                            json!({"instance_id":r.definition.id,"delta":delta}),
                        )
                        .await;
                }
            }
            tracing::warn!("实例 {} 推进失败: {e:#}", r.definition.id);
        }
        ticks = ticks.wrapping_add(1);
    }
}

#[cfg(test)]
mod history_revision_tests {
    use super::*;
    use n_core::sea_orm::{ConnectionTrait, Database, DbBackend, Statement};

    #[tokio::test]
    async fn pending_history_reads_do_not_block_quotes_or_risk_timers() {
        let (quotes_tx, mut quotes) = mpsc::channel(64);
        let (_history_tx, mut history) = mpsc::channel(1);
        let mut timer = tokio::time::interval(Duration::from_secs(3600));
        timer.tick().await;
        // The history sender is alive but no I/O result is available.
        quotes_tx.send(Vec::new()).await.unwrap();
        let event = tokio::time::timeout(
            Duration::from_secs(1),
            next_account_event(&mut timer, &mut quotes, &mut history),
        )
        .await
        .unwrap();
        assert!(matches!(event, AccountEvent::Quotes(_)));
        timer.reset_immediately();
        let event = tokio::time::timeout(
            Duration::from_secs(1),
            next_account_event(&mut timer, &mut quotes, &mut history),
        )
        .await
        .unwrap();
        assert!(matches!(event, AccountEvent::Tick));
    }

    #[tokio::test]
    async fn failed_history_handoff_can_retry_and_closed_worker_does_not_spin() {
        let (quotes_tx, mut quotes) = mpsc::channel(1);
        let (history_tx, mut history) = mpsc::channel(1);
        let mut timer = tokio::time::interval(Duration::from_secs(3600));
        timer.tick().await;
        let (applied, acknowledgment) = oneshot::channel();
        history_tx
            .send(HistoryMessage {
                key: ("JD0".into(), "15m".into()),
                result: Err(anyhow::anyhow!("temporary read failure")),
                applied,
            })
            .await
            .unwrap();
        let AccountEvent::History(message) =
            next_account_event(&mut timer, &mut quotes, &mut history).await
        else {
            panic!("history result must wake the consumer");
        };
        assert!(message.result.is_err());
        message.applied.send(false).unwrap();
        assert!(!acknowledgment.await.unwrap());
        drop(history_tx);
        // A closed worker must not produce an endless stream of ready events.
        assert!(tokio::time::timeout(
            Duration::from_millis(20),
            next_account_event(&mut timer, &mut quotes, &mut history)
        )
        .await
        .is_err());
        quotes_tx.send(Vec::new()).await.unwrap();
        assert!(matches!(
            next_account_event(&mut timer, &mut quotes, &mut history).await,
            AccountEvent::Quotes(_)
        ));
    }

    #[tokio::test]
    async fn stale_history_revision_never_advances_cursors() {
        let engine = n_core::trader::ExecutionEngine::new(
            n_core::trader::CtpTraderClient::for_port(1),
            Default::default(),
        );
        let key = ("JD0".into(), "15m".into());
        let mut cursors = HashMap::from([(key.clone(), "2026-11-13 10:00:00".into())]);
        let old = cursors.clone();
        assert!(!apply_history(
            &engine,
            &key,
            LoadedHistory {
                version: ("retired-strategy-revision".into(), Vec::new()),
                bars: Vec::new(),
                raw: None,
                rollovers: Vec::new(),
            },
            &mut cursors
        )
        .await
        .unwrap());
        assert_eq!(cursors, old);
    }

    #[tokio::test]
    async fn dropping_history_loader_aborts_its_background_task() {
        let (_tx, updates) = mpsc::channel(1);
        let task = tokio::spawn(std::future::pending::<()>());
        let handle = task.abort_handle();
        drop(HistoryLoader { updates, task });
        tokio::task::yield_now().await;
        assert!(handle.is_finished());
    }

    #[tokio::test]
    async fn revision_detects_backfill_and_derived_updates_without_loading_bar_payloads() {
        let db = Database::connect("sqlite::memory:").await.unwrap();
        db.execute(Statement::from_string(
            DbBackend::Sqlite,
            "CREATE TABLE klines(symbol TEXT,timeframe TEXT,ts TEXT,source TEXT)".to_owned(),
        ))
        .await
        .unwrap();
        let empty = market_history_revision(&db, "PB0", "15m").await.unwrap();
        assert!(empty.is_empty());
        db.execute(Statement::from_string(DbBackend::Sqlite,
            "INSERT INTO klines VALUES('PB0','5m','2026-10-27 10:05:00','raw'),('PB0','15m','2026-10-27 10:00:00','derived')".to_owned()
        )).await.unwrap();
        let initial = market_history_revision(&db, "PB0", "15m").await.unwrap();
        assert_eq!(
            initial,
            market_history_revision(&db, "PB0", "15m").await.unwrap()
        );
        db.execute(Statement::from_string(
            DbBackend::Sqlite,
            "INSERT INTO klines VALUES('PB0','5m','2026-10-27 10:00:00','raw')".to_owned(),
        ))
        .await
        .unwrap();
        let backfilled = market_history_revision(&db, "PB0", "15m").await.unwrap();
        assert_ne!(initial, backfilled);
        assert_eq!(initial[1].1, backfilled[1].1);
        db.execute(Statement::from_string(DbBackend::Sqlite,
            "INSERT INTO klines VALUES('PB0','5m','2026-10-27 10:10:00','derived'),('CU0','5m','2026-10-27 10:10:00','raw')".to_owned()
        )).await.unwrap();
        assert_eq!(
            backfilled,
            market_history_revision(&db, "PB0", "15m").await.unwrap()
        );
        db.execute(Statement::from_string(
            DbBackend::Sqlite,
            "INSERT INTO klines VALUES('PB0','15m','2026-10-27 10:15:00','derived')".to_owned(),
        ))
        .await
        .unwrap();
        assert_ne!(
            backfilled,
            market_history_revision(&db, "PB0", "15m").await.unwrap()
        );
    }
}
