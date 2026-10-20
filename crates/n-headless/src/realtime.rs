use axum::extract::ws::Message;
use n_protocol::ws::WsServerMessage;
use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use tokio::sync::{mpsc, Notify, OwnedSemaphorePermit, RwLock, Semaphore};

const CLIENT_QUEUE_CAPACITY: usize = 64;
const CLIENT_QUEUE_BYTES: usize = 8 * 1024 * 1024;

pub struct QueuedMessage {
    message: Message,
    permit: OwnedSemaphorePermit,
}

impl QueuedMessage {
    pub fn into_parts(self) -> (Message, OwnedSemaphorePermit) {
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
}

pub struct RealtimeHub {
    clients: RwLock<HashMap<usize, ClientSession>>,
    next_client_id: AtomicUsize,
}

impl RealtimeHub {
    pub fn new() -> Self {
        Self {
            clients: RwLock::new(HashMap::new()),
            next_client_id: AtomicUsize::new(1),
        }
    }

    pub async fn register_client(
        &self,
        device_id: String,
        role: String,
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
        };
        self.clients.write().await.insert(client_id, session);
        (client_id, rx, disconnect)
    }

    pub async fn unregister_client(&self, client_id: usize) {
        self.clients.write().await.remove(&client_id);
    }

    pub async fn send_to(&self, client_id: usize, msg: Message) {
        let mut map = self.clients.write().await;
        if let Some(session) = map.get(&client_id) {
            if !Self::enqueue(client_id, session, msg) {
                map.remove(&client_id);
            }
        }
    }

    // A blocked peer must reconnect and read a fresh snapshot, rather than retaining
    // an unlimited history of market/trading messages in the server's memory.
    fn enqueue(client_id: usize, session: &ClientSession, msg: Message) -> bool {
        let bytes = match &msg {
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
                bytes,
                "WebSocket待发送数据超出内存限额，关闭慢连接"
            );
            session.disconnect.notify_one();
            return false;
        };
        if let Err(error) = session.sender.try_send(QueuedMessage {
            message: msg,
            permit,
        }) {
            tracing::warn!(client_id, error = %error, "WebSocket发送队列不可用，关闭慢连接");
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
        let text = match serde_json::to_string(&msg) {
            Ok(s) => s,
            Err(_) => return,
        };
        let ws_msg = Message::Text(text);

        let mut map = self.clients.write().await;
        map.retain(|id, session| Self::enqueue(*id, session, ws_msg.clone()));
    }

    pub async fn broadcast_kline(
        &self,
        event_seq: u64,
        topic: &str,
        symbol: &str,
        timeframe: &str,
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
        let ws_msg = Message::Text(text);

        let mut map = self.clients.write().await;
        map.retain(|id, session| {
            // 如果客户端未设置任何订阅（空列表），默认全部接收；
            // 若显式设置了订阅，则仅当匹配 symbol 和 timeframe 时发送
            let symbol_match = session.subscribed_symbols.is_empty()
                || session.subscribed_symbols.contains(symbol);
            let tf_match = session.subscribed_timeframes.is_empty()
                || session.subscribed_timeframes.contains(timeframe);

            !symbol_match || !tf_match || Self::enqueue(*id, session, ws_msg.clone())
        });
    }

    pub async fn active_connections(&self) -> usize {
        self.clients.read().await.len()
    }
}
