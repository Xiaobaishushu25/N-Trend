use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use chrono::{Local, NaiveDateTime};
use n_core::service::Services;
use n_core::storage;
use serde::Serialize;
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Emitter, Manager};
use tokio::time::Duration;

mod commands;
mod state;

use state::{AppState, SchedulerState};

pub static QUITTING: AtomicBool = AtomicBool::new(false);

const DEFAULT_SYMBOLS: &str = "# 每行一个期货代码\nRB0\nAU0\nIF0\n";

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .setup(|app| {
            init_logging(app)?;

            let data_dir = app_data_dir(app)?;
            std::fs::create_dir_all(&data_dir)?;
            let db_path = data_dir.join("ntrend.db");
            let db = tauri::async_runtime::block_on(storage::connect(&db_path))?;
            let services = tauri::async_runtime::block_on(Services::new(db))?;

            // 首启种子：优先读取工程根目录 symbols.txt（若有），否则内置代码表
            let mut seed_text = DEFAULT_SYMBOLS.to_string();
            if let Ok(text) = std::fs::read_to_string("symbols.txt") {
                if !text.trim().is_empty() {
                    seed_text = text;
                }
            }
            tauri::async_runtime::block_on(services.seed_symbols(&seed_text))?;

            // 首次运行兼容导入 email.toml
            let settings = tauri::async_runtime::block_on(services.settings());
            if settings.email.smtp_password.is_empty() && settings.email.from.is_empty() {
                let imported = n_core::service::import_email_toml(std::path::Path::new("email.toml"))?;
                if !imported.smtp_password.is_empty() || imported.from != n_core::notify::email::EmailSettings::default().from {
                    let mut next = settings;
                    next.email = imported;
                    tauri::async_runtime::block_on(services.apply_settings(next))?;
                }
            }

            let auto_start = tauri::async_runtime::block_on(services.settings()).auto_start_scheduler;
            let state = Arc::new(AppState {
                services,
                scheduler: tokio::sync::RwLock::new(SchedulerState {
                    running: auto_start,
                    last_refresh: None,
                    last_scan: None,
                }),
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
            spawn_scheduler(app.handle().clone(), state);
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
        })
        .invoke_handler(tauri::generate_handler![
            commands::get_symbols,
            commands::add_symbol,
            commands::remove_symbol,
            commands::set_symbol_flags,
            commands::refresh_symbol_list,
            commands::enrich_symbol_names,
            commands::get_klines,
            commands::get_market_snapshot,
            commands::refresh_data_now,
            commands::run_scan_now,
            commands::get_scan_history,
            commands::get_scan_detail,
            commands::get_latest_signals,
            commands::get_settings,
            commands::update_settings,
            commands::scheduler_status,
            commands::set_scheduler_running,
            commands::app_info,
        ])
        .run(tauri::generate_context!())
        .expect("运行 N趋势 失败");
}

fn app_data_dir(app: &tauri::App) -> anyhow::Result<std::path::PathBuf> {
    let dir = app.path().app_data_dir()?;
    Ok(dir)
}

fn init_logging(app: &tauri::App) -> anyhow::Result<()> {
    let dir = app_data_dir(app)?;
    std::fs::create_dir_all(&dir)?;
    let file_appender = tracing_appender::rolling::daily(&dir, "ntrend.log");
    let (writer, guard) = tracing_appender::non_blocking(file_appender);
    // 日志写线程随进程存活，guard 需要长期持有
    std::mem::forget(guard);
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")),
        )
        .with_writer(writer)
        .with_ansi(false)
        .init();
    Ok(())
}

fn spawn_scheduler(app: AppHandle, state: Arc<AppState>) {
    tauri::async_runtime::spawn(async move {
        let mut ticker = tokio::time::interval(Duration::from_secs(60));
        ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        loop {
            ticker.tick().await;
            let now = Local::now();
            let cfg = state.services.scheduler_config().await;
            let action = {
                let rt = state.scheduler.read().await;
                if !rt.running {
                    continue;
                }
                let last_refresh = rt
                    .last_refresh
                    .and_then(|t| t.and_local_timezone(Local).single());
                let last_scan = rt
                    .last_scan
                    .and_then(|t| t.and_local_timezone(Local).single());
                n_core::scheduler::next_action(now, &cfg, last_refresh, last_scan)
            };
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

async fn tick_refresh(app: &AppHandle, state: &Arc<AppState>) {
    match state.services.refresh_data().await {
        Ok(stats) => {
            state.scheduler.write().await.last_refresh = Some(Local::now().naive_local());
            let _ = app.emit("data-updated", &stats);
            tracing::info!("定时刷新完成: 成功 {} 失败 {}", stats.succeeded, stats.failures);
        }
        Err(e) => tracing::error!("定时刷新失败: {e}"),
    }
}

async fn tick_scan(app: &AppHandle, state: &Arc<AppState>) {
    match state.services.run_scan().await {
        Ok(res) => {
            state.scheduler.write().await.last_scan = Some(Local::now().naive_local());
            let _ = app.emit("scan-completed", &res);
            tracing::info!("定时扫描完成: 品种 {} 信号 {}", res.scanned, res.active_count);
            if res.active_count > 0 {
                let _ = app.emit("signal-found", &res.signals);
                let settings = state.services.settings().await;
                if settings.email.enabled && settings.email.sendable() {
                    let (subject, body) = n_core::notify::email::scan_email_payload(&res.summary);
                    if let Err(e) = n_core::notify::email::send_summary(&subject, &body, &settings.email) {
                        tracing::error!("邮件发送失败: {e}");
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
    let quit_item = MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&show_item, &quit_item])?;

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
            "quit" => {
                QUITTING.store(true, Ordering::SeqCst);
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
