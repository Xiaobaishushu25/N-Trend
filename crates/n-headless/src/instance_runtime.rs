//! Each account progresses in its own task; one unavailable broker never blocks another.
use crate::state::ServerContext;
use n_core::trader::{
    broker::BrokerClient, execution::ExecutionTimings, instances::*,
    model::EXECUTION_QUOTE_MAX_AGE_SECONDS, strategy::StrategyEvent,
};
use serde_json::json;
use std::{
    collections::{HashMap, HashSet},
    future::Future,
    sync::{atomic::Ordering, Arc},
    time::{Duration, Instant},
};
use tokio::sync::{mpsc, oneshot, Mutex, RwLock};

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
                        let mut last_slow_broker_report: Option<Instant> = None;
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
                                    let started = Instant::now();
                                    let mut validate_time = Duration::ZERO;
                                    let mut reconcile_time = Duration::ZERO;
                                    let mut dispatch_time = Duration::ZERO;
                                    let (result, execution) = ExecutionTimings::measure(async {
                                        measure_stage(
                                            &mut validate_time,
                                            manager.validate_snapshot(&account.definition.id, &s),
                                        ).await?;
                                        let evs = measure_stage(
                                            &mut reconcile_time, account.engine.reconcile(&s),
                                        ).await?;
                                        dispatch_live_trade_emails(&c, &account.definition.name, evs);
                                        measure_stage(
                                            &mut dispatch_time,
                                            account.engine.dispatch(StrategyEvent::ExecutionUpdate(s.clone())),
                                        ).await
                                    }).await;
                                    let elapsed = started.elapsed();
                                    if elapsed >= Duration::from_millis(500)
                                        && last_slow_broker_report
                                            .is_none_or(|last| last.elapsed() >= Duration::from_secs(5))
                                    {
                                        tracing::warn!(instance_id = %account.definition.id,
                                            processing_ms = elapsed.as_millis(),
                                            validate_ms = validate_time.as_millis(),
                                            reconcile_ms = reconcile_time.as_millis(),
                                            strategy_dispatch_ms = dispatch_time.as_millis(),
                                            state_lock_wait_ms = execution.state_lock_wait.as_millis(),
                                            save_ms = execution.save_total.as_millis(),
                                            sqlite_write_ms = execution.sqlite_write.as_millis(),
                                            sqlite_commit_ms = execution.sqlite_commit.as_millis(),
                                            "实例柜台回报应用较慢");
                                        last_slow_broker_report = Some(Instant::now());
                                    }
                                    result?;
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
type HistoryVersion = (String, i64, HistoryRequirements);

#[derive(Clone, Copy, Default, PartialEq, Eq)]
struct HistoryRequirements {
    raw: bool,
    rollovers: bool,
}

#[derive(Clone)]
struct MarketHistory {
    bars: Arc<Vec<n_core::analyze::model::Bar>>,
    raw: Option<Arc<Vec<n_core::fetch::kline::Kline>>>,
    rollovers: Option<Arc<Vec<n_core::derive::rollover::RolloverRecord>>>,
}

struct LoadedHistory {
    version: HistoryVersion,
    data: MarketHistory,
}

type HistorySlot = Arc<Mutex<Option<(i64, MarketHistory)>>>;

/// One cached history per symbol and timeframe, shared by all accounts.
/// The per-key lock prevents duplicate loads without delaying other symbols.
#[derive(Default)]
pub(crate) struct HistoryCache {
    slots: Mutex<HashMap<HistoryKey, HistorySlot>>,
}

impl HistoryCache {
    async fn load(
        &self,
        db: &n_core::sea_orm::DatabaseConnection,
        services: &n_core::service::Services,
        key: &HistoryKey,
        strategy_revision: &str,
        needs: HistoryRequirements,
        applied: Option<&HistoryVersion>,
    ) -> anyhow::Result<Option<LoadedHistory>> {
        use n_core::storage::{cache_revisions::market_history_revision, repo};
        let (symbol, timeframe) = key;
        let slot = self
            .slots
            .lock()
            .await
            .entry(key.clone())
            .or_default()
            .clone();
        let mut slot = slot.lock().await;
        for _ in 0..3 {
            let revision = market_history_revision(db, symbol).await?;
            let version = (strategy_revision.to_owned(), revision, needs);
            if applied == Some(&version) {
                return Ok(None);
            }
            let cached = slot.as_ref().filter(|(rev, _)| *rev == revision);
            if let Some((_, data)) = cached.filter(|(_, data)| {
                (!needs.raw || data.raw.is_some()) && (!needs.rollovers || data.rollovers.is_some())
            }) {
                return Ok(Some(LoadedHistory {
                    version,
                    data: data.clone(),
                }));
            }

            let mut data = match cached {
                Some((_, data)) => data.clone(),
                None => MarketHistory {
                    bars: services.shared_bars_for(symbol, timeframe).await?,
                    raw: None,
                    rollovers: None,
                },
            };
            if needs.raw && data.raw.is_none() {
                data.raw = Some(services.shared_raw_for(symbol).await?);
            }
            if needs.rollovers && data.rollovers.is_none() {
                data.rollovers = Some(Arc::new(
                    repo::symbol_rollovers(db, symbol)
                        .await?
                        .into_iter()
                        .map(|v| n_core::derive::rollover::RolloverRecord {
                            symbol: v.symbol,
                            ts: v.ts,
                            from_contract: v.from_contract,
                            to_contract: v.to_contract,
                            confirmed: v.confirmed,
                        })
                        .collect(),
                ));
            }
            // A write during the reads may have mixed two database versions.
            if market_history_revision(db, symbol).await? == revision {
                *slot = Some((revision, data.clone()));
                return Ok(Some(LoadedHistory { version, data }));
            }
        }
        anyhow::bail!("行情历史加载期间持续更新，稍后重试: {symbol}/{timeframe}")
    }
}

struct HistoryMessage {
    key: HistoryKey,
    result: anyhow::Result<LoadedHistory>,
    applied: oneshot::Sender<bool>,
}

type QuoteBatch = Vec<n_core::trader::model::ReceivedExecutionQuote>;

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
                let needs_raw = r.engine.needs_completed_15m().await;
                let needs_rollovers = needs_raw && r.engine.needs_market_history().await;
                for key in subscriptions {
                    let started = Instant::now();
                    let needs = HistoryRequirements {
                        raw: key.1 == "15m" && needs_raw,
                        rollovers: key.1 == "15m" && needs_rollovers,
                    };
                    let result = match ctx
                        .history_cache
                        .load(
                            &ctx.db,
                            &ctx.services,
                            &key,
                            &strategy_revision,
                            needs,
                            versions.get(&key),
                        )
                        .await
                    {
                        Ok(None) => continue,
                        Ok(Some(update)) => Ok(update),
                        Err(err) => Err(err),
                    };
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
    let needs = update.version.2;
    if needs.raw {
        let raw = update
            .data
            .raw
            .as_ref()
            .expect("requested raw history was loaded");
        let rollovers = if needs.rollovers {
            update
                .data
                .rollovers
                .as_deref()
                .expect("requested rollovers were loaded")
                .clone()
        } else {
            Vec::new()
        };
        engine.on_market_history(&key.0, raw, rollovers).await?;
        engine.on_completed_15m(&key.0, raw).await?;
    }
    let last = match last_bars.get(key) {
        Some(last) => Some(last.clone()),
        None if key.1 == "15m" => engine.last_bar(&key.0).await,
        None => None,
    };
    let pending = match last {
        Some(last) => update
            .data
            .bars
            .iter()
            .filter(|b| b.dt.to_bar_ts() > last)
            .collect::<Vec<_>>(),
        None => update.data.bars.last().into_iter().collect(),
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
                    .data
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
    calendar: Option<&serde_json::Value>,
    previous: &mut HashSet<String>,
) {
    let mut current = HashSet::new();
    for health in calendar
        .and_then(serde_json::Value::as_array)
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

/// UI serialization, websocket publication and equity SQL never execute on
/// the quote consumer. One fixed-rate worker per instance bounds this cost
/// regardless of quote frequency; dropping the consumer aborts its worker.
struct InstancePublisher(tokio::task::JoinHandle<()>);
impl Drop for InstancePublisher {
    fn drop(&mut self) {
        self.0.abort();
    }
}
impl InstancePublisher {
    fn spawn(
        ctx: Arc<ServerContext>,
        r: Arc<InstanceRuntime>,
        diagnostics: Arc<RwLock<serde_json::Value>>,
    ) -> Self {
        Self(tokio::spawn(async move {
            let mut timer = tokio::time::interval(Duration::from_secs(1));
            timer.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
            let mut publisher =
                n_protocol::trader_stream::TraderPublisher::new(uuid::Uuid::new_v4().to_string());
            let mut fields = n_core::trader::execution::ExecutionSnapshotCache::default();
            let mut calendar_warnings = HashSet::new();
            let mut last_heartbeat = Instant::now();
            loop {
                timer.tick().await;
                if ctx.is_shutting_down.load(Ordering::Relaxed) {
                    break;
                }
                let manager = ctx.instances.get().unwrap();
                if manager.get(&r.definition.id).await.is_err() {
                    break;
                }
                let started = Instant::now();
                let mut snapshot_time = Duration::ZERO;
                let mut publish_time = Duration::ZERO;
                let mut equity_time = Duration::ZERO;
                let result: anyhow::Result<()> = async {
                    let mut snapshot = measure_stage(
                        &mut snapshot_time,
                        manager.build_shared_snapshot(&r.definition.id, &mut fields),
                    ).await?;
                    let publish_started = Instant::now();
                    let calendar = snapshot.object("strategy_diagnostics")
                        .and_then(|fields| fields.get("calendar_health"));
                    warn_calendar_coverage(&r.definition.id, calendar, &mut calendar_warnings);
                    snapshot.insert("runtime_diagnostics", diagnostics.read().await.clone());
                    let (baseline, delta, diagnostics) = publisher.update_shared(snapshot, true);
                    // Publish before potentially slow equity SQL, which must
                    // not hold up REST recovery or the quote/exit task either.
                    *r.published.write().await = baseline.clone();
                    if let Some(delta) = delta {
                        ctx.realtime_hub.broadcast_instance_delta(
                            ctx.next_event_seq(), &r.definition.id, delta, diagnostics, &baseline
                        ).await;
                    }
                    if last_heartbeat.elapsed() >= Duration::from_secs(15) {
                        ctx.realtime_hub.broadcast(ctx.next_event_seq(), "trader.instance.heartbeat",
                            json!({"instance_id":r.definition.id,"heartbeat":publisher.heartbeat()})).await;
                        last_heartbeat = Instant::now();
                    }
                    publish_time = publish_started.elapsed();
                    // Only the small broker snapshot is needed for sampling.
                    let broker = r.snapshot.read().await.clone();
                    measure_stage(&mut equity_time, manager.sample_equity(&r.definition.id, &broker)).await?;
                    Ok(())
                }.await;
                if let Err(error) = result {
                    tracing::warn!(instance_id = %r.definition.id, error = %error, "实例状态发布/净值采样失败，行情消费继续独立运行");
                }
                if started.elapsed() >= Duration::from_millis(500) {
                    tracing::warn!(instance_id = %r.definition.id,
                        publish_ms = started.elapsed().as_millis(),
                        snapshot_ms = snapshot_time.as_millis(),
                        websocket_publish_ms = publish_time.as_millis(),
                        equity_sample_ms = equity_time.as_millis(),
                        "实例后台发布较慢");
                }
            }
        }))
    }
}

async fn measure_stage<T>(elapsed: &mut Duration, operation: impl Future<Output = T>) -> T {
    let started = Instant::now();
    let result = operation.await;
    *elapsed += started.elapsed();
    result
}

#[derive(Default)]
struct AccountCycleTimings {
    elapsed: Duration,
    paper_quotes: Duration,
    engine_quotes: Duration,
    timer: Duration,
    paper_sync: Duration,
    history: Duration,
    final_drive: Duration,
    execution: ExecutionTimings,
    batch_quotes: usize,
    waiting_batches_at_start: usize,
    waiting_batches_at_end: usize,
    oldest_quote_age_seconds: i64,
    oldest_feed_to_consumer_seconds: i64,
    quotes_past_age_limit: usize,
    finished_at_ms: i64,
    failed: bool,
}

#[derive(Default)]
struct SlowAccountCycles {
    slowest: Option<AccountCycleTimings>,
    slow_cycles: usize,
    quotes_past_age_limit: usize,
    max_quote_age_seconds: i64,
    max_feed_to_consumer_seconds: i64,
    max_waiting_batches: usize,
}

impl SlowAccountCycles {
    fn observe(&mut self, cycle: AccountCycleTimings) {
        let slow = cycle.elapsed >= Duration::from_millis(250);
        if !slow && cycle.quotes_past_age_limit == 0 {
            return;
        }
        self.slow_cycles += usize::from(slow);
        self.quotes_past_age_limit += cycle.quotes_past_age_limit;
        self.max_quote_age_seconds = self.max_quote_age_seconds.max(cycle.oldest_quote_age_seconds);
        self.max_feed_to_consumer_seconds = self
            .max_feed_to_consumer_seconds
            .max(cycle.oldest_feed_to_consumer_seconds);
        self.max_waiting_batches = self
            .max_waiting_batches
            .max(cycle.waiting_batches_at_start.max(cycle.waiting_batches_at_end));
        if self
            .slowest
            .as_ref()
            .is_none_or(|slowest| cycle.elapsed > slowest.elapsed)
        {
            self.slowest = Some(cycle);
        }
    }

    fn report(&mut self, instance_id: &str) {
        let Some(cycle) = self.slowest.take() else {
            return;
        };
        let execution = cycle.execution;
        tracing::warn!(
            instance_id, slow_cycles = self.slow_cycles,
            quotes_past_age_limit = self.quotes_past_age_limit,
            max_quote_age_seconds = self.max_quote_age_seconds,
            max_feed_to_consumer_seconds = self.max_feed_to_consumer_seconds,
            max_waiting_batches = self.max_waiting_batches,
            batch_quotes = cycle.batch_quotes, failed = cycle.failed,
            sampled_cycle_finished_at_ms = cycle.finished_at_ms,
            processing_ms = cycle.elapsed.as_millis(),
            paper_quotes_ms = cycle.paper_quotes.as_millis(),
            engine_quotes_ms = cycle.engine_quotes.as_millis(),
            timer_ms = cycle.timer.as_millis(),
            paper_sync_ms = cycle.paper_sync.as_millis(),
            history_apply_ms = cycle.history.as_millis(),
            final_drive_ms = cycle.final_drive.as_millis(),
            observe_quotes_ms = execution.observe_quotes.as_millis(),
            strategy_handler_ms = execution.strategy_handler.as_millis(),
            flush_quotes_ms = execution.flush_quotes.as_millis(),
            drive_after_quotes_ms = execution.drive_after_quotes.as_millis(),
            drive_lock_wait_ms = execution.drive_lock_wait.as_millis(),
            state_lock_wait_ms = execution.state_lock_wait.as_millis(),
            broker_request_ms = execution.broker_requests.as_millis(),
            save_count = execution.save_count, save_ms = execution.save_total.as_millis(),
            sqlite_write_ms = execution.sqlite_write.as_millis(),
            sqlite_commit_ms = execution.sqlite_commit.as_millis(),
            "实例执行消费较慢或报价过期"
        );
        *self = Self::default();
    }
}

#[cfg(test)]
#[test]
fn slow_cycle_summary_keeps_the_slowest_breakdown_and_counts_expired_quotes() {
    let mut summary = SlowAccountCycles::default();
    summary.observe(AccountCycleTimings {
        elapsed: Duration::from_millis(100),
        ..Default::default()
    });
    assert!(summary.slowest.is_none());

    summary.observe(AccountCycleTimings {
        elapsed: Duration::from_millis(800),
        history: Duration::from_millis(600),
        waiting_batches_at_end: 4,
        ..Default::default()
    });
    summary.observe(AccountCycleTimings {
        elapsed: Duration::from_millis(100),
        oldest_quote_age_seconds: 20,
        oldest_feed_to_consumer_seconds: 16,
        quotes_past_age_limit: 3,
        ..Default::default()
    });
    assert_eq!(summary.slowest.unwrap().history, Duration::from_millis(600));
    assert_eq!(summary.slow_cycles, 1);
    assert_eq!(summary.quotes_past_age_limit, 3);
    assert_eq!(summary.max_quote_age_seconds, 20);
    assert_eq!(summary.max_feed_to_consumer_seconds, 16);
    assert_eq!(summary.max_waiting_batches, 4);
}

async fn run_account(ctx: Arc<ServerContext>, r: Arc<InstanceRuntime>) {
    let Some(mut rx) = r.quote_rx.lock().await.take() else {
        return;
    };
    let mut timer = tokio::time::interval(Duration::from_secs(1));
    timer.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    let diagnostics = Arc::new(RwLock::new(json!({})));
    let _publisher = InstancePublisher::spawn(ctx.clone(), r.clone(), diagnostics.clone());
    let mut last_bars = HashMap::<(String, String), String>::new();
    let mut history = HistoryLoader::spawn(ctx.clone(), r.clone());
    let mut history_errors = HashMap::<HistoryKey, String>::new();
    let mut last_timer = None::<Instant>;
    let mut total_quotes = 0u64;
    let mut queue_high_water = 0usize;
    let mut slow_cycles = SlowAccountCycles::default();
    let mut last_slow_report = Instant::now();
    loop {
        let (quotes, loaded) =
            match next_account_event(&mut timer, &mut rx, &mut history.updates).await {
                AccountEvent::Quotes(batch) => (Some(batch), None),
                AccountEvent::History(update) => (None, Some(update)),
                AccountEvent::Tick => (None, None),
                AccountEvent::Closed => break,
            };
        let waiting_batches_at_start = rx.len();
        queue_high_water = queue_high_water.max(waiting_batches_at_start);
        let quotes = quotes.map(|events| crate::runtime::drain_quote_batches(&mut rx, events));
        let batch_quotes = quotes.as_ref().map_or(0, Vec::len);
        total_quotes = total_quotes.saturating_add(batch_quotes as u64);
        let mut cycle = AccountCycleTimings {
            batch_quotes,
            waiting_batches_at_start,
            ..Default::default()
        };
        let dequeued_at = chrono::Utc::now().timestamp();
        for event in quotes.iter().flatten() {
            let age = dequeued_at.saturating_sub(event.quote.timestamp);
            cycle.oldest_quote_age_seconds = cycle.oldest_quote_age_seconds.max(age);
            cycle.oldest_feed_to_consumer_seconds = cycle
                .oldest_feed_to_consumer_seconds
                .max(dequeued_at.saturating_sub(event.received_at));
            cycle.quotes_past_age_limit += usize::from(age > EXECUTION_QUOTE_MAX_AGE_SECONDS);
        }
        let processing_started = Instant::now();
        let timer_due = last_timer.is_none_or(|at| at.elapsed() >= Duration::from_secs(1));
        let mut needs_drive = quotes.is_none() || timer_due;
        let manager = ctx.instances.get().unwrap();
        if ctx.is_shutting_down.load(Ordering::Relaxed)
            || manager.get(&r.definition.id).await.is_err()
        {
            break;
        }
        let (result, execution) = ExecutionTimings::measure(async {
            if r.quote_gap.swap(false, Ordering::Relaxed) {
                r.engine
                    .quote_gap_with_reason("本实例行情消费队列已满")
                    .await?;
            }
            if let Some(events) = quotes {
                if let BrokerClient::Paper(p) = &r.broker {
                    measure_stage(&mut cycle.paper_quotes, async {
                        p.on_received_quotes(&events)?;
                        let evs = r.engine.reconcile(&p.snapshot()).await?;
                        dispatch_live_trade_emails(&ctx, &r.definition.name, evs);
                        Ok::<_, anyhow::Error>(())
                    }).await?;
                }
                measure_stage(
                    &mut cycle.engine_quotes,
                    r.engine.on_received_quote_batch(events),
                ).await?;
            }
            if timer_due {
                last_timer = Some(Instant::now());
                if let Err(e) = measure_stage(
                    &mut cycle.timer,
                    r.engine.dispatch(StrategyEvent::Timer {
                        timestamp: chrono::Utc::now().timestamp(),
                    }),
                ).await
                {
                    r.engine.pause(true).await?;
                    tracing::error!("实例策略定时处理失败，继续执行平仓: {e}");
                }
            }
            if let BrokerClient::Paper(p) = &r.broker {
                measure_stage(&mut cycle.paper_sync, async {
                    let s = p.snapshot();
                    let evs = r.engine.reconcile(&s).await?;
                    dispatch_live_trade_emails(&ctx, &r.definition.name, evs);
                    r.engine.dispatch(StrategyEvent::ExecutionUpdate(s.clone())).await?;
                    *r.snapshot.write().await = s;
                    Ok::<_, anyhow::Error>(())
                }).await?;
            }
            // Apply at most one loaded symbol between quote batches. Slow I/O
            // remains in the background; engine mutations stay serialized here.
            if let Some(message) = loaded.or_else(|| history.updates.try_recv().ok()) {
                needs_drive = true;
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
                        history_errors.insert(message.key.clone(), error.to_string());
                        false
                    }
                };
                let _ = message.applied.send(applied);
                cycle.history = applied_at.elapsed();
                if cycle.history >= Duration::from_millis(250) {
                    tracing::warn!(instance_id = %r.definition.id,
                        symbol = %message.key.0, timeframe = %message.key.1,
                        apply_ms = cycle.history.as_millis(), quote_queue_batches = rx.len(),
                        "实例历史应用较慢，请检查策略计算与持久化耗时");
                }
            }
            if needs_drive {
                measure_stage(&mut cycle.final_drive, r.engine.drive()).await?;
            }
            *r.runtime_error.write().await = None;
            Ok::<_, anyhow::Error>(())
        }).await;
        cycle.execution = execution;
        cycle.failed = result.is_err();
        if let Err(e) = result {
            *r.runtime_error.write().await = Some(e.to_string());
            tracing::warn!("实例 {} 推进失败: {e:#}", r.definition.id);
        }
        cycle.elapsed = processing_started.elapsed();
        cycle.finished_at_ms = chrono::Utc::now().timestamp_millis();
        cycle.waiting_batches_at_end = rx.len();
        *diagnostics.write().await = json!({"quote_queue_batches":rx.len(),
            "quote_queue_high_water":queue_high_water,"processed_quotes":total_quotes,
            "last_batch_quotes":batch_quotes,"oldest_quote_age_seconds":cycle.oldest_quote_age_seconds,
            "last_processing_ms":cycle.elapsed.as_millis() as u64,
            "history_errors":history_errors.iter().map(|(key,error)|
                json!({"symbol":key.0,"timeframe":key.1,"error":error})).collect::<Vec<_>>()});
        slow_cycles.observe(cycle);
        if last_slow_report.elapsed() >= Duration::from_secs(5) {
            slow_cycles.report(&r.definition.id);
            last_slow_report = Instant::now();
        }
    }
    slow_cycles.report(&r.definition.id);
}

pub fn dispatch_live_trade_emails(
    ctx: &Arc<ServerContext>,
    instance_name: &str,
    events: n_core::trader::ReconcileEvents,
) {
    if events.fills.is_empty() && events.closes.is_empty() {
        return;
    }
    let ctx = ctx.clone();
    let inst_name = instance_name.to_string();
    tokio::spawn(async move {
        let cfg = ctx.services.config().await;
        if !cfg.email.enabled || !cfg.email.sendable() {
            return;
        }

        // 1. 处理开仓成交提醒
        if cfg.email.trade_open_enabled {
            for mut fill in events.fills {
                if !cfg
                    .email
                    .trade_instances
                    .iter()
                    .any(|id| id == &fill.instance_id)
                {
                    continue;
                }
                // 检查成交时间是否在 10 分钟以内，避免冷启动回放历史成交轰炸
                if let Ok(dt) =
                    chrono::NaiveDateTime::parse_from_str(&fill.trade_time, "%Y-%m-%d %H:%M:%S")
                {
                    let age = (chrono::Local::now().naive_local() - dt).num_seconds();
                    if age > 600 {
                        tracing::debug!(
                            trade_time = %fill.trade_time,
                            "跳过过期的开仓成交邮件提醒 (age: {}s)",
                            age
                        );
                        continue;
                    }
                }
                let resolved_name = if let Some(manager) = ctx.instances.get() {
                    if let Ok(r) = manager.get(&fill.instance_id).await {
                        let n = r.definition.name.trim();
                        if !n.is_empty() {
                            n.to_string()
                        } else if !inst_name.is_empty() && inst_name != "default" {
                            inst_name.clone()
                        } else {
                            "默认实盘".to_string()
                        }
                    } else if !inst_name.is_empty() && inst_name != "default" {
                        inst_name.clone()
                    } else {
                        "默认实盘".to_string()
                    }
                } else if !inst_name.is_empty() && inst_name != "default" {
                    inst_name.clone()
                } else {
                    "默认实盘".to_string()
                };
                fill.instance_name = resolved_name;
                let (subject, body) = n_core::notify::email::trade_open_email_payload(&fill);
                tracing::info!(
                    instance_id = %fill.instance_id,
                    symbol = %fill.symbol,
                    direction = %fill.direction,
                    fill_price = fill.fill_price,
                    "准备发送实盘开仓提醒邮件"
                );
                let email_cfg = cfg.email.clone();
                tokio::task::spawn_blocking(move || {
                    let _ = n_core::notify::email::send_summary(&subject, &body, &email_cfg);
                });
            }
        }

        // 2. 处理平仓成交提醒
        if cfg.email.trade_close_enabled {
            for mut close in events.closes {
                if !cfg
                    .email
                    .trade_instances
                    .iter()
                    .any(|id| id == &close.instance_id)
                {
                    continue;
                }
                if let Ok(dt) =
                    chrono::NaiveDateTime::parse_from_str(&close.exit_ts, "%Y-%m-%d %H:%M:%S")
                {
                    let age = (chrono::Local::now().naive_local() - dt).num_seconds();
                    if age > 600 {
                        tracing::debug!(
                            exit_ts = %close.exit_ts,
                            "跳过过期的平仓成交邮件提醒 (age: {}s)",
                            age
                        );
                        continue;
                    }
                }
                let resolved_name = if let Some(manager) = ctx.instances.get() {
                    if let Ok(r) = manager.get(&close.instance_id).await {
                        let n = r.definition.name.trim();
                        if !n.is_empty() {
                            n.to_string()
                        } else if !inst_name.is_empty() && inst_name != "default" {
                            inst_name.clone()
                        } else {
                            "默认实盘".to_string()
                        }
                    } else if !inst_name.is_empty() && inst_name != "default" {
                        inst_name.clone()
                    } else {
                        "默认实盘".to_string()
                    }
                } else if !inst_name.is_empty() && inst_name != "default" {
                    inst_name.clone()
                } else {
                    "默认实盘".to_string()
                };
                close.instance_name = resolved_name;
                let (subject, body) = n_core::notify::email::trade_close_email_payload(&close);
                tracing::info!(
                    instance_id = %close.instance_id,
                    symbol = %close.symbol,
                    exit_reason = %close.exit_reason,
                    realized_pnl_pts = close.realized_pnl_pts,
                    realized_pnl_amount = close.realized_pnl_amount,
                    "准备发送实盘平仓提醒邮件"
                );
                let email_cfg = cfg.email.clone();
                tokio::task::spawn_blocking(move || {
                    let _ = n_core::notify::email::send_summary(&subject, &body, &email_cfg);
                });
            }
        }
    });
}

#[cfg(test)]
#[path = "instance_market_tests.rs"]
mod market_load_tests;

#[cfg(test)]
mod history_revision_tests {
    use super::*;
    use n_core::sea_orm::ConnectionTrait;

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
                version: (
                    "retired-strategy-revision".into(),
                    0,
                    HistoryRequirements::default()
                ),
                data: MarketHistory {
                    bars: Arc::default(),
                    raw: None,
                    rollovers: None,
                },
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
    async fn accounts_share_history_across_strategy_revisions_until_market_data_changes() {
        let db = n_core::storage::connect(std::path::Path::new(":memory:"))
            .await
            .unwrap();
        let mut cfg = n_core::config::Config::default();
        cfg.data_source.primary_source = "offline".into();
        cfg.data_source.auto_spawn_bridge = false;
        let services =
            n_core::service::Services::new(db.clone(), cfg, "offline-config.json".into())
                .await
                .unwrap();
        db.execute_unprepared(
            "INSERT INTO klines VALUES
                ('PB0','5m','2026-10-27 10:00:00',100,101,99,100,9,5,'raw');",
        )
        .await
        .unwrap();
        let cache = HistoryCache::default();
        let key = ("PB0".to_owned(), "15m".to_owned());
        let bars_only = cache
            .load(
                &db,
                &services,
                &key,
                "a",
                HistoryRequirements::default(),
                None,
            )
            .await
            .unwrap()
            .unwrap();
        assert!(bars_only.data.raw.is_none());
        let needs = HistoryRequirements {
            raw: true,
            rollovers: true,
        };
        let (a, b) = tokio::join!(
            cache.load(&db, &services, &key, "a", needs, Some(&bars_only.version)),
            cache.load(&db, &services, &key, "b", needs, None),
        );
        let a = a.unwrap().unwrap();
        let b = b.unwrap().unwrap();
        assert!(Arc::ptr_eq(&a.data.bars, &bars_only.data.bars));
        assert!(Arc::ptr_eq(&a.data.bars, &b.data.bars));
        assert!(Arc::ptr_eq(
            a.data.raw.as_ref().unwrap(),
            b.data.raw.as_ref().unwrap()
        ));
        assert!(a.data.rollovers.as_ref().unwrap().is_empty());
        assert_eq!(b.data.raw.as_ref().unwrap()[0].close, 100.);
        db.execute_unprepared(
            "UPDATE klines SET close=close WHERE symbol='PB0';
             INSERT INTO klines VALUES
                ('CU0','5m','2026-10-27 10:00:00',100,101,99,100,9,5,'raw');",
        )
        .await
        .unwrap();
        assert!(cache
            .load(&db, &services, &key, "b", needs, Some(&b.version))
            .await
            .unwrap()
            .is_none());

        // An in-place price correction must invalidate even with the same count and timestamp.
        db.execute_unprepared("UPDATE klines SET close=101 WHERE symbol='PB0'")
            .await
            .unwrap();
        let corrected = cache
            .load(&db, &services, &key, "b", needs, Some(&b.version))
            .await
            .unwrap()
            .unwrap();
        assert_ne!(corrected.version.1, b.version.1);
        assert_eq!(corrected.data.raw.as_ref().unwrap()[0].close, 101.);
        assert_eq!(b.data.raw.as_ref().unwrap()[0].close, 100.);

        db.execute_unprepared(
            "INSERT INTO klines VALUES
                ('PB0','5m','2026-10-27 09:55:00',100,101,99,100,9,5,'raw');
             INSERT INTO rollovers
                (symbol, ts, from_contract, to_contract, confirmed, created_at, updated_at)
             VALUES ('PB0','2026-10-27 09:55:00','pb2611','pb2612',1,'','');",
        )
        .await
        .unwrap();
        let backfill = cache
            .load(&db, &services, &key, "b", needs, Some(&corrected.version))
            .await
            .unwrap()
            .unwrap();
        assert_eq!(backfill.data.raw.unwrap().len(), 2);
        assert_eq!(backfill.data.rollovers.unwrap().len(), 1);
    }
}
