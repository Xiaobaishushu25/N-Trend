use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Instant;
use chrono::Local;
use n_core::config::Config;
use n_core::service::Services;
use n_core::storage;
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
    let mut config = Config::load(&config_path, &db).await?;
    if config.email.smtp_password.is_empty() && config.email.from.is_empty() {
        for p in [data_dir.join("email.toml"), PathBuf::from("email.toml")] {
            if p.exists() { if let Ok(imported) = n_core::config::import_email_toml(&p) { if !imported.smtp_password.is_empty() || imported.from != n_core::notify::email::EmailSettings::default().from { config.email = imported; let _ = config.save(&config_path); tracing::info!("已从 {} 导入邮件配置", p.display()); break; } } }
        }
    }
    tracing::info!("配置 | 刷新 {}s 扫描 {}s 交易时段限制 {} 邮件 {}", config.scheduler.refresh_interval_secs, config.scheduler.scan_interval_secs, config.scheduler.trading_only, if config.email.enabled && config.email.sendable() { "已配置" } else { "未配置" });
    let services = Arc::new(Services::new(db, config.clone(), config_path.clone()).await?);
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
    Ok(())
}
async fn run_refresh(svc: &Services) -> anyhow::Result<()> {
    let t0 = Instant::now();
    tracing::info!("刷新触发 | {}", Local::now().format("%H:%M:%S"));
    match svc.run_refresh().await { Ok(s) => { tracing::info!("刷新完成 {}ms | 成功 {} 失败 {}", t0.elapsed().as_millis(), s.succeeded, s.failures); Ok(()) }, Err(e) => { tracing::error!("刷新失败: {e}"); Err(e) } }
}
async fn run_scan(svc: &Services) -> anyhow::Result<()> {
    let t0 = Instant::now();
    tracing::info!("扫描触发 | {}", Local::now().format("%H:%M:%S"));
    let res = svc.run_scan().await?;
    tracing::info!("扫描完成 {}ms | 扫描 {} 活跃 {} 新增预警 {} 新触发 {}", t0.elapsed().as_millis(), res.scanned, res.active_count, res.new_warnings.len(), res.newly_triggered.len());
    let cfg = svc.config().await;
    let min_score = cfg.notify.new_pattern_min_score;
    if cfg.email.enabled && cfg.email.sendable() {
        let mut emails = Vec::new();
        for e in res.new_warnings.iter().filter(|e| e.entry_score >= min_score) { emails.push((n_core::notify::email::EventEmailKind::Warning, e)); }
        for e in res.newly_triggered.iter().filter(|e| e.entry_score >= min_score) { emails.push((n_core::notify::email::EventEmailKind::Trigger, e)); }
        for (kind, e) in emails { let (subject, body) = n_core::notify::email::event_email_payload(kind, e); if let Err(err) = n_core::notify::email::send_summary(&subject, &body, &cfg.email) { tracing::error!("邮件发送失败: {err}"); } else { tracing::info!("已发送邮件 {} {}", e.symbol, if kind == n_core::notify::email::EventEmailKind::Warning { "预警" } else { "触发" }); } }
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
