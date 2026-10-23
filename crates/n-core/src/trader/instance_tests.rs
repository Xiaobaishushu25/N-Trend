use super::*;
use crate::trader::{broker::*, instances::*};
use serde_json::{json, Value};
use std::collections::BTreeMap;

fn directory() -> PathBuf {
    let p = std::env::temp_dir().join(format!("ntrend-instances-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir_all(&p).unwrap();
    p
}

#[tokio::test]
async fn client_ctp_credentials_keep_the_bound_identity_after_restart() {
    let root = directory();
    let db = crate::storage::connect(&root.join("test.db"))
        .await
        .unwrap();
    let dir = migrate_default(&root).unwrap();
    let e = Arc::new(ExecutionEngine::with_config_path(
        CtpTraderClient::for_port(1),
        config(),
        Some(dir.join("trader_config.json")),
    ));
    let manager = InstanceManager::open(db.clone(), root.clone(), e.clone(), registry())
        .await
        .unwrap();
    manager
        .set_ctp_credentials("default", "123456", "client-default")
        .await
        .unwrap();
    assert!(manager
        .set_ctp_credentials("default", "654321", "client-other")
        .await
        .is_err());
    assert_eq!(
        manager.list().await["instances"][0]["account"]["user_id"],
        "123456"
    );
    let expected = serde_json::to_value(manager.account("default").await.unwrap()).unwrap();
    assert!(expected.get("Password").is_none());
    let paused = manager.get("default").await.unwrap().engine.paused().await;
    drop(manager);
    drop(e);
    let e = Arc::new(ExecutionEngine::with_config_path(
        CtpTraderClient::for_port(1),
        config(),
        Some(dir.join("trader_config.json")),
    ));
    let manager = InstanceManager::open(db, root, e, registry())
        .await
        .unwrap();
    assert_eq!(
        serde_json::to_value(manager.account("default").await.unwrap()).unwrap(),
        expected
    );
    assert_eq!(
        manager.get("default").await.unwrap().engine.paused().await,
        paused
    );
    assert!(manager
        .set_ctp_credentials("default", "654321", "client-other")
        .await
        .is_err());
}
fn paper_config(balance: f64) -> PaperConfig {
    PaperConfig {
        initial_balance: balance,
        contracts: BTreeMap::from([(
            "PB2611".into(),
            PaperContract {
                multiplier: 5.0,
                margin_rate: 0.1,
                open_fee_per_lot: 2.0,
                close_fee_per_lot: 3.0,
                close_today_fee_per_lot: 4.0,
            },
        )]),
    }
}
fn quote_at(price: f64) -> ExecutionQuote {
    ExecutionQuote {
        instrument_id: "pb2611".into(),
        exchange_id: "SHFE".into(),
        underlying_symbol: "PB0".into(),
        last: price,
        bid: price - 1.0,
        ask: price + 1.0,
        tick: 1.0,
        lower: 500.0,
        upper: 2000.0,
        timestamp: chrono::Utc::now().timestamp(),
    }
}
fn plan(id: &str) -> EntryPlan {
    EntryPlan {
        signal_id: id.into(),
        symbol: "PB0".into(),
        instrument_id: "pb2611".into(),
        direction: Direction::Long,
        reference_price: 1000.0,
        trigger: EntryTrigger::Immediate,
        stop_price: Some(990.0),
        target_price: None,
        risk_per_unit: 10.0,
        requested_volume: 1,
        created_at: chrono::Utc::now().timestamp(),
        expires_at: None,
        reason: "test".into(),
        evidence: Value::Null,
    }
}
fn config() -> ExecutionConfig {
    ExecutionConfig {
        mode: ExecutionMode::FullAuto,
        min_entry_risk_ticks: 0,
        ..Default::default()
    }
}
fn request(id: &str, offset: &str, volume: i32) -> OrderInsertRequest {
    OrderInsertRequest {
        instrument_id: "pb2611".into(),
        direction: if offset == "open" { "buy" } else { "sell" }.into(),
        offset: offset.into(),
        price: if offset == "open" { 1100.0 } else { 900.0 },
        volume,
        exchange_id: Some("SHFE".into()),
        order_ref: Some(id.into()),
    }
}

struct Example;
impl Strategy for Example {
    fn on_event(
        &self,
        event: &StrategyEvent,
        c: &StrategyContext<'_>,
        state: &mut Value,
    ) -> Result<Vec<TradeIntent>> {
        if !state.is_object() {
            *state = json!({});
        }
        match event {
            StrategyEvent::Timer { .. } if c.allow_entries && state["entered"] != true => {
                state["entered"] = json!(true);
                Ok(vec![TradeIntent::Open(plan("example-signal"))])
            }
            StrategyEvent::Quote(q) if q.last >= 1010.0 && state["closed"] != true => {
                if let Some(p) = c.positions.iter().find(|p| !p.is_closed) {
                    state["closed"] = json!(true);
                    Ok(vec![TradeIntent::Close {
                        position_id: p.id.clone(),
                        direction: Direction::Long,
                        volume: None,
                        reason: "example_exit".into(),
                    }])
                } else {
                    Ok(vec![])
                }
            }
            _ => Ok(vec![]),
        }
    }
}
fn registry() -> Arc<StrategyRegistry> {
    let mut r = StrategyRegistry::default();
    r.register_strategy("example", "1", |_| Ok(Box::new(Example)));
    Arc::new(r)
}
fn definition() -> StrategyDefinition {
    StrategyDefinition {
        strategy_id: "example".into(),
        exit_policy: "protect_only_v1".into(),
        sizing_policy: "fixed_v1".into(),
        ..Default::default()
    }
}
async fn engine(root: &Path, id: &str, broker: Arc<PaperBroker>) -> ExecutionEngine {
    let mut e = ExecutionEngine::with_config_path(
        BrokerClient::Paper(broker),
        config(),
        Some(root.join("trader_config.json")),
    );
    e.registry = registry();
    e.bind(id, id).await.unwrap();
    e.set_definition(definition()).await.unwrap();
    e
}
async fn tick(e: &ExecutionEngine, p: &PaperBroker, price: f64) {
    let quotes = vec![("PB2611".into(), quote_at(price))];
    p.on_quotes(&quotes).unwrap();
    e.reconcile(&p.snapshot()).await.unwrap();
    e.state.lock().await.last_action_at = 0;
    e.on_quote_batch(quotes).await.unwrap();
    e.reconcile(&p.snapshot()).await.unwrap();
}

#[tokio::test]
async fn non_n_strategy_opens_closes_and_restores_without_score_or_s1() {
    let root = directory();
    let p = Arc::new(
        PaperBroker::open(
            "test".into(),
            paper_config(50000.0),
            root.join("paper.json"),
        )
        .unwrap(),
    );
    let e = engine(&root, "test", p.clone()).await;
    e.dispatch(StrategyEvent::Timer {
        timestamp: chrono::Utc::now().timestamp(),
    })
    .await
    .unwrap();
    tick(&e, &p, 1000.0).await;
    assert_eq!(e.list_positions().await[0].volume, 1);
    assert!(e.list_conditions().await[0].event_id.is_none());
    let version = e.definition().await.revision;
    drop(e);
    let mut e = ExecutionEngine::with_config_path(
        BrokerClient::Paper(p.clone()),
        config(),
        Some(root.join("trader_config.json")),
    );
    e.registry = registry();
    e.bind("test", "test").await.unwrap();
    assert_eq!(e.definition().await.revision, version);
    e.dispatch(StrategyEvent::Timer {
        timestamp: chrono::Utc::now().timestamp(),
    })
    .await
    .unwrap();
    assert_eq!(e.list_conditions().await.len(), 1);
    tick(&e, &p, 1015.0).await;
    assert!(e.list_positions().await[0].is_closed);
    assert_eq!(e.get_trade_history().await[0].exit_reason, "example_exit");
    assert_eq!(p.snapshot()["account"]["commission"], 6.0);
    assert_eq!(p.snapshot()["account"]["curr_margin"], 0.0);
}

#[test]
fn paper_funds_fees_idempotency_and_restart_are_account_scoped() {
    let root = directory();
    let path = root.join("paper.json");
    let p = PaperBroker::open("a".into(), paper_config(600.0), path.clone()).unwrap();
    p.on_quotes(&[("PB2611".into(), quote_at(1000.0))]).unwrap();
    assert!(p.insert(&request("one", "open", 1)).unwrap().success);
    assert!(p.insert(&request("one", "open", 1)).unwrap().success);
    assert_eq!(p.snapshot()["trades"].as_array().unwrap().len(), 1);
    assert!(!p.insert(&request("two", "open", 1)).unwrap().success);
    assert!(p.insert(&request("close", "auto", 1)).unwrap().success);
    let s = p.snapshot();
    assert_eq!(s["account"]["balance"], 584.0);
    assert_eq!(s["account"]["commission"], 6.0);
    assert_eq!(s["account"]["curr_margin"], 0.0);
    drop(p);
    let p = PaperBroker::open("a".into(), paper_config(600.0), path).unwrap();
    assert_eq!(p.snapshot()["trades"], s["trades"]);
    assert!(p.insert(&request("one", "open", 2)).is_err());
}

#[test]
fn paper_rejects_missing_cost_parameters_and_stale_quotes_cannot_fill() {
    let root = directory();
    let mut c = paper_config(10000.0);
    c.contracts.clear();
    let p = PaperBroker::open("a".into(), c, root.join("empty.json")).unwrap();
    assert!(!p.insert(&request("a", "open", 1)).unwrap().success);
    assert!(p.snapshot()["trades"].as_array().unwrap().is_empty());
    let p = PaperBroker::open("b".into(), paper_config(10000.0), root.join("stale.json")).unwrap();
    let mut q = quote_at(1000.0);
    q.timestamp -= 30;
    p.on_quotes(&[("PB2611".into(), q)]).unwrap();
    p.insert(&request("b", "open", 1)).unwrap();
    assert!(p.snapshot()["trades"].as_array().unwrap().is_empty());
    p.on_quotes(&[("PB2611".into(), quote_at(1000.0))]).unwrap();
    assert_eq!(p.snapshot()["trades"].as_array().unwrap().len(), 1);
}

#[tokio::test]
async fn shared_signal_is_deduplicated_per_instance_and_paused_exits_continue() {
    let a = directory();
    let b = directory();
    let pa = Arc::new(
        PaperBroker::open("a".into(), paper_config(50000.0), a.join("paper.json")).unwrap(),
    );
    let pb = Arc::new(
        PaperBroker::open("b".into(), paper_config(50000.0), b.join("paper.json")).unwrap(),
    );
    let ea = engine(&a, "a", pa.clone()).await;
    let eb = engine(&b, "b", pb.clone()).await;
    for e in [&ea, &eb] {
        let d = e.definition().await;
        let one = e
            .register_plan(plan("shared"), d.clone(), config())
            .await
            .unwrap();
        assert_eq!(
            e.register_plan(plan("shared"), d, config()).await.unwrap(),
            one
        );
    }
    tick(&ea, &pa, 1000.0).await;
    tick(&eb, &pb, 1000.0).await;
    assert_eq!(ea.list_positions().await[0].origin.instance_id, "a");
    assert_eq!(eb.list_positions().await[0].origin.instance_id, "b");
    ea.pause(true).await.unwrap();
    tick(&ea, &pa, 985.0).await;
    assert!(ea.list_positions().await[0].is_closed);
    assert!(!eb.list_positions().await[0].is_closed);
}

#[tokio::test]
async fn parameter_changes_freeze_pending_orders_and_survive_restart() {
    let root = directory();
    let p = Arc::new(
        PaperBroker::open("a".into(), paper_config(50000.0), root.join("paper.json")).unwrap(),
    );
    let e = engine(&root, "a", p.clone()).await;
    let d = e.definition().await;
    e.register_plan(plan("old"), d.clone(), config())
        .await
        .unwrap();
    let mut next = config();
    next.slippage_ticks = 7;
    e.update_config(next.clone()).await.unwrap();
    let new = e.definition().await;
    e.register_plan(plan("new"), new.clone(), next)
        .await
        .unwrap();
    let conditions = e.list_conditions().await;
    let old = conditions
        .iter()
        .find(|c| c.origin.signal_id == "old")
        .unwrap();
    assert_eq!(old.frozen.as_ref().unwrap().execution.slippage_ticks, 2);
    assert_eq!(old.origin.strategy_version, d.revision);
    assert_ne!(d.revision, new.revision);
    drop(e);
    let mut e = ExecutionEngine::with_config_path(
        BrokerClient::Paper(p),
        config(),
        Some(root.join("trader_config.json")),
    );
    e.registry = registry();
    e.bind("a", "a").await.unwrap();
    assert_eq!(e.get_config().await.slippage_ticks, 7);
    assert_eq!(e.list_conditions().await.len(), 2);
}

#[tokio::test]
async fn legacy_live_position_keeps_its_strategy_and_protection_after_instance_migration() {
    let root = directory();
    let p = Arc::new(
        PaperBroker::open(
            "default".into(),
            paper_config(50000.),
            root.join("paper.json"),
        )
        .unwrap(),
    );
    let e = ExecutionEngine::with_config_path(
        BrokerClient::Paper(p.clone()),
        config(),
        Some(root.join("trader_config.json")),
    );
    let id = e
        .register_condition(
            Some(7),
            "PB0",
            "pb2611",
            Dir::Up,
            1000.,
            980.,
            1100.,
            4.,
            "2026-10-23 09:15:00",
        )
        .await
        .unwrap();
    e.state
        .lock()
        .await
        .entry_symbols_ready
        .insert("PB0".into());
    tick(&e, &p, 1000.).await;
    tick(&e, &p, 1026.).await;
    let before = {
        let mut s = e.state.lock().await;
        let pos = s.positions[&id].clone();
        assert_eq!(pos.volume, 1);
        assert!(pos.lot1_closed);
        assert_eq!(pos.current_stop_price, pos.fill_price);
        // Old production files did not contain strategy origins or FrozenPlan.
        s.conditions.get_mut(&id).unwrap().frozen = None;
        s.conditions.get_mut(&id).unwrap().origin = TradeOrigin::default();
        s.positions.get_mut(&id).unwrap().origin = TradeOrigin::default();
        e.save(&mut s).unwrap();
        pos
    };
    let old_history = e.get_trade_history().await.len();
    drop(e);
    let dest = migrate_default(&root).unwrap();
    let restored = ExecutionEngine::with_config_path(
        BrokerClient::Paper(p.clone()),
        config(),
        Some(dest.join("trader_config.json")),
    );
    restored.bind("default", "default").await.unwrap();
    {
        let s = restored.state.lock().await;
        let pos = &s.positions[&id];
        assert_eq!(pos.origin.strategy_id, N_STRATEGY);
        assert_eq!(pos.volume, before.volume);
        assert_eq!(pos.fill_price, before.fill_price);
        assert_eq!(pos.current_stop_price, before.current_stop_price);
        assert_eq!(pos.target_price, before.target_price);
        assert_eq!(pos.risk, before.risk);
        assert_eq!(pos.lot1_closed, before.lot1_closed);
        assert_eq!(
            s.conditions[&id]
                .frozen
                .as_ref()
                .unwrap()
                .definition
                .exit_policy,
            "n_exit_v1"
        );
    }
    tick(&restored, &p, 1000.).await;
    assert!(restored.state.lock().await.positions[&id].is_closed);
    assert_eq!(restored.get_trade_history().await.len(), old_history + 1);
}

#[test]
fn migration_is_repeatable_preserves_originals_and_rejects_corrupt_state() {
    let root = directory();
    let old = b"{\"conditions\":{},\"last_ref\":12345}";
    std::fs::write(root.join("trader_execution.json"), old).unwrap();
    let dest = migrate_default(&root).unwrap();
    assert_eq!(
        std::fs::read(dest.join("trader_execution.json")).unwrap(),
        old
    );
    std::fs::write(dest.join("trader_execution.json"), b"{\"last_ref\":99999}").unwrap();
    migrate_default(&root).unwrap();
    assert_eq!(
        std::fs::read(root.join("trader_execution.json")).unwrap(),
        old
    );
    assert_eq!(
        std::fs::read(root.join("legacy-trader-backup/trader_execution.json")).unwrap(),
        old
    );
    assert!(
        String::from_utf8(std::fs::read(dest.join("trader_execution.json")).unwrap())
            .unwrap()
            .contains("99999")
    );
    let root = directory();
    std::fs::write(root.join("trader_execution.json"), b"broken").unwrap();
    assert!(migrate_default(&root).is_err());
    assert!(!root
        .join("trader_instances/default/migration_complete.json")
        .exists());
}

#[tokio::test]
async fn registry_persists_accounts_blocks_duplicate_bindings_and_checks_broker_identity() {
    let root = directory();
    let db = crate::storage::connect(&root.join("test.db"))
        .await
        .unwrap();
    let dir = migrate_default(&root).unwrap();
    let e = Arc::new(ExecutionEngine::with_config_path(
        CtpTraderClient::for_port(1),
        config(),
        Some(dir.join("trader_config.json")),
    ));
    let manager = InstanceManager::open(db.clone(), root.clone(), e.clone(), registry())
        .await
        .unwrap();
    manager
        .validate_snapshot(
            "default",
            &json!({"health":{"logged_in":true,"broker_id":"b","user_id":"u"}}),
        )
        .await
        .unwrap();
    assert!(manager
        .validate_snapshot(
            "default",
            &json!({"health":{"logged_in":true,"broker_id":"b","user_id":"wrong"}})
        )
        .await
        .is_err());
    let account = AccountDefinition {
        id: "paper-a".into(),
        name: "paper".into(),
        kind: AccountKind::Paper {
            config: paper_config(10000.0),
        },
    };
    let id = manager
        .create("test".into(), account.clone(), definition(), config())
        .await
        .unwrap();
    assert!(manager.get(&id).await.unwrap().engine.paused().await);
    assert!(manager
        .create("duplicate".into(), account, definition(), config())
        .await
        .is_err());
    let traversal = AccountDefinition {
        id: "../outside".into(),
        name: "x".into(),
        kind: AccountKind::Paper {
            config: paper_config(10000.0),
        },
    };
    assert!(manager
        .create("x".into(), traversal, definition(), config())
        .await
        .is_err());
    let row = manager.get(&id).await.unwrap();
    row.engine.pause(false).await.unwrap();
    row.engine
        .register_plan(plan("pending"), row.engine.definition().await, config())
        .await
        .unwrap();
    row.engine.pause(true).await.unwrap();
    assert!(manager.remove(&id).await.is_err());
    drop(row);
    drop(manager);
    drop(e);
    let e = Arc::new(ExecutionEngine::with_config_path(
        CtpTraderClient::for_port(1),
        config(),
        Some(dir.join("trader_config.json")),
    ));
    let manager = InstanceManager::open(db, root, e, registry())
        .await
        .unwrap();
    assert_eq!(manager.all().await.len(), 2);
    assert_eq!(
        manager
            .get(&id)
            .await
            .unwrap()
            .engine
            .list_conditions()
            .await
            .len(),
        1
    );
}

#[tokio::test]
async fn unavailable_old_code_preserves_protection_and_blocks_new_exposure() {
    let root = directory();
    let p = Arc::new(
        PaperBroker::open("a".into(), paper_config(50000.0), root.join("paper.json")).unwrap(),
    );
    let e = engine(&root, "a", p.clone()).await;
    e.register_plan(plan("old-code"), e.definition().await, config())
        .await
        .unwrap();
    tick(&e, &p, 1000.0).await;
    drop(e);
    let e = ExecutionEngine::with_config_path(
        BrokerClient::Paper(p.clone()),
        config(),
        Some(root.join("trader_config.json")),
    );
    e.bind("a", "a").await.unwrap();
    tick(&e, &p, 985.0).await;
    assert!(e.paused().await);
    assert!(e.execution_error().await.is_some());
    assert!(e.list_positions().await[0].is_closed);
}

#[tokio::test]
async fn equity_comparison_uses_the_requested_interval_and_marks_missing_samples() {
    use crate::sea_orm::{ConnectionTrait, DbBackend, Statement};
    let root = directory();
    let db = crate::storage::connect(&root.join("test.db"))
        .await
        .unwrap();
    let dir = migrate_default(&root).unwrap();
    let e = Arc::new(ExecutionEngine::with_config_path(
        CtpTraderClient::for_port(1),
        config(),
        Some(dir.join("trader_config.json")),
    ));
    let m = InstanceManager::open(db.clone(), root, e, registry())
        .await
        .unwrap();
    for (ts, equity, fees) in [
        (60i64, 1000.0, 0.0),
        (120, 1100.0, 2.0),
        (300, 990.0, 6.0),
        (600, 9000.0, 8.0),
    ] {
        db.execute(Statement::from_sql_and_values(DbBackend::Sqlite,"INSERT INTO trader_equity(instance_id,timestamp,equity,margin,fees,quality,trading_day) VALUES('default',?,?,?,?,?,?)",[ts.into(),equity.into(),500.0.into(),fees.into(),"ok".into(),"20261023".into()])).await.unwrap();
    }
    let report = m.comparison(60, 300).await.unwrap();
    let row = &report["instances"][0];
    assert_eq!(row["net_profit"], -10.0);
    assert_eq!(row["max_drawdown_pct"], 10.0);
    assert_eq!(row["observed_fees"], 6.0);
    assert_eq!(row["sample_count"], 3);
    assert_eq!(row["quality_flags"][0], "equity_sample_gap");
    let empty = m.comparison(700, 800).await.unwrap();
    assert!(empty["instances"][0]["net_profit"].is_null());
}

#[tokio::test]
async fn damaged_paper_account_is_disabled_without_blocking_other_accounts_or_overwriting_it() {
    let root = directory();
    let db = crate::storage::connect(&root.join("test.db"))
        .await
        .unwrap();
    let dir = migrate_default(&root).unwrap();
    let e = Arc::new(ExecutionEngine::with_config_path(
        CtpTraderClient::for_port(1),
        config(),
        Some(dir.join("trader_config.json")),
    ));
    let m = InstanceManager::open(db.clone(), root.clone(), e.clone(), registry())
        .await
        .unwrap();
    let account = AccountDefinition {
        id: "bad-paper".into(),
        name: "bad".into(),
        kind: AccountKind::Paper {
            config: paper_config(10000.0),
        },
    };
    let id = m
        .create("bad".into(), account, definition(), config())
        .await
        .unwrap();
    drop(m);
    drop(e);
    let path = root
        .join("trader_instances")
        .join(&id)
        .join("paper_account.json");
    std::fs::write(&path, b"damaged").unwrap();
    let e = Arc::new(ExecutionEngine::with_config_path(
        CtpTraderClient::for_port(1),
        config(),
        Some(dir.join("trader_config.json")),
    ));
    let m = InstanceManager::open(db, root, e, registry())
        .await
        .unwrap();
    let bad = m.get(&id).await.unwrap();
    assert!(matches!(bad.broker, BrokerClient::Disabled(_)));
    assert!(bad.engine.paused().await);
    assert!(bad.error.read().await.is_some());
    assert!(matches!(
        m.get("default").await.unwrap().broker,
        BrokerClient::Ctp(_)
    ));
    assert_eq!(std::fs::read(path).unwrap(), b"damaged");
}

#[tokio::test]
async fn instance_rename_updates_catalog_and_persists() {
    let root = directory();
    let db = crate::storage::connect(&root.join("test.db"))
        .await
        .unwrap();
    let dir = migrate_default(&root).unwrap();
    let e = Arc::new(ExecutionEngine::with_config_path(
        CtpTraderClient::for_port(1),
        config(),
        Some(dir.join("trader_config.json")),
    ));
    let m = InstanceManager::open(db.clone(), root.clone(), e.clone(), registry())
        .await
        .unwrap();
    assert_eq!(m.list().await["instances"][0]["instance"]["name"], "现有 N 字形策略");
    m.rename("default", "螺纹实盘1号".into()).await.unwrap();
    assert_eq!(m.list().await["instances"][0]["instance"]["name"], "螺纹实盘1号");
    drop(m);
    drop(e);
    let e = Arc::new(ExecutionEngine::with_config_path(
        CtpTraderClient::for_port(1),
        config(),
        Some(dir.join("trader_config.json")),
    ));
    let m = InstanceManager::open(db, root, e, registry())
        .await
        .unwrap();
    assert_eq!(m.list().await["instances"][0]["instance"]["name"], "螺纹实盘1号");
}
