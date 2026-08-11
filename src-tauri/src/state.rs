use n_core::service::Services;
use serde::{Deserialize, Serialize};
use tokio::sync::RwLock;

/// 设置表里保存“最近一次成功获取K线数据时间”的键
pub const KEY_LAST_REFRESH: &str = "scheduler_last_refresh";
/// 设置表里保存“最近一次成功扫描形态时间”的键
pub const KEY_LAST_SCAN: &str = "scheduler_last_scan";
/// 内存通知历史最多保留条数，软件退出即清空，不落盘
pub const NOTIFICATION_HISTORY_LIMIT: usize = 40;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct NotificationSignal {
    pub code: String,
    pub name: String,
    pub direction: String,
    pub level: String,
    pub grade: String,
    pub score: f64,
    pub entry: f64,
    pub stop: f64,
    pub target: f64,
    pub time: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct NotificationEntryTrigger {
    pub symbol: String,
    pub name: String,
    pub direction: String,
    pub entry: f64,
    pub latest: f64,
}

#[derive(Debug, Clone, Serialize)]
pub struct NotificationHistoryItem {
    pub id: u64,
    pub created_at: String,
    pub kind: String,
    pub title: Option<String>,
    pub content: String,
    pub signal: Option<NotificationSignal>,
    pub entry_trigger: Option<NotificationEntryTrigger>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct NewNotificationHistoryItem {
    pub kind: String,
    pub title: Option<String>,
    pub content: String,
    pub signal: Option<NotificationSignal>,
    pub entry_trigger: Option<NotificationEntryTrigger>,
}

pub struct AppState {
    pub services: Services,
    pub scheduler: RwLock<SchedulerState>,
    pub notification_history: std::sync::Mutex<Vec<NotificationHistoryItem>>,
    pub next_notification_id: std::sync::atomic::AtomicU64,
}

pub struct SchedulerState {
    pub running: bool,
    /// 最近一次成功获取K线数据的时间（展示用，重启后恢复）
    pub last_refresh: Option<chrono::NaiveDateTime>,
    /// 最近一次成功扫描形态的时间（展示用，重启后恢复）
    pub last_scan: Option<chrono::NaiveDateTime>,
    /// 上次刷新尝试的开始时间（调度用：下次刷新 = 起点 + 间隔，拉取耗时不再推迟下一次）
    pub refresh_anchor: Option<chrono::NaiveDateTime>,
    /// 上次扫描尝试的开始时间（调度用）
    pub scan_anchor: Option<chrono::NaiveDateTime>,
}

impl AppState {
    /// 把一条通知记入内存历史（最新在前，最多保留 40 条）。
    pub fn record_notification(&self, input: NewNotificationHistoryItem) {
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
        };
        let mut list = self.notification_history.lock().expect("通知历史锁可用");
        list.insert(0, item);
        list.truncate(NOTIFICATION_HISTORY_LIMIT);
    }

    pub fn notification_history(&self) -> Vec<NotificationHistoryItem> {
        let mut list = self
            .notification_history
            .lock()
            .expect("通知历史锁可用")
            .clone();
        list.sort_by(|a, b| b.id.cmp(&a.id));
        list
    }

    /// 记录一次成功的数据刷新（内存 + 落库，应用重启后仍可恢复）。
    pub async fn note_refresh_success(&self) {
        self.note_success(KEY_LAST_REFRESH, true).await;
    }

    /// 记录一次成功的形态扫描（内存 + 落库，应用重启后仍可恢复）。
    pub async fn note_scan_success(&self) {
        self.note_success(KEY_LAST_SCAN, false).await;
    }

    async fn note_success(&self, key: &str, is_refresh: bool) {
        let now = chrono::Local::now().naive_local();
        {
            let mut st = self.scheduler.write().await;
            if is_refresh {
                st.last_refresh = Some(now);
            } else {
                st.last_scan = Some(now);
            }
        }
        let mut map = std::collections::HashMap::new();
        map.insert(key.to_string(), now.format("%Y-%m-%d %H:%M:%S").to_string());
        if let Err(e) = n_core::storage::repo::set_settings(&self.services.db, &map).await {
            tracing::error!("保存最近更新时间失败: {e}");
        }
    }
}
