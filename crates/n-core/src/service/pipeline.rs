//! Atomic Raw 5m to Derived Pipeline.
//!
//! 提供统一的行情写入与派生原子入口（Issue 02 核心要求）：
//! 1. Per-Symbol 串行化互斥锁，保证同一品种的更新不出现并发交叉与老版本覆盖新版本；
//! 2. 单事务原子写入：Raw 5m 与受影响的 Derived 15m/60m 在单事务内完成替换，避免中间状态泄漏；
//! 3. 统一入口 `process_raw_batch` 与 `process_final_bar`，收敛定时刷新、延迟补拉与未来 Finality 落地。

use std::collections::{BTreeMap, HashMap};
use std::sync::{Arc, Mutex};

use anyhow::{Context, Result};
use sea_orm::DatabaseConnection;
use tokio::sync::Mutex as AsyncMutex;

use crate::derive::{aggregate, Timeframe};
use crate::fetch::kline::Kline;
use crate::storage::repo;
use super::{fetch_to_model, model_to_fetch};

/// 品种级更新互斥锁管理器（Per-Symbol Lock）。
/// 保证同一时刻对同一品种的数据写入与派生重算是串行的，
/// 不同品种之间完全并行无锁阻塞。
#[derive(Clone, Default)]
pub struct SymbolLocks {
    locks: Arc<Mutex<HashMap<String, Arc<AsyncMutex<()>>>>>,
}

impl SymbolLocks {
    pub fn new() -> Self {
        Self::default()
    }

    /// 获取指定品种的代码级异步锁句柄。
    pub fn get(&self, symbol: &str) -> Arc<AsyncMutex<()>> {
        let mut map = self.locks.lock().expect("SymbolLocks mutex poisoned");
        map.entry(symbol.to_string())
            .or_insert_with(|| Arc::new(AsyncMutex::new(())))
            .clone()
    }
}

/// 统一行情写入与派生管道。
#[derive(Clone)]
pub struct RawPipeline {
    db: DatabaseConnection,
    symbol_locks: SymbolLocks,
    judger: crate::finality::FinalityJudger,
}

impl RawPipeline {
    pub fn new(db: DatabaseConnection, symbol_locks: SymbolLocks) -> Self {
        Self {
            db,
            symbol_locks,
            judger: crate::finality::FinalityJudger::default(),
        }
    }

    pub fn with_judger(
        db: DatabaseConnection,
        symbol_locks: SymbolLocks,
        judger: crate::finality::FinalityJudger,
    ) -> Self {
        Self {
            db,
            symbol_locks,
            judger,
        }
    }

    /// 获取底层 FinalityJudger 引用。
    pub fn judger(&self) -> &crate::finality::FinalityJudger {
        &self.judger
    }

    /// 获取底层 symbol_locks 引用。
    pub fn symbol_locks(&self) -> &SymbolLocks {
        &self.symbol_locks
    }

    /// 统一批量原始 5m 行情更新入口。
    ///
    /// 步骤：
    /// 1. 获取目标品种的排他锁；
    /// 2. 读取 SQLite 中已有的全部 raw 5m 并与传入增量在内存中按时间戳合并；
    /// 3. 基于最新完整 raw 5m 重新聚合生成 15m 与 60m derived K 线；
    /// 4. 在单一数据库事务中原子完成：Raw upsert + 旧 Derived 删除 + 新 Derived 写入；
    /// 5. 返回本次写入/更新生成的派生 K 线总数。
    pub async fn process_raw_batch(&self, symbol: &str, incoming_bars: &[Kline]) -> Result<usize> {
        let lock = self.symbol_locks.get(symbol);
        let _guard = lock.lock().await;

        let raw_models: Vec<_> = incoming_bars
            .iter()
            .map(|k| fetch_to_model(symbol, "5m", "raw", k))
            .collect();

        // 读取已有 raw 并与新增量合并
        let existing = repo::raw_klines(&self.db, symbol).await?;
        let mut merged_bars: Vec<Kline> = existing.iter().map(model_to_fetch).collect();
        merge_klines(&mut merged_bars, incoming_bars);

        let mut derived_models = Vec::new();
        if merged_bars.len() >= 3 {
            for tf in [Timeframe::M15, Timeframe::M60] {
                for k in aggregate(&merged_bars, tf) {
                    derived_models.push(fetch_to_model(symbol, tf.as_str(), "derived", &k));
                }
            }
        }

        let derived_count = derived_models.len();
        repo::atomically_save_raw_and_derived(&self.db, symbol, raw_models, derived_models)
            .await
            .context("RawPipeline::process_raw_batch 原子事务写入失败")?;

        Ok(derived_count)
    }

    /// 单根 Final 5m 行情更新入口（未来 Finality 确认后的统一持久化入口）。
    pub async fn process_final_bar(&self, symbol: &str, bar: &Kline) -> Result<usize> {
        self.process_raw_batch(symbol, std::slice::from_ref(bar)).await
    }

    /// 显式全量重构指定品种的 Derived 15m/60m（在 symbol 锁与单事务内执行）。
    pub async fn rebuild_derived(&self, symbol: &str) -> Result<usize> {
        let lock = self.symbol_locks.get(symbol);
        let _guard = lock.lock().await;

        let raw = repo::raw_klines(&self.db, symbol).await?;
        if raw.len() < 3 {
            repo::delete_derived_klines(&self.db, symbol).await?;
            return Ok(0);
        }

        let bars: Vec<Kline> = raw.iter().map(model_to_fetch).collect();
        let mut derived_models = Vec::new();
        for tf in [Timeframe::M15, Timeframe::M60] {
            for k in aggregate(&bars, tf) {
                derived_models.push(fetch_to_model(symbol, tf.as_str(), "derived", &k));
            }
        }

        let derived_count = derived_models.len();
        repo::atomically_save_raw_and_derived(&self.db, symbol, Vec::new(), derived_models)
            .await
            .context("RawPipeline::rebuild_derived 事务写入失败")?;

        Ok(derived_count)
    }
}

/// 按时间戳合并并去重 K 线（最新版本覆盖旧版本，按时间正序排列）。
fn merge_klines(existing: &mut Vec<Kline>, incoming: &[Kline]) {
    let mut map: BTreeMap<String, Kline> = BTreeMap::new();
    for k in existing.drain(..) {
        map.insert(k.datetime.clone(), k);
    }
    for k in incoming {
        map.insert(k.datetime.clone(), k.clone());
    }
    *existing = map.into_values().collect();
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn symbol_locks_serializes_same_symbol_concurrent_updates() {
        let db = crate::storage::connect(std::path::Path::new(":memory:"))
            .await
            .unwrap();
        let pipeline = RawPipeline::new(db.clone(), SymbolLocks::new());

        let bar1 = Kline {
            datetime: "2026-08-28 10:35:00".to_string(),
            open: 100.0,
            high: 105.0,
            low: 99.0,
            close: 101.0,
            volume: 10.0,
            hold: 50.0,
        };
        let bar2 = Kline {
            datetime: "2026-08-28 10:40:00".to_string(),
            open: 101.0,
            high: 106.0,
            low: 100.0,
            close: 102.0,
            volume: 10.0,
            hold: 50.0,
        };
        let bar3 = Kline {
            datetime: "2026-08-28 10:45:00".to_string(),
            open: 102.0,
            high: 108.0,
            low: 101.0,
            close: 107.0,
            volume: 10.0,
            hold: 50.0,
        };

        // 模拟多个并发任务同时写入不同 bar
        let p1 = pipeline.clone();
        let p2 = pipeline.clone();
        let p3 = pipeline.clone();

        let t1 = tokio::spawn(async move {
            p1.process_raw_batch("RB0", &[bar1]).await.unwrap();
        });
        let t2 = tokio::spawn(async move {
            p2.process_raw_batch("RB0", &[bar2]).await.unwrap();
        });
        let t3 = tokio::spawn(async move {
            p3.process_raw_batch("RB0", &[bar3]).await.unwrap();
        });

        let _ = tokio::try_join!(t1, t2, t3).unwrap();

        // 验证并发写入完成后，3 根 raw 5m 全部存在且派生了正确的 15m
        let raw_rows = repo::raw_klines(&db, "RB0").await.unwrap();
        assert_eq!(raw_rows.len(), 3);

        let derived_15m = repo::klines(&db, "RB0", "15m", None, None).await.unwrap();
        assert_eq!(derived_15m.len(), 1);
        assert_eq!(derived_15m[0].close, 107.0);
        assert_eq!(derived_15m[0].high, 108.0);
    }

    #[tokio::test]
    async fn process_final_bar_single_bar_update() {
        let db = crate::storage::connect(std::path::Path::new(":memory:"))
            .await
            .unwrap();
        let pipeline = RawPipeline::new(db.clone(), SymbolLocks::new());

        let bar1 = Kline {
            datetime: "2026-08-28 10:45:00".to_string(),
            open: 100.0,
            high: 105.0,
            low: 99.0,
            close: 101.0,
            volume: 10.0,
            hold: 50.0,
        };
        pipeline.process_final_bar("CJ0", &bar1).await.unwrap();

        let raw = repo::raw_klines(&db, "CJ0").await.unwrap();
        assert_eq!(raw.len(), 1);
        assert_eq!(raw[0].close, 101.0);
    }
}
