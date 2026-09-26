use crate::auth::AuthDevice;
use crate::state::ServerContext;
use axum::extract::State;
use axum::http::StatusCode;
use axum::Json;
use n_protocol::error::ApiErrorResponse;
use std::sync::Arc;

pub async fn get_market_snapshot(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
) -> Result<Json<Vec<n_core::service::MarketSnapshot>>, (StatusCode, Json<ApiErrorResponse>)> {
    match ctx.services.realtime_quotes().await {
        Ok(snapshots) => Ok(Json(snapshots)),
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("获取行情快照失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}
