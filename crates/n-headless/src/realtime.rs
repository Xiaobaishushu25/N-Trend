use axum::extract::ws::Message;
use n_protocol::ws::WsServerMessage;
use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicUsize, Ordering};
use tokio::sync::{mpsc, RwLock};

#[allow(dead_code)]
pub struct ClientSession {
    pub device_id: String,
    pub role: String,
    pub sender: mpsc::UnboundedSender<Message>,
    pub subscribed_symbols: HashSet<String>,
    pub subscribed_timeframes: HashSet<String>,
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
    ) -> (usize, mpsc::UnboundedReceiver<Message>) {
        let (tx, rx) = mpsc::unbounded_channel();
        let client_id = self.next_client_id.fetch_add(1, Ordering::Relaxed);
        let session = ClientSession {
            device_id,
            role,
            sender: tx,
            subscribed_symbols: HashSet::new(),
            subscribed_timeframes: HashSet::new(),
        };
        self.clients.write().await.insert(client_id, session);
        (client_id, rx)
    }

    pub async fn unregister_client(&self, client_id: usize) {
        self.clients.write().await.remove(&client_id);
    }

    pub async fn send_to(&self, client_id: usize, msg: Message) {
        let map = self.clients.read().await;
        if let Some(session) = map.get(&client_id) {
            let _ = session.sender.send(msg);
        }
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

        let map = self.clients.read().await;
        for (_, session) in map.iter() {
            let _ = session.sender.send(ws_msg.clone());
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

        let map = self.clients.read().await;
        for (_, session) in map.iter() {
            // 如果客户端未设置任何订阅（空列表），默认全部接收；
            // 若显式设置了订阅，则仅当匹配 symbol 和 timeframe 时发送
            let symbol_match = session.subscribed_symbols.is_empty()
                || session.subscribed_symbols.contains(symbol);
            let tf_match = session.subscribed_timeframes.is_empty()
                || session.subscribed_timeframes.contains(timeframe);

            if symbol_match && tf_match {
                let _ = session.sender.send(ws_msg.clone());
            }
        }
    }

    pub async fn active_connections(&self) -> usize {
        self.clients.read().await.len()
    }
}
