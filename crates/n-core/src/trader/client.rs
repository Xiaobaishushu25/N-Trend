use anyhow::{Context, Result};
use reqwest::Client;
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
}
