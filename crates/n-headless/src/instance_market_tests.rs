//! Exercise real account loops while one UI publisher is stalled. No CTP,
//! outbound sockets, credentials or external market source.
use super::*;
use n_core::trader::{broker::PaperConfig, model::ExecutionQuote, strategy::*, ExecutionConfig};
struct QuoteCounter;
impl Strategy for QuoteCounter {
    fn handles_event(&self, e: &StrategyEvent) -> bool {
        matches!(e, StrategyEvent::Quote(_))
    }
    fn on_event(
        &self,
        _: &StrategyEvent,
        _: &StrategyContext<'_>,
        v: &mut serde_json::Value,
    ) -> anyhow::Result<Vec<TradeIntent>> {
        v["count"] = json!(v["count"].as_u64().unwrap_or(0) + 1);
        Ok(vec![])
    }
}

#[tokio::test]
async fn stalled_instance_publication_does_not_block_its_quotes_or_another_account() {
    let root =
        std::env::temp_dir().join(format!("ntrend-market-isolation-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir_all(&root).unwrap();
    let db = n_core::storage::connect(&root.join("test.db"))
        .await
        .unwrap();
    let mut cfg = n_core::config::Config::default();
    cfg.data_source.primary_source = "offline".into();
    cfg.data_source.auto_spawn_bridge = false;
    let services = Arc::new(
        n_core::service::Services::new(db.clone(), cfg, root.join("config.json"))
            .await
            .unwrap(),
    );
    let ctx = ServerContext::new(
        db.clone(),
        services,
        root.clone(),
        crate::secrets::ServerSecrets::default(),
    );
    let mut registry = StrategyRegistry::default();
    registry.register_strategy(N_STRATEGY, N_VERSION, |_| Ok(Box::new(QuoteCounter)));
    let manager = InstanceManager::open(
        db,
        root.clone(),
        ctx.execution_engine.clone(),
        Arc::new(registry),
    )
    .await
    .unwrap();
    ctx.instances.set(manager.clone()).ok().unwrap();
    let mut instances = vec![];
    for id in ["slow-publisher", "healthy-publisher"] {
        let id = manager
            .create(
                id.into(),
                AccountDefinition {
                    id: id.into(),
                    name: id.into(),
                    kind: AccountKind::Paper {
                        config: PaperConfig {
                            initial_balance: 50000.,
                            contracts: Default::default(),
                        },
                    },
                },
                StrategyDefinition::default(),
                ExecutionConfig::default(),
            )
            .await
            .unwrap();
        instances.push(manager.get(&id).await.unwrap());
    }
    // Force the actual publisher to wait indefinitely on its cache write.
    // Before isolation this lock stalled run_account after its first batch.
    let cache_gate = instances[0].published.write().await;
    let tasks = instances
        .iter()
        .map(|r| tokio::spawn(run_account(ctx.clone(), r.clone())))
        .collect::<Vec<_>>();
    let q = ExecutionQuote {
        instrument_id: "pb2701".into(),
        exchange_id: "SHFE".into(),
        underlying_symbol: "PB0".into(),
        last: 1000.,
        bid: 999.,
        ask: 1001.,
        tick: 1.,
        lower: 500.,
        upper: 2000.,
        timestamp: chrono::Utc::now().timestamp(),
    };
    // Several rounds force the consumer to return to its queue while the UI
    // remains blocked, instead of passing by draining one initial batch.
    for round in 0..8 {
        for r in &instances {
            r.quote_tx
                .send(vec![("PB0".into(), q.clone()); 16])
                .await
                .unwrap();
        }
        tokio::time::timeout(Duration::from_secs(5), async {
            loop {
                let mut all = true;
                for r in &instances {
                    let s = r.engine.strategy_diagnostics().await;
                    let revision = r.engine.definition().await.revision;
                    all &=
                        s["states"][&revision]["count"].as_u64().unwrap_or(0) >= (round + 1) * 16;
                }
                if all {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(2)).await;
            }
        })
        .await
        .expect("UI cache blocking must not stall either quote consumer");
    }
    for r in &instances {
        let revision = r.engine.definition().await.revision;
        assert_eq!(
            r.engine.strategy_diagnostics().await["states"][&revision]["count"],
            128
        );
        assert_eq!(r.quote_tx.capacity(), 64);
        assert!(!r.quote_gap.load(Ordering::Relaxed));
        assert!(r.engine.get_trade_history().await.is_empty());
    }
    assert!(cache_gate.is_null());
    drop(cache_gate);
    ctx.is_shutting_down.store(true, Ordering::Relaxed);
    for task in tasks {
        tokio::time::timeout(Duration::from_secs(3), task)
            .await
            .unwrap()
            .unwrap();
    }
    drop(instances);
    drop(manager);
    drop(ctx);
    tokio::task::yield_now().await;
    let resolved = root.canonicalize().unwrap();
    assert!(resolved.starts_with(std::env::temp_dir().canonicalize().unwrap()));
    // SQLite pools may close asynchronously on Windows; the unique test
    // directory can remain if the OS still has a database handle open.
    let _ = std::fs::remove_dir_all(resolved);
}
