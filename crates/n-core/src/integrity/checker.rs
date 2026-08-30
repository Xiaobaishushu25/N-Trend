//! Raw 5m Data Integrity Checker.
//!
//! 提供针对本地 SQLite 中 Raw 5m 历史数据的完整性检测（Issue 05）：
//! 1. 检测盘中及会话内的缺失 K 线（Missing Bars / Data Holes），并识别是否可由 API 自动补齐；
//! 2. 检测非法时段误入库的 K 线（Unexpected Bars / 幽灵K线）；
//! 3. 检测价格畸变或数值非法的 K 线（Corrupted Bars）。

use anyhow::{Context, Result};
use chrono::{Duration, NaiveDateTime};
use sea_orm::DatabaseConnection;
use serde::{Deserialize, Serialize};

use crate::storage::repo;
use super::schedule::{is_valid_5m_slot, next_expected_5m_slot};

/// 缺失 K 线连续区间。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct GapRange {
    pub start_ts: String,
    pub end_ts: String,
    pub missing_count: usize,
    pub recoverable_by_api: bool,
}

/// 单品种 Raw 5m 数据完整性体检报告。
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct SymbolIntegrityReport {
    pub symbol: String,
    pub total_bars: usize,
    pub first_ts: Option<String>,
    pub latest_ts: Option<String>,
    pub missing_count: usize,
    pub missing_gaps: Vec<GapRange>,
    pub unexpected_bars: Vec<String>,
    pub corrupted_bars: Vec<String>,
    pub is_clean: bool,
}

impl SymbolIntegrityReport {
    pub fn format_summary(&self) -> String {
        if self.is_clean {
            format!(
                "✅ [{}] 数据完整 | 共 {} 根 5m ({} ~ {})",
                self.symbol,
                self.total_bars,
                self.first_ts.as_deref().unwrap_or("-"),
                self.latest_ts.as_deref().unwrap_or("-")
            )
        } else {
            let recoverable_count: usize = self
                .missing_gaps
                .iter()
                .filter(|g| g.recoverable_by_api)
                .map(|g| g.missing_count)
                .sum();
            format!(
                "⚠️ [{}] 发现数据异常 | 总数: {} | 缺失: {} 根 (可恢复: {} 根, {} 个缺口) | 非法时段: {} | 畸变: {}",
                self.symbol,
                self.total_bars,
                self.missing_count,
                recoverable_count,
                self.missing_gaps.len(),
                self.unexpected_bars.len(),
                self.corrupted_bars.len()
            )
        }
    }
}

pub struct RawDataIntegrityChecker;

impl RawDataIntegrityChecker {
    /// 检查指定品种的 Raw 5m 数据完整性。
    ///
    /// - `max_api_window_bars`: 新浪 API 最大可回溯的 5m 根数（通常为 1000 根），
    ///   用于判断数据洞是否属于可自愈修复范围。
    pub async fn inspect_symbol(
        db: &DatabaseConnection,
        symbol: &str,
        max_api_window_bars: usize,
    ) -> Result<SymbolIntegrityReport> {
        let rows = repo::raw_klines(db, symbol)
            .await
            .context("查询 Raw 5m 数据失败")?;

        if rows.is_empty() {
            return Ok(SymbolIntegrityReport {
                symbol: symbol.to_string(),
                total_bars: 0,
                first_ts: None,
                latest_ts: None,
                missing_count: 0,
                missing_gaps: Vec::new(),
                unexpected_bars: Vec::new(),
                corrupted_bars: Vec::new(),
                is_clean: true,
            });
        }

        let first_ts = rows.first().map(|r| r.ts.clone());
        let latest_ts = rows.last().map(|r| r.ts.clone());

        let mut unexpected_bars = Vec::new();
        let mut corrupted_bars = Vec::new();
        let mut parsed_bars = Vec::with_capacity(rows.len());

        for row in &rows {
            // 1. 检查数据畸变
            if row.open <= 0.0
                || row.high <= 0.0
                || row.low <= 0.0
                || row.close <= 0.0
                || row.high < row.low
                || row.volume < 0.0
            {
                corrupted_bars.push(row.ts.clone());
            }

            // 2. 解析时间戳并检查合法时段
            if let Ok(dt) = NaiveDateTime::parse_from_str(&row.ts, "%Y-%m-%d %H:%M:%S") {
                if !is_valid_5m_slot(symbol, &dt) {
                    unexpected_bars.push(row.ts.clone());
                }
                parsed_bars.push(dt);
            } else {
                corrupted_bars.push(row.ts.clone());
            }
        }

        // 3. 检测中间缺失的 5m 数据洞（Missing Bars）
        let mut missing_slots: Vec<NaiveDateTime> = Vec::new();

        for i in 0..parsed_bars.len().saturating_sub(1) {
            let cur = parsed_bars[i];
            let next = parsed_bars[i + 1];

            // 仅当前后两根不是同一时间戳时检查（防重）
            if next <= cur {
                continue;
            }

            // 如果两根 K 线跨度在同一天，或者跨度在常规跨日范围内（< 4天）
            // 计算理论上在 cur 与 next 之间的所有合法 5m 槽位
            let mut expected = match next_expected_5m_slot(symbol, cur) {
                Some(e) => e,
                None => continue,
            };

            // 防止异常过大跳跃（例如跨几个月合约停牌）导致循环失控，限制单次缺口上限为 1500 根
            let mut gap_hops = 0;
            while expected < next && gap_hops < 1500 {
                gap_hops += 1;
                if is_valid_5m_slot(symbol, &expected) {
                    missing_slots.push(expected);
                }
                match next_expected_5m_slot(symbol, expected) {
                    Some(nxt) if nxt > expected => expected = nxt,
                    _ => break,
                }
            }
        }

        // 4. 将离散缺失槽位聚合成连续区间 GapRange，并判定是否可被 API 恢复
        let latest_dt = parsed_bars.last().copied();
        let api_reach_limit = latest_dt.map(|ldt| {
            ldt - Duration::minutes((max_api_window_bars as i64) * 5)
        });

        let missing_count = missing_slots.len();
        let missing_gaps = group_missing_slots(&missing_slots, symbol, api_reach_limit);

        let is_clean = missing_count == 0 && unexpected_bars.is_empty() && corrupted_bars.is_empty();

        Ok(SymbolIntegrityReport {
            symbol: symbol.to_string(),
            total_bars: rows.len(),
            first_ts,
            latest_ts,
            missing_count,
            missing_gaps,
            unexpected_bars,
            corrupted_bars,
            is_clean,
        })
    }
}

/// 将离散的缺失槽位按连续性聚合成 GapRange。
fn group_missing_slots(
    slots: &[NaiveDateTime],
    symbol: &str,
    api_reach_limit: Option<NaiveDateTime>,
) -> Vec<GapRange> {
    if slots.is_empty() {
        return Vec::new();
    }

    let mut ranges = Vec::new();
    let mut cur_start = slots[0];
    let mut cur_end = slots[0];
    let mut count = 1;

    for &slot in &slots[1..] {
        let expected = next_expected_5m_slot(symbol, cur_end);
        if expected == Some(slot) {
            cur_end = slot;
            count += 1;
        } else {
            // 连续段断开，归档前一段
            let recoverable = api_reach_limit.map_or(true, |limit| cur_start >= limit);
            ranges.push(GapRange {
                start_ts: cur_start.format("%Y-%m-%d %H:%M:%S").to_string(),
                end_ts: cur_end.format("%Y-%m-%d %H:%M:%S").to_string(),
                missing_count: count,
                recoverable_by_api: recoverable,
            });
            cur_start = slot;
            cur_end = slot;
            count = 1;
        }
    }

    // 归档最后一段
    let recoverable = api_reach_limit.map_or(true, |limit| cur_start >= limit);
    ranges.push(GapRange {
        start_ts: cur_start.format("%Y-%m-%d %H:%M:%S").to_string(),
        end_ts: cur_end.format("%Y-%m-%d %H:%M:%S").to_string(),
        missing_count: count,
        recoverable_by_api: recoverable,
    });

    ranges
}

#[cfg(test)]
mod tests {
    use super::*;
    use sea_orm::Set;
    use crate::storage::entities::klines;

    fn make_row(symbol: &str, ts: &str, close: f64) -> klines::ActiveModel {
        klines::ActiveModel {
            symbol: Set(symbol.to_string()),
            timeframe: Set("5m".to_string()),
            ts: Set(ts.to_string()),
            open: Set(100.0),
            high: Set(105.0),
            low: Set(99.0),
            close: Set(close),
            volume: Set(10.0),
            hold: Set(50.0),
            source: Set("raw".to_string()),
        }
    }

    #[tokio::test]
    async fn test_integrity_checker_clean_data() {
        let db = crate::storage::connect(std::path::Path::new(":memory:"))
            .await
            .unwrap();

        // 连续 3 根 5m
        let rows = vec![
            make_row("RB0", "2026-08-28 09:05:00", 100.0),
            make_row("RB0", "2026-08-28 09:10:00", 101.0),
            make_row("RB0", "2026-08-28 09:15:00", 102.0),
        ];
        repo::upsert_klines(&db, rows).await.unwrap();

        let report = RawDataIntegrityChecker::inspect_symbol(&db, "RB0", 1000).await.unwrap();
        assert!(report.is_clean);
        assert_eq!(report.missing_count, 0);
        assert_eq!(report.total_bars, 3);
        assert!(report.missing_gaps.is_empty());
    }

    #[tokio::test]
    async fn test_integrity_checker_detects_middle_hole() {
        let db = crate::storage::connect(std::path::Path::new(":memory:"))
            .await
            .unwrap();

        // 缺失 09:10:00 与 09:15:00
        let rows = vec![
            make_row("RB0", "2026-08-28 09:05:00", 100.0),
            make_row("RB0", "2026-08-28 09:20:00", 102.0),
        ];
        repo::upsert_klines(&db, rows).await.unwrap();

        let report = RawDataIntegrityChecker::inspect_symbol(&db, "RB0", 1000).await.unwrap();
        assert!(!report.is_clean);
        assert_eq!(report.missing_count, 2);
        assert_eq!(report.missing_gaps.len(), 1);
        assert_eq!(report.missing_gaps[0].start_ts, "2026-08-28 09:10:00");
        assert_eq!(report.missing_gaps[0].end_ts, "2026-08-28 09:15:00");
        assert!(report.missing_gaps[0].recoverable_by_api);
    }

    #[tokio::test]
    async fn test_integrity_checker_detects_unexpected_and_corrupted() {
        let db = crate::storage::connect(std::path::Path::new(":memory:"))
            .await
            .unwrap();

        // 12:00:00 午休（非交易时段）
        let row1 = make_row("RB0", "2026-08-28 12:00:00", 100.0);
        // 畸变数据（close = 0）
        let mut row2 = make_row("RB0", "2026-08-28 09:05:00", 0.0);
        row2.close = Set(0.0);

        repo::upsert_klines(&db, vec![row1, row2]).await.unwrap();

        let report = RawDataIntegrityChecker::inspect_symbol(&db, "RB0", 1000).await.unwrap();
        assert!(!report.is_clean);
        assert_eq!(report.unexpected_bars.len(), 1);
        assert_eq!(report.unexpected_bars[0], "2026-08-28 12:00:00");
        assert_eq!(report.corrupted_bars.len(), 1);
        assert_eq!(report.corrupted_bars[0], "2026-08-28 09:05:00");
    }
}
