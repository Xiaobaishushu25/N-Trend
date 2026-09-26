use std::collections::HashSet;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;
use futures_util::{SinkExt, StreamExt};
use n_protocol::dto::*;
use n_protocol::ws::*;
use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Emitter};
use tokio::sync::{mpsc, RwLock};
use tokio_tungstenite::connect_async;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::http::HeaderValue;
use tokio_tungstenite::tungstenite::Message;

use super::credential_store::CredentialStore;
use super::kline_cache::KlineMemoryCache;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum ConnectionStatus {
    Connected,
    Reconnecting,
    Unauthorized,
    VersionMismatch,
    Disconnected,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ConnectionStateDto {
    pub status: ConnectionStatus,
    pub server_url: String,
    pub quote_delay_ms: Option<u64>,
    pub last_event_seq: u64,
}

pub struct RealtimeClient {
    app: AppHandle,
    credentials: Arc<CredentialStore>,
    kline_cache: Arc<KlineMemoryCache>,
    subscribed_symbols: Arc<RwLock<HashSet<String>>>,
    subscribed_timeframes: Arc<RwLock<HashSet<String>>>,
    tx_sub: Arc<RwLock<Option<mpsc::UnboundedSender<WsClientMessage>>>>,
    last_event_seq: Arc<AtomicU64>,
    status: Arc<RwLock<ConnectionStatus>>,
    quote_delay_ms: Arc<AtomicU64>,
}

impl RealtimeClient {
    pub fn new(
        app: AppHandle,
        credentials: Arc<CredentialStore>,
        kline_cache: Arc<KlineMemoryCache>,
    ) -> Arc<Self> {
        let client = Arc::new(Self {
            app,
            credentials,
            kline_cache,
            subscribed_symbols: Arc::new(RwLock::new(HashSet::new())),
            subscribed_timeframes: Arc::new(RwLock::new(HashSet::new())),
            tx_sub: Arc::new(RwLock::new(None)),
            last_event_seq: Arc::new(AtomicU64::new(0)),
            status: Arc::new(RwLock::new(ConnectionStatus::Disconnected)),
            quote_delay_ms: Arc::new(AtomicU64::new(0)),
        });

        // Spawn background connection worker
        let worker_client = client.clone();
        tauri::async_runtime::spawn(async move {
            worker_client.run_loop().await;
        });

        client
    }

    pub async fn get_state(&self) -> ConnectionStateDto {
        ConnectionStateDto {
            status: self.status.read().await.clone(),
            server_url: self.credentials.get_server_url(),
            quote_delay_ms: {
                let delay = self.quote_delay_ms.load(Ordering::Relaxed);
                if delay > 0 { Some(delay) } else { None }
            },
            last_event_seq: self.last_event_seq.load(Ordering::Relaxed),
        }
    }

    pub async fn subscribe(&self, symbols: Vec<String>, timeframes: Vec<String>) {
        {
            let mut sym_lock = self.subscribed_symbols.write().await;
            for s in &symbols {
                sym_lock.insert(s.clone());
            }
            let mut tf_lock = self.subscribed_timeframes.write().await;
            for tf in &timeframes {
                tf_lock.insert(tf.clone());
            }
        }

        let msg = WsClientMessage::Subscribe { symbols, timeframes };
        if let Some(tx) = self.tx_sub.read().await.as_ref() {
            let _ = tx.send(msg);
        }
    }

    pub async fn unsubscribe(&self, symbols: Vec<String>, timeframes: Vec<String>) {
        {
            let mut sym_lock = self.subscribed_symbols.write().await;
            for s in &symbols {
                sym_lock.remove(s);
            }
            let mut tf_lock = self.subscribed_timeframes.write().await;
            for tf in &timeframes {
                tf_lock.remove(tf);
            }
        }

        let msg = WsClientMessage::Unsubscribe { symbols, timeframes };
        if let Some(tx) = self.tx_sub.read().await.as_ref() {
            let _ = tx.send(msg);
        }
    }

    async fn set_status(&self, new_status: ConnectionStatus) {
        let mut lock = self.status.write().await;
        if *lock != new_status {
            *lock = new_status.clone();
            let _ = self.app.emit("connection-status-changed", &new_status);
        }
    }

    async fn run_loop(&self) {
        let backoffs = [1, 2, 4, 8, 16, 30, 60];
        let mut backoff_idx = 0;

        loop {
            let server_url = self.credentials.get_server_url();
            let token = self.credentials.get_token();

            let ws_url = if server_url.starts_with("https://") {
                format!("wss://{}/api/v1/ws", &server_url[8..])
            } else if server_url.starts_with("http://") {
                format!("ws://{}/api/v1/ws", &server_url[7..])
            } else {
                format!("ws://{}/api/v1/ws", server_url)
            };

            tracing::info!("🔌 正在尝试连接实时服务: {ws_url}");
            self.set_status(ConnectionStatus::Reconnecting).await;

            let mut req = match ws_url.into_client_request() {
                Ok(r) => r,
                Err(e) => {
                    tracing::error!("WebSocket URL 格式错误: {e}");
                    tokio::time::sleep(Duration::from_secs(5)).await;
                    continue;
                }
            };

            if !token.is_empty() {
                if let Ok(val) = HeaderValue::from_str(&format!("Bearer {token}")) {
                    req.headers_mut().insert("Authorization", val);
                }
            }

            match connect_async(req).await {
                Ok((ws_stream, _resp)) => {
                    tracing::info!("✅ 实时 WebSocket 连接已建立");
                    self.set_status(ConnectionStatus::Connected).await;
                    backoff_idx = 0;

                    let (mut write, mut read) = ws_stream.split();
                    let (tx, mut rx) = mpsc::unbounded_channel::<WsClientMessage>();
                    *self.tx_sub.write().await = Some(tx);

                    // Resend current subscriptions
                    let current_syms: Vec<String> = self.subscribed_symbols.read().await.iter().cloned().collect();
                    let current_tfs: Vec<String> = self.subscribed_timeframes.read().await.iter().cloned().collect();
                    if !current_syms.is_empty() || !current_tfs.is_empty() {
                        let sub_msg = WsClientMessage::Subscribe {
                            symbols: current_syms,
                            timeframes: current_tfs,
                        };
                        if let Ok(json) = serde_json::to_string(&sub_msg) {
                            let _ = write.send(Message::Text(json)).await;
                        }
                    }

                    let mut ping_interval = tokio::time::interval(Duration::from_secs(30));
                    ping_interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);

                    loop {
                        tokio::select! {
                            _ = ping_interval.tick() => {
                                let ping_msg = WsClientMessage::Ping {
                                    client_time: chrono::Utc::now().timestamp_millis(),
                                };
                                if let Ok(json) = serde_json::to_string(&ping_msg) {
                                    if write.send(Message::Text(json)).await.is_err() {
                                        break;
                                    }
                                }
                            }
                            Some(client_msg) = rx.recv() => {
                                if let Ok(json) = serde_json::to_string(&client_msg) {
                                    if write.send(Message::Text(json)).await.is_err() {
                                        break;
                                    }
                                }
                            }
                            msg_opt = read.next() => {
                                match msg_opt {
                                    Some(Ok(Message::Text(text))) => {
                                        self.handle_incoming_text(&text).await;
                                    }
                                    Some(Ok(Message::Close(_))) | None => {
                                        tracing::warn!("WebSocket 连接由对端关闭");
                                        break;
                                    }
                                    Some(Err(e)) => {
                                        tracing::warn!("WebSocket 传输异常: {e}");
                                        break;
                                    }
                                    _ => {}
                                }
                            }
                        }
                    }

                    *self.tx_sub.write().await = None;
                }
                Err(e) => {
                    tracing::warn!("❌ WebSocket 连接失败: {e}");
                }
            }

            self.set_status(ConnectionStatus::Reconnecting).await;
            let wait_secs = backoffs[backoff_idx];
            if backoff_idx + 1 < backoffs.len() {
                backoff_idx += 1;
            }
            tracing::info!("⏱️ 将在 {wait_secs} 秒后重试连接...");
            tokio::time::sleep(Duration::from_secs(wait_secs)).await;
        }
    }

    async fn handle_incoming_text(&self, text: &str) {
        if let Ok(msg) = serde_json::from_str::<WsServerMessage>(text) {
            match msg {
                WsServerMessage::Pong { client_time, server_time } => {
                    let now = chrono::Utc::now().timestamp_millis();
                    let rtt = (now - client_time).max(0) as u64;
                    self.quote_delay_ms.store(rtt, Ordering::Relaxed);
                    let _ = self.app.emit("server-pong", server_time);
                }
                WsServerMessage::Subscribed { symbols, timeframes } => {
                    tracing::info!("已订阅品种: {:?}, 周期: {:?}", symbols, timeframes);
                }
                WsServerMessage::Error { code, message } => {
                    tracing::warn!("收到服务端 WebSocket 错误 [{code}]: {message}");
                    if code == "UNAUTHORIZED" {
                        self.set_status(ConnectionStatus::Unauthorized).await;
                    }
                }
                WsServerMessage::Event { event_seq, topic, payload } => {
                    let prev_seq = self.last_event_seq.swap(event_seq, Ordering::Relaxed);
                    if prev_seq > 0 && event_seq > prev_seq + 1 {
                        tracing::warn!(
                            "⚠️ WebSocket 跳号检测: 预期序号 {}, 收到序号 {}",
                            prev_seq + 1,
                            event_seq
                        );
                        let _ = self.app.emit("sequence-gap-detected", event_seq);
                    }

                    // Forward based on topic
                    match topic.as_str() {
                        "quote.updated" => {
                            let _ = self.app.emit("quote-updated", &payload);
                        }
                        "kline.partial" => {
                            if let (Some(sym), Some(tf)) = (
                                payload.get("symbol").and_then(|v| v.as_str()),
                                payload.get("timeframe").and_then(|v| v.as_str()),
                            ) {
                                if let Some(bar_val) = payload.get("bar") {
                                    if let Ok(bar) = serde_json::from_value::<KlineDto>(bar_val.clone()) {
                                        self.kline_cache.update_partial_bar(sym, tf, bar);
                                    }
                                }
                            }
                            let _ = self.app.emit("kline-partial", &payload);
                        }
                        "kline.closed" => {
                            if let (Some(sym), Some(tf)) = (
                                payload.get("symbol").and_then(|v| v.as_str()),
                                payload.get("timeframe").and_then(|v| v.as_str()),
                            ) {
                                if let Some(bar_val) = payload.get("bar") {
                                    if let Ok(bar) = serde_json::from_value::<KlineDto>(bar_val.clone()) {
                                        self.kline_cache.merge_closed_bar(sym, tf, bar);
                                    }
                                }
                            }
                            let _ = self.app.emit("kline-closed", &payload);
                        }
                        "data.updated" => {
                            let _ = self.app.emit("data-updated", &payload);
                        }
                        "scan.completed" => {
                            let _ = self.app.emit("scan-completed", &payload);
                        }
                        "entry.triggered" => {
                            let _ = self.app.emit("entry-trigger", &payload);
                        }
                        "manual_level.triggered" => {
                            let _ = self.app.emit("manual-level-alert", &payload);
                        }
                        "notification.created" => {
                            let _ = self.app.emit("notification-created", &payload);
                        }
                        "state.changed" => {
                            let _ = self.app.emit("state-changed", &payload);
                        }
                        "server.status" => {
                            if let Some(delay) = payload.get("quoteDelayMs").and_then(|v| v.as_u64()) {
                                self.quote_delay_ms.store(delay, Ordering::Relaxed);
                            }
                            let _ = self.app.emit("server-status", &payload);
                        }
                        _ => {
                            let _ = self.app.emit(&topic, &payload);
                        }
                    }
                }
            }
        }
    }
}
