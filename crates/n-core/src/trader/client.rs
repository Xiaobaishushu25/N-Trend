use std::time::Duration;
use anyhow::{Context, Result};
use reqwest::Client;

use super::model::*;

#[derive(Clone, Debug)]
pub struct CtpTraderClient {
    base_url: String,
    http: Client,
}

impl CtpTraderClient {
    pub fn new(base_url: String) -> Self {
        let http = Client::builder()
            .timeout(Duration::from_secs(5))
            .build()
            .unwrap_or_else(|_| Client::new());
        Self { base_url, http }
    }

    pub fn for_port(port: u16) -> Self {
        Self::new(format!("http://127.0.0.1:{port}"))
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
            .http
            .post(&url)
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
            .http
            .post(&url)
            .json(&req)
            .send()
            .await
            .with_context(|| format!("发送 CTP 撤单请求失败: {}", url))?;
        let res = resp.json::<OrderCancelResponse>().await?;
        Ok(res)
    }

    pub async fn emergency_cancel_all(&self) -> Result<bool> {
        let url = format!("{}/emergency/cancel_all", self.base_url);
        let resp = self.http.post(&url).send().await?;
        let val = resp.json::<serde_json::Value>().await?;
        Ok(val.get("success").and_then(|v| v.as_bool()).unwrap_or(false))
    }

    pub async fn emergency_close_all(&self) -> Result<bool> {
        let url = format!("{}/emergency/close_all", self.base_url);
        let resp = self.http.post(&url).send().await?;
        let val = resp.json::<serde_json::Value>().await?;
        Ok(val.get("success").and_then(|v| v.as_bool()).unwrap_or(false))
    }

    pub async fn sync(&self) -> Result<bool> {
        let url = format!("{}/sync", self.base_url);
        let resp = self.http.post(&url).send().await?;
        let val = resp.json::<serde_json::Value>().await?;
        Ok(val.get("success").and_then(|v| v.as_bool()).unwrap_or(false))
    }
}
