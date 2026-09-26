use crate::auth::{hash_token, RequireAdmin};
use crate::state::ServerContext;
use axum::extract::{Path, State};
use axum::http::StatusCode;
use axum::response::IntoResponse;
use axum::Json;
use n_protocol::auth::{DeviceItemDto, DeviceRegisterRequest, DeviceRegisterResponse};
use n_protocol::error::ApiErrorResponse;
use std::sync::Arc;

pub async fn register_device(
    State(ctx): State<Arc<ServerContext>>,
    Json(payload): Json<DeviceRegisterRequest>,
) -> Result<Json<DeviceRegisterResponse>, (StatusCode, Json<ApiErrorResponse>)> {
    let secrets = ctx.secrets.read().await;
    if !secrets.admin_key_matches(&payload.admin_key) {
        let err = ApiErrorResponse::new("UNAUTHORIZED", "管理密钥错误，配对绑定被拒绝", "");
        return Err((StatusCode::UNAUTHORIZED, Json(err)));
    }

    // 判断是否已有设备；若这是第一个注册的设备，自动赋予 admin 角色，后续设备默认为 standard
    let existing = n_core::storage::repo::list_devices(&ctx.db)
        .await
        .unwrap_or_default();
    let role = if existing.is_empty() {
        "admin"
    } else {
        "standard"
    };

    let device_id = uuid::Uuid::new_v4().to_string();
    let raw_token = format!(
        "{}_{}",
        uuid::Uuid::new_v4().simple(),
        uuid::Uuid::new_v4().simple()
    );
    let token_hash = hash_token(&raw_token);

    if let Err(e) =
        n_core::storage::repo::create_device(&ctx.db, &device_id, &payload.device_name, &token_hash, role)
            .await
    {
        tracing::error!("注册设备落库失败: {e}");
        let err = ApiErrorResponse::new("INTERNAL_ERROR", "设备注册落库失败", "");
        return Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)));
    }

    tracing::info!(
        "📱 新设备注册成功 | ID: {} | 名称: {} | 角色: {}",
        device_id,
        payload.device_name,
        role
    );

    Ok(Json(DeviceRegisterResponse {
        device_id,
        token: raw_token,
        role: role.to_string(),
        server_time: chrono::Local::now().timestamp_millis(),
    }))
}

pub async fn list_devices(
    State(ctx): State<Arc<ServerContext>>,
    RequireAdmin(_): RequireAdmin,
) -> Result<Json<Vec<DeviceItemDto>>, (StatusCode, Json<ApiErrorResponse>)> {
    match n_core::storage::repo::list_devices(&ctx.db).await {
        Ok(models) => {
            let dtos = models
                .into_iter()
                .map(|m| DeviceItemDto {
                    id: m.id,
                    name: m.name,
                    role: m.role,
                    created_at: m.created_at,
                    last_seen_at: m.last_seen_at,
                    revoked_at: m.revoked_at,
                })
                .collect();
            Ok(Json(dtos))
        }
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("获取设备列表失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

pub async fn revoke_device(
    State(ctx): State<Arc<ServerContext>>,
    RequireAdmin(_): RequireAdmin,
    Path(id): Path<String>,
) -> Result<impl IntoResponse, (StatusCode, Json<ApiErrorResponse>)> {
    match n_core::storage::repo::revoke_device(&ctx.db, &id).await {
        Ok(success) => {
            if success {
                tracing::warn!("🚫 设备已被吊销 | ID: {}", id);
                Ok(StatusCode::NO_CONTENT)
            } else {
                let err = ApiErrorResponse::new("NOT_FOUND", "设备不存在或已被吊销", "");
                Err((StatusCode::NOT_FOUND, Json(err)))
            }
        }
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("吊销设备失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}
