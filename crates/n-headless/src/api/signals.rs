use crate::auth::{AuthDevice, RequireAdmin};
use crate::state::ServerContext;
use axum::extract::{Path, Query, State};
use axum::http::StatusCode;
use axum::response::IntoResponse;
use axum::Json;
use n_core::sea_orm::{ConnectionTrait, DbBackend, Statement};
use n_protocol::dto::{OutcomeRefresh, V2ModelRow, V2PredictionRow, V2ReportBundle};
use n_protocol::error::ApiErrorResponse;
use serde::Deserialize;
use std::sync::Arc;

#[derive(Deserialize)]
pub struct ReviewStatsQuery {
    pub dimension: String,
    pub scope: Option<String>,
    pub version: Option<String>,
    pub score_min: Option<f64>,
    pub score_max: Option<f64>,
}

#[derive(Deserialize)]
pub struct RecentOutcomesQuery {
    pub limit: Option<usize>,
    pub symbol: Option<String>,
    pub version: Option<String>,
    pub direction: Option<String>,
    pub level: Option<String>,
    pub grade: Option<String>,
    pub score_min: Option<f64>,
    pub score_max: Option<f64>,
    pub outcome: Option<String>,
}

#[derive(Deserialize)]
pub struct V2PredictionsQuery {
    pub model_id: Option<String>,
}

pub async fn get_active_events(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
) -> Result<Json<serde_json::Value>, (StatusCode, Json<ApiErrorResponse>)> {
    match n_core::storage::repo::all_pattern_events(&ctx.db).await {
        Ok(events) => {
            let active: Vec<_> = events
                .into_iter()
                .filter(|e| e.state == "pending" || e.state == "triggered")
                .collect();
            Ok(Json(serde_json::to_value(active).unwrap_or_default()))
        }
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("获取活跃形态失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

pub async fn get_preclose_signals(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
) -> Result<Json<serde_json::Value>, (StatusCode, Json<ApiErrorResponse>)> {
    match n_core::storage::repo::all_preclose_signals(&ctx.db).await {
        Ok(rows) => Ok(Json(serde_json::to_value(rows).unwrap_or_default())),
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("获取收盘前预警失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

pub async fn get_preclose_candidates(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
) -> Result<Json<serde_json::Value>, (StatusCode, Json<ApiErrorResponse>)> {
    match n_core::storage::repo::all_preclose_candidates(&ctx.db).await {
        Ok(rows) => Ok(Json(serde_json::to_value(rows).unwrap_or_default())),
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("获取临时未收盘扫描失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

pub async fn get_review_stats(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
    Query(q): Query<ReviewStatsQuery>,
) -> Result<Json<serde_json::Value>, (StatusCode, Json<ApiErrorResponse>)> {
    let scope = q.scope.unwrap_or_default();
    match ctx
        .services
        .review_stats(
            &q.dimension,
            &scope,
            q.version.as_deref(),
            q.score_min,
            q.score_max,
        )
        .await
    {
        Ok(s) => Ok(Json(serde_json::to_value(s).unwrap_or_default())),
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("获取复盘统计失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

pub async fn get_recent_outcomes(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
    Query(q): Query<RecentOutcomesQuery>,
) -> Result<Json<serde_json::Value>, (StatusCode, Json<ApiErrorResponse>)> {
    let filter = n_core::service::OutcomeFilter {
        symbol: q.symbol,
        direction: q.direction,
        level: q.level,
        grade: q.grade,
        score_min: q.score_min,
        score_max: q.score_max,
        outcome: q.outcome,
        version: q.version,
    };

    match ctx.services.recent_outcomes(q.limit.unwrap_or(2000), &filter).await {
        Ok(list) => Ok(Json(serde_json::to_value(list).unwrap_or_default())),
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("获取明细列表失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

pub async fn get_review_signal(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
    Path(event_id): Path<i64>,
) -> Result<Json<serde_json::Value>, (StatusCode, Json<ApiErrorResponse>)> {
    match ctx.services.review_signal(event_id).await {
        Ok(s) => Ok(Json(serde_json::to_value(s).unwrap_or(serde_json::Value::Null))),
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("获取复盘信号详情失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

pub async fn refresh_outcomes(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
) -> Result<Json<OutcomeRefresh>, (StatusCode, Json<ApiErrorResponse>)> {
    match ctx.services.refresh_outcomes().await {
        Ok(r) => {
            let rev = n_core::storage::repo::bump_revision(&ctx.db, "signals").await.unwrap_or(1);
            let seq = ctx.next_event_seq();
            ctx.realtime_hub.broadcast(seq, "state.changed", serde_json::json!({ "scope": "signals", "revision": rev })).await;
            Ok(Json(OutcomeRefresh { updated: r.updated }))
        }
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("刷新结局失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

pub async fn get_v2_models(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
) -> Result<Json<Vec<V2ModelRow>>, (StatusCode, Json<ApiErrorResponse>)> {
    let rows = ctx.db.query_all(Statement::from_string(
        DbBackend::Sqlite,
        "SELECT model_id, name, schema_version, feature_whitelist, train_window, dataset_hash, coefficients, spline_knots, metrics, created_at, status, scoring_slot FROM v2_model_registry ORDER BY created_at DESC".to_string()
    )).await;
    match rows {
        Ok(r) => {
            let mut out = Vec::new();
            for row in r {
                let model_id: String = row.try_get("", "model_id").unwrap_or_default();
                let name: String = row.try_get("", "name").unwrap_or_default();
                let status: String = row.try_get("", "status").unwrap_or_default();
                let scoring_slot: String = row.try_get("", "scoring_slot").unwrap_or_default();
                let _ = scoring_slot;
                out.push(V2ModelRow {
                    model_id,
                    name,
                    status: status.clone(),
                    is_champion: status == "champion",
                    accuracy: 0.0,
                    precision: 0.0,
                    recall: 0.0,
                    f1: 0.0,
                    created_at: row.try_get("", "created_at").unwrap_or_default(),
                });
            }
            Ok(Json(out))
        }
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("获取 V2 模型失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

pub async fn get_v2_predictions(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
    Query(q): Query<V2PredictionsQuery>,
) -> Result<Json<Vec<V2PredictionRow>>, (StatusCode, Json<ApiErrorResponse>)> {
    let sql = if let Some(mid) = q.model_id.filter(|s| !s.is_empty()) {
        format!("SELECT id, event_id, model_id, p_win, logit, feature_hash, predicted_at, prediction_mode FROM v2_model_predictions WHERE model_id='{}' ORDER BY predicted_at DESC LIMIT 2000", mid.replace('\'', "''"))
    } else {
        "SELECT p.id, p.event_id, p.model_id, p.p_win, p.logit, p.feature_hash, p.predicted_at, p.prediction_mode FROM v2_model_predictions p INNER JOIN v2_model_registry r ON r.model_id = p.model_id WHERE r.status='champion' ORDER BY p.predicted_at DESC LIMIT 2000".to_string()
    };
    match ctx.db.query_all(Statement::from_string(DbBackend::Sqlite, sql)).await {
        Ok(rows) => {
            let mut out = Vec::new();
            for r in rows {
                out.push(V2PredictionRow {
                    event_id: r.try_get("", "event_id").unwrap_or(0),
                    model_id: r.try_get("", "model_id").unwrap_or_default(),
                    p_win: r.try_get("", "p_win").unwrap_or(0.0),
                    pred_label: 0,
                    created_at: r.try_get("", "predicted_at").unwrap_or_default(),
                });
            }
            Ok(Json(out))
        }
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("获取预测数据失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

pub async fn backfill_v2_predictions(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
) -> Result<Json<serde_json::Value>, (StatusCode, Json<ApiErrorResponse>)> {
    match n_core::v2::prediction::backfill(&ctx.db).await {
        Ok(res) => Ok(Json(serde_json::to_value(res).unwrap_or_default())),
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("回填模型预测失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

pub async fn get_v2_report(
    State(_ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
) -> Result<Json<V2ReportBundle>, (StatusCode, Json<ApiErrorResponse>)> {
    let mut v = serde_json::json!({});
    for name in [
        "logistic_report.md",
        "gam_report.md",
        "acceptance.md",
        "market_context_research.md",
    ] {
        let p = std::path::Path::new("target/v2_reports").join(name);
        if let Ok(s) = std::fs::read_to_string(&p) {
            v[name] = serde_json::Value::String(s);
        }
    }
    Ok(Json(V2ReportBundle { summary: v }))
}

#[derive(Deserialize)]
pub struct SetModelStatusReq {
    pub model_id: String,
    pub status: String,
}

pub async fn set_v2_model_status(
    State(ctx): State<Arc<ServerContext>>,
    RequireAdmin(_): RequireAdmin,
    Json(payload): Json<SetModelStatusReq>,
) -> Result<impl IntoResponse, (StatusCode, Json<ApiErrorResponse>)> {
    let sql = format!(
        "UPDATE v2_model_registry SET status='{}' WHERE model_id='{}'",
        payload.status.replace('\'', "''"),
        payload.model_id.replace('\'', "''")
    );
    match ctx.db.execute_unprepared(&sql).await {
        Ok(_) => Ok(StatusCode::OK),
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("更新模型状态失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}
