//! Independent Finality Observer and Shadow Validator.
//!
//! 零风险独立观测链：
//! - 5m 时间点到达（含 10:15 / 11:30 / 15:00 / 夜盘收盘等收盘边界）
//! - 从 T+0 开始，每 5 秒使用独立的 SinaClient 抓取原始 5m（不做 30s 过滤）
//! - 提取并归一化指纹，连续 3 次相同时标记 Candidate Final 并记录理论扫描时刻
//! - 持续暗中观察至 T+120s；若此后再次发生修改，标记 False Final / Late Revision
//! - 结果写入独立的数据表 bar_observations 与 bar_finality_trials，绝不改动生产派生与扫描

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use chrono::{DateTime, Datelike, Local, NaiveTime, Timelike};
use sea_orm::DatabaseConnection;
use serde::{Deserialize, Serialize};
use tokio::time::sleep;
use tracing::{error, info, warn};

use crate::fetch::kline::fetch_minute_raw;
use crate::fetch::SinaClient;
use crate::finality::model::{
    BarFingerprint, ObservationRecord, SessionType, DEFAULT_SENTINELS,
};
use crate::finality::tracker::BarFinalityTracker;
use crate::storage::repo::{insert_bar_observation, upsert_finality_trial};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FinalityConfig {
    pub enabled: bool,
    pub sentinels: Vec<String>,
    pub probe_interval_secs: u64,
    pub observe_duration_secs: u64,
    pub stable_required: usize,
}

impl Default for FinalityConfig {
    fn default() -> Self {
        Self {
            enabled: true,
            sentinels: DEFAULT_SENTINELS.iter().map(|s| s.to_string()).collect(),
            probe_interval_secs: 5,
            observe_duration_secs: 120,
            stable_required: 3,
        }
    }
}

pub struct FinalityObserver {
    db: DatabaseConnection,
    config: FinalityConfig,
    client: SinaClient,
}

impl FinalityObserver {
    /// 使用共享 SinaClient 构建观测器（所有探针请求均接入统一协调层与全局 RateLimiter，走 P0 优先级通道）。
    pub fn new(db: DatabaseConnection, config: FinalityConfig, client: SinaClient) -> Self {
        Self { db, config, client }
    }

    /// 便捷构造函数：使用全局共享 SinaClient。
    pub fn with_default_client(db: DatabaseConnection, config: FinalityConfig) -> Self {
        Self::new(db, config, SinaClient::global())
    }

    /// 启动后台独立观测轮询循环。
    pub fn start(self: Arc<Self>) -> tokio::task::JoinHandle<()> {
        if tokio::runtime::Handle::try_current().is_err() {
            warn!("FinalityObserver::start called outside Tokio runtime, fallback to dedicated thread");
            let me = self.clone();
            std::thread::Builder::new()
                .name("finality-observer".into())
                .spawn(move || {
                    let rt = tokio::runtime::Builder::new_multi_thread()
                        .enable_all()
                        .build()
                        .expect("finality observer runtime");
                    rt.block_on(async move {
                        let this = me;
                        let mut last_observed_boundary: Option<String> = None;
                        loop {
                            tokio::time::sleep(std::time::Duration::from_millis(500)).await;
                            if !this.config.enabled {
                                tokio::time::sleep(std::time::Duration::from_secs(5)).await;
                                continue;
                            }
                            let now = chrono::Local::now();
                            if now.minute() % 5 == 0 && now.second() == 0 {
                                let boundary_ts = now.format("%Y-%m-%d %H:%M:00").to_string();
                                if last_observed_boundary.as_deref() == Some(&boundary_ts) {
                                    continue;
                                }
                                last_observed_boundary = Some(boundary_ts.clone());
                                if is_valid_5m_trading_boundary(&now) {
                                    let observer = this.clone();
                                    tokio::spawn(async move {
                                        observer.run_observation_session(&boundary_ts, now).await;
                                    });
                                }
                            }
                        }
                    });
                })
                .expect("spawn finality observer thread");
            return tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .unwrap()
                .spawn(async {});
        }
        tokio::spawn(async move {
            info!(
                "🔭 Finality 独立观测系统已启动 | 哨兵品种: {:?} | 探测周期: {}s | 观察时长: {}s",
                self.config.sentinels, self.config.probe_interval_secs, self.config.observe_duration_secs
            );

            // 每 1 秒轮询一次当前秒数，对齐 5m 边界整点 (minute % 5 == 0 && second == 0)
            let mut last_observed_boundary: Option<String> = None;
            loop {
                sleep(Duration::from_millis(500)).await;
                if !self.config.enabled {
                    sleep(Duration::from_secs(5)).await;
                    continue;
                }

                let now = Local::now();
                if now.minute() % 5 == 0 && now.second() == 0 {
                    let boundary_ts = now.format("%Y-%m-%d %H:%M:00").to_string();
                    if last_observed_boundary.as_deref() == Some(&boundary_ts) {
                        continue;
                    }
                    last_observed_boundary = Some(boundary_ts.clone());

                    if is_valid_5m_trading_boundary(&now) {
                        info!("🎯 捕获 5m 观测边界: {}，启动 T+0~T+120s 影子探测会话", boundary_ts);
                        let observer = self.clone();
                        tokio::spawn(async move {
                            observer.run_observation_session(&boundary_ts, now).await;
                        });
                    }
                }
            }
        })
    }

    /// 针对特定 5m Bar 执行 120 秒的密集探测会话。
    pub async fn run_observation_session(&self, bar_ts: &str, session_start: DateTime<Local>) {
        let sentinels = &self.config.sentinels;
        let mut trackers: HashMap<String, BarFinalityTracker> = HashMap::new();
        for sym in sentinels {
            let st = SessionType::classify(sym, bar_ts);
            trackers.insert(sym.clone(), BarFinalityTracker::new(sym, bar_ts, st));
        }

        let total_probes = (self.config.observe_duration_secs / self.config.probe_interval_secs) as i32 + 1;
        info!(
            "[{}] 开始会话: 监控 {} 个哨兵品种，预计探测 {} 轮",
            bar_ts, sentinels.len(), total_probes
        );

        for probe_index in 0..total_probes {
            let probe_time = Local::now();
            let elapsed_ms = (probe_time - session_start).num_milliseconds().max(0);

            for symbol in sentinels {
                match fetch_minute_raw(&self.client, symbol, "5", 5).await {
                    Ok(resp) => {
                        // 寻找对应当前 bar_ts 的那根 K 线
                        let matched_bar = resp.klines.iter().rev().find(|k| k.datetime == bar_ts);
                        if let Some(bar) = matched_bar {
                            let fp = BarFingerprint::from(bar);
                            if let Some(tracker) = trackers.get_mut(symbol) {
                                let prev_fp = tracker.last_fingerprint.clone();
                                let res = tracker.record_probe(
                                    &probe_time.format("%Y-%m-%d %H:%M:%S%.3f").to_string(),
                                    elapsed_ms,
                                    fp.clone(),
                                );

                                if res.became_candidate_final {
                                    info!(
                                        "⚡ [Candidate Final] 品种: {} | 延迟: {:.1}s | 指纹: {}",
                                        symbol, elapsed_ms as f64 / 1000.0, fp.signature()
                                    );
                                }
                                if res.became_false_final {
                                    warn!(
                                        "🚨 [False Final / 晚修改] 品种: {} | 候选延迟: {:?}ms | 晚修订延迟: {}ms | 旧指纹: {:?} -> 新指纹: {}",
                                        symbol, tracker.candidate_delay_ms, elapsed_ms, prev_fp.map(|p| p.signature()), fp.signature()
                                    );
                                }

                                // 仅在发生 revision 或是首次探针时保留 raw_response，节省存储
                                let raw_to_save = if res.is_revision || probe_index == 0 {
                                    Some(resp.raw_text.clone())
                                } else {
                                    None
                                };

                                let obs_record = ObservationRecord {
                                    id: None,
                                    symbol: symbol.clone(),
                                    bar_ts: bar_ts.to_string(),
                                    observed_at: probe_time.format("%Y-%m-%d %H:%M:%S%.3f").to_string(),
                                    elapsed_ms,
                                    probe_index,
                                    open: bar.open,
                                    high: bar.high,
                                    low: bar.low,
                                    close: bar.close,
                                    volume: bar.volume,
                                    hold: bar.hold,
                                    fingerprint: fp.signature(),
                                    session_type: tracker.session_type.as_str().to_string(),
                                    is_revision: res.is_revision,
                                    raw_response: raw_to_save,
                                };

                                if let Err(e) = insert_bar_observation(&self.db, &obs_record).await {
                                    error!("写入 bar_observation 失败: {e:?}");
                                }

                                let trial = tracker.to_trial(
                                    probe_index == total_probes - 1,
                                    &probe_time.format("%Y-%m-%d %H:%M:%S%.3f").to_string(),
                                );
                                if let Err(e) = upsert_finality_trial(&self.db, &trial).await {
                                    error!("更新 finality_trial 失败: {e:?}");
                                }
                            }
                        }
                    }
                    Err(e) => {
                        warn!("探测失败 [{}] 品种 {}: {e:?}", bar_ts, symbol);
                    }
                }
            }

            if probe_index < total_probes - 1 {
                sleep(Duration::from_secs(self.config.probe_interval_secs)).await;
            }
        }

        info!(
            "🏁 [{}] 观测会话完成 (T+{}s) | 哨兵品种已全部定版落盘",
            bar_ts, self.config.observe_duration_secs
        );
    }
}

/// 检查某个时刻是否为有效的 5m 交易结束边界（涵盖普通盘中与所有收盘边界）。
pub fn is_valid_5m_trading_boundary(dt: &DateTime<Local>) -> bool {
    let weekday = dt.weekday().num_days_from_monday();
    let t = dt.time();

    // 周六仅凌晨 02:30 前允许夜盘收盘
    if weekday == 5 {
        return t <= NaiveTime::from_hms_opt(2, 30, 0).unwrap();
    }
    // 周日全天无交易
    if weekday == 6 {
        return false;
    }

    let h = dt.hour();
    let m = dt.minute();

    // 日盘 09:05 ~ 10:15
    if (h == 9 && m >= 5) || (h == 10 && m <= 15) {
        return true;
    }
    // 日盘 10:35 ~ 11:30
    if (h == 10 && m >= 35) || (h == 11 && m <= 30) {
        return true;
    }
    // 日盘 13:35 ~ 15:00
    if (h == 13 && m >= 35) || h == 14 || (h == 15 && m == 0) {
        return true;
    }
    // 夜盘 21:05 ~ 23:00 / 23:30
    if (h == 21 && m >= 5) || h == 22 || (h == 23 && m <= 30) {
        return true;
    }
    // 深夜盘 00:05 ~ 02:30 (有色金属/贵金属/原油)
    if (h == 0 && m >= 5) || h == 1 || (h == 2 && m <= 30) {
        return true;
    }

    false
}

#[cfg(test)]
mod tests {
    use super::*;

    fn dt(y: i32, m: u32, d: u32, h: u32, min: u32) -> DateTime<Local> {
        chrono::NaiveDate::from_ymd_opt(y, m, d)
            .unwrap()
            .and_hms_opt(h, min, 0)
            .unwrap()
            .and_local_timezone(Local)
            .unwrap()
    }

    #[test]
    fn test_valid_5m_boundaries() {
        // 10:15 / 11:30 / 15:00 收盘边界必须为 true
        assert!(is_valid_5m_trading_boundary(&dt(2026, 8, 3, 10, 15)));
        assert!(is_valid_5m_trading_boundary(&dt(2026, 8, 3, 11, 30)));
        assert!(is_valid_5m_trading_boundary(&dt(2026, 8, 3, 15, 0)));

        // 普通 5m
        assert!(is_valid_5m_trading_boundary(&dt(2026, 8, 3, 9, 30)));
        assert!(is_valid_5m_trading_boundary(&dt(2026, 8, 3, 14, 55)));
        assert!(is_valid_5m_trading_boundary(&dt(2026, 8, 3, 21, 5)));

        // 午休/休市时段必须为 false
        assert!(!is_valid_5m_trading_boundary(&dt(2026, 8, 3, 10, 20)));
        assert!(!is_valid_5m_trading_boundary(&dt(2026, 8, 3, 11, 35)));
        assert!(!is_valid_5m_trading_boundary(&dt(2026, 8, 3, 12, 0)));
        assert!(!is_valid_5m_trading_boundary(&dt(2026, 8, 3, 15, 5)));
        assert!(!is_valid_5m_trading_boundary(&dt(2026, 8, 3, 18, 0)));

        // 周末为 false
        assert!(!is_valid_5m_trading_boundary(&dt(2026, 8, 9, 10, 0))); // 周日
    }
}
