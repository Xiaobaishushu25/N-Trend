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

struct Counter {
    url: String,
    requests: Arc<StdMutex<Vec<(String, Value)>>>,
    drop_reply: Arc<AtomicBool>,
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
                let response = json!({"success":true,"order_ref":request["order_ref"],"msg":"submitted; no synthetic fill"}).to_string();
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
    engine
        .reconcile_entry_age("PB0", &["2026-10-16 10:00:00".into()])
        .await
        .unwrap();
    engine
        .reconcile(&json!({"bridge_online":true,"health": {
        "ready":true,"logged_in":true,"sync_pending":false,"trading_day":"20261016",
        "broker_id":"fake","user_id":"offline-test"
    }, "orders":orders,"trades":trades,"broker_positions":[]}))
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
async fn one_signal_survives_restart_unknown_ack_and_repeated_crossings_without_fake_fills() {
    let counter = Counter::new();
    let engine = counter.engine(ExecutionMode::FullAuto, 2);
    ready(&engine, vec![], vec![]).await;
    let id = register(&engine, 1000.0, 990.0).await;
    engine.state.lock().await.entry_symbols_ready.clear();
    quote(&engine, 1000.0).await;
    assert!(counter.inserts().is_empty()); // Startup waits for pending-signal age reconciliation.
    engine.reconcile_entry_age("PB0", &["2026-10-16 10:00:00".into()]).await.unwrap();
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
