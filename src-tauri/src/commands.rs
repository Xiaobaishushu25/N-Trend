use n_protocol::auth::*;
use n_protocol::config::*;
use n_protocol::dto::*;
use serde_json::Value;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use tauri::{AppHandle, Emitter, Manager, State};

use crate::client::{AuthRecord, BackupStatus, ConnectionStateDto, ConnectionStatus};
use crate::state::AppState;

static LOCAL_TRADER_CONFIG: Mutex<Option<Value>> = Mutex::new(None);
static CHART_REQUEST_SEQ: AtomicU64 = AtomicU64::new(1);

pub(crate) fn local_trader_config() -> Value {
    LOCAL_TRADER_CONFIG
        .lock()
        .unwrap()
        .clone()
        .unwrap_or_else(|| {
            serde_json::json!({
                "mode": "SemiAuto", "max_risk_percent": 1.0,
                "default_large_volume": 2, "default_small_volume": 1, "daily_loss_limit_r": 3.0
            })
        })
}

pub(crate) fn prepare_local_trader_snapshot(snapshot: &mut Value) {
    snapshot["config"] = local_trader_config();
    snapshot["source"] = serde_json::json!("local");
    snapshot["bridge_online"] = serde_json::json!(true);
    for key in ["managed_positions", "conditions", "history"] {
        snapshot[key] = serde_json::json!([]);
    }
    snapshot["statistics"] = Value::Null;
    snapshot["strategy_comparison"] = Value::Null;
}

#[tauri::command]
pub fn report_frontend_stage(window: tauri::Window, stage: String, elapsed_ms: f64) {
    if matches!(stage.as_str(), "entry_evaluated" | "app_mounted" | "route_ready")
        && elapsed_ms.is_finite()
    {
        tracing::info!(window = window.label(), stage, elapsed_ms, "前端启动阶段");
    }
}

#[tauri::command]
pub fn report_frontend_perf(
    window: tauri::Window,
    stage: String,
    elapsed_ms: f64,
    symbol: Option<String>,
    timeframe: Option<String>,
    rows: Option<usize>,
) {
    if !matches!(stage.as_str(),
        "layout_mounted" | "layout_events_ready" | "settings_loaded" | "settings_load_failed" | "symbols_loaded" | "symbols_load_failed" |
        "route_navigation_started" | "route_navigation_finished" | "route_navigation_failed" |
        "dashboard_mounted" | "dashboard_settings_ready" | "dashboard_events_ready" | "dashboard_load_started" | "dashboard_symbols_loaded" | "dashboard_rows_ready" | "dashboard_data_loaded" | "dashboard_load_finished" | "dashboard_load_failed" |
        "chart_load_started" | "chart_load_finished" | "chart_load_failed" | "chart_load_superseded" |
        "chart_created" | "chart_render_finished") || !elapsed_ms.is_finite() || elapsed_ms < 0.0 {
        return;
    }
    let symbol = symbol.as_deref().unwrap_or("");
    let timeframe = timeframe.as_deref().unwrap_or("");
    if symbol.len() > 32 || timeframe.len() > 16 || !symbol.is_ascii() || !timeframe.is_ascii() {
        return;
    }
    tracing::info!(window = window.label(), stage, elapsed_ms, symbol, timeframe, rows, "前端关键阶段");
}

#[tauri::command]
pub fn report_frontend_error(window: tauri::Window, stage: String, message: String) {
    if !matches!(stage.as_str(), "javascript_error" | "unhandled_rejection" | "router_error" | "vue_error") {
        return;
    }
    let message: String = message.chars().filter(|c| !c.is_control()).take(500).collect();
    tracing::error!(window = window.label(), stage, message, "前端执行异常");
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct AppInfo {
    pub name: String,
    pub version: String,
    pub platform: String,
    pub is_mobile: bool,
}

#[tauri::command]
pub async fn app_info() -> AppInfo {
    let platform = if cfg!(target_os = "android") {
        "android"
    } else if cfg!(target_os = "windows") {
        "windows"
    } else if cfg!(target_os = "macos") {
        "macos"
    } else if cfg!(target_os = "ios") {
        "ios"
    } else {
        "linux"
    };
    let is_mobile = cfg!(any(target_os = "android", target_os = "ios"));
    AppInfo {
        name: "ntrend".to_string(),
        version: env!("CARGO_PKG_VERSION").to_string(),
        platform: platform.to_string(),
        is_mobile,
    }
}

#[tauri::command]
pub async fn set_window_size(window: tauri::Window, width: f64, height: f64) -> Result<(), String> {
    #[cfg(desktop)]
    if let Ok(is_max) = window.is_maximized() {
        if is_max {
            let _ = window.unmaximize();
        }
    }
    window
        .set_size(tauri::LogicalSize::new(width, height))
        .map_err(|e| e.to_string())
}

// ── Connection & Device Auth ──

#[tauri::command]
pub async fn get_connection_status(
    state: State<'_, Arc<AppState>>,
) -> Result<ConnectionStateDto, String> {
    Ok(state.realtime.get_state().await)
}

#[tauri::command]
pub async fn get_auth_record(state: State<'_, Arc<AppState>>) -> Result<AuthRecord, String> {
    Ok(state.credentials.get_record())
}

#[tauri::command]
pub async fn update_auth_record(
    state: State<'_, Arc<AppState>>,
    record: AuthRecord,
) -> Result<(), String> {
    let mut record = record;
    record.server_url = record.server_url.trim().trim_end_matches('/').to_string();
    state.credentials.update_auth(record)?;
    state.realtime.reconnect();
    Ok(())
}

#[tauri::command]
pub async fn get_client_settings(
    state: State<'_, Arc<AppState>>,
) -> Result<ClientLocalSettings, String> {
    Ok(state.local_settings.read().await.clone())
}

#[tauri::command]
pub async fn update_client_settings(
    state: State<'_, Arc<AppState>>,
    settings: ClientLocalSettings,
) -> Result<(), String> {
    *state.local_settings.write().await = settings;
    state.save_local_settings().await
}

#[tauri::command]
pub async fn get_meta(state: State<'_, Arc<AppState>>) -> Result<MetaDto, String> {
    state.api.get_meta().await.map_err(|e| e.message)
}

#[tauri::command]
pub async fn get_server_status(state: State<'_, Arc<AppState>>) -> Result<ServerStatusDto, String> {
    state.api.get_server_status().await.map_err(|e| e.message)
}

// ── Notifications ──

#[tauri::command]
pub async fn record_notification(
    app: AppHandle,
    state: State<'_, Arc<AppState>>,
    item: NewNotificationHistoryItem,
) -> Result<Vec<NotificationHistoryItem>, String> {
    let _local = state.record_local_notification(item.clone());
    // Also record on server asynchronously
    let api = state.api.clone();
    tauri::async_runtime::spawn(async move {
        let _ = api.record_notification(&item).await;
    });
    let list = state.local_notifications();
    let _ = app.emit("notification-history-updated", &list);
    Ok(list)
}

#[tauri::command]
pub async fn get_notification_history(
    state: State<'_, Arc<AppState>>,
) -> Result<Vec<NotificationHistoryItem>, String> {
    match state.api.get_notifications(Some(40), false).await {
        Ok(items) => Ok(items),
        Err(_) => Ok(state.local_notifications()),
    }
}

// ── Symbols & Groups ──

#[tauri::command]
pub async fn get_symbols(state: State<'_, Arc<AppState>>) -> Result<Vec<SymbolDto>, String> {
    state.api.get_symbols().await.map_err(|e| e.message)
}

#[tauri::command]
pub async fn list_groups(state: State<'_, Arc<AppState>>) -> Result<Vec<GroupDto>, String> {
    state.api.list_groups().await.map_err(|e| e.message)
}

#[tauri::command]
pub async fn create_group(
    state: State<'_, Arc<AppState>>,
    name: String,
) -> Result<GroupDto, String> {
    state.api.create_group(&name).await.map_err(|e| e.message)
}

#[tauri::command]
pub async fn rename_group(
    state: State<'_, Arc<AppState>>,
    id: i64,
    name: String,
) -> Result<(), String> {
    state
        .api
        .rename_group(id, &name)
        .await
        .map_err(|e| e.message)
}

#[tauri::command]
pub async fn delete_group(state: State<'_, Arc<AppState>>, id: i64) -> Result<(), String> {
    state.api.delete_group(id).await.map_err(|e| e.message)
}

#[tauri::command]
pub async fn get_group_symbols(
    state: State<'_, Arc<AppState>>,
    group_id: i64,
) -> Result<Vec<SymbolDto>, String> {
    state
        .api
        .get_group_symbols(group_id)
        .await
        .map_err(|e| e.message)
}

#[tauri::command]
pub async fn list_symbol_groups(
    state: State<'_, Arc<AppState>>,
    symbol: String,
) -> Result<Vec<GroupDto>, String> {
    state
        .api
        .list_symbol_groups(&symbol)
        .await
        .map_err(|e| e.message)
}

#[tauri::command]
pub async fn add_symbol_to_group(
    state: State<'_, Arc<AppState>>,
    symbol: String,
    group_id: i64,
) -> Result<(), String> {
    state
        .api
        .add_symbol_to_group(group_id, &symbol)
        .await
        .map_err(|e| e.message)
}

#[tauri::command]
pub async fn remove_symbol_from_group(
    state: State<'_, Arc<AppState>>,
    symbol: String,
    group_id: i64,
) -> Result<(), String> {
    state
        .api
        .remove_symbol_from_group(group_id, &symbol)
        .await
        .map_err(|e| e.message)
}

#[tauri::command]
pub async fn reorder_groups(
    state: State<'_, Arc<AppState>>,
    ids: Vec<i64>,
    all_position: i64,
) -> Result<(), String> {
    state
        .api
        .reorder_groups(&ids, all_position)
        .await
        .map_err(|e| e.message)
}

#[tauri::command]
pub async fn get_group_all_position(state: State<'_, Arc<AppState>>) -> Result<i64, String> {
    state
        .api
        .get_group_all_position()
        .await
        .map_err(|e| e.message)
}

#[tauri::command]
pub async fn reorder_group_symbols(
    state: State<'_, Arc<AppState>>,
    group_id: i64,
    codes: Vec<String>,
) -> Result<(), String> {
    state
        .api
        .reorder_group_symbols(group_id, &codes)
        .await
        .map_err(|e| e.message)
}

#[tauri::command]
pub async fn reorder_symbols(
    state: State<'_, Arc<AppState>>,
    codes: Vec<String>,
) -> Result<(), String> {
    state
        .api
        .reorder_symbols(&codes)
        .await
        .map_err(|e| e.message)
}

#[tauri::command]
pub async fn add_symbol(state: State<'_, Arc<AppState>>, code: String) -> Result<usize, String> {
    state.api.add_symbol(&code).await.map_err(|e| e.message)
}

#[tauri::command]
pub async fn search_contracts(
    state: State<'_, Arc<AppState>>,
    keyword: String,
) -> Result<Vec<ContractSuggestionDto>, String> {
    state
        .api
        .search_contracts(&keyword)
        .await
        .map_err(|e| e.message)
}

#[tauri::command]
pub async fn remove_symbol(state: State<'_, Arc<AppState>>, code: String) -> Result<(), String> {
    state.api.remove_symbol(&code).await.map_err(|e| e.message)
}

#[tauri::command]
pub async fn set_symbol_flags(
    state: State<'_, Arc<AppState>>,
    code: String,
    watchlist: bool,
    enabled: bool,
) -> Result<(), String> {
    state
        .api
        .set_symbol_flags(&code, watchlist, enabled)
        .await
        .map_err(|e| e.message)
}

#[tauri::command]
pub async fn set_symbol_tick(
    state: State<'_, Arc<AppState>>,
    code: String,
    tick: f64,
) -> Result<(), String> {
    state
        .api
        .set_symbol_tick(&code, tick)
        .await
        .map_err(|e| e.message)
}

#[tauri::command]
pub async fn set_symbol_followed(
    state: State<'_, Arc<AppState>>,
    code: String,
    followed: bool,
) -> Result<(), String> {
    state
        .api
        .set_symbol_followed(&code, followed)
        .await
        .map_err(|e| e.message)
}

#[tauri::command]
pub async fn enrich_symbol_names(state: State<'_, Arc<AppState>>) -> Result<usize, String> {
    state.api.enrich_symbol_names().await.map_err(|e| e.message)
}

#[tauri::command]
pub async fn refresh_symbol_list(state: State<'_, Arc<AppState>>) -> Result<usize, String> {
    state.api.refresh_symbol_list().await.map_err(|e| e.message)
}

// ── Klines & Quotes ──

#[tauri::command]
pub async fn get_klines(
    state: State<'_, Arc<AppState>>,
    symbol: String,
    timeframe: String,
    limit: Option<usize>,
) -> Result<Vec<KlineDto>, String> {
    state
        .api
        .get_klines(&symbol, &timeframe, limit, None)
        .await
        .map_err(|e| e.message)
}

#[tauri::command]
pub async fn get_chart_klines(
    state: State<'_, Arc<AppState>>,
    app: AppHandle,
    symbol: String,
    timeframe: String,
    limit: Option<usize>,
) -> Result<ChartKlineResponse, String> {
    let started = std::time::Instant::now();
    let trace_id = format!("{}-{}", chrono::Utc::now().timestamp_millis(), CHART_REQUEST_SEQ.fetch_add(1, Ordering::Relaxed));
    tracing::info!(%trace_id, %symbol, %timeframe, ?limit, "K线客户端请求开始");
    // Automatically subscribe active symbol and timeframe in realtime client
    state
        .realtime
        .subscribe(vec![symbol.clone()], vec![timeframe.clone()])
        .await;
    tracing::info!(%trace_id, elapsed_ms = started.elapsed().as_millis(), "K线实时订阅完成");

    // 1. 如果有极新鲜的内存缓存（3秒内，如快速来回切换周期/品种），直接命中返回，实现 0ms 秒出图
    if let Some(fresh) = state
        .kline_cache
        .get_fresh(&symbol, &timeframe, std::time::Duration::from_secs(3))
    {
        tracing::info!(%trace_id, symbol, timeframe, elapsed_ms = started.elapsed().as_millis(), rows = fresh.rows.len(), "K线命中3秒内存缓存");
        return Ok(fresh);
    }

    let cached = state.kline_cache.get(&symbol, &timeframe);
    tracing::info!(%trace_id, cached = cached.is_some(), elapsed_ms = started.elapsed().as_millis(), "K线缓存检查完成");

    // 回退缓存后继续同一次下载；不能在 1500ms 时取消下载并从头重发。
    let fetch_state = state.inner().clone();
    let fetch_symbol = symbol.clone();
    let fetch_timeframe = timeframe.clone();
    let fetch_trace_id = trace_id.clone();
    let mut fetch_fut = Box::pin(async move {
        match tokio::time::timeout(
            std::time::Duration::from_secs(30),
            fetch_state.api.get_chart_klines(&fetch_symbol, &fetch_timeframe, limit, None, &fetch_trace_id),
        ).await {
            Ok(res) => res.map_err(|e| e.message),
            Err(_) => {
                tracing::warn!(trace_id = %fetch_trace_id, symbol = %fetch_symbol, timeframe = %fetch_timeframe, "获取K线超时(30s)");
                Err("获取K线超时，请检查网络或服务端状态".to_string())
            }
        }
    });
    let resp_res = if cached.is_some() {
        match tokio::time::timeout(std::time::Duration::from_millis(1500), &mut fetch_fut).await {
            Ok(res) => res,
            Err(_) => {
                tracing::warn!(%trace_id, symbol, timeframe, elapsed_ms = started.elapsed().as_millis(), "网络或服务端响应慢，先显示内存缓存");
                let bg_state = state.inner().clone();
                let bg_app = app.clone();
                let sym = symbol.clone();
                let tf = timeframe.clone();
                let bg_trace_id = trace_id.clone();
                tokio::spawn(async move {
                    if let Ok(resp) = fetch_fut.await {
                        tracing::info!(trace_id = %bg_trace_id, rows = resp.rows.len(), "K线后台刷新完成");
                        bg_state.kline_cache.put(&sym, &tf, &resp);
                        let _ = bg_app.emit("chart-klines-refreshed", serde_json::json!({
                            "symbol": sym,
                            "timeframe": tf,
                            "response": resp,
                        }));
                    } else {
                        tracing::warn!(trace_id = %bg_trace_id, "K线后台刷新失败");
                    }
                });
                return Ok(cached.unwrap());
            }
        }
    } else {
        fetch_fut.await
    };

    match resp_res {
        Ok(resp) => {
            tracing::info!(%trace_id, elapsed_ms = started.elapsed().as_millis(), rows = resp.rows.len(), status = %resp.status, "K线客户端请求完成");
            if started.elapsed() >= std::time::Duration::from_millis(500) {
                tracing::warn!(%trace_id, symbol, timeframe, elapsed_ms = started.elapsed().as_millis(), rows = resp.rows.len(), "K线客户端请求较慢");
            }
            state.kline_cache.put(&symbol, &timeframe, &resp);
            Ok(resp)
        }
        Err(e) => {
            tracing::warn!(%trace_id, elapsed_ms = started.elapsed().as_millis(), error = %e, "K线客户端请求结束于错误");
            if let Some(c) = cached {
                tracing::warn!("网络异常，展示内存缓存K线数据: {e}");
                Ok(c)
            } else {
                Err(e)
            }
        }
    }
}

#[tauri::command]
pub async fn get_trend_series(
    state: State<'_, Arc<AppState>>,
    symbol: String,
    timeframe: String,
    limit: Option<usize>,
) -> Result<Vec<TrendPointDto>, String> {
    match tokio::time::timeout(
        std::time::Duration::from_secs(5),
        state.api.get_trend_series(&symbol, &timeframe, limit),
    )
    .await
    {
        Ok(res) => res.map_err(|e| e.message),
        Err(_) => {
            tracing::warn!("获取趋势线超时，返回空序列以防阻塞页面");
            Ok(Vec::new())
        }
    }
}

#[tauri::command]
pub async fn get_intraday_chart(
    state: State<'_, Arc<AppState>>,
    symbol: String,
) -> Result<IntradayChartResponse, String> {
    state
        .api
        .get_intraday_chart(&symbol)
        .await
        .map_err(|e| e.message)
}

#[tauri::command]
pub async fn get_market_snapshot(
    state: State<'_, Arc<AppState>>,
) -> Result<Vec<MarketSnapshot>, String> {
    state.api.get_market_snapshot().await.map_err(|e| e.message)
}

// ── Manual Levels ──

#[tauri::command]
pub async fn list_manual_levels(
    state: State<'_, Arc<AppState>>,
    symbol: Option<String>,
    timeframe: Option<String>,
    active_only: Option<bool>,
) -> Result<Vec<ManualLevelDto>, String> {
    state
        .api
        .list_manual_levels(
            symbol.as_deref(),
            timeframe.as_deref(),
            active_only.unwrap_or(false),
        )
        .await
        .map_err(|e| e.message)
}

#[tauri::command]
pub async fn create_manual_level(
    state: State<'_, Arc<AppState>>,
    input: ManualLevelInput,
) -> Result<ManualLevelDto, String> {
    state
        .api
        .create_manual_level(&input)
        .await
        .map_err(|e| e.message)
}

#[tauri::command]
pub async fn update_manual_level(
    state: State<'_, Arc<AppState>>,
    id: i64,
    input: ManualLevelInput,
) -> Result<ManualLevelDto, String> {
    state
        .api
        .update_manual_level(id, &input)
        .await
        .map_err(|e| e.message)
}

#[tauri::command]
pub async fn set_manual_level_monitoring(
    state: State<'_, Arc<AppState>>,
    id: i64,
    enabled: bool,
) -> Result<ManualLevelDto, String> {
    state
        .api
        .set_manual_level_monitoring(id, enabled)
        .await
        .map_err(|e| e.message)
}

#[tauri::command]
pub async fn archive_manual_level(state: State<'_, Arc<AppState>>, id: i64) -> Result<(), String> {
    state
        .api
        .archive_manual_level(id)
        .await
        .map_err(|e| e.message)
}

#[tauri::command]
pub async fn delete_manual_level(state: State<'_, Arc<AppState>>, id: i64) -> Result<(), String> {
    state
        .api
        .delete_manual_level(id)
        .await
        .map_err(|e| e.message)
}

#[tauri::command]
pub async fn get_manual_level_events(
    state: State<'_, Arc<AppState>>,
    id: i64,
) -> Result<Vec<ManualLevelEventDto>, String> {
    state
        .api
        .get_manual_level_events(id)
        .await
        .map_err(|e| e.message)
}

#[tauri::command]
pub async fn get_active_events(state: State<'_, Arc<AppState>>) -> Result<Value, String> {
    state.api.get_active_events().await.map_err(|e| e.message)
}

#[tauri::command]
pub async fn get_active_single_bars(state: State<'_, Arc<AppState>>) -> Result<Value, String> {
    state
        .api
        .get_active_single_bars()
        .await
        .map_err(|e| e.message)
}

// ── Actions ──

#[tauri::command]
pub async fn refresh_data_now(state: State<'_, Arc<AppState>>) -> Result<RefreshStats, String> {
    state.api.refresh_data_now().await.map_err(|e| e.message)
}

#[tauri::command]
pub async fn run_scan_now(state: State<'_, Arc<AppState>>) -> Result<Value, String> {
    state.api.run_scan_now().await.map_err(|e| e.message)
}

#[tauri::command]
pub async fn run_scan_fast_now(state: State<'_, Arc<AppState>>) -> Result<Value, String> {
    state.api.run_scan_fast_now().await.map_err(|e| e.message)
}

#[tauri::command]
pub async fn rebuild_events_now(state: State<'_, Arc<AppState>>) -> Result<Value, String> {
    state.api.rebuild_events_now().await.map_err(|e| e.message)
}

#[tauri::command]
pub async fn refresh_outcomes_now(
    state: State<'_, Arc<AppState>>,
) -> Result<OutcomeRefresh, String> {
    state
        .api
        .refresh_outcomes_now()
        .await
        .map_err(|e| e.message)
}

// ── Review & Signals ──

#[tauri::command]
pub async fn get_review_stats(
    state: State<'_, Arc<AppState>>,
    dimension: String,
    scope: String,
    version: Option<String>,
    score_min: Option<f64>,
    score_max: Option<f64>,
) -> Result<Value, String> {
    state
        .api
        .get_review_stats(&dimension, &scope, version.as_deref(), score_min, score_max)
        .await
        .map_err(|e| e.message)
}

#[tauri::command]
pub async fn get_recent_outcomes(
    state: State<'_, Arc<AppState>>,
    limit: Option<usize>,
    symbol: Option<String>,
    version: Option<String>,
    direction: Option<String>,
    level: Option<String>,
    grade: Option<String>,
    score_min: Option<f64>,
    score_max: Option<f64>,
    outcome: Option<String>,
) -> Result<Value, String> {
    let filters = RecentOutcomeFilters {
        symbol,
        version,
        direction,
        level,
        grade,
        score_min,
        score_max,
        outcome,
    };
    state
        .api
        .get_recent_outcomes(limit, Some(&filters))
        .await
        .map_err(|e| e.message)
}

#[tauri::command]
pub async fn get_review_signal(
    state: State<'_, Arc<AppState>>,
    event_id: i64,
) -> Result<Value, String> {
    state
        .api
        .get_review_signal(event_id)
        .await
        .map_err(|e| e.message)
}

#[tauri::command]
pub async fn get_signal_user_data(
    state: State<'_, Arc<AppState>>,
    event_id: i64,
) -> Result<SignalUserData, String> {
    state
        .api
        .get_signal_user_data(event_id)
        .await
        .map_err(|e| e.message)
}

#[tauri::command]
pub async fn add_signal_annotation(
    state: State<'_, Arc<AppState>>,
    event_id: i64,
    content: String,
) -> Result<SignalAnnotationDto, String> {
    state
        .api
        .add_signal_annotation(event_id, &content)
        .await
        .map_err(|e| e.message)
}

#[tauri::command]
pub async fn delete_signal_annotation(
    state: State<'_, Arc<AppState>>,
    id: i64,
) -> Result<(), String> {
    state
        .api
        .delete_signal_annotation(id)
        .await
        .map_err(|e| e.message)
}

#[tauri::command]
pub async fn set_signal_decision(
    state: State<'_, Arc<AppState>>,
    event_id: i64,
    opened: bool,
) -> Result<SignalDecisionDto, String> {
    state
        .api
        .set_signal_decision(event_id, opened)
        .await
        .map_err(|e| e.message)
}

// ── V2 ML & Preclose ──

#[tauri::command]
pub async fn get_v2_models(state: State<'_, Arc<AppState>>) -> Result<Vec<V2ModelRow>, String> {
    state.api.get_v2_models().await.map_err(|e| e.message)
}

#[tauri::command]
pub async fn get_v2_dataset_report(
    state: State<'_, Arc<AppState>>,
) -> Result<V2ReportBundle, String> {
    state
        .api
        .get_v2_dataset_report()
        .await
        .map_err(|e| e.message)
}

#[tauri::command]
pub async fn get_v2_predictions(
    state: State<'_, Arc<AppState>>,
    model_id: Option<String>,
) -> Result<Vec<V2PredictionRow>, String> {
    state
        .api
        .get_v2_predictions(model_id.as_deref())
        .await
        .map_err(|e| e.message)
}

#[tauri::command]
pub async fn set_v2_model_status(
    state: State<'_, Arc<AppState>>,
    model_id: String,
    status: String,
) -> Result<(), String> {
    state
        .api
        .set_v2_model_status(&model_id, &status)
        .await
        .map_err(|e| e.message)
}

#[tauri::command]
pub async fn backfill_v2_predictions(state: State<'_, Arc<AppState>>) -> Result<Value, String> {
    state
        .api
        .backfill_v2_predictions()
        .await
        .map_err(|e| e.message)
}

#[tauri::command]
pub async fn get_active_preclose_signals(state: State<'_, Arc<AppState>>) -> Result<Value, String> {
    state
        .api
        .get_active_preclose_signals()
        .await
        .map_err(|e| e.message)
}

#[tauri::command]
pub async fn get_active_preclose_candidates(
    state: State<'_, Arc<AppState>>,
) -> Result<Value, String> {
    state
        .api
        .get_active_preclose_candidates()
        .await
        .map_err(|e| e.message)
}

#[tauri::command]
pub async fn get_preclose_candidates(state: State<'_, Arc<AppState>>) -> Result<Value, String> {
    state
        .api
        .get_preclose_candidates(None)
        .await
        .map_err(|e| e.message)
}

#[tauri::command]
pub async fn get_preclose_signals(state: State<'_, Arc<AppState>>) -> Result<Value, String> {
    state
        .api
        .get_preclose_signals(None)
        .await
        .map_err(|e| e.message)
}

// ── Settings (Backward Compatible & New) ──

#[tauri::command]
pub async fn get_config(state: State<'_, Arc<AppState>>) -> Result<Value, String> {
    let local = state.local_settings.read().await.clone();
    let ui_val = serde_json::json!({
        "flash_ms": 900,
        "breathe_hold_ms": 5000,
        "min_bar_spacing": local.min_bar_spacing,
        "chart_display_bars": local.chart_display_bars,
        "mobile_chart_fullscreen_bars": local.mobile_chart_fullscreen_bars,
        "chart_right_gap": local.chart_right_gap,
        "chart_show_first_signal": true,
        "score_pill_full_score": 3.5,
        "timeframes": local.timeframes,
        "last_group_id": local.last_group_id,
        "chart_review_focus_right": false,
    });

    match state.api.get_server_settings().await {
        Ok(server) => Ok(serde_json::json!({
            "app_config": server.app_config,
            "scheduler": server.scheduler,
            "fetch": server.fetch,
            "quote": server.quote,
            "notify": server.notify,
            "preclose": server.preclose,
            "log": server.log,
            "data_source": server.data_source,
            "email": server.email,
            "ui": ui_val,
        })),
        Err(e) => {
            tracing::warn!("无法从服务端读取配置，使用本地默认: {e}");
            Ok(serde_json::json!({
                "app_config": {
                    "auto_start_scheduler": true,
                    "logic_version": "1"
                },
                "scheduler": {
                    "refresh_interval_secs": 300,
                    "scan_interval_secs": 900,
                    "trading_only": true
                },
                "fetch": {
                    "request_interval_ms": 400,
                    "minutely_budget": 60,
                    "backfill_count": 1000,
                    "incremental_count": 10
                },
                "quote": {
                    "poll_interval_ms": 3000,
                    "request_interval_ms": 200,
                    "minutely_budget": 120
                },
                "email": {
                    "enabled": true,
                    "to": "",
                    "from": "",
                    "smtp_host": "smtp.qq.com",
                    "smtp_port": 465,
                    "smtp_user": "",
                    "smtp_password": ""
                },
                "notify": {
                    "in_app_new_pattern": true,
                    "new_pattern_min_score": 0.0,
                    "in_app_entry_trigger": true,
                    "system_entry_trigger": false
                },
                "preclose": {
                    "schema_version": 2,
                    "enabled": true,
                    "lead_secs": 180,
                    "horizon_minutes": 60,
                    "in_app_notify": true
                },
                "log": {
                    "level": "info"
                },
                "data_source": {
                    "primary_source": "tqsdk",
                    "fallback_enabled": true,
                    "tq_account": "",
                    "tq_password": "",
                    "bridge_port": 8765,
                    "auto_spawn_bridge": true,
                    "python_path": null
                },
                "ui": ui_val,
            }))
        }
    }
}

#[tauri::command]
pub async fn update_config(
    state: State<'_, Arc<AppState>>,
    config: Value,
) -> Result<Value, String> {
    // 1. Update UI config locally if provided
    if let Some(ui) = config.get("ui") {
        let mut local = state.local_settings.write().await;
        if let Some(v) = ui
            .get("chart_display_bars")
            .or_else(|| ui.get("chartDisplayBars"))
            .and_then(|v| v.as_u64())
        {
            local.chart_display_bars = v as usize;
        }
        if let Some(v) = ui
            .get("mobile_chart_fullscreen_bars")
            .or_else(|| ui.get("mobileChartFullscreenBars"))
            .and_then(|v| v.as_u64())
        {
            local.mobile_chart_fullscreen_bars = v as usize;
        }
        if let Some(v) = ui
            .get("chart_right_gap")
            .or_else(|| ui.get("chartRightGap"))
            .and_then(|v| v.as_u64())
        {
            local.chart_right_gap = v as usize;
        }
        if let Some(v) = ui
            .get("min_bar_spacing")
            .or_else(|| ui.get("minBarSpacing"))
            .and_then(|v| v.as_f64())
        {
            local.min_bar_spacing = v;
        }
        if let Some(v) = ui.get("timeframes").and_then(|v| v.as_array()) {
            local.timeframes = v
                .iter()
                .filter_map(|s| s.as_str().map(|s| s.to_string()))
                .collect();
        }
        if let Some(v) = ui.get("last_group_id").or_else(|| ui.get("lastGroupId")) {
            local.last_group_id = v.as_i64();
        }
        drop(local);
        let _ = state.save_local_settings().await;
    }

    // 2. Update server config
    if let Ok(server_settings) = state.api.get_server_settings().await {
        let mut update = ServerSettingsUpdate {
            config_revision: server_settings.config_revision,
            app_config: None,
            scheduler: None,
            fetch: None,
            quote: None,
            notify: None,
            preclose: None,
            log: None,
            data_source: None,
            email: None,
        };

        if let Some(app) = config.get("app_config").or_else(|| config.get("appConfig")) {
            if let Ok(dto) = serde_json::from_value::<AppConfigDto>(app.clone()) {
                update.app_config = Some(dto);
            }
        }
        if let Some(s) = config.get("scheduler") {
            if let Ok(dto) = serde_json::from_value::<SchedulerConfigDto>(s.clone()) {
                update.scheduler = Some(dto);
            }
        }
        if let Some(f) = config.get("fetch") {
            if let Ok(dto) = serde_json::from_value::<FetchConfigDto>(f.clone()) {
                update.fetch = Some(dto);
            }
        }
        if let Some(q) = config.get("quote") {
            if let Ok(dto) = serde_json::from_value::<QuoteConfigDto>(q.clone()) {
                update.quote = Some(dto);
            }
        }
        if let Some(n) = config.get("notify") {
            if let Ok(dto) = serde_json::from_value::<NotifyConfigDto>(n.clone()) {
                update.notify = Some(dto);
            }
        }
        if let Some(p) = config.get("preclose") {
            if let Ok(dto) = serde_json::from_value::<PrecloseConfigDto>(p.clone()) {
                update.preclose = Some(dto);
            }
        }
        if let Some(l) = config.get("log") {
            if let Ok(dto) = serde_json::from_value::<LogConfigDto>(l.clone()) {
                update.log = Some(dto);
            }
        }
        if let Some(ds) = config
            .get("data_source")
            .or_else(|| config.get("dataSource"))
        {
            if let Ok(dto) = serde_json::from_value::<DataSourceUpdateDto>(ds.clone()) {
                update.data_source = Some(dto);
            }
        }
        if let Some(em) = config.get("email") {
            if let Ok(dto) = serde_json::from_value::<EmailSettingsUpdateDto>(em.clone()) {
                update.email = Some(dto);
            }
        }

        let _ = state.api.update_server_settings(&update).await;
    }

    get_config(state).await
}

#[tauri::command]
pub async fn reset_config(state: State<'_, Arc<AppState>>) -> Result<Value, String> {
    let _ = state.api.reset_config().await;
    let mut local = state.local_settings.write().await;
    *local = ClientLocalSettings::default();
    drop(local);
    let _ = state.save_local_settings().await;
    get_config(state).await
}

#[tauri::command]
pub async fn set_last_group(
    state: State<'_, Arc<AppState>>,
    group_id: Option<i64>,
) -> Result<(), String> {
    {
        let mut local = state.local_settings.write().await;
        local.last_group_id = group_id;
    }
    let _ = state.save_local_settings().await;
    let _ = state.api.set_last_group(group_id).await;
    Ok(())
}

#[tauri::command]
pub async fn set_timeframes(
    state: State<'_, Arc<AppState>>,
    timeframes: Vec<String>,
) -> Result<(), String> {
    {
        let mut local = state.local_settings.write().await;
        local.timeframes = timeframes.clone();
    }
    let _ = state.save_local_settings().await;
    let _ = state.api.set_timeframes(&timeframes).await;
    Ok(())
}

#[tauri::command]
pub async fn get_server_settings(
    state: State<'_, Arc<AppState>>,
) -> Result<ServerSettingsDto, String> {
    state.api.get_server_settings().await.map_err(|e| e.message)
}

#[tauri::command]
pub async fn update_server_settings(
    state: State<'_, Arc<AppState>>,
    req: ServerSettingsUpdate,
) -> Result<ConfigApplyResult, String> {
    state
        .api
        .update_server_settings(&req)
        .await
        .map_err(|e| e.message)
}

// ── Admin & Runtime Controls ──

#[tauri::command]
pub async fn scheduler_status(state: State<'_, Arc<AppState>>) -> Result<SchedulerStatus, String> {
    state
        .api
        .get_scheduler_status()
        .await
        .map_err(|e| e.message)
}

#[tauri::command]
pub async fn set_scheduler_running(
    state: State<'_, Arc<AppState>>,
    running: bool,
) -> Result<SchedulerStatus, String> {
    state
        .api
        .set_scheduler_running(running)
        .await
        .map_err(|e| e.message)
}

#[tauri::command]
pub async fn restart_server(state: State<'_, Arc<AppState>>) -> Result<(), String> {
    state.api.restart_server().await.map_err(|e| e.message)
}

#[tauri::command]
pub async fn restart_bridge(state: State<'_, Arc<AppState>>) -> Result<(), String> {
    state.api.restart_bridge().await.map_err(|e| e.message)
}

#[tauri::command]
pub async fn list_devices(state: State<'_, Arc<AppState>>) -> Result<Vec<DeviceItemDto>, String> {
    state.api.list_devices().await.map_err(|e| e.message)
}

#[tauri::command]
pub async fn revoke_device(
    state: State<'_, Arc<AppState>>,
    device_id: String,
) -> Result<(), String> {
    state
        .api
        .revoke_device(&device_id)
        .await
        .map_err(|e| e.message)
}

#[tauri::command]
pub async fn update_device_role(
    state: State<'_, Arc<AppState>>,
    device_id: String,
    role: String,
) -> Result<(), String> {
    state
        .api
        .update_device_role(&device_id, &role)
        .await
        .map_err(|e| e.message)
}

#[tauri::command]
pub async fn pair_device(
    state: State<'_, Arc<AppState>>,
    req: DeviceRegisterRequest,
) -> Result<DeviceRegisterResponse, String> {
    state.api.pair_device(&req).await.map_err(|e| e.message)
}

// ── PC Database Backup ──

#[tauri::command]
pub async fn get_backup_status(state: State<'_, Arc<AppState>>) -> Result<BackupStatus, String> {
    Ok(state.backup_scheduler.get_status().await)
}

#[tauri::command]
pub async fn trigger_database_backup(state: State<'_, Arc<AppState>>) -> Result<String, String> {
    state.backup_scheduler.trigger_backup_now().await
}

#[tauri::command]
pub async fn open_backup_directory(state: State<'_, Arc<AppState>>) -> Result<(), String> {
    let dir = state.backup_scheduler.backup_dir();
    #[cfg(desktop)]
    {
        let _ = std::fs::create_dir_all(&dir);
        open::that(&dir).map_err(|e| format!("打开备份目录失败: {e}"))
    }
    #[cfg(mobile)]
    {
        let _ = dir;
        Err("移动端不支持打开本地备份目录".to_string())
    }
}

// ── Integrity ──

#[tauri::command]
pub async fn check_symbol_integrity(
    state: State<'_, Arc<AppState>>,
    symbol: String,
) -> Result<Value, String> {
    state
        .api
        .check_symbol_integrity(&symbol)
        .await
        .map_err(|e| e.message)
}

#[tauri::command]
pub async fn check_all_symbols_integrity(state: State<'_, Arc<AppState>>) -> Result<Value, String> {
    state
        .api
        .check_all_symbols_integrity()
        .await
        .map_err(|e| e.message)
}

#[tauri::command]
pub async fn repair_symbol_integrity(
    state: State<'_, Arc<AppState>>,
    symbol: String,
) -> Result<Value, String> {
    state
        .api
        .repair_symbol_integrity(&symbol)
        .await
        .map_err(|e| e.message)
}

#[tauri::command]
pub async fn get_finality_report(state: State<'_, Arc<AppState>>) -> Result<Value, String> {
    state
        .api
        .check_all_symbols_integrity()
        .await
        .map_err(|e| e.message)
}

#[tauri::command]
pub async fn get_finality_simulation(_state: State<'_, Arc<AppState>>) -> Result<Value, String> {
    Ok(serde_json::json!([]))
}

#[tauri::command]
pub async fn get_finality_sentinel_eval(_state: State<'_, Arc<AppState>>) -> Result<Value, String> {
    Ok(serde_json::json!([]))
}

// ── System Utilities ──

#[tauri::command]
pub async fn open_log_directory(app: AppHandle) -> Result<(), String> {
    let dir = app.path().app_data_dir().map_err(|e| e.to_string())?;
    #[cfg(desktop)]
    {
        std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
        open::that(&dir).map_err(|e| e.to_string())
    }
    #[cfg(mobile)]
    {
        let _ = dir;
        Err("移动端不支持打开本地日志目录".to_string())
    }
}

// ── Trader Commands ──

#[tauri::command]
pub async fn get_trader_snapshot(state: State<'_, Arc<AppState>>) -> Result<Value, String> {
    if let Ok(snapshot) = state.api.get_trader_snapshot().await {
        return Ok(snapshot);
    }
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_millis(500))
        .build()
        .map_err(|e| e.to_string())?;
    let mut snapshot = client
        .get("http://127.0.0.1:8766/snapshot")
        .send()
        .await
        .map_err(|e| e.to_string())?
        .error_for_status()
        .map_err(|e| e.to_string())?
        .json::<Value>()
        .await
        .map_err(|e| e.to_string())?;
    prepare_local_trader_snapshot(&mut snapshot);
    Ok(snapshot)
}

#[tauri::command]
pub async fn get_trader_status(state: State<'_, Arc<AppState>>) -> Result<Value, String> {
    if let Ok(res) = state.api.get_trader_status().await {
        return Ok(res);
    }
    // 当 n-headless 未启动时，直接直连本地运行的 CTP 桥接守护进程 (port 8766)
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_millis(500))
        .build()
        .unwrap_or_default();
    match client.get("http://127.0.0.1:8766/health").send().await {
        Ok(resp) => {
            if let Ok(health) = resp.json::<Value>().await {
                let current_cfg =
                    LOCAL_TRADER_CONFIG
                        .lock()
                        .unwrap()
                        .clone()
                        .unwrap_or_else(|| {
                            serde_json::json!({
                                "mode": "SemiAuto",
                                "max_risk_percent": 1.0,
                                "default_large_volume": 2,
                                "default_small_volume": 1,
                                "daily_loss_limit_r": 3.0
                            })
                        });
                return Ok(serde_json::json!({
                    "health": health,
                    "config": current_cfg
                }));
            }
        }
        Err(e) => return Err(format!("CTP 桥接服务连接失败: {e}")),
    }
    Err("CTP 状态获取失败".to_string())
}

#[tauri::command]
pub async fn get_execution_orders(state: State<'_, Arc<AppState>>, page: usize, page_size: usize) -> Result<Value, String> {
    state.api.get_execution_orders(page, page_size).await.map_err(|e| e.message)
}

#[tauri::command]
pub async fn get_trader_account(state: State<'_, Arc<AppState>>) -> Result<Value, String> {
    if let Ok(res) = state.api.get_trader_account().await {
        return Ok(res);
    }
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_millis(500))
        .build()
        .unwrap_or_default();
    match client.get("http://127.0.0.1:8766/account").send().await {
        Ok(resp) => {
            if let Ok(mut acc) = resp.json::<Value>().await {
                if acc.get("margin").is_none() {
                    if let Some(cm) = acc.get("curr_margin") {
                        acc["margin"] = cm.clone();
                    }
                }
                if acc.get("account_id").is_none() {
                    acc["account_id"] = Value::String("278272".to_string());
                }
                if acc.get("risk_ratio").is_none() {
                    let bal = acc.get("balance").and_then(|v| v.as_f64()).unwrap_or(0.0);
                    let mgn = acc.get("margin").and_then(|v| v.as_f64()).unwrap_or(0.0);
                    let ratio = if bal > 0.0 { mgn / bal } else { 0.0 };
                    acc["risk_ratio"] = serde_json::json!(ratio);
                }
                return Ok(acc);
            }
        }
        Err(e) => return Err(format!("CTP 账户查询失败: {e}")),
    }
    Err("CTP 账户获取失败".to_string())
}

#[tauri::command]
pub async fn get_trader_positions(state: State<'_, Arc<AppState>>) -> Result<Value, String> {
    if let Ok(res) = state.api.get_trader_positions().await {
        return Ok(res);
    }
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_millis(500))
        .build()
        .unwrap_or_default();
    match client.get("http://127.0.0.1:8766/positions").send().await {
        Ok(resp) => {
            if let Ok(broker_positions) = resp.json::<Value>().await {
                return Ok(serde_json::json!({
                    "broker_positions": broker_positions,
                    "managed_positions": []
                }));
            }
        }
        Err(e) => return Err(format!("CTP 持仓查询失败: {e}")),
    }
    Err("CTP 持仓获取失败".to_string())
}

#[tauri::command]
pub async fn get_condition_orders(state: State<'_, Arc<AppState>>) -> Result<Value, String> {
    if let Ok(res) = state.api.get_condition_orders().await {
        return Ok(res);
    }
    Ok(serde_json::json!([]))
}

#[tauri::command]
pub async fn cancel_condition_order(
    state: State<'_, Arc<AppState>>,
    id: String,
) -> Result<Value, String> {
    state
        .api
        .cancel_condition_order(&id)
        .await
        .map_err(|e| e.message)
}

#[tauri::command]
pub async fn confirm_condition_order(
    state: State<'_, Arc<AppState>>,
    id: String,
) -> Result<Value, String> {
    state
        .api
        .confirm_condition_order(&id)
        .await
        .map_err(|e| e.message)
}

#[tauri::command]
pub async fn update_trader_config(
    state: State<'_, Arc<AppState>>,
    config: Value,
) -> Result<Value, String> {
    *LOCAL_TRADER_CONFIG.lock().unwrap() = Some(config.clone());
    if state.realtime.get_state().await.status == ConnectionStatus::Connected {
        if let Ok(res) = state.api.update_trader_config(&config).await {
            return Ok(res);
        }
    }
    // 当服务端离线时，本地直接确认模式更新
    Ok(config)
}

#[tauri::command]
pub async fn emergency_cancel_all(state: State<'_, Arc<AppState>>) -> Result<Value, String> {
    if state.realtime.get_state().await.status == ConnectionStatus::Connected {
        if let Ok(res) = state.api.emergency_cancel_all().await {
            return Ok(res);
        }
    }
    let client = reqwest::Client::new();
    let resp = client
        .post("http://127.0.0.1:8766/emergency/cancel_all")
        .send()
        .await
        .map_err(|e| e.to_string())?;
    resp.json::<Value>().await.map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn emergency_close_all(state: State<'_, Arc<AppState>>) -> Result<Value, String> {
    if state.realtime.get_state().await.status == ConnectionStatus::Connected {
        if let Ok(res) = state.api.emergency_close_all().await {
            return Ok(res);
        }
    }
    let client = reqwest::Client::new();
    let resp = client
        .post("http://127.0.0.1:8766/emergency/close_all")
        .send()
        .await
        .map_err(|e| e.to_string())?;
    resp.json::<Value>().await.map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn get_trade_history(state: State<'_, Arc<AppState>>) -> Result<Value, String> {
    if let Ok(res) = state.api.get_trade_history().await {
        return Ok(res);
    }
    Ok(serde_json::json!([]))
}

#[tauri::command]
pub async fn get_trade_statistics(state: State<'_, Arc<AppState>>) -> Result<Value, String> {
    if let Ok(res) = state.api.get_trade_statistics().await {
        return Ok(res);
    }
    Ok(serde_json::json!({
        "total_trades": 0,
        "win_trades": 0,
        "loss_trades": 0,
        "win_rate_pct": 0.0,
        "total_r": 0.0,
        "avg_r": 0.0,
        "profit_factor": 0.0,
        "large_trades_count": 0,
        "small_trades_count": 0,
        "exit_reason_breakdown": {}
    }))
}
