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
    reconnecting: Option<bool>,
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

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TradingCalendarDay {
    pub date: String,
    pub trading: bool,
}

#[derive(Debug, Clone, Deserialize)]
struct TradingCalendarResponse {
    calendar: Vec<TradingCalendarDay>,
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
    SessionSettled,
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
    pub fn decode_execution_quote(
        code: &str,
        q: &serde_json::Value,
    ) -> crate::trader::model::ExecutionQuote {
        use chrono::TimeZone;
        let date = chrono::NaiveDateTime::parse_from_str(
            q["datetime"].as_str().unwrap_or(""),
            "%Y-%m-%d %H:%M:%S%.f",
        );
        let timestamp = date
            .ok()
            .and_then(|d| {
                chrono::FixedOffset::east_opt(8 * 3600)?
                    .from_local_datetime(&d)
                    .single()
            })
            .map(|d| d.timestamp())
            .unwrap_or(0);
        let number = |key: &str| q[key].as_f64().unwrap_or(0.0);
        crate::trader::model::ExecutionQuote {
            instrument_id: q["instrument_id"].as_str().unwrap_or(code).into(),
            exchange_id: q["exchange_id"].as_str().unwrap_or("").into(),
            underlying_symbol: q["underlying_symbol"].as_str().unwrap_or("").into(),
            last: number("latest"),
            bid: number("bid"),
            ask: number("ask"),
            tick: number("price_tick"),
            lower: number("lower_limit"),
            upper: number("upper_limit"),
            timestamp,
        }
    }

    pub async fn execution_events(
        &self,
        symbols: &[String],
        after: u64,
        stream_id: &str,
    ) -> Result<serde_json::Value> {
        Ok(self
            .http
            .post(format!("{}/api/execution-events", self.base_url))
            .timeout(Duration::from_secs(30))
            .json(&serde_json::json!({
                "symbols": symbols, "after_id": after, "stream_id": stream_id, "wait_seconds": 20
            }))
            .send()
            .await?
            .error_for_status()?
            .json()
            .await?)
    }

    /// Rich actual-contract quotes for execution. Do not use the Sina fallback,
    /// which does not provide the contract tick and daily price limits here.
    pub async fn execution_quotes(
        &self,
        codes: &[String],
    ) -> Result<HashMap<String, crate::trader::model::ExecutionQuote>> {
        if codes.is_empty() {
            return Ok(HashMap::new());
        }
        let body: serde_json::Value = self
            .http
            .post(format!("{}/api/quotes", self.base_url))
            .timeout(Duration::from_secs(5))
            .json(&QuotesRequest { symbols: codes })
            .send()
            .await?
            .error_for_status()?
            .json()
            .await?;
        Ok(body["quotes"]
            .as_object()
            .into_iter()
            .flatten()
            .map(|(code, q)| (code.to_uppercase(), Self::decode_execution_quote(code, q)))
            .collect())
    }

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
        let response = self
            .http
            .get(url)
            .timeout(Duration::from_millis(1500))
            .send()
            .await
            .ok()?;
        let health = response.json::<HealthResponse>().await.ok()?;
        if health.status.as_deref() == Some("ok")
            && health.tq_connected.unwrap_or(false)
            && health.worker_alive.unwrap_or(false)
            && !health.reconnecting.unwrap_or(false)
            && !health.stale.unwrap_or(false)
        {
            health.stream_id
        } else {
            None
        }
    }

    pub async fn fetch_trading_calendar(
        &self,
        start: Option<&str>,
        end: Option<&str>,
    ) -> Result<Vec<TradingCalendarDay>> {
        let mut url = format!("{}/api/trading-calendar", self.base_url);
        let mut query = Vec::new();
        if let Some(s) = start {
            query.push(format!("start={s}"));
        }
        if let Some(e) = end {
            query.push(format!("end={e}"));
        }
        if !query.is_empty() {
            url = format!("{}?{}", url, query.join("&"));
        }
        let response = self
            .http
            .get(url)
            .timeout(Duration::from_secs(10))
            .send()
            .await
            .context("请求天勤交易日历接口失败")?;
        if !response.status().is_success() {
            bail!("天勤交易日历接口返回异常: {}", response.status());
        }
        let data: TradingCalendarResponse =
            response.json().await.context("解析天勤交易日历失败")?;
        Ok(data.calendar)
    }
}

fn finalized_klines(
    symbol: &str,
    period: &str,
    mut rows: Vec<Kline>,
    count: usize,
    now: chrono::NaiveDateTime,
) -> Vec<Kline> {
    // A session's last 5m bar remains the SDK tail throughout the break.
    // Event delivery is the fast path; a fresh TQ fetch after the close settlement
    // period must also recover that bar after a missed event or a restart.
    let settled_session_tail = matches!(period, "5" | "5m")
        && rows.last().is_some_and(|row| {
            use chrono::Timelike;
            chrono::NaiveDateTime::parse_from_str(&row.datetime, "%Y-%m-%d %H:%M:%S")
                .ok()
                .is_some_and(|end| {
                    crate::session::SessionCalendar::is_session_close(
                        symbol,
                        end.hour(),
                        end.minute(),
                    ) && crate::finality::FinalityJudger::default().is_bar_final(symbol, &end, &now)
                })
        });
    if !settled_session_tail {
        rows.pop();
    }
    if rows.len() > count {
        rows.drain(0..rows.len() - count);
    }
    rows
}

fn trace_kline_response(
    symbol: &str,
    period: &str,
    requested: usize,
    raw_text_len: usize,
    klines: &[KlineDto],
) {
    tracing::info!(
        target: "market_trace",
        layer = "rust_tq_client",
        symbol,
        period,
        requested,
        rows = klines.len(),
        raw_text_len,
        first = %klines.first().map(|k| k.datetime.as_str()).unwrap_or("-"),
        last = %klines.last().map(|k| k.datetime.as_str()).unwrap_or("-"),
        "KLINE_TRACE"
    );

    if period != "5m" && period != "5" {
        return;
    }

    for pair in klines.windows(2) {
        let previous = &pair[0];
        let current = &pair[1];
        let price_gap_pct = (current.open - previous.close).abs() / previous.close.abs().max(1.0);
        let hold_change_pct = (current.hold - previous.hold).abs() / previous.hold.abs().max(1.0);
        if price_gap_pct >= 0.05 || hold_change_pct >= 0.20 {
            tracing::info!(
                target: "market_trace",
                layer = "rust_tq_client",
                symbol,
                period,
                prev_ts = %previous.datetime,
                prev_close = previous.close,
                prev_hold = previous.hold,
                ts = %current.datetime,
                open = current.open,
                close = current.close,
                hold = current.hold,
                price_gap_pct,
                hold_change_pct,
                "KLINE_ANOMALY"
            );
        }
    }
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

    async fn fetch_minute(&self, symbol: &str, period: &str, count: usize) -> Result<Vec<Kline>> {
        // 盘中排除未确认的末根；会话收盘末根在结算期后允许补拉恢复。
        let raw = self
            .fetch_minute_raw(symbol, period, count.saturating_add(1))
            .await?;
        Ok(finalized_klines(
            symbol,
            period,
            raw.klines,
            count,
            chrono::Local::now().naive_local(),
        ))
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

        trace_kline_response(symbol, period, count, raw_text.len(), &body.klines);

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
        match self
            .http
            .get(&url)
            .timeout(Duration::from_millis(1500))
            .send()
            .await
        {
            Ok(resp) => {
                if let Ok(health) = resp.json::<HealthResponse>().await {
                    health.status.as_deref() == Some("ok")
                        && health.tq_connected.unwrap_or(false)
                        && health.worker_alive.unwrap_or(false)
                        && !health.reconnecting.unwrap_or(false)
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
        let now = chrono::NaiveDateTime::parse_from_str("2026-09-02 10:10:20", "%Y-%m-%d %H:%M:%S")
            .unwrap();
        let closed = finalized_klines("RB0", "5m", rows, 10, now);
        assert_eq!(closed.len(), 2);
        assert_eq!(closed.last().unwrap().datetime, "2026-09-02 10:05:00");
    }

    #[test]
    fn session_tail_recovers_after_settlement_without_a_next_bar() {
        let dt = |s| chrono::NaiveDateTime::parse_from_str(s, "%Y-%m-%d %H:%M:%S").unwrap();
        for end in [
            "2026-10-30 10:15:00",
            "2026-10-30 11:30:00",
            "2026-10-30 15:00:00",
            "2026-10-30 23:00:00",
        ] {
            let rows = vec![bar(end, 100.0)];
            assert!(finalized_klines(
                "RB0",
                "5m",
                rows.clone(),
                10,
                dt(end) + chrono::Duration::seconds(74)
            )
            .is_empty());
            assert_eq!(
                finalized_klines(
                    "RB0",
                    "5m",
                    rows,
                    10,
                    dt(end) + chrono::Duration::seconds(75)
                )
                .len(),
                1
            );
        }
        // A stalled ordinary intraday tail is never finalized just because it is old.
        assert!(finalized_klines(
            "RB0",
            "5m",
            vec![bar("2026-10-30 11:25:00", 1.0)],
            10,
            dt("2026-10-30 11:52:00")
        )
        .is_empty());
        assert!(finalized_klines(
            "RB0",
            "15m",
            vec![bar("2026-10-30 11:30:00", 1.0)],
            10,
            dt("2026-10-30 11:52:00")
        )
        .is_empty());
        assert!(finalized_klines(
            "RB0",
            "5m",
            vec![bar("invalid", 1.0)],
            10,
            dt("2026-10-30 11:52:00")
        )
        .is_empty());
        assert!(finalized_klines(
            "RB0",
            "5m",
            vec![bar("2026-10-30 11:30:00", 1.0)],
            0,
            dt("2026-10-30 11:52:00")
        )
        .is_empty());
    }
}
