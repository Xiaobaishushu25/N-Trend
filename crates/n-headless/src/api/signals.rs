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
    pub offset: Option<usize>,
    pub paged: Option<bool>,
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
            let qualities = n_core::quality::store::display(&ctx.db, &active).await;
            let decorated: Vec<_> = active
                .iter()
                .map(|event| {
                    let mut value = serde_json::to_value(event).unwrap_or_default();
                    value["quality"] =
                        serde_json::to_value(qualities.get(&event.id)).unwrap_or_default();
                    value
                })
                .collect();
            Ok(Json(serde_json::to_value(decorated).unwrap_or_default()))
        }
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("获取活跃形态失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

pub async fn get_active_single_bars(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
) -> Result<Json<serde_json::Value>, (StatusCode, Json<ApiErrorResponse>)> {
    match ctx.services.get_active_single_bars().await {
        Ok(bars) => Ok(Json(serde_json::to_value(bars).unwrap_or_default())),
        Err(e) => {
            let err =
                ApiErrorResponse::new("INTERNAL_ERROR", format!("获取活跃单K形态失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

pub async fn get_preclose_signals(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
) -> Result<Json<serde_json::Value>, (StatusCode, Json<ApiErrorResponse>)> {
    let cutoff = n_core::storage::repo::preclose_retention_cutoff_ts();
    let _ = n_core::storage::repo::prune_preclose_signals(&ctx.db, &cutoff).await;
    match n_core::storage::repo::recent_preclose_signals(&ctx.db, &cutoff).await {
        Ok(rows) => Ok(Json(serde_json::to_value(rows).unwrap_or_default())),
        Err(e) => {
            let err =
                ApiErrorResponse::new("INTERNAL_ERROR", format!("获取收盘前预警失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

pub async fn get_preclose_candidates(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
) -> Result<Json<serde_json::Value>, (StatusCode, Json<ApiErrorResponse>)> {
    let cutoff = n_core::storage::repo::preclose_retention_cutoff_ts();
    let _ = n_core::storage::repo::prune_preclose_candidates(&ctx.db, &cutoff).await;
    match n_core::storage::repo::recent_preclose_candidates(&ctx.db, &cutoff).await {
        Ok(rows) => Ok(Json(serde_json::to_value(rows).unwrap_or_default())),
        Err(e) => {
            let err =
                ApiErrorResponse::new("INTERNAL_ERROR", format!("获取临时未收盘扫描失败: {e}"), "");
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
    Query(filters): Query<n_protocol::dto::RecentOutcomeFilters>,
) -> Result<Json<serde_json::Value>, (StatusCode, Json<ApiErrorResponse>)> {
    let result = if q.paged.unwrap_or(false) {
        ctx.services.review_page(q.limit.unwrap_or(80), q.offset.unwrap_or(0), &filters).await
            .and_then(|page| Ok(serde_json::to_value(page)?))
    } else {
        ctx.services.recent_outcomes(q.limit.unwrap_or(2000), &filters).await
            .and_then(|rows| Ok(serde_json::to_value(rows)?))
    };
    match result {
        Ok(value) => Ok(Json(value)),
        Err(e) => Err((StatusCode::INTERNAL_SERVER_ERROR, Json(ApiErrorResponse::new(
            "REVIEW_QUERY_FAILED", format!("获取复盘明细失败: {e}"), "")))),
    }
}

pub async fn get_review_signal(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
    Path(event_id): Path<i64>,
) -> Result<Json<serde_json::Value>, (StatusCode, Json<ApiErrorResponse>)> {
    match ctx.services.review_signal(event_id).await {
        Ok(s) => Ok(Json(
            serde_json::to_value(s).unwrap_or(serde_json::Value::Null),
        )),
        Err(e) => {
            let err =
                ApiErrorResponse::new("INTERNAL_ERROR", format!("获取复盘信号详情失败: {e}"), "");
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
            let rev = n_core::storage::repo::bump_revision(&ctx.db, "signals")
                .await
                .unwrap_or(1);
            let seq = ctx.next_event_seq();
            ctx.realtime_hub
                .broadcast(
                    seq,
                    "state.changed",
                    serde_json::json!({ "scope": "signals", "revision": rev }),
                )
                .await;
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
        "SELECT model_id, name, schema_version, feature_whitelist, train_window, dataset_hash, coefficients, spline_knots, metrics, created_at, status, scoring_slot FROM v2_model_registry WHERE name='logistic-v3' AND schema_version='3' AND status IN ('challenger','champion') ORDER BY created_at DESC".to_string()
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
                let metrics: serde_json::Value =
                    serde_json::from_str(&row.try_get::<String>("", "metrics").unwrap_or_default())
                        .unwrap_or_default();
                out.push(V2ModelRow {
                    model_id,
                    name,
                    status: status.clone(),
                    is_champion: status == "champion"
                        && n_core::v2::prediction::eligible(
                            &serde_json::from_str::<serde_json::Value>(
                                &row.try_get::<String>("", "metrics").unwrap_or_default(),
                            )
                            .unwrap_or_default(),
                        ),
                    accuracy: metrics["classification"]["accuracy"].as_f64().unwrap_or(0.),
                    precision: metrics["classification"]["precision"]
                        .as_f64()
                        .unwrap_or(0.),
                    recall: metrics["classification"]["recall"].as_f64().unwrap_or(0.),
                    f1: metrics["classification"]["f1"].as_f64().unwrap_or(0.),
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
    let rows=ctx.db.query_all(Statement::from_string(DbBackend::Sqlite,"SELECT model_id,metrics FROM v2_model_registry WHERE name='logistic-v3' AND schema_version='3' AND status='champion'".to_string())).await.map_err(|e|(StatusCode::INTERNAL_SERVER_ERROR,Json(ApiErrorResponse::new("INTERNAL_ERROR",e.to_string(),""))))?;
    let mut output = Vec::new();
    for row in rows {
        let model_id: String = row.try_get("", "model_id").unwrap_or_default();
        if q.model_id.as_ref().is_some_and(|id| id != &model_id) {
            continue;
        }
        let metrics: serde_json::Value =
            serde_json::from_str(&row.try_get::<String>("", "metrics").unwrap_or_default())
                .unwrap_or_default();
        if !n_core::v2::prediction::eligible(&metrics) {
            continue;
        }
        let predictions=ctx.db.query_all(Statement::from_sql_and_values(DbBackend::Sqlite,"SELECT event_id,model_id,p_win,predicted_at FROM prediction_history WHERE model_id=? AND origin='prospective' ORDER BY id DESC LIMIT 2000",vec![model_id.into()])).await.map_err(|e|(StatusCode::INTERNAL_SERVER_ERROR,Json(ApiErrorResponse::new("INTERNAL_ERROR",e.to_string(),""))))?;
        for p in predictions {
            let probability: f64 = p.try_get("", "p_win").unwrap_or(0.);
            output.push(V2PredictionRow {
                event_id: p.try_get("", "event_id").unwrap_or(0),
                model_id: p.try_get("", "model_id").unwrap_or_default(),
                p_win: probability,
                pred_label: i64::from(probability >= 0.5),
                created_at: p.try_get("", "predicted_at").unwrap_or_default(),
            });
        }
    }
    Ok(Json(output))
}

pub async fn get_v2_report(
    State(_ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
) -> Result<Json<V2ReportBundle>, (StatusCode, Json<ApiErrorResponse>)> {
    let row=_ctx.db.query_one(Statement::from_string(DbBackend::Sqlite,"SELECT report_json FROM analysis_runs WHERE status='completed' ORDER BY created_at DESC LIMIT 1".to_string())).await.ok().flatten();
    let report = row
        .and_then(|r| r.try_get::<String>("", "report_json").ok())
        .and_then(|s| serde_json::from_str::<serde_json::Value>(&s).ok())
        .unwrap_or_else(
            || serde_json::json!({"display_eligible":false,"message":"暂无经验证概率"}),
        );
    Ok(Json(V2ReportBundle { summary: report }))
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
    match n_core::storage::repo::set_v2_model_status(
        &ctx.db,
        &payload.model_id,
        &payload.status,
        "default",
    )
    .await
    {
        Ok(_) => Ok(StatusCode::OK),
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("更新模型状态失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

#[cfg(test)]
mod review_query_tests {
    use super::*;
    #[test]
    fn numeric_scores_dates_and_multiple_products_roundtrip_in_query() {
        let uri = "/review/outcomes?limit=40&offset=80&paged=true&quality_metric=quality_v2&quality_min=70.5&quality_max=90&symbols=BU0%2CSA0&time_field=trigger&time_start=2026-10-01%2009%3A00%3A00&hour_start=21%3A00&hour_end=02%3A30&sort_by=score_desc".parse().unwrap();
        let Query(page) = Query::<RecentOutcomesQuery>::try_from_uri(&uri).unwrap();
        let Query(filters) = Query::<n_protocol::dto::RecentOutcomeFilters>::try_from_uri(&uri).unwrap();
        assert_eq!((page.limit,page.offset,page.paged),(Some(40),Some(80),Some(true)));
        assert_eq!(filters.quality_min,Some(70.5));
        assert_eq!(filters.symbols.as_deref(),Some("BU0,SA0"));
        assert_eq!(filters.time_start.as_deref(),Some("2026-10-01 09:00:00"));
        assert_eq!(filters.hour_end.as_deref(),Some("02:30"));
        assert_eq!(filters.sort_by.as_deref(),Some("score_desc"));
    }
}
