use crate::auth::{AuthDevice, RequireAdmin};
use crate::state::ServerContext;
use axum::body::Body;
use axum::extract::{Query, State};
use axum::http::header::{CONTENT_DISPOSITION, CONTENT_LENGTH, CONTENT_TYPE};
use axum::http::{HeaderMap, HeaderValue, StatusCode};
use axum::response::IntoResponse;
use axum::Json;
use n_protocol::dto::SchedulerStatus;
use n_protocol::error::ApiErrorResponse;
use serde::Deserialize;
use std::sync::Arc;
use tokio::io::AsyncReadExt;

#[derive(Deserialize)]
pub struct SetRunningPayload {
    pub running: bool,
}

pub async fn get_scheduler_status(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
) -> Result<Json<SchedulerStatus>, (StatusCode, Json<ApiErrorResponse>)> {
    let last_refresh = ctx
        .last_refresh
        .read()
        .await
        .map(|t| t.format("%Y-%m-%d %H:%M:%S").to_string());
    let last_scan = ctx
        .last_scan
        .read()
        .await
        .map(|t| t.format("%Y-%m-%d %H:%M:%S").to_string());
    let running = ctx
        .scheduler_running
        .load(std::sync::atomic::Ordering::Relaxed);
    let active_data_source = if ctx.services.data_source.tq_is_available() {
        "天勤".to_string()
    } else {
        "新浪备用".to_string()
    };
    Ok(Json(SchedulerStatus {
        running,
        last_refresh,
        last_scan,
        active_data_source,
    }))
}

pub async fn set_scheduler_running(
    State(ctx): State<Arc<ServerContext>>,
    RequireAdmin(_): RequireAdmin,
    Json(payload): Json<SetRunningPayload>,
) -> Result<Json<SchedulerStatus>, (StatusCode, Json<ApiErrorResponse>)> {
    ctx.scheduler_running
        .store(payload.running, std::sync::atomic::Ordering::SeqCst);
    let last_refresh = ctx
        .last_refresh
        .read()
        .await
        .map(|t| t.format("%Y-%m-%d %H:%M:%S").to_string());
    let last_scan = ctx
        .last_scan
        .read()
        .await
        .map(|t| t.format("%Y-%m-%d %H:%M:%S").to_string());
    let active_data_source = if ctx.services.data_source.tq_is_available() {
        "天勤".to_string()
    } else {
        "新浪备用".to_string()
    };
    Ok(Json(SchedulerStatus {
        running: payload.running,
        last_refresh,
        last_scan,
        active_data_source,
    }))
}

pub async fn restart_bridge(
    State(ctx): State<Arc<ServerContext>>,
    RequireAdmin(_): RequireAdmin,
) -> Result<Json<serde_json::Value>, (StatusCode, Json<ApiErrorResponse>)> {
    tracing::info!("🔄 接收到 PC 主管理员请求：正在重启天勤 Python Bridge...");
    n_core::process::SidecarManager::stop();
    tokio::time::sleep(std::time::Duration::from_millis(500)).await;

    let cfg = ctx.services.config().await;
    if cfg.data_source.auto_spawn_bridge {
        let _ = n_core::process::SidecarManager::start(&cfg.data_source).await;
    }

    // 等待 2 秒后探活
    tokio::time::sleep(std::time::Duration::from_secs(2)).await;
    let healthy = ctx.services.data_source.tq_is_available();

    Ok(Json(serde_json::json!({
        "restarted": true,
        "healthy": healthy,
    })))
}

pub async fn restart_runtime(
    State(ctx): State<Arc<ServerContext>>,
    RequireAdmin(_): RequireAdmin,
) -> Result<Json<serde_json::Value>, (StatusCode, Json<ApiErrorResponse>)> {
    tracing::warn!("⚠️ 接收到 PC 主管理员安全重启服务指令，进入优雅停机...");
    ctx.is_shutting_down
        .store(true, std::sync::atomic::Ordering::SeqCst);

    // 延迟 1 秒异步退出进程，让 HTTP 响应先返回给客户端，然后由 systemd (Restart=always) 自动拉起
    tokio::spawn(async move {
        tokio::time::sleep(std::time::Duration::from_millis(1000)).await;
        n_core::process::SidecarManager::stop();
        tracing::info!("👋 服务端主动退出以完成重启");
        std::process::exit(0);
    });

    Ok(Json(serde_json::json!({
        "success": true,
        "message": "服务端正在执行安全优雅退出，等待 systemd 自动拉起恢复..."
    })))
}

#[derive(Deserialize)]
pub struct BackupQuery {
    #[serde(default)]
    automatic: bool,
}

pub async fn download_database_backup(
    State(ctx): State<Arc<ServerContext>>,
    RequireAdmin(_): RequireAdmin,
    Query(query): Query<BackupQuery>,
) -> Result<impl IntoResponse, (StatusCode, Json<ApiErrorResponse>)> {
    let now = chrono::Utc::now();
    if query.automatic
        && (!n_protocol::automatic_backup_window(now)
            || n_core::session::SessionCalendar::get("AU0")
                .is_in_trading_time_naive(&(now + chrono::Duration::hours(8)).naive_utc()))
    {
        return Err(backup_error(
            StatusCode::CONFLICT,
            "BACKUP_OUTSIDE_WINDOW",
            "自动备份仅在维护窗口执行".into(),
        ));
    }
    let download_permit = ctx
        .backup_download
        .clone()
        .try_acquire_owned()
        .map_err(|_| {
            backup_error(
                StatusCode::CONFLICT,
                "BACKUP_BUSY",
                "已有备份正在执行".into(),
            )
        })?;

    let (snapshot, file, cleanup) =
        open_backup(&ctx, query.automatic, now)
            .await
            .map_err(|error| {
                let msg = error.to_string();
                let (status, code) = if msg.contains("正在执行") {
                    (StatusCode::CONFLICT, "BACKUP_BUSY")
                } else {
                    (StatusCode::INTERNAL_SERVER_ERROR, "INTERNAL_ERROR")
                };
                tracing::warn!(error = %error, automatic = query.automatic, "打开数据库备份失败");
                backup_error(status, code, msg)
            })?;
    let mut headers = HeaderMap::new();
    headers.insert(CONTENT_TYPE, HeaderValue::from_static("application/gzip"));
    headers.insert(
        CONTENT_DISPOSITION,
        HeaderValue::from_static("attachment; filename=\"ntrend.sqlite.gz\""),
    );
    headers.insert(CONTENT_LENGTH, HeaderValue::from(snapshot.size_bytes));
    if let Ok(sha) = HeaderValue::from_str(&snapshot.sha256) {
        headers.insert("X-Database-SHA256", sha.clone());
        headers.insert("X-Backup-Sha256", sha);
    }
    tracing::info!(
        automatic = query.automatic,
        bytes = snapshot.size_bytes,
        "数据库备份开始下载"
    );
    let stream = futures_util::stream::try_unfold(
        (file, cleanup, download_permit),
        move |(mut file, cleanup, permit)| async move {
            let mut chunk = vec![0; 64 * 1024];
            let size = file.read(&mut chunk).await?;
            if size == 0 {
                return Ok::<_, std::io::Error>(None);
            }
            chunk.truncate(size);
            if query.automatic {
                // The single automatic download slot also caps aggregate backup traffic.
                tokio::time::sleep(std::time::Duration::from_secs_f64(
                    size as f64 / (384.0 * 1024.0),
                ))
                .await;
                if !n_protocol::automatic_backup_window(chrono::Utc::now()) {
                    return Err(std::io::Error::other("automatic backup window ended"));
                }
            }
            Ok(Some((chunk, (file, cleanup, permit))))
        },
    );
    Ok((headers, Body::from_stream(stream)))
}

fn backup_error(
    status: StatusCode,
    code: &str,
    msg: String,
) -> (StatusCode, Json<ApiErrorResponse>) {
    (status, Json(ApiErrorResponse::new(code, msg, "")))
}

struct TemporaryBackup(std::path::PathBuf);

impl Drop for TemporaryBackup {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
    }
}

async fn open_backup(
    ctx: &ServerContext,
    automatic: bool,
    now: chrono::DateTime<chrono::Utc>,
) -> anyhow::Result<(
    n_core::backup::SnapshotResult,
    tokio::fs::File,
    Option<TemporaryBackup>,
)> {
    let dir = ctx.data_dir.join("backups_temp");
    if automatic {
        let date = (now + chrono::Duration::hours(8)).date_naive();
        let mut cache = ctx.automatic_backup.lock().await;
        if let Some((_, snapshot)) = cache.as_ref().filter(|(day, _)| *day == date) {
            if let Ok(file) = tokio::fs::File::open(&snapshot.file_path).await {
                if file.metadata().await?.len() == snapshot.size_bytes {
                    return Ok((snapshot.clone(), file, None));
                }
            }
        }
        let snapshot =
            n_core::backup::generate_database_snapshot(&ctx.db, &dir.join("automatic")).await?;
        let file = tokio::fs::File::open(&snapshot.file_path).await?;
        *cache = Some((date, snapshot.clone()));
        return Ok((snapshot, file, None));
    }
    let snapshot = n_core::backup::generate_database_snapshot(&ctx.db, &dir).await?;
    let cleanup = TemporaryBackup(snapshot.file_path.clone());
    let file = tokio::fs::File::open(&snapshot.file_path).await?;
    Ok((snapshot, file, Some(cleanup)))
}

#[cfg(test)]
mod tests {
    use super::*;
    use n_core::sea_orm::{ConnectionTrait, DbBackend, Statement};
    use sha2::{Digest, Sha256};
    use std::io::Read;

    #[tokio::test]
    async fn streamed_backup_restores_and_automatic_requests_reuse_the_snapshot() {
        let root =
            std::env::temp_dir().join(format!("ntrend-backup-test-{}", uuid::Uuid::new_v4()));
        tokio::fs::create_dir_all(&root).await.unwrap();
        let db = n_core::storage::connect(&root.join("source.sqlite"))
            .await
            .unwrap();
        db.execute_unprepared(
            "CREATE TABLE backup_probe (n INTEGER); INSERT INTO backup_probe VALUES (42)",
        )
        .await
        .unwrap();
        let mut config = n_core::config::Config::default();
        config.data_source.primary_source = "offline".into();
        let services = n_core::service::Services::new(db.clone(), config, root.join("config.json"))
            .await
            .unwrap();
        let ctx = ServerContext::new(db, Arc::new(services), root.clone(), Default::default());
        let admin = || {
            RequireAdmin(AuthDevice {
                id: "test".into(),
                name: "test".into(),
                role: "admin".into(),
            })
        };

        let response = download_database_backup(
            State(ctx.clone()),
            admin(),
            Query(BackupQuery { automatic: false }),
        )
        .await
        .unwrap()
        .into_response();
        let sha = response.headers()["X-Backup-Sha256"]
            .to_str()
            .unwrap()
            .to_owned();
        let content_length = response.headers()[CONTENT_LENGTH]
            .to_str()
            .unwrap()
            .parse::<usize>()
            .unwrap();
        let busy = download_database_backup(
            State(ctx.clone()),
            admin(),
            Query(BackupQuery { automatic: false }),
        )
        .await
        .err()
        .unwrap();
        assert_eq!(busy.0, StatusCode::CONFLICT);
        let compressed = axum::body::to_bytes(response.into_body(), content_length)
            .await
            .unwrap();
        assert_eq!(compressed.len(), content_length);
        assert_eq!(format!("{:x}", Sha256::digest(&compressed)), sha);
        let mut raw = Vec::new();
        flate2::read::GzDecoder::new(compressed.as_ref())
            .read_to_end(&mut raw)
            .unwrap();
        let restored_file = root.join("restored.sqlite");
        tokio::fs::write(&restored_file, raw).await.unwrap();
        let restored = n_core::storage::connect(&restored_file).await.unwrap();
        let row = restored
            .query_one(Statement::from_string(
                DbBackend::Sqlite,
                "SELECT n FROM backup_probe",
            ))
            .await
            .unwrap()
            .unwrap();
        assert_eq!(row.try_get::<i64>("", "n").unwrap(), 42);
        restored.close().await.unwrap();
        assert_eq!(ctx.backup_download.available_permits(), 1);

        let now = chrono::Utc::now();
        let (first, file, _) = open_backup(&ctx, true, now).await.unwrap();
        drop(file);
        let (second, file, _) = open_backup(&ctx, true, now).await.unwrap();
        drop(file);
        assert_eq!(first.file_path, second.file_path);
        assert_eq!(first.sha256, second.sha256);
        drop(ctx);
        let resolved = root.canonicalize().unwrap();
        assert!(resolved.starts_with(std::env::temp_dir().canonicalize().unwrap()));
        let _ = std::fs::remove_dir_all(resolved);
    }
}
