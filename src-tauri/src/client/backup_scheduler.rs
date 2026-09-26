use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;
use chrono::{FixedOffset, Utc};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use tauri::{AppHandle, Emitter};
use tokio::sync::RwLock;

use super::remote_api::RemoteApiClient;

const BACKUP_RETENTION_COUNT: usize = 3;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BackupStatus {
    pub last_backup_date: Option<String>,
    pub backup_count: usize,
    pub backup_dir: String,
}

pub struct BackupScheduler {
    app: AppHandle,
    api: Arc<RemoteApiClient>,
    backup_dir: PathBuf,
    last_backup_date: RwLock<Option<String>>,
}

impl BackupScheduler {
    pub fn new(app: AppHandle, api: Arc<RemoteApiClient>, data_dir: &Path) -> Arc<Self> {
        let backup_dir = data_dir.join("backups");
        let _ = std::fs::create_dir_all(&backup_dir);

        let scheduler = Arc::new(Self {
            app,
            api,
            backup_dir,
            last_backup_date: RwLock::new(None),
        });

        // Initialize last_backup_date by inspecting existing backup files
        scheduler.refresh_last_backup_date();

        // Spawn background check loop
        let bg = scheduler.clone();
        tauri::async_runtime::spawn(async move {
            bg.run_loop().await;
        });

        scheduler
    }

    pub fn backup_dir(&self) -> PathBuf {
        self.backup_dir.clone()
    }

    pub async fn get_status(&self) -> BackupStatus {
        let last_date = self.last_backup_date.read().await.clone();
        let count = self.list_backup_files().len();
        BackupStatus {
            last_backup_date: last_date,
            backup_count: count,
            backup_dir: self.backup_dir.to_string_lossy().to_string(),
        }
    }

    fn shanghai_today() -> String {
        // Asia/Shanghai is UTC+8
        let offset = FixedOffset::east_opt(8 * 3600).unwrap();
        Utc::now().with_timezone(&offset).format("%Y-%m-%d").to_string()
    }

    fn refresh_last_backup_date(&self) {
        let files = self.list_backup_files();
        if let Some(latest) = files.last() {
            // filename: ntrend-YYYY-MM-DD-HHmmss.sqlite.gz
            if let Some(name) = latest.file_name().and_then(|n| n.to_str()) {
                if name.starts_with("ntrend-") && name.len() >= 17 {
                    let date_str = &name[7..17]; // YYYY-MM-DD
                    if let Ok(mut lock) = self.last_backup_date.try_write() {
                        *lock = Some(date_str.to_string());
                    }
                }
            }
        }
    }

    fn list_backup_files(&self) -> Vec<PathBuf> {
        let mut list = Vec::new();
        if let Ok(entries) = std::fs::read_dir(&self.backup_dir) {
            for entry in entries.flatten() {
                let path = entry.path();
                if let Some(name) = path.file_name().and_then(|n| n.to_str()) {
                    if name.starts_with("ntrend-") && name.ends_with(".sqlite.gz") {
                        list.push(path);
                    }
                }
            }
        }
        list.sort();
        list
    }

    pub async fn trigger_backup_now(&self) -> Result<String, String> {
        tracing::info!("📦 开始执行一致性数据库备份...");
        let (bytes, expected_sha) = self.api.download_database_backup().await.map_err(|e| e.message)?;

        // Verify SHA-256
        let mut hasher = Sha256::new();
        hasher.update(&bytes);
        let actual_sha = format!("{:x}", hasher.finalize());

        if !expected_sha.is_empty() && actual_sha != expected_sha {
            return Err(format!(
                "备份 SHA-256 校验失败! 预期: {expected_sha}, 实际: {actual_sha}"
            ));
        }

        let now_str = Utc::now()
            .with_timezone(&FixedOffset::east_opt(8 * 3600).unwrap())
            .format("%Y-%m-%d-%H%M%S")
            .to_string();

        let filename = format!("ntrend-{now_str}.sqlite.gz");
        let part_path = self.backup_dir.join(format!("{filename}.part"));
        let target_path = self.backup_dir.join(&filename);
        let sha_path = self.backup_dir.join(format!("{filename}.sha256"));

        std::fs::write(&part_path, &bytes).map_err(|e| format!("写入临时备份文件失败: {e}"))?;
        std::fs::rename(&part_path, &target_path).map_err(|e| format!("保存备份文件失败: {e}"))?;
        std::fs::write(&sha_path, format!("{actual_sha}  {filename}\n")).map_err(|e| format!("写入校验文件失败: {e}"))?;

        let today = Self::shanghai_today();
        *self.last_backup_date.write().await = Some(today);

        // Prune older backups
        self.prune_old_backups();

        tracing::info!("✅ 备份成功已保存: {} (SHA256: {})", target_path.display(), actual_sha);
        let _ = self.app.emit("backup-completed", &filename);
        Ok(filename)
    }

    fn prune_old_backups(&self) {
        let files = self.list_backup_files();
        if files.len() > BACKUP_RETENTION_COUNT {
            let to_remove = files.len() - BACKUP_RETENTION_COUNT;
            for file in &files[0..to_remove] {
                let _ = std::fs::remove_file(file);
                let sha_file = file.with_extension("").with_extension("sqlite.gz.sha256");
                let _ = std::fs::remove_file(sha_file);
                // Also try filename + ".sha256"
                if let Some(name) = file.file_name().and_then(|n| n.to_str()) {
                    let s_path = self.backup_dir.join(format!("{name}.sha256"));
                    let _ = std::fs::remove_file(s_path);
                }
                tracing::info!("🗑️ 清理旧备份文件: {}", file.display());
            }
        }
    }

    async fn run_loop(&self) {
        // Initial wait 15 seconds after startup before checking
        tokio::time::sleep(Duration::from_secs(15)).await;

        let retry_delays = [60, 300, 1800, 21600]; // 1m, 5m, 30m, 6h
        let mut retry_idx = 0;

        loop {
            let today = Self::shanghai_today();
            let need_backup = {
                let last = self.last_backup_date.read().await;
                last.as_deref() != Some(&today)
            };

            if need_backup {
                match self.trigger_backup_now().await {
                    Ok(_) => {
                        retry_idx = 0;
                        tokio::time::sleep(Duration::from_secs(6 * 3600)).await;
                    }
                    Err(e) => {
                        tracing::warn!("⚠️ 自动数据库备份未完成: {e}");
                        let delay = retry_delays[retry_idx];
                        if retry_idx + 1 < retry_delays.len() {
                            retry_idx += 1;
                        }
                        tokio::time::sleep(Duration::from_secs(delay)).await;
                    }
                }
            } else {
                tokio::time::sleep(Duration::from_secs(3600)).await;
            }
        }
    }
}
