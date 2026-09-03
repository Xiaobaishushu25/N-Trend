//! Hybrid data source combining TianQin primary and Sina fallback.

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use anyhow::Result;
use serde::{Deserialize, Serialize};
use tokio::sync::broadcast;
use tokio::sync::RwLock;

use crate::config::Config;
use crate::fetch::datasource::MarketDataSource;
use crate::fetch::kline::{Kline, RawKlineResponse};
use crate::fetch::quotes::Quote;
use crate::fetch::symbols::FuturesSymbol;
use crate::fetch::tq_client::TqBridgeClient;
use crate::fetch::SinaClient;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DataSourceEvent {
    pub from: String,
    pub to: String,
    pub reason: String,
}

pub struct HybridDataSource {
    tq_client: Arc<RwLock<TqBridgeClient>>,
    sina_client: SinaClient,
    config: Arc<RwLock<Config>>,
    tq_available: AtomicBool,
    consecutive_failures: AtomicU32,
    consecutive_successes: AtomicU32,
    last_health_check: RwLock<Instant>,
    failover_time: RwLock<Instant>,
    event_tx: broadcast::Sender<DataSourceEvent>,
}

impl HybridDataSource {
    pub fn new(
        tq_client: TqBridgeClient,
        sina_client: SinaClient,
        config: Arc<RwLock<Config>>,
    ) -> Self {
        let (event_tx, _) = broadcast::channel(64);
        Self {
            tq_client: Arc::new(RwLock::new(tq_client)),
            sina_client,
            config,
            // 启动时必须经过真实 /health 探测后才能切为 true。
            tq_available: AtomicBool::new(false),
            consecutive_failures: AtomicU32::new(0),
            consecutive_successes: AtomicU32::new(0),
            last_health_check: RwLock::new(Instant::now() - Duration::from_secs(60)),
            failover_time: RwLock::new(Instant::now() - Duration::from_secs(60)),
            event_tx,
        }
    }

    pub fn subscribe_events(&self) -> broadcast::Receiver<DataSourceEvent> {
        self.event_tx.subscribe()
    }

    pub fn sina_client(&self) -> &SinaClient {
        &self.sina_client
    }

    pub async fn tq_client(&self) -> TqBridgeClient {
        self.tq_client.read().await.clone()
    }

    pub fn tq_is_available(&self) -> bool {
        self.tq_available.load(Ordering::Relaxed)
    }

    /// 动态热更新天勤本地桥接端口
    pub async fn update_bridge_port(&self, port: u16) {
        *self.tq_client.write().await = TqBridgeClient::with_port(port);
        self.tq_available.store(false, Ordering::Relaxed);
        self.consecutive_failures.store(0, Ordering::Relaxed);
        self.consecutive_successes.store(0, Ordering::Relaxed);
        *self.last_health_check.write().await = Instant::now() - Duration::from_secs(60);
        *self.failover_time.write().await = Instant::now() - Duration::from_secs(60);
    }

    /// 立即将天勤标记为不可用。用于启动/配置切换失败，不经过“两次请求失败”宽限。
    pub async fn mark_tq_unavailable(&self, reason: &str, emit_event: bool) {
        let was_available = self.tq_available.swap(false, Ordering::Relaxed);
        self.consecutive_failures.store(0, Ordering::Relaxed);
        self.consecutive_successes.store(0, Ordering::Relaxed);
        *self.last_health_check.write().await = Instant::now();
        *self.failover_time.write().await = Instant::now();
        if emit_event && was_available {
            let _ = self.event_tx.send(DataSourceEvent {
                from: "tqsdk".to_string(),
                to: "sina".to_string(),
                reason: reason.to_string(),
            });
        }
    }

    /// 真实探测桥接服务；只有 /health 成功后才切回天勤并发送恢复通知。
    pub async fn probe_and_activate(&self, reason: &str, emit_event: bool) -> bool {
        let healthy = self.tq_client.read().await.is_healthy().await;
        *self.last_health_check.write().await = Instant::now();
        if !healthy {
            self.mark_tq_unavailable("天勤桥接服务健康检查失败，当前使用新浪备用数据源", emit_event)
                .await;
            return false;
        }

        let was_available = self.tq_available.swap(true, Ordering::Relaxed);
        self.consecutive_failures.store(0, Ordering::Relaxed);
        self.consecutive_successes.store(0, Ordering::Relaxed);
        *self.failover_time.write().await = Instant::now() - Duration::from_secs(60);
        if emit_event && !was_available {
            let _ = self.event_tx.send(DataSourceEvent {
                from: "sina".to_string(),
                to: "tqsdk".to_string(),
                reason: reason.to_string(),
            });
        }
        true
    }

    pub async fn check_and_update_health(&self) -> bool {
        let is_healthy = self.tq_client.read().await.is_healthy().await;
        *self.last_health_check.write().await = Instant::now();

        if is_healthy {
            if self.tq_available.load(Ordering::Relaxed) {
                self.consecutive_failures.store(0, Ordering::Relaxed);
                return true;
            }

            // 处于降级状态：需等待至少 15s 冷却期且连续 2 次探活成功才触发切回（防震荡）
            let in_cooldown = self.failover_time.read().await.elapsed() < Duration::from_secs(15);
            if in_cooldown {
                return false;
            }

            let succ = self.consecutive_successes.fetch_add(1, Ordering::Relaxed) + 1;
            if succ >= 2 {
                self.tq_available.store(true, Ordering::Relaxed);
                self.consecutive_failures.store(0, Ordering::Relaxed);
                self.consecutive_successes.store(0, Ordering::Relaxed);
                tracing::info!("天勤数据源探活确认稳定恢复，已切回主力天勤数据源");
                let _ = self.event_tx.send(DataSourceEvent {
                    from: "sina".to_string(),
                    to: "tqsdk".to_string(),
                    reason: "天勤数据源探活确认稳定恢复，已自动切回主力天勤数据源".to_string(),
                });
                return true;
            }
            false
        } else {
            self.consecutive_successes.store(0, Ordering::Relaxed);
            false
        }
    }

    async fn should_use_tq(&self) -> bool {
        let cfg = self.config.read().await;
        if cfg.data_source.primary_source != "tqsdk" {
            return false;
        }
        if !self.tq_available.load(Ordering::Relaxed) {
            let last = *self.last_health_check.read().await;
            if last.elapsed() > Duration::from_secs(10) {
                return self.check_and_update_health().await;
            }
            return false;
        }
        true
    }

    async fn record_tq_failure(&self, err: &anyhow::Error) {
        self.consecutive_successes.store(0, Ordering::Relaxed);
        let fails = self.consecutive_failures.fetch_add(1, Ordering::Relaxed) + 1;
        tracing::warn!("天勤接口请求异常 (第{}次): {err:#}", fails);
        if fails >= 3 && self.tq_available.swap(false, Ordering::Relaxed) {
            *self.failover_time.write().await = Instant::now();
            tracing::warn!("天勤数据源不可用，已自动切换为新浪数据源");
            let _ = self.event_tx.send(DataSourceEvent {
                from: "tqsdk".to_string(),
                to: "sina".to_string(),
                reason: format!("天勤接口请求异常（{}），已自动降级切换至新浪数据源", err),
            });
        }
    }

    fn record_tq_success(&self) {
        self.consecutive_failures.store(0, Ordering::Relaxed);
    }
}

impl MarketDataSource for HybridDataSource {
    fn name(&self) -> &'static str {
        if self.tq_available.load(Ordering::Relaxed) {
            "hybrid(tqsdk->sina)"
        } else {
            "hybrid(sina-fallback)"
        }
    }

    async fn fetch_quotes(&self, codes: &[String]) -> Result<HashMap<String, Quote>> {
        if codes.is_empty() {
            return Ok(HashMap::new());
        }

        let fallback_enabled = self.config.read().await.data_source.fallback_enabled;

        if self.should_use_tq().await {
            let tq = self.tq_client.read().await.clone();
            match tq.fetch_quotes(codes).await {
                Ok(quotes) if !quotes.is_empty() => {
                    self.record_tq_success();
                    // 如果部分品种天勤未返回（如特殊代码），且启用了降级时用新浪补充
                    if quotes.len() < codes.len() && fallback_enabled {
                        let missing: Vec<String> = codes
                            .iter()
                            .filter(|c| !quotes.contains_key(*c))
                            .cloned()
                            .collect();
                        if !missing.is_empty() {
                            if let Ok(sina_supplement) =
                                self.sina_client.fetch_quotes(&missing).await
                            {
                                let mut combined = quotes;
                                for (k, v) in sina_supplement {
                                    combined.insert(k, v);
                                }
                                return Ok(combined);
                            }
                        }
                    }
                    return Ok(quotes);
                }
                Ok(_) => {
                    tracing::warn!("天勤未返回任何行情数据");
                    if !fallback_enabled {
                        anyhow::bail!("天勤未返回任何行情数据，且已禁用自动降级切换");
                    }
                }
                Err(e) => {
                    self.record_tq_failure(&e).await;
                    if !fallback_enabled {
                        return Err(e);
                    }
                }
            }
        } else {
            let cfg = self.config.read().await;
            if cfg.data_source.primary_source == "tqsdk" && !fallback_enabled {
                anyhow::bail!("天勤数据源当前不可用，且已禁用自动降级切换至新浪");
            }
        }

        // 回退/直连新浪
        self.sina_client.fetch_quotes(codes).await
    }

    async fn fetch_minute(
        &self,
        symbol: &str,
        period: &str,
        count: usize,
    ) -> Result<Vec<Kline>> {
        let fallback_enabled = self.config.read().await.data_source.fallback_enabled;

        if self.should_use_tq().await {
            let tq = self.tq_client.read().await.clone();
            match tq.fetch_minute(symbol, period, count).await {
                Ok(klines) if !klines.is_empty() => {
                    self.record_tq_success();
                    return Ok(klines);
                }
                Ok(_) => {
                    tracing::warn!("天勤返回空K线 ({symbol}/{period})");
                    if !fallback_enabled {
                        anyhow::bail!("天勤返回空K线 ({symbol}/{period})，且已禁用自动降级切换");
                    }
                }
                Err(e) => {
                    self.record_tq_failure(&e).await;
                    if !fallback_enabled {
                        return Err(e);
                    }
                }
            }
        } else {
            let cfg = self.config.read().await;
            if cfg.data_source.primary_source == "tqsdk" && !fallback_enabled {
                anyhow::bail!("天勤数据源当前不可用，且已禁用自动降级切换至新浪");
            }
        }

        // 回退/直连新浪
        self.sina_client.fetch_minute(symbol, period, count).await
    }

    async fn fetch_minute_raw(
        &self,
        symbol: &str,
        period: &str,
        count: usize,
    ) -> Result<RawKlineResponse> {
        let fallback_enabled = self.config.read().await.data_source.fallback_enabled;

        if self.should_use_tq().await {
            let tq = self.tq_client.read().await.clone();
            match tq.fetch_minute_raw(symbol, period, count).await {
                Ok(raw) if !raw.klines.is_empty() => {
                    self.record_tq_success();
                    return Ok(raw);
                }
                Ok(_) => {
                    tracing::warn!("天勤返回空原始K线 ({symbol}/{period})");
                    if !fallback_enabled {
                        anyhow::bail!("天勤返回空原始K线 ({symbol}/{period})，且已禁用自动降级切换");
                    }
                }
                Err(e) => {
                    self.record_tq_failure(&e).await;
                    if !fallback_enabled {
                        return Err(e);
                    }
                }
            }
        } else {
            let cfg = self.config.read().await;
            if cfg.data_source.primary_source == "tqsdk" && !fallback_enabled {
                anyhow::bail!("天勤数据源当前不可用，且已禁用自动降级切换至新浪");
            }
        }

        // 回退/直连新浪
        self.sina_client
            .fetch_minute_raw(symbol, period, count)
            .await
    }

    async fn search_contracts(&self, keyword: &str) -> Result<Vec<FuturesSymbol>> {
        let fallback_enabled = self.config.read().await.data_source.fallback_enabled;

        if self.should_use_tq().await {
            let tq = self.tq_client.read().await.clone();
            match tq.search_contracts(keyword).await {
                Ok(rows) if !rows.is_empty() => {
                    self.record_tq_success();
                    return Ok(rows);
                }
                _ => {}
            }
        } else {
            let cfg = self.config.read().await;
            if cfg.data_source.primary_source == "tqsdk" && !fallback_enabled {
                return Ok(Vec::new());
            }
        }

        self.sina_client.search_contracts(keyword).await
    }

    async fn is_healthy(&self) -> bool {
        self.check_and_update_health().await || self.sina_client.is_healthy().await
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn hybrid_data_source_falls_back_when_tq_unavailable() {
        let config = Arc::new(RwLock::new(Config::default()));
        let tq_client = TqBridgeClient::with_port(9999); // 无效端口
        let sina_client = SinaClient::new();
        let hybrid = HybridDataSource::new(tq_client, sina_client, config);

        // 未经真实健康探测时必须保持降级态，不能先乐观标记为天勤。
        assert_eq!(hybrid.name(), "hybrid(sina-fallback)");
        // 当 Tq 端口无法连接时，健康检查返回 false
        let healthy = hybrid.tq_client().await.is_healthy().await;
        assert!(!healthy);
    }

    #[tokio::test]
    async fn hybrid_data_source_strictly_honors_fallback_disabled() {
        let mut cfg = Config::default();
        cfg.data_source.primary_source = "tqsdk".to_string();
        cfg.data_source.fallback_enabled = false;
        let config = Arc::new(RwLock::new(cfg));
        let tq_client = TqBridgeClient::with_port(9999); // 无效端口
        let sina_client = SinaClient::new();
        let hybrid = HybridDataSource::new(tq_client, sina_client, config);

        // 当关闭 fallback_enabled 且 Tq 不可用时，fetch 应返回 Err
        let res = hybrid.fetch_quotes(&["RB0".to_string()]).await;
        assert!(res.is_err(), "当 fallback_enabled 为 false 时不应降级回退到新浪");
    }
}
