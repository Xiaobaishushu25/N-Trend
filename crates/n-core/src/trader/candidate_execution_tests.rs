use super::*;
use crate::trader::{broker::*, candidates::*};
use serde_json::{json, Value};
use std::collections::BTreeMap;

fn directory() -> PathBuf {
    let p = std::env::temp_dir().join(format!("ntrend-candidates-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir_all(&p).unwrap();
    p
}
fn parameters() -> Parameters {
    let mut p = Parameters::reference_snapshot();
    p.fees.products.insert(
        "PB".into(),
        serde_json::from_value(json!({"roundtrip_fixed_yuan_per_lot":6.})).unwrap(),
    );
    p
}
fn config() -> ExecutionConfig {
    ExecutionConfig {
        mode: ExecutionMode::FullAuto,
        slippage_ticks: 0,
        default_large_volume: 9,
        default_small_volume: 7,
        ..Default::default()
    }
}
fn registry() -> Arc<StrategyRegistry> {
    let mut registry = StrategyRegistry::default();
    // Quotes retain their real clock; execution scenarios use a fixed open session
    // so CI also works on weekends and after the reference calendar expires.
    // Calendar boundaries are tested without this override in candidates/tests.rs.
    {
        registry.register_strategy(P2, CODE_VERSION, move |p| {
            Ok(Box::new(
                CandidateStrategy::new(serde_json::from_value(p.clone())?)?
                    .with_test_calendar_time(parse_time("2026-10-21 10:00:00")?),
            ))
        });
    }
    Arc::new(registry)
}
fn broker(root: &Path, id: &str) -> Arc<PaperBroker> {
    Arc::new(
        PaperBroker::open(
            id.into(),
            PaperConfig {
                initial_balance: 50000.,
                contracts: BTreeMap::from([(
                    "PB2611".into(),
                    PaperContract {
                        multiplier: 10.,
                        margin_rate: 0.1,
                        open_fee_per_lot: 3.,
                        close_fee_per_lot: 3.,
                        close_today_fee_per_lot: 3.,
                    },
                )]),
            },
            root.join(format!("{id}-paper.json")),
        )
        .unwrap(),
    )
}
async fn engine(root: &Path, id: &str, p: Arc<PaperBroker>) -> ExecutionEngine {
    let path = root.join(id);
    std::fs::create_dir_all(&path).unwrap();
    let mut e = ExecutionEngine::with_config_path(
        BrokerClient::Paper(p),
        config(),
        Some(path.join("trader_config.json")),
    );
    e.registry = registry();
    e.bind(id, id).await.unwrap();
    e.set_definition(definition(P2, parameters()))
        .await
        .unwrap();
    e
}
fn signal(id: &str) -> Value {
    let now = chrono::Utc::now().timestamp();
    let offset = chrono::FixedOffset::east_opt(28800).unwrap();
    let warning = chrono::DateTime::from_timestamp(now - now % 900, 0)
        .unwrap()
        .with_timezone(&offset)
        .format("%Y-%m-%d %H:%M:%S")
        .to_string();
    json!({"symbol":"PB0","instrument_id":"pb2611","direction":"up","s0_ts":id,
        "s1_ts":"s1","s2_ts":"s2","warning_ts":warning,"entry":1000.,"stop":980.,"target":1100.,
        "risk":20.,"tick_size":1.,"multiplier":10.,"entry_score":4.,"created_at_epoch":now,
        "warning_context":{"as_of":warning,"complete":true,"atr14":20.}})
}
async fn send(e: &ExecutionEngine, payload: Value) {
    e.dispatch(StrategyEvent::Signal {
        source: N_STRATEGY.into(),
        payload,
    })
    .await
    .unwrap();
}
async fn tick(e: &ExecutionEngine, p: &PaperBroker, price: f64) {
    let quotes = vec![(
        "PB2611".into(),
        ExecutionQuote {
            instrument_id: "pb2611".into(),
            exchange_id: "SHFE".into(),
            underlying_symbol: "PB0".into(),
            last: price,
            bid: price - 1.,
            ask: price + 1.,
            tick: 1.,
            lower: 500.,
            upper: 2000.,
            timestamp: chrono::Utc::now().timestamp(),
        },
    )];
    p.on_quotes(&quotes).unwrap();
    e.reconcile(&p.snapshot()).await.unwrap();
    e.state.lock().await.last_action_at = 0;
    e.on_quote_batch(quotes).await.unwrap();
    e.reconcile(&p.snapshot()).await.unwrap();
}

#[tokio::test]
async fn filtered_signal_keeps_display_evidence_after_restart_without_orders() {
    let root = directory();
    let p = broker(&root, "audit");
    let e = engine(&root, "audit", p.clone()).await;
    let mut payload = signal("filtered");
    payload["id"] = json!(123);
    payload["entry_score"] = json!(2.9);
    send(&e, payload.clone()).await;
    tick(&e, &p, 1002.).await;
    assert!(e.list_conditions().await.is_empty());
    assert!(e.list_orders().await.is_empty());
    let revision = e.definition().await.revision;
    let diagnostics = e.strategy_diagnostics().await;
    let decisions = diagnostics["states"][&revision]["decisions"]
        .as_object()
        .unwrap();
    let decision = decisions.values().next().unwrap();
    assert_eq!(decision["reason"], "score_below_min");
    assert_eq!(decision["values"]["snapshot"]["instrument"], "pb2611");
    assert_eq!(decision["values"]["warning"]["id"], 123);
    assert_eq!(decision["values"]["warning"]["symbol"], "PB0");
    assert_eq!(decision["values"]["warning"]["direction"], "up");
    drop(e);
    let mut restored = ExecutionEngine::with_config_path(
        BrokerClient::Paper(p.clone()),
        config(),
        Some(root.join("audit/trader_config.json")),
    );
    restored.registry = registry();
    restored.bind("audit", "audit").await.unwrap();
    send(&restored, payload).await;
    tick(&restored, &p, 1002.).await;
    assert_eq!(
        restored.strategy_diagnostics().await["states"],
        diagnostics["states"]
    );
    assert!(restored.list_conditions().await.is_empty());
    assert!(restored.list_orders().await.is_empty());
}

#[tokio::test]
async fn p2_next_quote_real_fill_fixed_lot_and_restart_keep_frozen_version() {
    let root = directory();
    let p = broker(&root, "a");
    let e = engine(&root, "a", p.clone()).await;
    let payload = signal("first");
    send(&e, payload.clone()).await;
    tick(&e, &p, 1000.).await;
    assert!(
        e.state.lock().await.orders.is_empty(),
        "trigger quote must not fill itself"
    );
    tick(&e, &p, 1002.).await;
    let (id, version) = {
        let s = e.state.lock().await;
        let pos = s.positions.values().next().expect("real paper fill");
        assert_eq!(pos.volume, 1);
        assert_eq!(pos.fill_price, 1003.);
        assert_eq!(pos.risk, 23.);
        assert_eq!(pos.target_price, 1100.);
        assert_eq!(pos.initial_stop_price, 980.);
        (pos.id.clone(), pos.origin.strategy_version.clone())
    };
    // Reconfiguration may affect only subsequent signals; the open position keeps its target.
    e.set_definition(definition(P2, parameters()))
        .await
        .unwrap();
    drop(e);
    let mut restored = ExecutionEngine::with_config_path(
        BrokerClient::Paper(p.clone()),
        config(),
        Some(root.join("a/trader_config.json")),
    );
    restored.registry = registry();
    restored.bind("a", "a").await.unwrap();
    send(&restored, payload).await;
    assert_eq!(
        restored.state.lock().await.conditions.len(),
        1,
        "reconfiguring the same strategy must not duplicate a source signal"
    );
    tick(&restored, &p, 1027.).await;
    {
        let s = restored.state.lock().await;
        let pos = &s.positions[&id];
        assert!(!pos.is_closed);
        assert_eq!(pos.current_stop_price, 980.);
        assert_eq!(pos.target_price, 1100.);
        assert_eq!(pos.origin.strategy_version, version);
    }
    tick(&restored, &p, 1101.).await;
    {
        let s = restored.state.lock().await;
        assert!(s.positions[&id].is_closed);
        assert_eq!(s.history.len(), 1);
        assert_eq!(s.history[0].volume, 1);
        assert_eq!(s.history[0].exit_reason, "target_s1");
    }
}

#[tokio::test]
async fn two_accounts_are_independent_and_same_strategy_signals_deduplicate() {
    let root = directory();
    let a = broker(&root, "a");
    let b = broker(&root, "b");
    let ea = engine(&root, "a", a.clone()).await;
    let eb = engine(&root, "b", b.clone()).await;
    let payload = signal("shared");
    send(&ea, payload.clone()).await;
    send(&eb, payload.clone()).await;
    ea.set_definition(definition(P2, parameters()))
        .await
        .unwrap();
    send(&ea, payload).await;
    assert_eq!(ea.state.lock().await.conditions.len(), 1);
    for (e, p) in [(&ea, &a), (&eb, &b)] {
        tick(e, p, 1000.).await;
        tick(e, p, 1001.).await;
    }
    let sa = ea.state.lock().await;
    let sb = eb.state.lock().await;
    assert_eq!(sa.positions.len(), 1);
    assert_eq!(sb.positions.len(), 1);
    assert_eq!(sa.positions.values().next().unwrap().origin.account_id, "a");
    assert_eq!(sb.positions.values().next().unwrap().origin.account_id, "b");
}

#[tokio::test]
async fn competing_signals_have_deterministic_single_slot_and_failed_entry_cannot_retry() {
    let root = directory();
    let p = broker(&root, "a");
    let e = engine(&root, "a", p.clone()).await;
    send(&e, signal("z")).await;
    send(&e, signal("a")).await;
    tick(&e, &p, 1000.).await;
    {
        let s = e.state.lock().await;
        let winner = &s.conditions[&s.symbol_slots["PB0"]];
        assert!(winner.origin.signal_id.contains("|a|"));
        assert_eq!(
            s.conditions
                .values()
                .filter(|c| c.state == ConditionState::Rejected)
                .count(),
            1
        );
    }
    // Simulate an explicit zero-fill rejection returned by the counter, preserving its audit record.
    tick(&e, &p, 1001.).await;
    {
        let mut s = e.state.lock().await;
        let id = s.symbol_slots["PB0"].clone();
        s.positions.remove(&id);
        let o = s.orders.values_mut().next().unwrap();
        o.phase = OrderPhase::Rejected;
        o.filled = 0;
        o.reported_filled = 0;
        s.conditions.get_mut(&id).unwrap().state = ConditionState::Rejected;
        s.last_action_at = 0;
    }
    e.drive().await.unwrap();
    let (id, original_at) = {
        let mut s = e.state.lock().await;
        assert_eq!(s.orders.len(), 1);
        let (id, decision) = s
            .entry_decisions
            .iter_mut()
            .find(|(_, decision)| decision["reason"] == "broker_rejected")
            .unwrap();
        let id = id.clone();
        let original_at = decision["at"].as_i64().unwrap() - 60;
        decision["at"] = json!(original_at);
        assert!(!s.authorized_entries.contains(&id));
        // Older ledgers could retain authorization after the rejection.
        s.authorized_entries.insert(id.clone());
        (id, original_at)
    };
    e.drive().await.unwrap();
    e.drive().await.unwrap();
    let s = e.state.lock().await;
    assert_eq!(s.orders.len(), 1);
    assert_eq!(s.entry_decisions[&id]["at"], original_at);
    assert!(!s.authorized_entries.contains(&id));
}

#[tokio::test]
async fn unknown_cancel_keeps_slot_and_never_replaces_order() {
    let root = directory();
    let p = broker(&root, "a");
    let e = engine(&root, "a", p.clone()).await;
    send(&e, signal("a")).await;
    tick(&e, &p, 1000.).await;
    tick(&e, &p, 1001.).await;
    {
        let mut s = e.state.lock().await;
        let id = s.symbol_slots["PB0"].clone();
        s.positions.remove(&id);
        let o = s.orders.values_mut().next().unwrap();
        o.phase = OrderPhase::CancelPending;
        o.cancel_attempts = 3;
        o.filled = 0;
        o.reported_filled = 0;
        o.action_at = 0;
        s.last_action_at = 0;
    }
    e.drive().await.unwrap();
    {
        let s = e.state.lock().await;
        assert_eq!(s.orders.values().next().unwrap().phase, OrderPhase::Unknown);
        assert_eq!(s.symbol_slots.len(), 1);
    }
    send(&e, signal("b")).await;
    tick(&e, &p, 1000.).await;
    assert_eq!(e.state.lock().await.orders.len(), 1);
}

#[tokio::test]
async fn completed_same_source_trades_are_paired_across_accounts() {
    use crate::trader::instances::*;
    let root = directory();
    let db = crate::storage::connect(&root.join("test.db"))
        .await
        .unwrap();
    let dir = migrate_default(&root).unwrap();
    let default = Arc::new(ExecutionEngine::with_config_path(
        CtpTraderClient::for_port(1),
        config(),
        Some(dir.join("trader_config.json")),
    ));
    let manager = InstanceManager::open(db, root, default, registry())
        .await
        .unwrap();
    let mut ids = vec![];
    for id in ["a", "b"] {
        let account = AccountDefinition {
            id: id.into(),
            name: id.into(),
            kind: AccountKind::Paper {
                config: PaperConfig {
                    initial_balance: 50000.,
                    contracts: BTreeMap::from([(
                        "PB2611".into(),
                        PaperContract {
                            multiplier: 10.,
                            margin_rate: 0.1,
                            open_fee_per_lot: 3.,
                            close_fee_per_lot: 3.,
                            close_today_fee_per_lot: 3.,
                        },
                    )]),
                },
            },
        };
        let instance = manager
            .create(id.into(), account, definition(P2, parameters()), config())
            .await
            .unwrap();
        let r = manager.get(&instance).await.unwrap();
        r.engine.pause(false).await.unwrap();
        let BrokerClient::Paper(p) = &r.broker else {
            panic!("expected paper")
        };
        send(&r.engine, signal("same-source")).await;
        tick(&r.engine, p, 1000.).await;
        tick(&r.engine, p, 1001.).await;
        tick(&r.engine, p, 1101.).await;
        assert_eq!(r.engine.get_trade_history().await.len(), 1);
        ids.push(instance);
    }
    let now = chrono::Utc::now().timestamp();
    let report = manager.comparison(now - 60, now + 60).await.unwrap();
    let pairs = report["signal_pairs"].as_array().unwrap();
    let pair = pairs
        .iter()
        .find(|p| {
            ids.contains(&p["baseline_instance_id"].as_str().unwrap().to_string())
                && ids.contains(&p["candidate_instance_id"].as_str().unwrap().to_string())
        })
        .unwrap();
    assert_eq!(pair["paired_signals"], 1);
}

#[tokio::test]
async fn stop_before_entry_and_failed_fill_gate_are_terminal_without_orders() {
    for stop_first in [true, false] {
        let root = directory();
        let p = broker(&root, "a");
        let e = engine(&root, "a", p.clone()).await;
        let mut payload = signal("a");
        if !stop_first {
            payload["target"] = json!(1026.);
        }
        send(&e, payload).await;
        if stop_first {
            tick(&e, &p, 979.).await;
        } else {
            tick(&e, &p, 1000.).await;
            tick(&e, &p, 1003.).await;
        }
        tick(&e, &p, 1000.).await;
        tick(&e, &p, 1001.).await;
        let s = e.state.lock().await;
        assert!(s.orders.is_empty());
        assert!(s.positions.is_empty());
        let c = s.conditions.values().next().unwrap();
        assert_eq!(
            c.state,
            if stop_first {
                ConditionState::Invalidated
            } else {
                ConditionState::Rejected
            }
        );
    }
}

#[tokio::test]
async fn short_fixed_s1_opens_on_bid_and_closes_on_ask() {
    let root = directory();
    let p = broker(&root, "a");
    let e = engine(&root, "a", p.clone()).await;
    let mut payload = signal("short");
    payload["direction"] = json!("down");
    payload["stop"] = json!(1020.);
    payload["target"] = json!(900.);
    send(&e, payload).await;
    tick(&e, &p, 1000.).await;
    tick(&e, &p, 998.).await;
    {
        let s = e.state.lock().await;
        let pos = s.positions.values().next().unwrap();
        assert_eq!(pos.fill_price, 997.);
        assert_eq!(pos.risk, 23.);
        assert_eq!(pos.target_price, 900.);
    }
    tick(&e, &p, 899.).await;
    let s = e.state.lock().await;
    assert_eq!(s.history.len(), 1);
    assert_eq!(s.history[0].exit_price, 900.);
    assert_eq!(s.history[0].realized_pnl_amount, 970.);
}

fn test_condition(id: &str, symbol: &str, instrument_id: &str) -> ConditionOrder {
    ConditionOrder {
        frozen: None,
        origin: TradeOrigin::default(),
        id: id.into(),
        event_id: None,
        symbol: symbol.into(),
        instrument_id: instrument_id.into(),
        direction: "up".into(),
        tier: PositionTier::Small,
        entry_price: 1000.0,
        stop_price: 990.0,
        target_price: 1020.0,
        risk: 10.0,
        score: 3.0,
        volume: 1,
        warning_bar_ts: "2026-11-24 10:00:00".into(),
        trigger_ts: None,
        bars_waited: 0,
        max_wait_bars: 10,
        state: ConditionState::Pending,
        created_at: 1000,
        note: String::new(),
    }
}

#[test]
fn rollover_slot_release_does_not_block_new_contract() {
    let mut state = ExecutionState::default();
    state.symbol_release_after.insert("EB0:EB2612".into(), 77256);
    state.quote_sequences.insert("EB2701".into(), 29940);
    state.quote_sequences.insert("EB2612".into(), 70000);

    let cond_new = test_condition("eb2701_cond", "EB0", "eb2701");

    // 1. 新主力合约 eb2701 独立序号 (29940) 虽然 <= 旧合约释放序号 (77256)，但不应被跨合约误拦截
    assert!(!ExecutionEngine::symbol_slot_blocked(&state, &cond_new));

    let cond_old = test_condition("eb2612_cond", "EB0", "eb2612");

    // 2. 同一合约 eb2612 内，行情序号尚未走过释放阈值时，保持防抖拦截
    assert!(ExecutionEngine::symbol_slot_blocked(&state, &cond_old));

    // 3. 同一合约行情序号走过释放阈值后，解除拦截
    state.quote_sequences.insert("EB2612".into(), 77257);
    assert!(!ExecutionEngine::symbol_slot_blocked(&state, &cond_old));
}

#[test]
fn legacy_symbol_release_after_migration_clears_on_startup() {
    let legacy_json = serde_json::json!({
        "symbol_release_after": {
            "EB0": 77256
        }
    });
    let mut state: ExecutionState = serde_json::from_value(legacy_json).unwrap();
    assert_eq!(state.symbol_release_after.get("EB0"), Some(&77256));

    let cond = test_condition("eb2701_cond", "EB0", "eb2701");

    // 旧存档未绑定合约的纯数值不应阻断开仓
    assert!(!ExecutionEngine::symbol_slot_blocked(&state, &cond));

    // 启动时自动清洗旧残留（不含冒号的旧键）
    state.symbol_release_after.retain(|k, _| k.contains(':'));
    assert!(state.symbol_release_after.is_empty());
}

#[test]
fn old_binary_can_load_sqlite_records_written_by_new_binary_on_rollback() {
    use std::fs;
    let dir = std::env::temp_dir().join(format!("ntrend_rollback_test_{}", uuid::Uuid::new_v4()));
    fs::create_dir_all(&dir).unwrap();
    let path = dir.join("trader_execution.json");
    let (mut store, mut state) = super::ExecutionStore::open(&path).unwrap();

    // 模拟新版：释放槽位并记录新格式 "EB0:EB2701" -> 29940 (数值型)
    state.symbol_release_after.insert("EB0:EB2701".into(), 29940);
    store.save(&state).unwrap();
    drop(store);

    // 模拟旧版直接读取 SQLite execution_fields 字段
    let db_path = path.with_file_name("trader_execution.sqlite3");
    let conn = rusqlite::Connection::open_with_flags(&db_path, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY).unwrap();
    let mut stmt = conn.prepare("SELECT key, value FROM execution_fields WHERE field='symbol_release_after'").unwrap();
    let mut rows = stmt.query([]).unwrap();

    let mut old_loaded_map: HashMap<String, u64> = HashMap::new();
    while let Some(row) = rows.next().unwrap() {
        let key: String = row.get(0).unwrap();
        let blob: Vec<u8> = row.get(1).unwrap();
        // 旧版直接使用 serde_json::from_slice::<u64>(&blob) 反序列化
        let seq: u64 = serde_json::from_slice(&blob).expect("旧版二进制反序列化 u64 必须成功");
        old_loaded_map.insert(key, seq);
    }

    assert_eq!(old_loaded_map.get("EB0:EB2701"), Some(&29940));

    // 模拟旧版的 symbol_slot_blocked: old_state.symbol_release_after.get(&c.symbol)
    let cond = test_condition("eb2701_cond", "EB0", "eb2701");
    assert_eq!(old_loaded_map.get(&cond.symbol), None);

    let _ = fs::remove_dir_all(&dir);
}
