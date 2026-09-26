use crate::auth::AuthDevice;
use crate::state::ServerContext;
use axum::extract::State;
use axum::http::StatusCode;
use axum::Json;
use n_protocol::dto::RefreshStats;
use n_protocol::error::ApiErrorResponse;
use std::sync::Arc;

pub async fn refresh_data_now(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
) -> Result<Json<RefreshStats>, (StatusCode, Json<ApiErrorResponse>)> {
    match ctx.services.refresh_data().await {
        Ok(s) => {
            *ctx.last_refresh.write().await = Some(chrono::Local::now().naive_local());
            let stats = RefreshStats {
                succeeded: s.succeeded,
                failures: s.failures,
            };
            let seq = ctx.next_event_seq();
            ctx.realtime_hub
                .broadcast(
                    seq,
                    "data.updated",
                    serde_json::to_value(&stats).unwrap_or_default(),
                )
                .await;
            Ok(Json(stats))
        }
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("刷新数据失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

pub async fn run_scan_now(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
) -> Result<Json<serde_json::Value>, (StatusCode, Json<ApiErrorResponse>)> {
    match ctx.services.run_scan().await {
        Ok(res) => {
            *ctx.last_scan.write().await = Some(chrono::Local::now().naive_local());
            let val = serde_json::to_value(&res).unwrap_or_default();
            let seq = ctx.next_event_seq();
            ctx.realtime_hub
                .broadcast(seq, "scan.completed", val.clone())
                .await;
            Ok(Json(val))
        }
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("执行扫描失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

pub async fn run_scan_fast_now(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
) -> Result<Json<serde_json::Value>, (StatusCode, Json<ApiErrorResponse>)> {
    match ctx.services.run_scan_fast().await {
        Ok(res) => {
            *ctx.last_scan.write().await = Some(chrono::Local::now().naive_local());
            let val = serde_json::to_value(&res).unwrap_or_default();
            let seq = ctx.next_event_seq();
            ctx.realtime_hub
                .broadcast(seq, "scan.completed", val.clone())
                .await;
            Ok(Json(val))
        }
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("执行快速扫描失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

pub async fn rebuild_events_now(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
) -> Result<Json<serde_json::Value>, (StatusCode, Json<ApiErrorResponse>)> {
    match ctx.services.rebuild_events().await {
        Ok(res) => {
            let val = serde_json::to_value(&res).unwrap_or_default();
            let rev = n_core::storage::repo::bump_revision(&ctx.db, "signals")
                .await
                .unwrap_or(1);
            let seq = ctx.next_event_seq();
            ctx.realtime_hub
                .broadcast(
                    seq,
                    "state.changed",
                    serde_json::json!({ "scope": "signals", "revision": rev }),
                )
                .await;
            Ok(Json(val))
        }
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("重建事件失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}
