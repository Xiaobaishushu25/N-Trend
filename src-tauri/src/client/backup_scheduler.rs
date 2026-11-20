use chrono::{FixedOffset, Utc};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;
use tauri::{AppHandle, Emitter};
use tokio::io::AsyncWriteExt;
use tokio::sync::{Mutex, RwLock};

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
    is_running: Mutex<()>,
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
            is_running: Mutex::new(()),
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
        Utc::now()
            .with_timezone(&offset)
            .format("%Y-%m-%d")
            .to_string()
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
        self.run_backup(false).await
    }

    async fn run_backup(&self, automatic: bool) -> Result<String, String> {
        let _guard = match self.is_running.try_lock() {
            Ok(g) => g,
            Err(_) => {
                tracing::warn!("⚠️ 数据库备份任务已在执行中，忽略重复触发");
                return Err("数据库备份任务已在执行中，请勿重复触发".to_string());
            }
        };

        tracing::info!("📦 开始执行一致性数据库备份...");
        match self.do_backup_now(automatic).await {
            Ok(filename) => {
                tracing::info!("✅ 备份成功已保存: {}", filename);
                let _ = self.app.emit("backup-completed", &filename);
                Ok(filename)
            }
            Err(e) => {
                tracing::error!("❌ 数据库备份失败: {e}");
                Err(e)
            }
        }
    }

    async fn do_backup_now(&self, automatic: bool) -> Result<String, String> {
        let (response, expected_sha) = self
            .api
            .download_database_backup(automatic)
            .await
            .map_err(|e| e.message)?;

        let now_str = Utc::now()
            .with_timezone(&FixedOffset::east_opt(8 * 3600).unwrap())
            .format("%Y-%m-%d-%H%M%S")
            .to_string();

        let filename = format!("ntrend-{now_str}.sqlite.gz");
        let part_path = self.backup_dir.join(format!("{filename}.part"));
        let target_path = self.backup_dir.join(&filename);
        let sha_path = self.backup_dir.join(format!("{filename}.sha256"));

        if let Err(error) = write_backup(response, &expected_sha, &part_path).await {
            let _ = tokio::fs::remove_file(&part_path).await;
            return Err(error);
        }
        if let Err(error) =
            tokio::fs::write(&sha_path, format!("{expected_sha}  {filename}\n")).await
        {
            let _ = tokio::fs::remove_file(&part_path).await;
            return Err(format!("写入校验文件失败: {error}"));
        }
        if let Err(error) = tokio::fs::rename(&part_path, &target_path).await {
            let _ = tokio::fs::remove_file(&part_path).await;
            let _ = tokio::fs::remove_file(&sha_path).await;
            return Err(format!("保存备份文件失败: {error}"));
        }

        let today = Self::shanghai_today();
        *self.last_backup_date.write().await = Some(today);

        // Prune older backups
        self.prune_old_backups();

        tracing::info!(
            "📁 备份文件已持久化: {} (SHA256: {})",
            target_path.display(),
            expected_sha
        );
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

        let retry_delays = [60, 300, 900];
        let mut retry_idx = 0;

        loop {
            let today = Self::shanghai_today();
            let need_backup = {
                let last = self.last_backup_date.read().await;
                last.as_deref() != Some(&today)
            };

            if need_backup && n_protocol::automatic_backup_window(Utc::now()) {
                match self.run_backup(true).await {
                    Ok(_) => {
                        retry_idx = 0;
                        tokio::time::sleep(Duration::from_secs(60)).await;
                    }
                    Err(e) => {
                        let delay = retry_delays[retry_idx];
                        tracing::warn!("⚠️ 自动数据库备份未完成 ({e})，将在 {delay} 秒后重试");
                        if retry_idx + 1 < retry_delays.len() {
                            retry_idx += 1;
                        }
                        tokio::time::sleep(Duration::from_secs(delay)).await;
                    }
                }
            } else {
                retry_idx = 0;
                tokio::time::sleep(Duration::from_secs(60)).await;
            }
        }
    }
}

async fn write_backup(
    mut response: reqwest::Response,
    expected_sha: &str,
    path: &Path,
) -> Result<(), String> {
    let mut file = tokio::fs::File::create(path)
        .await
        .map_err(|e| format!("创建临时备份失败: {e}"))?;
    let mut hasher = Sha256::new();
    while let Some(chunk) = response
        .chunk()
        .await
        .map_err(|e| format!("读取备份流失败: {e}"))?
    {
        hasher.update(&chunk);
        file.write_all(&chunk)
            .await
            .map_err(|e| format!("写入临时备份失败: {e}"))?;
    }
    let actual_sha = format!("{:x}", hasher.finalize());
    if !actual_sha.eq_ignore_ascii_case(expected_sha) {
        return Err(format!(
            "备份 SHA-256 校验失败，预期 {expected_sha}，实际 {actual_sha}"
        ));
    }
    file.sync_all()
        .await
        .map_err(|e| format!("同步备份文件失败: {e}"))
}
