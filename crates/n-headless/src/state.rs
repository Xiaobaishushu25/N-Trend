use crate::realtime::RealtimeHub;
use crate::secrets::ServerSecrets;
use n_core::sea_orm::DatabaseConnection;
use n_core::service::Services;
use std::collections::HashSet;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Instant;
use tokio::sync::{Mutex, RwLock};

pub struct ServerContext {
    pub db: DatabaseConnection,
    pub services: Arc<Services>,
    pub data_dir: PathBuf,
    pub secrets: Arc<RwLock<ServerSecrets>>,
    pub realtime_hub: Arc<RealtimeHub>,
    pub event_seq: AtomicU64,
    pub start_time: Instant,
    pub is_shutting_down: AtomicBool,
    #[allow(dead_code)]
    pub trigger_email_scheduled: Mutex<HashSet<i64>>,
    pub last_refresh: RwLock<Option<chrono::NaiveDateTime>>,
    pub last_scan: RwLock<Option<chrono::NaiveDateTime>>,
    pub scheduler_running: AtomicBool,
    pub execution_engine: Arc<n_core::trader::ExecutionEngine>,
    pub trader_snapshot: RwLock<serde_json::Value>,
}

impl ServerContext {
    pub fn new(
        db: DatabaseConnection,
        services: Arc<Services>,
        data_dir: PathBuf,
        secrets: ServerSecrets,
    ) -> Arc<Self> {
        let realtime_hub = Arc::new(RealtimeHub::new());
        let trader_client = n_core::trader::CtpTraderClient::for_port(8766);
        let config_path = data_dir.join("trader_config.json");
        let initial_config = if config_path.exists() {
            match std::fs::read_to_string(&config_path) {
                Ok(content) => {
                    match serde_json::from_str::<n_core::trader::ExecutionConfig>(&content) {
                        Ok(cfg) => {
                            tracing::info!(
                                "💾 成功从磁盘加载交易执行配置: {}",
                                config_path.display()
                            );
                            cfg
                        }
                        Err(e) => {
                            tracing::warn!("⚠️ 解析交易配置失败: {}, 将采用默认配置", e);
                            n_core::trader::ExecutionConfig::default()
                        }
                    }
                }
                Err(e) => {
                    tracing::warn!("⚠️ 读取交易配置失败: {}, 将采用默认配置", e);
                    n_core::trader::ExecutionConfig::default()
                }
            }
        } else {
            n_core::trader::ExecutionConfig::default()
        };

        let execution_engine = Arc::new(n_core::trader::ExecutionEngine::with_config_path(
            trader_client,
            initial_config,
            Some(config_path),
        ));
        Arc::new(Self {
            db,
            services,
            data_dir,
            secrets: Arc::new(RwLock::new(secrets)),
            realtime_hub,
            event_seq: AtomicU64::new(1),
            start_time: Instant::now(),
            is_shutting_down: AtomicBool::new(false),
            trigger_email_scheduled: Mutex::new(HashSet::new()),
            last_refresh: RwLock::new(None),
            last_scan: RwLock::new(None),
            scheduler_running: AtomicBool::new(true),
            execution_engine,
            trader_snapshot: RwLock::new(serde_json::Value::Null),
        })
    }

    pub fn next_event_seq(&self) -> u64 {
        self.event_seq.fetch_add(1, Ordering::SeqCst)
    }

    pub fn uptime_secs(&self) -> u64 {
        self.start_time.elapsed().as_secs()
    }
}
