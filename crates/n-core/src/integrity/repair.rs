//! Raw 5m Data Hole Self-Healing and Gap Repair.
//!
//! 实现缺洞自愈修复管道（Issue 05）：
//! 1. 扫描检测报告中的可恢复数据洞（Recoverable Gaps）；
//! 2. 通过共享 `SinaClient` 重新获取包含该区间的原始 K 线批次；
//! 3. 接入 `RawPipeline::process_raw_batch` 进行原子入库与受影响 Derived K 线的同步重算；
//! 4. 自动复检修复效果，生成修复前后对比报告。

use anyhow::{Context, Result};
use chrono::NaiveDateTime;
use sea_orm::DatabaseConnection;
use serde::{Deserialize, Serialize};

use crate::fetch::datasource::MarketDataSource;
use crate::fetch::HybridDataSource;
#[allow(unused_imports)]
use crate::fetch::SinaClient; // for test wrapper only
#[allow(unused_imports)] use crate::fetch::SinaClient as _SinaClient2;
use crate::service::pipeline::RawPipeline;
use super::checker::RawDataIntegrityChecker;

/// 单品种缺洞修复结果统计。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RepairResult {
    pub symbol: String,
    pub initial_missing: usize,
    pub repaired_count: usize,
    pub remaining_missing: usize,
    pub is_fully_repaired: bool,
    pub message: String,
}

pub struct IntegrityRepairer;

impl IntegrityRepairer {
    /// 自动对单个品种执行缺洞修复与复检。
    pub async fn repair_symbol(
        db: &DatabaseConnection,
        pipeline: &RawPipeline,
        hybrid: &HybridDataSource,
        symbol: &str,
        max_api_window_bars: usize,
    ) -> Result<RepairResult> {
        let initial_report = RawDataIntegrityChecker::inspect_symbol(db, symbol, max_api_window_bars).await?;

        if initial_report.missing_count == 0 {
            return Ok(RepairResult {
                symbol: symbol.to_string(),
                initial_missing: 0,
                repaired_count: 0,
                remaining_missing: 0,
                is_fully_repaired: true,
                message: "数据原本完整，无需修复".to_string(),
            });
        }

        let recoverable_gaps: Vec<_> = initial_report
            .missing_gaps
            .iter()
            .filter(|g| g.recoverable_by_api)
            .collect();

        if recoverable_gaps.is_empty() {
            return Ok(RepairResult {
                symbol: symbol.to_string(),
                initial_missing: initial_report.missing_count,
                repaired_count: 0,
                remaining_missing: initial_report.missing_count,
                is_fully_repaired: false,
                message: "缺失区间已超出新浪接口回溯窗口（永久历史缺口），无法自动恢复".to_string(),
            });
        }

        // 计算需要回溯的 K 线总根数：从当前最新 K 线到最旧一个可恢复缺口的起始时间
        let latest_dt = initial_report
            .latest_ts
            .as_deref()
            .and_then(|ts| NaiveDateTime::parse_from_str(ts, "%Y-%m-%d %H:%M:%S").ok());

        let oldest_gap_start = recoverable_gaps
            .iter()
            .filter_map(|g| NaiveDateTime::parse_from_str(&g.start_ts, "%Y-%m-%d %H:%M:%S").ok())
            .min();

        let needed_count = match (latest_dt, oldest_gap_start) {
            (Some(latest), Some(oldest)) => {
                let span_mins = (latest - oldest).num_minutes().max(0);
                // 白屏关联修复：旧公式 +20 对于跨日缺口(21:15~00:05)仅算50根，可能刚好卡在边界导致 fetched 未覆盖 gap
                // 改为 + missing_count*2 +30 冗余，确保 fetched 区间一定覆盖最旧缺口
                let raw = (span_mins / 5 + initial_report.missing_count as i64 * 2 + 30) as usize;
                raw.clamp(50, max_api_window_bars)
            }
            _ => max_api_window_bars,
        };

                tracing::info!(
            "🔧 [{symbol}] 启动数据缺洞自愈: 发现 {} 个可恢复缺口 (共 {} 根缺失)，请求回补 {} 根 5m",
            recoverable_gaps.len(),
            initial_report.missing_count,
            needed_count
        );
        for g in &recoverable_gaps {
            tracing::info!(
                "   ↳ 缺口明细 [{}]: {} ~ {} 缺{}根 {}",
                symbol,
                g.start_ts,
                g.end_ts,
                g.missing_count,
                if g.recoverable_by_api { "可恢复" } else { "不可恢复" }
            );
        }
        if initial_report.missing_gaps.len() > recoverable_gaps.len() {
            for g in &initial_report.missing_gaps {
                if !g.recoverable_by_api {
                    tracing::warn!(
                        "   ↳ 永久缺口(超出API窗口) [{}]: {} ~ {} 缺{}根",
                        symbol, g.start_ts, g.end_ts, g.missing_count
                    );
                }
            }
        }

        // 重新拉取对应跨度的 5m K线：优先走当前主力（天勤优先，新浪兜底），与日常 Hybrid 保持一致
        let fetch_source = if hybrid.tq_is_available() { "天勤(Hybrid→tqsdk)" } else { "新浪(Hybrid→sina)" };
        tracing::info!("📡 [{symbol}] 补全数据源: {} (tq_available={})", fetch_source, hybrid.tq_is_available());
        let fetched = hybrid.fetch_minute(symbol, "5", needed_count)
            .await
            .context("缺洞修复抓取失败")?;
        tracing::info!(
            "📥 [{symbol}] 回补抓取完成: 返回 {} 根 5m | 区间 {} ~ {} | 请求 {} 根",
            fetched.len(),
            fetched.first().map(|k| k.datetime.as_str()).unwrap_or("-"),
            fetched.last().map(|k| k.datetime.as_str()).unwrap_or("-"),
            needed_count
        );
        tracing::info!(
            "   ↳ 实际使用: {} | fetched_len={}",
            fetch_source, fetched.len()
        );

        // 接入 RawPipeline 原子落库并重新派生受影响的 15m/60m
        pipeline
            .process_raw_batch(symbol, &fetched)
            .await
            .context("缺洞修复数据写入原子管道失败")?;

        // 复检修复后的状态
        let post_report = RawDataIntegrityChecker::inspect_symbol(db, symbol, max_api_window_bars).await?;
        let repaired = initial_report.missing_count.saturating_sub(post_report.missing_count);
        let fully_repaired = post_report.missing_count == 0;
        tracing::info!(
            "📊 [{symbol}] 自愈复检: {} | 初始缺{}  repaired={} 剩余缺{} 全量修复={}",
            if fully_repaired { "✅ 全部补齐" } else if repaired>0 { "⚠️ 部分补齐" } else { "❌ 未补到" },
            initial_report.missing_count,
            repaired,
            post_report.missing_count,
            fully_repaired
        );
        if !post_report.missing_gaps.is_empty() {
            for g in &post_report.missing_gaps {
                tracing::warn!(
                    "   ↳ 仍缺 [{}]: {} ~ {} 缺{}根 {}",
                    symbol, g.start_ts, g.end_ts, g.missing_count,
                    if g.recoverable_by_api { "可恢复" } else { "永久缺口" }
                );
            }
        }
        if repaired == 0 && !fully_repaired {
            tracing::warn!(
                "⚠️ [{symbol}] 抓取区间 {} ~ {} 未覆盖缺口 {} ~ {} 或被Finality过滤，请检查fetch_minute返回 | needed={} fetched={}",
                fetched.first().map(|k| k.datetime.as_str()).unwrap_or("-"),
                fetched.last().map(|k| k.datetime.as_str()).unwrap_or("-"),
                recoverable_gaps.first().map(|g| g.start_ts.as_str()).unwrap_or("-"),
                recoverable_gaps.first().map(|g| g.end_ts.as_str()).unwrap_or("-"),
                needed_count,
                fetched.len()
            );
        }

        let message = if fully_repaired {
            format!("成功补齐全部 {} 根缺失 K 线", repaired)
        } else if repaired > 0 {
            format!("成功补齐 {} 根，仍有 {} 根属于永久历史缺口", repaired, post_report.missing_count)
        } else {
            "接口未返回所需历史区间，未能修复缺口".to_string()
        };

        Ok(RepairResult {
            symbol: symbol.to_string(),
            initial_missing: initial_report.missing_count,
            repaired_count: repaired,
            remaining_missing: post_report.missing_count,
            is_fully_repaired: fully_repaired,
            message,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::service::pipeline::SymbolLocks;
    use crate::fetch::kline::Kline;

    #[tokio::test]
    async fn test_repair_symbol_already_clean() {
        let db = crate::storage::connect(std::path::Path::new(":memory:"))
            .await
            .unwrap();
        let pipeline = RawPipeline::new(db.clone(), SymbolLocks::new());
        let cfg = crate::config::Config::default();
        let hybrid = HybridDataSource::new(
            crate::fetch::tq_client::TqBridgeClient::with_port(cfg.data_source.bridge_port),
            crate::fetch::SinaClient::global(),
            std::sync::Arc::new(tokio::sync::RwLock::new(cfg)),
        );

        let bar1 = Kline {
            datetime: "2026-08-28 09:05:00".to_string(),
            open: 100.0,
            high: 105.0,
            low: 99.0,
            close: 101.0,
            volume: 10.0,
            hold: 50.0,
        };
        pipeline.process_raw_batch("RB0", &[bar1]).await.unwrap();

        let res = IntegrityRepairer::repair_symbol(&db, &pipeline, &hybrid, "RB0", 1000)
            .await
            .unwrap();
        assert!(res.is_fully_repaired);
        assert_eq!(res.initial_missing, 0);
    }
}









