use axum::extract::Request;
use axum::http::{HeaderValue, Method, StatusCode};
use axum::middleware::Next;
use axum::response::{IntoResponse, Response};
use axum::Json;
use n_protocol::version::{require_current_peer, API_HEADER, RELEASE_HEADER};

pub async fn enforce_version(req: Request, next: Next) -> Response {
    // Metadata and probes remain available to diagnose an upgrade mismatch.
    let exempt =
        req.method() == Method::OPTIONS || matches!(req.uri().path(), "/healthz" | "/api/v2/meta");
    let result = require_current_peer(
        "客户端",
        req.headers()
            .get(RELEASE_HEADER)
            .and_then(|v| v.to_str().ok()),
        req.headers().get(API_HEADER).and_then(|v| v.to_str().ok()),
    );
    let mut response = match (exempt, result) {
        (false, Err(mut error)) => {
            error.request_id = req
                .headers()
                .get("x-request-id")
                .and_then(|v| v.to_str().ok())
                .unwrap_or_default()
                .to_string();
            (StatusCode::UPGRADE_REQUIRED, Json(error)).into_response()
        }
        _ => next.run(req).await,
    };
    response.headers_mut().insert(
        RELEASE_HEADER,
        HeaderValue::from_static(n_protocol::CURRENT_SERVER_VERSION),
    );
    response.headers_mut().insert(
        API_HEADER,
        HeaderValue::from_static(n_protocol::API_VERSION),
    );
    response
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{
        body::{to_bytes, Body},
        routing::get,
        Router,
    };
    use tower::ServiceExt;

    fn router() -> Router {
        Router::new()
            .route("/api/v2/review/outcomes", get(|| async { "loaded" }))
            .route(
                "/api/v2/ws",
                get(|| async { StatusCode::SWITCHING_PROTOCOLS }),
            )
            .route("/api/v2/meta", get(|| async { "meta" }))
            .route("/healthz", get(|| async { "OK" }))
            .layer(axum::middleware::from_fn(enforce_version))
    }

    #[tokio::test]
    async fn rejects_old_missing_and_newer_clients_before_rest_or_websocket_handlers() {
        for path in ["/api/v2/review/outcomes", "/api/v2/ws"] {
            for release in [None, Some("3.0.1"), Some("999.0.0")] {
                let mut req = Request::builder()
                    .uri(path)
                    .header(API_HEADER, n_protocol::API_VERSION);
                if let Some(release) = release {
                    req = req.header(RELEASE_HEADER, release);
                }
                let response = router()
                    .oneshot(req.body(Body::empty()).unwrap())
                    .await
                    .unwrap();
                assert_eq!(response.status(), StatusCode::UPGRADE_REQUIRED);
                assert_eq!(
                    response.headers()[RELEASE_HEADER],
                    n_protocol::CURRENT_SERVER_VERSION
                );
                let body = to_bytes(response.into_body(), 4096).await.unwrap();
                let error: n_protocol::ApiErrorResponse = serde_json::from_slice(&body).unwrap();
                assert_eq!(error.code, "UPGRADE_REQUIRED");
            }
        }
    }

    #[tokio::test]
    async fn current_clients_work_and_metadata_is_available_for_upgrade_diagnostics() {
        for (path, expected) in [
            ("/api/v2/review/outcomes", StatusCode::OK),
            ("/api/v2/ws", StatusCode::SWITCHING_PROTOCOLS),
        ] {
            let req = Request::builder()
                .uri(path)
                .header(RELEASE_HEADER, n_protocol::CURRENT_SERVER_VERSION)
                .header(API_HEADER, n_protocol::API_VERSION)
                .body(Body::empty())
                .unwrap();
            let response = router().oneshot(req).await.unwrap();
            assert_eq!(response.status(), expected);
            assert_eq!(response.headers()[API_HEADER], n_protocol::API_VERSION);
        }
        for path in ["/healthz", "/api/v2/meta"] {
            let response = router()
                .oneshot(Request::builder().uri(path).body(Body::empty()).unwrap())
                .await
                .unwrap();
            assert_eq!(response.status(), StatusCode::OK);
        }
    }

    #[tokio::test]
    async fn rejects_mutations_before_executing_them_and_does_not_expose_v1_routes() {
        use std::sync::{
            atomic::{AtomicUsize, Ordering},
            Arc,
        };
        let calls = Arc::new(AtomicUsize::new(0));
        let observed = calls.clone();
        let router = Router::new()
            .route(
                "/api/v2/actions/scan",
                axum::routing::post(move || {
                    let calls = observed.clone();
                    async move {
                        calls.fetch_add(1, Ordering::SeqCst);
                        "done"
                    }
                }),
            )
            .layer(axum::middleware::from_fn(enforce_version));
        for api in [None, Some("v1")] {
            let mut req = Request::builder()
                .method(Method::POST)
                .uri("/api/v2/actions/scan")
                .header(RELEASE_HEADER, n_protocol::CURRENT_SERVER_VERSION);
            if let Some(api) = api {
                req = req.header(API_HEADER, api);
            }
            let response = router
                .clone()
                .oneshot(req.body(Body::empty()).unwrap())
                .await
                .unwrap();
            assert_eq!(response.status(), StatusCode::UPGRADE_REQUIRED);
        }
        let req = Request::builder()
            .method(Method::POST)
            .uri("/api/v1/actions/scan")
            .header(RELEASE_HEADER, n_protocol::CURRENT_SERVER_VERSION)
            .header(API_HEADER, n_protocol::API_VERSION)
            .body(Body::empty())
            .unwrap();
        assert_eq!(
            router.oneshot(req).await.unwrap().status(),
            StatusCode::NOT_FOUND
        );
        assert_eq!(calls.load(Ordering::SeqCst), 0);
    }
}
