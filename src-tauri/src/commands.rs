use std::sync::Arc;

use n_core::config::Config;
use n_core::service::{MarketSnapshot, RefreshStats, ScanResult};
use n_core::storage::entities::{groups, klines, scans, signals, symbols};
use serde::Serialize;
use tauri::{Emitter, Manager, State};

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
pub async fn reorder_groups(
    state: State<'_, Arc<AppState>>,
    ids: Vec<i64>,
    all_position: i64,
) -> Result<(), String> {
    n_core::storage::repo::reorder_groups(&state.services.db, &ids)
        .await
        .map_err(|e| e.to_string())?;
    // 一并持久化「全部品种」在分组顺序中的位置（虚拟槽位）
    let mut map = std::collections::HashMap::new();
    map.insert("group_all_position".to_string(), all_position.to_string());
    n_core::storage::repo::set_settings(&state.services.db, &map)
        .await
        .map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn get_group_all_position(state: State<'_, Arc<AppState>>) -> Result<i64, String> {
    let pos = n_core::storage::repo::get_setting(&state.services.db, "group_all_position")
        .await
        .map_err(|e| e.to_string())?
        .and_then(|v| v.parse::<i64>().ok())
        .unwrap_or(0);
    Ok(pos)
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
pub async fn run_scan_now(
    app: tauri::AppHandle,
    state: State<'_, Arc<AppState>>,
) -> Result<ScanResult, String> {
    let result = state.services.run_scan().await.map_err(|e| e.to_string())?;
    state.note_scan_success().await;
    // 与定时扫描一致：广播扫描完成事件，让表格/K线等页面立即刷新，避免停留在旧数据
    let _ = app.emit("scan-completed", &result);
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
pub async fn get_config(state: State<'_, Arc<AppState>>) -> Result<Config, String> {
    Ok(state.services.config().await)
}

#[tauri::command]
pub async fn update_config(
    state: State<'_, Arc<AppState>>,
    config: Config,
) -> Result<Config, String> {
    state
        .services
        .apply_config(config)
        .await
        .map_err(|e| e.to_string())
}

/// 记录上次打开的分组表格（null=全部品种）。
#[tauri::command]
pub async fn set_last_group(
    state: State<'_, Arc<AppState>>,
    group_id: Option<i64>,
) -> Result<(), String> {
    state
        .services
        .set_last_group(group_id)
        .await
        .map_err(|e| e.to_string())
}

/// 设置启用的K线周期列表。
#[tauri::command]
pub async fn set_timeframes(
    state: State<'_, Arc<AppState>>,
    timeframes: Vec<String>,
) -> Result<(), String> {
    state
        .services
        .set_timeframes(timeframes)
        .await
        .map_err(|e| e.to_string())
}

/// 打开日志目录（与日志文件同目录）。
#[tauri::command]
pub async fn open_log_directory(app: tauri::AppHandle) -> Result<(), String> {
    let dir = app.path().app_data_dir().map_err(|e| e.to_string())?;
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    open::that(&dir).map_err(|e| e.to_string())
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




