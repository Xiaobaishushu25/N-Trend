//! 应用服务层：把抓取、存储、派生、分析串成完整业务流程。

mod settings;

pub use settings::{import_email_toml, Settings};

use anyhow::{anyhow, Result};
use chrono::Timelike;
use sea_orm::{DatabaseConnection, Set};
use serde::Serialize;
use std::collections::HashSet;
use tokio::sync::RwLock;

use crate::analyze::dto::SignalOutcome;
use crate::analyze::model::{Bar, DT, ATR_PERIOD};
use crate::derive::{aggregate, Timeframe};
use crate::fetch::kline::Kline;
use crate::fetch::SinaClient;
use crate::scheduler::SchedulerConfig;
use crate::storage::entities::{klines, signals, symbols};
use crate::storage::repo;

#[derive(Debug, Clone, Default, Serialize)]
pub struct RefreshStats {
    pub succeeded: usize,
    pub failures: usize,
}

#[derive(Debug, Clone, Serialize)]
pub struct SymbolFailure {
    pub symbol: String,
    pub reason: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct ScanResult {
    pub scan_id: i64,
    pub scanned: i64,
    pub active_count: i64,
    pub summary: String,
    pub signals: Vec<SignalOutcome>,
    pub failed: Vec<SymbolFailure>,
}

/// 单个品种的行情快照（最新价 + 相对上一交易日的涨跌幅）。
#[derive(Debug, Clone, serde::Serialize)]
pub struct MarketSnapshot {
    pub code: String,
    pub latest: Option<f64>,
    pub change_pct: Option<f64>,
}
pub struct Services {
    pub db: DatabaseConnection,
    client: RwLock<SinaClient>,
    settings: RwLock<Settings>,
    /// 本次进程内已完成整段深度回填的品种，避免每轮都拉几百根再去重
    deep_backfilled: RwLock<HashSet<String>>,
}

impl Services {
    pub async fn new(db: DatabaseConnection) -> Result<Self> {
        let settings = Settings::load(&db).await?;
        let client =
            SinaClient::with_limits(settings.request_interval_ms, settings.minutely_budget);
        Ok(Self {
            db,
            client: RwLock::new(client),
            settings: RwLock::new(settings),
            deep_backfilled: RwLock::new(HashSet::new()),
        })
    }

    pub async fn settings(&self) -> Settings {
        self.settings.read().await.clone()
    }

    pub async fn apply_settings(&self, s: Settings) -> Result<()> {
        *self.client.write().await =
            SinaClient::with_limits(s.request_interval_ms, s.minutely_budget);
        s.save(&self.db).await?;
        *self.settings.write().await = s;
        Ok(())
    }

    pub async fn scheduler_config(&self) -> SchedulerConfig {
        let s = self.settings().await;
        SchedulerConfig {
            refresh_interval_secs: s.refresh_interval_secs,
            scan_interval_secs: s.scan_interval_secs,
            trading_only: s.trading_only,
        }
    }

    /// 品种表为空时，用内置/导入的代码表初始化。
    pub async fn seed_symbols(&self, default_text: &str) -> Result<usize> {
        let existing = repo::list_symbols(&self.db, false).await?;
        if !existing.is_empty() {
            return Ok(0);
        }
        let codes = crate::fetch::kline::parse_symbol_list(default_text);
        let now = crate::analyze::time::now_display();
        let rows: Vec<symbols::ActiveModel> = codes
            .into_iter()
            .map(|code| symbols::ActiveModel {
                code: Set(code.clone()),
                name: Set(code.clone()),
                variety: Set(String::new()),
                exchange: Set(String::new()),
                node: Set(String::new()),
                watchlist: Set(true),
                enabled: Set(true),
                created_at: Set(now.clone()),
                updated_at: Set(now.clone()),
            })
            .collect();
        let count = rows.len();
        repo::upsert_symbols(&self.db, rows).await?;
        Ok(count)
    }

    /// 从新浪节点表刷新全部品种（名称/交易所/板块信息）。
    pub async fn refresh_symbol_list(&self) -> Result<usize> {
        let rows = crate::fetch::symbols::refresh(&*self.client.read().await).await?;
        let now = crate::analyze::time::now_display();
        let models: Vec<symbols::ActiveModel> = rows
            .into_iter()
            .map(|r| symbols::ActiveModel {
                code: Set(r.code),
                name: Set(r.name),
                variety: Set(r.variety),
                exchange: Set(r.exchange),
                node: Set(r.node),
                watchlist: Set(false),
                enabled: Set(true),
                created_at: Set(now.clone()),
                updated_at: Set(now.clone()),
            })
            .collect();
        let count = models.len();
        repo::upsert_symbols(&self.db, models).await?;
        Ok(count)
    }

    /// 只更新库内已有品种的名称（不新增品种），通过新浪批量行情接口一次补齐，
    /// 避免为了少数未知名称逐个请求全部节点。
    pub async fn enrich_existing_symbols(&self) -> Result<usize> {
        let existing = repo::list_symbols(&self.db, false).await?;
        if existing.is_empty() {
            return Ok(0);
        }
        let missing: Vec<String> = existing
            .iter()
            .filter(|s| s.name.is_empty() || s.name == s.code)
            .map(|s| s.code.clone())
            .collect();
        if missing.is_empty() {
            return Ok(0);
        }
        let names =
            crate::fetch::symbols::fetch_quote_names(&*self.client.read().await, &missing).await?;
        if names.is_empty() {
            return Ok(0);
        }
        let now = crate::analyze::time::now_display();
        let models: Vec<symbols::ActiveModel> = existing
            .iter()
            .filter_map(|s| {
                let name = names.get(&s.code)?;
                Some(symbols::ActiveModel {
                    code: Set(s.code.clone()),
                    name: Set(name.clone()),
                    variety: Set(s.variety.clone()),
                    exchange: Set(s.exchange.clone()),
                    node: Set(s.node.clone()),
                    watchlist: Set(s.watchlist),
                    enabled: Set(s.enabled),
                    created_at: Set(s.created_at.clone()),
                    updated_at: Set(now.clone()),
                })
            })
            .collect();
        let updated = models.len();
        repo::upsert_symbols(&self.db, models).await?;
        Ok(updated)
    }

    /// 是否存在需要补齐名称的品种（名称为空或等于代码）。
    pub async fn needs_name_enrich(&self) -> Result<bool> {
        let existing = repo::list_symbols(&self.db, false).await?;
        Ok(existing.iter().any(|s| s.name.is_empty() || s.name == s.code))
    }
    /// 新品种一次性回填历史 5m 并派生 15m/60m。
    pub async fn backfill_symbol(&self, symbol: &str, count: usize) -> Result<usize> {
        let rows =
            crate::fetch::kline::fetch_minute(&*self.client.read().await, symbol, "5", count).await?;
        let models: Vec<_> = rows
            .iter()
            .map(|k| fetch_to_model(symbol, "5m", "raw", k))
            .collect();
        repo::upsert_klines(&self.db, models).await?;
        self.derive_and_store(symbol).await?;
        Ok(rows.len())
    }

    /// 添加品种（不存在则建档）并回填历史数据。
    pub async fn add_symbol(&self, code: &str) -> Result<usize> {
        let code = code.trim().to_uppercase();
        if code.is_empty() {
            return Err(anyhow!("品种代码不能为空"));
        }
        if !repo::symbol_exists(&self.db, &code).await? {
            let now = crate::analyze::time::now_display();
            repo::upsert_symbols(
                &self.db,
                vec![symbols::ActiveModel {
                    code: Set(code.clone()),
                    name: Set(code.clone()),
                    variety: Set(String::new()),
                    exchange: Set(String::new()),
                    node: Set(String::new()),
                    watchlist: Set(true),
                    enabled: Set(true),
                    created_at: Set(now.clone()),
                    updated_at: Set(now),
                }],
            )
            .await?;
        }
        let count = self.settings().await.backfill_count;
        self.backfill_symbol(&code, count).await
    }

    /// 删除品种及其K线数据。
    pub async fn remove_symbol(&self, code: &str) -> Result<()> {
        repo::remove_symbol(&self.db, code).await?;
        repo::delete_symbol_klines(&self.db, code).await?;
        Ok(())
    }
    /// 定时增量刷新：每品种按增量窗口抓取，缺口过大时回补。
    pub async fn refresh_data(&self) -> Result<RefreshStats> {
        let symbols = repo::list_symbols(&self.db, true).await?;
        let mut stats = RefreshStats::default();
        for sym in symbols {
            match self.refresh_symbol_data(&sym.code).await {
                Ok(_) => stats.succeeded += 1,
                Err(e) => {
                    stats.failures += 1;
                    tracing::warn!("刷新 {} 失败: {e}", sym.code);
                }
            }
        }
        Ok(stats)
    }

    async fn refresh_symbol_data(&self, code: &str) -> Result<()> {
        let s = self.settings().await;
        let latest = repo::latest_ts(&self.db, code, "5m").await?;
        let stored = repo::raw_klines(&self.db, code).await?.len();

        // 按“最新已存K线 → 当前时间”的间隔估算需要补的根数（5分钟一根），
        // 保底增量根数、上限回填根数；避免每次都整段重抓再去重插入。
        let (gap_min, needed) = if let Some(latest_ts) = &latest {
            let now = chrono::Local::now().format("%Y-%m-%d %H:%M:%S").to_string();
            let gap = ts_gap_minutes(&now, latest_ts).unwrap_or(0).max(0);
            (gap, ((gap / 5 + 2) as usize).clamp(s.incremental_count, s.backfill_count))
        } else {
            (0, s.backfill_count)
        };
        // 历史深度不足回填目标且本次进程还没整段回填过：一次性补齐深度，之后只按时间差增量抓取
        let deep_done = self.deep_backfilled.read().await.contains(code);
        let count = if stored < s.backfill_count && !deep_done {
            s.backfill_count
        } else {
            needed
        };
        let fetched =
            crate::fetch::kline::fetch_minute(&*self.client.read().await, code, "5", count).await?;
        if count >= s.backfill_count {
            self.deep_backfilled.write().await.insert(code.to_string());
        }
        // 长时间停机检查：缺口超过接口单次最大窗口（约1000根5m）时中间无法补齐，
        // 记录日志并仅保留接口能取到的最近窗口，避免做无效的二次全量请求。
        if let Some(latest_ts) = &latest {
            let max_cover = (s.backfill_count * 5) as i64; // 接口窗口按分钟估算
            if gap_min > max_cover {
                tracing::warn!(
                    "{code} 停机约 {:.1} 小时，超过接口回补窗口（{} 根5m），中间存在数据缺口，仅保留最近窗口",
                    gap_min as f64 / 60.0,
                    s.backfill_count
                );
            } else if let Some(first) = fetched.first() {
                let hole = ts_gap_minutes(latest_ts, &first.datetime).unwrap_or(0);
                if hole > 60 {
                    tracing::info!("{code} 已补上 {} 分钟缺口", hole);
                }
            }
        }
        let models: Vec<_> = fetched
            .iter()
            .map(|k| fetch_to_model(code, "5m", "raw", k))
            .collect();
        repo::upsert_klines(&self.db, models).await?;
        self.derive_and_store(code).await?;
        Ok(())
    }

    /// 用原始 5m 重新派生并落库 15m/60m（策略热路径）。
    pub async fn derive_and_store(&self, symbol: &str) -> Result<()> {
        let raw = repo::raw_klines(&self.db, symbol).await?;
        if raw.len() < 3 {
            return Ok(());
        }
        let bars: Vec<Kline> = raw.iter().map(model_to_fetch).collect();
        let mut models = Vec::new();
        for tf in [Timeframe::M15, Timeframe::M60] {
            for k in aggregate(&bars, tf) {
                models.push(fetch_to_model(symbol, tf.as_str(), "derived", &k));
            }
        }
        repo::delete_derived_klines(&self.db, symbol).await?;
        repo::upsert_klines(&self.db, models).await?;
        Ok(())
    }

    /// 取某级别的分析用 bar；派生数据不足时从原始 5m 现场聚合兜底。
    pub async fn bars_for(&self, symbol: &str, timeframe: &str) -> Result<Vec<Bar>> {
        let rows = repo::klines(&self.db, symbol, timeframe, None, None).await?;
        let mut bars: Vec<Bar> = rows.iter().filter_map(model_to_bar).collect();
        if bars.len() >= ATR_PERIOD + 2 {
            return Ok(bars);
        }
        let tf = Timeframe::parse(timeframe).ok_or_else(|| anyhow!("不支持的级别 {timeframe}"))?;
        let raw = repo::raw_klines(&self.db, symbol).await?;
        let fetch_bars: Vec<Kline> = raw.iter().map(model_to_fetch).collect();
        let fallback: Vec<Bar> = aggregate(&fetch_bars, tf)
            .iter()
            .filter_map(fetch_to_bar)
            .collect();
        if fallback.len() > bars.len() {
            bars = fallback;
        }
        Ok(bars)
    }

    /// 图表数据：5m 读原始库；15m/60m 优先读派生缓存；其余级别现场聚合。
    pub async fn get_klines(
        &self,
        symbol: &str,
        timeframe: &str,
        limit: Option<usize>,
    ) -> Result<Vec<klines::Model>> {
        let tf = Timeframe::parse(timeframe).ok_or_else(|| anyhow!("不支持的级别 {timeframe}"))?;
        if tf == Timeframe::M5 {
            return repo::klines(&self.db, symbol, "5m", limit, None).await;
        }
        if matches!(tf, Timeframe::M15 | Timeframe::M60) {
            let rows = repo::klines(&self.db, symbol, tf.as_str(), None, None).await?;
            if !rows.is_empty() {
                return Ok(apply_limit(rows, limit));
            }
        }
        let raw = repo::raw_klines(&self.db, symbol).await?;
        let bars: Vec<Kline> = raw.iter().map(model_to_fetch).collect();
        let derived = aggregate(&bars, tf);
        let rows: Vec<klines::Model> = derived
            .into_iter()
            .map(|k| klines::Model {
                symbol: symbol.to_string(),
                timeframe: tf.as_str().to_string(),
                ts: k.datetime,
                open: k.open,
                high: k.high,
                low: k.low,
                close: k.close,
                volume: k.volume,
                hold: k.hold,
                source: "derived".to_string(),
            })
            .collect();
        Ok(apply_limit(rows, limit))
    }


/// 全部品种的最新价与涨跌幅（供左侧品种列表展示）。
pub async fn market_snapshot(&self) -> Result<Vec<MarketSnapshot>> {
    let symbols = repo::list_symbols(&self.db, false).await?;
    let mut out = Vec::with_capacity(symbols.len());
    for s in symbols {
        let rows = repo::klines(&self.db, &s.code, "5m", None, None).await?;
        let (latest, change_pct) = Self::snapshot_stats(&rows);
        out.push(MarketSnapshot {
            code: s.code,
            latest,
            change_pct,
        });
    }
    Ok(out)
}

fn snapshot_stats(rows: &[klines::Model]) -> (Option<f64>, Option<f64>) {
    let fmt = "%Y-%m-%d %H:%M:%S";
    let mut by_day: std::collections::BTreeMap<String, f64> = std::collections::BTreeMap::new();
    for r in rows {
        let Some(dt) = chrono::NaiveDateTime::parse_from_str(&r.ts, fmt).ok() else {
            continue;
        };
        let day = if dt.hour() >= 20 {
            (dt.date() + chrono::Days::new(1)).format("%Y-%m-%d").to_string()
        } else {
            dt.date().format("%Y-%m-%d").to_string()
        };
        by_day.insert(day, r.close);
    }
    let Some(latest) = rows.last().map(|r| r.close) else {
        return (None, None);
    };
    if by_day.len() < 2 {
        return (Some(latest), None);
    }
    let days: Vec<&String> = by_day.keys().collect();
    let prev = by_day[days[days.len() - 2]];
    let pct = (latest - prev) / prev * 100.0;
    (Some(latest), Some(pct))
}
    /// 全品种扫描：15m 结构 + 60m 趋势 → 信号持久化。
    pub async fn run_scan(&self) -> Result<ScanResult> {
        let started = crate::analyze::time::now_display();
        let symbols = repo::list_symbols(&self.db, true).await?;
        let mut active: Vec<SignalOutcome> = Vec::new();
        let mut failed: Vec<SymbolFailure> = Vec::new();
        let mut scanned = 0i64;

        for sym in symbols {
            let bars15 = self.bars_for(&sym.code, "15m").await?;
            let bars60 = self.bars_for(&sym.code, "60m").await?;
            if bars15.len() < ATR_PERIOD + 2 || bars60.len() < ATR_PERIOD + 2 {
                failed.push(SymbolFailure {
                    symbol: sym.code,
                    reason: "K线数据不足".to_string(),
                });
                continue;
            }
            match crate::analyze::analyze_bars(&sym.code, &bars15, &bars60) {
                Ok(outcome) => {
                    scanned += 1;
                    active.extend(crate::analyze::collect_active(&outcome));
                }
                Err(e) => failed.push(SymbolFailure {
                    symbol: sym.code,
                    reason: e.to_string(),
                }),
            }
        }

        let finished = crate::analyze::time::now_display();
        let active_count = active.len() as i64;
        let status = if scanned > 0 { "ok" } else { "no_data" };
        let summary = build_scan_summary(&started, &finished, scanned, active_count, &failed);

        let scan_id = repo::insert_scan(
            &self.db,
            started,
            finished,
            status.to_string(),
            scanned,
            active_count,
            summary.clone(),
        )
        .await?;

        let now = crate::analyze::time::now_display();
        let rows: Vec<signals::ActiveModel> = active
            .iter()
            .map(|o| {
                let s = &o.signal;
                signals::ActiveModel {
                    id: sea_orm::NotSet,
                    scan_id: Set(scan_id),
                    symbol: Set(o.symbol.clone()),
                    level: Set(s.level.clone()),
                    direction: Set(s.direction.clone()),
                    grade: Set(s.grade.clone()),
                    state: Set(s.state.clone()),
                    category: Set(s.category.clone()),
                    entry: Set(s.entry),
                    stop: Set(s.stop),
                    target: Set(s.target),
                    rr: Set(s.rr),
                    score: Set(s.score),
                    note: Set(s.note.clone()),
                    detail: Set(serde_json::to_string(s).unwrap_or_default()),
                    created_at: Set(now.clone()),
                }
            })
            .collect();
        repo::insert_signals(&self.db, rows).await?;

        Ok(ScanResult {
            scan_id,
            scanned,
            active_count,
            summary,
            signals: active,
            failed,
        })
    }
}

fn build_scan_summary(
    started: &str,
    finished: &str,
    scanned: i64,
    active_count: i64,
    failed: &[SymbolFailure],
) -> String {
    let mut out = String::new();
    out.push_str("=== 综合结论 ===\n");
    out.push_str(&format!("扫描时间: {started}\n"));
    out.push_str(&format!("完成时间: {finished}\n"));
    out.push_str(&format!("共扫描 {scanned} 个品种，{active_count} 个品种有关注信号\n"));
    if !failed.is_empty() {
        let list = failed
            .iter()
            .map(|f| format!("{}: {}", f.symbol, f.reason))
            .collect::<Vec<_>>()
            .join("；");
        out.push_str(&format!("以下品种分析失败: {list}\n"));
    }
    out
}

fn apply_limit(rows: Vec<klines::Model>, limit: Option<usize>) -> Vec<klines::Model> {
    match limit {
        Some(limit) if rows.len() > limit => rows[rows.len() - limit..].to_vec(),
        _ => rows,
    }
}

fn ts_gap_minutes(later: &str, earlier: &str) -> Option<i64> {
    let fmt = "%Y-%m-%d %H:%M:%S";
    let a = chrono::NaiveDateTime::parse_from_str(later, fmt).ok()?;
    let b = chrono::NaiveDateTime::parse_from_str(earlier, fmt).ok()?;
    Some((a - b).num_minutes())
}

pub fn model_to_fetch(m: &klines::Model) -> Kline {
    Kline {
        datetime: m.ts.clone(),
        open: m.open,
        high: m.high,
        low: m.low,
        close: m.close,
        volume: m.volume,
        hold: m.hold,
    }
}

pub fn fetch_to_model(
    symbol: &str,
    timeframe: &str,
    source: &str,
    k: &Kline,
) -> klines::ActiveModel {
    klines::ActiveModel {
        symbol: Set(symbol.to_string()),
        timeframe: Set(timeframe.to_string()),
        ts: Set(k.datetime.clone()),
        open: Set(k.open),
        high: Set(k.high),
        low: Set(k.low),
        close: Set(k.close),
        volume: Set(k.volume),
        hold: Set(k.hold),
        source: Set(source.to_string()),
    }
}

fn model_to_bar(m: &klines::Model) -> Option<Bar> {
    let dt = parse_dt(&m.ts)?;
    Some(Bar {
        dt,
        open: m.open,
        high: m.high,
        low: m.low,
        close: m.close,
        volume: m.volume,
        hold: m.hold,
    })
}

fn fetch_to_bar(k: &Kline) -> Option<Bar> {
    let dt = parse_dt(&k.datetime)?;
    Some(Bar {
        dt,
        open: k.open,
        high: k.high,
        low: k.low,
        close: k.close,
        volume: k.volume,
        hold: k.hold,
    })
}

fn parse_dt(s: &str) -> Option<DT> {
    let mut parts = s.split_whitespace();
    let date = parts.next()?;
    let time = parts.next().unwrap_or("00:00:00");
    let mut dp = date.split(|c: char| c == '-' || c == '/');
    let year = dp.next()?.parse().ok()?;
    let month = dp.next()?.parse().ok()?;
    let day = dp.next()?.parse().ok()?;
    let mut tp = time.split(':');
    let hour: i32 = tp.next()?.parse().ok()?;
    let minute: i32 = tp.next().unwrap_or("0").parse().ok()?;
    Some(DT {
        year,
        month,
        day,
        hour,
        minute,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_dt_handles_common_formats() {
        let dt = parse_dt("2026-08-03 09:15:00").unwrap();
        assert_eq!(dt.year, 2026);
        assert_eq!(dt.month, 8);
        assert_eq!(dt.day, 3);
        assert_eq!(dt.hour, 9);
        assert_eq!(dt.minute, 15);
        assert!(parse_dt("bad").is_none());
    }

    #[test]
    fn gap_minutes_works() {
        assert_eq!(
            ts_gap_minutes("2026-08-03 10:00:00", "2026-08-03 08:00:00"),
            Some(120)
        );
    }
}








