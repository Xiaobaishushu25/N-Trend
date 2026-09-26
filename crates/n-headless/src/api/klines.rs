use crate::auth::AuthDevice;
use crate::state::ServerContext;
use axum::extract::{Path, Query, State};
use axum::http::StatusCode;
use axum::Json;
use n_protocol::dto::{ChartKlineResponse, KlineDto, TrendPointDto};
use n_protocol::error::ApiErrorResponse;
use serde::Deserialize;
use std::sync::Arc;

#[derive(Deserialize)]
pub struct KlineQuery {
    pub limit: Option<usize>,
    #[allow(dead_code)]
    pub before: Option<i64>,
}

pub async fn get_chart_klines(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
    Path((symbol, timeframe)): Path<(String, String)>,
    Query(q): Query<KlineQuery>,
) -> Result<Json<ChartKlineResponse>, (StatusCode, Json<ApiErrorResponse>)> {
    let limit = q.limit.unwrap_or(1200).min(5000);
    match ctx.services.get_chart_klines(&symbol, &timeframe, Some(limit)).await {
        Ok(res) => {
            let rows: Vec<KlineDto> = res
                .rows
                .into_iter()
                .map(|k| KlineDto {
                    symbol: k.symbol,
                    timeframe: k.timeframe,
                    ts: k.ts,
                    open: k.open,
                    high: k.high,
                    low: k.low,
                    close: k.close,
                    volume: k.volume,
                    hold: k.hold,
                    source: k.source,
                    rollover: k.rollover,
                })
                .collect();

            let latest_closed = rows.last().and_then(|r| {
                chrono::NaiveDateTime::parse_from_str(&r.ts, "%Y-%m-%d %H:%M:%S")
                    .ok()
                    .and_then(|dt| dt.and_local_timezone(chrono::Local).single())
                    .map(|dt| dt.timestamp_millis())
            });

            let next_before = rows.first().and_then(|r| {
                chrono::NaiveDateTime::parse_from_str(&r.ts, "%Y-%m-%d %H:%M:%S")
                    .ok()
                    .and_then(|dt| dt.and_local_timezone(chrono::Local).single())
                    .map(|dt| dt.timestamp_millis())
            });

            let rev = n_core::storage::repo::get_revision(&ctx.db, &format!("kline:{symbol}:{timeframe}"))
                .await
                .unwrap_or(1);

            Ok(Json(ChartKlineResponse {
                rows,
                status: res.status,
                message: res.message,
                latest_closed_ts: latest_closed,
                series_revision: Some(rev),
                server_time: Some(chrono::Local::now().timestamp_millis()),
                has_more: Some(true),
                next_before,
                full_reload_required: Some(false),
            }))
        }
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("获取K线失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

pub async fn get_trend_series(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
    Path((symbol, timeframe)): Path<(String, String)>,
    Query(q): Query<KlineQuery>,
) -> Result<Json<Vec<TrendPointDto>>, (StatusCode, Json<ApiErrorResponse>)> {
    let limit = q.limit.unwrap_or(1200).min(5000);
    match ctx.services.trend_series(&symbol, &timeframe, Some(limit)).await {
        Ok(series) => {
            let dtos = series
                .into_iter()
                .map(|p| TrendPointDto {
                    ts: p.ts,
                    value: p.value,
                    direction: p.direction,
                })
                .collect();
            Ok(Json(dtos))
        }
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("获取趋势线失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}

pub async fn get_klines(
    State(ctx): State<Arc<ServerContext>>,
    device: AuthDevice,
    path: Path<(String, String)>,
    query: Query<KlineQuery>,
) -> Result<Json<Vec<KlineDto>>, (StatusCode, Json<ApiErrorResponse>)> {
    let res = get_chart_klines(State(ctx), device, path, query).await?;
    Ok(Json(res.0.rows))
}
