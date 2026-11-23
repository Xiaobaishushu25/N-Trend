use crate::state::ServerContext;
use axum::async_trait;
use axum::extract::{FromRequestParts, Query};
use axum::http::request::Parts;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use n_core::sea_orm::DatabaseConnection;
use n_protocol::error::ApiErrorResponse;
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::{Mutex, Semaphore};

const DEVICE_TOUCH_INTERVAL: Duration = Duration::from_secs(60);

pub(crate) struct DeviceActivity {
    last_queued: Mutex<HashMap<String, Instant>>,
    writer: Arc<Semaphore>,
}

impl Default for DeviceActivity {
    fn default() -> Self {
        Self {
            last_queued: Mutex::default(),
            writer: Arc::new(Semaphore::new(1)),
        }
    }
}

impl DeviceActivity {
    async fn mark_seen(&self, db: &DatabaseConnection, id: &str) {
        let mut last = self.last_queued.lock().await;
        let now = Instant::now();
        if last
            .get(id)
            .is_some_and(|time| now.duration_since(*time) < DEVICE_TOUCH_INTERVAL)
        {
            return;
        }
        let Ok(permit) = self.writer.clone().try_acquire_owned() else {
            return;
        };
        last.retain(|_, time| now.duration_since(*time) < DEVICE_TOUCH_INTERVAL);
        last.insert(id.to_owned(), now);
        let db = db.clone();
        let id = id.to_owned();
        tokio::spawn(async move {
            let _permit = permit;
            if let Err(error) = n_core::storage::repo::touch_device_last_seen(&db, &id).await {
                tracing::debug!(%error, "设备活动时间更新失败");
            }
        });
    }
}

pub fn hash_token(token: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(token.as_bytes());
    format!("{:x}", hasher.finalize())
}

#[derive(Clone, Debug)]
pub struct AuthDevice {
    pub id: String,
    pub name: String,
    pub role: String,
}

#[derive(Deserialize)]
struct TokenQuery {
    token: Option<String>,
}

#[async_trait]
impl FromRequestParts<Arc<ServerContext>> for AuthDevice {
    type Rejection = Response;

    async fn from_request_parts(
        parts: &mut Parts,
        state: &Arc<ServerContext>,
    ) -> Result<Self, Self::Rejection> {
        authenticate(parts, state, &state.db).await
    }
}

pub(crate) struct ChartDevice;

#[async_trait]
impl FromRequestParts<Arc<ServerContext>> for ChartDevice {
    type Rejection = Response;

    async fn from_request_parts(
        parts: &mut Parts,
        state: &Arc<ServerContext>,
    ) -> Result<Self, Self::Rejection> {
        authenticate(parts, state, state.services.chart_reader())
            .await
            .map(|_| Self)
    }
}

async fn authenticate(
    parts: &Parts,
    state: &ServerContext,
    reader: &DatabaseConnection,
) -> Result<AuthDevice, Response> {
    let request_id = parts
        .headers
        .get("x-request-id")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .to_string();

    let mut raw_token = None;

    // 1. 尝试从 Authorization: Bearer <token> 提取
    if let Some(auth_header) = parts.headers.get("authorization") {
        if let Ok(auth_str) = auth_header.to_str() {
            if let Some(token) = auth_str.strip_prefix("Bearer ") {
                raw_token = Some(token.trim().to_string());
            }
        }
    }

    // 2. 尝试从 Query 参数提取 (WebSocket 握手适配)
    if raw_token.is_none() {
        if let Ok(Query(query)) = Query::<TokenQuery>::try_from_uri(&parts.uri) {
            if let Some(t) = query.token {
                if !t.trim().is_empty() {
                    raw_token = Some(t.trim().to_string());
                }
            }
        }
    }

    let token = match raw_token {
        Some(t) if !t.is_empty() => t,
        _ => {
            let err = ApiErrorResponse::new(
                "UNAUTHORIZED",
                "缺少认证令牌，请携带 Bearer 令牌或 ?token= 参数",
                request_id,
            );
            return Err((StatusCode::UNAUTHORIZED, Json(err)).into_response());
        }
    };

    let hashed = hash_token(&token);
    match n_core::storage::repo::find_device_by_token_hash(reader, &hashed).await {
        Ok(Some(dev)) => {
            let id = dev.id.clone();
            let name = dev.name.clone();
            let role = dev.role.clone();
            state.device_activity.mark_seen(&state.db, &id).await;
            Ok(AuthDevice { id, name, role })
        }
        Ok(None) => {
            let err = ApiErrorResponse::new(
                "UNAUTHORIZED",
                "设备令牌无效或已被吊销，请重新配对绑定",
                request_id,
            );
            Err((StatusCode::UNAUTHORIZED, Json(err)).into_response())
        }
        Err(e) => {
            tracing::error!("验证令牌异常: {e}");
            let err = ApiErrorResponse::new("INTERNAL_ERROR", "服务端认证查询失败", request_id);
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)).into_response())
        }
    }
}

#[allow(dead_code)]
pub struct RequireAdmin(pub AuthDevice);

#[async_trait]
impl FromRequestParts<Arc<ServerContext>> for RequireAdmin {
    type Rejection = Response;

    async fn from_request_parts(
        parts: &mut Parts,
        state: &Arc<ServerContext>,
    ) -> Result<Self, Self::Rejection> {
        let device = AuthDevice::from_request_parts(parts, state).await?;
        if device.role == "admin" {
            Ok(RequireAdmin(device))
        } else {
            let request_id = parts
                .headers
                .get("x-request-id")
                .and_then(|v| v.to_str().ok())
                .unwrap_or("")
                .to_string();
            let err = ApiErrorResponse::new(
                "FORBIDDEN",
                "权限不足：该操作仅允许 PC 主管理员设备执行",
                request_id,
            );
            Err((StatusCode::FORBIDDEN, Json(err)).into_response())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use n_core::sea_orm::ConnectionTrait;

    #[tokio::test]
    async fn activity_is_coalesced_without_caching_device_revocation() {
        use n_core::storage::repo;
        let db = n_core::storage::connect(std::path::Path::new(":memory:"))
            .await
            .unwrap();
        repo::create_device(&db, "reader", "reader", "test-token-hash", "device")
            .await
            .unwrap();
        let activity = DeviceActivity::default();
        let mark_old = "UPDATE devices SET last_seen_at='2000-01-01 00:00:00' WHERE id='reader'";
        db.execute_unprepared(mark_old).await.unwrap();
        activity.mark_seen(&db, "reader").await;
        drop(activity.writer.acquire().await.unwrap());
        let device = repo::find_device_by_token_hash(&db, "test-token-hash")
            .await
            .unwrap()
            .unwrap();
        assert_ne!(device.last_seen_at.as_deref(), Some("2000-01-01 00:00:00"));

        db.execute_unprepared(mark_old).await.unwrap();
        for _ in 0..10 {
            activity.mark_seen(&db, "reader").await;
        }
        drop(activity.writer.acquire().await.unwrap());
        let device = repo::find_device_by_token_hash(&db, "test-token-hash")
            .await
            .unwrap()
            .unwrap();
        assert_eq!(device.last_seen_at.as_deref(), Some("2000-01-01 00:00:00"));
        repo::revoke_device(&db, "reader").await.unwrap();
        assert!(repo::find_device_by_token_hash(&db, "test-token-hash")
            .await
            .unwrap()
            .is_none());
    }
}
