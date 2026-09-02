//! TianQin (TqSdk) Local HTTP Bridge Client.

use std::collections::HashMap;
use std::time::Duration;

use anyhow::{bail, Context, Result};
use reqwest::Client;
use serde::{Deserialize, Serialize};

use crate::fetch::datasource::MarketDataSource;
use crate::fetch::kline::{Kline, RawKlineResponse};
use crate::fetch::quotes::Quote;
use crate::fetch::symbols::FuturesSymbol;

#[derive(Debug, Clone, Deserialize)]
struct HealthResponse {
    status: Option<String>,
    tq_connected: Option<bool>,
    worker_alive: Option<bool>,
    stale: Option<bool>,
    stream_id: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
struct QuotesResponse {
    quotes: HashMap<String, QuoteDto>,
}

#[derive(Debug, Clone, Deserialize)]
struct QuoteDto {
    code: String,
    name: String,
    latest: f64,
    prev_settle: f64,
    change_pct: Option<f64>,
}

#[derive(Debug, Clone, Deserialize)]
#[allow(dead_code)]
struct KlineResponse {
    symbol: Option<String>,
    period: Option<String>,
    klines: Vec<KlineDto>,
    includes_current: Option<bool>,
}

#[derive(Debug, Clone, Deserialize)]
struct KlineDto {
    datetime: String,
    open: f64,
    high: f64,
    low: f64,
    close: f64,
    volume: f64,
    hold: f64,
}

#[derive(Debug, Clone, Deserialize)]
struct SearchResponse {
    results: Vec<FuturesSymbol>,
}

#[derive(Debug, Clone, Serialize)]
struct QuotesRequest<'a> {
    symbols: &'a [String],
}

#[derive(Debug, Clone, Serialize)]
struct SubscribeKlinesRequest<'a> {
    symbols: &'a [String],
    period: &'a str,
    data_length: usize,
}

#[derive(Debug, Clone, Deserialize)]
pub struct SubscribeKlinesResponse {
    pub stream_id: String,
    pub subscribed: Vec<String>,
    #[serde(default)]
    pub failed: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum BarCloseProof {
    NextBarStarted,
    SessionNotTrading,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct ClosedBarEvent {
    pub event_id: u64,
    pub event_type: String,
    pub source: String,
    pub proof: BarCloseProof,
    pub symbol: String,
    pub tq_symbol: String,
    pub period: String,
    pub bar_end: String,
    pub next_bar_start: Option<String>,
    pub kline: Kline,
    pub market_event_time: String,
    pub emitted_at: String,
    pub event_lag_ms: u64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ClosedBarEventsResponse {
    pub stream_id: String,
    pub oldest_event_id: u64,
    pub latest_event_id: u64,
    pub events: Vec<ClosedBarEvent>,
}

#[derive(Clone)]
pub struct TqBridgeClient {
    base_url: String,
    http: Client,
}

impl TqBridgeClient {
    pub fn new(base_url: &str) -> Self {
        let http = Client::builder()
            .timeout(Duration::from_secs(40))
            .connect_timeout(Duration::from_millis(1500))
            .build()
            .unwrap_or_default();
        Self {
            base_url: base_url.trim_end_matches('/').to_string(),
            http,
        }
    }

    pub fn with_port(port: u16) -> Self {
        Self::new(&format!("http://127.0.0.1:{port}"))
    }

    pub async fn subscribe_klines(
        &self,
        symbols: &[String],
        period: &str,
        data_length: usize,
    ) -> Result<SubscribeKlinesResponse> {
        // 冷启动 22 品种首次创建序列需远超 12s，按品种数自适应超时，避免 BridgeCommandTimeout 误判。
        let adaptive_timeout = (15 + symbols.len() as u64).clamp(18, 35);
        let url = format!("{}/api/subscribe-klines", self.base_url);
        let response = self
            .http
            .post(url)
            .json(&SubscribeKlinesRequest {
                symbols,
                period,
                data_length,
            })
            .timeout(Duration::from_secs(adaptive_timeout))
            .send()
            .await
            .context("批量订阅天勤 K 线失败")?;
        if !response.status().is_success() {
            let status = response.status();
            let body = response.text().await.unwrap_or_default();
            let snippet: String = body.chars().take(800).collect();
            bail!(
                "批量订阅天勤 K 线返回 HTTP 错误: {} body: {}",
                status,
                snippet
            );
        }
        response.json().await.context("解析天勤订阅响应失败")
    }

    pub async fn poll_closed_bar_events(
        &self,
        after_id: u64,
        timeout_secs: u64,
    ) -> Result<ClosedBarEventsResponse> {
        let timeout_secs = timeout_secs.min(25);
        let url = format!(
            "{}/api/events?after_id={after_id}&timeout={timeout_secs}",
            self.base_url
        );
        let response = self
            .http
            .get(url)
            .timeout(Duration::from_secs(timeout_secs + 3))
            .send()
            .await
            .context("轮询天勤闭合 K 线事件失败")?;
        if !response.status().is_success() {
            let status = response.status();
            let body = response.text().await.unwrap_or_default();
            let snippet: String = body.chars().take(800).collect();
            bail!(
                "天勤闭合事件接口返回 HTTP 错误: {} body: {}",
                status,
                snippet
            );
        }
        response.json().await.context("解析天勤闭合事件失败")
    }

    pub async fn health_stream_id(&self) -> Option<String> {
        let url = format!("{}/health", self.base_url);
        let response = self.http.get(url).timeout(Duration::from_millis(1500)).send().await.ok()?;
        let health = response.json::<HealthResponse>().await.ok()?;
        if health.status.as_deref() == Some("ok")
            && health.tq_connected.unwrap_or(false)
            && health.worker_alive.unwrap_or(false)
            && !health.stale.unwrap_or(false)
        {
            health.stream_id
        } else {
            None
        }
    }
}

fn exclude_current_kline(mut rows: Vec<Kline>, count: usize) -> Vec<Kline> {
    rows.pop();
    if rows.len() > count {
        rows.drain(0..rows.len() - count);
    }
    rows
}

impl MarketDataSource for TqBridgeClient {
    fn name(&self) -> &'static str {
        "tqsdk"
    }

    async fn fetch_quotes(&self, codes: &[String]) -> Result<HashMap<String, Quote>> {
        if codes.is_empty() {
            return Ok(HashMap::new());
        }
        let url = format!("{}/api/quotes", self.base_url);
        let req_body = QuotesRequest { symbols: codes };
        let resp = self
            .http
            .post(&url)
            .json(&req_body)
            .send()
            .await
            .context("天勤桥接服务实时行情请求失败")?;

        if !resp.status().is_success() {
            let status = resp.status();
            let body = resp.text().await.unwrap_or_default();
            let snippet: String = body.chars().take(800).collect();
            bail!("天勤桥接服务返回 HTTP 错误: {} body: {}", status, snippet);
        }

        let body: QuotesResponse = resp
            .json()
            .await
            .context("天勤桥接服务实时行情 JSON 解析失败")?;

        let mut out = HashMap::with_capacity(body.quotes.len());
        for (code, dto) in body.quotes {
            out.insert(
                code.clone(),
                Quote {
                    code: dto.code,
                    name: dto.name,
                    latest: dto.latest,
                    prev_settle: dto.prev_settle,
                    change_pct: dto.change_pct,
                },
            );
        }
        Ok(out)
    }

    async fn fetch_minute(
        &self,
        symbol: &str,
        period: &str,
        count: usize,
    ) -> Result<Vec<Kline>> {
        // TqSdk 序列最后一行始终是进行中 K 线。没有闭合事件证明时生产接口
        // 必须无条件排除它；休市最后一根由 ClosedBarEvent 单独入库。
        let raw = self.fetch_minute_raw(symbol, period, count.saturating_add(1)).await?;
        Ok(exclude_current_kline(raw.klines, count))
    }

    async fn fetch_minute_raw(
        &self,
        symbol: &str,
        period: &str,
        count: usize,
    ) -> Result<RawKlineResponse> {
        let url = format!(
            "{}/api/kline?symbol={}&period={}&count={}",
            self.base_url, symbol, period, count
        );
        let resp = self
            .http
            .get(&url)
            .send()
            .await
            .context("天勤桥接服务 K 线请求失败")?;

        if !resp.status().is_success() {
            let status = resp.status();
            let body = resp.text().await.unwrap_or_default();
            let snippet: String = body.chars().take(800).collect();
            bail!("天勤桥接服务返回 HTTP 错误: {} body: {}", status, snippet);
        }

        let raw_text = resp.text().await.context("读取天勤 K 线响应文本失败")?;
        let body: KlineResponse =
            serde_json::from_str(&raw_text).context("天勤 K 线 JSON 解析失败")?;

        if body.klines.is_empty() {
            bail!("天勤接口未返回品种 {} 的 K 线数据", symbol);
        }

        let klines = body
            .klines
            .into_iter()
            .map(|dto| Kline {
                datetime: dto.datetime,
                open: dto.open,
                high: dto.high,
                low: dto.low,
                close: dto.close,
                volume: dto.volume,
                hold: dto.hold,
            })
            .collect();

        Ok(RawKlineResponse { klines, raw_text })
    }

    async fn search_contracts(&self, keyword: &str) -> Result<Vec<FuturesSymbol>> {
        let url = format!("{}/api/search?keyword={}", self.base_url, keyword);
        let resp = self
            .http
            .get(&url)
            .send()
            .await
            .context("天勤桥接服务合约搜索失败")?;

        if !resp.status().is_success() {
            return Ok(Vec::new());
        }

        let body: SearchResponse = resp.json().await.unwrap_or(SearchResponse {
            results: Vec::new(),
        });
        Ok(body.results)
    }

    async fn is_healthy(&self) -> bool {
        let url = format!("{}/health", self.base_url);
        match self.http.get(&url).timeout(Duration::from_millis(1500)).send().await {
            Ok(resp) => {
                if let Ok(health) = resp.json::<HealthResponse>().await {
                    health.status.as_deref() == Some("ok")
                        && health.tq_connected.unwrap_or(false)
                        && health.worker_alive.unwrap_or(false)
                        && !health.stale.unwrap_or(false)
                } else {
                    false
                }
            }
            Err(_) => false,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn bar(ts: &str, close: f64) -> Kline {
        Kline {
            datetime: ts.to_string(),
            open: close,
            high: close,
            low: close,
            close,
            volume: 1.0,
            hold: 1.0,
        }
    }

    #[test]
    fn production_rows_always_exclude_tq_current_tail() {
        let rows = vec![
            bar("2026-09-02 10:00:00", 1.0),
            bar("2026-09-02 10:05:00", 2.0),
            bar("2026-09-02 10:10:00", 3.0),
        ];
        let closed = exclude_current_kline(rows, 10);
        assert_eq!(closed.len(), 2);
        assert_eq!(closed.last().unwrap().datetime, "2026-09-02 10:05:00");
    }
}
