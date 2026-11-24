use axum::extract::ws::Message;
use n_protocol::trader_stream::{DiagnosticChange, SharedTraderSnapshot, TraderDelta};
use n_protocol::ws::WsServerMessage;
use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use tokio::sync::{mpsc, Notify, OwnedSemaphorePermit, RwLock, Semaphore};

const CLIENT_QUEUE_CAPACITY: usize = 64;
const CLIENT_QUEUE_BYTES: usize = 8 * 1024 * 1024;

pub struct QueuedMessage {
    message: Arc<Message>,
    permit: OwnedSemaphorePermit,
}

impl QueuedMessage {
    pub fn into_parts(self) -> (Arc<Message>, OwnedSemaphorePermit) {
        (self.message, self.permit)
    }
}

#[allow(dead_code)]
pub struct ClientSession {
    pub device_id: String,
    pub role: String,
    pub sender: mpsc::Sender<QueuedMessage>,
    byte_budget: Arc<Semaphore>,
    disconnect: Arc<Notify>,
    pub subscribed_symbols: HashSet<String>,
    pub subscribed_timeframes: HashSet<String>,
    capabilities: ClientCapabilities,
}

#[derive(Clone, Copy, Default)]
pub struct ClientCapabilities {
    pub trader_delta: bool,
    pub gzip: bool,
    pub compact_diagnostics: bool,
    pub conditions_patch: bool,
}

#[cfg(test)]
mod tests {
    use super::*;
    use futures_util::FutureExt;

    #[tokio::test]
    async fn slow_peer_is_disconnected_without_blocking_healthy_peer() {
        let hub = RealtimeHub::new();
        let (_, slow_rx, slow_disconnect) =
            hub.register_client("pad".into(), "standard".into()).await;
        let (_, mut healthy_rx, healthy_disconnect) =
            hub.register_client("pc".into(), "admin".into()).await;
        for seq in 0..=CLIENT_QUEUE_CAPACITY as u64 {
            hub.broadcast(seq, "trader.updated", serde_json::json!({"seq": seq}))
                .await;
            assert!(healthy_rx.recv().await.is_some());
        }
        assert_eq!(slow_rx.len(), CLIENT_QUEUE_CAPACITY);
        assert_eq!(hub.active_connections().await, 1);
        assert!(slow_disconnect.notified().now_or_never().is_some());
        assert!(healthy_disconnect.notified().now_or_never().is_none());
    }

    #[tokio::test]
    async fn direct_replies_are_bounded_and_signal_disconnect() {
        let hub = RealtimeHub::new();
        let (id, rx, disconnect) = hub.register_client("pad".into(), "standard".into()).await;
        for _ in 0..=CLIENT_QUEUE_CAPACITY {
            hub.send_to(id, Message::Text("pong".into())).await;
        }
        assert_eq!(rx.len(), CLIENT_QUEUE_CAPACITY);
        assert_eq!(hub.active_connections().await, 0);
        assert!(disconnect.notified().now_or_never().is_some());
    }

    #[tokio::test]
    async fn large_snapshots_are_limited_by_bytes_before_message_count() {
        let hub = RealtimeHub::new();
        let (id, rx, disconnect) = hub.register_client("pad".into(), "standard".into()).await;
        let msg = Message::Text("x".repeat(CLIENT_QUEUE_BYTES / 2));
        for _ in 0..3 {
            hub.send_to(id, msg.clone()).await;
        }
        assert_eq!(rx.len(), 2);
        assert_eq!(hub.active_connections().await, 0);
        assert!(disconnect.notified().now_or_never().is_some());
    }

    #[tokio::test]
    async fn gzip_clients_queue_compressed_bytes_and_legacy_clients_keep_their_limit() {
        use std::io::Read;
        let hub = RealtimeHub::new();
        let (id, mut modern, modern_disconnect) = hub
            .register_with_baseline("new".into(), "admin".into(), ClientCapabilities {
                trader_delta: true, gzip: true, ..Default::default()
            }, None)
            .await;
        let (_, legacy, legacy_disconnect) =
            hub.register_client("old".into(), "admin".into()).await;
        let payload = serde_json::json!({ "history": "x".repeat(CLIENT_QUEUE_BYTES / 2 - 1024) });
        for seq in 1..=3 {
            hub.broadcast(seq, "trader.updated", payload.clone()).await;
        }
        assert_eq!(modern.len(), 3);
        assert_eq!(legacy.len(), 2);
        assert!(legacy_disconnect.notified().now_or_never().is_some());
        assert!(modern_disconnect.notified().now_or_never().is_none());
        let (message, permit) = modern.recv().await.unwrap().into_parts();
        let Message::Binary(bytes) = message.as_ref() else {
            panic!("expected a gzip frame")
        };
        assert_eq!(permit.num_permits(), bytes.len());
        assert!(
            hub.clients.read().await[&id]
                .byte_budget
                .available_permits()
                > CLIENT_QUEUE_BYTES / 2
        );
        let mut text = String::new();
        flate2::read::GzDecoder::new(bytes.as_slice())
            .read_to_string(&mut text)
            .unwrap();
        let decoded: serde_json::Value = serde_json::from_str(&text).unwrap();
        assert_eq!(decoded["payload"], payload);
        assert_eq!(decoded["event_seq"], 1);
    }

    #[tokio::test]
    async fn compact_diagnostics_preserve_legacy_deltas() {
        let hub = RealtimeHub::new();
        let (_, mut modern, _) = hub
            .register_with_baseline("new".into(), "admin".into(), ClientCapabilities {
                trader_delta: true, compact_diagnostics: true, ..Default::default()
            }, None)
            .await;
        let (_, mut legacy, _) = hub.register_client("old".into(), "admin".into()).await;
        let mut publisher = n_protocol::trader_stream::TraderPublisher::new("test".into());
        let mut snapshot = serde_json::json!({ "strategy_diagnostics": {
            "states": {"current":{"decisions":{"old":"x".repeat(30000)}}},
            "entry_decisions": {"changed": 2, "removed": 8}
        }});
        publisher.update_instance(snapshot.clone());
        snapshot["strategy_diagnostics"]["entry_decisions"] =
            serde_json::json!({"changed":3,"added":null});
        let (_, delta, diagnostics) = publisher.update_instance(snapshot.clone());
        hub.broadcast_instance_delta(
            1,
            "test",
            delta.unwrap(),
            diagnostics,
            &SharedTraderSnapshot::default(),
        )
        .await;
        let (compact, _) = modern.recv().await.unwrap().into_parts();
        let (full, _) = legacy.recv().await.unwrap().into_parts();
        let Message::Text(compact) = compact.as_ref() else {
            panic!()
        };
        let Message::Text(full) = full.as_ref() else {
            panic!()
        };
        assert!(compact.len() < full.len() / 10);
        let compact: serde_json::Value = serde_json::from_str(compact).unwrap();
        let full: serde_json::Value = serde_json::from_str(full).unwrap();
        let payload = &compact["payload"];
        assert_eq!(payload["delta"]["base_revision"], 1);
        assert_eq!(payload["delta"]["revision"], 2);
        assert!(payload["delta"]["changes"]
            .get("strategy_diagnostics")
            .is_none());
        assert_eq!(
            payload["diagnostic_changes"],
            serde_json::json!([
                {"path":["entry_decisions","added"],"value":null},
                {"path":["entry_decisions","changed"],"value":3},
                {"path":["entry_decisions","removed"]}
            ])
        );
        assert_eq!(
            full["payload"]["delta"]["changes"]["strategy_diagnostics"],
            snapshot["strategy_diagnostics"]
        );
    }

    #[tokio::test]
    async fn byte_budget_is_held_until_inflight_send_finishes() {
        let hub = RealtimeHub::new();
        let (id, mut rx, disconnect) = hub.register_client("pc".into(), "admin".into()).await;
        let msg = Message::Text("x".repeat(CLIENT_QUEUE_BYTES));
        for _ in 0..3 {
            hub.send_to(id, msg.clone()).await;
            let (message, permit) = rx.recv().await.unwrap().into_parts();
            let map = hub.clients.read().await;
            assert_eq!(map[&id].byte_budget.available_permits(), 0);
            drop(map);
            drop((message, permit));
        }
        assert_eq!(hub.active_connections().await, 1);
        assert!(disconnect.notified().now_or_never().is_none());
    }

    #[tokio::test]
    async fn filtered_klines_do_not_fill_unrelated_peers_queue() {
        let hub = RealtimeHub::new();
        let (id, rx, disconnect) = hub.register_client("pad".into(), "standard".into()).await;
        hub.update_subscription(id, vec!["CF0".into()], vec!["15m".into()])
            .await;
        for seq in 0..=CLIENT_QUEUE_CAPACITY as u64 {
            hub.broadcast_kline(seq, "kline.partial", "SR0", "15m", serde_json::Value::Null)
                .await;
        }
        assert!(rx.is_empty());
        assert_eq!(hub.active_connections().await, 1);
        for seq in 0..=CLIENT_QUEUE_CAPACITY as u64 {
            hub.broadcast_kline(seq, "kline.partial", "CF0", "15m", serde_json::Value::Null)
                .await;
        }
        assert_eq!(rx.len(), CLIENT_QUEUE_CAPACITY);
        assert_eq!(hub.active_connections().await, 0);
        assert!(disconnect.notified().now_or_never().is_some());
    }

    #[tokio::test]
    async fn history_updates_reach_all_timeframes_for_the_subscribed_symbol() {
        let hub = RealtimeHub::new();
        let (first, mut first_rx, _) = hub.register_client("first".into(), "standard".into()).await;
        let (second, mut second_rx, _) = hub
            .register_client("second".into(), "standard".into())
            .await;
        let (other, other_rx, _) = hub.register_client("other".into(), "standard".into()).await;
        hub.update_subscription(first, vec!["CF0".into()], vec!["5m".into()])
            .await;
        hub.update_subscription(second, vec!["CF0".into()], vec!["1d".into()])
            .await;
        hub.update_subscription(other, vec!["SR0".into()], vec!["1d".into()])
            .await;
        let update = serde_json::json!({
            "symbol": "CF0", "previous_revision": 10, "series_revision": 13,
            "full_reload_required": false, "bars": [],
        });
        hub.broadcast_chart_history(1, "CF0", update).await;
        for rx in [&mut first_rx, &mut second_rx] {
            let (message, _) = rx.try_recv().unwrap().into_parts();
            assert!(
                matches!(message.as_ref(), Message::Text(text) if text.contains("kline.history"))
            );
        }
        assert!(other_rx.is_empty());
    }

    #[tokio::test]
    async fn trader_delta_peers_get_only_changes_and_legacy_peers_get_light_baseline() {
        let hub = RealtimeHub::new();
        let (id, mut modern, _) = hub.register_client("new".into(), "admin".into()).await;
        hub.set_trader_delta(id, true).await;
        let (_, mut legacy, _) = hub.register_client("old".into(), "admin".into()).await;
        let mut publisher = n_protocol::trader_stream::TraderPublisher::new("boot".into());
        let mut raw = serde_json::json!({"account":{"balance":1,"updated_at":1},
            "broker_positions":[{"position":2}], "execution_orders":vec![serde_json::json!({"note":"rejected"});4031]});
        publisher.update(raw.clone());
        raw["account"]["balance"] = serde_json::json!(2);
        let (snapshot, delta) = publisher.update(raw);
        hub.broadcast_trader(
            2,
            &delta.unwrap(),
            &SharedTraderSnapshot::from_value(snapshot),
        )
        .await;
        let (new, _) = modern.recv().await.unwrap().into_parts();
        let Message::Text(new) = new.as_ref() else {
            panic!()
        };
        let (old, _) = legacy.recv().await.unwrap().into_parts();
        let Message::Text(old) = old.as_ref() else {
            panic!()
        };
        let new: serde_json::Value = serde_json::from_str(new).unwrap();
        let old: serde_json::Value = serde_json::from_str(old).unwrap();
        assert_eq!(new["topic"], "trader.delta");
        assert_eq!(new["payload"]["changes"].as_object().unwrap().len(), 1);
        assert_eq!(old["topic"], "trader.updated");
        assert!(old["payload"].get("execution_orders").is_none());
        assert_eq!(old["payload"]["broker_positions"][0]["position"], 2);
    }

    #[tokio::test]
    async fn condition_updates_are_compact_only_for_clients_that_negotiated_them() {
        use n_protocol::trader_stream::{ConditionRows, TraderPublisher};
        use serde_json::value::to_raw_value;

        let hub = RealtimeHub::new();
        let (_, mut modern, _) = hub
            .register_with_baseline(
                "modern".into(),
                "admin".into(),
                ClientCapabilities {
                    trader_delta: true,
                    conditions_patch: true,
                    ..Default::default()
                },
                None,
            )
            .await;
        let (_, mut legacy, _) = hub
            .register_with_baseline(
                "legacy".into(),
                "admin".into(),
                ClientCapabilities {
                    trader_delta: true,
                    ..Default::default()
                },
                None,
            )
            .await;
        let snapshot = |state| {
            let rows: ConditionRows = ["a", "b"]
                .into_iter()
                .map(|id| {
                    let value = serde_json::json!({
                        "id": id, "state": if id == "a" { state } else { "pending" }
                    });
                    (id.to_owned(), Arc::from(to_raw_value(&value).unwrap()))
                })
                .collect();
            let mut snapshot = SharedTraderSnapshot::default();
            snapshot.insert_conditions(Arc::new(rows));
            snapshot
        };
        let mut publisher = TraderPublisher::new("boot".into());
        publisher.update_shared(snapshot("pending"), false);
        let (current, delta, _) = publisher.update_shared(snapshot("closed"), false);
        let delta = delta.unwrap();
        hub.broadcast_trader(1, &delta, &current).await;
        hub.broadcast_instance_delta(2, "instance", delta, None, &current)
            .await;

        for ptr in ["/payload", "/payload/delta"] {
            let read = |item: QueuedMessage| {
                let (frame, _) = item.into_parts();
                let Message::Text(text) = frame.as_ref() else {
                    panic!("expected text frame")
                };
                serde_json::from_str::<serde_json::Value>(text).unwrap()
            };
            let compact = read(modern.try_recv().unwrap());
            let compact = compact.pointer(ptr).unwrap();
            assert_eq!(
                compact["conditions_patch"]["upsert"]
                    .as_array()
                    .unwrap()
                    .len(),
                1
            );
            assert_eq!(compact["conditions_patch"]["upsert"][0]["state"], "closed");
            assert!(compact["changes"].get("conditions").is_none());
            let full = read(legacy.try_recv().unwrap());
            let full = full.pointer(ptr).unwrap();
            assert!(full.get("conditions_patch").is_none());
            assert_eq!(
                full["changes"]["conditions"],
                current.to_value()["conditions"]
            );
        }
    }
}

pub struct RealtimeHub {
    clients: RwLock<HashMap<usize, ClientSession>>,
    next_client_id: AtomicUsize,
    baseline_encoding: Semaphore,
}

impl RealtimeHub {
    pub fn new() -> Self {
        Self {
            clients: RwLock::new(HashMap::new()),
            next_client_id: AtomicUsize::new(1),
            baseline_encoding: Semaphore::new(1),
        }
    }

    pub async fn baseline_slot(&self) -> tokio::sync::SemaphorePermit<'_> {
        self.baseline_encoding
            .acquire()
            .await
            .expect("baseline semaphore stays open")
    }

    #[cfg(test)]
    pub async fn register_client(
        &self,
        device_id: String,
        role: String,
    ) -> (usize, mpsc::Receiver<QueuedMessage>, Arc<Notify>) {
        self.register_with_baseline(device_id, role, ClientCapabilities::default(), None)
            .await
    }

    pub async fn register_with_baseline(
        &self,
        device_id: String,
        role: String,
        capabilities: ClientCapabilities,
        baseline: Option<(u64, &SharedTraderSnapshot)>,
    ) -> (usize, mpsc::Receiver<QueuedMessage>, Arc<Notify>) {
        let (tx, rx) = mpsc::channel(CLIENT_QUEUE_CAPACITY);
        let disconnect = Arc::new(Notify::new());
        let client_id = self.next_client_id.fetch_add(1, Ordering::Relaxed);
        let session = ClientSession {
            device_id,
            role,
            sender: tx,
            byte_budget: Arc::new(Semaphore::new(CLIENT_QUEUE_BYTES)),
            disconnect: disconnect.clone(),
            subscribed_symbols: HashSet::new(),
            subscribed_timeframes: HashSet::new(),
            capabilities,
        };
        if let Some((event_seq, snapshot)) = baseline {
            if let Some(frame) =
                PreparedEvent::from_trader_snapshot(event_seq, snapshot, capabilities.gzip).await
            {
                if !Self::enqueue(
                    client_id,
                    &session,
                    frame.message(capabilities.gzip),
                    &frame.topic,
                ) {
                    return (client_id, rx, disconnect);
                }
            }
        }
        self.clients.write().await.insert(client_id, session);
        (client_id, rx, disconnect)
    }

    pub async fn unregister_client(&self, client_id: usize) {
        self.clients.write().await.remove(&client_id);
    }

    #[cfg(test)]
    pub async fn set_trader_delta(&self, client_id: usize, enabled: bool) {
        if let Some(session) = self.clients.write().await.get_mut(&client_id) {
            session.capabilities.trader_delta = enabled;
        }
    }

    pub async fn broadcast_trader(
        &self,
        event_seq: u64,
        delta: &TraderDelta,
        snapshot: &SharedTraderSnapshot,
    ) {
        let (mut delta_modes, mut legacy_gzip) = (HashMap::new(), None);
        {
            let clients = self.clients.read().await;
            for session in clients.values() {
                let capabilities = session.capabilities;
                if capabilities.trader_delta {
                    let compact = delta.conditions_patch.is_some() && capabilities.conditions_patch;
                    *delta_modes.entry(compact).or_insert(false) |= capabilities.gzip;
                } else {
                    *legacy_gzip.get_or_insert(false) |= capabilities.gzip;
                }
            }
        }
        let mut frames = HashMap::new();
        for (compact, gzip) in delta_modes {
            let mut outgoing = std::borrow::Cow::Borrowed(delta);
            if !compact && delta.conditions_patch.is_some() {
                snapshot.expand_conditions_patch(outgoing.to_mut());
            }
            if let Some(frame) = PreparedEvent::new(
                WsServerMessage::new_event(event_seq, "trader.delta", outgoing.as_ref()),
                gzip,
            )
            .await
            {
                frames.insert(compact, frame);
            }
        }
        let legacy = if let Some(gzip) = legacy_gzip {
            let _slot = self.baseline_slot().await;
            PreparedEvent::from_trader_snapshot(event_seq, snapshot, gzip).await
        } else {
            None
        };
        self.clients.write().await.retain(|id, session| {
            let capabilities = session.capabilities;
            let event = if capabilities.trader_delta {
                frames.get(&(delta.conditions_patch.is_some() && capabilities.conditions_patch))
            } else {
                legacy.as_ref()
            };
            event.map_or(true, |frame| {
                Self::enqueue(*id, session, frame.message(capabilities.gzip), &frame.topic)
            })
        });
    }

    pub async fn broadcast_instance_delta(
        &self,
        event_seq: u64,
        instance_id: &str,
        delta: TraderDelta,
        diagnostics: Option<Vec<DiagnosticChange>>,
        snapshot: &SharedTraderSnapshot,
    ) {
        const TOPIC: &str = "trader.instance.delta";
        let mode = |client: &ClientSession| {
            (
                diagnostics.is_some() && client.capabilities.compact_diagnostics,
                delta.conditions_patch.is_some() && client.capabilities.conditions_patch,
            )
        };
        let mut modes = HashMap::new();
        {
            let clients = self.clients.read().await;
            for session in clients.values() {
                *modes.entry(mode(session)).or_insert(false) |= session.capabilities.gzip;
            }
        }
        let mut frames = HashMap::new();
        for (key @ (compact_diagnostics, compact_conditions), gzip) in modes {
            let mut outgoing = std::borrow::Cow::Borrowed(&delta);
            if !compact_conditions && delta.conditions_patch.is_some() {
                snapshot.expand_conditions_patch(outgoing.to_mut());
            }
            let payload = if compact_diagnostics {
                outgoing.to_mut().changes.remove("strategy_diagnostics");
                serde_json::json!({"instance_id": instance_id, "delta": outgoing.as_ref(),
                    "diagnostic_changes": diagnostics})
            } else {
                serde_json::json!({"instance_id": instance_id, "delta": outgoing.as_ref()})
            };
            let event = WsServerMessage::Event {
                event_seq,
                topic: TOPIC.into(),
                payload,
            };
            if let Some(frame) = PreparedEvent::new(event, gzip).await {
                frames.insert(key, frame);
            }
        }
        self.clients.write().await.retain(|id, session| {
            frames.get(&mode(session)).map_or(true, |frame| {
                Self::enqueue(
                    *id,
                    session,
                    frame.message(session.capabilities.gzip),
                    TOPIC,
                )
            })
        });
    }

    pub async fn send_to(&self, client_id: usize, msg: Message) {
        let mut map = self.clients.write().await;
        if let Some(session) = map.get(&client_id) {
            if !Self::enqueue(client_id, session, Arc::new(msg), "direct") {
                map.remove(&client_id);
            }
        }
    }

    // A blocked peer must reconnect and read a fresh snapshot, rather than retaining
    // an unlimited history of market/trading messages in the server's memory.
    fn enqueue(client_id: usize, session: &ClientSession, msg: Arc<Message>, topic: &str) -> bool {
        let bytes = match msg.as_ref() {
            Message::Text(text) => text.len(),
            Message::Binary(data) | Message::Ping(data) | Message::Pong(data) => data.len(),
            Message::Close(frame) => frame.as_ref().map_or(2, |f| f.reason.len() + 2),
        }
        .max(1);
        let permit = u32::try_from(bytes).ok().and_then(|bytes| {
            session
                .byte_budget
                .clone()
                .try_acquire_many_owned(bytes)
                .ok()
        });
        let Some(permit) = permit else {
            tracing::warn!(
                client_id,
                topic,
                bytes,
                queued_bytes = CLIENT_QUEUE_BYTES - session.byte_budget.available_permits(),
                "WebSocket待发送数据超出内存限额，关闭慢连接"
            );
            session.disconnect.notify_one();
            return false;
        };
        if session
            .sender
            .try_send(QueuedMessage {
                message: msg,
                permit,
            })
            .is_err()
        {
            tracing::warn!(
                client_id,
                topic,
                bytes,
                "WebSocket发送队列不可用，关闭慢连接"
            );
            session.disconnect.notify_one();
            return false;
        }
        true
    }

    pub async fn update_subscription(
        &self,
        client_id: usize,
        symbols: Vec<String>,
        timeframes: Vec<String>,
    ) {
        let mut map = self.clients.write().await;
        if let Some(session) = map.get_mut(&client_id) {
            session.subscribed_symbols = symbols.into_iter().collect();
            session.subscribed_timeframes = timeframes.into_iter().collect();
        }
    }

    pub async fn broadcast(&self, event_seq: u64, topic: &str, payload: serde_json::Value) {
        let msg = WsServerMessage::Event {
            event_seq,
            topic: topic.to_string(),
            payload,
        };
        let gzip = {
            let clients = self.clients.read().await;
            if clients.is_empty() {
                return;
            }
            clients.values().any(|s| s.capabilities.gzip)
        };
        if let Some(frame) = PreparedEvent::new(msg, gzip).await {
            self.clients.write().await.retain(|id, session| {
                Self::enqueue(*id, session, frame.message(session.capabilities.gzip), topic)
            });
        }
    }

    pub async fn broadcast_kline(
        &self,
        event_seq: u64,
        topic: &str,
        symbol: &str,
        timeframe: &str,
        payload: serde_json::Value,
    ) {
        self.broadcast_chart_event(event_seq, topic, symbol, Some(timeframe), payload)
            .await;
    }

    pub async fn broadcast_chart_history(
        &self,
        event_seq: u64,
        symbol: &str,
        payload: serde_json::Value,
    ) {
        self.broadcast_chart_event(event_seq, "kline.history", symbol, None, payload)
            .await;
    }

    async fn broadcast_chart_event(
        &self,
        event_seq: u64,
        topic: &str,
        symbol: &str,
        timeframe: Option<&str>,
        payload: serde_json::Value,
    ) {
        let msg = WsServerMessage::Event {
            event_seq,
            topic: topic.to_string(),
            payload,
        };
        let text = match serde_json::to_string(&msg) {
            Ok(s) => s,
            Err(_) => return,
        };
        let ws_msg = Arc::new(Message::Text(text));

        let mut map = self.clients.write().await;
        map.retain(|id, session| {
            // 如果客户端未设置任何订阅（空列表），默认全部接收；
            // 若显式设置了订阅，则仅当匹配 symbol 和 timeframe 时发送
            let symbol_match = session.subscribed_symbols.is_empty()
                || session.subscribed_symbols.contains(symbol);
            let tf_match = timeframe.is_none_or(|timeframe| {
                session.subscribed_timeframes.is_empty()
                    || session.subscribed_timeframes.contains(timeframe)
            });

            !symbol_match || !tf_match || Self::enqueue(*id, session, ws_msg.clone(), topic)
        });
    }

    pub async fn active_connections(&self) -> usize {
        self.clients.read().await.len()
    }

    pub async fn chart_symbols(&self) -> HashSet<String> {
        self.clients
            .read()
            .await
            .values()
            .flat_map(|session| session.subscribed_symbols.iter().cloned())
            .collect()
    }
}

struct PreparedEvent {
    topic: String,
    text: Arc<Message>,
    gzip: Option<Arc<Message>>,
}

impl PreparedEvent {
    async fn from_trader_snapshot(
        event_seq: u64,
        snapshot: &SharedTraderSnapshot,
        gzip: bool,
    ) -> Option<Self> {
        #[derive(serde::Serialize)]
        struct SnapshotEvent<'a> {
            #[serde(rename = "type")]
            kind: &'static str,
            event_seq: u64,
            topic: &'static str,
            payload: &'a SharedTraderSnapshot,
        }

        let snapshot = snapshot.clone();
        tokio::task::spawn_blocking(move || {
            let topic = "trader.updated";
            let text = serde_json::to_string(&SnapshotEvent {
                kind: "event",
                event_seq,
                topic,
                payload: &snapshot,
            })
            .ok()?;
            let fields = (text.len() >= 256 * 1024)
                .then(|| snapshot.field_names().map(str::to_owned).collect());
            let compressed = if gzip && text.len() >= 8192 {
                gzip_message(&text).map(Arc::new)
            } else {
                None
            };
            Some(Self::from_frames(topic.into(), text, compressed, fields))
        })
        .await
        .ok()?
    }

    async fn new(event: WsServerMessage, gzip: bool) -> Option<Self> {
        let WsServerMessage::Event { topic, payload, .. } = &event else {
            return None;
        };
        let topic = topic.clone();
        let text = serde_json::to_string(&event).ok()?;
        let uncompressed_bytes = text.len();
        let large = uncompressed_bytes >= 256 * 1024;
        let fields = large.then(|| {
            payload
                .pointer("/delta/changes")
                .or_else(|| payload.get("changes"))
                .unwrap_or(payload)
                .as_object()
                .map(|object| object.keys().cloned().collect::<Vec<_>>())
                .unwrap_or_default()
        });
        let (text, gzip) = if gzip && uncompressed_bytes >= 8192 {
            tokio::task::spawn_blocking(move || {
                let compressed = gzip_message(&text).map(Arc::new);
                (text, compressed)
            })
            .await
            .ok()?
        } else {
            (text, None)
        };
        Some(Self::from_frames(topic, text, gzip, fields))
    }

    fn from_frames(
        topic: String,
        text: String,
        gzip: Option<Arc<Message>>,
        fields: Option<Vec<String>>,
    ) -> Self {
        if let Some(fields) = fields {
            let compressed_bytes = gzip.as_deref().and_then(|msg| {
                if let Message::Binary(bytes) = msg {
                    Some(bytes.len())
                } else {
                    None
                }
            });
            tracing::info!(
                topic,
                ?fields,
                uncompressed_bytes = text.len(),
                ?compressed_bytes,
                "大型 WebSocket 消息"
            );
        }
        Self {
            topic,
            text: Arc::new(Message::Text(text)),
            gzip,
        }
    }

    fn message(&self, gzip: bool) -> Arc<Message> {
        if gzip {
            if let Some(compressed) = &self.gzip {
                return compressed.clone();
            }
        }
        self.text.clone()
    }
}

fn gzip_message(text: &str) -> Option<Message> {
    use std::io::Write;
    let mut encoder = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::fast());
    if encoder.write_all(text.as_bytes()).is_ok() {
        if let Ok(bytes) = encoder.finish() {
            if bytes.len() < text.len() {
                return Some(Message::Binary(bytes));
            }
        }
    }
    None
}
