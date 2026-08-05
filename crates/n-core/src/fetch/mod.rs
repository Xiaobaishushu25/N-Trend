//! Sina futures data fetching with a polite rate limiter.

pub mod kline;
pub mod quotes;
pub mod symbols;

use std::collections::VecDeque;
use std::time::Duration;

use anyhow::{anyhow, bail, Context, Result};
use tokio::time::{sleep, Instant};

const DEFAULT_INTERVAL_MS: u64 = 400;
const DEFAULT_MINUTELY_BUDGET: usize = 60;
const MAX_RETRIES: usize = 2;

/// 滑动窗口节流器：同时约束“单请求最小间隔”和“每分钟请求预算”。
pub struct RateLimiter {
    min_interval: Duration,
    budget: usize,
    window: Duration,
    hits: tokio::sync::Mutex<VecDeque<Instant>>,
}

impl RateLimiter {
    pub fn new(interval_ms: u64, minutely_budget: usize) -> Self {
        Self::with_window(interval_ms, minutely_budget, 60_000)
    }

    pub fn with_window(interval_ms: u64, minutely_budget: usize, window_ms: u64) -> Self {
        Self {
            min_interval: Duration::from_millis(interval_ms.max(50)),
            budget: minutely_budget.max(1),
            window: Duration::from_millis(window_ms.max(100)),
            hits: tokio::sync::Mutex::new(VecDeque::new()),
        }
    }

    pub async fn acquire(&self) {
        loop {
            let mut hits = self.hits.lock().await;
            let now = Instant::now();
            while hits
                .front()
                .is_some_and(|t| now.duration_since(*t) >= self.window)
            {
                hits.pop_front();
            }
            if hits.len() < self.budget {
                let wait = hits
                    .back()
                    .map(|t| self.min_interval.saturating_sub(now.duration_since(*t)))
                    .unwrap_or(Duration::ZERO);
                if wait.is_zero() {
                    hits.push_back(now);
                    return;
                }
                drop(hits);
                sleep(wait).await;
                continue;
            }
            let oldest = *hits.front().expect("预算满时队列非空");
            drop(hits);
            sleep(self.window.saturating_sub(now.duration_since(oldest)) + Duration::from_millis(1))
                .await;
        }
    }
}

pub struct SinaClient {
    http: reqwest::Client,
    limiter: RateLimiter,
}

impl SinaClient {
    pub fn new() -> Self {
        Self::with_limits(DEFAULT_INTERVAL_MS, DEFAULT_MINUTELY_BUDGET)
    }

    pub fn with_limits(interval_ms: u64, minutely_budget: usize) -> Self {
        let http = reqwest::Client::builder()
            .user_agent(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            )
            .timeout(Duration::from_secs(30))
            .build()
            .expect("构建 HTTP 客户端失败");
        Self {
            http,
            limiter: RateLimiter::new(interval_ms, minutely_budget),
        }
    }

    /// 带节流与指数退避重试的文本请求。
    pub async fn get_text(&self, url: &str) -> Result<String> {
        self.get_text_with_headers(url, &[]).await
    }

    /// 带 Referer 头（新浪行情接口要求）的文本请求。
    pub async fn get_text_with_referer(&self, url: &str, referer: &str) -> Result<String> {
        self.get_text_with_headers(url, &[("Referer", referer)]).await
    }

    async fn get_text_with_headers(&self, url: &str, headers: &[(&str, &str)]) -> Result<String> {
        let mut attempt = 0usize;
        loop {
            self.limiter.acquire().await;
            let mut req = self.http.get(url);
            for (key, value) in headers {
                req = req.header(*key, *value);
            }
            match req.send().await {
                Ok(resp) if resp.status().is_success() => {
                    let bytes = resp.bytes().await.context("读取响应失败")?;
                    return decode_text(&bytes);
                }
                Ok(resp) => {
                    let status = resp.status();
                    if attempt < MAX_RETRIES {
                        attempt += 1;
                        sleep(backoff(attempt)).await;
                        continue;
                    }
                    bail!("HTTP {status} for {url}");
                }
                Err(e) => {
                    if attempt < MAX_RETRIES {
                        attempt += 1;
                        sleep(backoff(attempt)).await;
                        continue;
                    }
                    return Err(anyhow!("请求失败 {url}: {e}"));
                }
            }
        }
    }
}

impl Default for SinaClient {
    fn default() -> Self {
        Self::new()
    }
}

fn backoff(attempt: usize) -> Duration {
    Duration::from_millis(500 * (1u64 << attempt))
}

fn decode_text(bytes: &[u8]) -> Result<String> {
    match std::str::from_utf8(bytes) {
        Ok(text) => Ok(text.to_string()),
        Err(_) => {
            let (text, _, had_errors) = encoding_rs::GBK.decode(bytes);
            if had_errors {
                bail!("response is neither valid UTF-8 nor GBK");
            }
            Ok(text.into_owned())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn limiter_enforces_minute_budget() {
        let limiter = RateLimiter::with_window(1, 2, 200);
        let start = Instant::now();
        limiter.acquire().await;
        limiter.acquire().await;
        // 第三个请求需要等待窗口滑出
        limiter.acquire().await;
        assert!(start.elapsed() >= Duration::from_millis(50));
    }

    #[tokio::test]
    async fn limiter_enforces_min_interval() {
        let limiter = RateLimiter::new(60, 100);
        let start = Instant::now();
        limiter.acquire().await;
        limiter.acquire().await;
        assert!(start.elapsed() >= Duration::from_millis(50));
    }
}


