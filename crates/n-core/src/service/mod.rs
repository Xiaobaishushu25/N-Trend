//! 应用服务层：把抓取、存储、派生、分析串成完整业务流程。

use anyhow::{anyhow, Result};
use chrono::Timelike;
use sea_orm::{DatabaseConnection, Set};
use serde::Serialize;
use std::collections::{HashMap, HashSet};
use std::time::{Duration, Instant};
use tokio::sync::{Mutex, RwLock};

use crate::analyze::event;
use crate::analyze::model::{Bar, Dir, Grade, NPattern, Swing, ATR_PERIOD, DT};
use crate::analyze::outcome;
use crate::config::Config;
use crate::derive::{aggregate, rollover, Timeframe};
use crate::fetch::kline::{Kline, MINUTE_BAR_SETTLE_SECS};
use crate::fetch::SinaClient;
use crate::scheduler::SchedulerConfig;
use crate::storage::entities::{klines, pattern_events, symbols};
use crate::storage::repo;

const MONTH_KLINE_CACHE_TTL: Duration = Duration::from_secs(900);
const ROLLOVER_SCAN_SETTING_PREFIX: &str = "rollover_scanned::";
const ROLLOVER_PENDING_RETENTION_DAYS: i64 = 30;

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
    pub scanned: i64,
    pub active_count: i64,
    pub summary: String,
    pub signals: Vec<pattern_events::Model>,
    /// 本轮扫描新识别出的预警事件
    pub new_warnings: Vec<pattern_events::Model>,
    /// 本轮推进时新触发的事件
    pub newly_triggered: Vec<pattern_events::Model>,
    pub failed: Vec<SymbolFailure>,
}

impl ScanResult {
    /// 是否存在评分达到通知阈值的新预警或新触发，应用内卡片与邮件共用同一门槛。
    pub fn has_notifiable_signal(&self, min_score: f64) -> bool {
        self.new_warnings
            .iter()
            .chain(self.newly_triggered.iter())
            .any(|e| e.entry_score >= min_score)
    }
}

/// 结局回填结果（复盘页刷新按钮返回值）。
#[derive(Debug, Clone, Default, Serialize)]
pub struct OutcomeRefresh {
    pub updated: usize,
}

/// 复盘页明细表的一行（信号快照 + 结局 + 特征）。
#[derive(Debug, Clone, Serialize)]
pub struct OutcomeDetail {
    pub event_id: i64,
    pub symbol: String,
    /// 事件版本：前向事件系统固定为 4
    pub logic_version: String,
    pub warning_kind: String,
    pub warning_ts: String,
    pub detected_at: String,
    pub direction: String,
    pub level: String,
    pub grade: String,
    pub entry_score: f64,
    pub entry_score_dims: String,
    pub s0_ts: String,
    pub s0_price: f64,
    pub s1_ts: String,
    pub s1_price: f64,
    pub s2_ts: String,
    pub s2_price: f64,
    pub entry: f64,
    pub stop: f64,
    pub target: f64,
    pub risk: f64,
    pub rr: f64,
    pub created_at: String,
    pub state: String,
    pub outcome: String,
    pub exit_reason: String,
    pub trigger_ts: Option<String>,
    pub trigger_bar_ts: Option<String>,
    pub trigger_price: Option<f64>,
    pub trigger_score: Option<f64>,
    pub trigger_volume_ratio: Option<f64>,
    pub overshoot_r: Option<f64>,
    pub hold_score: Option<f64>,
    pub exit_ts: Option<String>,
    pub exit_price: Option<f64>,
    pub r_multiple: Option<f64>,
    pub mfe_r: Option<f64>,
    pub mae_r: Option<f64>,
    pub bars_held: Option<usize>,
    pub a_move: Option<f64>,
    pub b_move: Option<f64>,
    pub a_bars: Option<usize>,
    pub b_bars: Option<usize>,
    pub retracement: Option<f64>,
    /// A段逐K质量 q（ATR加权，不含幅度/速度项）
    pub a_q: Option<f64>,
    /// A段净推进幅度 = a_move - A段大跳空合计
    pub a_net_move: Option<f64>,
    /// A段大跳空合计（点数）
    pub a_gap_sum: Option<f64>,
    /// A段大跳空根数
    pub a_gap_count: Option<usize>,
    /// A段评分所用 ATR（S1 处）
    pub a_atr: Option<f64>,
    pub a_too_long: Option<bool>,
    pub b_too_long: Option<bool>,
    pub b_fast: Option<bool>,
    pub b_weakening: Option<bool>,
    pub b_weakening_ratio: Option<f64>,
    pub net_r: Option<f64>,
    pub rollover_crossed: bool,
    pub gap_crossed_entry: bool,
    pub gap_crossed_exit: bool,
    /// 用户批注（按创建时间正序）
    pub annotations: Vec<SignalAnnotationDto>,
    /// 用户是否按建议开仓；未记录时为 None
    pub opened: Option<bool>,
}

/// 复盘明细跳转K线图所需：完整事件 + 结局。
#[derive(Debug, Clone, Serialize)]
pub struct ReviewSignalDetail {
    pub event: pattern_events::Model,
    pub outcome: Option<OutcomeDetail>,
    pub annotations: Vec<SignalAnnotationDto>,
    pub opened: Option<bool>,
}

/// 用户给信号写的批注。
#[derive(Debug, Clone, Serialize)]
pub struct SignalAnnotationDto {
    pub id: i64,
    pub event_id: i64,
    pub content: String,
    pub created_at: String,
}

/// 用户是否按建议开仓的记录。
#[derive(Debug, Clone, Serialize)]
pub struct SignalDecisionDto {
    pub event_id: i64,
    pub opened: bool,
    pub updated_at: String,
}

/// 单个信号的用户记录聚合（K线右侧卡片用）。
#[derive(Debug, Clone, Serialize)]
pub struct SignalUserData {
    pub annotations: Vec<SignalAnnotationDto>,
    pub opened: Option<bool>,
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
    /// win / loss / no_trigger / open / insufficient_data / rollover
    pub outcome: Option<String>,
    /// 5，缺省不过滤
    pub version: Option<String>,
}

const EVENT_LOGIC_VERSION: &str = "5";

fn pattern_endpoint_prices(bars: &[Bar], candidate: &event::WarningCandidate) -> (f64, f64, f64) {
    if candidate.direction == Dir::Up {
        (
            bars[candidate.s0_index].low,
            bars[candidate.s1_index].high,
            bars[candidate.s2_index].low,
        )
    } else {
        (
            bars[candidate.s0_index].high,
            bars[candidate.s1_index].low,
            bars[candidate.s2_index].high,
        )
    }
}

async fn insert_warning_event(
    db: &DatabaseConnection,
    symbol: &str,
    bars: &[Bar],
    candidate: &event::WarningCandidate,
) -> Result<pattern_events::Model> {
    let direction = if candidate.direction == Dir::Up {
        "up"
    } else {
        "down"
    };
    let warning_ts = bar_ts(&bars[candidate.warning_index]);
    let now = now_ts();
    let dims = serde_json::json!({
        "dim_a": candidate.dim_a,
        "dim_b": candidate.dim_b,
        "dim_warning": candidate.dim_warning,
        "trend_state": candidate.trend_state,
        "trend_bonus": candidate.trend_bonus,
    })
    .to_string();
    let (s0_price, s1_price, s2_price) = pattern_endpoint_prices(bars, candidate);
    let row = pattern_events::ActiveModel {
        symbol: Set(symbol.to_string()),
        direction: Set(direction.to_string()),
        grade: Set(candidate.grade.clone()),
        level: Set(candidate.level.to_string()),
        s0_ts: Set(bar_ts(&bars[candidate.s0_index])),
        s0_price: Set(s0_price),
        s1_ts: Set(bar_ts(&bars[candidate.s1_index])),
        s1_price: Set(s1_price),
        s2_ts: Set(bar_ts(&bars[candidate.s2_index])),
        s2_price: Set(s2_price),
        a_move: Set(candidate.a_move),
        b_move: Set(candidate.b_move),
        a_bars: Set(candidate.a_bars as i64),
        b_bars: Set(candidate.b_bars as i64),
        retracement: Set(candidate.retracement),
        warning_ts: Set(warning_ts.clone()),
        detected_at: Set(warning_ts.clone()),
        warning_kind: Set(candidate.warning_kind.to_string()),
        entry_score: Set(candidate.entry_score),
        entry_score_dims: Set(dims),
        entry: Set(candidate.entry),
        stop: Set(candidate.stop),
        target: Set(candidate.target),
        risk: Set(candidate.risk),
        rr: Set(candidate.rr),
        state: Set("pending".to_string()),
        last_advance_ts: Set(Some(warning_ts)),
        trigger_ts: Set(None),
        trigger_bar_ts: Set(None),
        trigger_price: Set(None),
        trigger_score: Set(None),
        trigger_volume_ratio: Set(None),
        overshoot_r: Set(None),
        hold_score: Set(None),
        hold_score_history: Set("[]".to_string()),
        outcome: Set(None),
        exit_reason: Set(None),
        exit_ts: Set(None),
        exit_price: Set(None),
        r_multiple: Set(None),
        mfe_r: Set(None),
        mae_r: Set(None),
        created_at: Set(now.clone()),
        updated_at: Set(now),
        ..Default::default()
    };
    let id = repo::insert_pattern_event(db, row).await?;
    repo::pattern_event_by_id(db, id)
        .await?
        .ok_or_else(|| anyhow!("写入事件 {id} 后读取失败"))
}

/// 相似预警抑制：同品种、同方向、预警K线相差不超过 5 根 15m 时，如果既有
/// 事件仍处于未触发或已触发持仓状态，直接抑制新事件；前一条已离场后才退回
/// 入场价差不超过 0.3R 的相似判断。历史重放时会按预警K线先后逐条插入，
/// 因此这里比对全部既有事件（含已触发/已了结），避免旧事件重新生成。
fn has_similar_warning(
    bars: &[Bar],
    candidate: &event::WarningCandidate,
    events: &[pattern_events::Model],
) -> Result<bool> {
    let direction = if candidate.direction == Dir::Up {
        "up"
    } else {
        "down"
    };
    let warning_ts = bar_ts(&bars[candidate.warning_index]);
    Ok(events.iter().any(|e| {
        e.direction == direction
            && bar_gap(bars, &warning_ts, &e.warning_ts)
                .is_some_and(|bars_gap| bars_gap <= outcome::DEDUP_WARNING_BARS)
            && e.risk > 0.0
            && (event_active_at(e, &warning_ts)
                || (candidate.entry - e.entry).abs() <= outcome::DEDUP_ENTRY_R * e.risk)
    }))
}

/// 既有事件在指定预警K线收盘时间是否仍处于“未触发/已触发持仓”状态。
fn event_active_at(e: &pattern_events::Model, warning_ts: &str) -> bool {
    match e.state.as_str() {
        "pending" => true,
        "triggered" => e.exit_ts.as_deref().map_or(true, |exit| exit > warning_ts),
        "closed" => e
            .trigger_ts
            .as_deref()
            .zip(e.exit_ts.as_deref())
            .is_some_and(|(trigger, exit)| trigger <= warning_ts && warning_ts < exit),
        _ => false,
    }
}

/// 找出应删除的重复事件：同品种、同方向、预警K线相差不超过 5 根 15m 时，
/// 若最近一条族内事件在后续预警时仍持仓/未触发则直接并入；最近一条已离场
/// 则要求与族首的入场价差不超过 0.3R。族内只保留首见一条，其余返回给调用方
/// 删除。旧数据缺少入场价时退回结构键去重。
fn duplicate_event_ids(
    events: &[pattern_events::Model],
    bar_index: &outcome::WarningBarIndex,
) -> Vec<i64> {
    let mut ordered = events.to_vec();
    ordered.sort_by(|a, b| {
        a.warning_ts
            .cmp(&b.warning_ts)
            .then_with(|| a.id.cmp(&b.id))
    });

    let mut families: Vec<(&pattern_events::Model, &pattern_events::Model, String)> = Vec::new();
    let mut structure_min: HashMap<String, &pattern_events::Model> = HashMap::new();
    let mut delete_ids: Vec<i64> = Vec::new();

    for e in &ordered {
        if e.risk > 0.0 {
            let mut merged = false;
            for (anchor, last, last_warning_ts) in &mut families {
                if e.symbol == anchor.symbol
                    && e.direction == anchor.direction
                    && bar_index
                        .get(&(e.symbol.clone(), e.warning_ts.clone()))
                        .zip(bar_index.get(&(anchor.symbol.clone(), last_warning_ts.clone())))
                        .is_some_and(|(li, ei)| li.abs_diff(*ei) <= outcome::DEDUP_WARNING_BARS)
                    && (event_active_at(last, &e.warning_ts)
                        || (e.entry - anchor.entry).abs()
                            <= outcome::DEDUP_ENTRY_R * anchor.risk.max(1e-9))
                {
                    merged = true;
                    delete_ids.push(e.id);
                    *last_warning_ts = e.warning_ts.clone();
                    *last = e;
                    break;
                }
            }
            if merged {
                continue;
            }
            families.push((e, e, e.warning_ts.clone()));
            continue;
        }

        // 缺少入场价的历史记录不做删除，避免误伤；这里保留结构键分支以便
        // 与复盘统计口径一致地说明旧数据不会进入删除范围。
        let key = format!(
            "{}|{}|{}|{}|{}|{}",
            e.symbol, EVENT_LOGIC_VERSION, e.direction, e.level, e.s1_ts, e.s2_ts
        );
        if let Some(cur) = structure_min.get(&key) {
            if e.id < cur.id {
                delete_ids.push(cur.id);
                structure_min.insert(key, e);
            } else {
                delete_ids.push(e.id);
            }
        } else {
            structure_min.insert(key, e);
        }
    }
    delete_ids
}

fn event_outcome_str(e: &pattern_events::Model) -> String {
    if let Some(outcome) = e.outcome.as_deref() {
        return outcome.to_string();
    }
    if e.state == "expired" {
        "no_trigger".to_string()
    } else {
        "open".to_string()
    }
}

fn matches_outcome_filter(e: &pattern_events::Model, f: &OutcomeFilter) -> bool {
    if let Some(sym) = f.symbol.as_deref().filter(|x| !x.is_empty()) {
        if !e.symbol.to_lowercase().contains(&sym.to_lowercase()) {
            return false;
        }
    }
    if let Some(d) = f.direction.as_deref().filter(|x| !x.is_empty()) {
        if e.direction != d {
            return false;
        }
    }
    if let Some(l) = f.level.as_deref().filter(|x| !x.is_empty()) {
        if e.level != l {
            return false;
        }
    }
    if let Some(g) = f.grade.as_deref().filter(|x| !x.is_empty()) {
        if e.grade != g {
            return false;
        }
    }
    if !score_in_range(e.entry_score, f.score_min, f.score_max) {
        return false;
    }
    if let Some(out) = f.outcome.as_deref().filter(|x| !x.is_empty()) {
        if event_outcome_str(e) != out {
            return false;
        }
    }
    if let Some(v) = f.version.as_deref().filter(|x| !x.is_empty()) {
        if EVENT_LOGIC_VERSION != v {
            return false;
        }
    }
    true
}

fn score_in_range(score: f64, min: Option<f64>, max: Option<f64>) -> bool {
    if let Some(m) = min {
        if score < m {
            return false;
        }
    }
    if let Some(m) = max {
        if score > m {
            return false;
        }
    }
    true
}

fn parse_entry_dims(dims: &str) -> Option<[f64; 3]> {
    let v: serde_json::Value = serde_json::from_str(dims).ok()?;
    let a = v.get("dim_a")?.as_f64()?;
    let b = v.get("dim_b")?.as_f64()?;
    let w = v.get("dim_warning")?.as_f64()?;
    Some([a, b, w])
}

fn stat_row_from(e: &pattern_events::Model, tick: Option<f64>) -> Option<outcome::StatRow> {
    let risk = e.risk;
    let outcome = e
        .outcome
        .as_deref()
        .and_then(outcome::Outcome::parse)
        .or_else(|| {
            if e.state == "expired" {
                Some(outcome::Outcome::NoTrigger)
            } else {
                Some(outcome::Outcome::Open)
            }
        });
    Some(outcome::StatRow {
        signal_id: e.id,
        symbol: e.symbol.clone(),
        logic_version: EVENT_LOGIC_VERSION.to_string(),
        direction: e.direction.clone(),
        level: e.level.clone(),
        grade: e.grade.clone(),
        score: e.entry_score,
        created_at: e.created_at.clone(),
        warning_ts: Some(e.warning_ts.clone()),
        s0_ts: Some(e.s0_ts.clone()),
        s1_ts: Some(e.s1_ts.clone()),
        s2_ts: Some(e.s2_ts.clone()),
        trigger_bar_ts: e.trigger_bar_ts.clone(),
        entry: Some(e.entry),
        risk: e.risk,
        outcome,
        r_multiple: e.r_multiple,
        mfe_r: e.mfe_r,
        mae_r: e.mae_r,
        bars_held: e
            .trigger_ts
            .as_deref()
            .zip(e.exit_ts.as_deref())
            .and_then(|(a, b)| ts_diff_bars(a, b).map(|n| n + 1)),
        vol_ratio: e.trigger_volume_ratio,
        oi_increase: None,
        trend60_score: None,
        atr_percentile: None,
        exit_reason: e.exit_reason.as_deref().map(outcome::ExitReason::parse),
        target_tier: None,
        extended_target: e.rr > 1.0,
        b_vol_ratio: None,
        a_move_atr: None,
        trigger_lag_bars: Some(e.warning_ts.as_str())
            .zip(e.trigger_bar_ts.as_deref())
            .and_then(|(a, b)| ts_diff_bars(a, b)),
        trigger_overshoot_r: e.overshoot_r,
        a_move: Some(e.a_move),
        b_move: Some(e.b_move),
        a_bars: Some(e.a_bars as usize),
        b_bars: Some(e.b_bars as usize),
        retracement: Some(e.retracement),
        dims: parse_entry_dims(&e.entry_score_dims),
        net_r: e
            .r_multiple
            .zip(tick)
            .filter(|_| risk > 0.0)
            .map(|(r, t)| r - 2.5 * t / risk),
        rollover_crossed: e.outcome.as_deref() == Some("rollover"),
        gap_crossed_entry: false,
        gap_crossed_exit: false,
    })
}

fn outcome_detail_from(e: &pattern_events::Model, tick: Option<f64>) -> OutcomeDetail {
    let risk = e.risk;
    OutcomeDetail {
        event_id: e.id,
        symbol: e.symbol.clone(),
        logic_version: EVENT_LOGIC_VERSION.to_string(),
        warning_kind: e.warning_kind.clone(),
        warning_ts: e.warning_ts.clone(),
        detected_at: e.detected_at.clone(),
        direction: e.direction.clone(),
        level: e.level.clone(),
        grade: e.grade.clone(),
        entry_score: e.entry_score,
        entry_score_dims: e.entry_score_dims.clone(),
        s0_ts: e.s0_ts.clone(),
        s0_price: e.s0_price,
        s1_ts: e.s1_ts.clone(),
        s1_price: e.s1_price,
        s2_ts: e.s2_ts.clone(),
        s2_price: e.s2_price,
        entry: e.entry,
        stop: e.stop,
        target: e.target,
        risk: e.risk,
        rr: e.rr,
        created_at: e.created_at.clone(),
        state: e.state.clone(),
        outcome: event_outcome_str(e),
        exit_reason: e.exit_reason.clone().unwrap_or_default(),
        trigger_ts: e.trigger_ts.clone(),
        trigger_bar_ts: e.trigger_bar_ts.clone(),
        trigger_price: e.trigger_price,
        trigger_score: e.trigger_score,
        trigger_volume_ratio: e.trigger_volume_ratio,
        overshoot_r: e.overshoot_r,
        hold_score: e.hold_score,
        exit_ts: e.exit_ts.clone(),
        exit_price: e.exit_price,
        r_multiple: e.r_multiple,
        mfe_r: e.mfe_r,
        mae_r: e.mae_r,
        bars_held: e
            .trigger_ts
            .as_deref()
            .zip(e.exit_ts.as_deref())
            .and_then(|(a, b)| ts_diff_bars(a, b).map(|n| n + 1)),
        a_move: Some(e.a_move),
        b_move: Some(e.b_move),
        a_bars: Some(e.a_bars as usize),
        b_bars: Some(e.b_bars as usize),
        retracement: Some(e.retracement),
        a_q: None,
        a_net_move: None,
        a_gap_sum: None,
        a_gap_count: None,
        a_atr: None,
        a_too_long: None,
        b_too_long: None,
        b_fast: None,
        b_weakening: None,
        b_weakening_ratio: None,
        net_r: e
            .r_multiple
            .zip(tick)
            .filter(|_| risk > 0.0)
            .map(|(r, t)| r - 2.5 * t / risk),
        rollover_crossed: e.outcome.as_deref() == Some("rollover"),
        gap_crossed_entry: false,
        gap_crossed_exit: false,
        annotations: Vec::new(),
        opened: None,
    }
}

/// 用 15m K线现场回算 A/B 段结构细节，供复盘卡片展示。
/// 只依赖 S0/S1/S2 定死的时间与幅度，不随后续行情改变。
fn fill_leg_detail(
    row: &mut OutcomeDetail,
    bars: &[Bar],
    atr20: &[Option<f64>],
    index_by_ts: &HashMap<String, usize>,
) {
    let Some(&s0_index) = index_by_ts.get(&row.s0_ts) else {
        return;
    };
    let Some(&s1_index) = index_by_ts.get(&row.s1_ts) else {
        return;
    };
    let Some(&s2_index) = index_by_ts.get(&row.s2_ts) else {
        return;
    };
    let dir = if row.direction == "down" {
        Dir::Down
    } else {
        Dir::Up
    };
    let a_bars = row
        .a_bars
        .unwrap_or_else(|| s1_index.saturating_sub(s0_index) + 1);
    let b_bars = row
        .b_bars
        .unwrap_or_else(|| s2_index.saturating_sub(s1_index));
    let a_speed = row.a_move.unwrap_or(0.0) / a_bars.max(1) as f64;
    let b_speed = row.b_move.unwrap_or(0.0) / b_bars.max(1) as f64;
    let p = NPattern {
        level: if row.level == "large" {
            "large"
        } else {
            "fine"
        },
        dir,
        s0: Swing {
            index: s0_index,
            price: row.s0_price,
            is_high: dir == Dir::Down,
        },
        s1: Swing {
            index: s1_index,
            price: row.s1_price,
            is_high: dir == Dir::Up,
        },
        s2: Swing {
            index: s2_index,
            price: row.s2_price,
            is_high: dir == Dir::Down,
        },
        a_bars,
        b_bars,
        a_move: row.a_move.unwrap_or(0.0),
        b_move: row.b_move.unwrap_or(0.0),
        retracement: row.retracement.unwrap_or(0.0),
        grade: Grade::A,
        hard_failure: false,
        a_too_long: a_bars > 7,
        b_too_long: b_bars > 8,
        b_fast: a_speed > 0.0 && b_speed > 0.8 * a_speed,
        b_weakening: false,
        b_weakening_ratio: None,
        a_strong_trend: 0,
        b_strong_reverse: 0,
        c_move: 0.0,
        c_bars: 0,
        c_extended: false,
        c_hard_failure: false,
    };
    let d = crate::analyze::scoring::a_leg_detail(bars, atr20, &p);
    row.a_q = Some(d.q);
    row.a_net_move = Some(d.net_move);
    row.a_gap_sum = Some(d.gap_sum);
    row.a_gap_count = Some(d.gap_count);
    row.a_atr = Some(d.atr);
    row.a_too_long = Some(p.a_too_long);
    row.b_too_long = Some(p.b_too_long);
    row.b_fast = Some(p.b_fast);
    let (weakening, ratio) =
        crate::analyze::pattern::b_leg_weakening(bars, s1_index, s2_index, dir);
    row.b_weakening = Some(weakening);
    row.b_weakening_ratio = ratio;
}

fn now_ts() -> String {
    chrono::Local::now().format("%Y-%m-%d %H:%M:%S").to_string()
}

fn bar_ts(bar: &Bar) -> String {
    format!(
        "{:04}-{:02}-{:02} {:02}:{:02}:00",
        bar.dt.year, bar.dt.month, bar.dt.day, bar.dt.hour, bar.dt.minute
    )
}

/// 形态扫描与事件推进只使用已过确认余量的 15m bar，
/// 避免把最后一根仍在确认期的成分K线当成最终值。
fn settled_scan_bars_at(bars: Vec<Bar>, now: chrono::NaiveDateTime) -> Vec<Bar> {
    bars.into_iter()
        .filter(|b| {
            let Some(ts) = chrono::NaiveDate::from_ymd_opt(
                b.dt.year,
                b.dt.month as u32,
                b.dt.day as u32,
            )
            .and_then(|d| d.and_hms_opt(b.dt.hour as u32, b.dt.minute as u32, 0))
            else {
                return false;
            };
            now.signed_duration_since(ts).num_seconds() >= MINUTE_BAR_SETTLE_SECS
        })
        .collect()
}

fn settled_scan_bars(bars: Vec<Bar>) -> Vec<Bar> {
    settled_scan_bars_at(bars, chrono::Local::now().naive_local())
}

fn event_dir(e: &pattern_events::Model) -> Dir {
    if e.direction == "down" {
        Dir::Down
    } else {
        Dir::Up
    }
}

fn trail_r(grade: usize) -> f64 {
    if grade == 1 {
        1.0
    } else {
        outcome::TRAIL_STEP_R * grade as f64
    }
}

fn trigger_k_adj(bars: &[Bar], dir: Dir, i: usize) -> f64 {
    let atr20 = crate::analyze::indicators::atr(bars, ATR_PERIOD);
    match crate::analyze::scoring::single_reversal_pattern(bars, &atr20, dir, i, i) {
        Some(kind) if matches!(kind.as_str(), "strong" | "engulf") => 0.5,
        Some(_) => 0.2,
        None => -0.3,
    }
}

fn append_hold_history(e: &mut pattern_events::Model, ts: &str, score: f64) {
    let mut history: Vec<serde_json::Value> =
        serde_json::from_str(&e.hold_score_history).unwrap_or_default();
    if history
        .last()
        .and_then(|v| v.get("ts"))
        .and_then(|v| v.as_str())
        == Some(ts)
    {
        return;
    }
    history.push(serde_json::json!({ "ts": ts, "score": score }));
    e.hold_score_history = serde_json::json!(history).to_string();
}

fn hold_score_for(
    e: &pattern_events::Model,
    bars: &[Bar],
    dir: Dir,
    tc: usize,
    mfe_r: f64,
    mae_r: f64,
    held: usize,
) -> f64 {
    let volume_adj = match e.trigger_volume_ratio {
        Some(v) if v >= outcome::VOL_CONFIRM_RATIO => 0.5,
        Some(v) if v < 0.8 => -0.3,
        _ => 0.0,
    };
    let chase_adj = match e.overshoot_r {
        Some(v) if v >= 0.6 => 0.5,
        Some(v) if v < 0.1 => -0.5,
        _ => 0.0,
    };
    let time_adj = if held >= outcome::NO_FOLLOW_BAR && mfe_r < outcome::NO_FOLLOW_MFE_R {
        -0.6
    } else if held >= 3 && mfe_r <= 0.0 {
        -0.3
    } else {
        0.0
    };
    let pnl_adj = if mfe_r >= 1.0 { 0.5 } else { 0.0 } + if mae_r <= -0.8 { -0.5 } else { 0.0 };
    let score =
        e.entry_score + trigger_k_adj(bars, dir, tc) + volume_adj + chase_adj + time_adj + pnl_adj;
    score.clamp(0.0, 5.0)
}

fn advance_triggered(e: &mut pattern_events::Model, bars: &[Bar], w: usize) -> bool {
    let mut changed = false;
    let dir = event_dir(e);
    let risk = e.risk;
    if risk <= 0.0 || e.entry <= 0.0 {
        return false;
    }

    let crossed = |bar: &Bar| match dir {
        Dir::Up => bar.high >= e.entry,
        Dir::Down => bar.low <= e.entry,
    };
    let tc = if let Some(ts) = e.trigger_bar_ts.as_deref() {
        bars.iter().position(|b| bar_ts(b) == ts)
    } else {
        bars.iter()
            .enumerate()
            .skip(w + 1)
            .find(|(_, b)| crossed(b))
            .map(|(i, _)| i)
    };
    let Some(tc) = tc else {
        return false;
    };

    if e.trigger_bar_ts.is_none() {
        e.trigger_bar_ts = Some(bar_ts(&bars[tc]));
        if e.trigger_ts.is_none() {
            e.trigger_ts = Some(bar_ts(&bars[tc]));
        }
        e.trigger_price = Some(e.entry);
        changed = true;
    }

    if e.trigger_volume_ratio.is_none() {
        e.trigger_volume_ratio = outcome::vol_ratio_at(bars, tc);
        changed = true;
    }
    if e.overshoot_r.is_none() {
        let overshoot = match dir {
            Dir::Up => (bars[tc].high - e.entry) / risk,
            Dir::Down => (e.entry - bars[tc].low) / risk,
        };
        e.overshoot_r = Some(overshoot.max(0.0));
        changed = true;
    }
    if e.trigger_score.is_none() {
        let volume_adj = match e.trigger_volume_ratio {
            Some(v) if v >= outcome::VOL_CONFIRM_RATIO => 0.5,
            Some(v) if v < 0.8 => -0.3,
            _ => 0.0,
        };
        let chase_adj = match e.overshoot_r {
            Some(v) if v >= 0.6 => 0.5,
            Some(v) if v < 0.1 => -0.5,
            _ => 0.0,
        };
        e.trigger_score = Some(
            (e.entry_score + trigger_k_adj(bars, dir, tc) + volume_adj + chase_adj).clamp(0.0, 5.0),
        );
        changed = true;
    }

    let mut fill = e.trigger_price.unwrap_or(e.entry);
    if tc > 0 && !bars[tc - 1].rollover {
        let prev_close = bars[tc - 1].close;
        let cur_open = bars[tc].open;
        let gap = match dir {
            Dir::Up => prev_close < e.entry && cur_open > e.entry,
            Dir::Down => prev_close > e.entry && cur_open < e.entry,
        };
        if gap {
            fill = cur_open;
        }
    }
    if e.trigger_price != Some(fill) {
        e.trigger_price = Some(fill);
        changed = true;
    }

    let base_tp = match dir {
        Dir::Up => fill + risk,
        Dir::Down => fill - risk,
    };
    let mut mfe = e.mfe_r.unwrap_or(0.0);
    let mut mae = e.mae_r.unwrap_or(0.0);
    let mut trail_grade: Option<usize> = None;
    let mut exit: Option<(String, f64, String, f64, usize)> = None;
    let mut last_idx = tc;

    for i in tc..bars.len() {
        let bar = &bars[i];
        if bar.rollover {
            exit = Some((
                "rollover".to_string(),
                bar.open,
                bar_ts(bar),
                0.0,
                i - tc + 1,
            ));
            last_idx = i;
            break;
        }
        let held = i - tc + 1;
        let mfe_contrib = match dir {
            Dir::Up => (bar.high - fill) / risk,
            Dir::Down => (fill - bar.low) / risk,
        };
        let mae_contrib = match dir {
            Dir::Up => (bar.low - fill) / risk,
            Dir::Down => (fill - bar.high) / risk,
        };
        mfe = mfe.max(mfe_contrib);
        mae = mae.min(mae_contrib);

        let stop_hit = match dir {
            Dir::Up => bar.low <= e.stop,
            Dir::Down => bar.high >= e.stop,
        };
        if stop_hit {
            let stop_gap = i > 0
                && !bars[i - 1].rollover
                && match dir {
                    Dir::Up => bars[i - 1].close > e.stop && bar.open < e.stop,
                    Dir::Down => bars[i - 1].close < e.stop && bar.open > e.stop,
                };
            let exit_price = if stop_gap { bar.open } else { e.stop };
            let r = match dir {
                Dir::Up => (exit_price - fill) / risk,
                Dir::Down => (fill - exit_price) / risk,
            };
            exit = Some(("stop".to_string(), exit_price, bar_ts(bar), r, held));
            last_idx = i;
            break;
        }

        let reached_tp1 = match dir {
            Dir::Up => bar.high >= base_tp,
            Dir::Down => bar.low <= base_tp,
        };
        if trail_grade.is_none() && reached_tp1 {
            trail_grade = Some(1);
        }
        if let Some(mut grade) = trail_grade {
            loop {
                let next_grade = grade + 1;
                let next_r = trail_r(next_grade);
                let next_price = match dir {
                    Dir::Up => fill + next_r * risk,
                    Dir::Down => fill - next_r * risk,
                };
                let next_hit = match dir {
                    Dir::Up => bar.high >= next_price,
                    Dir::Down => bar.low <= next_price,
                };
                if !next_hit {
                    break;
                }
                grade = next_grade;
            }
            trail_grade = Some(grade);
            let trail_price = match dir {
                Dir::Up => fill + trail_r(grade) * risk,
                Dir::Down => fill - trail_r(grade) * risk,
            };
            let fell_back = match dir {
                Dir::Up => bar.low <= trail_price,
                Dir::Down => bar.high >= trail_price,
            };
            if fell_back {
                let r = match dir {
                    Dir::Up => (trail_price - fill) / risk,
                    Dir::Down => (fill - trail_price) / risk,
                };
                exit = Some(("target".to_string(), trail_price, bar_ts(bar), r, held));
                last_idx = i;
                break;
            }
        }

        if i == tc + outcome::NO_FOLLOW_BAR && mfe < outcome::NO_FOLLOW_MFE_R {
            let r = match dir {
                Dir::Up => (bar.close - fill) / risk,
                Dir::Down => (fill - bar.close) / risk,
            };
            exit = Some(("no_follow".to_string(), bar.close, bar_ts(bar), r, held));
            last_idx = i;
            break;
        }
        if held >= outcome::TIME_HORIZON_BARS {
            let r = match dir {
                Dir::Up => (bar.close - fill) / risk,
                Dir::Down => (fill - bar.close) / risk,
            };
            exit = Some(("time_exit".to_string(), bar.close, bar_ts(bar), r, held));
            last_idx = i;
            break;
        }
        last_idx = i;
    }

    let current_ts = bar_ts(&bars[last_idx]);
    if let Some((reason, price, ts, r, held)) = exit {
        let outcome_str = if reason == "rollover" {
            "rollover"
        } else if r > 0.0 {
            "win"
        } else {
            "loss"
        };
        e.state = "closed".to_string();
        e.outcome = Some(outcome_str.to_string());
        e.exit_reason = Some(reason);
        e.exit_ts = Some(ts.clone());
        e.exit_price = Some(price);
        e.r_multiple = if outcome_str == "rollover" {
            None
        } else {
            Some(r)
        };
        e.mfe_r = Some(mfe);
        e.mae_r = Some(mae);
        e.last_advance_ts = Some(ts);
        e.hold_score = Some(hold_score_for(e, bars, dir, tc, mfe, mae, held));
        append_hold_history(e, &current_ts, e.hold_score.unwrap_or(0.0));
        return true;
    }

    if e.mfe_r != Some(mfe) || e.mae_r != Some(mae) {
        e.mfe_r = Some(mfe);
        e.mae_r = Some(mae);
        changed = true;
    }
    let held = last_idx - tc + 1;
    let score = hold_score_for(e, bars, dir, tc, mfe, mae, held);
    if e.hold_score != Some(score) {
        e.hold_score = Some(score);
        changed = true;
    }
    if e.last_advance_ts.as_deref() != Some(current_ts.as_str()) {
        e.last_advance_ts = Some(current_ts.clone());
        changed = true;
    }
    append_hold_history(e, &current_ts, score);
    changed
}

fn advance_event_model(e: &mut pattern_events::Model, bars: &[Bar]) -> bool {
    let Some(w) = bars.iter().position(|b| bar_ts(b) == e.warning_ts) else {
        return false;
    };
    let mut changed = false;

    if e.state == "pending" {
        let scan_end = (w + 1 + outcome::PENDING_BARS).min(bars.len());
        let mut triggered = false;
        for j in w + 1..scan_end {
            if bars[j].rollover {
                e.state = "expired".to_string();
                e.outcome = Some("rollover".to_string());
                e.exit_reason = Some("rollover".to_string());
                e.exit_ts = Some(bar_ts(&bars[j]));
                e.last_advance_ts = Some(bar_ts(&bars[j]));
                changed = true;
                break;
            }
            let hit = match event_dir(e) {
                Dir::Up => bars[j].high >= e.entry,
                Dir::Down => bars[j].low <= e.entry,
            };
            if hit {
                e.state = "triggered".to_string();
                triggered = true;
                changed = true;
                break;
            }
            let stop_hit = match event_dir(e) {
                Dir::Up => bars[j].low <= e.stop,
                Dir::Down => bars[j].high >= e.stop,
            };
            if stop_hit {
                e.state = "expired".to_string();
                e.outcome = Some("no_trigger".to_string());
                e.exit_reason = Some("no_trigger".to_string());
                e.exit_ts = Some(bar_ts(&bars[j]));
                e.last_advance_ts = Some(bar_ts(&bars[j]));
                changed = true;
                break;
            }
        }
        if !changed && bars.len() > w + outcome::PENDING_BARS {
            e.state = "expired".to_string();
            e.outcome = Some("no_trigger".to_string());
            e.exit_reason = Some("no_trigger".to_string());
            e.last_advance_ts = Some(bar_ts(bars.last().expect("bars 非空")));
            changed = true;
        }
        if triggered && e.state == "triggered" {
            changed = advance_triggered(e, bars, w) || changed;
        }
    } else if e.state == "triggered" && advance_triggered(e, bars, w) {
        changed = true;
    }

    if changed {
        e.updated_at = now_ts();
    }
    changed
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

/// 60m 长期趋势线的一个数据点：MA20 值及其多空方向。
#[derive(Debug, Clone, Serialize)]
pub struct TrendPointDto {
    pub ts: String,
    pub value: f64,
    /// up = 线上且线上移；down = 线下且线下移；neutral = 其余震荡
    pub direction: String,
}

/// 入场价触发命中：最新价已触及某形态入场点（做空=跌破，做多=突破）。
#[derive(Debug, Clone, serde::Serialize)]
pub struct EntryTriggerHit {
    pub event_id: i64,
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
    /// 月合约 5m K 线缓存：同一品种换月确认短时间不重复抓取
    month_kline_cache: RwLock<HashMap<String, (Instant, usize, Vec<Kline>)>>,
    /// 串行化扫描：手动扫描与定时扫描不会同时跑，避免同一预警K线重复入库。
    scan_lock: Mutex<()>,
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
            month_kline_cache: RwLock::new(HashMap::new()),
            scan_lock: Mutex::new(()),
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
        let min_score = cfg.notify.new_pattern_min_score;
        let rows = repo::all_pattern_events(&self.db).await?;
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
            if row.state != "pending" || row.trigger_ts.is_some() || row.entry_score < min_score {
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
                let now = now_ts();
                let mut next = row.clone();
                next.state = "triggered".to_string();
                next.trigger_ts = Some(now.clone());
                next.trigger_price = Some(row.entry);
                next.updated_at = now;
                if let Err(e) = repo::update_pattern_event(&self.db, next).await {
                    tracing::warn!("记录实时触发失败 {} {}: {e}", row.symbol, row.id);
                }
                hits.push(EntryTriggerHit {
                    event_id: row.id,
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
        Ok(())
    }

    /// 低频换月扫描入口：只处理有新增断点或仍待确认的品种，
    /// 首次会按本地连续 5m 全量回扫，之后按进度增量扫描。
    pub async fn sync_rollovers_if_needed(&self) -> Result<()> {
        let symbols = repo::list_symbols(&self.db, true).await?;
        let codes: Vec<String> = symbols.into_iter().map(|s| s.code).collect();
        self.sync_rollovers_for_symbols(&codes).await
    }

    /// 只扫描指定品种（测试/运维用）。
    pub async fn sync_rollovers_for_symbols(&self, symbols: &[String]) -> Result<()> {
        for code in symbols {
            if let Err(e) = self.sync_symbol_rollovers_if_needed(code).await {
                tracing::warn!("{} 换月扫描失败: {e}", code);
            }
        }
        Ok(())
    }

    async fn sync_symbol_rollovers_if_needed(&self, symbol: &str) -> Result<()> {
        let raw = repo::raw_klines(&self.db, symbol).await?;
        if raw.len() < 3 {
            return Ok(());
        }
        let bars: Vec<Kline> = raw.iter().map(model_to_fetch).collect();
        let candidates = rollover::detect_candidates(symbol, &bars);
        if candidates.is_empty() {
            return Ok(());
        }

        let existing = repo::symbol_rollovers(&self.db, symbol).await?;
        let confirmed_ts: HashSet<String> = existing
            .iter()
            .filter(|r| r.confirmed)
            .map(|r| r.ts.clone())
            .collect();
        let pending_ts: HashSet<String> = existing
            .iter()
            .filter(|r| !r.confirmed)
            .map(|r| r.ts.clone())
            .collect();
        let mut confirmed_pairs: HashSet<(String, String)> = existing
            .iter()
            .filter(|r| r.confirmed && !r.from_contract.is_empty() && !r.to_contract.is_empty())
            .map(|r| (r.from_contract.clone(), r.to_contract.clone()))
            .collect();
        let progress_key = format!("{ROLLOVER_SCAN_SETTING_PREFIX}{symbol}");
        let progress = repo::get_setting(&self.db, &progress_key).await?;
        let pending: Vec<rollover::RolloverCandidate> = candidates
            .into_iter()
            .filter(|c| !confirmed_ts.contains(&c.ts))
            .filter(|c| {
                pending_ts.contains(&c.ts)
                    || progress.as_deref().map_or(true, |p| c.ts.as_str() > p)
            })
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

        // 一次按最早断点估足抓取深度，避免对同一合约重复请求。
        let count = pending
            .iter()
            .map(|c| bars_needed_for(&c.before.datetime))
            .max()
            .unwrap_or(300)
            .max(300);
        let mut month_bars: HashMap<String, Vec<Kline>> = HashMap::new();
        for code in &month_codes {
            match self.month_kline_cached(code, count).await {
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
        let mut stale: Vec<(String, String)> = Vec::new();
        for c in &pending {
            match rollover::confirm_candidate(c, &month_bars) {
                Ok(rollover::ConfirmResult::Confirmed(from, to)) => {
                    if confirmed_pairs.contains(&(from.clone(), to.clone())) {
                        tracing::debug!("{symbol} 已记录过 {from} -> {to}，忽略重复点 @ {}", c.ts);
                        continue;
                    }
                    confirmed_pairs.insert((from.clone(), to.clone()));
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
                Ok(rollover::ConfirmResult::NotRollover) => {
                    tracing::debug!("{symbol} 候选断点 {} 判定为普通断点", c.ts);
                    if pending_ts.contains(&c.ts) {
                        stale.push((symbol.to_string(), c.ts.clone()));
                    }
                }
                Ok(rollover::ConfirmResult::InsufficientData) => {
                    if candidate_within_retention(&c.ts) {
                        tracing::debug!("{symbol} 候选断点 {} 月合约数据不足，保留待确认", c.ts);
                        rows.push(rollover_row(symbol, &c.ts, None, None, false, &now));
                    } else {
                        tracing::debug!("{symbol} 候选断点 {} 数据不足且已超期，不再保留", c.ts);
                        if pending_ts.contains(&c.ts) {
                            stale.push((symbol.to_string(), c.ts.clone()));
                        }
                    }
                }
                Err(e) => {
                    tracing::warn!("{symbol} 候选断点 {} 确认失败: {e}", c.ts);
                    rows.push(rollover_row(symbol, &c.ts, None, None, false, &now));
                }
            }
        }
        for (sym, ts) in stale {
            repo::delete_symbol_rollover(&self.db, &sym, &ts).await?;
        }
        repo::upsert_rollovers(&self.db, rows).await?;

        let latest = pending.iter().map(|c| c.ts.as_str()).max().unwrap_or("");
        if !latest.is_empty() {
            let mut map = HashMap::new();
            map.insert(progress_key, latest.to_string());
            repo::set_settings(&self.db, &map).await?;
        }
        Ok(())
    }

    async fn month_kline_cached(&self, code: &str, count: usize) -> Result<Vec<Kline>> {
        let fresh = {
            let guard = self.month_kline_cache.read().await;
            guard
                .get(code)
                .filter(|(at, cached_count, _)| {
                    at.elapsed() < MONTH_KLINE_CACHE_TTL && *cached_count >= count
                })
                .map(|(_, _, rows)| rows.clone())
        };
        if let Some(rows) = fresh {
            return Ok(rows);
        }
        let rows =
            crate::fetch::kline::fetch_minute(&*self.client.read().await, code, "5", count).await?;
        self.month_kline_cache
            .write()
            .await
            .insert(code.to_string(), (Instant::now(), count, rows.clone()));
        Ok(rows)
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

    /// 当前周期长期趋势线：逐根 MA20 及方向，供图表叠加参考线使用。
    pub async fn trend_series(
        &self,
        symbol: &str,
        timeframe: &str,
        limit: Option<usize>,
    ) -> Result<Vec<TrendPointDto>> {
        let bars = self.bars_for(symbol, timeframe).await?;
        let ma = crate::analyze::indicators::ma_series(&bars, ATR_PERIOD);
        let mut out = Vec::with_capacity(bars.len());
        for i in 0..bars.len() {
            let Some(value) = ma[i] else {
                continue;
            };
            let prev = if i == 0 {
                value
            } else {
                ma[i - 1].unwrap_or(value)
            };
            let close = bars[i].close;
            let direction = if close > value && value > prev {
                "up"
            } else if close < value && value < prev {
                "down"
            } else {
                "neutral"
            };
            out.push(TrendPointDto {
                ts: format!(
                    "{:04}-{:02}-{:02} {:02}:{:02}:00",
                    bars[i].dt.year,
                    bars[i].dt.month,
                    bars[i].dt.day,
                    bars[i].dt.hour,
                    bars[i].dt.minute
                ),
                value,
                direction: direction.to_string(),
            });
        }
        Ok(apply_limit_dto(out, limit))
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

    /// 全品种扫描：15m 前向重放识别预警，插入 pattern_events 并推进在途事件。
    pub async fn run_scan(&self) -> Result<ScanResult> {
        let _scan_guard = self.scan_lock.lock().await;
        let started = now_ts();
        self.sync_rollovers_if_needed().await?;
        repo::delete_fast_pattern_events(&self.db).await?;
        let cfg = self.config().await;
        let min_score = cfg.notify.new_pattern_min_score;
        let symbols = repo::list_symbols(&self.db, true).await?;
        let mut failed: Vec<SymbolFailure> = Vec::new();
        let mut new_warnings: Vec<pattern_events::Model> = Vec::new();
        let mut newly_triggered: Vec<pattern_events::Model> = Vec::new();
        let mut signals: Vec<pattern_events::Model> = Vec::new();
        let mut scanned = 0i64;

        for sym in symbols {
            let bars15 = settled_scan_bars(self.bars_for(&sym.code, "15m").await?);
            if bars15.len() < ATR_PERIOD + 2 {
                failed.push(SymbolFailure {
                    symbol: sym.code,
                    reason: "K线数据不足".to_string(),
                });
                continue;
            }
            scanned += 1;
            let tick = crate::precision::effective_tick(sym.tick_size, &sym.code, &sym.variety);
            let candidates = event::replay_warnings(&sym.code, &bars15, tick);
            let mut events = repo::pattern_events_by_symbol(&self.db, &sym.code, None).await?;
            for c in candidates {
                let direction = if c.direction == Dir::Up { "up" } else { "down" };
                let warning_ts = bar_ts(&bars15[c.warning_index]);
                if repo::pattern_event_by_warning(&self.db, &sym.code, direction, &warning_ts)
                    .await?
                    .is_some()
                    || has_similar_warning(&bars15, &c, &events)?
                {
                    continue;
                }
                let model = insert_warning_event(&self.db, &sym.code, &bars15, &c).await?;
                events.push(model.clone());
                new_warnings.push(model);
            }

            let events = repo::pattern_events_by_symbol(&self.db, &sym.code, None).await?;
            for mut e in events {
                let was_triggered = e.trigger_ts.is_some();
                let changed = advance_event_model(&mut e, &bars15);
                if changed {
                    repo::update_pattern_event(&self.db, e.clone()).await?;
                }
                if !was_triggered && e.trigger_ts.is_some() {
                    newly_triggered.push(e.clone());
                }
                signals.push(e);
            }
        }

        self.cleanup_duplicate_events().await?;
        signals = repo::all_pattern_events(&self.db).await?;

        let finished = now_ts();
        let active_count = signals
            .iter()
            .filter(|e| matches!(e.state.as_str(), "pending" | "triggered"))
            .count() as i64;
        let summary = build_scan_summary(
            &started,
            &finished,
            scanned,
            active_count,
            min_score,
            &signals,
            &failed,
        );
        Ok(ScanResult {
            scanned,
            active_count,
            summary,
            signals,
            new_warnings,
            newly_triggered,
            failed,
        })
    }

    /// 清理历史遗留的重复事件：与复盘统计去重同口径，族内只保留首见一条，
    /// 其余物理删除，保证K线右侧与复盘明细不会再出现被去重信号。
    async fn cleanup_duplicate_events(&self) -> Result<usize> {
        let events = repo::all_pattern_events(&self.db).await?;
        if events.is_empty() {
            return Ok(0);
        }
        let mut symbols_with_events: Vec<&str> = Vec::new();
        for e in &events {
            if !symbols_with_events.contains(&e.symbol.as_str()) {
                symbols_with_events.push(e.symbol.as_str());
            }
        }
        let mut warning_bar_index: outcome::WarningBarIndex = HashMap::new();
        for symbol in symbols_with_events {
            let bars = self.bars_for(symbol, "15m").await?;
            for (idx, bar) in bars.iter().enumerate() {
                warning_bar_index.insert((symbol.to_string(), bar_ts(bar)), idx);
            }
        }
        let ids = duplicate_event_ids(&events, &warning_bar_index);
        let mut deleted = 0usize;
        for id in ids {
            repo::delete_pattern_event(&self.db, id).await?;
            deleted += 1;
        }
        Ok(deleted)
    }

    /// 清空旧事件后按当前全部 15m K 线重建：前向识别预警并插入，
    /// 再把每个事件推进到现在的真实状态（触发/失效/已了结）。
    pub async fn rebuild_events(&self) -> Result<usize> {
        let _scan_guard = self.scan_lock.lock().await;
        let old_events = repo::all_pattern_events(&self.db).await?;
        let old_keys: HashMap<i64, (String, String, String, String, String, String)> = old_events
            .iter()
            .map(|e| {
                (
                    e.id,
                    (
                        e.symbol.clone(),
                        e.direction.clone(),
                        e.s0_ts.clone(),
                        e.s1_ts.clone(),
                        e.s2_ts.clone(),
                        e.warning_ts.clone(),
                    ),
                )
            })
            .collect();
        let annotations = repo::all_signal_annotations(&self.db).await?;
        let decisions = repo::all_signal_decisions(&self.db).await?;
        repo::clear_pattern_events(&self.db).await?;
        repo::clear_signal_user_data(&self.db).await?;
        self.entry_notified.write().await.clear();
        let symbols = repo::list_symbols(&self.db, true).await?;
        let mut inserted = 0usize;
        for sym in symbols {
            let bars15 = settled_scan_bars(self.bars_for(&sym.code, "15m").await?);
            if bars15.len() < ATR_PERIOD + 2 {
                continue;
            }
            let tick = crate::precision::effective_tick(sym.tick_size, &sym.code, &sym.variety);
            let candidates = event::replay_warnings(&sym.code, &bars15, tick);
            let mut events = repo::pattern_events_by_symbol(&self.db, &sym.code, None).await?;
            for c in candidates {
                let direction = if c.direction == Dir::Up { "up" } else { "down" };
                let warning_ts = bar_ts(&bars15[c.warning_index]);
                if repo::pattern_event_by_warning(&self.db, &sym.code, direction, &warning_ts)
                    .await?
                    .is_some()
                    || has_similar_warning(&bars15, &c, &events)?
                {
                    continue;
                }
                let model = insert_warning_event(&self.db, &sym.code, &bars15, &c).await?;
                events.push(model);
                inserted += 1;
            }

            let events = repo::pattern_events_by_symbol(&self.db, &sym.code, None).await?;
            for mut e in events {
                if advance_event_model(&mut e, &bars15) {
                    repo::update_pattern_event(&self.db, e).await?;
                }
            }
        }
        self.cleanup_duplicate_events().await?;
        let new_events = repo::all_pattern_events(&self.db).await?;
        let mut new_keys_by_structure: HashMap<(String, String, String, String, String), i64> =
            HashMap::new();
        let mut new_keys_by_warning: HashMap<(String, String, String), i64> = HashMap::new();
        for e in &new_events {
            new_keys_by_structure
                .entry((
                    e.symbol.clone(),
                    e.direction.clone(),
                    e.s0_ts.clone(),
                    e.s1_ts.clone(),
                    e.s2_ts.clone(),
                ))
                .or_insert(e.id);
            new_keys_by_warning
                .entry((e.symbol.clone(), e.direction.clone(), e.warning_ts.clone()))
                .or_insert(e.id);
        }
        for ann in annotations {
            let Some((symbol, direction, s0_ts, s1_ts, s2_ts, warning_ts)) =
                old_keys.get(&ann.event_id)
            else {
                continue;
            };
            let structure_key = (
                symbol.clone(),
                direction.clone(),
                s0_ts.clone(),
                s1_ts.clone(),
                s2_ts.clone(),
            );
            let new_id = new_keys_by_structure
                .get(&structure_key)
                .copied()
                .or_else(|| {
                    new_keys_by_warning
                        .get(&(symbol.clone(), direction.clone(), warning_ts.clone()))
                        .copied()
                });
            let Some(new_id) = new_id else {
                continue;
            };
            repo::insert_signal_annotation_with_ts(&self.db, new_id, &ann.content, &ann.created_at)
                .await?;
        }
        for decision in decisions {
            let Some((symbol, direction, s0_ts, s1_ts, s2_ts, warning_ts)) =
                old_keys.get(&decision.event_id)
            else {
                continue;
            };
            let structure_key = (
                symbol.clone(),
                direction.clone(),
                s0_ts.clone(),
                s1_ts.clone(),
                s2_ts.clone(),
            );
            let new_id = new_keys_by_structure
                .get(&structure_key)
                .copied()
                .or_else(|| {
                    new_keys_by_warning
                        .get(&(symbol.clone(), direction.clone(), warning_ts.clone()))
                        .copied()
                });
            let Some(new_id) = new_id else {
                continue;
            };
            repo::insert_signal_decision_with_ts(
                &self.db,
                new_id,
                decision.opened,
                &decision.updated_at,
            )
            .await?;
        }
        Ok(inserted)
    }

    /// 复盘页“刷新”：只推进在途事件，不重新识别新预警。
    pub async fn refresh_outcomes(&self) -> Result<OutcomeRefresh> {
        let _scan_guard = self.scan_lock.lock().await;
        repo::delete_fast_pattern_events(&self.db).await?;
        let events = repo::all_pattern_events(&self.db).await?;
        let mut by_symbol: HashMap<String, Vec<pattern_events::Model>> = HashMap::new();
        for e in events {
            if matches!(e.state.as_str(), "pending" | "triggered") {
                by_symbol.entry(e.symbol.clone()).or_default().push(e);
            }
        }
        let mut updated = 0usize;
        for (symbol, list) in by_symbol {
            let bars15 = settled_scan_bars(self.bars_for(&symbol, "15m").await?);
            for mut e in list {
                if advance_event_model(&mut e, &bars15) {
                    repo::update_pattern_event(&self.db, e).await?;
                    updated += 1;
                }
            }
        }
        Ok(OutcomeRefresh { updated })
    }

    /// 复盘统计：直接汇总 pattern_events，不再回放补标。
    pub async fn review_stats(
        &self,
        dimension: &str,
        scope: &str,
        version: Option<&str>,
        score_min: Option<f64>,
        score_max: Option<f64>,
    ) -> Result<outcome::ReviewStats> {
        let events = repo::all_pattern_events(&self.db).await?;
        let symbols = repo::list_symbols(&self.db, false).await?;
        let tick_by_symbol: HashMap<String, f64> = symbols
            .iter()
            .map(|sym| {
                (
                    sym.code.clone(),
                    crate::precision::effective_tick(sym.tick_size, &sym.code, &sym.variety),
                )
            })
            .collect();
        let rows: Vec<outcome::StatRow> = events
            .iter()
            .filter_map(|e| {
                if let Some(v) = version {
                    if EVENT_LOGIC_VERSION != v {
                        return None;
                    }
                }
                stat_row_from(e, tick_by_symbol.get(&e.symbol).copied())
            })
            .filter(|r| score_in_range(r.score, score_min, score_max))
            .collect();
        let symbols_with_events: HashSet<&str> = rows.iter().map(|r| r.symbol.as_str()).collect();
        let mut warning_bar_index: outcome::WarningBarIndex = HashMap::new();
        for sym in symbols
            .iter()
            .filter(|s| symbols_with_events.contains(s.code.as_str()))
        {
            let bars = self.bars_for(&sym.code, "15m").await?;
            for (idx, bar) in bars.iter().enumerate() {
                warning_bar_index.insert((sym.code.clone(), bar_ts(bar)), idx);
            }
        }
        Ok(outcome::aggregate_stats_scoped_with_bar_index(
            &rows,
            outcome::GroupBy::parse(dimension),
            outcome::StatsScope::parse(scope),
            &warning_bar_index,
        ))
    }

    /// 最近信号明细（复盘页明细表）：真实事件，按预警时间倒序（同时间按 event_id 倒序）。
    pub async fn recent_outcomes(
        &self,
        limit: usize,
        filter: &OutcomeFilter,
    ) -> Result<Vec<OutcomeDetail>> {
        let events = repo::all_pattern_events(&self.db).await?;
        let symbols = repo::list_symbols(&self.db, false).await?;
        let tick_by_symbol: HashMap<String, f64> = symbols
            .iter()
            .map(|sym| {
                (
                    sym.code.clone(),
                    crate::precision::effective_tick(sym.tick_size, &sym.code, &sym.variety),
                )
            })
            .collect();
        let mut rows: Vec<OutcomeDetail> = events
            .iter()
            .filter(|e| matches_outcome_filter(e, filter))
            .filter_map(|e| {
                Some(outcome_detail_from(
                    e,
                    tick_by_symbol.get(&e.symbol).copied(),
                ))
            })
            .collect();
        rows.sort_by(|a, b| b.warning_ts.cmp(&a.warning_ts).then_with(|| b.event_id.cmp(&a.event_id)));
        rows.truncate(limit);
        let mut annotations_by_event: HashMap<i64, Vec<SignalAnnotationDto>> = HashMap::new();
        for ann in repo::all_signal_annotations(&self.db).await? {
            annotations_by_event
                .entry(ann.event_id)
                .or_default()
                .push(SignalAnnotationDto {
                    id: ann.id,
                    event_id: ann.event_id,
                    content: ann.content,
                    created_at: ann.created_at,
                });
        }
        let opened_by_event: HashMap<i64, bool> = repo::all_signal_decisions(&self.db)
            .await?
            .into_iter()
            .map(|d| (d.event_id, d.opened))
            .collect();
        for row in rows.iter_mut() {
            row.annotations = annotations_by_event
                .remove(&row.event_id)
                .unwrap_or_default();
            row.opened = opened_by_event.get(&row.event_id).copied();
        }
        let symbols_with_rows: HashSet<String> = rows.iter().map(|r| r.symbol.clone()).collect();
        for code in symbols_with_rows {
            let bars = self.bars_for(&code, "15m").await?;
            let atr20 = crate::analyze::indicators::atr(&bars, ATR_PERIOD);
            let index_by_ts: HashMap<String, usize> = bars
                .iter()
                .enumerate()
                .map(|(i, b)| (bar_ts(b), i))
                .collect();
            for row in rows.iter_mut().filter(|r| r.symbol == code) {
                fill_leg_detail(row, &bars, &atr20, &index_by_ts);
            }
        }
        Ok(rows)
    }

    /// 复盘跳转K线图：按 event_id 返回完整事件 + 真实结局。
    pub async fn review_signal(&self, event_id: i64) -> Result<Option<ReviewSignalDetail>> {
        let Some(row) = repo::pattern_event_by_id(&self.db, event_id).await? else {
            return Ok(None);
        };
        let symbols = repo::list_symbols(&self.db, false).await?;
        let tick = symbols.iter().find_map(|sym| {
            (sym.code == row.symbol)
                .then(|| crate::precision::effective_tick(sym.tick_size, &sym.code, &sym.variety))
        });
        let mut outcome = outcome_detail_from(&row, tick);
        let bars = self.bars_for(&row.symbol, "15m").await?;
        let atr20 = crate::analyze::indicators::atr(&bars, ATR_PERIOD);
        let index_by_ts: HashMap<String, usize> = bars
            .iter()
            .enumerate()
            .map(|(i, b)| (bar_ts(b), i))
            .collect();
        fill_leg_detail(&mut outcome, &bars, &atr20, &index_by_ts);
        let outcome = Some(outcome);
        let annotations = repo::signal_annotations_for_event(&self.db, event_id)
            .await?
            .into_iter()
            .map(|a| SignalAnnotationDto {
                id: a.id,
                event_id: a.event_id,
                content: a.content,
                created_at: a.created_at,
            })
            .collect();
        let opened = repo::signal_decision(&self.db, event_id)
            .await?
            .map(|d| d.opened);
        Ok(Some(ReviewSignalDetail {
            event: row,
            outcome,
            annotations,
            opened,
        }))
    }

    /// K线右侧卡片：读取单个信号的批注与开仓记录。
    pub async fn signal_user_data(&self, event_id: i64) -> Result<SignalUserData> {
        let annotations = repo::signal_annotations_for_event(&self.db, event_id)
            .await?
            .into_iter()
            .map(|a| SignalAnnotationDto {
                id: a.id,
                event_id: a.event_id,
                content: a.content,
                created_at: a.created_at,
            })
            .collect();
        let opened = repo::signal_decision(&self.db, event_id)
            .await?
            .map(|d| d.opened);
        Ok(SignalUserData {
            annotations,
            opened,
        })
    }

    pub async fn add_signal_annotation(
        &self,
        event_id: i64,
        content: &str,
    ) -> Result<SignalAnnotationDto> {
        let content = content.trim();
        if content.is_empty() {
            return Err(anyhow!("批注内容不能为空"));
        }
        let row = repo::add_signal_annotation(&self.db, event_id, content).await?;
        Ok(SignalAnnotationDto {
            id: row.id,
            event_id: row.event_id,
            content: row.content,
            created_at: row.created_at,
        })
    }

    pub async fn delete_signal_annotation(&self, id: i64) -> Result<()> {
        repo::delete_signal_annotation(&self.db, id).await
    }

    pub async fn set_signal_decision(
        &self,
        event_id: i64,
        opened: bool,
    ) -> Result<SignalDecisionDto> {
        let row = repo::set_signal_decision(&self.db, event_id, opened).await?;
        Ok(SignalDecisionDto {
            event_id: row.event_id,
            opened: row.opened,
            updated_at: row.updated_at,
        })
    }
}

fn build_scan_summary(
    started: &str,
    finished: &str,
    scanned: i64,
    active_count: i64,
    min_score: f64,
    signals: &[pattern_events::Model],
    failed: &[SymbolFailure],
) -> String {
    let qualifying_count = signals
        .iter()
        .filter(|e| e.entry_score >= min_score)
        .count();
    let pending = signals
        .iter()
        .filter(|e| e.state == "pending")
        .collect::<Vec<_>>();
    let triggered = signals
        .iter()
        .filter(|e| e.state != "pending")
        .collect::<Vec<_>>();
    let mut out = String::new();
    out.push_str("=== 综合结论 ===\n");
    out.push_str(&format!("扫描时间: {started}\n"));
    out.push_str(&format!("完成时间: {finished}\n"));
    out.push_str(&format!(
        "共扫描 {scanned} 个品种，{active_count} 条关注信号，其中 {qualifying_count} 条达到通知评分阈值（{min_score} 分）\n",
        qualifying_count = qualifying_count,
        min_score = min_score
    ));
    if !pending.is_empty() {
        out.push_str("\n=== 预警信号（等待触发）===\n");
        for (i, e) in pending.iter().enumerate() {
            out.push_str(&format_scan_signal(i + 1, e, min_score));
        }
    }
    if !triggered.is_empty() {
        out.push_str("\n=== 已触发信号 ===\n");
        for (i, e) in triggered.iter().enumerate() {
            out.push_str(&format_scan_signal(pending.len() + i + 1, e, min_score));
        }
    }
    if !failed.is_empty() {
        let list = failed
            .iter()
            .map(|f| format!("{}: {}", f.symbol, f.reason))
            .collect::<Vec<_>>()
            .join("; ");
        out.push_str(&format!("以下品种分析失败: {list}\n"));
    }
    out
}

fn format_scan_signal(index: usize, e: &pattern_events::Model, min_score: f64) -> String {
    let dir = if e.direction == "up" {
        "做多"
    } else {
        "做空"
    };
    let level = match e.level.as_str() {
        "fine" => "精细",
        "large" => "较大",
        "box" => "箱体",
        other => other,
    };
    let pattern = if e.level == "box" {
        level.to_string()
    } else {
        format!("{level}N")
    };
    let threshold_flag = if e.entry_score >= min_score {
        "达标"
    } else {
        "未达阈值"
    };
    let state = match e.state.as_str() {
        "pending" => "等待触发",
        "triggered" => "已触发",
        "expired" => "已失效",
        "closed" => "已平仓",
        other => other,
    };
    let warning_kind = match e.warning_kind.as_str() {
        "strong" => "强反转",
        // 历史落盘记录兼容：旧 engulf 与合并后的 strong 显示同一标签。
        "engulf" => "强反转",
        "wick" => "长影线",
        // 历史记录兼容；新扫描不再产生 fast，旧记录也会被清理。
        "fast" => "快速反转",
        "cumulative" => "累积反转",
        other => other,
    };
    format!(
        "{index}. {symbol} {dir} {pattern} | {grade} | {state} | 形态: {warning_kind} | 评分 {score:.2} | {threshold_flag}\n   入场: {entry:.1} | 止损: {stop:.1} | 目标: {target:.1} | R/R: {rr:.2}\n",
        index = index,
        symbol = e.symbol,
        dir = dir,
        pattern = pattern,
        grade = e.grade,
        state = state,
        warning_kind = warning_kind,
        score = e.entry_score,
        entry = e.entry,
        stop = e.stop,
        target = e.target,
        rr = e.rr,
        threshold_flag = threshold_flag,
    )
}

fn apply_limit(rows: Vec<klines::Model>, limit: Option<usize>) -> Vec<klines::Model> {
    match limit {
        Some(limit) if rows.len() > limit => rows[rows.len() - limit..].to_vec(),
        _ => rows,
    }
}

fn apply_limit_dto<T>(rows: Vec<T>, limit: Option<usize>) -> Vec<T> {
    match limit {
        Some(limit) if rows.len() > limit => {
            let skip = rows.len() - limit;
            rows.into_iter().skip(skip).collect()
        }
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

fn ts_diff_bars(later: &str, earlier: &str) -> Option<usize> {
    ts_gap_minutes(later, earlier).map(|m| (m / 15).max(0) as usize)
}

/// 两条时间在同一 15m K线序列里的序号差；任一K线不在序列内时返回 None。
fn bar_gap(bars: &[Bar], later: &str, earlier: &str) -> Option<usize> {
    let li = bars.iter().position(|b| bar_ts(b) == later)?;
    let ei = bars.iter().position(|b| bar_ts(b) == earlier)?;
    Some(li.abs_diff(ei))
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

fn candidate_within_retention(ts: &str) -> bool {
    let fmt = "%Y-%m-%d %H:%M:%S";
    let Ok(dt) = chrono::NaiveDateTime::parse_from_str(ts, fmt) else {
        return false;
    };
    let now = chrono::Local::now().naive_local();
    (now - dt).num_days() <= ROLLOVER_PENDING_RETENTION_DAYS
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

    fn pattern_event(id: i64, score: f64, state: &str) -> pattern_events::Model {
        pattern_events::Model {
            id,
            symbol: "MA0".to_string(),
            direction: "up".to_string(),
            grade: "A".to_string(),
            level: "fine".to_string(),
            s0_ts: "2026-08-11 09:15:00".to_string(),
            s0_price: 2550.0,
            s1_ts: "2026-08-11 09:30:00".to_string(),
            s1_price: 2590.0,
            s2_ts: "2026-08-11 09:45:00".to_string(),
            s2_price: 2570.0,
            a_move: 40.0,
            b_move: 20.0,
            a_bars: 1,
            b_bars: 1,
            retracement: 0.5,
            warning_ts: "2026-08-11 09:45:00".to_string(),
            detected_at: "2026-08-11 09:45:00".to_string(),
            warning_kind: "wick".to_string(),
            entry_score: score,
            entry_score_dims: serde_json::json!({
                "dim_a": 3.8,
                "dim_b": 3.4,
                "dim_warning": 3.5,
            })
            .to_string(),
            entry: 2577.0,
            stop: 2564.0,
            target: 2584.0,
            risk: 13.0,
            rr: 0.54,
            state: state.to_string(),
            last_advance_ts: Some("2026-08-11 09:45:00".to_string()),
            trigger_ts: None,
            trigger_bar_ts: None,
            trigger_price: None,
            trigger_score: None,
            trigger_volume_ratio: None,
            overshoot_r: None,
            hold_score: None,
            hold_score_history: "[]".to_string(),
            outcome: None,
            exit_reason: None,
            exit_ts: None,
            exit_price: None,
            r_multiple: None,
            mfe_r: None,
            mae_r: None,
            created_at: "2026-08-11 09:45:00".to_string(),
            updated_at: "2026-08-11 09:45:00".to_string(),
        }
    }

    fn bar_model(ts: &str, o: f64, h: f64, l: f64, c: f64) -> Bar {
        let mut b = dt_to_bar(parse_dt(ts).unwrap());
        b.open = o;
        b.high = h;
        b.low = l;
        b.close = c;
        b
    }

    fn scan_result(scores: &[f64]) -> ScanResult {
        ScanResult {
            scanned: 0,
            active_count: scores.len() as i64,
            summary: String::new(),
            signals: scores
                .iter()
                .map(|&s| pattern_event(1, s, "pending"))
                .collect(),
            new_warnings: scores
                .iter()
                .map(|&s| pattern_event(1, s, "pending"))
                .collect(),
            newly_triggered: Vec::new(),
            failed: Vec::new(),
        }
    }

    #[test]
    fn pattern_endpoint_prices_use_swing_extremes_not_closes() {
        let bars = vec![
            bar_model("2026-08-12 22:15:00", 2222.0, 2223.0, 2221.0, 2222.0),
            bar_model("2026-08-13 14:15:00", 2232.0, 2233.0, 2229.0, 2230.0),
            bar_model("2026-08-14 09:30:00", 2228.0, 2230.0, 2227.0, 2230.0),
        ];
        let candidate = event::WarningCandidate {
            direction: Dir::Up,
            grade: "A级".to_string(),
            level: "large",
            s0_index: 0,
            s1_index: 1,
            s2_index: 2,
            a_move: 11.0,
            b_move: 6.0,
            a_bars: 15,
            b_bars: 13,
            retracement: 0.545,
            warning_index: 2,
            warning_kind: "strong",
            entry_score: 2.99,
            dim_a: 3.0,
            dim_b: 3.0,
            dim_warning: 2.5,
            entry: 2231.0,
            stop: 2226.0,
            target: 2233.0,
            risk: 5.0,
            rr: 0.4,
            trend_state: String::new(),
            trend_bonus: 0.0,
        };

        assert_eq!(
            pattern_endpoint_prices(&bars, &candidate),
            (2221.0, 2233.0, 2227.0)
        );

        let down = event::WarningCandidate {
            direction: Dir::Down,
            s0_index: 0,
            s1_index: 1,
            s2_index: 2,
            ..candidate
        };
        assert_eq!(
            pattern_endpoint_prices(&bars, &down),
            (2223.0, 2229.0, 2230.0)
        );
    }

    #[test]
    fn fill_leg_detail_calculates_q_net_move_and_gap() {
        let bars = vec![
            bar_model("2026-08-11 09:15:00", 100.0, 100.0, 100.0, 100.0),
            bar_model("2026-08-11 09:30:00", 110.0, 140.0, 110.0, 138.0),
            bar_model("2026-08-11 09:45:00", 130.0, 132.0, 128.0, 130.0),
        ];
        let atr20 = vec![Some(10.0); bars.len()];
        let index_by_ts: HashMap<String, usize> = bars
            .iter()
            .enumerate()
            .map(|(i, b)| (bar_ts(b), i))
            .collect();
        let mut e = pattern_event(1, 3.0, "pending");
        e.s0_ts = "2026-08-11 09:15:00".to_string();
        e.s0_price = 100.0;
        e.s1_ts = "2026-08-11 09:30:00".to_string();
        e.s1_price = 140.0;
        e.s2_ts = "2026-08-11 09:45:00".to_string();
        e.s2_price = 130.0;
        e.a_move = 40.0;
        e.a_bars = 2;
        e.b_move = 10.0;
        e.b_bars = 1;
        let mut row = outcome_detail_from(&e, None);
        fill_leg_detail(&mut row, &bars, &atr20, &index_by_ts);

        assert_eq!(row.a_net_move, Some(30.0));
        assert_eq!(row.a_gap_count, Some(1));
        assert!((row.a_gap_sum.unwrap() - 10.0).abs() < 1e-9);
        assert!(row.a_q.unwrap() > 0.0);
        assert!(row.a_atr.unwrap() > 0.0);
    }

    #[test]
    fn scan_result_email_gate_respects_min_score() {
        assert!(!scan_result(&[2.0, 2.4]).has_notifiable_signal(2.5));
        assert!(scan_result(&[2.0, 2.5]).has_notifiable_signal(2.5));
        assert!(scan_result(&[3.0]).has_notifiable_signal(2.5));
    }

    #[test]
    fn score_in_range_respects_closed_and_open_bounds() {
        assert!(score_in_range(2.8, Some(2.8), Some(3.6)));
        assert!(score_in_range(3.6, Some(2.8), Some(3.6)));
        assert!(!score_in_range(2.79, Some(2.8), Some(3.6)));
        assert!(!score_in_range(3.61, Some(2.8), Some(3.6)));
        assert!(score_in_range(3.5, None, Some(3.6)));
        assert!(score_in_range(3.6, Some(3.6), None));
        assert!(score_in_range(2.0, None, None));
    }

    #[test]
    fn scan_summary_lists_all_signals_with_threshold_flags() {
        let mut fine = pattern_event(1, 3.8, "pending");
        fine.direction = "down".to_string();
        let low = pattern_event(2, 2.3, "pending");
        let summary = build_scan_summary(
            "2026-08-14 09:30",
            "2026-08-14 09:35",
            23,
            2,
            2.5,
            &[fine, low],
            &[],
        );

        assert!(
            summary.contains("共扫描 23 个品种，2 条关注信号，其中 1 条达到通知评分阈值（2.5 分）")
        );
        assert!(summary.contains("=== 预警信号（等待触发）==="));
        assert!(summary.contains("MA0 做空 精细N | A | 等待触发 | 形态: 长影线 | 评分 3.80 | 达标"));
        assert!(
            summary.contains("MA0 做多 精细N | A | 等待触发 | 形态: 长影线 | 评分 2.30 | 未达阈值")
        );
        assert!(summary.contains("入场: 2577.0 | 止损: 2564.0 | 目标: 2584.0 | R/R: 0.54"));
    }

    #[test]
    fn pending_stop_breach_expires_without_trigger() {
        let mut e = pattern_event(1, 3.8, "pending");
        let bars = vec![
            bar_model("2026-08-11 09:45:00", 2572.0, 2576.0, 2566.0, 2570.0),
            bar_model("2026-08-11 10:00:00", 2568.0, 2570.0, 2563.0, 2564.0),
        ];

        assert!(advance_event_model(&mut e, &bars));
        assert_eq!(e.state, "expired");
        assert_eq!(e.outcome.as_deref(), Some("no_trigger"));
        assert_eq!(e.exit_reason.as_deref(), Some("no_trigger"));
        assert_eq!(e.exit_ts.as_deref(), Some("2026-08-11 10:00:00"));
    }

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
    fn settled_scan_bars_waits_for_settle_grace() {
        let bars = vec![
            bar_model("2026-08-19 14:00:00", 0.0, 0.0, 0.0, 0.0),
            bar_model("2026-08-19 14:15:00", 0.0, 0.0, 0.0, 0.0),
        ];
        let not_yet = chrono::NaiveDateTime::parse_from_str(
            "2026-08-19 14:15:29",
            "%Y-%m-%d %H:%M:%S",
        )
        .unwrap();
        let settled = chrono::NaiveDateTime::parse_from_str(
            "2026-08-19 14:15:30",
            "%Y-%m-%d %H:%M:%S",
        )
        .unwrap();

        let pending = settled_scan_bars_at(bars.clone(), not_yet);
        assert_eq!(pending.len(), 1);
        assert_eq!(pending[0].dt.minute, 0);

        let ready = settled_scan_bars_at(bars, settled);
        assert_eq!(ready.len(), 2);
    }

    #[test]
    fn bar_gap_counts_actual_bars_across_lunch_and_night() {
        let bars = vec![
            bar_model("2026-07-23 11:15:00", 0.0, 0.0, 0.0, 0.0),
            bar_model("2026-07-23 11:30:00", 0.0, 0.0, 0.0, 0.0),
            bar_model("2026-07-23 13:45:00", 0.0, 0.0, 0.0, 0.0),
            bar_model("2026-07-23 14:00:00", 0.0, 0.0, 0.0, 0.0),
            bar_model("2026-07-23 21:00:00", 0.0, 0.0, 0.0, 0.0),
            bar_model("2026-07-23 21:15:00", 0.0, 0.0, 0.0, 0.0),
            bar_model("2026-07-24 09:00:00", 0.0, 0.0, 0.0, 0.0),
            bar_model("2026-07-24 09:15:00", 0.0, 0.0, 0.0, 0.0),
        ];
        assert_eq!(
            bar_gap(&bars, "2026-07-23 14:00:00", "2026-07-23 11:15:00"),
            Some(3)
        );
        assert_eq!(
            bar_gap(&bars, "2026-07-24 09:15:00", "2026-07-23 21:00:00"),
            Some(3)
        );
        assert_eq!(
            bar_gap(&bars, "2026-07-24 09:15:00", "2026-07-23 11:15:00"),
            Some(7)
        );
    }

    #[test]
    fn similar_warning_suppresses_existing_events_in_any_state() {
        let bars = vec![
            bar_model("2026-08-14 22:30:00", 14160.0, 14180.0, 14150.0, 14170.0),
            bar_model("2026-08-14 22:45:00", 14165.0, 14185.0, 14155.0, 14175.0),
        ];
        let candidate = event::WarningCandidate {
            direction: Dir::Up,
            grade: "A级".to_string(),
            level: "fine",
            s0_index: 0,
            s1_index: 0,
            s2_index: 1,
            a_move: 20.0,
            b_move: 10.0,
            a_bars: 1,
            b_bars: 1,
            retracement: 0.5,
            warning_index: 1,
            warning_kind: "strong",
            entry_score: 3.8,
            dim_a: 3.0,
            dim_b: 3.0,
            dim_warning: 3.5,
            entry: 14175.0,
            stop: 14140.0,
            target: 14210.0,
            risk: 35.0,
            rr: 1.0,
            trend_state: String::new(),
            trend_bonus: 0.0,
        };
        let mut existing = pattern_event(397, 3.8, "triggered");
        existing.warning_ts = "2026-08-14 22:30:00".to_string();
        existing.entry = 14170.0;
        existing.risk = 38.675;
        assert!(has_similar_warning(&bars, &candidate, &[existing]).unwrap());
    }

    #[test]
    fn similar_warning_suppresses_active_event_even_when_entry_far() {
        let bars = vec![
            bar_model("2026-08-14 22:30:00", 14160.0, 14180.0, 14150.0, 14170.0),
            bar_model("2026-08-14 22:45:00", 14165.0, 14185.0, 14155.0, 14175.0),
        ];
        let candidate = event::WarningCandidate {
            direction: Dir::Up,
            grade: "A级".to_string(),
            level: "fine",
            s0_index: 0,
            s1_index: 0,
            s2_index: 1,
            a_move: 20.0,
            b_move: 10.0,
            a_bars: 1,
            b_bars: 1,
            retracement: 0.5,
            warning_index: 1,
            warning_kind: "strong",
            entry_score: 3.8,
            dim_a: 3.0,
            dim_b: 3.0,
            dim_warning: 3.5,
            entry: 15000.0,
            stop: 14950.0,
            target: 15100.0,
            risk: 50.0,
            rr: 1.0,
            trend_state: String::new(),
            trend_bonus: 0.0,
        };
        let mut existing = pattern_event(397, 3.8, "triggered");
        existing.warning_ts = "2026-08-14 22:30:00".to_string();
        existing.entry = 14170.0;
        existing.risk = 38.675;
        existing.trigger_ts = Some("2026-08-14 22:30:00".to_string());
        assert!(has_similar_warning(&bars, &candidate, &[existing]).unwrap());
    }

    #[test]
    fn similar_warning_requires_entry_closeness_after_exit() {
        let bars = vec![
            bar_model("2026-08-14 22:30:00", 14160.0, 14180.0, 14150.0, 14170.0),
            bar_model("2026-08-14 22:45:00", 14165.0, 14185.0, 14155.0, 14175.0),
            bar_model("2026-08-14 23:00:00", 14170.0, 14190.0, 14160.0, 14180.0),
        ];
        let candidate = event::WarningCandidate {
            direction: Dir::Up,
            grade: "A级".to_string(),
            level: "fine",
            s0_index: 0,
            s1_index: 0,
            s2_index: 2,
            a_move: 20.0,
            b_move: 10.0,
            a_bars: 1,
            b_bars: 1,
            retracement: 0.5,
            warning_index: 2,
            warning_kind: "strong",
            entry_score: 3.8,
            dim_a: 3.0,
            dim_b: 3.0,
            dim_warning: 3.5,
            entry: 15000.0,
            stop: 14950.0,
            target: 15100.0,
            risk: 50.0,
            rr: 1.0,
            trend_state: String::new(),
            trend_bonus: 0.0,
        };
        let mut existing = pattern_event(397, 3.8, "closed");
        existing.warning_ts = "2026-08-14 22:30:00".to_string();
        existing.entry = 14170.0;
        existing.risk = 38.675;
        existing.trigger_ts = Some("2026-08-14 22:30:00".to_string());
        existing.exit_ts = Some("2026-08-14 22:45:00".to_string());
        assert!(!has_similar_warning(&bars, &candidate, &[existing]).unwrap());
    }

    #[test]
    fn duplicate_event_ids_keeps_active_family_even_when_entry_far() {
        let mut first = pattern_event(1179, 3.8, "closed");
        first.symbol = "SA0".to_string();
        first.direction = "up".to_string();
        first.warning_ts = "2026-07-23 09:45:00".to_string();
        first.entry = 1015.0;
        first.risk = 6.0;
        first.trigger_ts = Some("2026-07-23 10:00:00".to_string());
        first.exit_ts = Some("2026-07-23 13:45:00".to_string());

        let mut second = pattern_event(1180, 3.6, "closed");
        second.symbol = "SA0".to_string();
        second.direction = "up".to_string();
        second.warning_ts = "2026-07-23 10:00:00".to_string();
        second.entry = 1017.0;
        second.risk = 7.0;
        second.trigger_ts = Some("2026-07-23 10:15:00".to_string());
        second.exit_ts = Some("2026-07-23 13:45:00".to_string());

        let mut bar_index: outcome::WarningBarIndex = HashMap::new();
        bar_index.insert(
            ("SA0".to_string(), "2026-07-23 09:45:00".to_string()),
            0usize,
        );
        bar_index.insert(
            ("SA0".to_string(), "2026-07-23 10:00:00".to_string()),
            1usize,
        );

        let mut ids = duplicate_event_ids(&[first, second], &bar_index);
        ids.sort_unstable();
        assert_eq!(ids, vec![1180]);
    }

    #[test]
    fn duplicate_event_ids_keeps_family_first_and_drops_rest() {
        let mut first = pattern_event(397, 3.8, "closed");
        first.symbol = "SS0".to_string();
        first.direction = "up".to_string();
        first.warning_ts = "2026-08-14 22:30:00".to_string();
        first.entry = 14170.0;
        first.risk = 38.675;
        first.trigger_ts = Some("2026-08-14 22:30:00".to_string());
        first.exit_ts = Some("2026-08-14 22:45:00".to_string());

        let mut second = pattern_event(1712, 3.6, "triggered");
        second.symbol = "SS0".to_string();
        second.direction = "up".to_string();
        second.warning_ts = "2026-08-14 22:45:00".to_string();
        second.entry = 14175.0;
        second.risk = 28.65;

        let mut far = pattern_event(1713, 3.4, "pending");
        far.symbol = "SS0".to_string();
        far.direction = "up".to_string();
        far.warning_ts = "2026-08-14 23:00:00".to_string();
        far.entry = 14250.0;
        far.risk = 30.0;

        let bars = vec![
            bar_model("2026-08-14 22:30:00", 0.0, 0.0, 0.0, 0.0),
            bar_model("2026-08-14 22:45:00", 0.0, 0.0, 0.0, 0.0),
            bar_model("2026-08-14 23:00:00", 0.0, 0.0, 0.0, 0.0),
        ];
        let mut bar_index: outcome::WarningBarIndex = HashMap::new();
        for (idx, bar) in bars.iter().enumerate() {
            bar_index.insert(("SS0".to_string(), bar_ts(bar)), idx);
        }

        let mut ids = duplicate_event_ids(&[first, second, far], &bar_index);
        ids.sort_unstable();
        // 1712 在 1713 预警时仍处于 triggered 持仓，入场价差再大也并入同一族。
        assert_eq!(ids, vec![1712, 1713]);
    }

    #[test]
    fn duplicate_event_ids_requires_entry_closeness_after_family_exits() {
        let mut first = pattern_event(397, 3.8, "closed");
        first.symbol = "SS0".to_string();
        first.direction = "up".to_string();
        first.warning_ts = "2026-08-14 22:30:00".to_string();
        first.entry = 14170.0;
        first.risk = 38.675;
        first.trigger_ts = Some("2026-08-14 22:30:00".to_string());
        first.exit_ts = Some("2026-08-14 22:45:00".to_string());

        let mut second = pattern_event(1712, 3.6, "closed");
        second.symbol = "SS0".to_string();
        second.direction = "up".to_string();
        second.warning_ts = "2026-08-14 22:45:00".to_string();
        second.entry = 14175.0;
        second.risk = 28.65;
        second.trigger_ts = Some("2026-08-14 22:45:00".to_string());
        second.exit_ts = Some("2026-08-14 23:00:00".to_string());

        let mut far = pattern_event(1713, 3.4, "pending");
        far.symbol = "SS0".to_string();
        far.direction = "up".to_string();
        far.warning_ts = "2026-08-14 23:15:00".to_string();
        far.entry = 14250.0;
        far.risk = 30.0;

        let bars = vec![
            bar_model("2026-08-14 22:30:00", 0.0, 0.0, 0.0, 0.0),
            bar_model("2026-08-14 22:45:00", 0.0, 0.0, 0.0, 0.0),
            bar_model("2026-08-14 23:00:00", 0.0, 0.0, 0.0, 0.0),
            bar_model("2026-08-14 23:15:00", 0.0, 0.0, 0.0, 0.0),
        ];
        let mut bar_index: outcome::WarningBarIndex = HashMap::new();
        for (idx, bar) in bars.iter().enumerate() {
            bar_index.insert(("SS0".to_string(), bar_ts(bar)), idx);
        }

        let mut ids = duplicate_event_ids(&[first, second, far], &bar_index);
        ids.sort_unstable();
        assert_eq!(ids, vec![1712]);
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





