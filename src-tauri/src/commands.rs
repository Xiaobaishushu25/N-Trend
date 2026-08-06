use std::sync::Arc;

use n_core::service::{MarketSnapshot, RefreshStats, ScanResult, Settings};
use n_core::storage::entities::{groups, klines, scans, signals, symbols};
use serde::Serialize;
use tauri::State;

use crate::state::AppState;
use crate::AppInfo;

#[derive(Debug, Clone, Serialize)]
pub struct SchedulerStatus {
    pub running: bool,
    pub last_refresh: Option<String>,
    pub last_scan: Option<String>,
}

#[tauri::command]
pub async fn app_info() -> AppInfo {
    AppInfo {
        name: "ntrend".to_string(),
        version: env!("CARGO_PKG_VERSION").to_string(),
    }
}

#[tauri::command]
pub async fn get_symbols(state: State<'_, Arc<AppState>>) -> Result<Vec<symbols::Model>, String> {
    n_core::storage::repo::list_symbols(&state.services.db, false)
        .await
        .map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn list_groups(state: State<'_, Arc<AppState>>) -> Result<Vec<groups::Model>, String> {
    n_core::storage::repo::list_groups(&state.services.db)
        .await
        .map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn create_group(
    state: State<'_, Arc<AppState>>,
    name: String,
) -> Result<groups::Model, String> {
    n_core::storage::repo::create_group(&state.services.db, &name)
        .await
        .map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn rename_group(
    state: State<'_, Arc<AppState>>,
    id: i64,
    name: String,
) -> Result<(), String> {
    n_core::storage::repo::rename_group(&state.services.db, id, &name)
        .await
        .map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn delete_group(state: State<'_, Arc<AppState>>, id: i64) -> Result<(), String> {
    n_core::storage::repo::delete_group(&state.services.db, id)
        .await
        .map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn get_group_symbols(
    state: State<'_, Arc<AppState>>,
    group_id: i64,
) -> Result<Vec<symbols::Model>, String> {
    n_core::storage::repo::group_symbols(&state.services.db, group_id)
        .await
        .map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn list_symbol_groups(
    state: State<'_, Arc<AppState>>,
    symbol: String,
) -> Result<Vec<groups::Model>, String> {
    n_core::storage::repo::symbol_groups(&state.services.db, &symbol)
        .await
        .map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn add_symbol_to_group(
    state: State<'_, Arc<AppState>>,
    symbol: String,
    group_id: i64,
) -> Result<(), String> {
    n_core::storage::repo::add_symbol_to_group(&state.services.db, &symbol, group_id)
        .await
        .map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn remove_symbol_from_group(
    state: State<'_, Arc<AppState>>,
    symbol: String,
    group_id: i64,
) -> Result<(), String> {
    n_core::storage::repo::remove_symbol_from_group(&state.services.db, &symbol, group_id)
        .await
        .map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn reorder_group_symbols(
    state: State<'_, Arc<AppState>>,
    group_id: i64,
    codes: Vec<String>,
) -> Result<(), String> {
    n_core::storage::repo::reorder_group_symbols(&state.services.db, group_id, &codes)
        .await
        .map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn reorder_symbols(
    state: State<'_, Arc<AppState>>,
    codes: Vec<String>,
) -> Result<(), String> {
    n_core::storage::repo::reorder_symbols(&state.services.db, &codes)
        .await
        .map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn add_symbol(state: State<'_, Arc<AppState>>, code: String) -> Result<usize, String> {
    state.services.add_symbol(&code).await.map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn remove_symbol(state: State<'_, Arc<AppState>>, code: String) -> Result<(), String> {
    state.services.remove_symbol(&code).await.map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn set_symbol_flags(
    state: State<'_, Arc<AppState>>,
    code: String,
    watchlist: bool,
    enabled: bool,
) -> Result<(), String> {
    n_core::storage::repo::set_symbol_flags(&state.services.db, &code, watchlist, enabled)
        .await
        .map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn enrich_symbol_names(state: State<'_, Arc<AppState>>) -> Result<usize, String> {
    state
        .services
        .enrich_existing_symbols()
        .await
        .map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn refresh_symbol_list(state: State<'_, Arc<AppState>>) -> Result<usize, String> {
    state
        .services
        .refresh_symbol_list()
        .await
        .map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn get_klines(
    state: State<'_, Arc<AppState>>,
    symbol: String,
    timeframe: String,
    limit: Option<usize>,
) -> Result<Vec<klines::Model>, String> {
    state
        .services
        .get_klines(&symbol, &timeframe, limit)
        .await
        .map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn get_market_snapshot(state: State<'_, Arc<AppState>>) -> Result<Vec<MarketSnapshot>, String> {
    state.services.market_snapshot().await.map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn refresh_data_now(state: State<'_, Arc<AppState>>) -> Result<RefreshStats, String> {
    let stats = state.services.refresh_data().await.map_err(|e| e.to_string())?;
    state.note_refresh_success().await;
    Ok(stats)
}

#[tauri::command]
pub async fn run_scan_now(state: State<'_, Arc<AppState>>) -> Result<ScanResult, String> {
    let result = state.services.run_scan().await.map_err(|e| e.to_string())?;
    state.note_scan_success().await;
    Ok(result)
}

#[tauri::command]
pub async fn get_scan_history(
    state: State<'_, Arc<AppState>>,
    limit: Option<u64>,
) -> Result<Vec<scans::Model>, String> {
    n_core::storage::repo::recent_scans(&state.services.db, limit.unwrap_or(20))
        .await
        .map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn get_scan_detail(
    state: State<'_, Arc<AppState>>,
    scan_id: i64,
) -> Result<Vec<signals::Model>, String> {
    n_core::storage::repo::signals_for_scan(&state.services.db, scan_id)
        .await
        .map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn get_latest_signals(
    state: State<'_, Arc<AppState>>,
    limit: Option<u64>,
) -> Result<Vec<signals::Model>, String> {
    n_core::storage::repo::latest_signals(&state.services.db, limit.unwrap_or(50))
        .await
        .map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn get_settings(state: State<'_, Arc<AppState>>) -> Result<Settings, String> {
    Ok(state.services.settings().await)
}

#[tauri::command]
pub async fn update_settings(
    state: State<'_, Arc<AppState>>,
    settings: Settings,
) -> Result<Settings, String> {
    state
        .services
        .apply_settings(settings)
        .await
        .map_err(|e| e.to_string())?;
    Ok(state.services.settings().await)
}

#[tauri::command]
pub async fn scheduler_status(state: State<'_, Arc<AppState>>) -> Result<SchedulerStatus, String> {
    let rt = state.scheduler.read().await;
    Ok(SchedulerStatus {
        running: rt.running,
        last_refresh: rt.last_refresh.map(crate::fmt_naive),
        last_scan: rt.last_scan.map(crate::fmt_naive),
    })
}

#[tauri::command]
pub async fn set_scheduler_running(
    state: State<'_, Arc<AppState>>,
    running: bool,
) -> Result<SchedulerStatus, String> {
    state.scheduler.write().await.running = running;
    scheduler_status(state).await
}




