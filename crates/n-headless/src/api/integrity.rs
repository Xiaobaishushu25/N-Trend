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
        match n_core::integrity::RawDataIntegrityChecker::inspect_symbol_with_revision(
            &ctx.db, &s, 1000,
        )
        .await
        {
            Ok((report, revision)) => {
                let mut report = serde_json::to_value(report).unwrap_or_default();
                report["history_revision"] = serde_json::json!(revision);
                Ok(Json(report))
            }
            Err(e) => {
                let err =
                    ApiErrorResponse::new("INTERNAL_ERROR", format!("检查完整性失败: {e}"), "");
                Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
            }
        }
    } else {
        match ctx.services.check_all_symbols_integrity().await {
            Ok(reports) => Ok(Json(serde_json::to_value(reports).unwrap_or_default())),
            Err(e) => {
                let err =
                    ApiErrorResponse::new("INTERNAL_ERROR", format!("检查完整性失败: {e}"), "");
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
            Ok(res) => {
                let revision = if res.repaired_count > 0 {
                    invalidate_chart_history(&ctx, &s).await
                } else {
                    None
                };
                let mut result = serde_json::to_value(res).unwrap_or_default();
                if let Some(revision) = revision {
                    result["history_revision"] = serde_json::json!(revision);
                }
                Ok(Json(result))
            }
            Err(e) => {
                let err =
                    ApiErrorResponse::new("INTERNAL_ERROR", format!("修复完整性失败: {e}"), "");
                Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
            }
        }
    } else {
        match ctx.services.repair_all_symbols_integrity().await {
            Ok(results) => {
                for result in results.iter().filter(|result| result.repaired_count > 0) {
                    invalidate_chart_history(&ctx, &result.symbol).await;
                }
                Ok(Json(serde_json::to_value(results).unwrap_or_default()))
            }
            Err(e) => {
                let err =
                    ApiErrorResponse::new("INTERNAL_ERROR", format!("修复完整性失败: {e}"), "");
                Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
            }
        }
    }
}

async fn invalidate_chart_history(ctx: &ServerContext, symbol: &str) -> Option<i64> {
    let revision = n_core::storage::cache_revisions::market_history_revision(&ctx.db, symbol)
        .await
        .ok();
    ctx.realtime_hub
        .broadcast_chart_history(
            ctx.next_event_seq(),
            symbol,
            serde_json::json!({
                "symbol": symbol, "series_revision": revision, "full_reload_required": true,
            }),
        )
        .await;
    revision
}
