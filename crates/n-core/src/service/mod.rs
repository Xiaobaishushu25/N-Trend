//! 应用服务层：把抓取、存储、派生、分析串成完整业务流程。

use anyhow::{anyhow, Result};
use chrono::Timelike;
use sea_orm::{DatabaseConnection, Set};
use serde::Serialize;
use std::collections::{HashMap, HashSet};
use tokio::sync::RwLock;

use crate::analyze::dto::SignalOutcome;
use crate::analyze::model::{Bar, ATR_PERIOD, DT};
use crate::analyze::outcome;
use crate::config::Config;
use crate::derive::{aggregate, rollover, Timeframe};
use crate::fetch::kline::Kline;
use crate::fetch::SinaClient;
use crate::scheduler::SchedulerConfig;
use crate::storage::entities::{klines, signal_outcomes, signals, symbols};
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

/// 结局回填结果（复盘页刷新按钮返回值）。
#[derive(Debug, Clone, Default, Serialize)]
pub struct OutcomeRefresh {
    pub updated: usize,
}

/// 复盘页明细表的一行（信号快照 + 结局 + 特征）。
#[derive(Debug, Clone, Serialize)]
pub struct OutcomeDetail {
    pub signal_id: i64,
    pub symbol: String,
    pub direction: String,
    pub level: String,
    pub grade: String,
    pub score: f64,
    pub entry: f64,
    pub stop: f64,
    pub target: f64,
    pub rr: f64,
    pub created_at: String,
    pub outcome: String,
    pub exit_reason: String,
    /// 模拟回放找到的入场触达时间（快照 trigger_ts 缺失时用于图上补画触发标记）
    pub entry_ts: Option<String>,
    pub exit_ts: Option<String>,
    pub exit_price: Option<f64>,
    pub r_multiple: Option<f64>,
    pub mfe_r: Option<f64>,
    pub mae_r: Option<f64>,
    pub bars_held: Option<i64>,
    pub vol_ratio: Option<f64>,
    pub oi_increase: Option<bool>,
    pub trend60_score: Option<f64>,
    /// 模拟窗口内跨过连续合约换月（不计入盈亏统计）
    pub rollover_crossed: bool,
    /// 入场价被跳空穿越
    pub gap_crossed_entry: bool,
    /// 止损价被跳空穿越
    pub gap_crossed_exit: bool,
}

/// 复盘明细跳转K线图所需：完整形态结构 + 结局。
#[derive(Debug, Clone, Serialize)]
pub struct ReviewSignalDetail {
    pub pattern: crate::analyze::dto::PatternDto,
    pub outcome: Option<OutcomeDetail>,
}

/// 最近信号明细的筛选条件（均为可选，空值不过滤）。
#[derive(Debug, Clone, Default)]
pub struct OutcomeFilter {
    /// 品种代码包含匹配（不区分大小写）
    pub symbol: Option<String>,
    /// up / down
    pub direction: Option<String>,
    /// fine / large
    pub level: Option<String>,
    /// A级 / B级 / C级 / 回撤过浅 / 回撤过深
    pub grade: Option<String>,
    pub score_min: Option<f64>,
    pub score_max: Option<f64>,
    /// win / loss / no_trigger / open / insufficient_data
    pub outcome: Option<String>,
}

fn matches_outcome_filter(
    s: &signals::Model,
    o: &signal_outcomes::Model,
    f: &OutcomeFilter,
) -> bool {
    if let Some(sym) = f.symbol.as_deref().filter(|x| !x.is_empty()) {
        if !s.symbol.to_lowercase().contains(&sym.to_lowercase()) {
            return false;
        }
    }
    if let Some(d) = f.direction.as_deref().filter(|x| !x.is_empty()) {
        if s.direction != d {
            return false;
        }
    }
    if let Some(l) = f.level.as_deref().filter(|x| !x.is_empty()) {
        if s.level != l {
            return false;
        }
    }
    if let Some(g) = f.grade.as_deref().filter(|x| !x.is_empty()) {
        if s.grade != g {
            return false;
        }
    }
    if let Some(min) = f.score_min {
        if s.score < min {
            return false;
        }
    }
    if let Some(max) = f.score_max {
        if s.score > max {
            return false;
        }
    }
    if let Some(out) = f.outcome.as_deref().filter(|x| !x.is_empty()) {
        if o.outcome != out {
            return false;
        }
    }
    true
}

/// 从 signals.detail JSON 读取预警时间与结构两端时间戳。
fn parse_detail_ts(detail: &str) -> (Option<String>, Option<String>, Option<String>) {
    let Ok(v) = serde_json::from_str::<serde_json::Value>(detail) else {
        return (None, None, None);
    };
    let get = |path: &[&str]| -> Option<String> {
        let mut cur: &serde_json::Value = &v;
        for p in path {
            cur = cur.get(p)?;
        }
        cur.as_str().map(|s| s.to_string())
    };
    (get(&["warning_ts"]), get(&["s1", "ts"]), get(&["s2", "ts"]))
}

fn signal_input_from(s: &signals::Model) -> Option<outcome::SignalInput> {
    let (warning_ts, s1_ts, s2_ts) = parse_detail_ts(&s.detail);
    Some(outcome::SignalInput {
        symbol: s.symbol.clone(),
        direction: s.direction.clone(),
        level: s.level.clone(),
        entry: s.entry,
        stop: s.stop,
        target: s.target,
        risk: (s.entry - s.stop).abs(),
        created_at: s.created_at.clone(),
        warning_ts,
        s1_ts,
        s2_ts,
    })
}

/// 换月记录更新晚于结果回填时，已终局的信号也要重算（可能因新确认换月而改为 rollover）。
fn needs_outcome_refresh(
    outcome: Option<&signal_outcomes::Model>,
    latest_rollover_updated: Option<&str>,
) -> bool {
    let Some(outcome) = outcome else {
        return true;
    };
    if outcome.sim_version != outcome::SIM_VERSION
        || !outcome::Outcome::parse(&outcome.outcome).is_some_and(|x| x.is_terminal())
    {
        return true;
    }
    latest_rollover_updated.is_some_and(|ts| ts > outcome.updated_at.as_str())
}

fn stat_row_from(
    s: &signals::Model,
    o: Option<&signal_outcomes::Model>,
) -> Option<outcome::StatRow> {
    let (warning_ts, s1_ts, s2_ts) = parse_detail_ts(&s.detail);
    Some(outcome::StatRow {
        signal_id: s.id,
        symbol: s.symbol.clone(),
        direction: s.direction.clone(),
        level: s.level.clone(),
        grade: s.grade.clone(),
        score: s.score,
        created_at: s.created_at.clone(),
        warning_ts,
        s1_ts,
        s2_ts,
        outcome: o.and_then(|x| outcome::Outcome::parse(&x.outcome)),
        r_multiple: o.and_then(|x| x.r_multiple),
        bars_held: o.and_then(|x| x.bars_held.map(|b| b as usize)),
        vol_ratio: o.and_then(|x| x.vol_ratio),
        oi_increase: o.and_then(|x| x.oi_increase),
        trend60_score: o.and_then(|x| x.trend60_score),
        atr_percentile: o.and_then(|x| x.atr_percentile),
        rollover_crossed: o.is_some_and(|x| x.rollover_crossed.unwrap_or(false)),
        gap_crossed_entry: o.is_some_and(|x| x.gap_crossed_entry.unwrap_or(false)),
        gap_crossed_exit: o.is_some_and(|x| x.gap_crossed_exit.unwrap_or(false)),
    })
}

fn outcome_detail_from(s: &signals::Model, o: &signal_outcomes::Model) -> OutcomeDetail {
    OutcomeDetail {
        signal_id: s.id,
        symbol: s.symbol.clone(),
        direction: s.direction.clone(),
        level: s.level.clone(),
        grade: s.grade.clone(),
        score: s.score,
        entry: s.entry,
        stop: s.stop,
        target: s.target,
        rr: s.rr,
        created_at: s.created_at.clone(),
        outcome: o.outcome.clone(),
        exit_reason: o.exit_reason.clone(),
        entry_ts: o.entry_ts.clone(),
        exit_ts: o.exit_ts.clone(),
        exit_price: o.exit_price,
        r_multiple: o.r_multiple,
        mfe_r: o.mfe_r,
        mae_r: o.mae_r,
        bars_held: o.bars_held,
        vol_ratio: o.vol_ratio,
        oi_increase: o.oi_increase,
        trend60_score: o.trend60_score,
        rollover_crossed: o.rollover_crossed.unwrap_or(false),
        gap_crossed_entry: o.gap_crossed_entry.unwrap_or(false),
        gap_crossed_exit: o.gap_crossed_exit.unwrap_or(false),
    }
}

/// 单个品种的行情快照（最新价 + 相对上一交易日的涨跌幅）。
#[derive(Debug, Clone, serde::Serialize)]
pub struct MarketSnapshot {
    pub code: String,
    pub latest: Option<f64>,
    pub change_pct: Option<f64>,
}

/// Kline chart data: same fields as the klines table plus a rollover marker.
#[derive(Debug, Clone, Serialize)]
pub struct KlineDto {
    pub symbol: String,
    pub timeframe: String,
    pub ts: String,
    pub open: f64,
    pub high: f64,
    pub low: f64,
    pub close: f64,
    pub volume: f64,
    pub hold: f64,
    pub source: String,
    /// true when this bar is the first bar after a continuous-contract rollover
    pub rollover: bool,
}

/// 入场价触发命中：最新价已触及某形态入场点（做空=跌破，做多=突破）。
#[derive(Debug, Clone, serde::Serialize)]
pub struct EntryTriggerHit {
    pub signal_id: i64,
    pub symbol: String,
    pub name: String,
    pub direction: String,
    pub level: String,
    pub grade: String,
    pub entry: f64,
    pub latest: f64,
}

pub struct Services {
    pub db: DatabaseConnection,
    client: RwLock<SinaClient>,
    /// 实时行情专用客户端：独立限速额度，避免与K线抓取互相排队。
    quote_client: RwLock<SinaClient>,
    config: RwLock<Config>,
    /// 配置文件路径（保存配置用）
    config_path: std::path::PathBuf,
    /// 已发过入场价提醒的形态（symbol+direction+level+entry），避免重复通知
    entry_notified: RwLock<HashSet<(String, String, String, u64)>>,
    /// 本次进程内已完成整段深度回填的品种，避免每轮都拉几百根再去重
    deep_backfilled: RwLock<HashSet<String>>,
}

impl Services {
    pub async fn new(
        db: DatabaseConnection,
        config: Config,
        config_path: std::path::PathBuf,
    ) -> Result<Self> {
        let client = SinaClient::with_limits(
            config.fetch.request_interval_ms,
            config.fetch.minutely_budget,
        );
        // 实时行情轮询：单批最多 50 个品种、轮询间隔数秒，200ms/120次每分钟足够，
        // 且与K线抓取的 60/分钟预算互不影响。
        let quote_client = SinaClient::with_limits(
            config.quote.request_interval_ms,
            config.quote.minutely_budget,
        );
        Ok(Self {
            db,
            client: RwLock::new(client),
            quote_client: RwLock::new(quote_client),
            config: RwLock::new(config),
            config_path,
            entry_notified: RwLock::new(HashSet::new()),
            deep_backfilled: RwLock::new(HashSet::new()),
        })
    }

    pub async fn config(&self) -> Config {
        self.config.read().await.clone()
    }

    /// 应用新配置：重建抓取/实时行情限速器，写 JSON 文件，更新内存。
    pub async fn apply_config(&self, c: Config) -> Result<Config> {
        *self.client.write().await =
            SinaClient::with_limits(c.fetch.request_interval_ms, c.fetch.minutely_budget);
        *self.quote_client.write().await =
            SinaClient::with_limits(c.quote.request_interval_ms, c.quote.minutely_budget);
        c.save(&self.config_path)?;
        *self.config.write().await = c;
        Ok(self.config().await)
    }

    /// 记录上次打开的分组 tab：仅更新 UI 配置并落盘，不重建限速器。
    pub async fn set_last_group(&self, group_id: Option<i64>) -> Result<()> {
        let mut c = self.config.write().await;
        c.ui.last_group_id = group_id;
        c.save(&self.config_path)
    }

    /// 每次行情轮询后对比最新价与形态入场点：做空最新价跌破入场点、做多最新价突破入场点
    /// 即视为命中；同一形态只通知一次（跨轮询、跨扫描都不重复）。
    /// 两个触发价通知开关都关闭时不检测。
    pub async fn entry_trigger_hits(
        &self,
        snapshots: &[MarketSnapshot],
    ) -> Result<Vec<EntryTriggerHit>> {
        let cfg = self.config().await;
        if !cfg.notify.in_app_entry_trigger && !cfg.notify.system_entry_trigger {
            return Ok(Vec::new());
        }
        let rows = repo::latest_signals(&self.db, 500).await?;
        let symbols = repo::list_symbols(&self.db, false).await?;
        let name_by_code: HashMap<String, String> = symbols
            .iter()
            .map(|s| (s.code.clone(), s.name.clone()))
            .collect();
        let by_code: HashMap<&str, f64> = snapshots
            .iter()
            .filter_map(|s| s.latest.map(|v| (s.code.as_str(), v)))
            .collect();
        let mut notified = self.entry_notified.write().await;
        let mut hits = Vec::new();
        for row in rows {
            if !is_active_signal_state(&row.state) {
                continue;
            }
            let Some(latest) = by_code.get(row.symbol.as_str()).copied() else {
                continue;
            };
            let crossed = match row.direction.as_str() {
                "down" => latest < row.entry,
                _ => latest > row.entry,
            };
            if !crossed {
                continue;
            }
            let name = name_by_code.get(&row.symbol).cloned().unwrap_or_default();
            let key = (
                row.symbol.clone(),
                row.direction.clone(),
                row.level.clone(),
                row.entry.to_bits(),
            );
            if notified.insert(key) {
                hits.push(EntryTriggerHit {
                    signal_id: row.id,
                    symbol: row.symbol,
                    name,
                    direction: row.direction,
                    level: row.level,
                    grade: row.grade,
                    entry: row.entry,
                    latest,
                });
            }
        }
        Ok(hits)
    }

    /// 更新启用的K线周期列表：去重并过滤未知周期，为空时回退为全部；仅落盘不重建限速器。
    pub async fn set_timeframes(&self, timeframes: Vec<String>) -> Result<()> {
        let mut c = self.config.write().await;
        let mut next: Vec<String> = Vec::new();
        for tf in timeframes {
            if crate::config::DEFAULT_TIMEFRAMES.contains(&tf.as_str()) && !next.contains(&tf) {
                next.push(tf);
            }
        }
        if next.is_empty() {
            next = crate::config::DEFAULT_TIMEFRAMES
                .iter()
                .map(|s| s.to_string())
                .collect();
        }
        c.ui.timeframes = next;
        c.save(&self.config_path)
    }

    /// 将所有配置恢复为默认值：重建限速器、写 JSON、更新内存，返回新的默认配置。
    pub async fn reset_config(&self) -> Result<Config> {
        let c = Config::default();
        *self.client.write().await =
            SinaClient::with_limits(c.fetch.request_interval_ms, c.fetch.minutely_budget);
        *self.quote_client.write().await =
            SinaClient::with_limits(c.quote.request_interval_ms, c.quote.minutely_budget);
        c.save(&self.config_path)?;
        *self.config.write().await = c.clone();
        Ok(c)
    }

    pub async fn scheduler_config(&self) -> SchedulerConfig {
        self.config().await.scheduler
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
                tick_size: Set(crate::precision::default_tick(&code, "")),
                created_at: Set(now.clone()),
                updated_at: Set(now.clone()),
                ..Default::default()
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
            .map(|r| {
                let tick = crate::precision::default_tick(&r.code, &r.variety);
                symbols::ActiveModel {
                    code: Set(r.code),
                    name: Set(r.name),
                    variety: Set(r.variety),
                    exchange: Set(r.exchange),
                    node: Set(r.node),
                    watchlist: Set(false),
                    enabled: Set(true),
                    tick_size: Set(tick),
                    created_at: Set(now.clone()),
                    updated_at: Set(now.clone()),
                    ..Default::default()
                }
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
            // 名称为空、等于代码、或过短（如历史版本误存的“连”）都视为待补齐
            .filter(|s| s.name.is_empty() || s.name == s.code || s.name.chars().count() <= 2)
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
                    tick_size: Set(if s.tick_size > 0.0 {
                        s.tick_size
                    } else {
                        crate::precision::default_tick(&s.code, &s.variety)
                    }),
                    created_at: Set(s.created_at.clone()),
                    updated_at: Set(now.clone()),
                    ..Default::default()
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
        Ok(existing
            .iter()
            .any(|s| s.name.is_empty() || s.name == s.code))
    }

    /// 为 tick_size 未设置（0）的品种补齐内置默认精度；已显式设置的不覆盖。
    pub async fn backfill_tick_sizes(&self) -> Result<usize> {
        let symbols = repo::list_symbols(&self.db, false).await?;
        let mut updated = 0usize;
        for s in symbols {
            if s.tick_size > 0.0 {
                continue;
            }
            let tick = crate::precision::default_tick(&s.code, &s.variety);
            repo::set_symbol_tick(&self.db, &s.code, tick).await?;
            updated += 1;
        }
        Ok(updated)
    }
    /// 新品种一次性回填历史 5m 并派生 15m/60m。
    pub async fn backfill_symbol(&self, symbol: &str, count: usize) -> Result<usize> {
        let rows =
            crate::fetch::kline::fetch_minute(&*self.client.read().await, symbol, "5", count)
                .await?;
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
            // 新代码先向行情接口确认存在并取中文名：
            // 无效代码在这里就给出明确提示，避免建档后回填时报「接口没有返回K线数据」这类模糊错误
            let names = crate::fetch::symbols::fetch_quote_names(
                &*self.client.read().await,
                &[code.clone()],
            )
            .await?;
            let Some(name) = names.get(&code) else {
                return Err(anyhow!(
                    "未找到品种「{code}」，请检查代码（示例：RB0、AU0、IF0）"
                ));
            };
            let now = crate::analyze::time::now_display();
            repo::upsert_symbols(
                &self.db,
                vec![symbols::ActiveModel {
                    code: Set(code.clone()),
                    name: Set(name.clone()),
                    variety: Set(String::new()),
                    exchange: Set(String::new()),
                    node: Set(String::new()),
                    watchlist: Set(true),
                    enabled: Set(true),
                    tick_size: Set(crate::precision::default_tick(&code, "")),
                    created_at: Set(now.clone()),
                    updated_at: Set(now),
                    ..Default::default()
                }],
            )
            .await?;
        }
        let count = self.config().await.fetch.backfill_count;
        self.backfill_symbol(&code, count).await
    }

    /// 标题栏搜索提示用：按前缀搜索新浪期货合约（如 RB → RB0、RB2609、RB2608…）。
    pub async fn search_contracts(
        &self,
        keyword: &str,
    ) -> Result<Vec<crate::fetch::symbols::FuturesSymbol>> {
        crate::fetch::symbols::search_contracts(&*self.client.read().await, keyword).await
    }

    /// 删除品种及其K线数据。
    pub async fn remove_symbol(&self, code: &str) -> Result<()> {
        repo::remove_symbol(&self.db, code).await?;
        repo::delete_symbol_klines(&self.db, code).await?;
        repo::delete_symbol_rollovers(&self.db, code).await?;
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
        let s = self.config().await;
        let latest = repo::latest_ts(&self.db, code, "5m").await?;
        let stored = repo::raw_klines(&self.db, code).await?.len();

        // 按“最新已存K线 → 当前时间”的间隔估算需要补的根数（5分钟一根），
        // 保底增量根数、上限回填根数；避免每次都整段重抓再去重插入。
        let (gap_min, needed) = if let Some(latest_ts) = &latest {
            let now = chrono::Local::now().format("%Y-%m-%d %H:%M:%S").to_string();
            let gap = ts_gap_minutes(&now, latest_ts).unwrap_or(0).max(0);
            (
                gap,
                ((gap / 5 + 2) as usize).clamp(s.fetch.incremental_count, s.fetch.backfill_count),
            )
        } else {
            (0, s.fetch.backfill_count)
        };
        // 历史深度不足回填目标且本次进程还没整段回填过：一次性补齐深度，之后只按时间差增量抓取
        let deep_done = self.deep_backfilled.read().await.contains(code);
        let count = if stored < s.fetch.backfill_count && !deep_done {
            s.fetch.backfill_count
        } else {
            needed
        };
        let fetched =
            crate::fetch::kline::fetch_minute(&*self.client.read().await, code, "5", count).await?;
        if count >= s.fetch.backfill_count {
            self.deep_backfilled.write().await.insert(code.to_string());
        }
        // 长时间停机检查：缺口超过接口单次最大窗口（约1000根5m）时中间无法补齐，
        // 记录日志并仅保留接口能取到的最近窗口，避免做无效的二次全量请求。
        if let Some(latest_ts) = &latest {
            let max_cover = (s.fetch.backfill_count * 5) as i64; // 接口窗口按分钟估算
            if gap_min > max_cover {
                tracing::warn!(
                    "{code} 停机约 {:.1} 小时，超过接口回补窗口（{} 根5m），中间存在数据缺口，仅保留最近窗口",
                    gap_min as f64 / 60.0,
                    s.fetch.backfill_count
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
        self.sync_rollovers(symbol, &bars).await?;
        Ok(())
    }

    /// 轻量换月识别：只在连续 5m 上找候选断点，对少量月合约拉数据做价格确认。
    /// 网络失败时保留已有记录，不阻塞行情派生。
    async fn sync_rollovers(&self, symbol: &str, bars: &[Kline]) -> Result<()> {
        let candidates = rollover::detect_candidates(symbol, bars);
        if candidates.is_empty() {
            return Ok(());
        }
        let existing = repo::symbol_rollovers(&self.db, symbol).await?;
        let confirmed_ts: HashSet<String> = existing
            .iter()
            .filter(|r| r.confirmed)
            .map(|r| r.ts.clone())
            .collect();
        let pending: Vec<rollover::RolloverCandidate> = candidates
            .into_iter()
            .filter(|c| !confirmed_ts.contains(&c.ts))
            .collect();
        if pending.is_empty() {
            return Ok(());
        }

        let prefix = contract_prefix(symbol);
        let contracts = match self.search_contracts(&prefix).await {
            Ok(rows) => rows,
            Err(e) => {
                tracing::warn!("{symbol} 换月确认跳过（合约列表获取失败）: {e}");
                return Ok(());
            }
        };
        let mut month_codes: Vec<String> = contracts
            .into_iter()
            .map(|r| r.code)
            .filter(|code| rollover::is_month_contract(code))
            .collect();
        month_codes.sort();
        month_codes.dedup();
        if month_codes.is_empty() {
            tracing::warn!("{symbol} 换月确认跳过（未找到月合约）");
            return Ok(());
        }

        // 一次按最早断点估足抓取深度，避免对同一合约重复请求。
        let count = pending
            .iter()
            .map(|c| bars_needed_for(&c.before.datetime))
            .max()
            .unwrap_or(300)
            .max(300);
        let mut month_bars: HashMap<String, Vec<Kline>> = HashMap::new();
        for code in &month_codes {
            match crate::fetch::kline::fetch_minute(&*self.client.read().await, code, "5", count)
                .await
            {
                Ok(rows) => {
                    month_bars.insert(code.clone(), rows);
                }
                Err(e) => {
                    tracing::debug!("{code} 换月确认拉取失败（跳过）: {e}");
                }
            }
        }

        let now = crate::analyze::time::now_display();
        let mut rows = Vec::new();
        for c in &pending {
            match rollover::confirm_candidate(c, &month_bars) {
                Ok(Some((from, to))) => {
                    tracing::info!("{symbol} 识别换月 {from} -> {to} @ {}", c.ts);
                    rows.push(rollover_row(
                        symbol,
                        &c.ts,
                        Some(&from),
                        Some(&to),
                        true,
                        &now,
                    ));
                }
                Ok(None) => {
                    tracing::debug!("{symbol} 候选断点 {} 未获月合约确认，落库为待确认", c.ts);
                    rows.push(rollover_row(symbol, &c.ts, None, None, false, &now));
                }
                Err(e) => {
                    tracing::warn!("{symbol} 候选断点 {} 确认失败: {e}", c.ts);
                    rows.push(rollover_row(symbol, &c.ts, None, None, false, &now));
                }
            }
        }
        repo::upsert_rollovers(&self.db, rows).await?;
        Ok(())
    }

    /// 取某级别的分析用 bar；派生数据不足时从原始 5m 现场聚合兜底。
    pub async fn bars_for(&self, symbol: &str, timeframe: &str) -> Result<Vec<Bar>> {
        let rows = repo::klines(&self.db, symbol, timeframe, None, None).await?;
        let mut bars: Vec<Bar> = rows.iter().filter_map(model_to_bar).collect();
        let rollovers = repo::symbol_rollovers(&self.db, symbol).await?;
        mark_rollover_bars(&mut bars, &rollovers, timeframe);
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
            mark_rollover_bars(&mut bars, &rollovers, timeframe);
        }
        Ok(bars)
    }

    /// 图表数据：5m 读原始库；15m/60m 优先读派生缓存；其余级别现场聚合。
    pub async fn get_klines(
        &self,
        symbol: &str,
        timeframe: &str,
        limit: Option<usize>,
    ) -> Result<Vec<KlineDto>> {
        let tf = Timeframe::parse(timeframe).ok_or_else(|| anyhow!("不支持的级别 {timeframe}"))?;
        let rollovers = repo::symbol_rollovers(&self.db, symbol).await?;
        if tf == Timeframe::M5 {
            let rows = repo::klines(&self.db, symbol, "5m", limit, None).await?;
            return Ok(mark_rollover_models(rows, &rollovers, "5m"));
        }
        if matches!(tf, Timeframe::M15 | Timeframe::M60) {
            let rows = repo::klines(&self.db, symbol, tf.as_str(), None, None).await?;
            if !rows.is_empty() {
                let rows = apply_limit(rows, limit);
                return Ok(mark_rollover_models(rows, &rollovers, tf.as_str()));
            }
        }
        let raw = repo::raw_klines(&self.db, symbol).await?;
        let bars: Vec<Kline> = raw.iter().map(model_to_fetch).collect();
        let derived = aggregate(&bars, tf);
        let mut bars: Vec<Bar> = derived.iter().filter_map(fetch_to_bar).collect();
        mark_rollover_bars(&mut bars, &rollovers, tf.as_str());
        let rows: Vec<KlineDto> = bars
            .iter()
            .map(|b| bar_to_kline_dto(symbol, tf.as_str(), "derived", b))
            .collect();
        Ok(apply_limit_dto(rows, limit))
    }

    /// 全部品种的最新价与涨跌幅（供左侧品种列表展示）。
    pub async fn market_snapshot(&self) -> Result<Vec<MarketSnapshot>> {
        let symbols = repo::list_symbols(&self.db, false).await?;
        let mut out = Vec::with_capacity(symbols.len());
        for s in symbols {
            // 快照只需要“最新收盘价 + 上一交易日收盘价”，最近 200 根 5m 足够，
            // 不再把每个品种的全部K线读出来
            let rows = repo::klines(&self.db, &s.code, "5m", Some(200), None).await?;
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
                (dt.date() + chrono::Days::new(1))
                    .format("%Y-%m-%d")
                    .to_string()
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

    /// 实时现价快照：从新浪批量行情接口拉取启用品种的实时价。
    /// 缺失/解析失败的品种返回 `latest: None`，由前端回退到库内旧数据。
    pub async fn realtime_quotes(&self) -> Result<Vec<MarketSnapshot>> {
        let symbols = repo::list_symbols(&self.db, true).await?;
        let codes: Vec<String> = symbols.iter().map(|s| s.code.clone()).collect();
        if codes.is_empty() {
            return Ok(Vec::new());
        }
        let quotes =
            crate::fetch::quotes::fetch_quotes(&*self.quote_client.read().await, &codes).await?;
        Ok(symbols
            .into_iter()
            .map(|s| {
                let q = quotes.get(&s.code);
                MarketSnapshot {
                    code: s.code,
                    latest: q.map(|q| q.latest),
                    change_pct: q.and_then(|q| q.change_pct),
                }
            })
            .collect())
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
            let tick = crate::precision::effective_tick(sym.tick_size, &sym.code, &sym.variety);
            match crate::analyze::analyze_bars(&sym.code, &bars15, &bars60, tick) {
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

        // 扫描后顺手刷新信号结局：新增信号第一次回填，在途信号按最新K线更新
        if let Err(e) = self.refresh_outcomes().await {
            tracing::warn!("信号结局回填失败: {e}");
        }

        Ok(ScanResult {
            scan_id,
            scanned,
            active_count,
            summary,
            signals: active,
            failed,
        })
    }

    /// 结局回填：对尚无终局（或 open / 数据不足）的信号重新模拟并落库。
    pub async fn refresh_outcomes(&self) -> Result<OutcomeRefresh> {
        let sigs = repo::all_signals(&self.db).await?;
        let outs = repo::all_outcomes(&self.db).await?;
        // 跳过已终结且之后没有换月记录更新的信号；旧版本、未终结或换月更新过的信号重算
        let rollovers = repo::all_rollovers(&self.db).await?;
        let mut latest_rollover_updated: HashMap<String, String> = HashMap::new();
        for r in rollovers {
            let symbol = r.symbol.clone();
            match latest_rollover_updated.get_mut(&symbol) {
                Some(cur) if *cur < r.updated_at => *cur = r.updated_at.clone(),
                Some(_) => {}
                None => {
                    latest_rollover_updated.insert(symbol, r.updated_at);
                }
            }
        }
        let by_id: HashMap<i64, &signal_outcomes::Model> =
            outs.iter().map(|o| (o.signal_id, o)).collect();
        let need: Vec<signals::Model> = sigs
            .into_iter()
            .filter(|s| {
                needs_outcome_refresh(
                    by_id.get(&s.id).copied(),
                    latest_rollover_updated.get(&s.symbol).map(String::as_str),
                )
            })
            .collect();
        if need.is_empty() {
            return Ok(OutcomeRefresh { updated: 0 });
        }

        let mut by_symbol: HashMap<String, Vec<signals::Model>> = HashMap::new();
        for s in need {
            by_symbol.entry(s.symbol.clone()).or_default().push(s);
        }
        let mut annotations: Vec<(i64, outcome::SignalAnnotation)> = Vec::new();
        for (symbol, list) in by_symbol {
            let bars15 = self.bars_for(&symbol, "15m").await?;
            let bars60 = self.bars_for(&symbol, "60m").await?;
            if bars15.len() < 3 {
                continue;
            }
            for s in list {
                if let Some(input) = signal_input_from(&s) {
                    if let Some(ann) = outcome::annotate(&input, &bars15, &bars60) {
                        annotations.push((s.id, ann));
                    }
                }
            }
        }

        let now = crate::analyze::time::now_display();
        let rows: Vec<signal_outcomes::ActiveModel> = annotations
            .into_iter()
            .map(|(id, ann)| signal_outcomes::ActiveModel {
                signal_id: Set(id),
                sim_version: Set(ann.sim_version),
                outcome: Set(ann.outcome.as_str().to_string()),
                exit_reason: Set(ann.exit_reason.as_str().to_string()),
                entry_ts: Set(ann.entry_ts),
                exit_ts: Set(ann.exit_ts),
                exit_price: Set(ann.exit_price),
                r_multiple: Set(ann.r_multiple),
                mfe_r: Set(ann.mfe_r),
                mae_r: Set(ann.mae_r),
                bars_held: Set(ann.bars_held.map(|b| b as i64)),
                vol_ratio: Set(ann.vol_ratio),
                oi_increase: Set(ann.oi_increase),
                trend60_score: Set(ann.trend60_score),
                atr_percentile: Set(ann.atr_percentile),
                rollover_crossed: Set(Some(ann.rollover_crossed)),
                gap_crossed_entry: Set(Some(ann.gap_crossed_entry)),
                gap_crossed_exit: Set(Some(ann.gap_crossed_exit)),
                updated_at: Set(now.clone()),
                ..Default::default()
            })
            .collect();
        let updated = rows.len();
        repo::upsert_outcomes(&self.db, rows).await?;
        Ok(OutcomeRefresh { updated })
    }

    /// 复盘统计：先按结构键去重（取首条），再按维度分组汇总；
    /// scope 控制统计口径（all/tradable/standard）。
    pub async fn review_stats(&self, dimension: &str, scope: &str) -> Result<outcome::ReviewStats> {
        let sigs = repo::all_signals(&self.db).await?;
        let outs = repo::all_outcomes(&self.db).await?;
        let by_id: HashMap<i64, signal_outcomes::Model> =
            outs.into_iter().map(|o| (o.signal_id, o)).collect();
        let rows: Vec<outcome::StatRow> = sigs
            .iter()
            .filter_map(|s| stat_row_from(s, by_id.get(&s.id)))
            .collect();
        Ok(outcome::aggregate_stats_scoped(
            &rows,
            outcome::GroupBy::parse(dimension),
            outcome::StatsScope::parse(scope),
        ))
    }

    /// 最近信号明细（复盘页明细表）：已回填结局的信号，按 signal_id 倒序。
    pub async fn recent_outcomes(
        &self,
        limit: usize,
        filter: &OutcomeFilter,
    ) -> Result<Vec<OutcomeDetail>> {
        let sigs = repo::all_signals(&self.db).await?;
        let outs = repo::all_outcomes(&self.db).await?;
        let by_id: HashMap<i64, signal_outcomes::Model> =
            outs.into_iter().map(|o| (o.signal_id, o)).collect();
        // 与统计口径一致：同一结构（品种+方向+级别+s1/s2 时间）只保留首次识别的快照
        let mut seen: HashSet<String> = HashSet::new();
        let mut rows: Vec<OutcomeDetail> = Vec::new();
        for s in &sigs {
            let Some(o) = by_id.get(&s.id) else {
                continue;
            };
            let (_, s1_ts, s2_ts) = parse_detail_ts(&s.detail);
            let key = match (&s1_ts, &s2_ts) {
                (Some(a), Some(b)) => {
                    format!("{}|{}|{}|{}|{}", s.symbol, s.direction, s.level, a, b)
                }
                // 旧数据缺少结构时间戳时退回信号自身，不做合并
                _ => format!("{}|{}|{}|id{}", s.symbol, s.direction, s.level, s.id),
            };
            if !seen.insert(key) {
                continue;
            }
            if !matches_outcome_filter(s, o, filter) {
                continue;
            }
            rows.push(outcome_detail_from(s, o));
        }
        rows.sort_by(|a, b| b.signal_id.cmp(&a.signal_id));
        rows.truncate(limit);
        Ok(rows)
    }

    /// 复盘跳转K线图：按 signal_id 返回完整形态 + 结局。
    pub async fn review_signal(&self, signal_id: i64) -> Result<Option<ReviewSignalDetail>> {
        let Some(row) = repo::signal_by_id(&self.db, signal_id).await? else {
            return Ok(None);
        };
        let Ok(pattern) = serde_json::from_str::<crate::analyze::dto::PatternDto>(&row.detail)
        else {
            return Ok(None);
        };
        let outcome = match repo::outcome_by_signal(&self.db, signal_id).await? {
            Some(o) => Some(outcome_detail_from(&row, &o)),
            None => None,
        };
        Ok(Some(ReviewSignalDetail { pattern, outcome }))
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
    out.push_str(&format!(
        "共扫描 {scanned} 个品种，{active_count} 个品种有关注信号\n"
    ));
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

fn apply_limit_dto(rows: Vec<KlineDto>, limit: Option<usize>) -> Vec<KlineDto> {
    match limit {
        Some(limit) if rows.len() > limit => rows[rows.len() - limit..].to_vec(),
        _ => rows,
    }
}

fn bar_to_kline_dto(symbol: &str, timeframe: &str, source: &str, bar: &Bar) -> KlineDto {
    KlineDto {
        symbol: symbol.to_string(),
        timeframe: timeframe.to_string(),
        ts: format!(
            "{:04}-{:02}-{:02} {:02}:{:02}:00",
            bar.dt.year, bar.dt.month, bar.dt.day, bar.dt.hour, bar.dt.minute
        ),
        open: bar.open,
        high: bar.high,
        low: bar.low,
        close: bar.close,
        volume: bar.volume,
        hold: bar.hold,
        source: source.to_string(),
        rollover: bar.rollover,
    }
}

fn mark_rollover_models(
    rows: Vec<klines::Model>,
    rollovers: &[crate::storage::entities::rollovers::Model],
    timeframe: &str,
) -> Vec<KlineDto> {
    let mut bars: Vec<Bar> = rows.iter().filter_map(model_to_bar).collect();
    mark_rollover_bars(&mut bars, rollovers, timeframe);
    rows.into_iter()
        .zip(bars)
        .map(|(m, b)| KlineDto {
            symbol: m.symbol,
            timeframe: m.timeframe,
            ts: m.ts,
            open: m.open,
            high: m.high,
            low: m.low,
            close: m.close,
            volume: m.volume,
            hold: m.hold,
            source: m.source,
            rollover: b.rollover,
        })
        .collect()
}

fn ts_gap_minutes(later: &str, earlier: &str) -> Option<i64> {
    let fmt = "%Y-%m-%d %H:%M:%S";
    let a = chrono::NaiveDateTime::parse_from_str(later, fmt).ok()?;
    let b = chrono::NaiveDateTime::parse_from_str(earlier, fmt).ok()?;
    Some((a - b).num_minutes())
}

/// 信号是否处于关注中（未过期/未失效），入场价提醒只针对这些信号。
fn is_active_signal_state(state: &str) -> bool {
    matches!(state, "即将触发" | "当前已触发" | "已触发，接近时效边界")
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
        rollover: false,
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
        rollover: false,
    })
}

/// 连续合约代码取品种前缀（BU0 -> BU；已存在的非连续代码原样返回）。
fn contract_prefix(symbol: &str) -> String {
    let trimmed = symbol.trim_end_matches(|c: char| c.is_ascii_digit());
    if trimmed.is_empty() {
        symbol.to_string()
    } else {
        trimmed.to_string()
    }
}

/// 断点距今的分钟数折算成 5m 根数，保证月合约切片能覆盖断点两侧。
fn bars_needed_for(ts: &str) -> usize {
    let fmt = "%Y-%m-%d %H:%M:%S";
    let Ok(dt) = chrono::NaiveDateTime::parse_from_str(ts, fmt) else {
        return 300;
    };
    let now = chrono::Local::now().naive_local();
    let mins = (now - dt).num_minutes().max(0);
    ((mins / 5) as usize).max(300)
}

fn rollover_row(
    symbol: &str,
    ts: &str,
    from: Option<&str>,
    to: Option<&str>,
    confirmed: bool,
    now: &str,
) -> crate::storage::entities::rollovers::ActiveModel {
    use crate::storage::entities::rollovers;
    use sea_orm::Set;
    rollovers::ActiveModel {
        symbol: Set(symbol.to_string()),
        ts: Set(ts.to_string()),
        from_contract: Set(from.unwrap_or("").to_string()),
        to_contract: Set(to.unwrap_or("").to_string()),
        confirmed: Set(confirmed),
        created_at: Set(now.to_string()),
        updated_at: Set(now.to_string()),
    }
}

/// 把 rollovers 表的时间戳标记到目标级别的 bar 上：5m 精确到该根，
/// 15m/60m 标记换月后第一根聚合 bar（如 21:05 -> 15m 的 21:15、60m 的 22:00）。
fn mark_rollover_bars(
    bars: &mut [Bar],
    rollovers: &[crate::storage::entities::rollovers::Model],
    timeframe: &str,
) {
    if bars.is_empty() || rollovers.is_empty() {
        return;
    }
    let is_5m = timeframe == "5m";
    let mut ri = 0usize;
    for bar in bars.iter_mut() {
        while ri < rollovers.len() {
            if !rollovers[ri].confirmed {
                ri += 1;
                continue;
            }
            let ts = &rollovers[ri].ts;
            let bar_start = format!(
                "{:04}-{:02}-{:02} {:02}:{:02}:00",
                bar.dt.year, bar.dt.month, bar.dt.day, bar.dt.hour, bar.dt.minute
            );
            let hit = if is_5m {
                bar_start == *ts
            } else {
                bar_start >= *ts
            };
            if hit {
                bar.rollover = true;
                ri += 1;
            } else if bar_start < *ts {
                break;
            } else {
                ri += 1;
            }
        }
    }
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

    #[test]
    fn mark_rollover_bars_5m_exact_and_aggregated_first_after() {
        use crate::storage::entities::rollovers;
        let rollover = |ts: &str| rollovers::Model {
            symbol: "BU0".to_string(),
            ts: ts.to_string(),
            from_contract: "BU2609".to_string(),
            to_contract: "BU2610".to_string(),
            confirmed: true,
            created_at: "2026-08-05 21:10:00".to_string(),
            updated_at: "2026-08-05 21:10:00".to_string(),
        };
        let mut bars = vec![
            parse_dt("2026-08-05 15:00:00").map(dt_to_bar),
            parse_dt("2026-08-05 21:00:00").map(dt_to_bar),
            parse_dt("2026-08-05 21:15:00").map(dt_to_bar),
        ]
        .into_iter()
        .flatten()
        .collect::<Vec<_>>();
        mark_rollover_bars(&mut bars, &[rollover("2026-08-05 21:05:00")], "5m");
        assert!(bars.iter().all(|b| !b.rollover)); // 5m 没有 21:05 这根时不标记

        let mut bars = vec![
            parse_dt("2026-08-05 15:00:00").map(dt_to_bar),
            parse_dt("2026-08-05 21:00:00").map(dt_to_bar),
            parse_dt("2026-08-05 21:15:00").map(dt_to_bar),
        ]
        .into_iter()
        .flatten()
        .collect::<Vec<_>>();
        mark_rollover_bars(&mut bars, &[rollover("2026-08-05 21:05:00")], "15m");
        assert!(!bars[0].rollover);
        assert!(!bars[1].rollover);
        assert!(bars[2].rollover);
    }

    #[test]
    fn mark_rollover_models_marks_aggregated_first_bar() {
        use crate::storage::entities::rollovers;
        let rows = vec![
            kline_model("2026-08-05 15:00:00"),
            kline_model("2026-08-05 21:00:00"),
            kline_model("2026-08-05 21:15:00"),
        ];
        let rollovers = vec![rollovers::Model {
            symbol: "BU0".to_string(),
            ts: "2026-08-05 21:05:00".to_string(),
            from_contract: "BU2609".to_string(),
            to_contract: "BU2610".to_string(),
            confirmed: true,
            created_at: "2026-08-05 21:10:00".to_string(),
            updated_at: "2026-08-05 21:10:00".to_string(),
        }];
        let out = mark_rollover_models(rows, &rollovers, "15m");
        assert!(!out[0].rollover);
        assert!(!out[1].rollover);
        assert!(out[2].rollover);
        assert_eq!(out[2].ts, "2026-08-05 21:15:00");
        assert_eq!(out[2].source, "derived");
    }

    #[test]
    fn contract_prefix_strips_continuous_digits() {
        assert_eq!(contract_prefix("BU0"), "BU");
        assert_eq!(contract_prefix("RB2610"), "RB");
        assert_eq!(contract_prefix("0"), "0");
    }

    #[test]
    fn needs_refresh_when_rollover_updated_after_terminal_outcome() {
        let o = outcome_model("win", outcome::SIM_VERSION, "2026-08-10 02:05:00");
        assert!(needs_outcome_refresh(Some(&o), Some("2026-08-10 09:26:00")));
        assert!(!needs_outcome_refresh(
            Some(&o),
            Some("2026-08-10 02:05:00")
        ));
        assert!(!needs_outcome_refresh(
            Some(&o),
            Some("2026-08-10 01:00:00")
        ));
        assert!(!needs_outcome_refresh(Some(&o), None));
    }

    #[test]
    fn needs_refresh_for_missing_or_non_terminal_outcome() {
        assert!(needs_outcome_refresh(None, None));

        let rollover = outcome_model("rollover", outcome::SIM_VERSION, "2026-08-10 02:05:00");
        assert!(needs_outcome_refresh(
            Some(&rollover),
            Some("2026-08-10 09:26:00")
        ));

        let old_version = outcome_model("win", outcome::SIM_VERSION - 1, "2026-08-10 02:05:00");
        assert!(needs_outcome_refresh(Some(&old_version), None));

        let open = outcome_model("open", outcome::SIM_VERSION, "2026-08-10 02:05:00");
        assert!(needs_outcome_refresh(Some(&open), None));
    }
}

#[cfg(test)]
fn dt_to_bar(dt: DT) -> Bar {
    Bar {
        dt,
        open: 0.0,
        high: 0.0,
        low: 0.0,
        close: 0.0,
        volume: 0.0,
        hold: 0.0,
        rollover: false,
    }
}

#[cfg(test)]
fn kline_model(ts: &str) -> klines::Model {
    klines::Model {
        symbol: "BU0".to_string(),
        timeframe: "15m".to_string(),
        ts: ts.to_string(),
        open: 0.0,
        high: 0.0,
        low: 0.0,
        close: 0.0,
        volume: 0.0,
        hold: 0.0,
        source: "derived".to_string(),
    }
}

#[cfg(test)]
fn outcome_model(outcome: &str, sim_version: i64, updated_at: &str) -> signal_outcomes::Model {
    signal_outcomes::Model {
        signal_id: 1,
        sim_version,
        outcome: outcome.to_string(),
        exit_reason: String::new(),
        entry_ts: None,
        exit_ts: None,
        exit_price: None,
        r_multiple: None,
        mfe_r: None,
        mae_r: None,
        bars_held: None,
        vol_ratio: None,
        oi_increase: None,
        trend60_score: None,
        atr_percentile: None,
        rollover_crossed: Some(false),
        gap_crossed_entry: Some(false),
        gap_crossed_exit: Some(false),
        updated_at: updated_at.to_string(),
    }
}
