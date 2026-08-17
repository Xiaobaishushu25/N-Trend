use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use chrono::{Local, NaiveDateTime};
use n_core::config::Config;
use n_core::service::Services;
use n_core::storage;
use serde::Serialize;
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Emitter, Manager};
use tauri_plugin_window_state::{AppHandleExt, StateFlags};
use tokio::time::Duration;

mod commands;
mod state;

use state::{AppState, SchedulerState, KEY_LAST_REFRESH, KEY_LAST_SCAN};

pub static QUITTING: AtomicBool = AtomicBool::new(false);

const DEFAULT_SYMBOLS: &str = "# 每行一个期货代码\nRB0\nAU0\nIF0\n";

/// 日志时间：使用本地时间（北京时间），替代 tracing 默认的 UTC 时间。
#[derive(Clone, Debug)]
struct LocalTime;

impl tracing_subscriber::fmt::time::FormatTime for LocalTime {
    fn format_time(&self, w: &mut tracing_subscriber::fmt::format::Writer<'_>) -> std::fmt::Result {
        write!(w, "{}", Local::now().format("%Y-%m-%d %H:%M:%S%.3f"))
    }
}

/// 日志过滤规则：读取配置中的日志级别；RUST_LOG 环境变量仍可整体覆盖。
fn log_filter(level: &str) -> tracing_subscriber::EnvFilter {
    tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| {
        tracing_subscriber::EnvFilter::try_new(level)
            .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info"))
    })
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        // 单实例：重复启动时不再创建新进程，而是唤起已有实例的主窗口
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.show();
                let _ = w.unminimize();
                let _ = w.set_focus();
            }
        }))
        .plugin(tauri_plugin_notification::init())
        // 开机自启：Windows 写注册表 Run 项，macOS 用 LaunchAgent（当前仅在 Windows 部署）
        .plugin(tauri_plugin_autostart::init(
            tauri_plugin_autostart::MacosLauncher::LaunchAgent,
            None,
        ))
        // 记住窗口位置/大小/最大化状态；不保存 VISIBLE（本应用关闭是隐藏到托盘，
        // 若保存可见性，退出时窗口处于隐藏状态会导致下次启动时窗口不可见）
        .plugin(
            tauri_plugin_window_state::Builder::default()
                .with_state_flags(StateFlags::all() & !StateFlags::VISIBLE)
                .build(),
        )
        .setup(|app| {
            let data_dir = app_data_dir(app)?;
            std::fs::create_dir_all(&data_dir)?;
            let db_path = data_dir.join("ntrend.db");
            let db = tauri::async_runtime::block_on(storage::connect(&db_path))?;

            // 配置：JSON 文件优先，旧版 DB 设置首次启动自动迁移
            let config_path = data_dir.join("config.json");
            let config = tauri::async_runtime::block_on(Config::load(&config_path, &db))?;
            init_logging(&data_dir, &config.log.level)?;

            let services =
                tauri::async_runtime::block_on(Services::new(db, config.clone(), config_path))?;

            // 首启种子：优先读取工程根目录 symbols.txt（若有），否则内置代码表
            let mut seed_text = DEFAULT_SYMBOLS.to_string();
            if let Ok(text) = std::fs::read_to_string("symbols.txt") {
                if !text.trim().is_empty() {
                    seed_text = text;
                }
            }
            tauri::async_runtime::block_on(services.seed_symbols(&seed_text))?;

            // 首次运行兼容导入 email.toml
            let current = tauri::async_runtime::block_on(services.config());
            if current.email.smtp_password.is_empty() && current.email.from.is_empty() {
                let imported =
                    n_core::config::import_email_toml(std::path::Path::new("email.toml"))?;
                if !imported.smtp_password.is_empty()
                    || imported.from != n_core::notify::email::EmailSettings::default().from
                {
                    let mut next = current;
                    next.email = imported;
                    tauri::async_runtime::block_on(services.apply_config(next))?;
                }
            }

            // 补齐品种默认精度（未显式设置的行）
            let _ = tauri::async_runtime::block_on(services.backfill_tick_sizes());

            let auto_start = tauri::async_runtime::block_on(services.config())
                .app_config
                .auto_start_scheduler;
            // 恢复上次运行记录的最后成功时间，避免应用重启后丢失
            let saved =
                tauri::async_runtime::block_on(n_core::storage::repo::all_settings(&services.db))
                    .unwrap_or_default();
            let last_refresh = saved
                .get(KEY_LAST_REFRESH)
                .and_then(|s| NaiveDateTime::parse_from_str(s, "%Y-%m-%d %H:%M:%S").ok());
            let last_scan = saved
                .get(KEY_LAST_SCAN)
                .and_then(|s| NaiveDateTime::parse_from_str(s, "%Y-%m-%d %H:%M:%S").ok());
            let state = Arc::new(AppState {
                services,
                scheduler: tokio::sync::RwLock::new(SchedulerState {
                    running: auto_start,
                    last_refresh,
                    last_scan,
                    // 调度起点先用最近一次成功时间：重启后若已超过间隔，首个 tick 即补跑
                    refresh_anchor: last_refresh,
                    scan_anchor: last_scan,
                }),
                notification_history: std::sync::Mutex::new(Vec::new()),
                next_notification_id: std::sync::atomic::AtomicU64::new(1),
            });
            app.manage(state.clone());
            // 后台补齐品种名称：不阻塞启动，完成后通知前端刷新；成功后打标记避免每次启动重复联网
            {
                let enrich_app = app.handle().clone();
                let enrich_state = state.clone();
                tauri::async_runtime::spawn(async move {
                    use n_core::storage::repo;
                    let already_done = repo::all_settings(&enrich_state.services.db)
                        .await
                        .map(|m| m.get("names_enriched").map(String::as_str) == Some("1"))
                        .unwrap_or(false);
                    if already_done {
                        return;
                    }
                    match enrich_state.services.needs_name_enrich().await {
                        Ok(false) => {
                            let mut map = std::collections::HashMap::new();
                            map.insert("names_enriched".to_string(), "1".to_string());
                            let _ = repo::set_settings(&enrich_state.services.db, &map).await;
                            return;
                        }
                        Ok(true) => {}
                        Err(e) => {
                            tracing::warn!("检查品种名称失败: {e}");
                            return;
                        }
                    }
                    match enrich_state.services.enrich_existing_symbols().await {
                        Ok(n) => {
                            tracing::info!("已补齐 {n} 个品种的名称");
                            let mut map = std::collections::HashMap::new();
                            map.insert("names_enriched".to_string(), "1".to_string());
                            let _ = repo::set_settings(&enrich_state.services.db, &map).await;
                            let _ = enrich_app.emit("symbols-updated", n);
                        }
                        Err(e) => tracing::warn!("补齐品种名称失败: {e}"),
                    }
                });
            }
            spawn_scheduler(app.handle().clone(), state.clone());
            spawn_quote_poller(app.handle().clone(), state);
            setup_tray(app)?;
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                if window.label() == "main" && !QUITTING.load(Ordering::SeqCst) {
                    api.prevent_close();
                    let _ = window.hide();
                }
            }
            // 复盘统计等子窗口关闭后立即落盘，避免下次打开时又回到默认大小
            if let tauri::WindowEvent::Destroyed = event {
                if window.label() != "main" {
                    let _ = window
                        .app_handle()
                        .save_window_state(StateFlags::all() & !StateFlags::VISIBLE);
                }
            }
        })
        .invoke_handler(tauri::generate_handler![
            commands::get_symbols,
            commands::list_groups,
            commands::create_group,
            commands::rename_group,
            commands::delete_group,
            commands::get_group_symbols,
            commands::list_symbol_groups,
            commands::add_symbol_to_group,
            commands::remove_symbol_from_group,
            commands::reorder_groups,
            commands::get_group_all_position,
            commands::reorder_group_symbols,
            commands::reorder_symbols,
            commands::add_symbol,
            commands::search_contracts,
            commands::remove_symbol,
            commands::set_symbol_flags,
            commands::set_symbol_tick,
            commands::refresh_symbol_list,
            commands::enrich_symbol_names,
            commands::get_klines,
            commands::get_trend_series,
            commands::get_market_snapshot,
            commands::refresh_data_now,
            commands::run_scan_now,
            commands::rebuild_events_now,
            commands::refresh_outcomes_now,
            commands::get_review_stats,
            commands::get_recent_outcomes,
            commands::get_review_signal,
            commands::get_config,
            commands::update_config,
            commands::set_last_group,
            commands::set_timeframes,
            commands::reset_config,
            commands::open_log_directory,
            commands::scheduler_status,
            commands::set_scheduler_running,
            commands::app_info,
            commands::record_notification,
            commands::get_notification_history,
        ])
        .run(tauri::generate_context!())
        .expect("运行 N趋势 失败");
}

fn app_data_dir(app: &tauri::App) -> anyhow::Result<std::path::PathBuf> {
    let dir = app.path().app_data_dir()?;
    Ok(dir)
}

fn init_logging(dir: &std::path::Path, level: &str) -> anyhow::Result<()> {
    std::fs::create_dir_all(dir)?;
    let file_appender = tracing_appender::rolling::daily(dir, "ntrend.log");
    let (writer, guard) = tracing_appender::non_blocking(file_appender);
    // 日志写线程随进程存活，guard 需要长期持有
    std::mem::forget(guard);
    tracing_subscriber::fmt()
        .with_env_filter(log_filter(level))
        .with_timer(LocalTime)
        .with_writer(writer)
        .with_ansi(false)
        .init();
    Ok(())
}

fn spawn_scheduler(app: AppHandle, state: Arc<AppState>) {
    tauri::async_runtime::spawn(async move {
        // 15 秒一跳：比 60 秒更接近计划时刻，长时间任务结束后也能尽快补上节奏
        let mut ticker = tokio::time::interval(Duration::from_secs(15));
        ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        // 启动特例：刷新改为分钟网格对齐后，启动时若不在边界时刻会最多等一个周期才有数据，
        // 因此在交易时段内的首次 tick 强制刷新一次，之后回到边界对齐节奏
        let mut startup_refresh_done = false;
        loop {
            ticker.tick().await;
            let now = Local::now();
            let cfg = state.services.scheduler_config().await;
            let mut action = {
                let rt = state.scheduler.read().await;
                if !rt.running {
                    continue;
                }
                let last_refresh = rt
                    .refresh_anchor
                    .and_then(|t| t.and_local_timezone(Local).single());
                let last_scan = rt
                    .scan_anchor
                    .and_then(|t| t.and_local_timezone(Local).single());
                n_core::scheduler::next_action(now, &cfg, last_refresh, last_scan)
            };
            if !startup_refresh_done {
                startup_refresh_done = true;
                if action == n_core::scheduler::SchedulerAction::None
                    && n_core::scheduler::is_trading_time(&now)
                {
                    action = n_core::scheduler::SchedulerAction::Refresh;
                }
            }
            match action {
                n_core::scheduler::SchedulerAction::None => {}
                n_core::scheduler::SchedulerAction::Refresh => {
                    tick_refresh(&app, &state).await;
                }
                n_core::scheduler::SchedulerAction::Scan => {
                    tick_scan(&app, &state).await;
                }
                n_core::scheduler::SchedulerAction::RefreshAndScan => {
                    tick_refresh(&app, &state).await;
                    tick_scan(&app, &state).await;
                }
            }
        }
    });
}

/// 实时现价轮询：交易时段内按配置的轮询间隔批量拉一次
/// 新浪实时行情并推送 `quote-updated` 事件，前端订阅后原地更新价格。
/// 与调度器共用“运行/暂停”开关；盘外价格不变化，不发起请求。
fn spawn_quote_poller(app: AppHandle, state: Arc<AppState>) {
    tauri::async_runtime::spawn(async move {
        loop {
            // 轮询间隔从配置实时读取，保存设置后下一轮即生效
            let interval_ms = state.services.config().await.quote.poll_interval_ms;
            tokio::time::sleep(Duration::from_millis(interval_ms)).await;
            let now = Local::now();
            {
                let rt = state.scheduler.read().await;
                if !rt.running {
                    continue;
                }
            }
            if !n_core::scheduler::is_trading_time(&now) {
                continue;
            }
            match state.services.realtime_quotes().await {
                Ok(snapshots) => {
                    // 每次轮询后对比形态入场点，命中则广播事件（去重在前端/后端均处理）
                    if let Ok(hits) = state.services.entry_trigger_hits(&snapshots).await {
                        if !hits.is_empty() {
                            let _ = app.emit("entry-trigger", &hits);
                        }
                    }
                    let _ = app.emit("quote-updated", &snapshots);
                }
                Err(e) => tracing::warn!("实时行情轮询失败: {e}"),
            }
        }
    });
}

async fn tick_refresh(app: &AppHandle, state: &Arc<AppState>) {
    // 先把本次尝试的起点记下来：节奏从起点起算，拉取耗时不会把下一次向后推
    state.scheduler.write().await.refresh_anchor = Some(Local::now().naive_local());
    match state.services.refresh_data().await {
        Ok(stats) => {
            state.note_refresh_success().await;
            let _ = app.emit("data-updated", &stats);
            tracing::info!(
                "定时刷新完成: 成功 {} 失败 {}",
                stats.succeeded,
                stats.failures
            );
        }
        Err(e) => tracing::error!("定时刷新失败: {e}"),
    }
}

async fn tick_scan(app: &AppHandle, state: &Arc<AppState>) {
    state.scheduler.write().await.scan_anchor = Some(Local::now().naive_local());
    match state.services.run_scan().await {
        Ok(res) => {
            state.note_scan_success().await;
            let _ = app.emit("scan-completed", &res);
            tracing::info!(
                "定时扫描完成: 品种 {} 信号 {}",
                res.scanned,
                res.active_count
            );
            let cfg = state.services.config().await;
            let min_score = cfg.notify.new_pattern_min_score;
            if cfg.email.enabled && cfg.email.sendable() {
                let mut emails = Vec::new();
                for e in res.new_warnings.iter().filter(|e| e.entry_score >= min_score) {
                    emails.push((
                        n_core::notify::email::EventEmailKind::Warning,
                        e,
                    ));
                }
                for e in res
                    .newly_triggered
                    .iter()
                    .filter(|e| e.entry_score >= min_score)
                {
                    emails.push((n_core::notify::email::EventEmailKind::Trigger, e));
                }
                for (kind, e) in emails {
                    let (subject, body) = n_core::notify::email::event_email_payload(kind, e);
                    if let Err(err) = n_core::notify::email::send_summary(&subject, &body, &cfg.email) {
                        tracing::error!("邮件发送失败: {err}");
                    }
                }
            }
        }
        Err(e) => tracing::error!("定时扫描失败: {e}"),
    }
}

fn setup_tray(app: &tauri::App) -> anyhow::Result<()> {
    use tauri::menu::{Menu, MenuItem};

    let show_item = MenuItem::with_id(app, "show", "显示主窗口", true, None::<&str>)?;
    let settings_item = MenuItem::with_id(app, "settings", "设置", true, None::<&str>)?;
    let quit_item = MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&show_item, &settings_item, &quit_item])?;

    let mut builder = TrayIconBuilder::with_id("main-tray")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "show" => {
                if let Some(w) = app.get_webview_window("main") {
                    let _ = w.show();
                    let _ = w.unminimize();
                    let _ = w.set_focus();
                }
            }
            "settings" => {
                open_settings_window(app);
            }
            "quit" => {
                QUITTING.store(true, Ordering::SeqCst);
                let _ = app.save_window_state(StateFlags::all() & !StateFlags::VISIBLE);
                app.exit(0);
            }
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                let app = tray.app_handle();
                if let Some(w) = app.get_webview_window("main") {
                    let _ = w.show();
                    let _ = w.set_focus();
                }
            }
        });
    if let Some(icon) = app.default_window_icon() {
        builder = builder.icon(icon.clone());
    }
    builder.build(app)?;
    Ok(())
}

/// 打开设置窗口：已存在则聚焦，否则新建独立窗口。
/// 与主窗口一致使用自定义 titlebar（无系统装饰）。
fn open_settings_window(app: &tauri::AppHandle) {
    if let Some(w) = app.get_webview_window("settings") {
        let _ = w.show();
        let _ = w.unminimize();
        let _ = w.set_focus();
        return;
    }
    let _ = tauri::WebviewWindowBuilder::new(
        app,
        "settings",
        tauri::WebviewUrl::App("index.html#/settings".into()),
    )
    .title("设置")
    .inner_size(760.0, 640.0)
    .min_inner_size(680.0, 520.0)
    .center()
    .resizable(true)
    .decorations(false)
    .build();
}

// 供命令层读取状态使用
pub(crate) fn fmt_naive(t: NaiveDateTime) -> String {
    t.format("%Y-%m-%d %H:%M:%S").to_string()
}

#[derive(Debug, Clone, Serialize)]
pub struct AppInfo {
    pub name: String,
    pub version: String,
}

// build-001800

// layout-003532
// wheel-004322
// signal-consistency-005058

// global-zoom-010805
// margin-8-011518
// gaps+max-012837
// pricegap-013445
// gapfill-convention-1430
// sessionbreak-gapfilter-1605

// bg-enrich-013841
