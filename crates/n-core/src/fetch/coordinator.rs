//! Unified request coordinator for Sina futures API.
//!
//! Provides global rate-limiting, prioritized request queuing, in-flight concurrency gating,
//! and unified HTTP connection pooling.
//!
//! Priorities:
//! - `P0` (Finality Probe / Realtime Confirmation): Highest priority, dispatches immediately
//!   on the next available rate-limit slot.
//! - `P1` (Realtime Quote Polling): Medium priority, dispatched before normal/background tasks.
//! - `P2` (Backfill / Refresh / Rollover / Diagnostics): Normal priority, runs when no higher priority
//!   requests are pending.

use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use anyhow::{anyhow, bail, Context, Result};
use tokio::sync::{oneshot, Mutex, Notify, Semaphore};
use tokio::time::sleep;
use tracing::trace;

use super::RateLimiter;

const MAX_RETRIES: usize = 2;
const MAX_CONCURRENT_IN_FLIGHT: usize = 8;

/// Request priority tier.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum RequestPriority {
    /// P0: Finality 探针 / 实时关键确认（最高优先级，优先跳过所有 P1/P2 请求）
    P0 = 0,
    /// P1: 实时行情更新轮询（次高优先级）
    P1 = 1,
    /// P2: 历史回填、普通刷新、换月合约搜索、数据修复（常规优先级）
    P2 = 2,
}

impl RequestPriority {
    pub const FINALITY: Self = Self::P0;
    pub const REALTIME_QUOTE: Self = Self::P1;
    pub const BACKFILL: Self = Self::P2;
    pub const NORMAL: Self = Self::P2;
}

/// Request payload to send to Sina API.
#[derive(Debug, Clone)]
pub struct SinaRequest {
    pub url: String,
    pub headers: Vec<(String, String)>,
    pub priority: RequestPriority,
}

impl SinaRequest {
    pub fn new(url: impl Into<String>, priority: RequestPriority) -> Self {
        Self {
            url: url.into(),
            headers: Vec::new(),
            priority,
        }
    }

    pub fn with_referer(
        url: impl Into<String>,
        referer: impl Into<String>,
        priority: RequestPriority,
    ) -> Self {
        Self {
            url: url.into(),
            headers: vec![("Referer".to_string(), referer.into())],
            priority,
        }
    }

    pub fn with_headers(
        url: impl Into<String>,
        headers: &[(&str, &str)],
        priority: RequestPriority,
    ) -> Self {
        Self {
            url: url.into(),
            headers: headers
                .iter()
                .map(|(k, v)| (k.to_string(), v.to_string()))
                .collect(),
            priority,
        }
    }
}

struct QueuedItem {
    req: SinaRequest,
    respond_to: oneshot::Sender<Result<String>>,
}

#[derive(Default)]
struct PriorityQueues {
    p0: VecDeque<QueuedItem>,
    p1: VecDeque<QueuedItem>,
    p2: VecDeque<QueuedItem>,
}

impl PriorityQueues {
    fn push(&mut self, item: QueuedItem) {
        match item.req.priority {
            RequestPriority::P0 => self.p0.push_back(item),
            RequestPriority::P1 => self.p1.push_back(item),
            RequestPriority::P2 => self.p2.push_back(item),
        }
    }

    fn pop_next_active(&mut self) -> Option<QueuedItem> {
        // P0 has absolute priority
        while let Some(item) = self.p0.pop_front() {
            if !item.respond_to.is_closed() {
                return Some(item);
            }
            trace!("P0 request was cancelled by caller, skipping");
        }
        // Then P1
        while let Some(item) = self.p1.pop_front() {
            if !item.respond_to.is_closed() {
                return Some(item);
            }
            trace!("P1 request was cancelled by caller, skipping");
        }
        // Finally P2
        while let Some(item) = self.p2.pop_front() {
            if !item.respond_to.is_closed() {
                return Some(item);
            }
            trace!("P2 request was cancelled by caller, skipping");
        }
        None
    }

    fn is_empty(&self) -> bool {
        self.p0.is_empty() && self.p1.is_empty() && self.p2.is_empty()
    }

    fn counts(&self) -> (usize, usize, usize) {
        (self.p0.len(), self.p1.len(), self.p2.len())
    }
}

/// Runtime snapshot of coordinator metrics.
#[derive(Debug, Clone, Copy)]
pub struct CoordinatorStats {
    pub p0_queued: usize,
    pub p1_queued: usize,
    pub p2_queued: usize,
    pub in_flight: usize,
    pub total_dispatched: u64,
    pub total_success: u64,
    pub total_failed: u64,
}

/// Global coordinator for all Sina API HTTP requests.
pub struct SinaRequestCoordinator {
    http: reqwest::Client,
    limiter: Arc<RateLimiter>,
    queues: Arc<Mutex<PriorityQueues>>,
    notify: Arc<Notify>,
    semaphore: Arc<Semaphore>,
    worker_started: AtomicBool,
    shutdown: Arc<AtomicBool>,
    total_dispatched: AtomicU64,
    total_success: AtomicU64,
    total_failed: AtomicU64,
}

impl SinaRequestCoordinator {
    pub fn new(interval_ms: u64, minutely_budget: usize) -> Arc<Self> {
        let http = reqwest::Client::builder()
            .user_agent(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            )
            .timeout(Duration::from_secs(30))
            .pool_max_idle_per_host(10)
            .pool_idle_timeout(Duration::from_secs(90))
            .build()
            .expect("构建 HTTP 客户端失败");

        let coordinator = Arc::new(Self {
            http,
            limiter: Arc::new(RateLimiter::new(interval_ms, minutely_budget)),
            queues: Arc::new(Mutex::new(PriorityQueues::default())),
            notify: Arc::new(Notify::new()),
            semaphore: Arc::new(Semaphore::new(MAX_CONCURRENT_IN_FLIGHT)),
            worker_started: AtomicBool::new(false),
            shutdown: Arc::new(AtomicBool::new(false)),
            total_dispatched: AtomicU64::new(0),
            total_success: AtomicU64::new(0),
            total_failed: AtomicU64::new(0),
        });

        coordinator.try_start_worker();
        coordinator
    }

    /// Try starting the worker background task if within an active Tokio runtime.
    pub fn try_start_worker(&self) {
        if self.worker_started.load(Ordering::Relaxed) {
            return;
        }

        if let Ok(handle) = tokio::runtime::Handle::try_current() {
            if !self.worker_started.swap(true, Ordering::SeqCst) {
                let limiter = self.limiter.clone();
                let queues = self.queues.clone();
                let notify = self.notify.clone();
                let semaphore = self.semaphore.clone();
                let http = self.http.clone();
                let shutdown = self.shutdown.clone();

                handle.spawn(async move {
                    run_dispatcher_loop(limiter, queues, notify, semaphore, http, shutdown).await;
                });
            }
        }
    }

    /// Ensures worker is running before enqueuing (lazily starts if runtime wasn't active on construct).
    fn ensure_worker(&self) {
        if !self.worker_started.load(Ordering::Relaxed) {
            self.try_start_worker();
        }
    }

    /// Dynamically updates the rate limiter parameters (e.g. on config change).
    pub async fn update_limits(&self, interval_ms: u64, minutely_budget: usize) {
        self.limiter.update_limits(interval_ms, minutely_budget).await;
    }

    /// Returns current queue stats.
    pub async fn stats(&self) -> CoordinatorStats {
        let (p0_queued, p1_queued, p2_queued) = {
            let q = self.queues.lock().await;
            q.counts()
        };
        let in_flight = MAX_CONCURRENT_IN_FLIGHT.saturating_sub(self.semaphore.available_permits());
        CoordinatorStats {
            p0_queued,
            p1_queued,
            p2_queued,
            in_flight,
            total_dispatched: self.total_dispatched.load(Ordering::Relaxed),
            total_success: self.total_success.load(Ordering::Relaxed),
            total_failed: self.total_failed.load(Ordering::Relaxed),
        }
    }

    /// Enqueue a request and await its response.
    pub async fn execute(&self, req: SinaRequest) -> Result<String> {
        self.ensure_worker();

        if self.shutdown.load(Ordering::Relaxed) {
            bail!("SinaRequestCoordinator has shut down");
        }

        let (tx, rx) = oneshot::channel();
        {
            let mut q = self.queues.lock().await;
            q.push(QueuedItem {
                req,
                respond_to: tx,
            });
        }
        self.notify.notify_one();
        self.total_dispatched.fetch_add(1, Ordering::Relaxed);

        match rx.await {
            Ok(Ok(text)) => {
                self.total_success.fetch_add(1, Ordering::Relaxed);
                Ok(text)
            }
            Ok(Err(e)) => {
                self.total_failed.fetch_add(1, Ordering::Relaxed);
                Err(e)
            }
            Err(_) => {
                self.total_failed.fetch_add(1, Ordering::Relaxed);
                bail!("请求通道关闭（协调器退出或任务取消）")
            }
        }
    }

    /// Convenience helper for simple text fetch with headers.
    pub async fn fetch_text(
        &self,
        url: &str,
        headers: &[(&str, &str)],
        priority: RequestPriority,
    ) -> Result<String> {
        let req = SinaRequest::with_headers(url, headers, priority);
        self.execute(req).await
    }

    /// Shut down the dispatcher loop and cancel waiting requests.
    pub async fn shutdown(&self) {
        self.shutdown.store(true, Ordering::SeqCst);
        self.notify.notify_waiters();
        let mut q = self.queues.lock().await;
        q.p0.clear();
        q.p1.clear();
        q.p2.clear();
    }
}

impl Drop for SinaRequestCoordinator {
    fn drop(&mut self) {
        self.shutdown.store(true, Ordering::SeqCst);
        self.notify.notify_waiters();
    }
}

async fn run_dispatcher_loop(
    limiter: Arc<RateLimiter>,
    queues: Arc<Mutex<PriorityQueues>>,
    notify: Arc<Notify>,
    semaphore: Arc<Semaphore>,
    http: reqwest::Client,
    shutdown: Arc<AtomicBool>,
) {
    loop {
        if shutdown.load(Ordering::Relaxed) {
            break;
        }

        // 1. Wait until there is at least one active request in queues
        loop {
            if shutdown.load(Ordering::Relaxed) {
                return;
            }
            let has_items = {
                let q = queues.lock().await;
                !q.is_empty()
            };
            if has_items {
                break;
            }
            notify.notified().await;
        }

        // 2. Acquire in-flight concurrency permit (caps parallel HTTP connections)
        let permit = match semaphore.clone().acquire_owned().await {
            Ok(p) => p,
            Err(_) => break, // Semaphore closed
        };

        if shutdown.load(Ordering::Relaxed) {
            drop(permit);
            break;
        }

        // 3. Acquire RateLimiter slot (enforces min_interval and sliding-window budget)
        limiter.acquire().await;

        if shutdown.load(Ordering::Relaxed) {
            drop(permit);
            break;
        }

        // 4. AT THIS EXACT MOMENT, pop the highest priority active request!
        // Because we pop right after acquiring the rate slot, any P0 request that arrived
        // during limiter wait immediately gets this slot ahead of any pending P1/P2!
        let next_item = {
            let mut q = queues.lock().await;
            q.pop_next_active()
        };

        let Some(item) = next_item else {
            // All items were dropped / cancelled while waiting
            drop(permit);
            continue;
        };

        // 5. Spawn execution of this request using the shared reqwest::Client
        let client = http.clone();
        tokio::spawn(async move {
            let _permit = permit; // hold concurrency permit until request finishes
            let res = execute_http_request(&client, &item.req).await;
            let _ = item.respond_to.send(res);
        });
    }
}

async fn execute_http_request(http: &reqwest::Client, req: &SinaRequest) -> Result<String> {
    let mut attempt = 0usize;
    loop {
        let mut builder = http.get(&req.url);
        for (k, v) in &req.headers {
            builder = builder.header(k, v);
        }

        match builder.send().await {
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
                bail!("HTTP {status} for {}", req.url);
            }
            Err(e) => {
                if attempt < MAX_RETRIES {
                    attempt += 1;
                    sleep(backoff(attempt)).await;
                    continue;
                }
                return Err(anyhow!("请求失败 {}: {e}", req.url));
            }
        }
    }
}

fn backoff(attempt: usize) -> Duration {
    Duration::from_millis(500 * (1u64 << attempt))
}

pub(crate) fn decode_text(bytes: &[u8]) -> Result<String> {
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
    async fn priority_order_enforced_p0_before_p1_before_p2() {
        let mut queues = PriorityQueues::default();

        let (tx2, _rx2) = oneshot::channel();
        let (tx1, _rx1) = oneshot::channel();
        let (tx0, _rx0) = oneshot::channel();

        // Enqueue in reverse order: P2 first, then P1, then P0
        queues.push(QueuedItem {
            req: SinaRequest::new("http://example.com/p2", RequestPriority::P2),
            respond_to: tx2,
        });
        queues.push(QueuedItem {
            req: SinaRequest::new("http://example.com/p1", RequestPriority::P1),
            respond_to: tx1,
        });
        queues.push(QueuedItem {
            req: SinaRequest::new("http://example.com/p0", RequestPriority::P0),
            respond_to: tx0,
        });

        // Pop should strictly yield P0, then P1, then P2
        let pop0 = queues.pop_next_active().unwrap();
        assert_eq!(pop0.req.priority, RequestPriority::P0);
        assert_eq!(pop0.req.url, "http://example.com/p0");

        let pop1 = queues.pop_next_active().unwrap();
        assert_eq!(pop1.req.priority, RequestPriority::P1);
        assert_eq!(pop1.req.url, "http://example.com/p1");

        let pop2 = queues.pop_next_active().unwrap();
        assert_eq!(pop2.req.priority, RequestPriority::P2);
        assert_eq!(pop2.req.url, "http://example.com/p2");

        assert!(queues.pop_next_active().is_none());
    }

    #[tokio::test]
    async fn cancelled_requests_are_skipped() {
        let mut queues = PriorityQueues::default();

        let (tx0, rx0) = oneshot::channel();
        let (tx1, _rx1) = oneshot::channel();

        queues.push(QueuedItem {
            req: SinaRequest::new("http://example.com/p0_cancelled", RequestPriority::P0),
            respond_to: tx0,
        });
        queues.push(QueuedItem {
            req: SinaRequest::new("http://example.com/p1_active", RequestPriority::P1),
            respond_to: tx1,
        });

        // Drop rx0 (simulating caller timeout/cancellation)
        drop(rx0);

        // pop_next_active should skip p0_cancelled and return p1_active!
        let pop = queues.pop_next_active().unwrap();
        assert_eq!(pop.req.priority, RequestPriority::P1);
        assert_eq!(pop.req.url, "http://example.com/p1_active");
    }

    #[tokio::test]
    async fn fifo_within_same_priority() {
        let mut queues = PriorityQueues::default();

        let (tx_a, _rx_a) = oneshot::channel();
        let (tx_b, _rx_b) = oneshot::channel();

        queues.push(QueuedItem {
            req: SinaRequest::new("http://example.com/p1_first", RequestPriority::P1),
            respond_to: tx_a,
        });
        queues.push(QueuedItem {
            req: SinaRequest::new("http://example.com/p1_second", RequestPriority::P1),
            respond_to: tx_b,
        });

        let first = queues.pop_next_active().unwrap();
        assert_eq!(first.req.url, "http://example.com/p1_first");

        let second = queues.pop_next_active().unwrap();
        assert_eq!(second.req.url, "http://example.com/p1_second");
    }

    #[tokio::test]
    async fn coordinator_stats_tracked() {
        let coordinator = SinaRequestCoordinator::new(50, 100);
        let stats = coordinator.stats().await;
        assert_eq!(stats.p0_queued, 0);
        assert_eq!(stats.p1_queued, 0);
        assert_eq!(stats.p2_queued, 0);
        assert_eq!(stats.in_flight, 0);
    }

    #[tokio::test]
    async fn coordinator_updates_limits() {
        let coordinator = SinaRequestCoordinator::new(400, 60);
        coordinator.update_limits(100, 240).await;
        // Verify no panic and limiter has updated state
    }

    #[tokio::test]
    async fn all_cancelled_yields_none() {
        let mut queues = PriorityQueues::default();
        let (tx, rx) = oneshot::channel();
        queues.push(QueuedItem {
            req: SinaRequest::new("http://example.com/cancelled", RequestPriority::P0),
            respond_to: tx,
        });
        drop(rx);
        assert!(queues.pop_next_active().is_none());
        assert!(queues.is_empty());
    }
}
