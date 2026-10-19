use crate::auth::AuthDevice;
use crate::state::ServerContext;
use axum::extract::{Path, Query, State};
use axum::http::{HeaderMap, StatusCode};
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
    headers: HeaderMap,
) -> Result<Json<ChartKlineResponse>, (StatusCode, Json<ApiErrorResponse>)> {
    let limit = q.limit.unwrap_or(1200).min(5000);
    let started = std::time::Instant::now();
    let trace_id = headers.get("X-Ntrend-Trace-Id").and_then(|v| v.to_str().ok())
        .filter(|v| v.len() <= 80 && v.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'-'))
        .unwrap_or("untraced");
    tracing::info!(trace_id, %symbol, %timeframe, limit, "K线服务端请求开始");
    let result = ctx.services.get_chart_klines(&symbol, &timeframe, Some(limit), trace_id).await;
    tracing::info!(trace_id, %symbol, %timeframe, elapsed_ms = started.elapsed().as_millis(), success = result.is_ok(), "K线服务端取数完成");
    if started.elapsed() >= std::time::Duration::from_millis(500) {
        tracing::warn!(trace_id, symbol, timeframe, limit, elapsed_ms = started.elapsed().as_millis(), success = result.is_ok(), "图表服务端取数较慢");
    }
    match result {
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
            tracing::info!(trace_id, rows = rows.len(), elapsed_ms = started.elapsed().as_millis(), "K线服务端修订号查询完成");

            let partial_bar = res.partial_bar.map(|p| KlineDto {
                symbol: p.symbol,
                timeframe: p.timeframe,
                ts: p.ts,
                open: p.open,
                high: p.high,
                low: p.low,
                close: p.close,
                volume: p.volume,
                hold: p.hold,
                source: p.source,
                rollover: p.rollover,
            });

            tracing::info!(trace_id, rows = rows.len(), total_ms = started.elapsed().as_millis(), "K线服务端响应组装完成");
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
                partial_bar,
            }))
        }
        Err(e) => {
            tracing::error!(trace_id, elapsed_ms = started.elapsed().as_millis(), error = %e, "K线服务端请求失败");
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
    let res = get_chart_klines(State(ctx), device, path, query, HeaderMap::new()).await?;
    Ok(Json(res.0.rows))
}

pub async fn get_intraday_chart(
    State(ctx): State<Arc<ServerContext>>,
    _device: AuthDevice,
    Path(symbol): Path<String>,
) -> Result<Json<n_protocol::dto::IntradayChartResponse>, (StatusCode, Json<ApiErrorResponse>)> {
    match ctx.services.get_intraday_chart(&symbol).await {
        Ok(res) => Ok(Json(res)),
        Err(e) => {
            let err = ApiErrorResponse::new("INTERNAL_ERROR", format!("获取分时数据失败: {e}"), "");
            Err((StatusCode::INTERNAL_SERVER_ERROR, Json(err)))
        }
    }
}
