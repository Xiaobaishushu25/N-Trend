use crate::auth::AuthDevice;
use crate::state::ServerContext;
use axum::extract::{Query, State};
use axum::http::StatusCode;
use axum::response::IntoResponse;
use axum::Json;
use n_protocol::error::ApiErrorResponse;
use n_protocol::{NewNotificationHistoryItem, NotificationHistoryItem};
use serde::Deserialize;
use std::sync::Arc;

#[derive(Deserialize)]
pub struct NotificationQuery {
    pub limit: Option<u64>,
}

#[derive(Deserialize)]
pub struct MarkReadReq {
    pub up_to_id: Option<u64>,
}

pub async fn list_notifications(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
    Query(q): Query<NotificationQuery>,
) -> Result<Json<Vec<NotificationHistoryItem>>, (StatusCode, Json<ApiErrorResponse>)> {
    let limit = q.limit.unwrap_or(40).min(200);
    match n_core::storage::repo::list_notification_history(&ctx.db, limit).await {
        Ok(items) => Ok(Json(items)),
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("获取通知历史失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

pub async fn record_notification(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
    Json(item): Json<NewNotificationHistoryItem>,
) -> Result<Json<NotificationHistoryItem>, (StatusCode, Json<ApiErrorResponse>)> {
    match n_core::storage::repo::insert_notification_history(&ctx.db, &item).await {
        Ok(saved) => {
            let seq = ctx.next_event_seq();
            ctx.realtime_hub
                .broadcast(
                    seq,
                    "notification.created",
                    serde_json::to_value(&saved).unwrap_or_default(),
                )
                .await;
            Ok(Json(saved))
        }
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("记录通知失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

pub async fn mark_notifications_read(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
    Json(payload): Json<MarkReadReq>,
) -> Result<impl IntoResponse, (StatusCode, Json<ApiErrorResponse>)> {
    match n_core::storage::repo::mark_notifications_read(&ctx.db, payload.up_to_id).await {
        Ok(_) => Ok(StatusCode::OK),
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("标记已读失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

pub async fn clear_notifications(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
) -> Result<impl IntoResponse, (StatusCode, Json<ApiErrorResponse>)> {
    match n_core::storage::repo::clear_notification_history(&ctx.db).await {
        Ok(_) => Ok(StatusCode::NO_CONTENT),
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("清空通知失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}
