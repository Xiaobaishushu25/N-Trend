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
    pub instances: tokio::sync::OnceCell<Arc<n_core::trader::instances::InstanceManager>>,
    pub ctp_sidecars: Mutex<std::collections::HashMap<String, crate::ctp_sidecar::CtpSidecar>>,
    pub ctp_login_control: Mutex<()>,
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
        let instance_dir =
            n_core::trader::instances::migrate_default(&data_dir).unwrap_or_else(|e| {
                tracing::error!("交易状态迁移失败: {e:#}");
                data_dir.join("trader_instances/default")
            });
        let config_path = instance_dir.join("trader_config.json");
        let mut config_error = None;
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
                            config_error = Some(format!("解析交易配置失败，禁止交易: {e}"));
                            n_core::trader::ExecutionConfig::default()
                        }
                    }
                }
                Err(e) => {
                    config_error = Some(format!("读取交易配置失败，禁止交易: {e}"));
                    n_core::trader::ExecutionConfig::default()
                }
            }
        } else {
            n_core::trader::ExecutionConfig::default()
        };

        let mut engine = n_core::trader::ExecutionEngine::with_config_path(
            trader_client,
            initial_config,
            Some(config_path),
        );
        if let Some(error) = config_error {
            engine.disable_loading(error);
        }
        let execution_engine = Arc::new(engine);
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
            instances: tokio::sync::OnceCell::new(),
            ctp_sidecars: Mutex::new(std::collections::HashMap::new()),
            ctp_login_control: Mutex::new(()),
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
