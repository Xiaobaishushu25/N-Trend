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

#[path = "execution_foundation_tests.rs"]
mod foundation;
#[path = "p3_execution_tests.rs"]
mod p3_tests;

#[path = "execution_market_tests.rs"]
mod market_tests;

#[tokio::test]
async fn historical_orders_are_paged_newest_first_without_changing_archive() {
    let counter = Counter::new();
    let engine = counter.engine(ExecutionMode::MonitorOnly, 2);
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
        engine.save(&mut state).unwrap();
    }
    drop(engine);
    let engine = counter.engine(ExecutionMode::MonitorOnly, 2);
    let first = engine.execution_orders_page(1, 20).await;
    let second = engine.execution_orders_page(2, 20).await;
    assert_eq!(first.total, 4031);
    assert_eq!(first.items.len(), 20);
    assert_eq!(first.items[0].request.order_ref.as_deref(), Some("4031"));
    assert_eq!(first.items[19].request.order_ref.as_deref(), Some("4012"));
    assert_eq!(second.items[0].request.order_ref.as_deref(), Some("4011"));
    assert_eq!(engine.execution_orders_page(202, 20).await.items.len(), 11);
    assert!(engine
        .execution_orders_page(usize::MAX, 100)
        .await
        .items
        .is_empty());
    assert_eq!(
        engine
            .execution_orders_page(0, usize::MAX)
            .await
            .items
            .len(),
        100
    );
    assert_eq!(engine.execution_orders_page(0, 0).await.page_size, 1);
    assert_eq!(engine.state.lock().await.orders.len(), 4031);
}

#[tokio::test]
async fn cancelled_order_in_archive_accepts_a_late_fill_after_restart() {
    let counter = Counter::new();
    let engine = counter.engine(ExecutionMode::FullAuto, 2);
    ready(&engine, vec![], vec![]).await;
    let id = register(&engine, 1000.0, 990.0).await;
    quote(&engine, 1000.0).await;
    let open = counter.inserts()[0].clone();
    let canceled = order(&open, "canceled", 0);
    ready(&engine, vec![canceled.clone()], vec![]).await;
    let reference = open["order_ref"].as_str().unwrap();
    let marker = counter.directory.join("trader_execution.json");
    let connection = rusqlite::Connection::open(marker.with_extension("sqlite3")).unwrap();
    let archived = || {
        connection.query_row("SELECT settled FROM execution_orders WHERE reference=?1",
            [reference], |row| row.get::<_, bool>(0)).unwrap()
    };
    assert!(archived());
    drop(engine);

    let engine = counter.engine(ExecutionMode::FullAuto, 2);
    let fill = trade(&open, "late-fill", 1, 1000.0);
    ready(&engine, vec![canceled.clone()], vec![fill.clone()]).await;
    ready(&engine, vec![canceled], vec![fill]).await;
    let state = engine.state.lock().await;
    assert_eq!(state.orders[reference].filled, 1);
    assert_eq!(state.positions[&id].volume, 1);
    assert_eq!(state.seen_trades.len(), 1);
    assert!(archived());
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
        timestamp: engine.now_utc().timestamp(),
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
async fn semi_auto_stop_reserves_late_fills_and_closes_only_unreserved_quantity() {
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
    // One reported fill is missing, so reserve it and close the other known lot.
    assert_eq!(counter.inserts().len(), 3);
    assert_eq!(counter.inserts()[2]["volume"], 1);
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
    assert_eq!(counter.inserts()[1]["volume"], 1);
    engine.drive().await.unwrap();
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
    assert_eq!(counter.inserts()[1]["volume"], 1);
    assert_eq!(counter.inserts()[2]["volume"], 1);
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
async fn cancel_pending_keeps_unknown_exposure_without_starving_other_conditions() {
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
                origin: Default::default(),
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
                submitted_at: chrono::Utc::now().timestamp(),
                entry_fill_notional: 0.,
                action_at: chrono::Utc::now().timestamp() - 20, // 20s ago
                trading_day: "20261019".to_string(),
                note: "waiting for counter".into(),
                cancel_attempts: 3,
                funds_at_submit: None,
                entry_rules: None,
            },
        );
    }

    // Keep ambiguous exposure while allowing independent conditions to progress.
    engine.drive().await.unwrap();

    {
        let s = engine.state.lock().await;
        let o = s.orders.get("ref_stuck").unwrap();
        assert_eq!(o.phase, OrderPhase::Unknown);
        assert!(o.note.contains("撤单未确认"));
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
async fn narrow_stop_filter_changes_only_new_signals_and_freezes_old_conditions() {
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
    assert!(counter.inserts().is_empty());
    let old = engine
        .list_conditions()
        .await
        .into_iter()
        .find(|c| c.id == id)
        .unwrap();
    assert_eq!(
        old.frozen.as_ref().unwrap().execution.min_entry_risk_ticks,
        10
    );
    let new = engine
        .register_condition(
            Some(124),
            "PB0",
            "pb2611",
            Dir::Up,
            1000.0,
            995.0,
            1200.0,
            3.6,
            "2026-10-16 10:15:00",
        )
        .await
        .unwrap();
    quote(&engine, 1000.0).await;
    assert_eq!(counter.inserts().len(), 1);
    assert_ne!(new, id);
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
    let mut old = export_execution_state(&path).unwrap();
    old.as_object_mut().unwrap().remove("entry_funding_waits");
    for order in old["orders"].as_object_mut().unwrap().values_mut() {
        order.as_object_mut().unwrap().remove("funds_at_submit");
    }
    std::fs::remove_file(path.with_extension("sqlite3")).unwrap();
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

#[tokio::test]
async fn quote_gap_protects_live_exposure_without_reopening_historical_tasks() {
    let counter = Counter::new();
    let engine = counter.engine(ExecutionMode::FullAuto, 2);
    ready(&engine, vec![], vec![]).await;
    let id = register(&engine, 1000.0, 990.0).await;
    quote(&engine, 1000.0).await;
    let open = counter.inserts()[0].clone();
    ready(
        &engine,
        vec![order(&open, "all_traded", 2)],
        vec![trade(&open, "open", 2, 1000.0)],
    )
    .await;
    {
        let mut s = engine.state.lock().await;
        let mut historical = s.conditions[&id].clone();
        historical.id = "historical".into();
        historical.state = ConditionState::Closed;
        historical.note = "already closed".into();
        s.conditions.insert(historical.id.clone(), historical);
        let mut position = s.positions[&id].clone();
        position.id = "historical".into();
        position.is_closed = true;
        position.volume = 0;
        s.positions.insert(position.id.clone(), position);
        let mut historical_order = s.orders.values().next().unwrap().clone();
        historical_order.condition_id = "historical".into();
        s.orders
            .insert("historical".into(), historical_order.clone());
        let mut rejected = s.conditions[&id].clone();
        rejected.id = "rejected".into();
        rejected.state = ConditionState::Rejected;
        s.conditions.insert(rejected.id.clone(), rejected);
        historical_order.condition_id = "rejected".into();
        historical_order.phase = OrderPhase::Rejected;
        historical_order.filled = 0;
        historical_order.reported_filled = 0;
        s.orders.insert("rejected".into(), historical_order);
        // An orphaned archive entry must not panic the risk handler.
        let mut orphan = s.orders.values().next().unwrap().clone();
        orphan.condition_id = "missing".into();
        orphan.phase = OrderPhase::Unknown;
        s.orders.insert("orphan".into(), orphan);
    }
    engine
        .quote_gap_with_reason("offline regression")
        .await
        .unwrap();
    {
        let s = engine.state.lock().await;
        assert_eq!(s.closes.len(), 1);
        assert_eq!(s.closes[&id].reason, "market_gap");
        assert!(s.aborted_entries.contains(&id));
        assert!(!s.aborted_entries.contains("historical"));
        assert!(!s.aborted_entries.contains("rejected"));
        assert_eq!(s.conditions["historical"].state, ConditionState::Closed);
        assert_eq!(s.conditions["historical"].note, "already closed");
        assert_eq!(s.conditions["rejected"].state, ConditionState::Rejected);
        assert!(s.quotes.is_empty());
        assert!(s
            .market_error
            .as_ref()
            .unwrap()
            .contains("offline regression"));
        assert!(s.error.is_none());
    }
    // Recovery clears transport health but never releases the latched exit.
    engine.market_recovered().await;
    assert!(engine.state.lock().await.market_error.is_some());
    engine.drive().await.unwrap();
    assert_eq!(counter.inserts().len(), 1);
    age(&engine).await;
    quote(&engine, 1001.0).await;
    assert!(engine.execution_error().await.is_none());
    assert_eq!(engine.state.lock().await.closes[&id].reason, "market_gap");
    assert_eq!(counter.inserts().len(), 2);
    assert_eq!(counter.inserts()[1]["offset"], "auto");
    assert_eq!(counter.inserts()[1]["volume"], 2);
    drop(engine);
    let restored = counter.engine(ExecutionMode::FullAuto, 2);
    assert_eq!(restored.state.lock().await.closes[&id].reason, "market_gap");
}

#[tokio::test]
async fn quote_gap_keeps_terminal_orders_with_unreconciled_fills_protected() {
    let counter = Counter::new();
    let engine = counter.engine(ExecutionMode::FullAuto, 2);
    ready(&engine, vec![], vec![]).await;
    let id = register(&engine, 1000.0, 990.0).await;
    quote(&engine, 1000.0).await;
    let open = counter.inserts()[0].clone();
    ready(&engine, vec![order(&open, "canceled", 1)], vec![]).await;
    engine.quote_gap().await.unwrap();
    assert!(engine.state.lock().await.aborted_entries.contains(&id));
    ready(
        &engine,
        vec![order(&open, "canceled", 1)],
        vec![trade(&open, "late-fill", 1, 1000.0)],
    )
    .await;
    age(&engine).await;
    quote(&engine, 1001.0).await;
    assert_eq!(counter.inserts().len(), 2);
    assert_eq!(counter.inserts()[1]["volume"], 1);
    assert_eq!(counter.inserts()[1]["offset"], "auto");
}

#[tokio::test]
async fn quote_batch_keeps_an_intermediate_stop_and_persists_before_using_latest_book() {
    let counter = Counter::new();
    let engine = counter.engine(ExecutionMode::FullAuto, 2);
    ready(&engine, vec![], vec![]).await;
    let id = register(&engine, 1000.0, 990.0).await;
    quote(&engine, 1000.0).await;
    let open = counter.inserts()[0].clone();
    ready(
        &engine,
        vec![order(&open, "all_traded", 2)],
        vec![trade(&open, "open", 2, 1000.0)],
    )
    .await;
    age(&engine).await;
    let mut q = engine.state.lock().await.quotes["PB2611"].clone();
    q.last = 989.0;
    q.bid = 988.0;
    q.ask = 990.0;
    let first = ("PB2611".into(), q.clone());
    q.last = 1001.0;
    q.bid = 1000.0;
    q.ask = 1002.0;
    engine
        .on_quote_batch(vec![first, ("PB2611".into(), q)])
        .await
        .unwrap();
    assert_eq!(engine.state.lock().await.closes[&id].reason, "stop");
    assert_eq!(counter.inserts().len(), 2);
    assert_eq!(counter.inserts()[1]["offset"], "auto");
    assert!(counter.inserts()[1]["price"].as_f64().unwrap() >= 990.0);
    drop(engine);
    let restored = counter.engine(ExecutionMode::FullAuto, 2);
    assert_eq!(restored.state.lock().await.closes[&id].reason, "stop");
}

#[tokio::test]
async fn queued_expired_stop_is_persisted_and_waits_for_the_rebounded_book() {
    let counter = Counter::new();
    let now = chrono::Utc::now().timestamp();
    let clock = Arc::new(ReplayClock::new(now));
    let engine = counter.engine(ExecutionMode::FullAuto, 2).with_clock(clock.clone());
    ready(&engine, vec![], vec![]).await;
    let id = register(&engine, 1000.0, 990.0).await;
    quote(&engine, 1000.0).await;
    let open = counter.inserts()[0].clone();
    ready(&engine, vec![order(&open, "all_traded", 2)], vec![trade(&open, "open", 2, 1000.0)]).await;
    let mut book = {
        let mut state = engine.state.lock().await;
        state.positions.get_mut(&id).unwrap().open_ts = super::super::candidates::label(now - 3);
        state.quotes["PB2611"].clone()
    };
    book.timestamp = now + 1;
    book.last = 989.0;
    book.bid = 988.0;
    book.ask = 990.0;
    let (tx, mut rx) = tokio::sync::mpsc::channel(64);
    tx.try_send(ReceivedExecutionQuote {
        symbol: "PB2611".into(), quote: book.clone(), received_at: now + 1,
    }).unwrap();
    assert_eq!(tx.capacity(), 63);

    clock.advance(now + 20).unwrap();
    engine.on_received_quote_batch(vec![rx.recv().await.unwrap()]).await.unwrap();
    let persisted = export_execution_state(&counter.directory.join("trader_execution.json")).unwrap();
    assert_eq!(persisted["closes"][&id]["reason"], "stop");
    assert_eq!(counter.inserts().len(), 1);
    assert_eq!(engine.state.lock().await.quotes["PB2611"].last, 1000.0);

    book.timestamp = now + 20;
    book.last = 1001.0;
    book.bid = 1000.0;
    book.ask = 1002.0;
    engine.on_received_quote_batch(vec![ReceivedExecutionQuote {
        symbol: "PB2611".into(), quote: book, received_at: now + 20,
    }]).await.unwrap();
    let orders = counter.inserts();
    assert_eq!(orders.len(), 2);
    assert_eq!(orders[1]["offset"], "auto");
    assert!(orders[1]["price"].as_f64().unwrap() >= 990.0);
}

#[tokio::test]
async fn late_protection_requires_a_valid_receipt_and_prior_position_exposure() {
    let counter = Counter::new();
    let now = chrono::Utc::now().timestamp();
    let clock = Arc::new(ReplayClock::new(now));
    let engine = counter.engine(ExecutionMode::FullAuto, 2).with_clock(clock.clone());
    ready(&engine, vec![], vec![]).await;
    let id = register(&engine, 1000.0, 990.0).await;
    quote(&engine, 1000.0).await;
    let open = counter.inserts()[0].clone();
    ready(&engine, vec![order(&open, "all_traded", 2)], vec![trade(&open, "open", 2, 1000.0)]).await;
    let mut book = {
        let mut state = engine.state.lock().await;
        state.positions.get_mut(&id).unwrap().open_ts = super::super::candidates::label(now + 2);
        state.quotes.remove("PB2611").unwrap()
    };
    book.last = 989.0;
    book.timestamp = now + 1;
    clock.advance(now + 25).unwrap();
    let before_fill = ReceivedExecutionQuote {
        symbol: "PB2611".into(), quote: book.clone(), received_at: now + 1,
    };
    book.timestamp = now + 3;
    let stale_on_arrival = ReceivedExecutionQuote {
        symbol: "PB2611".into(), quote: book, received_at: now + 24,
    };
    engine.on_received_quote_batch(vec![before_fill, stale_on_arrival]).await.unwrap();
    assert!(!engine.state.lock().await.closes.contains_key(&id));
    assert_eq!(counter.inserts().len(), 1);
}

#[tokio::test]
async fn signal_interleaved_with_configuration_freezes_the_committed_mode() {
    let counter = Counter::new();
    let engine = counter.engine(ExecutionMode::FullAuto, 2);
    ready(&engine, vec![], vec![]).await;
    let definition = engine.definition().await;
    let mut cfg = engine.get_config().await;
    cfg.mode = ExecutionMode::SemiAuto;
    let identity = engine.identity.write().await;
    let mut pending = Box::pin(engine.dispatch(StrategyEvent::Signal {
        source: N_STRATEGY.into(),
        payload: json!({
            "signal_id":"config-interleaving", "symbol":"PB0", "instrument_id":"pb2611",
            "direction":"up", "entry":1000.0, "stop":990.0, "target":1020.0,
            "entry_score":4.0, "warning_ts":"2026-10-16 10:00:00"
        }),
    }));
    assert!(futures_util::poll!(&mut pending).is_pending());
    engine.configure_strategy(definition, cfg).await.unwrap();
    let committed_revision = engine.definition().await.revision;
    drop(identity);
    pending.await.unwrap();

    let conditions = engine.list_conditions().await;
    assert_eq!(conditions.len(), 1);
    let frozen = conditions[0].frozen.as_ref().unwrap();
    assert_eq!(frozen.definition.revision, committed_revision);
    assert_eq!(frozen.execution.mode, ExecutionMode::SemiAuto);
    quote(&engine, 1000.0).await;
    assert!(counter.inserts().is_empty());
}

#[tokio::test]
async fn batched_observation_persists_a_stop_immediately_before_the_batch_finishes() {
    let counter = Counter::new();
    let engine = counter.engine(ExecutionMode::FullAuto, 2);
    ready(&engine, vec![], vec![]).await;
    let id = register(&engine, 1000.0, 990.0).await;
    quote(&engine, 1000.0).await;
    let open = counter.inserts()[0].clone();
    ready(
        &engine,
        vec![order(&open, "all_traded", 2)],
        vec![trade(&open, "open", 2, 1000.0)],
    )
    .await;
    let mut q = engine.state.lock().await.quotes["PB2611"].clone();
    q.last = 989.0;
    q.bid = 988.0;
    q.ask = 990.0;
    engine
        .observe_quotes([("PB2611".into(), q)].into_iter().collect(), false)
        .await
        .unwrap();
    let persisted: ExecutionState = serde_json::from_value(
        export_execution_state(&counter.directory.join("trader_execution.json")).unwrap(),
    ).unwrap();
    assert_eq!(persisted.closes[&id].reason, "stop");
    assert!(persisted.aborted_entries.contains(&id));
    assert_eq!(counter.inserts().len(), 1);
}

#[tokio::test]
async fn repeated_stop_and_unchanged_quotes_do_not_rewrite_large_state() {
    let counter = Counter::new();
    let engine = counter.engine(ExecutionMode::FullAuto, 2);
    ready(&engine, vec![], vec![]).await;
    let id = register(&engine, 1000.0, 990.0).await;
    quote(&engine, 1000.0).await;
    let open = counter.inserts()[0].clone();
    ready(
        &engine,
        vec![order(&open, "all_traded", 2)],
        vec![trade(&open, "open", 2, 1000.0)],
    )
    .await;
    let mut q = engine.state.lock().await.quotes["PB2611"].clone();
    q.last = 989.0;
    q.bid = 988.0;
    q.ask = 990.0;
    engine
        .observe_quotes([("PB2611".into(), q.clone())].into(), false)
        .await
        .unwrap();
    {
        let mut s = engine.state.lock().await;
        s.broker_ready = false;
        let mut rejected = s.orders.values().next().unwrap().clone();
        rejected.phase = OrderPhase::Rejected;
        rejected.condition_id = "historical-condition".into();
        rejected.filled = 0;
        rejected.reported_filled = 0;
        rejected.note = "historical rejection ".repeat(35);
        for index in 0..8800 {
            s.orders
                .insert(format!("historical-{index}"), rejected.clone());
        }
        engine.save(&mut s).unwrap();
    }
    let path = counter.directory.join("trader_execution.json");
    let disk = export_execution_state(&path).unwrap();
    assert!(serde_json::to_vec(&disk).unwrap().len() > 8_000_000);
    let db = rusqlite::Connection::open(path.with_extension("sqlite3")).unwrap();
    db.execute_batch(
        "CREATE TRIGGER keep_archived_orders BEFORE UPDATE ON execution_orders
         WHEN old.reference LIKE 'historical-%'
         BEGIN SELECT RAISE(ABORT, 'archive touched'); END;
         CREATE TRIGGER keep_archived_inserts BEFORE INSERT ON execution_orders
         WHEN new.reference LIKE 'historical-%'
         BEGIN SELECT RAISE(ABORT, 'archive written'); END;",
    ).unwrap();
    {
        let mut state = engine.state.lock().await;
        state.orders.get_mut(open["order_ref"].as_str().unwrap()).unwrap().note = "updated".into();
        state.last_bars.insert("PB0".into(), "2026-10-20 10:00:00".into());
        engine.save(&mut state).unwrap();
    }
    engine.store.lock().unwrap().as_ref().unwrap().set_read_only();
    engine
        .on_quote_batch(vec![("PB2611".into(), q); 40])
        .await
        .unwrap();
    let s = engine.state.lock().await;
    assert_eq!(s.closes[&id].reason, "stop");
    assert_eq!(s.orders.len(), 8801);
    assert_eq!(counter.inserts().len(), 1);
}

#[tokio::test]
async fn seat_rejected_exit_survives_restart_and_waits_for_new_order_sync() {
    let counter = Counter::new();
    let now = chrono::Utc::now().timestamp();
    let clock = Arc::new(crate::trader::execution::ReplayClock::new(now));
    let engine = counter
        .engine(ExecutionMode::FullAuto, 2)
        .with_clock(clock.clone());
    ready(&engine, vec![], vec![]).await;
    let id = register(&engine, 1000.0, 990.0).await;
    quote(&engine, 1000.0).await;
    let open = counter.inserts()[0].clone();
    let opened = order(&open, "all_traded", 2);
    let fill = trade(&open, "open", 2, 1000.0);
    ready(&engine, vec![opened.clone()], vec![fill.clone()]).await;
    let mut stopped = engine.state.lock().await.quotes["PB2611"].clone();
    stopped.timestamp = now;
    stopped.last = 989.0;
    stopped.bid = 988.0;
    stopped.ask = 990.0;
    engine
        .on_quote_batch(vec![("PB2611".into(), stopped.clone())])
        .await
        .unwrap();
    let mut rejected = order(&counter.inserts()[1], "rejected", 0);
    rejected["status_msg"] = json!("交易所拒绝报单 [21]: CTP:该交易席位未连接到交易所");
    ready(
        &engine,
        vec![opened.clone(), rejected.clone()],
        vec![fill.clone()],
    )
    .await;
    drop(engine);

    let engine = counter
        .engine(ExecutionMode::FullAuto, 2)
        .with_clock(clock.clone());
    ready(
        &engine,
        vec![opened.clone(), rejected.clone()],
        vec![fill.clone()],
    )
    .await;
    let synced_snapshot = |at: i64| {
        json!({
            "bridge_online":true, "health":{
                "ready":true, "logged_in":true, "sync_pending":false, "trading_day":"20261016",
                "broker_id":"fake", "user_id":"offline-test",
                "orders_updated_at":at
            },
            "orders":[opened.clone(), rejected.clone()], "trades":[fill.clone()]
        })
    };
    clock.advance(now + 5).unwrap();
    stopped.timestamp = now + 5;
    engine.reconcile(&synced_snapshot(now + 5)).await.unwrap();
    engine
        .on_quote_batch(vec![("PB2611".into(), stopped.clone()); 5])
        .await
        .unwrap();
    assert_eq!(counter.inserts().len(), 2);
    clock.advance(now + 16).unwrap();
    stopped.timestamp = now + 16;
    engine.reconcile(&synced_snapshot(now)).await.unwrap();
    engine
        .on_quote_batch(vec![("PB2611".into(), stopped.clone()); 5])
        .await
        .unwrap();
    assert_eq!(counter.inserts().len(), 2);
    assert_eq!(engine.state.lock().await.closes[&id].reason, "stop");
    engine.reconcile(&synced_snapshot(now + 16)).await.unwrap();
    engine
        .on_quote_batch(vec![("PB2611".into(), stopped); 5])
        .await
        .unwrap();
    assert_eq!(counter.inserts().len(), 3);
    assert_eq!(counter.inserts()[2]["offset"], "auto");
}

#[tokio::test]
async fn price_rejected_entry_waits_for_a_fresh_different_limit_price() {
    let counter = Counter::new();
    let now = chrono::Utc::now().timestamp();
    let clock = Arc::new(crate::trader::execution::ReplayClock::new(now));
    let engine = counter
        .engine(ExecutionMode::FullAuto, 2)
        .with_clock(clock.clone());
    ready(&engine, vec![], vec![]).await;
    register(&engine, 1000.0, 990.0).await;
    quote(&engine, 1000.0).await;
    let mut rejected = order(&counter.inserts()[0], "rejected", 0);
    rejected["status_msg"] = json!("交易所拒绝报单 [163]: CTP:报单价格不在涨跌停板价范围内");
    ready(&engine, vec![rejected.clone()], vec![]).await;
    let mut q = engine.state.lock().await.quotes["PB2611"].clone();
    clock.advance(now + 3).unwrap();
    q.timestamp = now + 3;
    engine
        .on_quote_batch(vec![("PB2611".into(), q.clone()); 10])
        .await
        .unwrap();
    assert_eq!(counter.inserts().len(), 1);
    q.last = 998.0;
    q.bid = 997.0;
    q.ask = 999.0;
    engine
        .on_quote_batch(vec![("PB2611".into(), q.clone())])
        .await
        .unwrap();
    assert_eq!(counter.inserts().len(), 2);
    assert_ne!(counter.inserts()[0]["price"], counter.inserts()[1]["price"]);
    ready(&engine, vec![rejected], vec![]).await;
    engine
        .on_quote_batch(vec![("PB2611".into(), q)])
        .await
        .unwrap();
    assert_eq!(counter.inserts().len(), 2);
}

#[tokio::test]
async fn snapshot_reuses_history_and_refreshes_only_changed_fields() {
    let counter = Counter::new();
    let engine = counter.engine(ExecutionMode::FullAuto, 2);
    let id = register(&engine, 1000.0, 990.0).await;
    let mut cache = ExecutionSnapshotCache::default();
    let first = engine.snapshot_fields(&mut cache, true).await.unwrap();
    let second = engine.snapshot_fields(&mut cache, true).await.unwrap();
    for key in ["history", "conditions", "statistics", "managed_positions"] {
        assert!(
            std::ptr::eq(first.get(key).unwrap(), second.get(key).unwrap()),
            "{key}"
        );
    }
    engine
        .state
        .lock()
        .await
        .conditions
        .get_mut(&id)
        .unwrap()
        .note = "updated".into();
    let next = engine.snapshot_fields(&mut cache, true).await.unwrap();
    assert!(!std::ptr::eq(
        first.get("conditions").unwrap(),
        next.get("conditions").unwrap()
    ));
    assert_eq!(next.to_value()["conditions"][0]["note"], "updated");
    assert!(std::ptr::eq(
        first.get("history").unwrap(),
        next.get("history").unwrap()
    ));
    assert!(std::ptr::eq(
        first.get("statistics").unwrap(),
        next.get("statistics").unwrap()
    ));
}

#[test]
fn price_rejected_protective_exit_can_recheck_a_pinned_book_after_sync() {
    let wait = execution_safety::RejectionWait::Price {
        submitted_at: 100,
        rejected_price: 500.0,
        rejected_trading_day: "20261123".into(),
        is_exit: true,
    };
    let mut q = ExecutionQuote {
        instrument_id: "pb2611".into(),
        exchange_id: "SHFE".into(),
        underlying_symbol: "PB0".into(),
        last: 500.0,
        bid: 500.0,
        ask: 501.0,
        tick: 1.0,
        lower: 500.0,
        upper: 2000.0,
        timestamp: 105,
    };
    assert!(wait.waiting(&q, 500.0, 105, 103.0, "20261123"));
    q.timestamp = 117;
    assert!(wait.waiting(&q, 500.0, 117, 100.0, "20261123"));
    assert!(!wait.waiting(&q, 500.0, 117, 112.0, "20261123"));
}

#[tokio::test]
async fn quote_gap_health_waits_for_fresh_data_without_clearing_broker_errors() {
    let counter = Counter::new();
    let engine = counter.engine(ExecutionMode::MonitorOnly, 2);
    quote(&engine, 1000.0).await;
    let mut q = engine.state.lock().await.quotes["PB2611"].clone();
    engine.quote_gap().await.unwrap();
    q.timestamp -= 60;
    engine
        .on_quotes([("PB2611".into(), q.clone())].into_iter().collect())
        .await
        .unwrap();
    assert!(engine.state.lock().await.market_error.is_some());
    engine.state.lock().await.error = Some("broker mismatch".into());
    q.timestamp = chrono::Utc::now().timestamp();
    engine
        .on_quotes([("PB2611".into(), q)].into_iter().collect())
        .await
        .unwrap();
    assert!(engine.state.lock().await.market_error.is_none());
    assert_eq!(
        engine.execution_error().await.as_deref(),
        Some("broker mismatch")
    );
}

#[tokio::test]
async fn idle_transport_recovery_works_for_n_p2_and_b5_without_clearing_other_errors() {
    use crate::trader::{b5, candidates};
    for definition in [
        StrategyDefinition::default(),
        candidates::definition(candidates::P2, candidates::Parameters::reference_snapshot()),
        b5::definition(),
    ] {
        let counter = Counter::new();
        let engine = counter.engine(ExecutionMode::FullAuto, 2);
        engine.set_definition(definition).await.unwrap();
        engine.pause(true).await.unwrap();
        engine.market_unavailable().await;
        assert!(engine.execution_error().await.is_some());
        engine.market_recovered().await; // healthy heartbeat, no quotes at all
        assert!(engine.execution_error().await.is_none());
        {
            let mut s = engine.state.lock().await;
            assert!(s.paused);
            assert!(s.quotes.is_empty());
            s.error = Some("broker mismatch".into());
            s.persistence_error = Some("disk full".into());
            s.strategy_error = Some("strategy failed".into());
        }
        engine.market_recovered().await;
        let s = engine.state.lock().await;
        assert_eq!(s.error.as_deref(), Some("broker mismatch"));
        assert_eq!(s.persistence_error.as_deref(), Some("disk full"));
        assert_eq!(s.strategy_error.as_deref(), Some("strategy failed"));
        assert!(counter.inserts().is_empty());
    }
}

#[tokio::test]
async fn recovered_transport_cannot_submit_using_a_closed_session_book() {
    let counter = Counter::new();
    let engine = counter.engine(ExecutionMode::FullAuto, 2);
    ready(&engine, vec![], vec![]).await;
    register(&engine, 1000.0, 990.0).await;
    // Cache a valid book without driving, then simulate a transport failure.
    let mut q = ExecutionQuote {
        instrument_id: "pb2611".into(),
        exchange_id: "SHFE".into(),
        underlying_symbol: String::new(),
        last: 1000.,
        bid: 999.,
        ask: 1001.,
        tick: 1.,
        lower: 500.,
        upper: 2000.,
        timestamp: chrono::Utc::now().timestamp(),
    };
    engine
        .on_quotes([("PB0".into(), q.clone())].into_iter().collect())
        .await
        .unwrap();
    engine.market_unavailable().await;
    engine.market_recovered().await;
    engine.drive().await.unwrap();
    assert!(counter.inserts().is_empty());
    q.timestamp -= 60;
    engine
        .on_quote_batch(vec![("PB0".into(), q.clone())])
        .await
        .unwrap();
    assert!(counter.inserts().is_empty());
    assert!(engine.execution_error().await.is_none());
    q.timestamp = chrono::Utc::now().timestamp();
    engine
        .on_quote_batch(vec![("PB0".into(), q)])
        .await
        .unwrap();
    assert_eq!(counter.inserts().len(), 1);
}

#[tokio::test]
async fn strategy_quote_callback_sees_current_alias_book_on_first_tick_and_roll() {
    struct AliasStrategy;
    impl Strategy for AliasStrategy {
        fn on_event(
            &self,
            event: &StrategyEvent,
            c: &StrategyContext<'_>,
            state: &mut Value,
        ) -> Result<Vec<TradeIntent>> {
            if let StrategyEvent::Quote(q) = event {
                let alias = &c.quotes["PB0"];
                assert_eq!(alias.instrument_id, q.instrument_id);
                assert_eq!(alias.last, q.last);
                *state = json!({"seen": q.instrument_id});
            }
            Ok(vec![])
        }
    }
    let counter = Counter::new();
    let mut engine = counter.engine(ExecutionMode::MonitorOnly, 2);
    let mut registry = StrategyRegistry::default();
    registry.register_strategy(N_STRATEGY, N_VERSION, |_| Ok(Box::new(AliasStrategy)));
    engine.registry = Arc::new(registry);
    for (instrument, price) in [("pb2611", 1000.), ("pb2612", 1100.)] {
        let q = ExecutionQuote {
            instrument_id: instrument.into(),
            exchange_id: "SHFE".into(),
            underlying_symbol: format!("SHFE.{instrument}"),
            last: price,
            bid: price - 1.,
            ask: price + 1.,
            tick: 1.,
            lower: 500.,
            upper: 2000.,
            timestamp: chrono::Utc::now().timestamp(),
        };
        engine
            .on_quote_batch(vec![("PB0".into(), q)])
            .await
            .unwrap();
        let s = engine.state.lock().await;
        assert!(s.strategy_error.is_none());
        assert_eq!(
            s.strategy_states[&s.definition.revision]["seen"],
            instrument
        );
    }
}

#[test]
fn frozen_plan_arc_roundtrip_and_shared_pointer_on_clone() {
    let plan = EntryPlan {
        signal_id: "sig_123".into(),
        symbol: "PB0".into(),
        instrument_id: "pb2611".into(),
        direction: Direction::Long,
        reference_price: 16500.0,
        trigger: EntryTrigger::Breakout { price: 16500.0 },
        stop_price: Some(16400.0),
        target_price: Some(16700.0),
        risk_per_unit: 100.0,
        requested_volume: 2,
        created_at: chrono::Utc::now().timestamp(),
        expires_at: Some(chrono::Utc::now().timestamp() + 600),
        reason: "breakout_setup".into(),
        evidence: serde_json::json!({
            "b5": { "cluster": "PB0|up|a|b", "score": 3.8 },
            "nested_tree": { "key": "value", "list": [1, 2, 3] }
        }),
    };

    let original = ConditionOrder {
        frozen: Some(Arc::new(FrozenPlan {
            definition: StrategyDefinition::default(),
            execution: ExecutionConfig::default(),
            plan,
        })),
        origin: TradeOrigin {
            strategy_id: "n_pattern_v1".into(),
            strategy_version: "1".into(),
            signal_id: "sig_123".into(),
            ..Default::default()
        },
        id: "cond_test_1".into(),
        event_id: Some(1001),
        symbol: "PB0".into(),
        instrument_id: "pb2611".into(),
        direction: "up".into(),
        tier: PositionTier::Large,
        entry_price: 16500.0,
        stop_price: 16400.0,
        target_price: 16700.0,
        risk: 100.0,
        score: 3.8,
        volume: 2,
        warning_bar_ts: "2026-11-05 14:00:00".into(),
        trigger_ts: None,
        bars_waited: 0,
        max_wait_bars: 12,
        state: ConditionState::Pending,
        created_at: chrono::Utc::now().timestamp(),
        note: "initial_setup".into(),
    };

    // 1. JSON 往返兼容性测试 (Serialize -> JSON string -> Deserialize into Arc<FrozenPlan>)
    let json_str = serde_json::to_string_pretty(&original).expect("Serialization failed");
    let deserialized: ConditionOrder =
        serde_json::from_str(&json_str).expect("Deserialization failed");

    // 验证所有字段精确反序列化回结构体中
    assert!(deserialized.frozen.is_some());
    let frozen_deser = deserialized.frozen.as_ref().unwrap();
    assert_eq!(frozen_deser.plan.signal_id, "sig_123");
    assert_eq!(frozen_deser.plan.evidence["b5"]["cluster"], "PB0|up|a|b");
    assert_eq!(frozen_deser.plan.evidence["nested_tree"]["key"], "value");
    assert_eq!(deserialized.id, "cond_test_1");
    assert_eq!(deserialized.entry_price, 16500.0);

    // 再次序列化回写并比对 JSON Value 等价性
    let reserialized_json: serde_json::Value =
        serde_json::to_value(&deserialized).expect("Re-serialization failed");
    let original_json: serde_json::Value =
        serde_json::to_value(&original).expect("Original serialization failed");
    assert_eq!(
        reserialized_json, original_json,
        "JSON 往返数据结构必须保持完全一致"
    );

    // 2. 验证指针共享 (Arc::ptr_eq on clone)
    let mut cloned = original.clone();
    assert!(
        Arc::ptr_eq(
            original.frozen.as_ref().unwrap(),
            cloned.frozen.as_ref().unwrap()
        ),
        "ConditionOrder clone 时 FrozenPlan 必须通过 Arc 共享，不得深拷贝"
    );

    // 3. 验证独立修改非冻结字段互不干扰
    cloned.state = ConditionState::Triggered;
    cloned.note = "triggered_now".into();
    cloned.trigger_ts = Some("2026-11-05 14:05:00".into());

    assert_eq!(original.state, ConditionState::Pending);
    assert_eq!(original.note, "initial_setup");
    assert_eq!(original.trigger_ts, None);

    assert_eq!(cloned.state, ConditionState::Triggered);
    assert_eq!(cloned.note, "triggered_now");
    assert_eq!(cloned.trigger_ts, Some("2026-11-05 14:05:00".into()));

    // 验证修改后指针依然共享
    assert!(Arc::ptr_eq(
        original.frozen.as_ref().unwrap(),
        cloned.frozen.as_ref().unwrap()
    ));
}

#[test]
fn incremental_indices_match_full_table_scan_across_lifecycle() {
    fn check_consistency(s: &ExecutionState) {
        // 1. started_entries
        let expected_started: HashSet<String> = s
            .orders
            .values()
            .filter(|o| o.reason.is_none())
            .map(|o| o.condition_id.clone())
            .collect();
        for cond_id in s.conditions.keys() {
            assert_eq!(
                s.is_entry_started(cond_id),
                expected_started.contains(cond_id),
                "is_entry_started mismatch for condition {}",
                cond_id
            );
        }

        // 2. exposed_entries
        let expected_exposed: HashSet<String> = s
            .orders
            .values()
            .filter(|o| {
                o.reason.is_none()
                    && (!matches!(o.phase, OrderPhase::Terminal | OrderPhase::Rejected)
                        || o.filled < o.reported_filled)
            })
            .map(|o| o.condition_id.clone())
            .chain(
                s.positions
                    .values()
                    .filter(|p| p.volume > 0)
                    .map(|p| p.id.clone()),
            )
            .collect();
        for cond_id in s.conditions.keys() {
            assert_eq!(
                s.is_entry_exposed(cond_id),
                expected_exposed.contains(cond_id),
                "is_entry_exposed mismatch for condition {}",
                cond_id
            );
        }

        // 3. blocked_orders
        let is_blocked_full_scan = |cond_id: &str| -> bool {
            s.orders.values().any(|o| {
                o.condition_id == cond_id
                    && (!matches!(o.phase, OrderPhase::Terminal | OrderPhase::Rejected)
                        || o.filled < o.reported_filled)
            })
        };
        for cond_id in s.conditions.keys() {
            assert_eq!(
                s.is_order_blocked(cond_id),
                is_blocked_full_scan(cond_id),
                "is_order_blocked mismatch for condition {}",
                cond_id
            );
        }

        // 4. Verify that s.rebuild_indices() does not mutate state when incremental was used
        let mut cloned = s.clone();
        cloned.rebuild_indices();
        assert_eq!(
            s.entry_started_conditions, cloned.entry_started_conditions,
            "Incremental started set differs from rebuilt"
        );
        assert_eq!(
            s.unsettled_orders, cloned.unsettled_orders,
            "Incremental unsettled set differs from rebuilt"
        );
    }

    let mut state = ExecutionState::default();

    // Add 10 mock conditions in various states
    for i in 1..=10 {
        let id = format!("cond_{i}");
        state.conditions.insert(
            id.clone(),
            ConditionOrder {
                frozen: None,
                origin: TradeOrigin::default(),
                id: id.clone(),
                event_id: Some(i),
                symbol: "PB0".into(),
                instrument_id: "pb2611".into(),
                direction: if i % 2 == 0 { "up" } else { "down" }.into(),
                tier: PositionTier::Small,
                entry_price: 15000.0,
                stop_price: 14900.0,
                target_price: 15200.0,
                risk: 100.0,
                score: 3.0,
                volume: 1,
                warning_bar_ts: "2026-11-05 10:00:00".into(),
                trigger_ts: None,
                bars_waited: 0,
                max_wait_bars: 10,
                state: match i {
                    1 => ConditionState::Pending,
                    2 => ConditionState::WaitingConfirm,
                    3 => ConditionState::Triggered,
                    4 => ConditionState::Working,
                    5 => ConditionState::PartFilled,
                    6 => ConditionState::Filled,
                    7 => ConditionState::Expired,
                    8 => ConditionState::Canceled,
                    9 => ConditionState::Invalidated,
                    _ => ConditionState::Closed,
                },
                created_at: 1000,
                note: "test".into(),
            },
        );
    }

    state.rebuild_indices();
    check_consistency(&state);

    // Lifecycle Step 1: Submit Entry Order for cond_1
    let order_ref_1 = "order_1".to_string();
    state.orders.insert(
        order_ref_1.clone(),
        ExecutionOrder {
            origin: TradeOrigin::default(),
            request: OrderInsertRequest {
                instrument_id: "pb2611".into(),
                exchange_id: Some("SHFE".into()),
                direction: "buy".into(),
                offset: "open".into(),
                price: 15000.0,
                volume: 1,
                order_ref: Some(order_ref_1.clone()),
            },
            condition_id: "cond_1".into(),
            reason: None,
            phase: OrderPhase::Unknown,
            reported_filled: 0,
            filled: 0,
            submitted_at: chrono::Utc::now().timestamp(),
            entry_fill_notional: 0.,
            action_at: 1001,
            trading_day: "20261105".into(),
            note: "".into(),
            cancel_attempts: 0,
            funds_at_submit: None,
            entry_rules: None,
        },
    );
    state.record_order_update(&order_ref_1);
    check_consistency(&state);

    // Lifecycle Step 2: Order transitions to Working
    if let Some(o) = state.orders.get_mut(&order_ref_1) {
        o.phase = OrderPhase::Working;
    }
    state.record_order_update(&order_ref_1);
    check_consistency(&state);

    // Lifecycle Step 3: Counter reports partial fill, not locally aligned yet (filled=0, reported=1)
    if let Some(o) = state.orders.get_mut(&order_ref_1) {
        o.reported_filled = 1;
    }
    state.record_order_update(&order_ref_1);
    check_consistency(&state);

    // Lifecycle Step 4: Reconciled trade aligns fill (filled=1, reported=1, phase=Terminal)
    if let Some(o) = state.orders.get_mut(&order_ref_1) {
        o.filled = 1;
        o.phase = OrderPhase::Terminal;
    }
    state.record_order_update(&order_ref_1);
    check_consistency(&state);
    assert!(state.is_entry_started("cond_1"));
    assert!(!state.is_entry_exposed("cond_1"));

    // Lifecycle Step 5: Position opens for cond_1
    state.positions.insert(
        "cond_1".into(),
        ManagedPosition {
            origin: TradeOrigin::default(),
            id: "cond_1".into(),
            condition_order_id: "cond_1".into(),
            symbol: "PB0".into(),
            instrument_id: "pb2611".into(),
            direction: "up".into(),
            tier: PositionTier::Small,
            fill_price: 15000.0,
            initial_stop_price: 14900.0,
            target_price: 15200.0,
            risk: 100.0,
            volume: 1,
            open_ts: "2026-11-05 10:05:00".into(),
            bars_held: 1,
            max_mfe_r: 0.5,
            max_mae_r: -0.1,
            trailing_grade: 0,
            current_stop_price: 14900.0,
            lot1_closed: false,
            exit_reason: None,
            exit_price: None,
            is_closed: false,
            realized_pnl_pts: None,
            r_multiple: None,
            event_id: Some(1),
            planned_entry_price: Some(15000.0),
            score: Some(3.0),
            warning_bar_ts: Some("2026-11-05 10:00:00".into()),
            trigger_ts: None,
            order_insert_ts: None,
            is_manual: false,
            user_note: None,
        },
    );
    check_consistency(&state);
    assert!(state.is_entry_exposed("cond_1"));

    // Lifecycle Step 6: Submit Exit Order (reason is Some)
    let exit_ref = "order_exit_1".to_string();
    state.orders.insert(
        exit_ref.clone(),
        ExecutionOrder {
            origin: TradeOrigin::default(),
            request: OrderInsertRequest {
                instrument_id: "pb2611".into(),
                exchange_id: Some("SHFE".into()),
                direction: "sell".into(),
                offset: "close".into(),
                price: 14900.0,
                volume: 1,
                order_ref: Some(exit_ref.clone()),
            },
            condition_id: "cond_1".into(),
            reason: Some("stop".into()),
            phase: OrderPhase::Working,
            reported_filled: 0,
            filled: 0,
            submitted_at: chrono::Utc::now().timestamp(),
            entry_fill_notional: 0.,
            action_at: 1010,
            trading_day: "20261105".into(),
            note: "".into(),
            cancel_attempts: 0,
            funds_at_submit: None,
            entry_rules: None,
        },
    );
    state.record_order_update(&exit_ref);
    check_consistency(&state);

    // Lifecycle Step 7: Close position and exit order terminates
    if let Some(pos) = state.positions.get_mut("cond_1") {
        pos.volume = 0;
        pos.is_closed = true;
    }
    if let Some(exit_ord) = state.orders.get_mut(&exit_ref) {
        exit_ord.filled = 1;
        exit_ord.reported_filled = 1;
        exit_ord.phase = OrderPhase::Terminal;
    }
    state.record_order_update(&exit_ref);
    check_consistency(&state);
    assert!(state.is_entry_started("cond_1"));
    assert!(!state.is_entry_exposed("cond_1"));

    // Lifecycle Step 8: Cancel pending order on cond_2
    let order_ref_2 = "order_2".to_string();
    state.orders.insert(
        order_ref_2.clone(),
        ExecutionOrder {
            origin: TradeOrigin::default(),
            request: OrderInsertRequest {
                instrument_id: "pb2611".into(),
                exchange_id: Some("SHFE".into()),
                direction: "sell".into(),
                offset: "open".into(),
                price: 15000.0,
                volume: 1,
                order_ref: Some(order_ref_2.clone()),
            },
            condition_id: "cond_2".into(),
            reason: None,
            phase: OrderPhase::CancelPending,
            reported_filled: 0,
            filled: 0,
            submitted_at: chrono::Utc::now().timestamp(),
            entry_fill_notional: 0.,
            action_at: 1020,
            trading_day: "20261105".into(),
            note: "".into(),
            cancel_attempts: 1,
            funds_at_submit: None,
            entry_rules: None,
        },
    );
    state.record_order_update(&order_ref_2);
    check_consistency(&state);
    assert!(state.is_entry_started("cond_2"));
    assert!(state.is_entry_exposed("cond_2"));
    assert!(state.is_order_blocked("cond_2"));

    // Reject order on cond_2
    if let Some(o) = state.orders.get_mut(&order_ref_2) {
        o.phase = OrderPhase::Rejected;
    }
    state.record_order_update(&order_ref_2);
    check_consistency(&state);
    assert!(state.is_entry_started("cond_2"));
    assert!(!state.is_entry_exposed("cond_2"));
    assert!(!state.is_order_blocked("cond_2"));
}

#[tokio::test]

async fn quote_context_filters_terminal_history_but_timer_preserves_all() {
    #[derive(Clone)]

    struct ContextCheckingStrategy {
        observed: Arc<StdMutex<Vec<(String, usize, usize)>>>,
    }

    impl Strategy for ContextCheckingStrategy {
        fn on_event(
            &self,

            event: &StrategyEvent,

            c: &StrategyContext<'_>,

            _state: &mut Value,
        ) -> Result<Vec<TradeIntent>> {
            match event {
                StrategyEvent::Quote(_) => {
                    self.observed.lock().unwrap().push((
                        "quote".into(),
                        c.orders.len(),
                        c.conditions.len(),
                    ));
                }

                StrategyEvent::Timer { .. } => {
                    self.observed.lock().unwrap().push((
                        "timer".into(),
                        c.orders.len(),
                        c.conditions.len(),
                    ));
                }

                _ => {}
            }

            Ok(vec![])
        }
    }

    let observed = Arc::new(StdMutex::new(Vec::new()));

    let strat = ContextCheckingStrategy {
        observed: observed.clone(),
    };

    let counter = Counter::new();

    let mut engine = counter.engine(ExecutionMode::MonitorOnly, 2);

    let mut registry = StrategyRegistry::default();

    let strat_clone = strat.clone();

    registry.register_strategy(N_STRATEGY, N_VERSION, move |_| {
        Ok(Box::new(strat_clone.clone()))
    });

    engine.registry = Arc::new(registry);

    let revision = engine.definition().await.revision;

    {
        let mut s = engine.state.lock().await;

        // 1 active condition (Pending)

        let active_cond = ConditionOrder {
            frozen: None,

            origin: TradeOrigin {
                account_id: "default".into(),

                instance_id: "inst".into(),

                strategy_id: N_STRATEGY.into(),

                strategy_version: revision.clone(),

                signal_id: "sig_active".into(),
            },

            id: "cond_active".into(),

            event_id: Some(1),

            symbol: "PB0".into(),

            instrument_id: "pb2611".into(),

            direction: "up".into(),

            tier: PositionTier::Small,

            entry_price: 15000.0,

            stop_price: 14800.0,

            target_price: 15400.0,

            risk: 200.0,

            score: 3.0,

            volume: 1,

            warning_bar_ts: "2026-11-05 10:00:00".into(),

            trigger_ts: None,

            bars_waited: 0,

            max_wait_bars: 10,

            state: ConditionState::Pending,

            created_at: 1000,

            note: "".into(),
        };

        s.conditions.insert("cond_active".into(), active_cond);

        // 1 open position condition (Working, with non-closed position)

        let pos_cond = ConditionOrder {
            frozen: None,

            origin: TradeOrigin {
                account_id: "default".into(),

                instance_id: "inst".into(),

                strategy_id: N_STRATEGY.into(),

                strategy_version: revision.clone(),

                signal_id: "sig_pos".into(),
            },

            id: "cond_pos".into(),

            event_id: Some(2),

            symbol: "PB0".into(),

            instrument_id: "pb2611".into(),

            direction: "up".into(),

            tier: PositionTier::Small,

            entry_price: 15000.0,

            stop_price: 14800.0,

            target_price: 15400.0,

            risk: 200.0,

            score: 3.0,

            volume: 1,

            warning_bar_ts: "2026-11-05 10:00:00".into(),

            trigger_ts: Some("2026-11-05 10:05:00".into()),

            bars_waited: 0,

            max_wait_bars: 10,

            state: ConditionState::Working,

            created_at: 1000,

            note: "".into(),
        };

        s.conditions.insert("cond_pos".into(), pos_cond);

        s.positions.insert(
            "cond_pos".into(),
            ManagedPosition {
                origin: TradeOrigin {
                    account_id: "default".into(),

                    instance_id: "inst".into(),

                    strategy_id: N_STRATEGY.into(),

                    strategy_version: revision.clone(),

                    signal_id: "sig_pos".into(),
                },

                id: "cond_pos".into(),

                condition_order_id: "cond_pos".into(),

                symbol: "PB0".into(),

                instrument_id: "pb2611".into(),

                direction: "up".into(),

                tier: PositionTier::Small,

                fill_price: 15000.0,

                initial_stop_price: 14800.0,

                target_price: 15400.0,

                risk: 200.0,

                volume: 1,

                open_ts: "2026-11-05 10:05:00".into(),

                bars_held: 0,

                max_mfe_r: 0.0,

                max_mae_r: 0.0,

                trailing_grade: 0,

                current_stop_price: 14800.0,

                lot1_closed: false,

                exit_reason: None,

                exit_price: None,

                is_closed: false,

                realized_pnl_pts: None,

                r_multiple: None,

                event_id: Some(2),

                planned_entry_price: Some(15000.0),

                score: Some(3.0),

                warning_bar_ts: Some("2026-11-05 10:00:00".into()),

                trigger_ts: Some("2026-11-05 10:05:00".into()),

                order_insert_ts: Some("2026-11-05 10:05:00".into()),
                is_manual: false,
                user_note: None,
            },
        );

        // 5 terminal conditions

        for i in 1..=5 {
            let id = format!("cond_term_{i}");

            s.conditions.insert(
                id.clone(),
                ConditionOrder {
                    frozen: None,

                    origin: TradeOrigin {
                        account_id: "default".into(),

                        instance_id: "inst".into(),

                        strategy_id: N_STRATEGY.into(),

                        strategy_version: revision.clone(),

                        signal_id: format!("sig_term_{i}"),
                    },

                    id: id.clone(),

                    event_id: Some(10 + i as i64),

                    symbol: "PB0".into(),

                    instrument_id: "pb2611".into(),

                    direction: "up".into(),

                    tier: PositionTier::Small,

                    entry_price: 15000.0,

                    stop_price: 14800.0,

                    target_price: 15400.0,

                    risk: 200.0,

                    score: 3.0,

                    volume: 1,

                    warning_bar_ts: "2026-11-05 09:00:00".into(),

                    trigger_ts: Some("2026-11-05 09:10:00".into()),

                    bars_waited: 0,

                    max_wait_bars: 10,

                    state: ConditionState::Closed,

                    created_at: 900,

                    note: "".into(),
                },
            );
        }

        // 1 unsettled order

        s.orders.insert(
            "ord_unsettled".into(),
            ExecutionOrder {
                origin: TradeOrigin {
                    account_id: "default".into(),

                    instance_id: "inst".into(),

                    strategy_id: N_STRATEGY.into(),

                    strategy_version: revision.clone(),

                    signal_id: "sig_active".into(),
                },

                condition_id: "cond_active".into(),

                request: OrderInsertRequest {
                    instrument_id: "pb2611".into(),

                    exchange_id: Some("SHFE".into()),

                    direction: "buy".into(),

                    offset: "open".into(),

                    price: 15000.0,

                    volume: 1,

                    order_ref: Some("ord_unsettled".into()),
                },

                reason: None,

                phase: OrderPhase::Working,

                reported_filled: 0,

                filled: 0,

                submitted_at: chrono::Utc::now().timestamp(),

                entry_fill_notional: 0.,

                action_at: 1005,

                trading_day: "20261105".into(),

                note: "".into(),

                cancel_attempts: 0,

                funds_at_submit: None,

                entry_rules: None,
            },
        );

        // 10 terminal orders

        for i in 1..=10 {
            let id = format!("ord_term_{i}");

            s.orders.insert(
                id.clone(),
                ExecutionOrder {
                    origin: TradeOrigin {
                        account_id: "default".into(),

                        instance_id: "inst".into(),

                        strategy_id: N_STRATEGY.into(),

                        strategy_version: revision.clone(),

                        signal_id: format!("sig_term_{i}"),
                    },

                    condition_id: "cond_term_1".into(),

                    request: OrderInsertRequest {
                        instrument_id: "pb2611".into(),

                        exchange_id: Some("SHFE".into()),

                        direction: "buy".into(),

                        offset: "open".into(),

                        price: 15000.0,

                        volume: 1,

                        order_ref: Some(id.clone()),
                    },

                    reason: None,

                    phase: OrderPhase::Terminal,

                    reported_filled: 1,

                    filled: 1,

                    submitted_at: chrono::Utc::now().timestamp(),

                    entry_fill_notional: 0.,

                    action_at: 910,

                    trading_day: "20261105".into(),

                    note: "".into(),

                    cancel_attempts: 0,

                    funds_at_submit: None,

                    entry_rules: None,
                },
            );
        }

        s.rebuild_indices();
    }

    // Dispatch Quote event with fresh timestamp so valid() is true

    let q = ExecutionQuote {
        instrument_id: "pb2611".into(),

        exchange_id: "SHFE".into(),

        underlying_symbol: "SHFE.pb2611".into(),

        last: 15000.0,

        bid: 14999.0,

        ask: 15001.0,

        tick: 1.0,

        lower: 14000.0,

        upper: 16000.0,

        timestamp: chrono::Utc::now().timestamp(),
    };

    engine
        .observe_quotes([("PB0".into(), q)].into_iter().collect(), false)
        .await
        .unwrap();

    let records = observed.lock().unwrap().clone();

    assert_eq!(records.len(), 1);

    assert_eq!(records[0].0, "quote");

    // Quote event must prune context:

    // orders has ONLY the 1 unsettled order (not all 11 orders!)

    assert_eq!(records[0].1, 1);

    // conditions has ONLY the 2 active/relevant conditions (not all 7 conditions!)

    assert_eq!(records[0].2, 2);

    // Now dispatch Timer event

    engine
        .dispatch(StrategyEvent::Timer { timestamp: 1030 })
        .await
        .unwrap();

    let records = observed.lock().unwrap().clone();

    assert_eq!(records.len(), 2);

    assert_eq!(records[1].0, "timer");

    // Timer event MUST preserve all conditions and orders:

    assert_eq!(records[1].1, 11);

    assert_eq!(records[1].2, 7);
}

#[tokio::test]

async fn b5_timer_correctly_releases_terminal_triggered_conditions() {
    use crate::trader::b5;

    let counter = Counter::new();

    let engine = counter.engine(ExecutionMode::MonitorOnly, 2);

    let def = b5::definition();

    engine.set_definition(def.clone()).await.unwrap();

    let active_def = engine.definition().await;

    {
        let mut s = engine.state.lock().await;

        // Insert a terminal triggered condition with trigger_ts

        let cond = ConditionOrder {
            frozen: None,

            origin: TradeOrigin {
                account_id: "default".into(),

                instance_id: "default".into(),

                strategy_id: active_def.strategy_id.clone(),

                strategy_version: active_def.revision.clone(),

                signal_id: "sig_b5".into(),
            },

            id: "b5_cond_1".into(),

            event_id: Some(1),

            symbol: "BU0".into(),

            instrument_id: "bu2612".into(),

            direction: "up".into(),

            tier: PositionTier::Small,

            entry_price: 3000.0,

            stop_price: 2900.0,

            target_price: 3200.0,

            risk: 100.0,

            score: 3.0,

            volume: 1,

            warning_bar_ts: "2026-11-05 10:00:00".into(),

            trigger_ts: Some("2026-11-05 10:10:00".into()),

            bars_waited: 0,

            max_wait_bars: 10,

            state: ConditionState::Closed,

            created_at: 1000,

            note: "".into(),
        };

        s.conditions.insert("b5_cond_1".into(), cond);

        s.rebuild_indices();
    }

    // Dispatch Timer

    engine
        .dispatch(StrategyEvent::Timer { timestamp: 2000 })
        .await
        .unwrap();

    let s = engine.state.lock().await;

    let b5_state = s
        .strategy_states
        .get(&active_def.revision)
        .expect("b5 state exists");

    // Verify released contains b5_cond_1 -> 2000

    assert_eq!(b5_state["released"]["b5_cond_1"], 2000);
}

#[tokio::test]

async fn persistence_failure_protects_memory_and_blocks_new_orders_while_preserving_existing_position_stops(
) {
    let root = std::env::temp_dir().join(format!("ntrend_pf_test_{}", uuid::Uuid::new_v4()));

    std::fs::create_dir_all(&root).unwrap();

    let config_file = root.join("trader_config.json");

    let state_file = root.join("trader_execution.json");

    let counter = Counter::new();

    // Create engine with config path (which maps to trader_execution.json)

    let engine = ExecutionEngine::with_config_path(
        BrokerClient::Ctp(CtpTraderClient::new(counter.url.clone())),
        ExecutionConfig::default(),
        Some(config_file.clone()),
    );

    let cond_id = "pos_to_protect".to_string();

    {
        let mut s = engine.state.lock().await;

        s.broker_connected = true;

        s.broker_ready = true;

        s.conditions.insert(
            cond_id.clone(),
            ConditionOrder {
                frozen: None,

                origin: TradeOrigin::default(),

                id: cond_id.clone(),

                event_id: Some(1),

                symbol: "PB0".into(),

                instrument_id: "pb2611".into(),

                direction: "up".into(),

                tier: PositionTier::Small,

                entry_price: 15000.0,

                stop_price: 14800.0,

                target_price: 15400.0,

                risk: 200.0,

                score: 3.0,

                volume: 1,

                warning_bar_ts: "2026-11-05 10:00:00".into(),

                trigger_ts: Some("2026-11-05 10:05:00".into()),

                bars_waited: 0,

                max_wait_bars: 10,

                state: ConditionState::Working,

                created_at: 1000,

                note: "".into(),
            },
        );

        s.positions.insert(
            cond_id.clone(),
            ManagedPosition {
                origin: TradeOrigin::default(),

                id: cond_id.clone(),

                condition_order_id: cond_id.clone(),

                symbol: "PB0".into(),

                instrument_id: "pb2611".into(),

                direction: "up".into(),

                tier: PositionTier::Small,

                fill_price: 15000.0,

                initial_stop_price: 14800.0,

                target_price: 15400.0,

                risk: 200.0,

                volume: 1,

                open_ts: "2026-11-05 10:05:00".into(),

                bars_held: 0,

                max_mfe_r: 0.0,

                max_mae_r: 0.0,

                trailing_grade: 0,

                current_stop_price: 14800.0,

                lot1_closed: false,

                exit_reason: None,

                exit_price: None,

                is_closed: false,

                realized_pnl_pts: None,

                r_multiple: None,

                event_id: Some(1),

                planned_entry_price: Some(15000.0),

                score: Some(3.0),

                warning_bar_ts: Some("2026-11-05 10:00:00".into()),

                trigger_ts: Some("2026-11-05 10:05:00".into()),

                order_insert_ts: Some("2026-11-05 10:05:00".into()),
                is_manual: false,
                user_note: None,
            },
        );

        s.rebuild_indices();

        // Persist initial valid state to disk

        engine.save(&mut s).unwrap();
    }

    assert!(state_file.exists());

    let initial_disk_content = export_execution_state(&state_file).unwrap();
    engine.store.lock().unwrap().as_ref().unwrap().set_read_only();

    // Attempt to register a new plan that attempts to persist state

    let plan = EntryPlan {
        signal_id: "new_signal".into(),

        symbol: "PB0".into(),

        instrument_id: "pb2611".into(),

        direction: Direction::Long,

        reference_price: 15000.0,

        trigger: EntryTrigger::Immediate,

        stop_price: Some(14800.0),

        target_price: Some(15400.0),

        risk_per_unit: 200.0,

        requested_volume: 1,

        created_at: chrono::Utc::now().timestamp(),

        expires_at: Some(chrono::Utc::now().timestamp() + 600),

        reason: "test".into(),

        evidence: serde_json::json!({"entry_score": 3.0}),
    };

    let reg_result = engine
        .register_plan(plan, engine.definition().await, ExecutionConfig::default())
        .await;

    assert!(reg_result.is_err(), "Persistence failure must return Err");

    // 1. Verify old file on disk remains completely intact and uncorrupted

    let disk_content_after_fail = export_execution_state(&state_file).unwrap();

    assert_eq!(initial_disk_content, disk_content_after_fail);

    // 2. Verify in-memory state recorded persistence error and did not commit the failed next state

    {
        let s = engine.state.lock().await;

        assert!(
            s.persistence_error.is_some(),
            "In-memory state must record persistence error"
        );

        assert!(
            !s.conditions.contains_key("new_signal"),
            "Failed plan must not be committed to memory"
        );
    }

    // 3. Verify drive() blocks any new entry orders from being placed

    engine.drive().await.unwrap();

    assert!(
        counter.inserts().is_empty(),
        "drive() must not submit orders when persistence_error is set"
    );

    // 4. Verify existing position protection still executes according to rule in memory:

    // Quote drops below stop price (14790 < 14800) with fresh timestamp

    let drop_quote = ExecutionQuote {
        instrument_id: "pb2611".into(),

        exchange_id: "SHFE".into(),

        underlying_symbol: "SHFE.pb2611".into(),

        last: 14790.0,

        bid: 14789.0,

        ask: 14791.0,

        tick: 1.0,

        lower: 14000.0,

        upper: 16000.0,

        timestamp: chrono::Utc::now().timestamp(),
    };

    let _ = engine
        .observe_quotes([("PB0".into(), drop_quote)].into_iter().collect(), false)
        .await;

    let s = engine.state.lock().await;

    assert!(
        s.closes.contains_key(&cond_id),
        "Stop loss exit task must be latched in memory"
    );

    assert_eq!(
        s.conditions[&cond_id].state,
        ConditionState::Closing,
        "Condition must be transitioned to Closing"
    );

    assert_eq!(s.closes[&cond_id].reason, "stop");

    // Cleanup

    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn test_night_session_and_external_trade_chronology() {
    use super::*;

    // 1. Session phase check:
    // Friday night 22:59:09 belongs to phase 0 (night session pre-midnight)
    assert_eq!(trade_session_phase("22:59:09"), 0);
    // 01:15:00 belongs to phase 1 (night session post-midnight)
    assert_eq!(trade_session_phase("01:15:00"), 1);
    // Monday day 14:08:43 belongs to phase 2 (day session)
    assert_eq!(trade_session_phase("14:08:43"), 2);

    // 2. Position open check:
    // Position opened on 2026-11-16 14:08:43 (Monday day session)
    let pos = ManagedPosition {
        origin: TradeOrigin::default(),
        id: "pos_pp2701".into(),
        condition_order_id: "cond_pp2701".into(),
        symbol: "PP0".into(),
        instrument_id: "pp2701".into(),
        direction: "up".into(),
        tier: PositionTier::Large,
        fill_price: 9252.0,
        initial_stop_price: 9195.835,
        target_price: 9477.0,
        risk: 56.165,
        volume: 2,
        open_ts: "2026-11-16 14:08:43".into(),
        bars_held: 0,
        max_mfe_r: 0.0,
        max_mae_r: 0.0,
        trailing_grade: 0,
        current_stop_price: 9195.835,
        lot1_closed: false,
        exit_reason: None,
        exit_price: None,
        is_closed: false,
        realized_pnl_pts: None,
        r_multiple: None,
        event_id: None,
        planned_entry_price: None,
        score: None,
        warning_bar_ts: None,
        trigger_ts: None,
        order_insert_ts: None,
        user_note: None,
        is_manual: false,
    };

    // Trade from Friday night: trade_time 22:59:09, trading_day 20261116
    let friday_night_trade = TraderTradeRecord {
        trade_id: "102191655".into(),
        order_ref: "1".into(),
        instrument_id: "pp2701".into(),
        exchange_id: "DCE".into(),
        direction: "sell".into(),
        offset: "close".into(),
        price: 9419.0,
        volume: 2,
        trade_date: "20261113".into(),
        trade_time: "22:59:09".into(),
        trading_day: "20261116".into(),
    };

    // Friday night trade happened BEFORE Monday afternoon position, so trade_is_after_position_open must be false!
    assert!(!trade_is_after_position_open(&friday_night_trade, &pos));

    // A legitimate exit on Monday afternoon at 14:30:00 must be true
    let monday_later_trade = TraderTradeRecord {
        trade_id: "102191999".into(),
        order_ref: "2".into(),
        instrument_id: "pp2701".into(),
        exchange_id: "DCE".into(),
        direction: "sell".into(),
        offset: "close".into(),
        price: 9240.0,
        volume: 2,
        trade_date: "20261116".into(),
        trade_time: "14:30:00".into(),
        trading_day: "20261116".into(),
    };
    assert!(trade_is_after_position_open(&monday_later_trade, &pos));
}

#[tokio::test]
async fn reconcile_produces_fill_and_close_events() {
    let client = CtpTraderClient::new("http://127.0.0.1:0".into());
    let engine = ExecutionEngine::new(client, ExecutionConfig::default());
    let cond_id = "test_cond_1".to_string();
    {
        let mut s = engine.state.lock().await;
        s.broker_connected = true;
        s.broker_ready = true;
        s.trading_day = "20261120".into();
        s.conditions.insert(
            cond_id.clone(),
            ConditionOrder {
                frozen: None,
                origin: TradeOrigin {
                    account_id: "acc1".into(),
                    instance_id: "inst1".into(),
                    strategy_id: "n_strategy".into(),
                    strategy_version: "1".into(),
                    signal_id: "1".into(),
                },
                id: cond_id.clone(),
                event_id: Some(101),
                symbol: "RB0".into(),
                instrument_id: "rb2505".into(),
                direction: "up".into(),
                tier: PositionTier::Small,
                entry_price: 3500.0,
                stop_price: 3480.0,
                target_price: 3550.0,
                risk: 20.0,
                score: 3.5,
                volume: 1,
                state: ConditionState::Triggered,
                warning_bar_ts: "2026-11-20 09:15:00".into(),
                trigger_ts: Some("2026-11-20 09:30:00".into()),
                note: "".into(),
                bars_waited: 0,
                created_at: 100,
                max_wait_bars: 4,
            },
        );
        s.orders.insert(
            "1001".into(),
            ExecutionOrder {
                origin: TradeOrigin {
                    account_id: "acc1".into(),
                    instance_id: "inst1".into(),
                    strategy_id: "n_strategy".into(),
                    strategy_version: "1".into(),
                    signal_id: "1".into(),
                },
                request: OrderInsertRequest {
                    instrument_id: "rb2505".into(),
                    direction: "buy".into(),
                    offset: "open".into(),
                    price: 3502.0,
                    volume: 1,
                    exchange_id: Some("SHFE".into()),
                    order_ref: Some("1001".into()),
                },
                condition_id: cond_id.clone(),
                reason: None,
                phase: OrderPhase::Working,
                reported_filled: 0,
                filled: 0,
                note: "".into(),
                submitted_at: 100,
                action_at: 100,
                trading_day: "20261120".into(),
                entry_fill_notional: 0.0,
                cancel_attempts: 0,
                funds_at_submit: None,
                entry_rules: None,
            },
        );
    }

    // 1. Reconcile entry fill
    let snap_entry = json!({
        "health": { "ready": true, "logged_in": true, "broker_id": "9999", "user_id": "test_user" },
        "orders": [{
            "order_ref": "1001", "instrument_id": "rb2505", "volume_total_original": 1,
            "volume_traded": 1, "status": "all_traded", "status_msg": ""
        }],
        "trades": [{
            "trade_id": "T1", "order_ref": "1001", "instrument_id": "rb2505",
            "exchange_id": "SHFE", "direction": "buy", "offset": "open",
            "price": 3502.0, "volume": 1, "trade_date": "20261120",
            "trade_time": "09:30:01", "trading_day": "20261120"
        }],
        "account": { "balance": 100000.0, "available": 85000.0, "curr_margin": 15000.0, "close_profit": 0.0, "updated_at": 100.0 }
    });

    let ev1 = engine.reconcile(&snap_entry).await.unwrap();
    assert_eq!(ev1.fills.len(), 1);
    assert_eq!(ev1.closes.len(), 0);
    let fill = &ev1.fills[0];
    assert_eq!(fill.symbol, "RB0");
    assert_eq!(fill.instance_id, "inst1");
    assert_eq!(fill.signal_id, Some(101));
    assert_eq!(fill.fill_price, 3502.0);
    assert_eq!(fill.planned_entry_price, 3500.0);
    assert_eq!(fill.slippage_pts, 2.0);
    assert!(fill.account.is_some());
    assert_eq!(fill.account.as_ref().unwrap().balance, 100000.0);

    // 2. Insert exit order and reconcile exit fill
    {
        let mut s = engine.state.lock().await;
        s.orders.insert(
            "1002".into(),
            ExecutionOrder {
                origin: TradeOrigin {
                    account_id: "acc1".into(),
                    instance_id: "inst1".into(),
                    strategy_id: "n_strategy".into(),
                    strategy_version: "1".into(),
                    signal_id: "1".into(),
                },
                request: OrderInsertRequest {
                    instrument_id: "rb2505".into(),
                    direction: "sell".into(),
                    offset: "close".into(),
                    price: 3555.0,
                    volume: 1,
                    exchange_id: Some("SHFE".into()),
                    order_ref: Some("1002".into()),
                },
                condition_id: cond_id.clone(),
                reason: Some("target".into()),
                phase: OrderPhase::Working,
                reported_filled: 0,
                filled: 0,
                note: "".into(),
                submitted_at: 200,
                action_at: 200,
                trading_day: "20261120".into(),
                entry_fill_notional: 0.0,
                cancel_attempts: 0,
                funds_at_submit: None,
                entry_rules: None,
            },
        );
    }

    let snap_exit = json!({
        "health": { "ready": true, "logged_in": true, "broker_id": "9999", "user_id": "test_user" },
        "orders": [{
            "order_ref": "1002", "instrument_id": "rb2505", "volume_total_original": 1,
            "volume_traded": 1, "status": "all_traded", "status_msg": ""
        }],
        "trades": [{
            "trade_id": "T2", "order_ref": "1002", "instrument_id": "rb2505",
            "exchange_id": "SHFE", "direction": "sell", "offset": "close",
            "price": 3555.0, "volume": 1, "trade_date": "20261120",
            "trade_time": "10:15:00", "trading_day": "20261120"
        }],
        "account": { "balance": 100530.0, "available": 100530.0, "curr_margin": 0.0, "close_profit": 530.0, "updated_at": 200.0 }
    });

    let ev2 = engine.reconcile(&snap_exit).await.unwrap();
    assert_eq!(ev2.fills.len(), 0);
    assert_eq!(ev2.closes.len(), 1);
    let close = &ev2.closes[0];
    assert_eq!(close.symbol, "RB0");
    assert_eq!(close.exit_price, 3555.0);
    assert_eq!(close.exit_reason, "target");
    assert_eq!(close.planned_exit_price, Some(3550.0));
    assert_eq!(close.realized_pnl_pts, 53.0);
    assert!(close.pnl_diff_amount.is_some());
    assert!(close.pnl_diff_amount.unwrap() > 0.0);
}
