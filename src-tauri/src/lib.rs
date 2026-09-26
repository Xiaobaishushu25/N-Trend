#[cfg(desktop)]
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Instant;

use chrono::Local;
#[cfg(desktop)]
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::Manager;
#[cfg(desktop)]
use tauri_plugin_window_state::{AppHandleExt, StateFlags};

pub mod client;
mod commands;
mod state;

use client::{BackupScheduler, CredentialStore, KlineMemoryCache, RealtimeClient, RemoteApiClient};
use state::AppState;

#[cfg(desktop)]
pub static QUITTING: AtomicBool = AtomicBool::new(false);

/// 日志时间：使用本地时间（北京时间），替代 tracing 默认的 UTC 时间。
#[derive(Clone, Debug)]
struct LocalTime;

impl tracing_subscriber::fmt::time::FormatTime for LocalTime {
    fn format_time(&self, w: &mut tracing_subscriber::fmt::format::Writer<'_>) -> std::fmt::Result {
        write!(w, "{}", Local::now().format("%Y-%m-%d %H:%M:%S%.3f"))
    }
}

/// 日志过滤规则：读取环境变量或默认 info
fn log_filter(level: &str) -> tracing_subscriber::EnvFilter {
    if std::env::var("RUST_LOG").is_ok() {
        return tracing_subscriber::EnvFilter::try_from_default_env()
            .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new(level));
    }
    let base = format!(
        "{level},reqwest=warn,rustls=warn,h2=warn,tungstenite=warn,tao=warn,wry=warn"
    );
    tracing_subscriber::EnvFilter::try_new(&base)
        .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info"))
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let builder = tauri::Builder::default().plugin(tauri_plugin_notification::init());

    #[cfg(desktop)]
    let builder = builder
        // 单实例：重复启动时不再创建新进程，而是唤起已有实例的主窗口
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.show();
                let _ = w.unminimize();
                let _ = w.set_focus();
            }
        }))
        // 开机自启：Windows 写注册表 Run 项，macOS 用 LaunchAgent
        .plugin(tauri_plugin_autostart::init(
            tauri_plugin_autostart::MacosLauncher::LaunchAgent,
            None,
        ))
        // 记住窗口位置/大小/最大化状态
        .plugin(
            tauri_plugin_window_state::Builder::default()
                .with_state_flags(StateFlags::all() & !StateFlags::VISIBLE)
                .build(),
        );

    builder
        .setup(|app| {
            let setup_t0 = Instant::now();
            let data_dir = app_data_dir(app)?;
            std::fs::create_dir_all(&data_dir)?;

            init_logging(&data_dir, "info")?;
            tracing::info!("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━");
            tracing::info!(
                "🚀 ntrend v{} 客户端启动 | 数据目录: {}",
                env!("CARGO_PKG_VERSION"),
                data_dir.display()
            );

            // 1. 初始化客户端模块
            let credentials = Arc::new(CredentialStore::new(&data_dir));
            let api = Arc::new(RemoteApiClient::new(credentials.clone()));
            let kline_cache = Arc::new(KlineMemoryCache::new());
            let realtime = RealtimeClient::new(app.handle().clone(), credentials.clone(), kline_cache.clone());
            let backup_scheduler = BackupScheduler::new(app.handle().clone(), api.clone(), &data_dir);

            // 2. 初始化全局客户端状态
            let state = Arc::new(AppState::new(
                &data_dir,
                api,
                realtime,
                kline_cache,
                credentials,
                backup_scheduler,
            ));
            app.manage(state);

            // 3. 桌面端设置系统托盘
            #[cfg(desktop)]
            setup_tray(app)?;

            tracing::info!("✅ 主窗口就绪 总耗时 {}ms", setup_t0.elapsed().as_millis());
            tracing::info!("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━");
            Ok(())
        })
        .on_window_event(|_window, _event| {
            #[cfg(desktop)]
            {
                if let tauri::WindowEvent::CloseRequested { api, .. } = _event {
                    if _window.label() == "main" && !QUITTING.load(Ordering::SeqCst) {
                        api.prevent_close();
                        let _ = _window.hide();
                    } else if _window.label() != "main" {
                        let _ = _window
                            .app_handle()
                            .save_window_state(StateFlags::all() & !StateFlags::VISIBLE);
                    }
                }
            }
        })
        .invoke_handler(tauri::generate_handler![
            commands::app_info,
            commands::set_window_size,
            commands::get_connection_status,
            commands::get_auth_record,
            commands::update_auth_record,
            commands::get_client_settings,
            commands::update_client_settings,
            commands::get_meta,
            commands::get_server_status,
            commands::record_notification,
            commands::get_notification_history,
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
            commands::set_symbol_followed,
            commands::refresh_symbol_list,
            commands::enrich_symbol_names,
            commands::get_klines,
            commands::get_chart_klines,
            commands::get_trend_series,
            commands::get_market_snapshot,
            commands::list_manual_levels,
            commands::create_manual_level,
            commands::update_manual_level,
            commands::set_manual_level_monitoring,
            commands::archive_manual_level,
            commands::delete_manual_level,
            commands::get_manual_level_events,
            commands::get_active_events,
            commands::refresh_data_now,
            commands::run_scan_now,
            commands::run_scan_fast_now,
            commands::rebuild_events_now,
            commands::refresh_outcomes_now,
            commands::get_review_stats,
            commands::get_recent_outcomes,
            commands::get_review_signal,
            commands::get_signal_user_data,
            commands::add_signal_annotation,
            commands::delete_signal_annotation,
            commands::set_signal_decision,
            commands::get_v2_models,
            commands::get_v2_dataset_report,
            commands::get_v2_predictions,
            commands::set_v2_model_status,
            commands::backfill_v2_predictions,
            commands::get_active_preclose_signals,
            commands::get_active_preclose_candidates,
            commands::get_preclose_candidates,
            commands::get_preclose_signals,
            commands::get_config,
            commands::update_config,
            commands::reset_config,
            commands::set_last_group,
            commands::set_timeframes,
            commands::get_server_settings,
            commands::update_server_settings,
            commands::scheduler_status,
            commands::set_scheduler_running,
            commands::restart_server,
            commands::restart_bridge,
            commands::list_devices,
            commands::revoke_device,
            commands::update_device_role,
            commands::pair_device,
            commands::get_backup_status,
            commands::trigger_database_backup,
            commands::open_backup_directory,
            commands::check_symbol_integrity,
            commands::check_all_symbols_integrity,
            commands::repair_symbol_integrity,
            commands::get_finality_report,
            commands::get_finality_simulation,
            commands::get_finality_sentinel_eval,
            commands::open_log_directory,
        ])
        .run(tauri::generate_context!())
        .expect("运行 N趋势 客户端失败");
}

fn app_data_dir(app: &tauri::App) -> anyhow::Result<std::path::PathBuf> {
    let dir = app.path().app_data_dir()?;
    Ok(dir)
}

fn init_logging(dir: &std::path::Path, level: &str) -> anyhow::Result<()> {
    std::fs::create_dir_all(dir)?;
    // 清理 14 天前的旧日志
    if let Ok(entries) = std::fs::read_dir(dir) {
        let now = std::time::SystemTime::now();
        for entry in entries.flatten() {
            if let Some(name) = entry.file_name().to_str() {
                if name.starts_with("ntrend.log") {
                    if let Ok(meta) = entry.metadata() {
                        if let Ok(modified) = meta.modified() {
                            if let Ok(elapsed) = now.duration_since(modified) {
                                if elapsed.as_secs() > 14 * 24 * 3600 {
                                    let _ = std::fs::remove_file(entry.path());
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    let file_appender = tracing_appender::rolling::daily(dir, "ntrend.log");
    let (writer, guard) = tracing_appender::non_blocking(file_appender);
    std::mem::forget(guard);
    tracing_subscriber::fmt()
        .with_env_filter(log_filter(level))
        .with_timer(LocalTime)
        .with_writer(writer)
        .with_ansi(false)
        .with_target(false)
        .init();
    Ok(())
}

#[cfg(desktop)]
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

#[cfg(desktop)]
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
    .resizable(true)
    .decorations(false)
    .build();
}
