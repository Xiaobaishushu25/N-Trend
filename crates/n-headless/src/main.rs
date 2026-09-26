mod api;
mod auth;
mod lock;
mod realtime;
mod runtime;
mod secrets;
mod state;

use chrono::Local;
use lock::ProcessLock;
use n_core::config::Config;
use n_core::service::Services;
use n_core::storage;
use secrets::ServerSecrets;
use state::ServerContext;
use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::sync::Arc;
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
        return tracing_subscriber::EnvFilter::try_from_default_env()
            .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new(level));
    }
    let base = format!(
        "{level},sqlx=warn,sea-orm=warn,sea_orm=warn,hyper=warn,reqwest=warn,rustls=warn,h2=warn,tower_http=warn"
    );
    tracing_subscriber::EnvFilter::try_new(&base)
        .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info,sqlx=warn,sea-orm=warn"))
}

fn peek_log_level(path: &Path) -> Option<String> {
    let text = std::fs::read_to_string(path).ok()?;
    let v: serde_json::Value = serde_json::from_str(&text).ok()?;
    v.get("log")?.get("level")?.as_str().map(|s| s.to_string())
}

fn resolve_data_dir() -> PathBuf {
    let mut args = std::env::args().skip(1);
    while let Some(a) = args.next() {
        if a == "--data-dir" {
            if let Some(v) = args.next() {
                return PathBuf::from(v);
            }
        }
        if a.starts_with("--data-dir=") {
            return PathBuf::from(a.trim_start_matches("--data-dir="));
        }
    }
    if let Ok(v) = std::env::var("NTREND_DATA_DIR").or_else(|_| std::env::var("DATA_DIR")) {
        if !v.trim().is_empty() {
            return PathBuf::from(v);
        }
    }
    PathBuf::from("./data")
}

fn resolve_listen_addr() -> SocketAddr {
    let mut args = std::env::args().skip(1);
    let mut host = "0.0.0.0".to_string();
    let mut port = 8081u16;

    while let Some(a) = args.next() {
        if a == "--port" {
            if let Some(v) = args.next() {
                if let Ok(p) = v.parse::<u16>() {
                    port = p;
                }
            }
        } else if a == "--host" {
            if let Some(v) = args.next() {
                host = v;
            }
        }
    }

    if let Ok(v) = std::env::var("NTREND_LISTEN_ADDR").or_else(|_| std::env::var("BIND_ADDR")) {
        if let Ok(addr) = v.parse::<SocketAddr>() {
            return addr;
        }
    }

    format!("{host}:{port}").parse().unwrap_or_else(|_| "0.0.0.0:8081".parse().unwrap())
}

fn print_help() {
    println!(
        r#"ntrend-server (n-headless) - ntrend 权威服务端
用法:
  cargo run -p n-headless -- --data-dir ./data
  cargo run -p n-headless -- --data-dir /opt/ntrend/data --host 0.0.0.0 --port 8081
  cargo run -p n-headless -- --data-dir ./data --check-integrity
  cargo run -p n-headless -- --data-dir ./data --repair-integrity
"#
    );
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    if std::env::args().any(|a| a == "--help" || a == "-h") {
        print_help();
        return Ok(());
    }

    let data_dir = resolve_data_dir();
    std::fs::create_dir_all(&data_dir)?;
    let db_path = data_dir.join("ntrend.db");
    let config_path = data_dir.join("config.json");
    let peek_level = peek_log_level(&config_path).unwrap_or_else(|| "info".to_string());
    init_logging(&data_dir, &peek_level)?;

    // 获取单实例排他进程锁，避免两个进程同时读写 SQLite WAL
    let _process_lock = ProcessLock::acquire(&data_dir)?;

    tracing::info!(
        "🚀 ntrend-server v{} 启动 | 数据目录: {}",
        env!("CARGO_PKG_VERSION"),
        data_dir.display()
    );

    let db = storage::connect(&db_path).await?;

    // 命令行单次维护动作（兼容原有 CLI）
    if std::env::args().any(|a| a == "--check-integrity") {
        let symbols = n_core::storage::repo::list_symbols(&db, true).await?;
        println!("\n=== Raw 5m 数据完整性体检报告 ===\n");
        for s in &symbols {
            let report = n_core::integrity::RawDataIntegrityChecker::inspect_symbol(&db, &s.code, 1000).await?;
            println!("{}", report.format_summary());
        }
        return Ok(());
    }

    if std::env::args().any(|a| a == "--repair-integrity") {
        let config = Config::load(&config_path, &db).await?;
        let services = Arc::new(Services::new(db.clone(), config, config_path.clone()).await?);
        println!("\n=== 启动 Raw 5m 数据缺洞自愈修复管道 ===\n");
        let results = services.repair_all_symbols_integrity().await?;
        for r in &results {
            if r.initial_missing > 0 {
                println!(
                    "🛠️ [{}] 初始缺失: {} 根 | 修复: {} 根 | 剩余: {} 根 | 结果: {}",
                    r.symbol, r.initial_missing, r.repaired_count, r.remaining_missing, r.message
                );
            }
        }
        return Ok(());
    }

    let mut config = Config::load(&config_path, &db).await?;
    let mut secrets = ServerSecrets::load_or_create(&data_dir)?;

    // 自动兼容与同步密码（若 config 中有密码而 secrets 中为空，迁移至 secrets）
    let mut secrets_need_save = false;
    if !config.data_source.tq_password.is_empty() && secrets.tq_password.is_empty() {
        secrets.tq_password = config.data_source.tq_password.clone();
        secrets_need_save = true;
    }
    if !config.email.smtp_password.is_empty() && secrets.smtp_password.is_empty() {
        secrets.smtp_password = config.email.smtp_password.clone();
        secrets_need_save = true;
    }
    if secrets_need_save {
        let _ = secrets.save(&data_dir);
    }

    // 确保运行时 config 具备 secrets 中的真实密码
    if config.data_source.tq_password.is_empty() && !secrets.tq_password.is_empty() {
        config.data_source.tq_password = secrets.tq_password.clone();
    }
    if config.email.smtp_password.is_empty() && !secrets.smtp_password.is_empty() {
        config.email.smtp_password = secrets.smtp_password.clone();
    }

    let services = Arc::new(Services::new(db.clone(), config.clone(), config_path.clone()).await?);

    // 启动天勤 Python Sidecar（若已配置自动拉起）
    if config.data_source.auto_spawn_bridge {
        let _ = n_core::process::SidecarManager::start(&config.data_source).await;
    }

    // 初始化全局上下文
    let ctx = ServerContext::new(db, services, data_dir, secrets);

    // 启动后台运行时任务（调度器、实时现价轮询、天勤闭合事件消费）
    runtime::spawn_runtime_tasks(ctx.clone());

    // 构建 Axum REST API 与 WebSocket 路由
    let router = api::build_router(ctx.clone());
    let listen_addr = resolve_listen_addr();
    let listener = tokio::net::TcpListener::bind(listen_addr).await?;

    tracing::info!(
        "🌐 ntrend-server 服务就绪，监听地址: http://{}",
        listen_addr
    );

    axum::serve(listener, router)
        .with_graceful_shutdown(async move {
            let _ = tokio::signal::ctrl_c().await;
            tracing::info!("🛑 收到终止信号，正在优雅关闭服务...");
        })
        .await?;

    n_core::process::SidecarManager::stop();
    tracing::info!("👋 ntrend-server 已完全退出");
    Ok(())
}

fn init_logging(dir: &Path, level: &str) -> anyhow::Result<()> {
    std::fs::create_dir_all(dir)?;
    let file_appender = tracing_appender::rolling::daily(dir, "ntrend.log");
    let (file_writer, guard) = tracing_appender::non_blocking(file_appender);
    std::mem::forget(guard);
    let filter = log_filter(level);
    let console = tracing_subscriber::fmt::layer()
        .with_timer(LocalTime)
        .with_target(false)
        .with_writer(std::io::stdout);
    let file = tracing_subscriber::fmt::layer()
        .with_timer(LocalTime)
        .with_target(false)
        .with_ansi(false)
        .with_writer(file_writer);
    tracing_subscriber::registry()
        .with(filter)
        .with(console)
        .with(file)
        .init();
    Ok(())
}
