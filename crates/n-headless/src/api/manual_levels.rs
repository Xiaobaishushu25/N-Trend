use crate::auth::AuthDevice;
use crate::state::ServerContext;
use axum::extract::{Path, Query, State};
use axum::http::StatusCode;
use axum::response::IntoResponse;
use axum::Json;
use n_protocol::dto::{ManualLevelDto, ManualLevelEventDto, ManualLevelInput};
use n_protocol::error::ApiErrorResponse;
use serde::Deserialize;
use std::sync::Arc;

#[derive(Deserialize)]
pub struct ListManualLevelsQuery {
    pub symbol: Option<String>,
    pub timeframe: Option<String>,
    pub active_only: Option<bool>,
}

#[derive(Deserialize)]
pub struct SetMonitoringReq {
    pub enabled: bool,
}

pub async fn list_manual_levels(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
    Query(q): Query<ListManualLevelsQuery>,
) -> Result<Json<Vec<ManualLevelDto>>, (StatusCode, Json<ApiErrorResponse>)> {
    match ctx
        .services
        .list_manual_levels(
            q.symbol.as_deref(),
            q.timeframe.as_deref(),
            q.active_only.unwrap_or(false),
        )
        .await
    {
        Ok(list) => {
            let dtos = list
                .into_iter()
                .map(|l| ManualLevelDto {
                    id: l.id,
                    symbol: l.symbol,
                    timeframe: l.timeframe,
                    name: l.name,
                    start_ts: l.start_ts,
                    end_ts: l.end_ts,
                    zone_low: l.zone_low,
                    zone_high: l.zone_high,
                    role: l.role,
                    role_override: l.role_override,
                    role_confidence: l.role_confidence,
                    status: l.status,
                    monitor_enabled: l.monitor_enabled,
                    current_phase: l.current_phase,
                    last_event_ts: l.last_event_ts,
                    created_at: l.created_at,
                    updated_at: l.updated_at,
                })
                .collect();
            Ok(Json(dtos))
        }
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("获取手动画线列表失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

pub async fn create_manual_level(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
    Json(input): Json<ManualLevelInput>,
) -> Result<Json<ManualLevelDto>, (StatusCode, Json<ApiErrorResponse>)> {
    let core_input = n_core::service::ManualLevelInput {
        symbol: input.symbol,
        timeframe: input.timeframe,
        name: input.name,
        start_ts: input.start_ts,
        end_ts: input.end_ts,
        zone_low: input.zone_low,
        zone_high: input.zone_high,
        role_override: input.role_override,
        monitor_enabled: input.monitor_enabled,
    };

    match ctx.services.create_manual_level(core_input).await {
        Ok(l) => {
            let rev = n_core::storage::repo::bump_revision(&ctx.db, "manual_levels").await.unwrap_or(1);
            let seq = ctx.next_event_seq();
            ctx.realtime_hub.broadcast(seq, "state.changed", serde_json::json!({ "scope": "manual_levels", "revision": rev })).await;

            Ok(Json(ManualLevelDto {
                id: l.id,
                symbol: l.symbol,
                timeframe: l.timeframe,
                name: l.name,
                start_ts: l.start_ts,
                end_ts: l.end_ts,
                zone_low: l.zone_low,
                zone_high: l.zone_high,
                role: l.role,
                role_override: l.role_override,
                role_confidence: l.role_confidence,
                status: l.status,
                monitor_enabled: l.monitor_enabled,
                current_phase: l.current_phase,
                last_event_ts: l.last_event_ts,
                created_at: l.created_at,
                updated_at: l.updated_at,
            }))
        }
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("创建手动画线失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

pub async fn update_manual_level(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
    Path(id): Path<i64>,
    Json(input): Json<ManualLevelInput>,
) -> Result<Json<ManualLevelDto>, (StatusCode, Json<ApiErrorResponse>)> {
    let core_input = n_core::service::ManualLevelInput {
        symbol: input.symbol,
        timeframe: input.timeframe,
        name: input.name,
        start_ts: input.start_ts,
        end_ts: input.end_ts,
        zone_low: input.zone_low,
        zone_high: input.zone_high,
        role_override: input.role_override,
        monitor_enabled: input.monitor_enabled,
    };

    match ctx.services.update_manual_level(id, core_input).await {
        Ok(l) => {
            let rev = n_core::storage::repo::bump_revision(&ctx.db, "manual_levels").await.unwrap_or(1);
            let seq = ctx.next_event_seq();
            ctx.realtime_hub.broadcast(seq, "state.changed", serde_json::json!({ "scope": "manual_levels", "revision": rev })).await;

            Ok(Json(ManualLevelDto {
                id: l.id,
                symbol: l.symbol,
                timeframe: l.timeframe,
                name: l.name,
                start_ts: l.start_ts,
                end_ts: l.end_ts,
                zone_low: l.zone_low,
                zone_high: l.zone_high,
                role: l.role,
                role_override: l.role_override,
                role_confidence: l.role_confidence,
                status: l.status,
                monitor_enabled: l.monitor_enabled,
                current_phase: l.current_phase,
                last_event_ts: l.last_event_ts,
                created_at: l.created_at,
                updated_at: l.updated_at,
            }))
        }
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("更新手动画线失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

pub async fn set_manual_level_monitoring(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
    Path(id): Path<i64>,
    Json(payload): Json<SetMonitoringReq>,
) -> Result<Json<ManualLevelDto>, (StatusCode, Json<ApiErrorResponse>)> {
    match ctx.services.set_manual_level_monitoring(id, payload.enabled).await {
        Ok(l) => {
            let rev = n_core::storage::repo::bump_revision(&ctx.db, "manual_levels").await.unwrap_or(1);
            let seq = ctx.next_event_seq();
            ctx.realtime_hub.broadcast(seq, "state.changed", serde_json::json!({ "scope": "manual_levels", "revision": rev })).await;

            Ok(Json(ManualLevelDto {
                id: l.id,
                symbol: l.symbol,
                timeframe: l.timeframe,
                name: l.name,
                start_ts: l.start_ts,
                end_ts: l.end_ts,
                zone_low: l.zone_low,
                zone_high: l.zone_high,
                role: l.role,
                role_override: l.role_override,
                role_confidence: l.role_confidence,
                status: l.status,
                monitor_enabled: l.monitor_enabled,
                current_phase: l.current_phase,
                last_event_ts: l.last_event_ts,
                created_at: l.created_at,
                updated_at: l.updated_at,
            }))
        }
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("切换监控状态失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

pub async fn archive_manual_level(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
    Path(id): Path<i64>,
) -> Result<impl IntoResponse, (StatusCode, Json<ApiErrorResponse>)> {
    match ctx.services.archive_manual_level(id).await {
        Ok(_) => {
            let rev = n_core::storage::repo::bump_revision(&ctx.db, "manual_levels").await.unwrap_or(1);
            let seq = ctx.next_event_seq();
            ctx.realtime_hub.broadcast(seq, "state.changed", serde_json::json!({ "scope": "manual_levels", "revision": rev })).await;
            Ok(StatusCode::OK)
        }
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("归档关键区域失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

pub async fn delete_manual_level(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
    Path(id): Path<i64>,
) -> Result<impl IntoResponse, (StatusCode, Json<ApiErrorResponse>)> {
    match ctx.services.delete_manual_level(id).await {
        Ok(_) => {
            let rev = n_core::storage::repo::bump_revision(&ctx.db, "manual_levels").await.unwrap_or(1);
            let seq = ctx.next_event_seq();
            ctx.realtime_hub.broadcast(seq, "state.changed", serde_json::json!({ "scope": "manual_levels", "revision": rev })).await;
            Ok(StatusCode::NO_CONTENT)
        }
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("删除关键区域失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

pub async fn get_manual_level_events(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
    Path(id): Path<i64>,
) -> Result<Json<Vec<ManualLevelEventDto>>, (StatusCode, Json<ApiErrorResponse>)> {
    match ctx.services.manual_level_events(id).await {
        Ok(events) => {
            let dtos = events
                .into_iter()
                .map(|e| ManualLevelEventDto {
                    id: e.id,
                    level_id: e.level_id,
                    symbol: e.symbol,
                    timeframe: e.timeframe,
                    event_type: e.event_type,
                    role: e.role,
                    bar_ts: e.bar_ts,
                    price: e.price,
                    reason: e.reason,
                    phase: e.phase,
                    role_confidence: e.role_confidence,
                    volume_ratio: e.volume_ratio,
                    created_at: e.created_at,
                })
                .collect();
            Ok(Json(dtos))
        }
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("获取画线事件历史失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}
