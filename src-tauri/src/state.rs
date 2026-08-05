use n_core::service::Services;
use tokio::sync::RwLock;

/// 设置表里保存“最近一次成功获取K线数据时间”的键
pub const KEY_LAST_REFRESH: &str = "scheduler_last_refresh";
/// 设置表里保存“最近一次成功扫描形态时间”的键
pub const KEY_LAST_SCAN: &str = "scheduler_last_scan";

pub struct AppState {
    pub services: Services,
    pub scheduler: RwLock<SchedulerState>,
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
