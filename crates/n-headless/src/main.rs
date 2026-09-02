use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Instant;
use chrono::Local;
use n_core::config::Config;
use n_core::service::Services;
use n_core::storage;
use tracing_subscriber::prelude::*;
#[derive(Clone, Debug)]
struct LocalTime;
impl tracing_subscriber::fmt::time::FormatTime for LocalTime {
    fn format_time(&self, w: &mut tracing_subscriber::fmt::format::Writer<'_>) -> std::fmt::Result {
        write!(w, "{}", Local::now().format("%Y-%m-%d %H:%M:%S%.3f"))
    }
}
fn log_filter(level: &str) -> tracing_subscriber::EnvFilter {
    if std::env::var("RUST_LOG").is_ok() {
        return tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| tracing_subscriber::EnvFilter::new(level));
    }
    let base = format!("{level},sqlx=warn,sea-orm=warn,sea_orm=warn,hyper=warn,reqwest=warn,rustls=warn,h2=warn");
    tracing_subscriber::EnvFilter::try_new(&base).unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info,sqlx=warn,sea-orm=warn"))
}
fn peek_log_level(path: &Path) -> Option<String> {
    let text = std::fs::read_to_string(path).ok()?;
    let v: serde_json::Value = serde_json::from_str(&text).ok()?;
    v.get("log")?.get("level")?.as_str().map(|s| s.to_string())
}
fn resolve_data_dir() -> PathBuf {
    let mut args = std::env::args().skip(1);
    while let Some(a) = args.next() {
        if a == "--data-dir" { if let Some(v) = args.next() { return PathBuf::from(v); } }
        if a.starts_with("--data-dir=") { return PathBuf::from(a.trim_start_matches("--data-dir=")); }
    }
    if let Ok(v) = std::env::var("NTREND_DATA_DIR") { if !v.trim().is_empty() { return PathBuf::from(v); } }
    PathBuf::from("./data")
}
fn print_help() {
    println!(r#"n-headless - ntrend 无头版
用法:
  cargo run -p n-headless -- --data-dir ./data
  cargo run -p n-headless -- --data-dir /opt/ntrend/data
  cargo run -p n-headless -- --data-dir ./data --report-finality
  cargo run -p n-headless -- --data-dir ./data --simulate-finality
  cargo run -p n-headless -- --data-dir ./data --sentinel-eval
  cargo run -p n-headless -- --data-dir ./data --check-integrity
  cargo run -p n-headless -- --data-dir ./data --repair-integrity
把本地 C:\Users\...\AppData\Roaming\com.ntrend.app\ntrend.db 复制到 ./data/ntrend.db 即可
"#);
}
#[tokio::main]
async fn main() -> anyhow::Result<()> {
    if std::env::args().any(|a| a == "--help" || a == "-h") { print_help(); return Ok(()); }
    let data_dir = resolve_data_dir();
    std::fs::create_dir_all(&data_dir)?;
    let db_path = data_dir.join("ntrend.db");
    let config_path = data_dir.join("config.json");
    let peek_level = peek_log_level(&config_path).unwrap_or_else(|| "info".to_string());
    init_logging(&data_dir, &peek_level)?;
    tracing::info!("n-headless v{} 启动 | 数据目录: {}", env!("CARGO_PKG_VERSION"), data_dir.display());
    if !db_path.exists() { tracing::warn!("未找到 {}，将新建空库。请把本地 ntrend.db 复制到此路径", db_path.display()); }
    let db = storage::connect(&db_path).await?;

    if std::env::args().any(|a| a == "--report-finality") {
        let trials = n_core::storage::repo::load_all_finality_trials(&db).await?;
        let report = n_core::finality::summarize_trials(&trials);
        println!("\n{}", n_core::finality::format_finality_report(&report));
        return Ok(());
    }

    if std::env::args().any(|a| a == "--simulate-finality") {
        let obs = n_core::storage::repo::load_all_observations(&db).await?;
        let strats = n_core::finality::StrategyDef::default_strategies();
        let results = n_core::finality::simulate_strategies(&obs, &strats);
        println!("\n=== 多策略离线仿真对比结果 ===\n");
        println!("{}", n_core::finality::format_simulation_table(&results));
        return Ok(());
    }

    if std::env::args().any(|a| a == "--sentinel-eval") {
        let obs = n_core::storage::repo::load_all_observations(&db).await?;
        let trials = n_core::storage::repo::load_all_finality_trials(&db).await?;
        let default_sentinels: Vec<String> = n_core::finality::DEFAULT_SENTINELS.iter().map(|s| s.to_string()).collect();
        let res = n_core::finality::evaluate_sentinels(&obs, &trials, &default_sentinels);
        println!("\n=== 哨兵批次定盘安全性评估 ===");
        println!("哨兵集合: {:?}", res.sentinel_symbols);
        println!("总批次数: {} | 有效全哨兵批次: {}", res.total_batches, res.valid_batches);
        println!("存在其他品种晚修订的批次数: {} (批次误判率: {:.2}%)", res.batches_with_non_sentinel_late_revision, res.batch_false_final_rate * 100.0);
        println!("哨兵批次确认平均延迟: {:.1}s | P95延迟: {:.1}s\n", res.avg_sentinel_batch_delay_secs, res.p95_sentinel_batch_delay_secs);
        return Ok(());
    }

    if std::env::args().any(|a| a == "--check-integrity") {
        let symbols = n_core::storage::repo::list_symbols(&db, true).await?;
        println!("\n=== Raw 5m 数据完整性体检报告 (Issue 05) ===\n");
        let mut all_clean = true;
        for s in &symbols {
            let report = n_core::integrity::RawDataIntegrityChecker::inspect_symbol(&db, &s.code, 1000).await?;
            println!("{}", report.format_summary());
            for gap in &report.missing_gaps {
                println!(
                    "   ↳ 缺口: {} ~ {} (缺失 {} 根, {})",
                    gap.start_ts,
                    gap.end_ts,
                    gap.missing_count,
                    if gap.recoverable_by_api { "可接口自愈" } else { "超出接口窗口" }
                );
            }
            if !report.is_clean {
                all_clean = false;
            }
        }
        if all_clean {
            println!("\n🎉 全部 {} 个监控品种数据均 100% 完整无缺洞！\n", symbols.len());
        } else {
            println!("\n提示: 可使用 `--repair-integrity` 参数尝试自动补齐上述可自愈缺口。\n");
        }
        return Ok(());
    }

    if std::env::args().any(|a| a == "--repair-integrity") {
        let config = Config::load(&config_path, &db).await?;
        let services = Arc::new(Services::new(db.clone(), config, config_path.clone()).await?);
        println!("\n=== 启动 Raw 5m 数据缺洞自愈修复管道 (Issue 05) ===\n");
        let results = services.repair_all_symbols_integrity().await?;
        for r in &results {
            if r.initial_missing > 0 {
                println!("🛠️ [{}] 初始缺失: {} 根 | 修复: {} 根 | 剩余: {} 根 | 结果: {}", r.symbol, r.initial_missing, r.repaired_count, r.remaining_missing, r.message);
            }
        }
        println!("\n自愈修复执行完毕。\n");
        return Ok(());
    }

    let mut config = Config::load(&config_path, &db).await?;
    if config.email.smtp_password.is_empty() && config.email.from.is_empty() {
        for p in [data_dir.join("email.toml"), PathBuf::from("email.toml")] {
            if p.exists() { if let Ok(imported) = n_core::config::import_email_toml(&p) { if !imported.smtp_password.is_empty() || imported.from != n_core::notify::email::EmailSettings::default().from { config.email = imported; let _ = config.save(&config_path); tracing::info!("已从 {} 导入邮件配置", p.display()); break; } } }
        }
    }
    tracing::info!("配置 | 刷新 {}s 扫描 {}s 交易时段限制 {} 邮件 {}", config.scheduler.refresh_interval_secs, config.scheduler.scan_interval_secs, config.scheduler.trading_only, if config.email.enabled && config.email.sendable() { "已配置" } else { "未配置" });
    let services = Arc::new(Services::new(db, config.clone(), config_path.clone()).await?);
    services.spawn_finality_observer();
    let cnt = n_core::storage::repo::list_symbols(&services.db, false).await.map(|v| v.len()).unwrap_or(0);
    tracing::info!("品种数 {} | 邮件 {}", cnt, services.config().await.email.to);
    let svc = services.clone();
    tokio::spawn(async move {
        let mut ticker = tokio::time::interval(std::time::Duration::from_secs(15));
        ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        let mut startup_done = false;
        let mut last_refresh: Option<chrono::DateTime<Local>> = None;
        let mut last_scan: Option<chrono::DateTime<Local>> = None;
        loop {
            ticker.tick().await;
            let now = Local::now();
            let cfg = svc.config().await.scheduler.clone();
            if !startup_done { startup_done = true; if n_core::scheduler::is_trading_time(&now) { let _ = run_refresh(&svc).await; last_refresh = Some(now); continue; } }
            let action = n_core::scheduler::next_action(now, &cfg, last_refresh, last_scan);
            match action {
                n_core::scheduler::SchedulerAction::None => {},
                n_core::scheduler::SchedulerAction::Refresh => { if run_refresh(&svc).await.is_ok() { last_refresh = Some(now); } },
                n_core::scheduler::SchedulerAction::Scan => { let _ = run_refresh(&svc).await; last_refresh = Some(now); if run_scan(&svc).await.is_ok() { last_scan = Some(now); } },
                n_core::scheduler::SchedulerAction::RefreshAndScan => { if run_refresh(&svc).await.is_ok() { last_refresh = Some(now); } if run_scan(&svc).await.is_ok() { last_scan = Some(now); } },
            }
        }
    });
    tokio::signal::ctrl_c().await?;
    tracing::info!("收到退出信号");
    n_core::process::SidecarManager::stop();
    Ok(())
}
async fn run_refresh(svc: &Services) -> anyhow::Result<()> {
    let t0 = Instant::now();
    tracing::info!("刷新触发 | {}", Local::now().format("%H:%M:%S"));
    match svc.refresh_data().await { Ok(s) => { tracing::info!("刷新完成 {}ms | 成功 {} 失败 {}", t0.elapsed().as_millis(), s.succeeded, s.failures); Ok(()) }, Err(e) => { tracing::error!("刷新失败: {e}"); Err(e) } }
}
async fn run_scan(svc: &Services) -> anyhow::Result<()> {
    let t0 = Instant::now();
    tracing::info!("扫描触发 | {}", Local::now().format("%H:%M:%S"));
    let res = svc.run_scan().await?;
    tracing::info!("扫描完成 {}ms | 扫描 {} 活跃 {} 新增预警 {} 新触发 {}", t0.elapsed().as_millis(), res.scanned, res.active_count, res.new_warnings.len(), res.newly_triggered.len());
    let cfg = svc.config().await;
    let min_score = cfg.notify.new_pattern_min_score;
    if cfg.email.enabled && cfg.email.sendable() {
        let triggered_ids: std::collections::HashSet<i64> = res.newly_triggered.iter().map(|e| e.id).collect();
        let mut emails = Vec::new();
        for e in res.new_warnings.iter().filter(|e| e.entry_score >= min_score && !triggered_ids.contains(&e.id)) { emails.push((n_core::notify::email::EventEmailKind::Warning, e)); }
        for e in res.newly_triggered.iter().filter(|e| e.entry_score >= min_score) { emails.push((n_core::notify::email::EventEmailKind::Trigger, e)); }
        for (kind, e) in emails { let (subject, body) = n_core::notify::email::event_email_payload(kind, e); tracing::info!("[SEND_MAIL] subject='{}' to='{}' symbol='{}'", subject, cfg.email.to, e.symbol); if let Err(err) = n_core::notify::email::send_summary(&subject, &body, &cfg.email) { tracing::error!("邮件发送失败: {err}"); } else { tracing::info!("已发送邮件 {} {}", e.symbol, if kind == n_core::notify::email::EventEmailKind::Warning { "预警" } else { "触发" }); } }
        // 单K锤/针独立邮件（不受评分阈值限制，有就发，与 src-tauri/lib.rs 保持一致）
        for sb in &res.single_bars {
            let (subject, body) = n_core::notify::email::single_bar_email_payload(sb);
            if let Err(err) = n_core::notify::email::send_summary(&subject, &body, &cfg.email) {
                tracing::error!("单K邮件发送失败: {err}");
            } else {
                tracing::info!("[SEND_MAIL] 单K {} {} {}", sb.symbol, sb.kind, sb.trigger_bar_ts);
            }
        }
    } else if res.has_notifiable_signal(min_score) { tracing::warn!("有新信号但邮件未配置"); }
    Ok(())
}
fn init_logging(dir: &Path, level: &str) -> anyhow::Result<()> {
    std::fs::create_dir_all(dir)?;
    let file_appender = tracing_appender::rolling::daily(dir, "ntrend.log");
    let (file_writer, guard) = tracing_appender::non_blocking(file_appender);
    std::mem::forget(guard);
    let filter = log_filter(level);
    let console = tracing_subscriber::fmt::layer().with_timer(LocalTime).with_target(false).with_writer(std::io::stdout);
    let file = tracing_subscriber::fmt::layer().with_timer(LocalTime).with_target(false).with_ansi(false).with_writer(file_writer);
    tracing_subscriber::registry().with(filter).with(console).with(file).init();
    Ok(())
}
