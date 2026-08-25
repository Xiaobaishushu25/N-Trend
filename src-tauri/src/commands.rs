use std::time::Instant;
use std::sync::Arc;

use n_core::analyze::outcome::ReviewStats;
use n_core::config::Config;
use n_core::service::{
    KlineDto, MarketSnapshot, OutcomeDetail, OutcomeRefresh, RefreshStats, ReviewSignalDetail,
    ScanResult, SignalAnnotationDto, SignalDecisionDto, SignalUserData, TrendPointDto,
};
use n_core::storage::entities::{groups, symbols};
use serde::Serialize;
use tauri::{Emitter, Manager, State};

use crate::state::{AppState, NewNotificationHistoryItem, NotificationHistoryItem};
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
pub async fn record_notification(
    app: tauri::AppHandle,
    state: State<'_, Arc<AppState>>,
    item: NewNotificationHistoryItem,
) -> Result<Vec<NotificationHistoryItem>, String> {
    state.record_notification(item);
    let history = state.notification_history();
    let _ = app.emit("notification-history-updated", &history);
    Ok(history)
}

#[tauri::command]
pub async fn get_notification_history(
    state: State<'_, Arc<AppState>>,
) -> Result<Vec<NotificationHistoryItem>, String> {
    Ok(state.notification_history())
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
    let t0 = Instant::now();
    tracing::info!("👆 用户添加品种 | {}", code);
    match state.services.add_symbol(&code).await {
        Ok(n) => {
            tracing::info!("✅ 添加品种完成 | {} 耗时 {}ms | 新增 {} 条K线", code, t0.elapsed().as_millis(), n);
            Ok(n)
        }
        Err(e) => {
            tracing::error!("❌ 添加品种失败 | {} 耗时 {}ms | {e}", code, t0.elapsed().as_millis());
            Err(e.to_string())
        }
    }
}

#[tauri::command]
pub async fn search_contracts(
    state: State<'_, Arc<AppState>>,
    keyword: String,
) -> Result<Vec<n_core::fetch::symbols::FuturesSymbol>, String> {
    state
        .services
        .search_contracts(&keyword)
        .await
        .map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn remove_symbol(state: State<'_, Arc<AppState>>, code: String) -> Result<(), String> {
    let t0 = Instant::now();
    tracing::info!("👆 用户移除品种 | {}", code);
    match state.services.remove_symbol(&code).await {
        Ok(()) => {
            tracing::info!("✅ 移除品种完成 | {} 耗时 {}ms", code, t0.elapsed().as_millis());
            Ok(())
        }
        Err(e) => {
            tracing::error!("❌ 移除品种失败 | {} 耗时 {}ms | {e}", code, t0.elapsed().as_millis());
            Err(e.to_string())
        }
    }
}

#[tauri::command]
pub async fn set_symbol_flags(
    state: State<'_, Arc<AppState>>,
    code: String,
    watchlist: bool,
    enabled: bool,
) -> Result<(), String> {
    tracing::info!("👆 更新品种标记 | {} watchlist={} enabled={}", code, watchlist, enabled);
    match n_core::storage::repo::set_symbol_flags(&state.services.db, &code, watchlist, enabled).await {
        Ok(()) => {
            tracing::info!("✅ 品种标记已更新 | {}", code);
            Ok(())
        }
        Err(e) => {
            tracing::error!("❌ 更新品种标记失败 | {} | {e}", code);
            Err(e.to_string())
        }
    }
}

/// 更新品种最小变动价位（tick）。
#[tauri::command]
pub async fn set_symbol_tick(
    state: State<'_, Arc<AppState>>,
    code: String,
    tick: f64,
) -> Result<(), String> {
    tracing::info!("👆 更新品种 tick | {} -> {}", code, tick);
    match n_core::storage::repo::set_symbol_tick(&state.services.db, &code, tick).await {
        Ok(()) => {
            tracing::info!("✅ 品种 tick 已更新 | {}", code);
            Ok(())
        }
        Err(e) => {
            tracing::error!("❌ 更新品种 tick 失败 | {} | {e}", code);
            Err(e.to_string())
        }
    }
}

#[tauri::command]
pub async fn enrich_symbol_names(state: State<'_, Arc<AppState>>) -> Result<usize, String> {
    let t0 = Instant::now();
    tracing::info!("👆 用户触发补齐品种名称");
    match state.services.enrich_existing_symbols().await {
        Ok(n) => {
            tracing::info!("✅ 补齐品种名称完成 耗时 {}ms | 共 {} 个", t0.elapsed().as_millis(), n);
            Ok(n)
        }
        Err(e) => {
            tracing::error!("❌ 补齐品种名称失败 耗时 {}ms | {e}", t0.elapsed().as_millis());
            Err(e.to_string())
        }
    }
}

#[tauri::command]
pub async fn refresh_symbol_list(state: State<'_, Arc<AppState>>) -> Result<usize, String> {
    let t0 = Instant::now();
    tracing::info!("👆 用户触发刷新可交易品种列表");
    match state.services.refresh_symbol_list().await {
        Ok(n) => {
            tracing::info!("✅ 品种列表刷新完成 耗时 {}ms | 共 {} 个", t0.elapsed().as_millis(), n);
            Ok(n)
        }
        Err(e) => {
            tracing::error!("❌ 品种列表刷新失败 耗时 {}ms | {e}", t0.elapsed().as_millis());
            Err(e.to_string())
        }
    }
}

#[tauri::command]
pub async fn get_klines(
    state: State<'_, Arc<AppState>>,
    symbol: String,
    timeframe: String,
    limit: Option<usize>,
) -> Result<Vec<KlineDto>, String> {
    state
        .services
        .get_klines(&symbol, &timeframe, limit)
        .await
        .map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn get_trend_series(
    state: State<'_, Arc<AppState>>,
    symbol: String,
    timeframe: String,
    limit: Option<usize>,
) -> Result<Vec<TrendPointDto>, String> {
    state
        .services
        .trend_series(&symbol, &timeframe, limit)
        .await
        .map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn get_market_snapshot(
    state: State<'_, Arc<AppState>>,
) -> Result<Vec<MarketSnapshot>, String> {
    state
        .services
        .market_snapshot()
        .await
        .map_err(|e| e.to_string())
}

/// 首屏轻量读取：直接返回 DB 中 pending / triggered 的活跃信号，不触发扫描计算（秒级）
#[tauri::command]
pub async fn get_active_events(
    state: State<'_, Arc<AppState>>,
) -> Result<Vec<n_core::storage::entities::pattern_events::Model>, String> {
    let events = n_core::storage::repo::all_pattern_events(&state.services.db)
        .await
        .map_err(|e| e.to_string())?;
    Ok(events
        .into_iter()
        .filter(|e| e.state == "pending" || e.state == "triggered")
        .collect())
}

#[tauri::command]
pub async fn refresh_data_now(state: State<'_, Arc<AppState>>) -> Result<RefreshStats, String> {
    let t0 = Instant::now();
    tracing::info!("👆 用户手动触发刷新数据");
    match state.services.refresh_data().await {
        Ok(stats) => {
            state.note_refresh_success().await;
            tracing::info!(
                "✅ 手动刷新完成 耗时 {}ms | 成功 {} 失败 {} | 总计 {}",
                t0.elapsed().as_millis(),
                stats.succeeded,
                stats.failures,
                stats.succeeded + stats.failures
            );
            if stats.failures > 0 {
                tracing::warn!("⚠ 手动刷新有 {} 个品种失败", stats.failures);
            }
            Ok(stats)
        }
        Err(e) => {
            tracing::error!("❌ 手动刷新失败 耗时 {}ms | {e}", t0.elapsed().as_millis());
            Err(e.to_string())
        }
    }
}

#[tauri::command]
pub async fn run_scan_now(
    app: tauri::AppHandle,
    state: State<'_, Arc<AppState>>,
) -> Result<ScanResult, String> {
    let t0 = Instant::now();
    tracing::info!("👆 用户手动触发立即扫描");
    match state.services.run_scan().await {
        Ok(result) => {
            state.note_scan_success().await;
            let _ = app.emit("scan-completed", &result);
            tracing::info!(
                "✅ 手动扫描完成 耗时 {}ms | 扫描 {} 活跃 {} 新增预警 {} 新触发 {}",
                t0.elapsed().as_millis(),
                result.scanned,
                result.active_count,
                result.new_warnings.len(),
                result.newly_triggered.len()
            );
            Ok(result)
        }
        Err(e) => {
            tracing::error!("❌ 手动扫描失败 耗时 {}ms | {e}", t0.elapsed().as_millis());
            Err(e.to_string())
        }
    }
}

#[tauri::command]
pub async fn rebuild_events_now(
    app: tauri::AppHandle,
    state: State<'_, Arc<AppState>>,
) -> Result<ScanResult, String> {
    let t0 = Instant::now();
    tracing::info!("👆 用户手动触发重建事件");
    if let Err(e) = state.services.rebuild_events().await {
        tracing::error!("❌ 重建事件失败 耗时 {}ms | {e}", t0.elapsed().as_millis());
        return Err(e.to_string());
    }
    tracing::info!("✓ 事件表已清空重建 耗时 {}ms，开始重新扫描…", t0.elapsed().as_millis());
    state
        .notification_history
        .lock()
        .expect("通知历史锁可用")
        .clear();
    let empty_history: Vec<NotificationHistoryItem> = Vec::new();
    let _ = app.emit("notification-history-updated", &empty_history);
    match state.services.run_scan().await {
        Ok(result) => {
            state.note_scan_success().await;
            let _ = app.emit("scan-completed", &result);
            tracing::info!(
                "✅ 重建后扫描完成 总耗时 {}ms | 扫描 {} 活跃 {} 新增预警 {} 新触发 {}",
                t0.elapsed().as_millis(),
                result.scanned,
                result.active_count,
                result.new_warnings.len(),
                result.newly_triggered.len()
            );
            Ok(result)
        }
        Err(e) => {
            tracing::error!("❌ 重建后扫描失败 总耗时 {}ms | {e}", t0.elapsed().as_millis());
            Err(e.to_string())
        }
    }
}

/// 立即对未终结信号做一次结局回填（复盘页"刷新"按钮）。
#[tauri::command]
pub async fn refresh_outcomes_now(
    state: State<'_, Arc<AppState>>,
) -> Result<OutcomeRefresh, String> {
    let t0 = Instant::now();
    tracing::info!("👆 用户手动触发结局回填");
    match state.services.refresh_outcomes().await {
        Ok(r) => {
            tracing::info!(
                "✅ 结局回填完成 耗时 {}ms | 已更新 {}",
                t0.elapsed().as_millis(),
                r.updated
            );
            Ok(r)
        }
        Err(e) => {
            tracing::error!("❌ 结局回填失败 耗时 {}ms | {e}", t0.elapsed().as_millis());
            Err(e.to_string())
        }
    }
}

/// 复盘统计：按维度分组（score_band/grade/direction/level/hour/symbol/vol_confirm/oi/trend60）。
#[tauri::command]
pub async fn get_review_stats(
    state: State<'_, Arc<AppState>>,
    dimension: String,
    scope: Option<String>,
    version: Option<String>,
    score_min: Option<f64>,
    score_max: Option<f64>,
) -> Result<ReviewStats, String> {
    let scope = scope.unwrap_or_default();
    state
        .services
        .review_stats(&dimension, &scope, version.as_deref(), score_min, score_max)
        .await
        .map_err(|e| e.to_string())
}

/// 最近信号明细（复盘页明细表）。
#[tauri::command]
pub async fn get_recent_outcomes(
    state: State<'_, Arc<AppState>>,
    limit: Option<u64>,
    symbol: Option<String>,
    direction: Option<String>,
    level: Option<String>,
    grade: Option<String>,
    score_min: Option<f64>,
    score_max: Option<f64>,
    outcome: Option<String>,
    version: Option<String>,
) -> Result<Vec<OutcomeDetail>, String> {
    let filter = n_core::service::OutcomeFilter {
        symbol,
        direction,
        level,
        grade,
        score_min,
        score_max,
        outcome,
        version,
    };
    state
        .services
        .recent_outcomes(limit.unwrap_or(2000) as usize, &filter)
        .await
        .map_err(|e| e.to_string())
}

/// 复盘明细跳转K线图：按 signal_id 返回完整形态结构 + 结局。
#[tauri::command]
pub async fn get_review_signal(
    state: State<'_, Arc<AppState>>,
    event_id: i64,
) -> Result<Option<ReviewSignalDetail>, String> {
    state
        .services
        .review_signal(event_id)
        .await
        .map_err(|e| e.to_string())
}

/// K线右侧卡片：读取某个信号的批注与开仓记录。
#[tauri::command]
pub async fn get_signal_user_data(
    state: State<'_, Arc<AppState>>,
    event_id: i64,
) -> Result<SignalUserData, String> {
    state
        .services
        .signal_user_data(event_id)
        .await
        .map_err(|e| e.to_string())
}

/// 给某个信号追加一条批注。
#[tauri::command]
pub async fn add_signal_annotation(
    state: State<'_, Arc<AppState>>,
    event_id: i64,
    content: String,
) -> Result<SignalAnnotationDto, String> {
    state
        .services
        .add_signal_annotation(event_id, &content)
        .await
        .map_err(|e| e.to_string())
}

/// 删除一条批注。
#[tauri::command]
pub async fn delete_signal_annotation(
    state: State<'_, Arc<AppState>>,
    id: i64,
) -> Result<(), String> {
    state
        .services
        .delete_signal_annotation(id)
        .await
        .map_err(|e| e.to_string())
}

/// 记录/修改某个信号是否按建议开仓。
#[tauri::command]
pub async fn set_signal_decision(
    state: State<'_, Arc<AppState>>,
    event_id: i64,
    opened: bool,
) -> Result<SignalDecisionDto, String> {
    state
        .services
        .set_signal_decision(event_id, opened)
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
    let old = state.services.config().await;
    tracing::info!(
        "👆 用户更新配置 | 刷新 {}s->{}s 扫描 {}s->{}s 交易时段 {}->{} 日志 {}->{}",
        old.scheduler.refresh_interval_secs, config.scheduler.refresh_interval_secs,
        old.scheduler.scan_interval_secs, config.scheduler.scan_interval_secs,
        old.scheduler.trading_only, config.scheduler.trading_only,
        old.log.level, config.log.level
    );
    match state.services.apply_config(config).await {
        Ok(c) => {
            tracing::info!("✅ 配置已更新");
            if old.log.level != c.log.level {
                tracing::info!("ℹ 日志级别已改为 {}，重启后生效", c.log.level);
            }
            Ok(c)
        }
        Err(e) => {
            tracing::error!("❌ 配置更新失败 | {e}");
            Err(e.to_string())
        }
    }
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
    tracing::info!("👆 用户更新K线周期 | {:?}", timeframes);
    match state.services.set_timeframes(timeframes).await {
        Ok(()) => {
            tracing::info!("✅ K线周期已更新");
            Ok(())
        }
        Err(e) => {
            tracing::error!("❌ 更新K线周期失败 | {e}");
            Err(e.to_string())
        }
    }
}

/// 将所有配置恢复为默认值，返回新的默认配置。
#[tauri::command]
pub async fn reset_config(state: State<'_, Arc<AppState>>) -> Result<Config, String> {
    tracing::info!("👆 用户重置配置为默认值");
    match state.services.reset_config().await {
        Ok(c) => {
            tracing::info!("✅ 配置已重置");
            Ok(c)
        }
        Err(e) => {
            tracing::error!("❌ 配置重置失败 | {e}");
            Err(e.to_string())
        }
    }
}

/// 打开日志目录（与日志文件同目录）。
#[tauri::command]
pub async fn open_log_directory(app: tauri::AppHandle) -> Result<(), String> {
    tracing::info!("👆 用户打开日志目录");
    let dir = app.path().app_data_dir().map_err(|e| {
        tracing::error!("❌ 获取日志目录失败 | {e}");
        e.to_string()
    })?;
    std::fs::create_dir_all(&dir).map_err(|e| {
        tracing::error!("❌ 创建日志目录失败 | {e}");
        e.to_string()
    })?;
    open::that(&dir).map_err(|e| {
        tracing::error!("❌ 打开日志目录失败 | {e}");
        e.to_string()
    })?;
    tracing::info!("✅ 已打开日志目录 | {}", dir.display());
    Ok(())
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
    tracing::info!("👆 用户{}调度器", if running { "启动" } else { "暂停" });
    state.scheduler.write().await.running = running;
    let s = scheduler_status(state).await?;
    tracing::info!("✅ 调度器已{} | 运行中: {}", if running { "启动" } else { "暂停" }, s.running);
    Ok(s)
}
