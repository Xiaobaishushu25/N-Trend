use crate::auth::{AuthDevice, RequireAdmin};
use crate::state::ServerContext;
use axum::body::Body;
use axum::extract::State;
use axum::http::header::{CONTENT_DISPOSITION, CONTENT_LENGTH, CONTENT_TYPE};
use axum::http::{HeaderMap, HeaderValue, StatusCode};
use axum::response::IntoResponse;
use axum::Json;
use n_protocol::dto::SchedulerStatus;
use n_protocol::error::ApiErrorResponse;
use serde::Deserialize;
use std::sync::Arc;

#[derive(Deserialize)]
pub struct SetRunningPayload {
    pub running: bool,
}

pub async fn get_scheduler_status(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
) -> Result<Json<SchedulerStatus>, (StatusCode, Json<ApiErrorResponse>)> {
    let last_refresh = ctx.last_refresh.read().await.map(|t| t.format("%Y-%m-%d %H:%M:%S").to_string());
    let last_scan = ctx.last_scan.read().await.map(|t| t.format("%Y-%m-%d %H:%M:%S").to_string());
    let running = ctx.scheduler_running.load(std::sync::atomic::Ordering::Relaxed);
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
    ctx.scheduler_running.store(payload.running, std::sync::atomic::Ordering::SeqCst);
    let last_refresh = ctx.last_refresh.read().await.map(|t| t.format("%Y-%m-%d %H:%M:%S").to_string());
    let last_scan = ctx.last_scan.read().await.map(|t| t.format("%Y-%m-%d %H:%M:%S").to_string());
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

pub async fn download_database_backup(
    State(ctx): State<Arc<ServerContext>>,
    RequireAdmin(_): RequireAdmin,
) -> Result<impl IntoResponse, (StatusCode, Json<ApiErrorResponse>)> {
    let temp_dir = ctx.data_dir.join("backups_temp");
    match n_core::backup::generate_database_snapshot(&ctx.db, &temp_dir).await {
        Ok(snapshot) => {
            let bytes = match tokio::fs::read(&snapshot.file_path).await {
                Ok(b) => b,
                Err(e) => {
                    let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("读取快照失败: {e}"), "");
                    return Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)));
                }
            };

            // 发送完毕后删除服务端临时快照文件
            let _ = tokio::fs::remove_file(&snapshot.file_path).await;

            let mut headers = HeaderMap::new();
            headers.insert(CONTENT_TYPE, HeaderValue::from_static("application/gzip"));
            headers.insert(
                CONTENT_DISPOSITION,
                HeaderValue::from_static("attachment; filename=\"ntrend.sqlite.gz\""),
            );
            headers.insert(CONTENT_LENGTH, HeaderValue::from(bytes.len()));
            if let Ok(sha_val) = HeaderValue::from_str(&snapshot.sha256) {
                headers.insert("X-Database-SHA256", sha_val.clone());
                headers.insert("X-Backup-Sha256", sha_val);
            }

            tracing::info!(
                "📦 [备份下载] 成功生成并发送一致性数据库快照: 大小 {} 字节, SHA-256: {}",
                bytes.len(),
                snapshot.sha256
            );

            Ok((headers, Body::from(bytes)))
        }
        Err(e) => {
            let err = ApiErrorResponse::new(
                "INTERNAL_ERROR",
                format!("生成一致性快照备份失败: {e}"),
                "",
            );
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}
