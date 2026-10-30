//! Atomic Raw 5m to Derived Pipeline.
//!
//! 提供统一的行情写入与派生原子入口（Issue 02 核心要求）：
//! 1. Per-Symbol 串行化互斥锁，保证同一品种的更新不出现并发交叉与老版本覆盖新版本；
//! 2. 单事务原子写入：Raw 5m 与受影响的 Derived 15m/60m 在单事务内完成替换，避免中间状态泄漏；
//! 3. 统一入口 `process_raw_batch` 与 `process_final_bar`，收敛定时刷新、延迟补拉与未来 Finality 落地。

use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::{Arc, Mutex};

use anyhow::{Context, Result};
use chrono::NaiveDateTime;
use sea_orm::DatabaseConnection;
use tokio::sync::Mutex as AsyncMutex;

use super::{fetch_to_model, model_to_fetch};
use crate::derive::{aggregate, Timeframe};
use crate::fetch::kline::Kline;
use crate::storage::repo;

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
    /// 2. 比较传入窗口，只处理新增或修正的 raw 5m；重复补拉不写库；
    /// 3. 读取受影响小时附近的切片，重算受影响 15m/60m 桶；
    /// 4. 单事务 upsert raw 与这些派生桶，保留其他历史与修订号；
    /// 5. 派生缓存缺失时才初始化全部派生历史。
    pub async fn process_raw_batch(&self, symbol: &str, incoming_bars: &[Kline]) -> Result<usize> {
        let lock = self.symbol_locks.get(symbol);
        let _guard = lock.lock().await;

        if incoming_bars.is_empty() { return Ok(0); }
        let mut incoming = BTreeMap::new();
        for row in incoming_bars {
            NaiveDateTime::parse_from_str(&row.datetime, "%Y-%m-%d %H:%M:%S")
                .context("原始K线时间无效")?;
            incoming.insert(row.datetime.as_str(), row);
        }
        let start = *incoming.keys().next().unwrap();
        let end = *incoming.keys().next_back().unwrap();
        let previous = repo::raw_klines_window(&self.db, symbol, start, end).await?;
        let previous: HashMap<_, _> = previous.iter().map(|row| (row.ts.as_str(), row)).collect();
        let changed: Vec<Kline> = incoming.values().filter(|row| {
            !previous.get(row.datetime.as_str()).is_some_and(|old| {
                old.open == row.open && old.high == row.high && old.low == row.low
                    && old.close == row.close && old.volume == row.volume && old.hold == row.hold
            })
        }).map(|row| (*row).clone()).collect();
        let initialize = repo::klines(&self.db, symbol, "15m", Some(1), None).await?.is_empty()
            || repo::klines(&self.db, symbol, "60m", Some(1), None).await?.is_empty();
        if changed.is_empty() && !initialize { return Ok(0); }
        let existing = if initialize {
            repo::raw_klines(&self.db, symbol).await?
        } else {
            let first = NaiveDateTime::parse_from_str(&changed[0].datetime, "%Y-%m-%d %H:%M:%S")? - chrono::Duration::minutes(60);
            let last = NaiveDateTime::parse_from_str(&changed.last().unwrap().datetime, "%Y-%m-%d %H:%M:%S")? + chrono::Duration::minutes(60);
            repo::raw_klines_window(&self.db, symbol, &first.format("%Y-%m-%d %H:%M:%S").to_string(), &last.format("%Y-%m-%d %H:%M:%S").to_string()).await?
        };
        let mut merged_bars: Vec<Kline> = existing.iter().map(model_to_fetch).collect();
        merge_klines(&mut merged_bars, &changed);
        tracing::info!(
            target: "market_trace",
            layer = "raw_pipeline",
            symbol,
            incoming = incoming_bars.len(),
            changed = changed.len(),
            existing = existing.len(),
            merged = merged_bars.len(),
            "KLINE_PIPELINE"
        );
        trace_pipeline_anomalies(symbol, &merged_bars, &changed);

        let mut derived_models = Vec::new();
        if (initialize && merged_bars.len() >= 3) || (!initialize && !merged_bars.is_empty()) {
            for tf in [Timeframe::M15, Timeframe::M60] {
                let affected: HashSet<_> = changed.iter().map(|row| {
                    crate::derive::current_bucket_end_ts(
                        NaiveDateTime::parse_from_str(&row.datetime, "%Y-%m-%d %H:%M:%S").unwrap(), tf)
                }).collect();
                for k in aggregate(&merged_bars, tf) {
                    if initialize || affected.contains(&k.datetime) {
                        derived_models.push(fetch_to_model(symbol, tf.as_str(), "derived", &k));
                    }
                }
            }
        }

        let derived_count = derived_models.len();
        let raw_models = changed.iter().map(|k| fetch_to_model(symbol, "5m", "raw", k)).collect();
        repo::atomically_upsert_raw_and_derived(&self.db, symbol, raw_models, derived_models)
            .await
            .context("RawPipeline::process_raw_batch 原子事务写入失败")?;

        Ok(derived_count)
    }

    /// 单根 Final 5m 行情更新入口（未来 Finality 确认后的统一持久化入口）。
    pub async fn process_final_bar(&self, symbol: &str, bar: &Kline) -> Result<usize> {
        self.process_raw_batch(symbol, std::slice::from_ref(bar))
            .await
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

fn trace_pipeline_anomalies(symbol: &str, merged: &[Kline], incoming: &[Kline]) {
    let incoming_timestamps: HashSet<&str> = incoming.iter().map(|k| k.datetime.as_str()).collect();

    for pair in merged.windows(2) {
        let previous = &pair[0];
        let current = &pair[1];
        if !incoming_timestamps.contains(previous.datetime.as_str())
            && !incoming_timestamps.contains(current.datetime.as_str())
        {
            continue;
        }

        let price_gap_pct = (current.open - previous.close).abs() / previous.close.abs().max(1.0);
        let hold_change_pct = (current.hold - previous.hold).abs() / previous.hold.abs().max(1.0);
        if price_gap_pct >= 0.05 || hold_change_pct >= 0.20 {
            let timestamp_gap_minutes =
                NaiveDateTime::parse_from_str(&current.datetime, "%Y-%m-%d %H:%M:%S")
                    .ok()
                    .and_then(|current_ts| {
                        NaiveDateTime::parse_from_str(&previous.datetime, "%Y-%m-%d %H:%M:%S")
                            .ok()
                            .map(|previous_ts| (current_ts - previous_ts).num_minutes())
                    });
            tracing::info!(
                target: "market_trace",
                layer = "raw_pipeline",
                symbol,
                prev_ts = %previous.datetime,
                prev_close = previous.close,
                prev_hold = previous.hold,
                ts = %current.datetime,
                open = current.open,
                close = current.close,
                hold = current.hold,
                timestamp_gap_minutes = ?timestamp_gap_minutes,
                price_gap_pct,
                hold_change_pct,
                "KLINE_ANOMALY"
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample(ts: &str, price: f64) -> Kline {
        Kline { datetime: ts.into(), open: price, high: price+1., low: price-1., close: price,
            volume: 10., hold: 50. }
    }

    async fn assert_full_aggregation(db: &DatabaseConnection, raw: &[Kline]) {
        for tf in [Timeframe::M15, Timeframe::M60] {
            let expected = aggregate(raw, tf);
            // Match SQL upsert semantics for session fragments with the same endpoint.
            let expected: BTreeMap<_, _> = expected.iter().map(|b| (b.datetime.as_str(), b)).collect();
            let actual = repo::klines(db, "RB0", tf.as_str(), None, None).await.unwrap();
            assert_eq!(actual.len(), expected.len());
            for bar in actual {
                let want = expected[bar.ts.as_str()];
                assert_eq!((bar.open,bar.high,bar.low,bar.close,bar.volume,bar.hold),
                    (want.open,want.high,want.low,want.close,want.volume,want.hold), "{} {}", tf.as_str(),bar.ts);
            }
        }
    }

    #[tokio::test]
    async fn incremental_updates_match_full_rebuild_across_breaks_midnight_and_corrections() {
        let db = crate::storage::connect(std::path::Path::new(":memory:")).await.unwrap();
        let p = RawPipeline::new(db.clone(), SymbolLocks::new());
        let stamps = ["2026-10-29 23:50:00","2026-10-29 23:55:00","2026-10-30 00:00:00",
            "2026-10-30 00:05:00","2026-10-30 09:05:00","2026-10-30 09:15:00",
            "2026-10-30 11:20:00","2026-10-30 11:25:00","2026-10-30 11:30:00",
            "2026-10-30 13:35:00","2026-10-30 13:40:00"];
        let mut raw=Vec::new();
        for (i,stamp) in stamps.iter().enumerate() {
            let bar=sample(stamp,100.+i as f64);
            p.process_final_bar("RB0",&bar).await.unwrap();
            raw.push(bar);
            if raw.len()>=3 { assert_full_aggregation(&db,&raw).await; }
        }
        let changes=vec![sample("2026-10-30 09:10:00",150.),sample("2026-10-30 11:30:00",160.)];
        p.process_raw_batch("RB0",&changes).await.unwrap();
        merge_klines(&mut raw,&changes);
        assert_full_aggregation(&db,&raw).await;
        // A correction whose surrounding hour has only one raw bar still updates its buckets.
        let change=sample("2026-10-30 09:05:00",180.);
        p.process_final_bar("RB0",&change).await.unwrap();
        merge_klines(&mut raw,&[change]);
        assert_full_aggregation(&db,&raw).await;
        p.rebuild_derived("RB0").await.unwrap();
        assert_full_aggregation(&db,&raw).await;
    }

    #[tokio::test]
    async fn duplicate_refresh_writes_nothing_and_one_new_bar_touches_only_two_derived_buckets() {
        use sea_orm::ConnectionTrait;
        let db = crate::storage::connect(std::path::Path::new(":memory:")).await.unwrap();
        let p = RawPipeline::new(db.clone(), SymbolLocks::new());
        let start = NaiveDateTime::parse_from_str("2026-10-01 09:00:00","%Y-%m-%d %H:%M:%S").unwrap();
        let raw:Vec<_>=(1..=2000).map(|i| sample(&(start+chrono::Duration::minutes(i*5)).format("%Y-%m-%d %H:%M:%S").to_string(),100.)).collect();
        p.process_raw_batch("RB0",&raw).await.unwrap();
        db.execute_unprepared("CREATE TABLE candle_write_audit (kind TEXT, tf TEXT, ts TEXT)").await.unwrap();
        for (kind,reference) in [("INSERT","NEW"),("UPDATE","NEW"),("DELETE","OLD")] {
            db.execute_unprepared(&format!("CREATE TRIGGER audit_{kind} AFTER {kind} ON klines BEGIN INSERT INTO candle_write_audit VALUES ('{kind}',{reference}.timeframe,{reference}.ts); END")).await.unwrap();
        }
        assert_eq!(p.process_raw_batch("RB0",&raw[1990..]).await.unwrap(),0);
        let count=db.query_one(sea_orm::Statement::from_string(sea_orm::DatabaseBackend::Sqlite,"SELECT COUNT(*) AS n FROM candle_write_audit".to_string())).await.unwrap().unwrap();
        assert_eq!(count.try_get::<i64>("","n").unwrap(),0);
        let next=sample(&(start+chrono::Duration::minutes(2001*5)).format("%Y-%m-%d %H:%M:%S").to_string(),120.);
        assert_eq!(p.process_final_bar("RB0",&next).await.unwrap(),2);
        let writes=db.query_all(sea_orm::Statement::from_string(sea_orm::DatabaseBackend::Sqlite,"SELECT kind,tf,ts FROM candle_write_audit".to_string())).await.unwrap();
        assert_eq!(writes.len(),3);
        assert!(writes.iter().all(|row| row.try_get::<String>("","kind").unwrap()!="DELETE"));
    }

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
