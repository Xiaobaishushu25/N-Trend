use crate::auth::AuthDevice;
use crate::state::ServerContext;
use axum::extract::ws::{Message, WebSocket, WebSocketUpgrade};
use axum::extract::State;
use axum::response::IntoResponse;
use futures_util::{SinkExt, StreamExt};
use n_protocol::ws::{WsClientMessage, WsServerMessage};
use std::sync::Arc;

pub async fn ws_handler(
    ws: WebSocketUpgrade,
    State(ctx): State<Arc<ServerContext>>,
    device: AuthDevice,
) -> impl IntoResponse {
    ws.on_upgrade(move |socket| handle_socket(socket, ctx, device))
}

async fn handle_socket(socket: WebSocket, ctx: Arc<ServerContext>, device: AuthDevice) {
    let (client_id, mut rx) = ctx
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
        while let Some(msg) = rx.recv().await {
            if sender.send(msg).await.is_err() {
                break;
            }
        }
    });

    // 任务2：接收客户端上行消息（心跳、订阅）
    let hub_clone = ctx.realtime_hub.clone();
    let device_name = device.name.clone();
    let recv_task = tokio::spawn(async move {
        while let Some(Ok(msg)) = receiver.next().await {
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
                                    .update_subscription(client_id, symbols.clone(), timeframes.clone())
                                    .await;
                                let ack = WsServerMessage::Subscribed {
                                    symbols,
                                    timeframes,
                                };
                                if let Ok(reply) = serde_json::to_string(&ack) {
                                    hub_clone.send_to(client_id, Message::Text(reply)).await;
                                }
                            }
                            WsClientMessage::Unsubscribe { symbols, timeframes } => {
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

    // 等待任一任务结束
    tokio::select! {
        _ = send_task => {},
        _ = recv_task => {},
    };

    ctx.realtime_hub.unregister_client(client_id).await;
    tracing::info!(
        "🔌 WebSocket 客户端断开连接 | ClientID: {} | 设备: {}",
        client_id,
        device.name
    );
}
