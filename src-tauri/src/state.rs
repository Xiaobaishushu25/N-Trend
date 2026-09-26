use std::path::{Path, PathBuf};
use std::sync::Arc;
use tokio::sync::RwLock;

use n_protocol::config::ClientLocalSettings;
use n_protocol::dto::{NewNotificationHistoryItem, NotificationHistoryItem};

use crate::client::{BackupScheduler, CredentialStore, KlineMemoryCache, RealtimeClient, RemoteApiClient};

pub const NOTIFICATION_HISTORY_LIMIT: usize = 40;

pub struct AppState {
    pub api: Arc<RemoteApiClient>,
    pub realtime: Arc<RealtimeClient>,
    pub kline_cache: Arc<KlineMemoryCache>,
    pub credentials: Arc<CredentialStore>,
    pub backup_scheduler: Arc<BackupScheduler>,
    pub local_settings: RwLock<ClientLocalSettings>,
    pub settings_path: PathBuf,
    pub notifications: std::sync::Mutex<Vec<NotificationHistoryItem>>,
    pub next_notification_id: std::sync::atomic::AtomicU64,
}

impl AppState {
    pub fn new(
        data_dir: &Path,
        api: Arc<RemoteApiClient>,
        realtime: Arc<RealtimeClient>,
        kline_cache: Arc<KlineMemoryCache>,
        credentials: Arc<CredentialStore>,
        backup_scheduler: Arc<BackupScheduler>,
    ) -> Self {
        let settings_path = data_dir.join("client_settings.json");
        let mut local_settings = ClientLocalSettings::default();

        if let Ok(content) = std::fs::read_to_string(&settings_path) {
            if let Ok(saved) = serde_json::from_str::<ClientLocalSettings>(&content) {
                local_settings = saved;
            }
        }

        Self {
            api,
            realtime,
            kline_cache,
            credentials,
            backup_scheduler,
            local_settings: RwLock::new(local_settings),
            settings_path,
            notifications: std::sync::Mutex::new(Vec::new()),
            next_notification_id: std::sync::atomic::AtomicU64::new(1),
        }
    }

    pub fn record_local_notification(&self, input: NewNotificationHistoryItem) -> NotificationHistoryItem {
        let id = self
            .next_notification_id
            .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let item = NotificationHistoryItem {
            id,
            created_at: chrono::Local::now().format("%Y-%m-%d %H:%M:%S").to_string(),
            kind: input.kind,
            title: input.title,
            content: input.content,
            signal: input.signal,
            entry_trigger: input.entry_trigger,
            single_bar: input.single_bar,
            manual_level: input.manual_level,
            read_at: None,
        };

        let mut list = self.notifications.lock().unwrap();
        list.insert(0, item.clone());
        if list.len() > NOTIFICATION_HISTORY_LIMIT {
            list.truncate(NOTIFICATION_HISTORY_LIMIT);
        }
        item
    }

    pub fn local_notifications(&self) -> Vec<NotificationHistoryItem> {
        self.notifications.lock().unwrap().clone()
    }

    pub async fn save_local_settings(&self) -> Result<(), String> {
        let settings = self.local_settings.read().await.clone();
        let json = serde_json::to_string_pretty(&settings).map_err(|e| e.to_string())?;
        std::fs::write(&self.settings_path, json).map_err(|e| e.to_string())?;
        Ok(())
    }
}
