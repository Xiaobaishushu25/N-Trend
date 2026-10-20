//! Three end-to-end state-machine regressions with a fake HTTP counter.
//! No CTP library, credentials, or real trading connection.
use super::*;
use serde_json::{json, Value};
use std::io::{Read, Write};
use std::net::TcpListener;
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, Mutex as StdMutex,
};
use std::time::Duration;

#[tokio::test]
async fn historical_orders_are_paged_newest_first_without_changing_archive() {
    let engine = ExecutionEngine::new(CtpTraderClient::new("http://127.0.0.1:1".into()), ExecutionConfig::default());
    {
        let mut state = engine.state.lock().await;
        for reference in 1..=4031 {
            let order: ExecutionOrder = serde_json::from_value(json!({
                "request":{"instrument_id":"pb2611", "direction":"buy", "offset":"open",
                    "price":1000, "volume":2, "order_ref":reference.to_string()},
                "condition_id":"condition", "reason":null, "phase":"Rejected", "reported_filled":0,
                "filled":0, "action_at":1, "trading_day":if reference>4000 {"20261021"} else {"20261020"},
                "note":"funds rejected"
            })).unwrap();
            state.orders.insert(reference.to_string(), order);
        }
    }
    let first = engine.execution_orders_page(1, 20).await;
    let second = engine.execution_orders_page(2, 20).await;
    assert_eq!(first.total, 4031);
    assert_eq!(first.items.len(), 20);
    assert_eq!(first.items[0].request.order_ref.as_deref(), Some("4031"));
    assert_eq!(first.items[19].request.order_ref.as_deref(), Some("4012"));
    assert_eq!(second.items[0].request.order_ref.as_deref(), Some("4011"));
    assert_eq!(engine.execution_orders_page(202, 20).await.items.len(), 11);
    assert!(engine.execution_orders_page(usize::MAX, 100).await.items.is_empty());
    assert_eq!(engine.execution_orders_page(0, usize::MAX).await.items.len(), 100);
    assert_eq!(engine.execution_orders_page(0, 0).await.page_size, 1);
    assert_eq!(engine.state.lock().await.orders.len(), 4031);
}

struct Counter {
    url: String,
    requests: Arc<StdMutex<Vec<(String, Value)>>>,
    drop_reply: Arc<AtomicBool>,
    reject_funds: Arc<AtomicBool>,
    stop: Arc<AtomicBool>,
    thread: Option<std::thread::JoinHandle<()>>,
    directory: PathBuf,
}

impl Counter {
    fn new() -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        listener.set_nonblocking(true).unwrap();
        let requests = Arc::new(StdMutex::new(Vec::new()));
        let log = requests.clone();
        let stop = Arc::new(AtomicBool::new(false));
        let stopped = stop.clone();
        let drop_reply = Arc::new(AtomicBool::new(false));
        let drop_response = drop_reply.clone();
        let reject_funds = Arc::new(AtomicBool::new(false));
        let reject_response = reject_funds.clone();
        let thread = std::thread::spawn(move || {
            while !stopped.load(Ordering::Relaxed) {
                let Ok((mut socket, _)) = listener.accept() else {
                    std::thread::sleep(Duration::from_millis(1));
                    continue;
                };
                socket.set_nonblocking(false).unwrap();
                socket
                    .set_read_timeout(Some(Duration::from_secs(2)))
                    .unwrap();
                let mut bytes = Vec::new();
                let (start, length, route) = loop {
                    let mut part = [0; 4096];
                    let n = socket.read(&mut part).unwrap();
                    assert!(n > 0);
                    bytes.extend_from_slice(&part[..n]);
                    if let Some(end) = bytes.windows(4).position(|s| s == b"\r\n\r\n") {
                        let header = String::from_utf8_lossy(&bytes[..end]).to_lowercase();
                        let length = header
                            .lines()
                            .find_map(|s| s.strip_prefix("content-length:"))
                            .unwrap()
                            .trim()
                            .parse::<usize>()
                            .unwrap();
                        break (
                            end + 4,
                            length,
                            header.split_whitespace().nth(1).unwrap().to_string(),
                        );
                    }
                };
                while bytes.len() < start + length {
                    let mut part = [0; 4096];
                    let n = socket.read(&mut part).unwrap();
                    assert!(n > 0);
                    bytes.extend_from_slice(&part[..n]);
                }
                let request: Value = serde_json::from_slice(&bytes[start..start + length]).unwrap();
                let reject = reject_response.swap(false, Ordering::Relaxed);
                let response = json!({"success":!reject,"order_ref":request["order_ref"],
                    "msg":if reject {"柜台拒绝报单 [31]: CTP:资金不足，约缺少资金[1000.00]"} else {"submitted; no synthetic fill"}}).to_string();
                log.lock().unwrap().push((route, request));
                if drop_response.swap(false, Ordering::Relaxed) {
                    continue;
                }
                write!(socket,"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",response.len(),response).unwrap();
            }
        });
        let directory =
            std::env::temp_dir().join(format!("ntrend-execution-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir(&directory).unwrap();
        Self {
            url,
            requests,
            drop_reply,
            reject_funds,
            stop,
            thread: Some(thread),
            directory,
        }
    }
    fn engine(&self, mode: ExecutionMode, volume: i32) -> ExecutionEngine {
        ExecutionEngine::with_config_path(
            CtpTraderClient::new(self.url.clone()),
            ExecutionConfig {
                mode,
                default_large_volume: volume,
                ..Default::default()
            },
            Some(self.directory.join("trader_config.json")),
        )
    }
    fn inserts(&self) -> Vec<Value> {
        self.requests
            .lock()
            .unwrap()
            .iter()
            .filter(|(route, _)| route == "/order/insert")
            .map(|(_, r)| r.clone())
            .collect()
    }
}
impl Drop for Counter {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        let _ = self.thread.take().unwrap().join();
        std::fs::remove_dir_all(&self.directory).unwrap();
    }
}

async fn ready(engine: &ExecutionEngine, orders: Vec<Value>, trades: Vec<Value>) {
    ready_with_funds(engine, orders, trades, 50000.0, 0.0).await;
}
async fn ready_with_funds(
    engine: &ExecutionEngine,
    orders: Vec<Value>,
    trades: Vec<Value>,
    available: f64,
    margin: f64,
) {
    engine
        .reconcile_entry_age("PB0", &["2026-10-16 10:00:00".into()])
        .await
        .unwrap();
    engine
        .reconcile(&json!({"bridge_online":true,"health": {
        "ready":true,"logged_in":true,"sync_pending":false,"trading_day":"20261016",
        "broker_id":"fake","user_id":"offline-test"
    }, "account":{"available":available,"curr_margin":margin,"updated_at":chrono::Utc::now().timestamp_micros() as f64 / 1000000.0},
       "orders":orders,"trades":trades,"broker_positions":[]}))
        .await
        .unwrap();
}
async fn register(engine: &ExecutionEngine, entry: f64, stop: f64) -> String {
    engine
        .register_condition(
            Some(123),
            "PB0",
            "pb2611",
            Dir::Up,
            entry,
            stop,
            1200.0,
            3.6,
            "2026-10-16 10:00:00",
        )
        .await
        .unwrap()
}
async fn age(engine: &ExecutionEngine) {
    let mut s = engine.state.lock().await;
    s.last_action_at = 0;
    for o in s.orders.values_mut() {
        o.action_at -= 10;
    }
}
async fn quote(engine: &ExecutionEngine, price: f64) {
    let q = ExecutionQuote {
        instrument_id: "pb2611".into(),
        exchange_id: "SHFE".into(),
        underlying_symbol: String::new(),
        last: price,
        bid: price - 1.0,
        ask: price + 1.0,
        tick: 1.0,
        lower: 500.0,
        upper: 2000.0,
        timestamp: chrono::Utc::now().timestamp(),
    };
    engine
        .on_quotes([("PB2611".into(), q)].into_iter().collect())
        .await
        .unwrap();
    engine.drive().await.unwrap();
}
fn order(request: &Value, status: &str, filled: i32) -> Value {
    json!({"order_ref":request["order_ref"],"instrument_id":"pb2611","status":status,"volume_traded":filled,"status_msg":"counter state"})
}
fn trade(request: &Value, id: &str, volume: i32, price: f64) -> Value {
    json!({"order_ref":request["order_ref"],"instrument_id":"pb2611","exchange_id":"SHFE","direction":request["direction"],
        "offset":if request["offset"] == "open" {"open"} else {"close_today"},"trade_id":id,"volume":volume,"price":price,
        "trade_date":"20261016","trade_time":"10:00:01"})
}

#[tokio::test]
async fn continuous_trailing_waits_for_first_fill_tracks_each_quote_and_survives_restart() {
    for (direction, sign) in [(Dir::Up, 1.0), (Dir::Down, -1.0)] {
        let counter = Counter::new();
        let mut engine = counter.engine(ExecutionMode::FullAuto, 2);
        ready(&engine, vec![], vec![]).await;
        let id = engine
            .register_condition(
                Some(321),
                "PB0",
                "pb2611",
                direction,
                1000.0,
                1000.0 - sign * 10.0,
                1000.0 + sign * 200.0,
                3.6,
                "2026-10-16 10:00:00",
            )
            .await
            .unwrap();
        quote(&engine, 1000.0).await;
        let open = counter.inserts()[0].clone();
        let opened = trade(&open, "open", 2, 1000.0);
        ready(
            &engine,
            vec![order(&open, "all_traded", 2)],
            vec![opened.clone()],
        )
        .await;

        age(&engine).await;
        quote(&engine, 1000.0 + sign * 10.0).await;
        let close1 = counter.inserts()[1].clone();
        assert_eq!(close1["volume"], 1);
        quote(&engine, 1000.0 + sign * 12.0).await;
        let pos = engine.list_positions().await.remove(0);
        assert!(!pos.lot1_closed);
        assert_eq!(pos.current_stop_price, 1000.0 - sign * 10.0);

        let first_exit = trade(&close1, "first-exit", 1, 1000.0 + sign * 10.0);
        let mut orders = vec![
            order(&open, "all_traded", 2),
            order(&close1, "all_traded", 1),
        ];
        let mut trades = vec![opened, first_exit];
        ready(&engine, orders.clone(), trades.clone()).await;
        let pos = engine.list_positions().await.remove(0);
        assert!(pos.lot1_closed);
        assert_eq!(pos.volume, 1);
        assert_eq!(pos.current_stop_price, 1000.0);

        for (mfe, locked) in [
            (1.59, 0.0),
            (1.6, 0.8),
            (2.08, 1.28),
            (2.09, 1.29),
            (2.69, 1.89),
        ] {
            quote(&engine, 1000.0 + sign * mfe * 10.0).await;
            let pos = engine.list_positions().await.remove(0);
            assert!((pos.current_stop_price - (1000.0 + sign * locked * 10.0)).abs() < 1e-8);
            assert_eq!(pos.trailing_grade > 1, locked > 0.0);
        }
        assert_eq!(counter.inserts().len(), 2);
        let stop = engine.list_positions().await[0].current_stop_price;
        drop(engine);
        engine = counter.engine(ExecutionMode::FullAuto, 2);
        ready(&engine, orders.clone(), trades.clone()).await;
        quote(&engine, 1000.0 + sign * 25.0).await;
        assert_eq!(engine.list_positions().await[0].current_stop_price, stop);
        assert_eq!(counter.inserts().len(), 2);

        age(&engine).await;
        quote(&engine, stop - sign * 0.01).await;
        let close2 = counter.inserts()[2].clone();
        assert_eq!(close2["volume"], 1);
        assert_eq!(close2["direction"], if sign > 0.0 { "sell" } else { "buy" });
        {
            let state = engine.state.lock().await;
            assert_eq!(state.closes[&id].reason, "trailing");
            assert_eq!(state.closes[&id].reference, stop);
        }
        assert!(!engine.list_positions().await[0].is_closed); // Wait for the actual fill.
        orders.push(order(&close2, "all_traded", 1));
        trades.push(trade(&close2, "second-exit", 1, stop));
        ready(&engine, orders, trades).await;
        assert!(engine.list_positions().await[0].is_closed);
        assert_eq!(engine.get_trade_history().await.len(), 2);
    }
}

#[tokio::test]
async fn one_signal_survives_restart_unknown_ack_and_repeated_crossings_without_fake_fills() {
    let counter = Counter::new();
    let engine = counter.engine(ExecutionMode::FullAuto, 2);
    ready(&engine, vec![], vec![]).await;
    let id = register(&engine, 1000.0, 990.0).await;
    engine.state.lock().await.entry_symbols_ready.clear();
    quote(&engine, 1000.0).await;
    assert!(counter.inserts().is_empty()); // Startup waits for pending-signal age reconciliation.
    engine
        .reconcile_entry_age("PB0", &["2026-10-16 10:00:00".into()])
        .await
        .unwrap();
    counter.drop_reply.store(true, Ordering::Relaxed);
    quote(&engine, 1000.0).await;
    assert!(engine.list_positions().await.is_empty());
    assert_eq!(counter.inserts().len(), 1);
    drop(engine);
    let engine = counter.engine(ExecutionMode::FullAuto, 2);
    assert_eq!(register(&engine, 1000.0, 990.0).await, id);
    ready(&engine, vec![], vec![]).await;
    for p in [999.0, 1001.0, 999.0, 1000.0] {
        age(&engine).await;
        quote(&engine, p).await;
    }
    assert_eq!(counter.inserts().len(), 1); // Unknown does not mean failed: never blind resend.
    let first = counter.inserts()[0].clone();
    let fill1 = trade(&first, "t1", 1, 1002.0);
    ready(
        &engine,
        vec![order(&first, "canceled", 1)],
        vec![fill1.clone()],
    )
    .await;
    assert_eq!(engine.list_positions().await[0].volume, 1);
    age(&engine).await;
    quote(&engine, 1000.0).await;
    let second = counter.inserts()[1].clone();
    assert_eq!(second["volume"], 1);
    let fills = vec![fill1, trade(&second, "t2", 1, 1004.0)];
    let orders = vec![
        order(&first, "canceled", 1),
        order(&second, "all_traded", 1),
    ];
    ready(&engine, orders.clone(), fills.clone()).await;
    ready(&engine, orders, fills).await; // Replay must not add the fills twice.
    let pos = &engine.list_positions().await[0];
    assert_eq!(pos.volume, 2);
    assert_eq!(pos.fill_price, 1003.0);
    assert!(engine.get_trade_history().await.is_empty());
}

#[tokio::test]
async fn semi_auto_stop_waits_for_cancel_and_late_fills_then_reprices_only_remaining_quantity() {
    let counter = Counter::new();
    let engine = counter.engine(ExecutionMode::SemiAuto, 3);
    ready(&engine, vec![], vec![]).await;
    let id = register(&engine, 1010.0, 1000.0).await;
    quote(&engine, 1010.0).await;
    assert!(counter.inserts().is_empty());
    assert!(engine.confirm_open(&id).await.unwrap());
    engine.drive().await.unwrap();
    let open = counter.inserts()[0].clone();
    let opened = trade(&open, "open", 3, 1010.0);
    ready(
        &engine,
        vec![order(&open, "all_traded", 3)],
        vec![opened.clone()],
    )
    .await;
    age(&engine).await;
    quote(&engine, 1000.0).await;
    let close = counter.inserts()[1].clone();
    assert_eq!(close["volume"], 3);
    assert!(close["price"].as_f64().unwrap() < 1000.0);
    assert_eq!(engine.list_positions().await[0].volume, 3);
    assert!(engine.get_trade_history().await.is_empty());
    let fill1 = trade(&close, "close1", 1, 999.0);
    ready(
        &engine,
        vec![
            order(&open, "all_traded", 3),
            order(&close, "part_traded", 1),
        ],
        vec![opened.clone(), fill1.clone()],
    )
    .await;
    age(&engine).await;
    quote(&engine, 990.0).await;
    assert_eq!(
        counter.requests.lock().unwrap().last().unwrap().0,
        "/order/cancel"
    );
    assert_eq!(counter.inserts().len(), 2);
    let canceled = vec![order(&open, "all_traded", 3), order(&close, "canceled", 2)];
    ready(
        &engine,
        canceled.clone(),
        vec![opened.clone(), fill1.clone()],
    )
    .await;
    age(&engine).await;
    engine.drive().await.unwrap();
    assert_eq!(counter.inserts().len(), 2); // The second reported fill has not arrived yet.
    let fill2 = trade(&close, "close2", 1, 998.0);
    let mut fills = vec![opened, fill1, fill2];
    ready(&engine, canceled.clone(), fills.clone()).await;
    age(&engine).await;
    quote(&engine, 985.0).await;
    let replacement = counter.inserts()[2].clone();
    assert_eq!(replacement["volume"], 1);
    assert!(replacement["price"].as_f64().unwrap() < close["price"].as_f64().unwrap());
    assert_eq!(engine.list_positions().await[0].volume, 1);
    fills.push(trade(&replacement, "close3", 1, 984.0));
    let mut orders = canceled;
    orders.push(order(&replacement, "all_traded", 1));
    ready(&engine, orders, fills).await;
    assert!(engine.list_positions().await[0].is_closed);
    assert_eq!(engine.get_trade_history().await.len(), 3);
}

#[tokio::test]
async fn stop_during_partial_entry_cancels_open_and_exits_late_fills_without_reentering() {
    let counter = Counter::new();
    let engine = counter.engine(ExecutionMode::FullAuto, 3);
    ready(&engine, vec![], vec![]).await;
    register(&engine, 1010.0, 1000.0).await;
    quote(&engine, 1010.0).await;
    let open = counter.inserts()[0].clone();
    let first = trade(&open, "open1", 1, 1010.0);
    ready(
        &engine,
        vec![order(&open, "part_traded", 1)],
        vec![first.clone()],
    )
    .await;
    age(&engine).await;
    quote(&engine, 1000.0).await;
    assert_eq!(
        counter.requests.lock().unwrap().last().unwrap().0,
        "/order/cancel"
    );
    let late = trade(&open, "open2", 1, 1005.0);
    ready(
        &engine,
        vec![order(&open, "canceled", 2)],
        vec![first, late],
    )
    .await;
    assert_eq!(engine.list_positions().await[0].volume, 2);
    age(&engine).await;
    quote(&engine, 1000.0).await;
    assert_eq!(counter.inserts()[1]["volume"], 2);
    assert_eq!(counter.inserts()[1]["direction"], "sell");
    for p in [1010.0, 999.0, 1010.0] {
        age(&engine).await;
        quote(&engine, p).await;
    }
    assert_eq!(
        counter
            .inserts()
            .iter()
            .filter(|r| r["offset"] == "open")
            .count(),
        1
    );
}

#[tokio::test]
async fn sub_1r_target_presets_1to1_and_ignores_s1_until_reaching_1r() {
    let counter = Counter::new();
    let engine = counter.engine(ExecutionMode::FullAuto, 2);
    ready(&engine, vec![], vec![]).await;

    // Register a long condition where S1 target (1015.0) is smaller than risk (1010 - 1000 = 10.0):
    // Target space = 5.0 <= 10.0 risk (RR = 0.5 <= 1.0).
    let id = engine
        .register_condition(
            Some(201),
            "PB0",
            "pb2611",
            Dir::Up,
            1010.0,
            1000.0,
            1015.0, // S1 target is 5 pts above entry, but risk is 10 pts
            3.6,
            "2026-10-16 10:00:00",
        )
        .await
        .unwrap();

    // 1. Verify condition target_price is preset to 1:1 (1020.0), not S1 (1015.0)
    {
        let s = engine.state.lock().await;
        let cond = s.conditions.get(&id).unwrap();
        assert_eq!(cond.target_price, 1020.0);
        assert!(cond.note.contains("目标空间<1R(S1:1015.0)，止盈预设1:1"));
    }

    // 2. Trigger entry and fill at 1010.0
    quote(&engine, 1010.0).await;
    assert_eq!(counter.inserts().len(), 1);
    let open = counter.inserts()[0].clone();
    assert_eq!(open["volume"], 2);
    let opened = trade(&open, "open1", 2, 1010.0);
    ready(
        &engine,
        vec![order(&open, "all_traded", 2)],
        vec![opened.clone()],
    )
    .await;

    // Position is established with 2 lots, target_price = 1020.0
    let pos = &engine.list_positions().await[0];
    assert_eq!(pos.volume, 2);
    assert_eq!(pos.target_price, 1020.0);

    // 3. Price touches original S1 (1015.0):
    // Must NOT exit or latch target_s1! Still holds 2 lots.
    age(&engine).await;
    quote(&engine, 1015.0).await;
    assert_eq!(counter.inserts().len(), 1); // No new order inserted
    assert_eq!(engine.list_positions().await[0].volume, 2);

    // 4. Price touches 1.0R (1020.0):
    // Should trigger target_1r for lot 1 (1 lot)
    age(&engine).await;
    quote(&engine, 1020.0).await;
    assert_eq!(counter.inserts().len(), 2);
    let close1 = counter.inserts()[1].clone();
    assert_eq!(close1["volume"], 1);
    assert_eq!(close1["offset"], "auto");
    assert_eq!(close1["direction"], "sell");

    // 5. Lot 1 fills at 1020.0:
    let close1_fill = trade(&close1, "close1", 1, 1020.0);
    ready(
        &engine,
        vec![
            order(&open, "all_traded", 2),
            order(&close1, "all_traded", 1),
        ],
        vec![opened, close1_fill],
    )
    .await;

    // Remaining 1 lot moves to breakeven, and must NOT exit at target_s1
    let pos = &engine.list_positions().await[0];
    assert_eq!(pos.volume, 1);
    assert!(pos.lot1_closed);
    assert_eq!(pos.current_stop_price, 1010.0); // breakeven stop

    // Quote 1020.0 again - target_r is 1.0, not > 1.0, so no target_s1 exit
    age(&engine).await;
    quote(&engine, 1020.0).await;
    assert_eq!(counter.inserts().len(), 2);

    // Also verify registering inverted S1 (target below entry for long) does not fail
    let inverted_id = engine
        .register_condition(
            Some(202),
            "PB0",
            "pb2611",
            Dir::Up,
            1010.0,
            1000.0,
            1005.0, // inverted S1 (below entry)
            3.6,
            "2026-10-16 10:05:00",
        )
        .await
        .unwrap();
    {
        let s = engine.state.lock().await;
        let cond = s.conditions.get(&inverted_id).unwrap();
        assert_eq!(cond.target_price, 1020.0);
        assert!(cond.note.contains("目标空间<1R(S1:1005.0)，止盈预设1:1"));
    }
}

#[tokio::test]
async fn entry_overshoot_beyond_point_two_r_suspends_order_until_pullback() {
    let counter = Counter::new();
    let engine = counter.engine(ExecutionMode::FullAuto, 2);
    ready(&engine, vec![], vec![]).await;

    // entry = 1000.0, stop = 990.0 (risk = 10.0) -> max slippage = max(10 * 0.2, 2 * 1) = 2.0 (max buy 1002.0)
    let id = register(&engine, 1000.0, 990.0).await;

    // Market gaps up to 1005.0 (ask = 1006.0, exceeding 1002.0 limit)
    quote(&engine, 1005.0).await;

    // Must NOT insert any buy order at sky-high price!
    assert!(counter.inserts().is_empty());
    {
        let s = engine.state.lock().await;
        let cond = s.conditions.get(&id).unwrap();
        assert!(cond.note.contains("超入场滑点限制(上限1002.0)"));
    }

    // Market pulls back to 1000.0 (ask = 1001.0, within 1002.0 limit)
    age(&engine).await;
    quote(&engine, 1000.0).await;

    // Order is now safely inserted!
    assert_eq!(counter.inserts().len(), 1);
    let open = counter.inserts()[0].clone();
    assert_eq!(open["volume"], 2);
    assert_eq!(open["direction"], "buy");
    assert!(open["price"].as_f64().unwrap() <= 1002.0);
}

#[tokio::test]
async fn cancel_pending_deadlock_unblocks_and_does_not_starve_other_conditions() {
    let counter = Counter::new();
    let engine = counter.engine(ExecutionMode::FullAuto, 2);
    ready(&engine, vec![], vec![]).await;

    // Condition A has an order that was sent to cancel, but counter was non-responsive
    let id_a = register(&engine, 1000.0, 990.0).await;
    {
        let mut s = engine.state.lock().await;
        s.orders.insert(
            "ref_stuck".to_string(),
            ExecutionOrder {
                request: OrderInsertRequest {
                    instrument_id: "inst_a".to_string(),
                    exchange_id: Some("DCE".to_string()),
                    direction: "buy".to_string(),
                    offset: "open".to_string(),
                    price: 1000.0,
                    volume: 1,
                    order_ref: Some("ref_stuck".to_string()),
                },
                condition_id: id_a.clone(),
                reason: None,
                phase: OrderPhase::CancelPending,
                reported_filled: 0,
                filled: 0,
                action_at: chrono::Utc::now().timestamp() - 20, // 20s ago
                trading_day: "20261019".to_string(),
                note: "waiting for counter".into(),
                cancel_attempts: 3,
                funds_at_submit: None,
                entry_rules: None,
            },
        );
    }

    // Now drive runs. It should unblock stuck order to Rejected and not loop endlessly!
    engine.drive().await.unwrap();

    {
        let s = engine.state.lock().await;
        let o = s.orders.get("ref_stuck").unwrap();
        assert_eq!(o.phase, OrderPhase::Rejected);
        assert!(o.note.contains("撤单多次未获回报"));
    }
}

#[tokio::test]
async fn insufficient_funds_wait_survives_quote_spam_replay_restart_and_small_balance_changes() {
    let counter = Counter::new();
    let engine = counter.engine(ExecutionMode::FullAuto, 2);
    ready_with_funds(&engine, vec![], vec![], 1000.0, 40000.0).await;
    let id = register(&engine, 1000.0, 990.0).await;
    quote(&engine, 1000.0).await;
    let first = counter.inserts()[0].clone();
    let mut denied = order(&first, "rejected", 0);
    denied["status_msg"] = json!("交易所拒绝报单 [31]: CTP:资金不足，约缺少资金[1000.00]");
    ready_with_funds(&engine, vec![denied.clone()], vec![], 1000.0, 40000.0).await;
    for _ in 0..100 {
        age(&engine).await;
        quote(&engine, 1000.0).await;
    }
    assert_eq!(counter.inserts().len(), 1);
    assert!(engine.list_conditions().await[0].note.contains("资金不足"));
    drop(engine);

    let engine = counter.engine(ExecutionMode::FullAuto, 2);
    ready_with_funds(&engine, vec![denied.clone()], vec![], 1500.0, 40000.0).await;
    for _ in 0..100 {
        age(&engine).await;
        quote(&engine, 1000.0).await;
    }
    assert_eq!(counter.inserts().len(), 1); // More funds, still below required amount.
    ready_with_funds(&engine, vec![denied.clone()], vec![], 2001.0, 39000.0).await;
    age(&engine).await;
    quote(&engine, 1000.0).await;
    assert_eq!(counter.inserts().len(), 2);
    assert!(!engine
        .state
        .lock()
        .await
        .entry_funding_waits
        .contains_key(&id));
    // Replaying the old rejection must not reinstate a wait over the newer intent.
    ready_with_funds(&engine, vec![denied], vec![], 2001.0, 39000.0).await;
    age(&engine).await;
    quote(&engine, 1000.0).await;
    assert_eq!(counter.inserts().len(), 2);
}

#[tokio::test]
async fn immediate_funds_rejection_is_not_retried_by_the_next_drive() {
    let counter = Counter::new();
    let engine = counter.engine(ExecutionMode::FullAuto, 2);
    ready_with_funds(&engine, vec![], vec![], 1000.0, 40000.0).await;
    register(&engine, 1000.0, 990.0).await;
    counter.reject_funds.store(true, Ordering::Relaxed);
    quote(&engine, 1000.0).await;
    assert_eq!(counter.inserts().len(), 1);
    for _ in 0..10 {
        age(&engine).await;
        quote(&engine, 1000.0).await;
    }
    assert_eq!(counter.inserts().len(), 1);
}

#[tokio::test]
async fn rejection_without_numeric_shortfall_waits_for_margin_release_not_floating_profit() {
    let counter = Counter::new();
    let engine = counter.engine(ExecutionMode::FullAuto, 2);
    ready_with_funds(&engine, vec![], vec![], 1000.0, 40000.0).await;
    register(&engine, 1000.0, 990.0).await;
    quote(&engine, 1000.0).await;
    let mut denied = order(&counter.inserts()[0], "rejected", 0);
    denied["status_msg"] = json!("柜台拒绝报单 [31]: 资金不足");
    ready_with_funds(&engine, vec![denied.clone()], vec![], 1000.0, 40000.0).await;
    ready_with_funds(&engine, vec![denied.clone()], vec![], 1001.0, 40000.0).await;
    age(&engine).await;
    quote(&engine, 1000.0).await;
    assert_eq!(counter.inserts().len(), 1);
    ready_with_funds(&engine, vec![denied], vec![], 15000.0, 20000.0).await;
    age(&engine).await;
    quote(&engine, 1000.0).await;
    assert_eq!(counter.inserts().len(), 2);
}

#[tokio::test]
async fn narrow_stop_filter_is_configurable_and_old_conditions_cannot_bypass_it() {
    let counter = Counter::new();
    let engine = counter.engine(ExecutionMode::FullAuto, 2);
    let mut cfg = engine.get_config().await;
    cfg.min_entry_risk_ticks = 10;
    engine.update_config(cfg).await.unwrap();
    ready(&engine, vec![], vec![]).await;
    let id = register(&engine, 1000.0, 995.0).await;
    quote(&engine, 1000.0).await;
    assert!(counter.inserts().is_empty());
    assert!(engine.list_conditions().await[0]
        .note
        .contains("止损距离5.00 tick小于10 tick"));
    let mut cfg = engine.get_config().await;
    cfg.min_entry_risk_ticks = 5;
    engine.update_config(cfg).await.unwrap();
    age(&engine).await;
    quote(&engine, 1000.0).await;
    assert_eq!(counter.inserts().len(), 1);
    assert_eq!(counter.inserts()[0]["volume"], 2);
    assert_eq!(engine.list_conditions().await[0].id, id);
}

#[test]
fn user_selected_live_entry_filter_defaults_to_ten_for_new_and_legacy_configs() {
    assert_eq!(ExecutionConfig::default().min_entry_risk_ticks, 10);
    let mut legacy = serde_json::to_value(ExecutionConfig::default()).unwrap();
    legacy
        .as_object_mut()
        .unwrap()
        .remove("min_entry_risk_ticks");
    let loaded: ExecutionConfig = serde_json::from_value(legacy).unwrap();
    assert_eq!(loaded.min_entry_risk_ticks, 10);
}

#[tokio::test]
async fn zero_or_stale_account_funds_block_new_entries_but_never_block_stop_exits() {
    let counter = Counter::new();
    let engine = counter.engine(ExecutionMode::FullAuto, 2);
    ready_with_funds(&engine, vec![], vec![], 0.0, 50000.0).await;
    register(&engine, 1000.0, 990.0).await;
    quote(&engine, 1000.0).await;
    assert!(counter.inserts().is_empty());
    ready(&engine, vec![], vec![]).await;
    engine
        .state
        .lock()
        .await
        .entry_funds
        .as_mut()
        .unwrap()
        .updated_at -= 120.0;
    age(&engine).await;
    quote(&engine, 1000.0).await;
    assert!(counter.inserts().is_empty());
    ready(&engine, vec![], vec![]).await;
    age(&engine).await;
    quote(&engine, 1000.0).await;
    let open = counter.inserts()[0].clone();
    ready_with_funds(
        &engine,
        vec![order(&open, "all_traded", 2)],
        vec![trade(&open, "open", 2, 1000.0)],
        0.0,
        50000.0,
    )
    .await;
    let mut cfg = engine.get_config().await;
    cfg.min_entry_risk_ticks = 100;
    engine.update_config(cfg).await.unwrap();
    age(&engine).await;
    quote(&engine, 990.0).await;
    let inserts = counter.inserts();
    assert_eq!(inserts.len(), 2);
    assert_eq!(inserts[1]["offset"], "auto");
    assert_eq!(inserts[1]["direction"], "sell");
}

#[tokio::test]
async fn legacy_funds_rejection_is_migrated_to_wait_and_expiry_prevents_late_reentry() {
    let counter = Counter::new();
    let engine = counter.engine(ExecutionMode::FullAuto, 2);
    ready_with_funds(&engine, vec![], vec![], 1000.0, 40000.0).await;
    let id = register(&engine, 1000.0, 990.0).await;
    counter.reject_funds.store(true, Ordering::Relaxed);
    quote(&engine, 1000.0).await;
    drop(engine);
    let path = counter.directory.join("trader_execution.json");
    let mut old: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
    old.as_object_mut().unwrap().remove("entry_funding_waits");
    for order in old["orders"].as_object_mut().unwrap().values_mut() {
        order.as_object_mut().unwrap().remove("funds_at_submit");
    }
    std::fs::write(&path, serde_json::to_vec(&old).unwrap()).unwrap();
    let engine = counter.engine(ExecutionMode::FullAuto, 2);
    ready_with_funds(&engine, vec![], vec![], 1000.0, 40000.0).await;
    quote(&engine, 1000.0).await;
    assert_eq!(counter.inserts().len(), 1);
    assert!(engine
        .state
        .lock()
        .await
        .entry_funding_waits
        .contains_key(&id));
    // Signal expires while waiting for capital; a later replenishment is not a new signal.
    let bars: Vec<String> = (0..=13)
        .map(|i| format!("2026-10-16 10:{i:02}:00"))
        .collect();
    engine.reconcile_entry_age("PB0", &bars).await.unwrap();
    ready_with_funds(&engine, vec![], vec![], 50000.0, 0.0).await;
    age(&engine).await;
    quote(&engine, 1000.0).await;
    assert_eq!(counter.inserts().len(), 1);
    assert_eq!(
        engine.list_conditions().await[0].state,
        ConditionState::Expired
    );
}

#[tokio::test]
async fn real_filter_skips_orders_while_baseline_shadows_continue_and_archive() {
    let counter = Counter::new();
    let engine = counter.engine(ExecutionMode::FullAuto, 2);
    ready(&engine, vec![], vec![]).await;
    let id = register(&engine, 1000.0, 995.0).await;
    quote(&engine, 1000.0).await;
    assert!(counter.inserts().is_empty());
    let summary = engine.comparison_summary().await;
    assert_eq!(summary["runs"][0]["groups"][0]["opened"], 1);
    assert_eq!(summary["runs"][0]["groups"][1]["filtered"], 1);
    engine.cancel_condition(&id).await.unwrap();
    assert!(engine
        .requested_contracts()
        .await
        .contains(&"pb2611".to_owned()));
    quote(&engine, 1008.0).await;
    quote(&engine, 1001.0).await;
    assert!(counter.inserts().is_empty());
    let summary = engine.comparison_summary().await;
    assert_eq!(summary["runs"][0]["groups"][0]["closed"], 1);
    assert!(counter.directory.join("strategy_comparison.jsonl").exists());
    drop(engine);
    let restored = counter.engine(ExecutionMode::FullAuto, 2);
    assert_eq!(restored.comparison_summary().await, summary);
}

#[tokio::test]
async fn comparison_archive_failure_does_not_disable_real_stop_orders() {
    let counter = Counter::new();
    let engine = counter.engine(ExecutionMode::FullAuto, 2);
    ready(&engine, vec![], vec![]).await;
    register(&engine, 1000.0, 990.0).await;
    quote(&engine, 1000.0).await;
    let open = counter.inserts()[0].clone();
    let orders = engine.list_orders().await;
    assert_eq!(
        orders[0]
            .entry_rules
            .as_ref()
            .unwrap()
            .config
            .min_entry_risk_ticks,
        10
    );
    ready(
        &engine,
        vec![order(&open, "all_traded", 2)],
        vec![trade(&open, "open", 2, 1000.0)],
    )
    .await;
    std::fs::create_dir(counter.directory.join("strategy_comparison.jsonl")).unwrap();
    age(&engine).await;
    quote(&engine, 990.0).await;
    assert_eq!(counter.inserts().len(), 2);
    assert_eq!(counter.inserts()[1]["offset"], "auto");
    assert!(engine.state.lock().await.persistence_error.is_none());
    assert!(engine.comparison_summary().await["archive_error"].is_string());
}
