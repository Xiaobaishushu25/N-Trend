//! New review endpoints. Legacy routes stay in signals.rs until UI migration.
use crate::{
    auth::{AuthDevice, RequireAdmin},
    state::ServerContext,
};
use axum::{
    extract::{Path, Query, State},
    http::StatusCode,
    Json,
};
use n_protocol::{error::ApiErrorResponse, review::*};
use serde::Deserialize;
use std::sync::Arc;

type Failure = (StatusCode, Json<ApiErrorResponse>);
fn failure(e: anyhow::Error) -> Failure {
    let (code, status) = if let Some(e) = e.downcast_ref::<n_core::review::ReviewError>() {
        (
            e.code,
            match e.code {
                "NOT_FOUND" => StatusCode::NOT_FOUND,
                "SNAPSHOT_EXPIRED" => StatusCode::GONE,
                "INVALID_QUERY" => StatusCode::BAD_REQUEST,
                "QUEUE_FULL" => StatusCode::TOO_MANY_REQUESTS,
                _ => StatusCode::INTERNAL_SERVER_ERROR,
            },
        )
    } else {
        ("REVIEW_FAILED", StatusCode::INTERNAL_SERVER_ERROR)
    };
    if status.is_server_error() {
        tracing::error!(error=%e,"review request failed");
    }
    (status, Json(ApiErrorResponse::new(code, e.to_string(), "")))
}
fn invalid(e: anyhow::Error) -> Failure {
    (
        StatusCode::BAD_REQUEST,
        Json(ApiErrorResponse::new("INVALID_QUERY", e.to_string(), "")),
    )
}

pub async fn metadata(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
) -> Result<Json<ReviewMetadata>, Failure> {
    ctx.review.metadata().await.map(Json).map_err(failure)
}
pub async fn query(
    State(ctx): State<Arc<ServerContext>>,
    device: AuthDevice,
    Json(q): Json<ReviewQuery>,
) -> Result<Json<ReviewQueryResponse>, Failure> {
    ctx.review
        .query(&device.id, q)
        .await
        .map(Json)
        .map_err(failure)
}
pub async fn items(
    State(ctx): State<Arc<ServerContext>>,
    device: AuthDevice,
    Path(id): Path<String>,
    Query(p): Query<ReviewPageRequest>,
) -> Result<Json<ReviewItemsPage>, Failure> {
    if !(1..=500).contains(&p.limit.unwrap_or(100))
        || p.cursor.is_some_and(|n| n > i64::MAX as usize)
    {
        return Err(invalid(anyhow::anyhow!("invalid page bounds")));
    }
    ctx.review
        .items(&device.id, &id, p)
        .await
        .map(Json)
        .map_err(failure)
}
pub async fn aggregate(
    State(ctx): State<Arc<ServerContext>>,
    device: AuthDevice,
    Json(q): Json<ReviewAggregateRequest>,
) -> Result<Json<ReviewAggregateResponse>, Failure> {
    ctx.review
        .aggregate(&device.id, q)
        .await
        .map(Json)
        .map_err(failure)
}

#[derive(Deserialize)]
pub struct ResultQuery {
    pub version: String,
    pub cursor: Option<usize>,
    pub limit: Option<usize>,
}
#[derive(Deserialize)]
pub struct DetailQuery {
    pub version: Option<String>,
}
pub async fn detail(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
    Path(id): Path<i64>,
    Query(q): Query<DetailQuery>,
) -> Result<Json<ReviewResultDetail>, Failure> {
    let result = match q.version {
        Some(version) => ctx.review.detail(id, &version).await,
        None => ctx.review.current_detail(id).await,
    };
    result.map(Json).map_err(failure)
}
pub async fn holding(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
    Path(id): Path<i64>,
    Query(q): Query<ResultQuery>,
) -> Result<Json<ReviewHoldingPage>, Failure> {
    if !(1..=500).contains(&q.limit.unwrap_or(100))
        || q.cursor.is_some_and(|n| n > i64::MAX as usize)
    {
        return Err(invalid(anyhow::anyhow!("invalid page bounds")));
    }
    ctx.review
        .holding(
            id,
            &q.version,
            q.cursor.unwrap_or(0),
            q.limit.unwrap_or(100),
        )
        .await
        .map(Json)
        .map_err(failure)
}
pub async fn rebuild(
    State(ctx): State<Arc<ServerContext>>,
    _admin: RequireAdmin,
    Json(q): Json<ReviewRebuildRequest>,
) -> Result<(StatusCode, Json<ReviewJob>), Failure> {
    ctx.review
        .rebuild(q)
        .await
        .map(|j| (StatusCode::ACCEPTED, Json(j)))
        .map_err(failure)
}
pub async fn job(
    State(ctx): State<Arc<ServerContext>>,
    _admin: RequireAdmin,
    Path(id): Path<String>,
) -> Result<Json<ReviewJob>, Failure> {
    ctx.review.get_job(&id).await.map(Json).map_err(failure)
}

pub fn routes() -> axum::Router<Arc<ServerContext>> {
    use axum::routing::{get, post};
    axum::Router::new()
        .route("/review/metadata", get(metadata))
        .route("/review/query", post(query))
        .route("/review/queries/:id/items", get(items))
        .route("/review/aggregate", post(aggregate))
        .route("/review/signals/:id/detail", get(detail))
        .route("/review/signals/:id/holding", get(holding))
        .route("/review/rebuild", post(rebuild))
        .route("/review/jobs/:id", get(job))
        .layer(tower_http::compression::CompressionLayer::new())
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{
        body::{to_bytes, Body},
        http::Request,
    };
    use serde_json::{json, Value};
    use tower::ServiceExt;

    async fn call(
        router: &axum::Router,
        method: &str,
        path: &str,
        token: Option<&str>,
        body: Value,
    ) -> (StatusCode, axum::http::HeaderMap, Vec<u8>) {
        let mut request = Request::builder()
            .method(method)
            .uri(path)
            .header(
                n_protocol::version::RELEASE_HEADER,
                n_protocol::CURRENT_SERVER_VERSION,
            )
            .header(n_protocol::version::API_HEADER, n_protocol::API_VERSION)
            .header("content-type", "application/json")
            .header("accept-encoding", "gzip");
        if let Some(token) = token {
            request = request.header("authorization", format!("Bearer {token}"));
        }
        let response = router
            .clone()
            .oneshot(request.body(Body::from(body.to_string())).unwrap())
            .await
            .unwrap();
        let status = response.status();
        let headers = response.headers().clone();
        (
            status,
            headers,
            to_bytes(response.into_body(), 4 * 1024 * 1024)
                .await
                .unwrap()
                .to_vec(),
        )
    }
    fn decoded(headers: &axum::http::HeaderMap, body: &[u8]) -> Value {
        use std::io::Read;
        let bytes = if headers.get("content-encoding").is_some_and(|h| h == "gzip") {
            let mut out = vec![];
            flate2::read::GzDecoder::new(body)
                .read_to_end(&mut out)
                .unwrap();
            out
        } else {
            body.to_vec()
        };
        serde_json::from_slice(&bytes).unwrap()
    }
    #[tokio::test]
    async fn routes_enforce_auth_validate_queries_compress_and_keep_legacy_contracts() {
        let root = std::env::temp_dir().join(format!("ntrend-review-api-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&root).unwrap();
        let db = n_core::storage::connect(std::path::Path::new(":memory:"))
            .await
            .unwrap();
        for (token, role) in [
            ("review-admin-test", "admin"),
            ("review-read-test", "standard"),
        ] {
            n_core::storage::repo::create_device(
                &db,
                token,
                token,
                &crate::auth::hash_token(token),
                role,
            )
            .await
            .unwrap();
        }
        let mut config = n_core::config::Config::default();
        config.data_source.primary_source = "offline".into();
        config.data_source.auto_spawn_bridge = false;
        let services = Arc::new(
            n_core::service::Services::new(db.clone(), config, root.join("config.json"))
                .await
                .unwrap(),
        );
        let ctx = ServerContext::new(db, services, root, crate::secrets::ServerSecrets::default());
        let router = crate::api::build_router(ctx);
        assert_eq!(
            call(&router, "GET", "/api/v2/review/metadata", None, Value::Null)
                .await
                .0,
            StatusCode::UNAUTHORIZED
        );
        let (status, headers, body) = call(
            &router,
            "GET",
            "/api/v2/review/metadata",
            Some("review-read-test"),
            Value::Null,
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(headers.get("content-encoding").unwrap(), "gzip");
        assert_eq!(decoded(&headers, &body)["profile"], PROFILE);
        assert_eq!(
            call(
                &router,
                "POST",
                "/api/v2/review/rebuild",
                Some("review-read-test"),
                json!({})
            )
            .await
            .0,
            StatusCode::FORBIDDEN
        );
        assert_eq!(
            call(
                &router,
                "POST",
                "/api/v2/review/query",
                Some("review-read-test"),
                json!({"filters":{"score_min":101}})
            )
            .await
            .0,
            StatusCode::BAD_REQUEST
        );
        assert_eq!(
            call(
                &router,
                "POST",
                "/api/v2/review/aggregate",
                Some("review-read-test"),
                json!({"snapshot_id":"missing","dimensions":["unknown"]})
            )
            .await
            .0,
            StatusCode::BAD_REQUEST
        );
        assert_eq!(
            call(
                &router,
                "GET",
                "/api/v2/review/queries/expired/items",
                Some("review-read-test"),
                Value::Null
            )
            .await
            .0,
            StatusCode::GONE
        );
        let (status, h, b) = call(
            &router,
            "POST",
            "/api/v2/review/query",
            Some("review-read-test"),
            json!({}),
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        let page = decoded(&h, &b);
        assert_eq!(page["total"], 0);
        assert_eq!(page["scored"], 0);
        assert_eq!(page["unscored"], 0);
        assert!(page["groups"]["score_band"].is_array());
        let (status, h, b) = call(
            &router,
            "POST",
            "/api/v2/review/rebuild",
            Some("review-admin-test"),
            json!({}),
        )
        .await;
        assert_eq!(status, StatusCode::ACCEPTED);
        let job = decoded(&h, &b);
        assert_eq!(
            call(
                &router,
                "GET",
                &format!("/api/v2/review/jobs/{}", job["id"].as_str().unwrap()),
                Some("review-read-test"),
                Value::Null
            )
            .await
            .0,
            StatusCode::FORBIDDEN
        );
        assert_eq!(
            call(
                &router,
                "GET",
                "/api/v2/review/signals/1/detail?version=missing",
                Some("review-read-test"),
                Value::Null
            )
            .await
            .0,
            StatusCode::NOT_FOUND
        );
        // Event-only links resolve a published head; missing heads are an explicit 404.
        assert_eq!(call(&router, "GET", "/api/v2/review/signals/1/detail", Some("review-read-test"), Value::Null).await.0, StatusCode::NOT_FOUND);
        let (status, h, b) = call(
            &router,
            "GET",
            "/api/v2/review/outcomes?paged=true",
            Some("review-read-test"),
            Value::Null,
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        let legacy = decoded(&h, &b);
        assert!(legacy["items"].is_array());
        assert!(legacy["symbols"].is_array());
        assert!(legacy.get("snapshot_id").is_none());
    }
}
