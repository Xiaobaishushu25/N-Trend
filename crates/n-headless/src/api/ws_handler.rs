use crate::auth::AuthDevice;
use crate::state::ServerContext;
use axum::extract::ws::{Message, WebSocket, WebSocketUpgrade};
use axum::extract::State;
use axum::response::IntoResponse;
use futures_util::{SinkExt, StreamExt};
use n_protocol::ws::{WsClientMessage, WsServerMessage};
use std::sync::Arc;
use std::time::Duration;

pub async fn ws_handler(
    ws: WebSocketUpgrade,
    State(ctx): State<Arc<ServerContext>>,
    device: AuthDevice,
) -> impl IntoResponse {
    ws.on_upgrade(move |socket| handle_socket(socket, ctx, device))
}

async fn handle_socket(socket: WebSocket, ctx: Arc<ServerContext>, device: AuthDevice) {
    let (client_id, mut rx, disconnect) = ctx
        .realtime_hub
        .register_client(device.id.clone(), device.role.clone())
        .await;

    tracing::info!(
        "🔌 WebSocket 客户端已建立连接 | ClientID: {} | 设备: {} ({})",
        client_id,
        device.name,
        device.role
    );

    let (mut sender, mut receiver) = socket.split();

    // 任务1：从 RealtimeHub 转发消息给客户端
    let send_task = tokio::spawn(async move {
        while let Some(queued) = rx.recv().await {
            let (msg, _permit) = queued.into_parts();
            match tokio::time::timeout(Duration::from_secs(10), sender.send(msg)).await {
                Ok(Ok(())) => {}
                _ => break,
            }
        }
    });

    // 任务2：接收客户端上行消息（心跳、订阅）
    let hub_clone = ctx.realtime_hub.clone();
    let device_name = device.name.clone();
    let recv_task = tokio::spawn(async move {
        while let Ok(Some(Ok(msg))) =
            tokio::time::timeout(Duration::from_secs(90), receiver.next()).await
        {
            match msg {
                Message::Text(text) => {
                    if let Ok(client_msg) = serde_json::from_str::<WsClientMessage>(&text) {
                        match client_msg {
                            WsClientMessage::Subscribe {
                                symbols,
                                timeframes,
                            } => {
                                tracing::info!(
                                    "📡 WebSocket 客户端 [{}] ({}) 订阅品种: {:?}, 周期: {:?}",
                                    client_id,
                                    device_name,
                                    symbols,
                                    timeframes
                                );
                                hub_clone
                                    .update_subscription(
                                        client_id,
                                        symbols.clone(),
                                        timeframes.clone(),
                                    )
                                    .await;
                                let ack = WsServerMessage::Subscribed {
                                    symbols,
                                    timeframes,
                                };
                                if let Ok(reply) = serde_json::to_string(&ack) {
                                    hub_clone.send_to(client_id, Message::Text(reply)).await;
                                }
                            }
                            WsClientMessage::Unsubscribe {
                                symbols,
                                timeframes,
                            } => {
                                tracing::info!(
                                    "📡 WebSocket 客户端 [{}] ({}) 退订品种: {:?}, 周期: {:?}",
                                    client_id,
                                    device_name,
                                    symbols,
                                    timeframes
                                );
                            }
                            WsClientMessage::Ping { client_time } => {
                                let pong = WsServerMessage::Pong {
                                    client_time,
                                    server_time: chrono::Local::now().timestamp_millis(),
                                };
                                if let Ok(reply) = serde_json::to_string(&pong) {
                                    hub_clone.send_to(client_id, Message::Text(reply)).await;
                                }
                            }
                        }
                    }
                }
                Message::Close(_) => {
                    break;
                }
                _ => {}
            }
        }
    });

    finish_connection(send_task, recv_task, disconnect).await;
    ctx.realtime_hub.unregister_client(client_id).await;
    tracing::info!(
        "🔌 WebSocket 客户端断开连接 | ClientID: {} | 设备: {}",
        client_id,
        device.name
    );
}

async fn finish_connection(
    mut send_task: tokio::task::JoinHandle<()>,
    mut recv_task: tokio::task::JoinHandle<()>,
    disconnect: Arc<tokio::sync::Notify>,
) {
    tokio::select! {
        _ = &mut send_task => {},
        _ = &mut recv_task => {},
        _ = disconnect.notified() => {},
    };

    // Dropping a JoinHandle detaches its task. Explicitly stop the other half so
    // blocked sockets, their queues and captured server state are released.
    if !send_task.is_finished() {
        send_task.abort();
        let _ = send_task.await;
    }
    if !recv_task.is_finished() {
        recv_task.abort();
        let _ = recv_task.await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use tokio::sync::Notify;

    struct Dropped(Arc<AtomicUsize>);
    impl Drop for Dropped {
        fn drop(&mut self) {
            self.0.fetch_add(1, Ordering::SeqCst);
        }
    }

    fn blocked_task(dropped: Arc<AtomicUsize>) -> tokio::task::JoinHandle<()> {
        let guard = Dropped(dropped);
        tokio::spawn(async move {
            let _guard = guard;
            std::future::pending::<()>().await;
        })
    }

    #[tokio::test]
    async fn finished_sender_cancels_blocked_receiver() {
        let dropped = Arc::new(AtomicUsize::new(0));
        finish_connection(
            tokio::spawn(async {}),
            blocked_task(dropped.clone()),
            Arc::new(Notify::new()),
        )
        .await;
        assert_eq!(dropped.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn finished_receiver_cancels_blocked_sender() {
        let dropped = Arc::new(AtomicUsize::new(0));
        finish_connection(
            blocked_task(dropped.clone()),
            tokio::spawn(async {}),
            Arc::new(Notify::new()),
        )
        .await;
        assert_eq!(dropped.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn queue_overflow_cancels_both_blocked_socket_halves() {
        let dropped = Arc::new(AtomicUsize::new(0));
        let disconnect = Arc::new(Notify::new());
        disconnect.notify_one();
        finish_connection(
            blocked_task(dropped.clone()),
            blocked_task(dropped.clone()),
            disconnect,
        )
        .await;
        assert_eq!(dropped.load(Ordering::SeqCst), 2);
    }
}
