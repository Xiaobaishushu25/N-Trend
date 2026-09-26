pub mod actions;
pub mod admin;
pub mod annotations;
pub mod devices;
pub mod integrity;
pub mod klines;
pub mod manual_levels;
pub mod meta;
pub mod notifications;
pub mod quotes;
pub mod settings;
pub mod signals;
pub mod symbols;
pub mod ws_handler;

use crate::state::ServerContext;
use axum::extract::DefaultBodyLimit;
use axum::routing::{delete, get, post, put};
use axum::Router;
use std::sync::Arc;
use tower_http::cors::CorsLayer;

pub fn build_router(ctx: Arc<ServerContext>) -> Router {
    let api_v1 = Router::new()
        // 元数据与探活
        .route("/meta", get(meta::get_meta))
        .route("/status", get(meta::get_status))
        // WebSocket 实时流
        .route("/ws", get(ws_handler::ws_handler))
        // 设备管理
        .route("/devices/register", post(devices::register_device))
        .route("/devices", get(devices::list_devices))
        .route("/devices/:id", delete(devices::revoke_device))
        .route("/devices/:id/role", put(devices::update_device_role))
        // 品种与分组
        .route("/symbols", get(symbols::get_symbols).post(symbols::add_symbol))
        .route("/symbols/:code", delete(symbols::remove_symbol))
        .route("/symbols/:code/groups", get(symbols::get_symbol_groups))
        .route("/symbols/reorder", post(symbols::reorder_symbols))
        .route("/symbols/flags", post(symbols::set_symbol_flags))
        .route("/symbols/tick", post(symbols::set_symbol_tick))
        .route("/symbols/followed", post(symbols::set_symbol_followed))
        .route("/symbols/refresh", post(symbols::refresh_symbol_list))
        .route("/symbols/enrich", post(symbols::enrich_symbol_names))
        .route("/contracts/search", get(symbols::search_contracts))
        .route("/groups", get(symbols::list_groups).post(symbols::create_group))
        .route(
            "/groups/:id",
            put(symbols::rename_group).delete(symbols::delete_group),
        )
        .route("/groups/reorder", post(symbols::reorder_groups))
        .route("/groups/all-position", get(symbols::get_group_all_position))
        .route(
            "/groups/:id/symbols",
            get(symbols::get_group_symbols).post(symbols::add_symbol_to_group),
        )
        .route(
            "/groups/:id/symbols/:code",
            delete(symbols::remove_symbol_from_group),
        )
        .route(
            "/groups/:id/reorder-symbols",
            post(symbols::reorder_group_symbols),
        )
        // K 线与趋势线
        .route(
            "/klines/:symbol/:timeframe",
            get(klines::get_klines),
        )
        .route(
            "/chart-klines/:symbol/:timeframe",
            get(klines::get_chart_klines),
        )
        .route(
            "/trend-series/:symbol/:timeframe",
            get(klines::get_trend_series),
        )
        // 实时行情
        .route("/quotes", get(quotes::get_market_snapshot))
        // 信号与复盘
        .route("/signals/active", get(signals::get_active_events))
        .route("/signals/preclose", get(signals::get_preclose_signals))
        .route(
            "/signals/preclose-candidates",
            get(signals::get_preclose_candidates),
        )
        .route("/review/stats", get(signals::get_review_stats))
        .route("/review/outcomes", get(signals::get_recent_outcomes))
        .route("/review/signal/:id", get(signals::get_review_signal))
        .route(
            "/review/outcomes/refresh",
            post(signals::refresh_outcomes),
        )
        .route("/v2/models", get(signals::get_v2_models))
        .route("/v2/models/status", post(signals::set_v2_model_status))
        .route("/v2/predictions", get(signals::get_v2_predictions))
        .route(
            "/v2/predictions/backfill",
            post(signals::backfill_v2_predictions),
        )
        .route("/v2/report", get(signals::get_v2_report))
        // 关键区域手动画线
        .route(
            "/manual-levels",
            get(manual_levels::list_manual_levels).post(manual_levels::create_manual_level),
        )
        .route(
            "/manual-levels/:id",
            put(manual_levels::update_manual_level).delete(manual_levels::delete_manual_level),
        )
        .route(
            "/manual-levels/:id/monitoring",
            post(manual_levels::set_manual_level_monitoring),
        )
        .route(
            "/manual-levels/:id/archive",
            post(manual_levels::archive_manual_level),
        )
        .route(
            "/manual-levels/:id/events",
            get(manual_levels::get_manual_level_events),
        )
        // 批注与决策
        .route(
            "/annotations/:id",
            get(annotations::get_signal_user_data).delete(annotations::delete_annotation),
        )
        .route("/annotations", post(annotations::add_annotation))
        .route("/decisions", post(annotations::set_decision))
        // 通知历史
        .route(
            "/notifications",
            get(notifications::list_notifications)
                .post(notifications::record_notification)
                .delete(notifications::clear_notifications),
        )
        .route(
            "/notifications/read",
            post(notifications::mark_notifications_read),
        )
        // 服务端配置
        .route(
            "/settings/server",
            get(settings::get_server_settings).put(settings::update_server_settings),
        )
        .route("/settings/last-group", post(settings::set_last_group))
        .route("/settings/timeframes", post(settings::set_timeframes))
        .route("/settings/reset", post(settings::reset_config))
        // 管理与控制
        .route("/admin/bridge/restart", post(admin::restart_bridge))
        .route("/admin/runtime/restart", post(admin::restart_runtime))
        .route("/admin/runtime/status", get(admin::get_scheduler_status))
        .route("/admin/runtime/scheduler", post(admin::set_scheduler_running))
        .route(
            "/admin/database-backup",
            get(admin::download_database_backup),
        )
        // 手动触发操作
        .route("/actions/refresh", post(actions::refresh_data_now))
        .route("/actions/scan", post(actions::run_scan_now))
        .route("/actions/scan-fast", post(actions::run_scan_fast_now))
        .route(
            "/actions/rebuild-events",
            post(actions::rebuild_events_now),
        )
        // 数据完整性
        .route("/integrity", get(integrity::check_integrity))
        .route("/integrity/repair", post(integrity::repair_integrity));

    Router::new()
        .route("/healthz", get(meta::healthz))
        .nest("/api/v1", api_v1)
        .layer(DefaultBodyLimit::max(50 * 1024 * 1024))
        .layer(CorsLayer::permissive())
        .layer(axum::middleware::from_fn(log_http_request))
        .with_state(ctx)
}

async fn log_http_request(
    req: axum::extract::Request,
    next: axum::middleware::Next,
) -> axum::response::Response {
    let method = req.method().clone();
    let uri = req.uri().clone();
    let path = uri.path().to_string();
    let t0 = std::time::Instant::now();

    let resp = next.run(req).await;

    let elapsed = t0.elapsed().as_millis();
    let status = resp.status().as_u16();

    // 过滤高频健康探测请求避免日志冗余
    if path != "/healthz" {
        if status >= 400 {
            tracing::warn!("📥 [HTTP] {} {} -> {} ({}ms)", method, path, status, elapsed);
        } else {
            tracing::info!("📥 [HTTP] {} {} -> {} ({}ms)", method, path, status, elapsed);
        }
    }

    resp
}
