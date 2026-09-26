use crate::auth::AuthDevice;
use crate::state::ServerContext;
use axum::extract::{Path, State};
use axum::http::StatusCode;
use axum::response::IntoResponse;
use axum::Json;
use n_protocol::error::ApiErrorResponse;
use serde::Deserialize;
use std::sync::Arc;

#[derive(Deserialize)]
pub struct AddAnnotationReq {
    pub event_id: i64,
    pub content: String,
}

#[derive(Deserialize)]
pub struct SetDecisionReq {
    pub event_id: i64,
    pub opened: bool,
}

pub async fn get_signal_user_data(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
    Path(event_id): Path<i64>,
) -> Result<Json<n_core::service::SignalUserData>, (StatusCode, Json<ApiErrorResponse>)> {
    match ctx.services.signal_user_data(event_id).await {
        Ok(data) => Ok(Json(data)),
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("获取批注决策失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

pub async fn add_annotation(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
    Json(payload): Json<AddAnnotationReq>,
) -> Result<Json<n_core::service::SignalAnnotationDto>, (StatusCode, Json<ApiErrorResponse>)> {
    match ctx
        .services
        .add_signal_annotation(payload.event_id, &payload.content)
        .await
    {
        Ok(a) => {
            let rev = n_core::storage::repo::bump_revision(&ctx.db, "signals").await.unwrap_or(1);
            let seq = ctx.next_event_seq();
            ctx.realtime_hub.broadcast(seq, "state.changed", serde_json::json!({ "scope": "signals", "revision": rev })).await;
            Ok(Json(a))
        }
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("添加批注失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

pub async fn delete_annotation(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
    Path(id): Path<i64>,
) -> Result<impl IntoResponse, (StatusCode, Json<ApiErrorResponse>)> {
    match ctx.services.delete_signal_annotation(id).await {
        Ok(_) => {
            let rev = n_core::storage::repo::bump_revision(&ctx.db, "signals").await.unwrap_or(1);
            let seq = ctx.next_event_seq();
            ctx.realtime_hub.broadcast(seq, "state.changed", serde_json::json!({ "scope": "signals", "revision": rev })).await;
            Ok(StatusCode::NO_CONTENT)
        }
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("删除批注失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

pub async fn set_decision(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
    Json(payload): Json<SetDecisionReq>,
) -> Result<Json<n_core::service::SignalDecisionDto>, (StatusCode, Json<ApiErrorResponse>)> {
    match ctx
        .services
        .set_signal_decision(payload.event_id, payload.opened)
        .await
    {
        Ok(d) => {
            let rev = n_core::storage::repo::bump_revision(&ctx.db, "signals").await.unwrap_or(1);
            let seq = ctx.next_event_seq();
            ctx.realtime_hub.broadcast(seq, "state.changed", serde_json::json!({ "scope": "signals", "revision": rev })).await;
            Ok(Json(d))
        }
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("记录交易决策失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}
