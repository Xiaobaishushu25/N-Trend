use crate::auth::AuthDevice;
use crate::state::ServerContext;
use axum::extract::State;
use axum::http::StatusCode;
use axum::response::IntoResponse;
use axum::Json;
use n_protocol::dto::{MetaDto, ServerStatusDto};
use std::sync::Arc;

pub async fn healthz() -> impl IntoResponse {
    (StatusCode::OK, "OK")
}

pub async fn get_meta() -> impl IntoResponse {
    let now = chrono::Local::now().timestamp_millis();
    let meta = MetaDto {
        api_version: n_protocol::API_VERSION.to_string(),
        server_version: env!("CARGO_PKG_VERSION").to_string(),
        min_client_version: n_protocol::MIN_CLIENT_VERSION.to_string(),
        server_time: now,
    };
    Json(meta)
}

pub async fn get_status(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
) -> impl IntoResponse {
    let tq_available = ctx.services.data_source.tq_is_available();
    let symbols = n_core::storage::repo::list_symbols(&ctx.db, false)
        .await
        .map(|v| v.len())
        .unwrap_or(0);
    let last_refresh = ctx
        .last_refresh
        .read()
        .await
        .map(|t| t.format("%Y-%m-%d %H:%M:%S").to_string());
    let last_scan = ctx
        .last_scan
        .read()
        .await
        .map(|t| t.format("%Y-%m-%d %H:%M:%S").to_string());
    let active_connections = ctx.realtime_hub.active_connections().await;

    let status = ServerStatusDto {
        tq_available,
        db_available: true,
        last_refresh,
        last_scan,
        quote_delay_ms: None,
        symbol_count: symbols,
        uptime_secs: ctx.uptime_secs(),
        active_connections,
    };
    Json(status)
}
