use crate::realtime::RealtimeHub;
use crate::secrets::ServerSecrets;
use n_core::service::Services;
use n_core::sea_orm::DatabaseConnection;
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
}

impl ServerContext {
    pub fn new(
        db: DatabaseConnection,
        services: Arc<Services>,
        data_dir: PathBuf,
        secrets: ServerSecrets,
    ) -> Arc<Self> {
        let realtime_hub = Arc::new(RealtimeHub::new());
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
        })
    }

    pub fn next_event_seq(&self) -> u64 {
        self.event_seq.fetch_add(1, Ordering::SeqCst)
    }

    pub fn uptime_secs(&self) -> u64 {
        self.start_time.elapsed().as_secs()
    }
}
