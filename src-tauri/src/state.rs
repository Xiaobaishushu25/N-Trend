use n_core::service::Services;
use tokio::sync::RwLock;

pub struct AppState {
    pub services: Services,
    pub scheduler: RwLock<SchedulerState>,
}

pub struct SchedulerState {
    pub running: bool,
    pub last_refresh: Option<chrono::NaiveDateTime>,
    pub last_scan: Option<chrono::NaiveDateTime>,
}
