//! Each account progresses in its own task; one unavailable broker never blocks another.
use crate::state::ServerContext;
use n_core::trader::{broker::BrokerClient, instances::*, strategy::StrategyEvent};
use serde_json::json;
use std::{
    collections::{HashMap, HashSet},
    sync::{atomic::Ordering, Arc},
    time::{Duration, Instant},
};

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

pub(crate) async fn forward_strategy_history(
    ctx: &ServerContext,
    engine: &n_core::trader::ExecutionEngine,
    symbol: &str,
    raw: &[n_core::fetch::kline::Kline],
) -> anyhow::Result<()> {
    if !engine.needs_market_history().await {
        return Ok(());
    }
    let rolls = n_core::storage::repo::symbol_rollovers(&ctx.db, symbol)
        .await?
        .into_iter()
        .map(|r| n_core::derive::rollover::RolloverRecord {
            symbol: r.symbol,
            ts: r.ts,
            from_contract: r.from_contract,
            to_contract: r.to_contract,
            confirmed: r.confirmed,
        })
        .collect();
    engine.on_market_history(symbol, raw, rolls).await
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
    let mut history_versions = HashMap::<(String, String), (String, HistoryRevision)>::new();
    let mut ticks = 0u64;
    let mut next_history_refresh = Instant::now();
    loop {
        let quotes = tokio::select! { _=timer.tick()=>None, batch=rx.recv()=>batch };
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
            // Bar reads use the shared cache, not another market connection.
            if Instant::now() >= next_history_refresh {
                next_history_refresh = Instant::now() + Duration::from_secs(5);
                let mut subscriptions = r
                    .engine
                    .active_symbols()
                    .await
                    .into_iter()
                    .map(|s| (s, "15m".to_string()))
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
                for (symbol, tf) in subscriptions {
                    let key = (symbol.clone(), tf.clone());
                    let version = (
                        strategy_revision.clone(),
                        market_history_revision(&ctx.db, &symbol, &tf).await?,
                    );
                    if history_versions.get(&key) == Some(&version) {
                        continue;
                    }
                    if tf == "15m" && r.engine.needs_completed_15m().await {
                        let rows = n_core::storage::repo::raw_klines(&ctx.db, &symbol).await?;
                        let raw = rows
                            .iter()
                            .map(n_core::service::model_to_fetch)
                            .collect::<Vec<_>>();
                        forward_strategy_history(&ctx, &r.engine, &symbol, &raw).await?;
                        r.engine.on_completed_15m(&symbol, &raw).await?;
                    }
                    let bars = ctx.services.bars_for(&symbol, &tf).await?;
                    let last = if let Some(last) = last_bars.get(&key) {
                        Some(last.clone())
                    } else if tf == "15m" {
                        r.engine.last_bar(&symbol).await
                    } else {
                        None
                    };
                    let pending = if let Some(last) = last {
                        bars.iter()
                            .filter(|b| b.dt.to_bar_ts() > last)
                            .collect::<Vec<_>>()
                    } else {
                        bars.last().into_iter().collect()
                    };
                    for b in pending {
                        if tf == "15m" {
                            r.engine
                                .on_price_update(
                                    &symbol,
                                    b.high,
                                    b.low,
                                    b.close,
                                    &b.dt.to_bar_ts(),
                                    true,
                                )
                                .await?;
                        } else {
                            r.engine
                                .dispatch(StrategyEvent::BarClosed {
                                    symbol: symbol.clone(),
                                    timeframe: tf.clone(),
                                    timestamp: b.dt.to_bar_ts(),
                                    high: b.high,
                                    low: b.low,
                                    close: b.close,
                                })
                                .await?;
                        }
                        last_bars.insert(key.clone(), b.dt.to_bar_ts());
                    }
                    if tf == "15m" {
                        r.engine
                            .reconcile_entry_age(
                                &symbol,
                                &bars.iter().map(|b| b.dt.to_bar_ts()).collect::<Vec<_>>(),
                            )
                            .await?;
                    }
                    history_versions.insert(key, version);
                }
            }
            *r.runtime_error.write().await = None;
            let mut snapshot = manager.build_snapshot(&r.definition.id).await?;
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
