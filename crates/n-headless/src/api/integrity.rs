use crate::auth::RequireAdmin;
use crate::state::ServerContext;
use axum::extract::{Query, State};
use axum::http::StatusCode;
use axum::Json;
use n_protocol::error::ApiErrorResponse;
use serde::Deserialize;
use std::sync::Arc;

#[derive(Deserialize)]
pub struct IntegrityQuery {
    pub symbol: Option<String>,
}

pub async fn check_integrity(
    State(ctx): State<Arc<ServerContext>>,
    RequireAdmin(_): RequireAdmin,
    Query(q): Query<IntegrityQuery>,
) -> Result<Json<serde_json::Value>, (StatusCode, Json<ApiErrorResponse>)> {
    if let Some(s) = q.symbol {
        match n_core::integrity::RawDataIntegrityChecker::inspect_symbol(&ctx.db, &s, 1000).await {
            Ok(report) => Ok(Json(serde_json::to_value(report).unwrap_or_default())),
            Err(e) => {
                let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("检查完整性失败: {e}"), "");
                Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
            }
        }
    } else {
        match ctx.services.check_all_symbols_integrity().await {
            Ok(reports) => Ok(Json(serde_json::to_value(reports).unwrap_or_default())),
            Err(e) => {
                let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("检查完整性失败: {e}"), "");
                Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
            }
        }
    }
}

pub async fn repair_integrity(
    State(ctx): State<Arc<ServerContext>>,
    RequireAdmin(_): RequireAdmin,
    Query(q): Query<IntegrityQuery>,
) -> Result<Json<serde_json::Value>, (StatusCode, Json<ApiErrorResponse>)> {
    if let Some(s) = q.symbol {
        match ctx.services.repair_symbol_integrity(&s).await {
            Ok(res) => Ok(Json(serde_json::to_value(res).unwrap_or_default())),
            Err(e) => {
                let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("修复完整性失败: {e}"), "");
                Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
            }
        }
    } else {
        match ctx.services.repair_all_symbols_integrity().await {
            Ok(results) => Ok(Json(serde_json::to_value(results).unwrap_or_default())),
            Err(e) => {
                let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("修复完整性失败: {e}"), "");
                Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
            }
        }
    }
}
