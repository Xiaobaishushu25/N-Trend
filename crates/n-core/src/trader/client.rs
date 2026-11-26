use anyhow::{Context, Result};
use reqwest::Client;
use serde::{Deserialize, Serialize};
use std::time::Duration;

use super::model::*;

#[derive(Clone, Debug)]
pub struct CtpTraderClient {
    base_url: String,
    expected_identity: std::sync::Arc<std::sync::RwLock<Option<(String, String)>>>,
    http: Client,
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};
    use std::net::TcpListener;

    #[tokio::test]
    async fn stream_handles_fragmented_unicode_multiple_frames_and_disconnect() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let worker = std::thread::spawn(move || {
            let (mut socket, _) = listener.accept().unwrap();
            socket
                .set_read_timeout(Some(Duration::from_secs(2)))
                .unwrap();
            let mut request = [0; 4096];
            socket.read(&mut request).unwrap();
            let body = "data: {\"revision\":1,\"message\":\"持仓\"}\n\ndata: {\"revision\":2}\n\n";
            write!(socket, "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\nConnection: close\r\n\r\n", body.len()).unwrap();
            // Include UTF-8 splits and several events on one connection.
            for fragment in body.as_bytes().chunks(2) {
                socket.write_all(fragment).unwrap();
            }
        });
        let client = CtpTraderClient::new(format!("http://{address}"));
        let mut stream = client.event_stream().await.unwrap();
        assert_eq!(stream.next_snapshot().await.unwrap()["message"], "持仓");
        assert_eq!(stream.next_snapshot().await.unwrap()["revision"], 2);
        assert!(stream.next_snapshot().await.is_err());
        worker.join().unwrap();
    }

    #[test]
    fn test_ctp_market_event_deserialization_and_quote_conversion() {
        let json_data = r#"{
            "type": "tick",
            "seq_id": 42,
            "stream_id": "stream_1",
            "instrument_id": "rb2501",
            "exchange_id": "SHFE",
            "last_price": 3500.0,
            "volume": 1000,
            "turnover": 35000000.0,
            "open_interest": 5000.0,
            "bid_price1": 3499.0,
            "bid_volume1": 10,
            "ask_price1": 3501.0,
            "ask_volume1": 20,
            "upper_limit_price": 3800.0,
            "lower_limit_price": 3200.0,
            "price_tick": 1.0,
            "action_day": "20261113",
            "trading_day": "20261113",
            "update_time": "14:30:00",
            "update_millisec": 500,
            "datetime": "2026-11-13T14:30:00.500+08:00",
            "timestamp": 1763015400.5,
            "recv_time": 1763015400.501
        }"#;

        let event: CtpMarketEvent = serde_json::from_str(json_data).unwrap();
        assert_eq!(event.seq_id(), 42);
        assert_eq!(event.stream_id(), "stream_1");

        let quote = event.to_execution_quote().unwrap();
        assert_eq!(quote.instrument_id, "rb2501");
        assert_eq!(quote.exchange_id, "SHFE");
        assert_eq!(quote.underlying_symbol, "rb2501");
        assert_eq!(quote.last, 3500.0);
        assert_eq!(quote.bid, 3499.0);
        assert_eq!(quote.ask, 3501.0);
        assert_eq!(quote.tick, 1.0);
        assert_eq!(quote.lower, 3200.0);
        assert_eq!(quote.upper, 3800.0);
        assert_eq!(quote.timestamp, 1763015400);

        // Valid within 15s age
        assert!(quote.valid_at(1763015405));
        // Stale after 16s age
        assert!(!quote.valid_at(1763015420));
    }
}

fn trim_ascii_bytes(slice: &[u8]) -> &[u8] {
    let mut start = 0;
    while start < slice.len() && slice[start].is_ascii_whitespace() {
        start += 1;
    }
    let mut end = slice.len();
    while end > start && slice[end - 1].is_ascii_whitespace() {
        end -= 1;
    }
    &slice[start..end]
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "type")]
pub enum CtpMarketEvent {
    #[serde(rename = "tick")]
    Tick {
        seq_id: u64,
        stream_id: String,
        instrument_id: String,
        exchange_id: String,
        last_price: f64,
        volume: u64,
        turnover: f64,
        open_interest: f64,
        bid_price1: f64,
        bid_volume1: u64,
        ask_price1: f64,
        ask_volume1: u64,
        upper_limit_price: f64,
        lower_limit_price: f64,
        price_tick: f64,
        action_day: String,
        trading_day: String,
        update_time: String,
        update_millisec: u32,
        datetime: String,
        timestamp: f64,
        recv_time: f64,
    },
    #[serde(rename = "md_status")]
    Status {
        seq_id: u64,
        stream_id: String,
        status: String,
        #[serde(default)]
        reason: Option<i32>,
        #[serde(default)]
        error_id: Option<i32>,
        #[serde(default)]
        error_msg: Option<String>,
        #[serde(default)]
        trading_day: Option<String>,
        #[serde(default)]
        timestamp: Option<f64>,
    },
    #[serde(rename = "gap")]
    Gap {
        stream_id: String,
        reason: String,
        #[serde(default)]
        oldest_seq: Option<u64>,
    },
}

impl CtpMarketEvent {
    pub fn seq_id(&self) -> u64 {
        match self {
            Self::Tick { seq_id, .. } => *seq_id,
            Self::Status { seq_id, .. } => *seq_id,
            Self::Gap { .. } => 0,
        }
    }

    pub fn stream_id(&self) -> &str {
        match self {
            Self::Tick { stream_id, .. } => stream_id.as_str(),
            Self::Status { stream_id, .. } => stream_id.as_str(),
            Self::Gap { stream_id, .. } => stream_id.as_str(),
        }
    }

    pub fn to_execution_quote(&self) -> Option<ExecutionQuote> {
        match self {
            Self::Tick {
                instrument_id,
                exchange_id,
                last_price,
                bid_price1,
                ask_price1,
                upper_limit_price,
                lower_limit_price,
                price_tick,
                timestamp,
                ..
            } => Some(ExecutionQuote {
                instrument_id: instrument_id.clone(),
                exchange_id: exchange_id.clone(),
                underlying_symbol: instrument_id.clone(),
                last: *last_price,
                bid: *bid_price1,
                ask: *ask_price1,
                tick: *price_tick,
                lower: *lower_limit_price,
                upper: *upper_limit_price,
                timestamp: *timestamp as i64,
            }),
            _ => None,
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct CtpMarketBatch {
    pub stream_id: String,
    pub latest_seq_id: u64,
    pub gap: bool,
    pub events: Vec<CtpMarketEvent>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct CtpMarketHealth {
    pub connected: bool,
    pub logged_in: bool,
    pub stream_id: String,
    pub latest_seq_id: u64,
    pub buffer_size: usize,
    pub subscribed_count: usize,
    pub subscribed: Vec<String>,
    pub last_tick_time: f64,
}

pub struct CtpMarketEventStream {
    response: reqwest::Response,
    buffer: Vec<u8>,
}

impl CtpMarketEventStream {
    pub async fn next_event(&mut self) -> Result<CtpMarketEvent> {
        loop {
            if let Some(end) = self.buffer.iter().position(|&b| b == b'\n') {
                let line: Vec<u8> = self.buffer.drain(..=end).collect();
                let trimmed = trim_ascii_bytes(&line);
                if trimmed.is_empty() || trimmed.starts_with(b":") {
                    continue;
                }
                if let Some(data) = trimmed.strip_prefix(b"data:") {
                    let data = trim_ascii_bytes(data);
                    return serde_json::from_slice(data).context("CTP 行情事件格式错误");
                }
                continue;
            }
            let chunk =
                tokio::time::timeout(Duration::from_secs(30), self.response.chunk()).await??;
            let Some(chunk) = chunk else {
                anyhow::bail!("CTP 行情推送连接已关闭")
            };
            self.buffer.extend_from_slice(&chunk);
            if self.buffer.len() > 4 * 1024 * 1024 {
                anyhow::bail!("CTP 行情推送帧过大");
            }
        }
    }
}

pub struct TraderSnapshotStream {
    response: reqwest::Response,
    buffer: Vec<u8>,
}

impl TraderSnapshotStream {
    pub async fn next_snapshot(&mut self) -> Result<serde_json::Value> {
        loop {
            if let Some(end) = self.buffer.iter().position(|&b| b == b'\n') {
                let line: Vec<u8> = self.buffer.drain(..=end).collect();
                if let Some(data) = line.strip_prefix(b"data: ") {
                    return serde_json::from_slice(data).context("CTP 快照格式错误");
                }
                continue;
            }
            let chunk =
                tokio::time::timeout(Duration::from_secs(40), self.response.chunk()).await??;
            let Some(chunk) = chunk else {
                anyhow::bail!("CTP 快照推送连接关闭")
            };
            self.buffer.extend_from_slice(&chunk);
            if self.buffer.len() > 4 * 1024 * 1024 {
                anyhow::bail!("CTP 推送帧过大");
            }
        }
    }
}

impl CtpTraderClient {
    pub fn new(base_url: String) -> Self {
        let http = Client::builder()
            .timeout(Duration::from_secs(5))
            .build()
            .unwrap_or_else(|_| Client::new());
        Self {
            base_url,
            http,
            expected_identity: std::sync::Arc::new(std::sync::RwLock::new(None)),
        }
    }

    pub fn expect_identity(&self, broker: &str, user: &str) {
        *self.expected_identity.write().unwrap() = Some((broker.into(), user.into()));
    }
    fn mutation(&self, path: &str) -> reqwest::RequestBuilder {
        let mut request = self.http.post(format!("{}{}", self.base_url, path));
        if let Some((broker, user)) = &*self.expected_identity.read().unwrap() {
            request = request
                .header("X-Ntrend-Broker", broker)
                .header("X-Ntrend-User", user);
        }
        request
    }
    pub fn for_port(port: u16) -> Self {
        Self::new(format!("http://127.0.0.1:{port}"))
    }

    /// A separate client has no total-body timeout for the persistent SSE stream.
    /// The consumer applies an idle timeout to each received chunk instead.
    pub async fn event_stream(&self) -> Result<TraderSnapshotStream> {
        let response = Client::builder()
            .connect_timeout(Duration::from_secs(5))
            .build()?
            .get(format!("{}/events", self.base_url))
            .send()
            .await?
            .error_for_status()
            .context("CTP 快照推送连接失败")?;
        Ok(TraderSnapshotStream {
            response,
            buffer: Vec::new(),
        })
    }

    pub async fn snapshot(&self) -> Result<serde_json::Value> {
        Ok(self
            .http
            .get(format!("{}/snapshot", self.base_url))
            .send()
            .await?
            .error_for_status()?
            .json()
            .await?)
    }

    pub async fn health(&self) -> Result<TraderHealth> {
        let url = format!("{}/health", self.base_url);
        let resp = self
            .http
            .get(&url)
            .send()
            .await
            .with_context(|| format!("请求 CTP 健康检查失败: {}", url))?;
        let res = resp.json::<TraderHealth>().await?;
        Ok(res)
    }

    pub async fn account(&self) -> Result<TraderAccount> {
        let url = format!("{}/account", self.base_url);
        let resp = self
            .http
            .get(&url)
            .send()
            .await
            .with_context(|| format!("请求 CTP 账户资金失败: {}", url))?;
        let res = resp.json::<TraderAccount>().await?;
        Ok(res)
    }

    pub async fn settlement(&self, trading_day: &str) -> Result<super::execution::SettlementReply> {
        anyhow::ensure!(self.expected_identity.read().unwrap().as_ref().is_some_and(|(broker, user)|
            !broker.is_empty() && !user.is_empty()), "结算查询缺少柜台账户身份");
        Ok(self.mutation("/settlement/query")
            .timeout(Duration::from_secs(30))
            .json(&serde_json::json!({"trading_day": trading_day}))
            .send().await?
            .error_for_status()?
            .json().await?)
    }

    pub async fn positions(&self) -> Result<Vec<TraderPosition>> {
        let url = format!("{}/positions", self.base_url);
        let resp = self
            .http
            .get(&url)
            .send()
            .await
            .with_context(|| format!("请求 CTP 持仓列表失败: {}", url))?;
        let res = resp.json::<Vec<TraderPosition>>().await?;
        Ok(res)
    }

    pub async fn orders(&self) -> Result<Vec<TraderOrderRecord>> {
        let url = format!("{}/orders", self.base_url);
        let resp = self
            .http
            .get(&url)
            .send()
            .await
            .with_context(|| format!("请求 CTP 订单列表失败: {}", url))?;
        let res = resp.json::<Vec<TraderOrderRecord>>().await?;
        Ok(res)
    }

    pub async fn trades(&self) -> Result<Vec<TraderTradeRecord>> {
        let url = format!("{}/trades", self.base_url);
        let resp = self
            .http
            .get(&url)
            .send()
            .await
            .with_context(|| format!("请求 CTP 成交列表失败: {}", url))?;
        let res = resp.json::<Vec<TraderTradeRecord>>().await?;
        Ok(res)
    }

    pub async fn updates(&self, since_id: u64) -> Result<UpdatesResponse> {
        let url = format!("{}/updates?since={}", self.base_url, since_id);
        let resp = self
            .http
            .get(&url)
            .send()
            .await
            .with_context(|| format!("请求 CTP 事件更新失败: {}", url))?;
        let res = resp.json::<UpdatesResponse>().await?;
        Ok(res)
    }

    pub async fn insert_order(&self, req: &OrderInsertRequest) -> Result<OrderInsertResponse> {
        let url = format!("{}/order/insert", self.base_url);
        let resp = self
            .mutation("/order/insert")
            .json(req)
            .send()
            .await
            .with_context(|| format!("发送 CTP 报单请求失败: {}", url))?;
        let res = resp.json::<OrderInsertResponse>().await?;
        Ok(res)
    }

    pub async fn cancel_order(
        &self,
        order_ref: &str,
        instrument_id: &str,
        exchange_id: Option<&str>,
    ) -> Result<OrderCancelResponse> {
        let url = format!("{}/order/cancel", self.base_url);
        let req = OrderCancelRequest {
            order_ref: order_ref.to_string(),
            instrument_id: instrument_id.to_string(),
            exchange_id: exchange_id.map(|s| s.to_string()),
        };
        let resp = self
            .mutation("/order/cancel")
            .json(&req)
            .send()
            .await
            .with_context(|| format!("发送 CTP 撤单请求失败: {}", url))?;
        let res = resp.json::<OrderCancelResponse>().await?;
        Ok(res)
    }

    pub async fn emergency_cancel_all(&self) -> Result<bool> {
        let resp = self.mutation("/emergency/cancel_all").send().await?;
        let val = resp.json::<serde_json::Value>().await?;
        Ok(val
            .get("success")
            .and_then(|v| v.as_bool())
            .unwrap_or(false))
    }

    pub async fn emergency_close_all(&self) -> Result<bool> {
        let resp = self.mutation("/emergency/close_all").send().await?;
        let val = resp.json::<serde_json::Value>().await?;
        Ok(val
            .get("success")
            .and_then(|v| v.as_bool())
            .unwrap_or(false))
    }

    pub async fn sync(&self) -> Result<bool> {
        let resp = self.mutation("/sync").send().await?;
        let val = resp.json::<serde_json::Value>().await?;
        Ok(val
            .get("success")
            .and_then(|v| v.as_bool())
            .unwrap_or(false))
    }

    pub async fn market_event_stream(
        &self,
        stream_id: &str,
        after_id: u64,
    ) -> Result<CtpMarketEventStream> {
        let mut url = format!("{}/market/events?after_id={}", self.base_url, after_id);
        if !stream_id.is_empty() {
            url.push_str(&format!("&stream_id={}", stream_id));
        }
        let response = Client::builder()
            .connect_timeout(Duration::from_secs(5))
            .build()?
            .get(&url)
            .send()
            .await?
            .error_for_status()
            .context("CTP 行情推送连接失败")?;
        Ok(CtpMarketEventStream {
            response,
            buffer: Vec::new(),
        })
    }

    pub async fn fetch_market_ticks(
        &self,
        stream_id: &str,
        after_id: u64,
        limit: usize,
    ) -> Result<CtpMarketBatch> {
        let mut url = format!(
            "{}/market/ticks?after_id={}&limit={}",
            self.base_url, after_id, limit
        );
        if !stream_id.is_empty() {
            url.push_str(&format!("&stream_id={}", stream_id));
        }
        let resp = self
            .http
            .get(&url)
            .send()
            .await
            .with_context(|| format!("请求 CTP 行情批次失败: {}", url))?
            .error_for_status()?;
        let batch = resp.json::<CtpMarketBatch>().await?;
        Ok(batch)
    }

    pub async fn subscribe_market(&self, symbols: &[String]) -> Result<Vec<String>> {
        #[derive(Serialize)]
        struct SubReq<'a> {
            symbols: &'a [String],
        }
        #[derive(Deserialize)]
        #[allow(dead_code)]
        struct SubResp {
            success: bool,
            subscribed: Vec<String>,
        }
        let url = format!("{}/market/subscribe", self.base_url);
        let resp = self
            .http
            .post(&url)
            .json(&SubReq { symbols })
            .send()
            .await
            .with_context(|| format!("请求 CTP 订阅行情失败: {}", url))?
            .error_for_status()?;
        let res = resp.json::<SubResp>().await?;
        Ok(res.subscribed)
    }

    pub async fn unsubscribe_market(&self, symbols: &[String]) -> Result<Vec<String>> {
        #[derive(Serialize)]
        struct UnsubReq<'a> {
            symbols: &'a [String],
        }
        #[derive(Deserialize)]
        #[allow(dead_code)]
        struct UnsubResp {
            success: bool,
            subscribed: Vec<String>,
        }
        let url = format!("{}/market/unsubscribe", self.base_url);
        let resp = self
            .http
            .post(&url)
            .json(&UnsubReq { symbols })
            .send()
            .await
            .with_context(|| format!("请求 CTP 退订行情失败: {}", url))?
            .error_for_status()?;
        let res = resp.json::<UnsubResp>().await?;
        Ok(res.subscribed)
    }

    pub async fn market_health(&self) -> Result<CtpMarketHealth> {
        let url = format!("{}/market/health", self.base_url);
        let resp = self
            .http
            .get(&url)
            .send()
            .await
            .with_context(|| format!("请求 CTP 行情健康检查失败: {}", url))?
            .error_for_status()?;
        let res = resp.json::<CtpMarketHealth>().await?;
        Ok(res)
    }
}
