//! Sina futures data fetching with a polite rate limiter.

pub mod coordinator;
pub mod datasource;
pub mod hybrid;
pub mod kline;
pub mod quotes;
pub mod symbols;
pub mod tq_client;

pub use coordinator::{CoordinatorStats, RequestPriority, SinaRequest, SinaRequestCoordinator};
pub use datasource::MarketDataSource;
pub use hybrid::{DataSourceEvent, HybridDataSource};
pub use tq_client::{
    BarCloseProof, ClosedBarEvent, ClosedBarEventsResponse, SubscribeKlinesResponse,
    TqBridgeClient,
};

use std::collections::VecDeque;
use std::sync::{Arc, OnceLock};
use std::time::Duration;

use anyhow::Result;
use tokio::time::{sleep, Instant};

const DEFAULT_INTERVAL_MS: u64 = 400;
const DEFAULT_MINUTELY_BUDGET: usize = 60;

struct LimiterState {
    min_interval: Duration,
    budget: usize,
    window: Duration,
    hits: VecDeque<Instant>,
}

/// 滑动窗口节流器：同时约束“单请求最小间隔”和“每分钟请求预算”。
pub struct RateLimiter {
    state: tokio::sync::Mutex<LimiterState>,
}

impl RateLimiter {
    pub fn new(interval_ms: u64, minutely_budget: usize) -> Self {
        Self::with_window(interval_ms, minutely_budget, 60_000)
    }

    pub fn with_window(interval_ms: u64, minutely_budget: usize, window_ms: u64) -> Self {
        Self {
            state: tokio::sync::Mutex::new(LimiterState {
                min_interval: Duration::from_millis(interval_ms.max(50)),
                budget: minutely_budget.max(1),
                window: Duration::from_millis(window_ms.max(100)),
                hits: VecDeque::new(),
            }),
        }
    }

    /// 动态热更新单请求最小间隔与每分钟预算。
    pub async fn update_limits(&self, interval_ms: u64, minutely_budget: usize) {
        let mut state = self.state.lock().await;
        state.min_interval = Duration::from_millis(interval_ms.max(50));
        state.budget = minutely_budget.max(1);
    }

    pub async fn acquire(&self) {
        loop {
            let mut state = self.state.lock().await;
            let now = Instant::now();
            while state
                .hits
                .front()
                .is_some_and(|t| now.duration_since(*t) >= state.window)
            {
                state.hits.pop_front();
            }
            if state.hits.len() < state.budget {
                let wait = state
                    .hits
                    .back()
                    .map(|t| state.min_interval.saturating_sub(now.duration_since(*t)))
                    .unwrap_or(Duration::ZERO);
                if wait.is_zero() {
                    state.hits.push_back(now);
                    return;
                }
                drop(state);
                sleep(wait).await;
                continue;
            }
            let oldest = *state.hits.front().expect("预算满时队列非空");
            let window = state.window;
            drop(state);
            sleep(window.saturating_sub(now.duration_since(oldest)) + Duration::from_millis(1))
                .await;
        }
    }
}

static GLOBAL_COORDINATOR: OnceLock<Arc<SinaRequestCoordinator>> = OnceLock::new();

/// 获取或创建全局进程单例 SinaRequestCoordinator。
pub fn global_sina_coordinator() -> Arc<SinaRequestCoordinator> {
    GLOBAL_COORDINATOR
        .get_or_init(|| SinaRequestCoordinator::new(DEFAULT_INTERVAL_MS, DEFAULT_MINUTELY_BUDGET))
        .clone()
}

/// 客户端门面：轻量级、可廉价 Clone，底层接入统一的 `SinaRequestCoordinator` 与 `Global RateLimiter`。
#[derive(Clone)]
pub struct SinaClient {
    coordinator: Arc<SinaRequestCoordinator>,
}

impl SinaClient {
    /// 默认使用进程级共享协调器与全局限流器。
    pub fn new() -> Self {
        Self {
            coordinator: global_sina_coordinator(),
        }
    }

    /// 显式获取全局共享客户端单例。
    pub fn global() -> Self {
        Self::new()
    }

    /// 获取底层全局协调器 Arc 引用。
    pub fn global_coordinator() -> Arc<SinaRequestCoordinator> {
        global_sina_coordinator()
    }

    /// 创建具有独立限流与独立协调器的客户端（常用于独立集成测试或隔离环境）。
    pub fn with_limits(interval_ms: u64, minutely_budget: usize) -> Self {
        Self {
            coordinator: SinaRequestCoordinator::new(interval_ms, minutely_budget),
        }
    }

    /// 使用指定的协调器构建客户端句柄。
    pub fn with_coordinator(coordinator: Arc<SinaRequestCoordinator>) -> Self {
        Self { coordinator }
    }

    /// 获取底层协调器句柄。
    pub fn coordinator(&self) -> &Arc<SinaRequestCoordinator> {
        &self.coordinator
    }

    /// 动态热更新底层限速器参数。
    pub async fn update_limits(&self, interval_ms: u64, minutely_budget: usize) {
        self.coordinator
            .update_limits(interval_ms, minutely_budget)
            .await;
    }

    /// 默认优先级 (P2) 的文本请求。
    pub async fn get_text(&self, url: &str) -> Result<String> {
        self.get_text_with_priority(url, RequestPriority::P2).await
    }

    /// 按指定优先级请求文本。
    pub async fn get_text_with_priority(
        &self,
        url: &str,
        priority: RequestPriority,
    ) -> Result<String> {
        self.coordinator.fetch_text(url, &[], priority).await
    }

    /// 带 Referer 头的文本请求（默认按 P1 实时行情优先级调度）。
    pub async fn get_text_with_referer(&self, url: &str, referer: &str) -> Result<String> {
        self.get_text_with_referer_and_priority(url, referer, RequestPriority::P1)
            .await
    }

    /// 带 Referer 头及指定优先级的文本请求。
    pub async fn get_text_with_referer_and_priority(
        &self,
        url: &str,
        referer: &str,
        priority: RequestPriority,
    ) -> Result<String> {
        self.coordinator
            .fetch_text(url, &[("Referer", referer)], priority)
            .await
    }

    /// 带自定义 Header 头及指定优先级的文本请求。
    pub async fn get_text_with_headers(
        &self,
        url: &str,
        headers: &[(&str, &str)],
    ) -> Result<String> {
        self.get_text_with_headers_and_priority(url, headers, RequestPriority::P2)
            .await
    }

    /// 带自定义 Header 头及指定优先级的文本请求。
    pub async fn get_text_with_headers_and_priority(
        &self,
        url: &str,
        headers: &[(&str, &str)],
        priority: RequestPriority,
    ) -> Result<String> {
        self.coordinator.fetch_text(url, headers, priority).await
    }
}

impl Default for SinaClient {
    fn default() -> Self {
        Self::new()
    }
}

impl MarketDataSource for SinaClient {
    fn name(&self) -> &'static str {
        "sina"
    }

    async fn fetch_quotes(
        &self,
        codes: &[String],
    ) -> Result<std::collections::HashMap<String, quotes::Quote>> {
        quotes::fetch_quotes(self, codes).await
    }

    async fn fetch_minute(
        &self,
        symbol: &str,
        period: &str,
        count: usize,
    ) -> Result<Vec<kline::Kline>> {
        kline::fetch_minute(self, symbol, period, count).await
    }

    async fn fetch_minute_raw(
        &self,
        symbol: &str,
        period: &str,
        count: usize,
    ) -> Result<kline::RawKlineResponse> {
        kline::fetch_minute_raw(self, symbol, period, count).await
    }

    async fn search_contracts(&self, keyword: &str) -> Result<Vec<symbols::FuturesSymbol>> {
        symbols::search_contracts(self, keyword).await
    }

    async fn is_healthy(&self) -> bool {
        true
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

    #[tokio::test]
    async fn limiter_update_limits_dynamically() {
        let limiter = RateLimiter::new(200, 10);
        limiter.update_limits(50, 100).await;
        let start = Instant::now();
        limiter.acquire().await;
        limiter.acquire().await;
        // 应该以 50ms 左右的间隔通过
        assert!(start.elapsed() >= Duration::from_millis(40));
    }

    #[tokio::test]
    async fn sina_client_shares_coordinator() {
        let client_a = SinaClient::new();
        let client_b = client_a.clone();
        assert!(Arc::ptr_eq(client_a.coordinator(), client_b.coordinator()));
    }
}
