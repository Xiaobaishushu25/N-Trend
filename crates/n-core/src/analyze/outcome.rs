//! 复盘统计与出场规则常量。
//!
//! 前向事件识别、入场评分、触发与持仓推进由 analyze::event 与 service 负责；
//! 本模块保留复盘聚合统计、分组口径和出场规则常量。

use std::collections::BTreeMap;
use std::collections::HashMap;

use chrono::{Datelike, Timelike};
use serde::Serialize;

use crate::analyze::model::Bar;

/// 前向事件系统版本号，写入复盘统计结果。
pub const SIM_VERSION: i64 = 12;
/// 相似预警去重：预警K线最多相隔多少根 15m。
pub const DEDUP_WARNING_BARS: usize = 5;
/// 相似预警去重：计划/实际入场价差上限（按前一条信号的 risk 折算）。
pub const DEDUP_ENTRY_R: f64 = 0.3;
/// 品种 + 预警K线时间到实际 15m 序列位置的映射，用于按K线根数做去重距离。
pub type WarningBarIndex = HashMap<(String, String), usize>;
/// 预警后最多等待多少根 15m bar 触发，与分析器 PENDING_MAX_AGE 对齐
pub const PENDING_BARS: usize = 12;
/// 入场后第 5 根 bar 做无跟随检查
pub const NO_FOLLOW_BAR: usize = 5;
/// 无跟随检查阈值：最大浮盈不足 0.5R 即退出
pub const NO_FOLLOW_MFE_R: f64 = 0.5;
/// 推动止盈起点：触及 1R 后才开始锁定
pub const TRAIL_START_R: f64 = 1.0;
/// 推动止盈步长：后续每达到 0.8×2R、0.8×3R、0.8×4R…… 上移一级
pub const TRAIL_STEP_R: f64 = 0.8;
/// 时间退出上限：入场后 60 根 15m bar 未决按收盘平仓
pub const TIME_HORIZON_BARS: usize = 60;
/// 量能确认窗口：触发 bar 前 20 根 15m 均量
pub const VOL_AVG_WINDOW: usize = 20;
/// 量能确认阈值：触发 bar 成交量 ≥ 前 20 根均量的 2.0 倍（复盘分桶显示 ≥2.0 才有明显区分度）
pub const VOL_CONFIRM_RATIO: f64 = 2.0;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Outcome {
    Win,
    Loss,
    NoTrigger,
    Open,
    InsufficientData,
    /// 模拟窗口内跨过连续合约换月：不计入盈亏统计，单独计数
    Rollover,
}

impl Outcome {
    pub fn as_str(self) -> &'static str {
        match self {
            Outcome::Win => "win",
            Outcome::Loss => "loss",
            Outcome::NoTrigger => "no_trigger",
            Outcome::Open => "open",
            Outcome::InsufficientData => "insufficient_data",
            Outcome::Rollover => "rollover",
        }
    }

    pub fn parse(s: &str) -> Option<Outcome> {
        match s {
            "win" => Some(Outcome::Win),
            "loss" => Some(Outcome::Loss),
            "no_trigger" => Some(Outcome::NoTrigger),
            "open" => Some(Outcome::Open),
            "insufficient_data" => Some(Outcome::InsufficientData),
            "rollover" => Some(Outcome::Rollover),
            _ => None,
        }
    }

    pub fn is_terminal(self) -> bool {
        matches!(
            self,
            Outcome::Win | Outcome::Loss | Outcome::NoTrigger | Outcome::Rollover
        )
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ExitReason {
    Stop,
    Target,
    NoFollow,
    TimeExit,
    Rollover,
    None,
}

impl ExitReason {
    pub fn as_str(self) -> &'static str {
        match self {
            ExitReason::Stop => "stop",
            ExitReason::Target => "target",
            ExitReason::NoFollow => "no_follow",
            ExitReason::TimeExit => "time_exit",
            ExitReason::Rollover => "rollover",
            ExitReason::None => "",
        }
    }

    pub fn parse(s: &str) -> ExitReason {
        match s {
            "stop" => ExitReason::Stop,
            "target" => ExitReason::Target,
            "no_follow" => ExitReason::NoFollow,
            "time_exit" => ExitReason::TimeExit,
            "rollover" => ExitReason::Rollover,
            _ => ExitReason::None,
        }
    }
}

/// 兼容两种时间格式：K线 ts 带秒、signals.created_at/预警时间无秒。
pub fn parse_minute(ts: &str) -> Option<(i32, i32, i32, i32, i32)> {
    for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"] {
        if let Ok(dt) = chrono::NaiveDateTime::parse_from_str(ts, fmt) {
            return Some((
                dt.year(),
                dt.month() as i32,
                dt.day() as i32,
                dt.hour() as i32,
                dt.minute() as i32,
            ));
        }
    }
    None
}

/// 触发 bar 的量能比：成交量 / 前 20 根均量；均量缺失时返回 None。
pub(crate) fn vol_ratio_at(bars: &[Bar], ec: usize) -> Option<f64> {
    let vol = bars.get(ec)?.volume;
    if vol <= 0.0 {
        return None;
    }
    let lo = ec.saturating_sub(VOL_AVG_WINDOW);
    if lo == ec {
        return None;
    }
    let mut sum = 0.0;
    let mut count = 0usize;
    for b in &bars[lo..ec] {
        if b.volume > 0.0 {
            sum += b.volume;
            count += 1;
        }
    }
    if count == 0 {
        return None;
    }
    Some(vol / (sum / count as f64))
}

// ===== 复盘统计 =====

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GroupBy {
    ScoreBand,
    Grade,
    Direction,
    Level,
    Hour,
    Symbol,
    VolConfirm,
    OiIncrease,
    Trend60Band,
    SymbolHour,
    ScoreVol,
    HourAtrBand,
    ExitReason,
    VolRatioBand,
    BVolRatioBand,
    RetracementBand,
    BaSpeedBand,
    AStrengthBand,
    TriggerLagBand,
    OvershootBand,
    TpTier,
    GapCombo,
    DimTrend,
    DimALeg,
    DimBLeg,
    DimWarning,
    DimTrigger,
    DimRr,
    DimMomentum,
}

impl GroupBy {
    pub fn parse(s: &str) -> GroupBy {
        match s {
            "grade" => GroupBy::Grade,
            "direction" => GroupBy::Direction,
            "level" => GroupBy::Level,
            "hour" => GroupBy::Hour,
            "symbol" => GroupBy::Symbol,
            "vol_confirm" => GroupBy::VolConfirm,
            "oi" => GroupBy::OiIncrease,
            "trend60" => GroupBy::Trend60Band,
            "symbol_hour" => GroupBy::SymbolHour,
            "score_vol" => GroupBy::ScoreVol,
            "hour_atr" => GroupBy::HourAtrBand,
            "exit_reason" => GroupBy::ExitReason,
            "vol_band" => GroupBy::VolRatioBand,
            "b_vol" => GroupBy::BVolRatioBand,
            "retracement" => GroupBy::RetracementBand,
            "b_a_speed" => GroupBy::BaSpeedBand,
            "a_strength" => GroupBy::AStrengthBand,
            "trigger_lag" => GroupBy::TriggerLagBand,
            "overshoot" => GroupBy::OvershootBand,
            "tp_tier" => GroupBy::TpTier,
            "gap_combo" => GroupBy::GapCombo,
            "dim_trend" => GroupBy::DimTrend,
            "dim_a" => GroupBy::DimALeg,
            "dim_b" => GroupBy::DimBLeg,
            "dim_warning" => GroupBy::DimWarning,
            "dim_trigger" => GroupBy::DimTrigger,
            "dim_rr" => GroupBy::DimRr,
            "dim_momentum" => GroupBy::DimMomentum,
            _ => GroupBy::ScoreBand,
        }
    }
}

/// 复盘统计口径：仅影响统计聚合，不影响明细、K线图或交易规则。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StatsScope {
    /// 全部信号（旧行为，不过滤评分）
    All,
    /// 可交易信号：评分 ≥ 2.5（小仓试错 + 标准仓）
    Tradable,
    /// 标准仓信号：评分 ≥ 3.5
    Standard,
}

/// 可交易信号的最低评分（小仓试错 + 标准仓）
pub const TRADABLE_MIN_SCORE: f64 = 2.5;
/// 标准仓信号的最低评分
pub const STANDARD_MIN_SCORE: f64 = 3.5;

impl StatsScope {
    pub fn parse(s: &str) -> StatsScope {
        match s {
            "tradable" => StatsScope::Tradable,
            "standard" => StatsScope::Standard,
            // 空值或未知值按旧的“全部信号”处理，兼容旧调用
            _ => StatsScope::All,
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            StatsScope::All => "all",
            StatsScope::Tradable => "tradable",
            StatsScope::Standard => "standard",
        }
    }

    fn matches(self, score: f64) -> bool {
        match self {
            StatsScope::All => true,
            StatsScope::Tradable => score >= TRADABLE_MIN_SCORE,
            StatsScope::Standard => score >= STANDARD_MIN_SCORE,
        }
    }
}

/// 参与统计的信号行（信号快照 + 结局）。
#[derive(Debug, Clone)]
pub struct StatRow {
    pub signal_id: i64,
    pub symbol: String,
    /// 前向事件系统版本，写入统计去重键；当前固定为 4。
    pub logic_version: String,
    pub direction: String,
    pub level: String,
    pub grade: String,
    pub score: f64,
    pub created_at: String,
    pub warning_ts: Option<String>,
    /// a 段起点（s0）K线时间，用于判断多个预警是否同源
    pub s0_ts: Option<String>,
    pub s1_ts: Option<String>,
    pub s2_ts: Option<String>,
    /// 触发所在 15m K线收盘时间；未触发事件为 None
    pub trigger_bar_ts: Option<String>,
    /// 实际入场价；未触发事件为 None
    pub entry: Option<f64>,
    /// 单笔风险，用于相似预警的入场价差折算
    pub risk: f64,
    pub outcome: Option<Outcome>,
    pub r_multiple: Option<f64>,
    pub mfe_r: Option<f64>,
    pub mae_r: Option<f64>,
    pub bars_held: Option<usize>,
    pub vol_ratio: Option<f64>,
    pub oi_increase: Option<bool>,
    pub trend60_score: Option<f64>,
    pub atr_percentile: Option<f64>,
    pub exit_reason: Option<ExitReason>,
    pub target_tier: Option<String>,
    pub extended_target: bool,
    pub b_vol_ratio: Option<f64>,
    pub a_move_atr: Option<f64>,
    pub trigger_lag_bars: Option<usize>,
    pub trigger_overshoot_r: Option<f64>,
    pub a_move: Option<f64>,
    pub b_move: Option<f64>,
    pub a_bars: Option<usize>,
    pub b_bars: Option<usize>,
    pub retracement: Option<f64>,
    pub dims: Option<[f64; 6]>,
    pub net_r: Option<f64>,
    pub rollover_crossed: bool,
    pub gap_crossed_entry: bool,
    pub gap_crossed_exit: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct GroupStat {
    pub key: String,
    pub n: usize,
    pub pending: usize,
    pub no_trigger: usize,
    pub settled: usize,
    pub wins: usize,
    pub losses: usize,
    pub rollover: usize,
    pub gap_entry: usize,
    pub gap_exit: usize,
    pub win_rate: Option<f64>,
    pub avg_r: Option<f64>,
    pub avg_bars: Option<f64>,
    pub avg_win_r: Option<f64>,
    pub avg_loss_r: Option<f64>,
    pub payoff: Option<f64>,
    pub profit_factor: Option<f64>,
    pub r_ge1_rate: Option<f64>,
    pub r_ge2_rate: Option<f64>,
    pub mfe_ge1_rate: Option<f64>,
    pub mae_le_neg1_rate: Option<f64>,
    pub avg_r_mfe_ge1: Option<f64>,
    pub avg_r_mae_le_neg1: Option<f64>,
    pub avg_net_r: Option<f64>,
    pub ext_target_n: usize,
    pub tp1_exits: usize,
    pub tp2_exits: usize,
    pub tp2_conversion: Option<f64>,
    pub tp2_of_ext_rate: Option<f64>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ReviewStats {
    pub sim_version: i64,
    pub overall: GroupStat,
    pub groups: Vec<GroupStat>,
}

/// 结构键：同一演变结构跨扫描的重复信号按首条（min signal_id）去重。
/// 仅用于未触发事件及缺少交易族信息的旧记录。
fn structure_key(r: &StatRow) -> String {
    match (&r.s1_ts, &r.s2_ts) {
        (Some(s1), Some(s2)) => {
            format!(
                "{}|{}|{}|{}|{}|{}",
                r.symbol, r.logic_version, r.direction, r.level, s1, s2
            )
        }
        // 旧数据缺少结构时间戳时退回信号自身，不做合并
        _ => format!(
            "{}|{}|{}|{}|id{}",
            r.symbol, r.logic_version, r.direction, r.level, r.signal_id
        ),
    }
}

fn warning_bars_gap_str(later: &str, earlier: &str) -> Option<usize> {
    let (a, b) = (later, earlier);
    let pa = parse_minute(a)?;
    let pb = parse_minute(b)?;
    let minutes = chrono::NaiveDate::from_ymd_opt(pa.0, pa.1 as u32, pa.2 as u32)?
        .and_hms_opt(pa.3 as u32, pa.4 as u32, 0)?
        .signed_duration_since(
            chrono::NaiveDate::from_ymd_opt(pb.0, pb.1 as u32, pb.2 as u32)?.and_hms_opt(
                pb.3 as u32,
                pb.4 as u32,
                0,
            )?,
        )
        .num_minutes();
    Some((minutes / 15).max(0) as usize)
}

/// 优先按实际 15m K线序号差计算；旧数据或K线范围外找不到序号时退回自然时间估算。
fn warning_bars_gap(
    symbol: &str,
    later: &str,
    earlier: &str,
    bar_index: &WarningBarIndex,
) -> Option<usize> {
    match (
        bar_index.get(&(symbol.to_string(), later.to_string())),
        bar_index.get(&(symbol.to_string(), earlier.to_string())),
    ) {
        (Some(&li), Some(&ei)) => Some(li.abs_diff(ei)),
        _ => warning_bars_gap_str(later, earlier),
    }
}

/// 入场价差按族首（更早一条）信号的 risk 折算，与实时层口径一致。
fn entries_close(candidate: &StatRow, anchor: &StatRow) -> bool {
    match (candidate.entry, anchor.entry) {
        (Some(x), Some(y)) => (x - y).abs() <= DEDUP_ENTRY_R * anchor.risk.max(1e-9),
        _ => false,
    }
}

/// 统计去重：同品种、同方向、预警K线相差不超过 5 根 15m、入场价差不超过
/// 0.3R 的信号聚成同一族，族内保留首见（min signal_id）；不再依赖 A/B 段、
/// 级别或评级是否相同。缺少入场价的旧记录退回结构键。
fn dedup_first_seen<'a>(rows: &'a [StatRow], bar_index: &WarningBarIndex) -> Vec<&'a StatRow> {
    let mut ordered: Vec<&StatRow> = rows.iter().collect();
    ordered.sort_by(|a, b| {
        a.warning_ts
            .as_deref()
            .cmp(&b.warning_ts.as_deref())
            .then_with(|| a.signal_id.cmp(&b.signal_id))
    });

    // 族内保存族首（用于入场价/风险比较）和最近一条预警K线时间（用于连续
    // 预警的 5 根距离判断，允许 0/5/10 这类连续节奏持续并入同一族）。
    let mut families: Vec<(&StatRow, String)> = Vec::new();
    let mut structure_min: HashMap<String, &StatRow> = HashMap::new();
    for r in ordered {
        if r.entry.is_some() {
            let mut merged = false;
            for (anchor, last_warning_ts) in &mut families {
                if r.symbol == anchor.symbol
                    && r.logic_version == anchor.logic_version
                    && r.direction == anchor.direction
                    && entries_close(r, anchor)
                    && r.warning_ts
                        .as_deref()
                        .zip(Some(last_warning_ts.as_str()))
                        .and_then(|(a, b)| warning_bars_gap(&r.symbol, a, b, bar_index))
                        .is_some_and(|gap| gap <= DEDUP_WARNING_BARS)
                {
                    merged = true;
                    if let Some(ts) = r.warning_ts.as_deref() {
                        *last_warning_ts = ts.to_string();
                    }
                    break;
                }
            }
            if !merged {
                families.push((r, r.warning_ts.clone().unwrap_or_default()));
            }
        } else {
            let key = structure_key(r);
            structure_min
                .entry(key)
                .and_modify(|cur| {
                    if r.signal_id < cur.signal_id {
                        *cur = r;
                    }
                })
                .or_insert(r);
        }
    }
    let mut out: Vec<&StatRow> = families.into_iter().map(|(r, _)| r).collect();
    out.extend(structure_min.into_values());
    out.sort_by_key(|r| r.signal_id);
    out
}

fn summarize(key: &str, rows: &[&StatRow]) -> GroupStat {
    let n = rows.len();
    let mut settled = 0usize;
    let mut wins = 0usize;
    let mut losses = 0usize;
    let mut rollover = 0usize;
    let mut gap_entry = 0usize;
    let mut gap_exit = 0usize;
    let mut no_trigger = 0usize;
    let mut pending = 0usize;
    let mut r_sum = 0.0_f64;
    let mut r_count = 0usize;
    let mut bars_sum = 0.0_f64;
    let mut bars_count = 0usize;
    let mut win_r_sum = 0.0_f64;
    let mut win_r_count = 0usize;
    let mut loss_r_sum = 0.0_f64;
    let mut loss_r_count = 0usize;
    let mut gross_win = 0.0_f64;
    let mut gross_loss = 0.0_f64;
    let mut r_ge1 = 0usize;
    let mut r_ge2 = 0usize;
    let mut mfe_count = 0usize;
    let mut mfe_ge1 = 0usize;
    let mut r_mfe_ge1_sum = 0.0_f64;
    let mut r_mfe_ge1_count = 0usize;
    let mut mae_count = 0usize;
    let mut mae_le_neg1 = 0usize;
    let mut r_mae_le_neg1_sum = 0.0_f64;
    let mut r_mae_le_neg1_count = 0usize;
    let mut net_r_sum = 0.0_f64;
    let mut net_r_count = 0usize;
    let mut ext_target_n = 0usize;
    let mut tp1_exits = 0usize;
    let mut tp2_exits = 0usize;

    for r in rows {
        if r.gap_crossed_entry {
            gap_entry += 1;
        }
        if r.gap_crossed_exit {
            gap_exit += 1;
        }
        if r.extended_target {
            ext_target_n += 1;
        }
        match r.target_tier.as_deref() {
            Some("tp2") => tp2_exits += 1,
            Some("tp1") => tp1_exits += 1,
            _ => {}
        }
        match r.outcome {
            Some(Outcome::Win) => {
                settled += 1;
                wins += 1;
                if let Some(x) = r.r_multiple {
                    r_sum += x;
                    r_count += 1;
                    win_r_sum += x;
                    win_r_count += 1;
                    gross_win += x;
                    if x >= 1.0 {
                        r_ge1 += 1;
                    }
                    if x >= 2.0 {
                        r_ge2 += 1;
                    }
                }
                if let Some(b) = r.bars_held {
                    bars_sum += b as f64;
                    bars_count += 1;
                }
                if let Some(x) = r.net_r {
                    net_r_sum += x;
                    net_r_count += 1;
                }
            }
            Some(Outcome::Loss) => {
                settled += 1;
                losses += 1;
                if let Some(x) = r.r_multiple {
                    r_sum += x;
                    r_count += 1;
                    loss_r_sum += x;
                    loss_r_count += 1;
                    gross_loss += x;
                }
                if let Some(b) = r.bars_held {
                    bars_sum += b as f64;
                    bars_count += 1;
                }
                if let Some(x) = r.net_r {
                    net_r_sum += x;
                    net_r_count += 1;
                }
            }
            Some(Outcome::NoTrigger) => no_trigger += 1,
            Some(Outcome::Rollover) => rollover += 1,
            Some(Outcome::Open) | Some(Outcome::InsufficientData) => pending += 1,
            None => {}
        }
        if matches!(r.outcome, Some(Outcome::Win) | Some(Outcome::Loss)) {
            if let Some(m) = r.mfe_r {
                mfe_count += 1;
                if m >= 1.0 {
                    mfe_ge1 += 1;
                    if let Some(x) = r.r_multiple {
                        r_mfe_ge1_sum += x;
                        r_mfe_ge1_count += 1;
                    }
                }
            }
            if let Some(m) = r.mae_r {
                mae_count += 1;
                if m <= -1.0 {
                    mae_le_neg1 += 1;
                    if let Some(x) = r.r_multiple {
                        r_mae_le_neg1_sum += x;
                        r_mae_le_neg1_count += 1;
                    }
                }
            }
        }
    }

    let avg_win_r = if win_r_count > 0 {
        Some(win_r_sum / win_r_count as f64)
    } else {
        None
    };
    let avg_loss_r = if loss_r_count > 0 {
        Some(loss_r_sum / loss_r_count as f64)
    } else {
        None
    };
    GroupStat {
        key: key.to_string(),
        n,
        pending,
        no_trigger,
        settled,
        wins,
        losses,
        rollover,
        gap_entry,
        gap_exit,
        win_rate: if settled > 0 {
            Some(wins as f64 / settled as f64)
        } else {
            None
        },
        avg_r: if r_count > 0 {
            Some(r_sum / r_count as f64)
        } else {
            None
        },
        avg_bars: if bars_count > 0 {
            Some(bars_sum / bars_count as f64)
        } else {
            None
        },
        avg_win_r,
        avg_loss_r,
        payoff: avg_win_r
            .zip(avg_loss_r)
            .filter(|(_, loss)| *loss < 0.0)
            .map(|(win, loss)| win / -loss),
        profit_factor: if gross_loss < 0.0 {
            Some(gross_win / -gross_loss)
        } else {
            None
        },
        r_ge1_rate: if r_count > 0 {
            Some(r_ge1 as f64 / r_count as f64)
        } else {
            None
        },
        r_ge2_rate: if r_count > 0 {
            Some(r_ge2 as f64 / r_count as f64)
        } else {
            None
        },
        mfe_ge1_rate: if mfe_count > 0 {
            Some(mfe_ge1 as f64 / mfe_count as f64)
        } else {
            None
        },
        mae_le_neg1_rate: if mae_count > 0 {
            Some(mae_le_neg1 as f64 / mae_count as f64)
        } else {
            None
        },
        avg_r_mfe_ge1: if r_mfe_ge1_count > 0 {
            Some(r_mfe_ge1_sum / r_mfe_ge1_count as f64)
        } else {
            None
        },
        avg_r_mae_le_neg1: if r_mae_le_neg1_count > 0 {
            Some(r_mae_le_neg1_sum / r_mae_le_neg1_count as f64)
        } else {
            None
        },
        avg_net_r: if net_r_count > 0 {
            Some(net_r_sum / net_r_count as f64)
        } else {
            None
        },
        ext_target_n,
        tp1_exits,
        tp2_exits,
        tp2_conversion: if ext_target_n > 0 {
            Some(tp2_exits as f64 / ext_target_n as f64)
        } else {
            None
        },
        tp2_of_ext_rate: if tp1_exits + tp2_exits > 0 {
            Some(tp2_exits as f64 / (tp1_exits + tp2_exits) as f64)
        } else {
            None
        },
    }
}

fn hour_of(r: &StatRow) -> String {
    let ts = r
        .warning_ts
        .as_deref()
        .filter(|s| !s.is_empty())
        .unwrap_or(&r.created_at);
    parse_minute(ts)
        .map(|(_, _, _, h, _)| format!("{h:02}:00"))
        .unwrap_or_else(|| "未知".to_string())
}

fn score_band_label(score: f64) -> String {
    if score < 2.5 {
        "<2.5".to_string()
    } else if score < 3.5 {
        "2.5-3.5".to_string()
    } else {
        "3.5-5.0".to_string()
    }
}

fn vol_label(r: &StatRow) -> String {
    match r.vol_ratio {
        Some(v) if v >= VOL_CONFIRM_RATIO => "放量确认".to_string(),
        Some(_) => "未放量".to_string(),
        None => "数据缺失".to_string(),
    }
}

fn atr_band_label(v: Option<f64>) -> String {
    match v {
        Some(x) if x < 0.25 => "低波动".to_string(),
        Some(x) if x >= 0.75 => "高波动".to_string(),
        Some(_) => "中波动".to_string(),
        None => "数据缺失".to_string(),
    }
}

fn bucket(group_by: GroupBy, r: &StatRow) -> String {
    match group_by {
        GroupBy::ScoreBand => {
            if r.score < 2.5 {
                "<2.5".to_string()
            } else if r.score < 3.5 {
                "2.5-3.5".to_string()
            } else {
                "3.5-5.0".to_string()
            }
        }
        GroupBy::Grade => r.grade.clone(),
        GroupBy::Direction => {
            if r.direction == "up" {
                "做多".to_string()
            } else {
                "做空".to_string()
            }
        }
        GroupBy::Level => {
            if r.level == "fine" {
                "精细".to_string()
            } else {
                "较大".to_string()
            }
        }
        GroupBy::Hour => hour_of(r),
        GroupBy::Symbol => r.symbol.clone(),
        GroupBy::VolConfirm => match r.vol_ratio {
            Some(v) if v >= VOL_CONFIRM_RATIO => "放量确认".to_string(),
            Some(_) => "未放量".to_string(),
            None => "数据缺失".to_string(),
        },
        GroupBy::OiIncrease => match r.oi_increase {
            Some(true) => "增仓".to_string(),
            Some(false) => "未增仓".to_string(),
            None => "数据缺失".to_string(),
        },
        GroupBy::Trend60Band => match r.trend60_score {
            Some(v) if v >= 3.5 => "≥3.5".to_string(),
            Some(v) if v >= 2.5 => "2.5-3.5".to_string(),
            Some(_) => "<2.5".to_string(),
            None => "数据缺失".to_string(),
        },
        GroupBy::SymbolHour => format!("{} {}", r.symbol, hour_of(r)),
        GroupBy::ScoreVol => format!("{} / {}", score_band_label(r.score), vol_label(r)),
        GroupBy::HourAtrBand => format!("{} / {}", hour_of(r), atr_band_label(r.atr_percentile)),
        GroupBy::ExitReason => match r.exit_reason {
            Some(ExitReason::Target) => "止盈".to_string(),
            Some(ExitReason::Stop) => "止损".to_string(),
            Some(ExitReason::NoFollow) => "无跟随".to_string(),
            Some(ExitReason::TimeExit) => "时间退出".to_string(),
            Some(ExitReason::Rollover) => "换月".to_string(),
            _ => "其他".to_string(),
        },
        GroupBy::VolRatioBand => match r.vol_ratio {
            Some(v) if v >= 2.0 => "≥2.0".to_string(),
            Some(v) if v >= 1.3 => "1.3-2.0".to_string(),
            Some(v) if v >= 1.0 => "1.0-1.3".to_string(),
            Some(v) if v >= 0.8 => "0.8-1.0".to_string(),
            Some(_) => "<0.8".to_string(),
            None => "数据缺失".to_string(),
        },
        GroupBy::BVolRatioBand => match r.b_vol_ratio {
            Some(v) if v >= 1.3 => "≥1.3".to_string(),
            Some(v) if v >= 1.0 => "1.0-1.3".to_string(),
            Some(v) if v >= 0.8 => "0.8-1.0".to_string(),
            Some(_) => "<0.8".to_string(),
            None => "数据缺失".to_string(),
        },
        GroupBy::RetracementBand => match r.retracement {
            Some(v) if v >= 0.66 => ">66%".to_string(),
            Some(v) if v >= 0.50 => "50-66%".to_string(),
            Some(v) if v >= 0.33 => "33-50%".to_string(),
            Some(v) if v >= 0.20 => "20-33%".to_string(),
            Some(_) => "<20%".to_string(),
            None => "数据缺失".to_string(),
        },
        GroupBy::BaSpeedBand => {
            let speed = match (r.a_move, r.b_move, r.a_bars, r.b_bars) {
                (Some(a), Some(b), Some(ab), Some(bb)) if ab > 0 && bb > 0 && a > 0.0 => {
                    Some((b / bb as f64) / (a / ab as f64))
                }
                _ => None,
            };
            match speed {
                Some(v) if v >= 1.2 => "≥1.2".to_string(),
                Some(v) if v >= 0.8 => "0.8-1.2".to_string(),
                Some(v) if v >= 0.5 => "0.5-0.8".to_string(),
                Some(_) => "<0.5".to_string(),
                None => "数据缺失".to_string(),
            }
        }
        GroupBy::AStrengthBand => match r.a_move_atr {
            Some(v) if v >= 10.0 => "≥10".to_string(),
            Some(v) if v >= 6.0 => "6-10".to_string(),
            Some(v) if v >= 3.0 => "3-6".to_string(),
            Some(_) => "<3".to_string(),
            None => "数据缺失".to_string(),
        },
        GroupBy::TriggerLagBand => match r.trigger_lag_bars {
            Some(1) => "1".to_string(),
            Some(2..=3) => "2-3".to_string(),
            Some(4..=6) => "4-6".to_string(),
            Some(7..=12) => "7-12".to_string(),
            Some(_) => ">12".to_string(),
            None => "数据缺失".to_string(),
        },
        GroupBy::OvershootBand => match r.trigger_overshoot_r {
            Some(v) if v >= 0.6 => "≥0.6".to_string(),
            Some(v) if v >= 0.3 => "0.3-0.6".to_string(),
            Some(v) if v >= 0.1 => "0.1-0.3".to_string(),
            Some(_) => "<0.1".to_string(),
            None => "数据缺失".to_string(),
        },
        GroupBy::TpTier => match r.target_tier.as_deref() {
            Some("tp2") => "TP2扩展止盈".to_string(),
            Some("tp1") => "TP1止盈".to_string(),
            _ => "其他".to_string(),
        },
        GroupBy::GapCombo => match (r.gap_crossed_entry, r.gap_crossed_exit) {
            (true, true) => "双跳空".to_string(),
            (true, false) => "入场跳空".to_string(),
            (false, true) => "出场跳空".to_string(),
            (false, false) => "无跳空".to_string(),
        },
        GroupBy::DimTrend => dim_band(r.dims.map(|d| d[0])),
        GroupBy::DimALeg => dim_band(r.dims.map(|d| d[1])),
        GroupBy::DimBLeg => dim_band(r.dims.map(|d| d[2])),
        GroupBy::DimWarning => dim_band(r.dims.map(|d| d[3])),
        GroupBy::DimTrigger => dim_band(r.dims.map(|d| d[3])),
        GroupBy::DimRr => dim_band(r.dims.map(|d| d[4])),
        GroupBy::DimMomentum => dim_band(r.dims.map(|d| d[5])),
    }
}

fn dim_band(v: Option<f64>) -> String {
    match v {
        Some(x) if x >= 3.5 => "≥3.5".to_string(),
        Some(x) if x >= 2.0 => "2.0-3.5".to_string(),
        Some(_) => "<2.0".to_string(),
        None => "数据缺失".to_string(),
    }
}

/// 分组排序：有序维度按"高到低"排（评分段/趋势分/等级/量能/持仓），
/// 名义维度（品种/小时）按已结算数降序，保证复盘页顺序稳定且直观。
fn group_rank(group_by: GroupBy, key: &str) -> (u8, String) {
    let rank = match group_by {
        GroupBy::ScoreBand => match key {
            "3.5-5.0" => 0,
            "2.5-3.5" => 1,
            "<2.5" => 2,
            _ => 9,
        },
        GroupBy::Trend60Band => match key {
            "≥3.5" => 0,
            "2.5-3.5" => 1,
            "<2.5" => 2,
            _ => 9,
        },
        GroupBy::Grade => match key {
            "A级" => 0,
            "B级" => 1,
            "C级" => 2,
            "回撤过浅" => 3,
            "回撤过深" => 4,
            _ => 9,
        },
        GroupBy::Direction => {
            if key == "做多" {
                0
            } else {
                1
            }
        }
        GroupBy::Level => {
            if key == "精细" {
                0
            } else {
                1
            }
        }
        GroupBy::VolConfirm => match key {
            "放量确认" => 0,
            "未放量" => 1,
            _ => 2,
        },
        GroupBy::OiIncrease => match key {
            "增仓" => 0,
            "未增仓" => 1,
            _ => 2,
        },
        GroupBy::Hour => key
            .split_once(':')
            .and_then(|(h, _)| h.parse::<u8>().ok())
            .unwrap_or(24),
        GroupBy::Symbol => 0,
        GroupBy::SymbolHour | GroupBy::ScoreVol | GroupBy::HourAtrBand => 0,
        GroupBy::ExitReason => match key {
            "止盈" => 0,
            "止损" => 1,
            "无跟随" => 2,
            "时间退出" => 3,
            "换月" => 4,
            "其他" => 5,
            _ => 9,
        },
        GroupBy::VolRatioBand => match key {
            "≥2.0" => 0,
            "1.3-2.0" => 1,
            "1.0-1.3" => 2,
            "0.8-1.0" => 3,
            "<0.8" => 4,
            _ => 9,
        },
        GroupBy::BVolRatioBand => match key {
            "≥1.3" => 0,
            "1.0-1.3" => 1,
            "0.8-1.0" => 2,
            "<0.8" => 3,
            _ => 9,
        },
        GroupBy::RetracementBand => match key {
            ">66%" => 0,
            "50-66%" => 1,
            "33-50%" => 2,
            "20-33%" => 3,
            "<20%" => 4,
            _ => 9,
        },
        GroupBy::BaSpeedBand => match key {
            "≥1.2" => 0,
            "0.8-1.2" => 1,
            "0.5-0.8" => 2,
            "<0.5" => 3,
            _ => 9,
        },
        GroupBy::AStrengthBand => match key {
            "≥10" => 0,
            "6-10" => 1,
            "3-6" => 2,
            "<3" => 3,
            _ => 9,
        },
        GroupBy::TriggerLagBand => match key {
            "1" => 0,
            "2-3" => 1,
            "4-6" => 2,
            "7-12" => 3,
            ">12" => 4,
            _ => 9,
        },
        GroupBy::OvershootBand => match key {
            "<0.1" => 0,
            "0.1-0.3" => 1,
            "0.3-0.6" => 2,
            "≥0.6" => 3,
            _ => 9,
        },
        GroupBy::TpTier => match key {
            "TP2扩展止盈" => 0,
            "TP1止盈" => 1,
            "其他" => 2,
            _ => 9,
        },
        GroupBy::GapCombo => match key {
            "双跳空" => 0,
            "入场跳空" => 1,
            "出场跳空" => 2,
            "无跳空" => 3,
            _ => 9,
        },
        GroupBy::DimTrend
        | GroupBy::DimALeg
        | GroupBy::DimBLeg
        | GroupBy::DimWarning
        | GroupBy::DimTrigger
        | GroupBy::DimRr
        | GroupBy::DimMomentum => match key {
            "≥3.5" => 0,
            "2.0-3.5" => 1,
            "<2.0" => 2,
            _ => 9,
        },
    };
    (rank, key.to_string())
}

/// 聚合复盘统计：先按结构键去重（取首条），再按维度分组。
pub fn aggregate_stats(rows: &[StatRow], group_by: GroupBy) -> ReviewStats {
    aggregate_stats_scoped(rows, group_by, StatsScope::All)
}

/// 按统计口径过滤后再聚合：all 不过滤，tradable 仅保留评分 ≥ 2.5，
/// standard 仅保留评分 ≥ 3.5。
pub fn aggregate_stats_scoped(
    rows: &[StatRow],
    group_by: GroupBy,
    scope: StatsScope,
) -> ReviewStats {
    aggregate_stats_scoped_with_bar_index(rows, group_by, scope, &HashMap::new())
}

/// 带实际 15m K线序号的聚合：去重距离按序号差计算，避免午休/夜盘自然时间被折算成根数。
pub fn aggregate_stats_scoped_with_bar_index(
    rows: &[StatRow],
    group_by: GroupBy,
    scope: StatsScope,
    warning_bar_index: &WarningBarIndex,
) -> ReviewStats {
    let dedup: Vec<&StatRow> = dedup_first_seen(rows, warning_bar_index)
        .into_iter()
        .filter(|r| scope.matches(r.score))
        .collect();
    let overall = summarize("全部", &dedup);

    let mut buckets: BTreeMap<String, Vec<&StatRow>> = BTreeMap::new();
    for r in &dedup {
        buckets.entry(bucket(group_by, r)).or_default().push(r);
    }
    let mut groups: Vec<GroupStat> = buckets
        .into_iter()
        .map(|(k, v)| summarize(&k, &v))
        .collect();
    groups.sort_by(|a, b| {
        let (ra, ka) = group_rank(group_by, &a.key);
        let (rb, kb) = group_rank(group_by, &b.key);
        ra.cmp(&rb)
            .then_with(|| ka.cmp(&kb))
            .then_with(|| b.settled.cmp(&a.settled))
            .then_with(|| b.n.cmp(&a.n))
    });

    ReviewStats {
        sim_version: SIM_VERSION,
        overall,
        groups,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stats_dedup_and_grouping() {
        let mk = |id: i64,
                  symbol: &str,
                  score: f64,
                  outcome: Option<Outcome>,
                  r: Option<f64>,
                  s2_ts: &'static str| {
            StatRow {
                signal_id: id,
                symbol: symbol.to_string(),
                logic_version: "1".to_string(),
                direction: "up".to_string(),
                level: "fine".to_string(),
                grade: "A级".to_string(),
                score,
                created_at: "2026-08-03 09:15".to_string(),
                warning_ts: Some("2026-08-03 09:15".to_string()),
                s0_ts: Some("2026-08-03 08:30".to_string()),
                s1_ts: Some("2026-08-03 08:45".to_string()),
                s2_ts: Some(s2_ts.to_string()),
                trigger_bar_ts: None,
                entry: None,
                risk: 0.0,
                outcome,
                r_multiple: r,
                mfe_r: Some(2.0),
                mae_r: Some(-1.0),
                bars_held: Some(3),
                vol_ratio: Some(2.0),
                oi_increase: Some(true),
                trend60_score: Some(3.8),
                atr_percentile: Some(0.6),
                exit_reason: None,
                target_tier: None,
                extended_target: false,
                b_vol_ratio: None,
                a_move_atr: None,
                trigger_lag_bars: None,
                trigger_overshoot_r: None,
                a_move: None,
                b_move: None,
                a_bars: None,
                b_bars: None,
                retracement: None,
                dims: None,
                net_r: None,
                rollover_crossed: false,
                gap_crossed_entry: false,
                gap_crossed_exit: false,
            }
        };
        // 同一结构键重复 3 次（id 递增），应只保留首条；另一结构 2 条
        let rows = vec![
            mk(
                1,
                "RB0",
                3.2,
                Some(Outcome::Win),
                Some(1.5),
                "2026-08-03 09:15",
            ),
            mk(
                2,
                "RB0",
                3.2,
                Some(Outcome::Loss),
                Some(-1.0),
                "2026-08-03 09:15",
            ),
            mk(
                3,
                "RB0",
                3.2,
                Some(Outcome::Win),
                Some(2.0),
                "2026-08-03 09:15",
            ),
            mk(
                4,
                "RB0",
                2.0,
                Some(Outcome::Loss),
                Some(-1.0),
                "2026-08-03 10:30",
            ),
            mk(
                5,
                "RB0",
                2.0,
                Some(Outcome::Win),
                Some(1.0),
                "2026-08-03 10:30",
            ),
        ];
        let stats = aggregate_stats(&rows, GroupBy::ScoreBand);
        assert_eq!(stats.overall.n, 2); // 去重后 2 个实例
        assert_eq!(stats.overall.settled, 2);
        assert_eq!(stats.overall.wins, 1);
        assert_eq!(stats.overall.losses, 1);
        assert_eq!(stats.overall.win_rate, Some(0.5));
        assert_eq!(stats.groups.len(), 2);
        // 评分段按高到低排列：2.5-3.5 在 <2.5 之前
        assert_eq!(stats.groups[0].key, "2.5-3.5");

        let vol = aggregate_stats(&rows, GroupBy::VolConfirm);
        assert!(vol.groups.iter().any(|g| g.key == "放量确认"));
    }

    #[test]
    fn stats_dedup_isolates_logic_versions() {
        let mk = |id: i64, version: &str| StatRow {
            signal_id: id,
            symbol: "RB0".to_string(),
            logic_version: version.to_string(),
            direction: "up".to_string(),
            level: "fine".to_string(),
            grade: "A级".to_string(),
            score: 3.8,
            created_at: "2026-08-03 09:15".to_string(),
            warning_ts: Some("2026-08-03 09:15".to_string()),
            s0_ts: Some("2026-08-03 08:30".to_string()),
            s1_ts: Some("2026-08-03 08:45".to_string()),
            s2_ts: Some("2026-08-03 09:15".to_string()),
            trigger_bar_ts: None,
            entry: None,
            risk: 0.0,
            outcome: Some(Outcome::Win),
            r_multiple: Some(1.0),
            mfe_r: Some(1.5),
            mae_r: Some(-0.5),
            bars_held: Some(3),
            vol_ratio: Some(1.0),
            oi_increase: Some(true),
            trend60_score: Some(3.0),
            atr_percentile: Some(0.4),
            exit_reason: Some(ExitReason::Target),
            target_tier: None,
            extended_target: false,
            b_vol_ratio: None,
            a_move_atr: None,
            trigger_lag_bars: None,
            trigger_overshoot_r: None,
            a_move: None,
            b_move: None,
            a_bars: None,
            b_bars: None,
            retracement: None,
            dims: None,
            net_r: None,
            rollover_crossed: false,
            gap_crossed_entry: false,
            gap_crossed_exit: false,
        };
        // 同结构同版本只留首条；1.0 与 2.0 同名结构互不覆盖。
        let rows = vec![mk(1, "1"), mk(2, "1"), mk(3, "2"), mk(4, "2")];
        let stats = aggregate_stats(&rows, GroupBy::ScoreBand);
        assert_eq!(stats.overall.n, 2);
    }

    #[test]
    fn stats_merge_same_trade_family_keep_first_seen() {
        let mk =
            |id: i64, warning: &str, trigger: &str, entry: f64, outcome: Outcome, r: f64| StatRow {
                signal_id: id,
                symbol: "UR0".to_string(),
                logic_version: "3".to_string(),
                direction: "up".to_string(),
                level: "fine".to_string(),
                grade: "C级".to_string(),
                score: 3.8,
                created_at: warning.to_string(),
                warning_ts: Some(warning.to_string()),
                s0_ts: Some(format!("2026-08-13 09:{id:02}")),
                s1_ts: Some(format!("2026-08-13 13:{id:02}")),
                s2_ts: Some(warning.to_string()),
                trigger_bar_ts: Some(trigger.to_string()),
                entry: Some(entry),
                risk: 17.0,
                outcome: Some(outcome),
                r_multiple: Some(r),
                mfe_r: Some(2.0),
                mae_r: Some(-1.0),
                bars_held: Some(3),
                vol_ratio: Some(1.0),
                oi_increase: Some(true),
                trend60_score: Some(3.0),
                atr_percentile: Some(0.4),
                exit_reason: None,
                target_tier: None,
                extended_target: false,
                b_vol_ratio: None,
                a_move_atr: None,
                trigger_lag_bars: None,
                trigger_overshoot_r: None,
                a_move: None,
                b_move: None,
                a_bars: None,
                b_bars: None,
                retracement: None,
                dims: None,
                net_r: None,
                rollover_crossed: false,
                gap_crossed_entry: false,
                gap_crossed_exit: false,
            };
        // 1436/1437 场景：同品种同方向、预警K线相差 1 根、入场价相同，只算首见一笔；
        // 预警K线相隔 6 根后即使入场价相同，也算另一笔实际交易。
        let rows = vec![
            mk(
                1,
                "2026-08-13 09:30",
                "2026-08-14 10:00",
                1675.0,
                Outcome::Loss,
                -0.375,
            ),
            mk(
                2,
                "2026-08-13 09:45",
                "2026-08-14 10:00",
                1675.0,
                Outcome::Loss,
                -1.0,
            ),
            mk(
                3,
                "2026-08-13 11:15",
                "2026-08-14 10:45",
                1675.0,
                Outcome::Win,
                1.0,
            ),
        ];
        let stats = aggregate_stats(&rows, GroupBy::ScoreBand);
        assert_eq!(stats.overall.n, 2);
        assert_eq!(stats.overall.settled, 2);
        assert_eq!(stats.overall.losses, 1);
        assert_eq!(stats.overall.wins, 1);
        assert_eq!(stats.overall.avg_r, Some(0.3125));
        assert_eq!(stats.overall.avg_loss_r, Some(-0.375));
    }

    #[test]
    fn stats_dedup_similar_warnings_by_proximity_only() {
        let mk = |id: i64, warning: &str, entry: f64, risk: f64, outcome: Outcome| StatRow {
            signal_id: id,
            symbol: "JD0".to_string(),
            logic_version: "3".to_string(),
            direction: "down".to_string(),
            level: "large".to_string(),
            grade: "A级".to_string(),
            score: 3.8,
            created_at: warning.to_string(),
            warning_ts: Some(warning.to_string()),
            s0_ts: Some("2026-08-13 09:30".to_string()),
            s1_ts: Some(format!("2026-08-13 13:{id:02}")),
            s2_ts: Some(warning.to_string()),
            trigger_bar_ts: None,
            entry: Some(entry),
            risk,
            outcome: Some(outcome),
            r_multiple: Some(if outcome == Outcome::Loss { -1.0 } else { 1.0 }),
            mfe_r: Some(1.5),
            mae_r: Some(-0.5),
            bars_held: Some(3),
            vol_ratio: Some(1.0),
            oi_increase: Some(true),
            trend60_score: Some(3.0),
            atr_percentile: Some(0.4),
            exit_reason: None,
            target_tier: None,
            extended_target: false,
            b_vol_ratio: None,
            a_move_atr: None,
            trigger_lag_bars: None,
            trigger_overshoot_r: None,
            a_move: Some(140.0),
            b_move: Some(60.0),
            a_bars: Some(20),
            b_bars: Some(4),
            retracement: Some(0.5),
            dims: None,
            net_r: None,
            rollover_crossed: false,
            gap_crossed_entry: false,
            gap_crossed_exit: false,
        };
        // 655/656 场景：A 段不同、预警K线仅差 1 根、入场价接近，仍只算首见一笔。
        let rows = vec![
            mk(1, "2026-08-13 13:45", 3950.0, 50.0, Outcome::Loss),
            mk(2, "2026-08-13 14:00", 3955.0, 50.0, Outcome::Win),
        ];
        let stats = aggregate_stats(&rows, GroupBy::ScoreBand);
        assert_eq!(stats.overall.n, 1);
        assert_eq!(stats.overall.avg_r, Some(-1.0));

        // 入场价差超过 0.3R 时不合并。
        let rows = vec![
            mk(1, "2026-08-13 13:45", 3950.0, 50.0, Outcome::Loss),
            mk(2, "2026-08-13 14:00", 3968.0, 50.0, Outcome::Win),
        ];
        let stats = aggregate_stats(&rows, GroupBy::ScoreBand);
        assert_eq!(stats.overall.n, 2);

        // 预警K线相隔 6 根时不合并。
        let rows = vec![
            mk(1, "2026-08-13 13:45", 3950.0, 50.0, Outcome::Loss),
            mk(2, "2026-08-13 15:15", 3950.0, 50.0, Outcome::Win),
        ];
        let stats = aggregate_stats(&rows, GroupBy::ScoreBand);
        assert_eq!(stats.overall.n, 2);

        // 午休跨段按实际 15m 根数计：11:15 到 14:00 实际相隔 3 根，
        // 按自然时间却会算成 11 根并错误拆成两笔。
        let mut bar_index = HashMap::new();
        bar_index.insert(("JD0".to_string(), "2026-08-13 11:15".to_string()), 0usize);
        bar_index.insert(("JD0".to_string(), "2026-08-13 14:00".to_string()), 3usize);
        let rows = vec![
            mk(1, "2026-08-13 11:15", 3950.0, 50.0, Outcome::Loss),
            mk(2, "2026-08-13 14:00", 3950.0, 50.0, Outcome::Win),
        ];
        let stats = aggregate_stats_scoped_with_bar_index(
            &rows,
            GroupBy::ScoreBand,
            StatsScope::All,
            &bar_index,
        );
        assert_eq!(stats.overall.n, 1);
    }

    #[test]
    fn stats_composite_dimensions() {
        let mk =
            |id: i64, symbol: &str, ts: &str, score: f64, vol: f64, atr: Option<f64>| StatRow {
                signal_id: id,
                symbol: symbol.to_string(),
                logic_version: "1".to_string(),
                direction: "up".to_string(),
                level: "fine".to_string(),
                grade: "A级".to_string(),
                score,
                created_at: ts.to_string(),
                warning_ts: Some(ts.to_string()),
                s0_ts: Some("2026-08-03 08:30".to_string()),
                s1_ts: Some("2026-08-03 08:45".to_string()),
                s2_ts: Some(format!("2026-08-03 09:{id:02}")),
                trigger_bar_ts: None,
                entry: None,
                risk: 0.0,
                outcome: Some(Outcome::Win),
                r_multiple: Some(1.0),
                mfe_r: Some(1.5),
                mae_r: Some(-0.5),
                bars_held: Some(3),
                vol_ratio: Some(vol),
                oi_increase: Some(true),
                trend60_score: Some(3.0),
                atr_percentile: atr,
                exit_reason: None,
                target_tier: None,
                extended_target: false,
                b_vol_ratio: None,
                a_move_atr: None,
                trigger_lag_bars: None,
                trigger_overshoot_r: None,
                a_move: None,
                b_move: None,
                a_bars: None,
                b_bars: None,
                retracement: None,
                dims: None,
                net_r: None,
                rollover_crossed: false,
                gap_crossed_entry: false,
                gap_crossed_exit: false,
            };
        let rows = vec![
            mk(1, "RB0", "2026-08-03 09:15", 3.8, 2.0, Some(0.8)),
            mk(2, "RB0", "2026-08-03 10:30", 3.0, 1.0, Some(0.5)),
            mk(3, "RB0", "2026-08-03 09:45", 2.2, 0.5, Some(0.2)),
        ];

        let symbol_hour = aggregate_stats(&rows, GroupBy::SymbolHour);
        assert!(symbol_hour.groups.iter().any(|g| g.key == "RB0 09:00"));
        assert!(symbol_hour.groups.iter().any(|g| g.key == "RB0 10:00"));

        let score_vol = aggregate_stats(&rows, GroupBy::ScoreVol);
        assert!(score_vol
            .groups
            .iter()
            .any(|g| g.key == "3.5-5.0 / 放量确认"));
        assert!(score_vol.groups.iter().any(|g| g.key == "2.5-3.5 / 未放量"));
        assert!(score_vol.groups.iter().any(|g| g.key == "<2.5 / 未放量"));

        let hour_atr = aggregate_stats(&rows, GroupBy::HourAtrBand);
        assert!(hour_atr.groups.iter().any(|g| g.key == "09:00 / 高波动"));
        assert!(hour_atr.groups.iter().any(|g| g.key == "10:00 / 中波动"));
        assert!(hour_atr.groups.iter().any(|g| g.key == "09:00 / 低波动"));
    }

    #[test]
    fn stats_count_gap_crossed() {
        let mk = |id: i64, gap_entry: bool, gap_exit: bool| StatRow {
            signal_id: id,
            symbol: format!("RB{id}"),
            logic_version: "1".to_string(),
            direction: "up".to_string(),
            level: "fine".to_string(),
            grade: "A级".to_string(),
            score: 3.5,
            created_at: "2026-08-03 09:15".to_string(),
            warning_ts: Some("2026-08-03 09:15".to_string()),
            s0_ts: Some("2026-08-03 08:30".to_string()),
            s1_ts: Some("2026-08-03 08:45".to_string()),
            s2_ts: Some(format!("2026-08-03 09:{id:02}")),
            trigger_bar_ts: None,
            entry: None,
            risk: 0.0,
            outcome: Some(Outcome::Win),
            r_multiple: Some(1.0),
            mfe_r: Some(1.5),
            mae_r: Some(-0.5),
            bars_held: Some(3),
            vol_ratio: Some(1.0),
            oi_increase: Some(false),
            trend60_score: Some(3.0),
            atr_percentile: Some(0.4),
            exit_reason: None,
            target_tier: None,
            extended_target: false,
            b_vol_ratio: None,
            a_move_atr: None,
            trigger_lag_bars: None,
            trigger_overshoot_r: None,
            a_move: None,
            b_move: None,
            a_bars: None,
            b_bars: None,
            retracement: None,
            dims: None,
            net_r: None,
            rollover_crossed: false,
            gap_crossed_entry: gap_entry,
            gap_crossed_exit: gap_exit,
        };
        let rows = vec![mk(1, true, false), mk(2, false, true), mk(3, true, true)];
        let stats = aggregate_stats(&rows, GroupBy::ScoreBand);
        assert_eq!(stats.overall.gap_entry, 2);
        assert_eq!(stats.overall.gap_exit, 2);
    }

    fn mk_stat(id: i64, outcome: Option<Outcome>, r: Option<f64>) -> StatRow {
        StatRow {
            signal_id: id,
            symbol: format!("RB{id}"),
            logic_version: "1".to_string(),
            direction: "up".to_string(),
            level: "fine".to_string(),
            grade: "A级".to_string(),
            score: 3.5,
            created_at: "2026-08-03 09:15".to_string(),
            warning_ts: Some("2026-08-03 09:15".to_string()),
            s0_ts: Some("2026-08-03 08:30".to_string()),
            s1_ts: Some("2026-08-03 08:45".to_string()),
            s2_ts: Some(format!("2026-08-03 09:{id:02}")),
            trigger_bar_ts: None,
            entry: None,
            risk: 0.0,
            outcome,
            r_multiple: r,
            mfe_r: Some(1.5),
            mae_r: Some(-0.5),
            bars_held: Some(3),
            vol_ratio: Some(1.0),
            oi_increase: Some(true),
            trend60_score: Some(3.0),
            atr_percentile: Some(0.4),
            exit_reason: Some(ExitReason::Target),
            target_tier: None,
            extended_target: false,
            b_vol_ratio: None,
            a_move_atr: None,
            trigger_lag_bars: None,
            trigger_overshoot_r: None,
            a_move: None,
            b_move: None,
            a_bars: None,
            b_bars: None,
            retracement: None,
            dims: None,
            net_r: None,
            rollover_crossed: false,
            gap_crossed_entry: false,
            gap_crossed_exit: false,
        }
    }

    fn group_keys(stats: &ReviewStats) -> Vec<&str> {
        stats.groups.iter().map(|g| g.key.as_str()).collect()
    }

    #[test]
    fn stats_r_distribution_and_conditional_metrics() {
        let mut rows = vec![
            mk_stat(1, Some(Outcome::Win), Some(2.0)),
            mk_stat(2, Some(Outcome::Win), Some(1.0)),
            mk_stat(3, Some(Outcome::Loss), Some(-1.0)),
            mk_stat(4, Some(Outcome::Loss), Some(-2.0)),
        ];
        rows[0].mfe_r = Some(2.5);
        rows[0].mae_r = Some(-0.5);
        rows[0].net_r = Some(1.9);
        rows[1].mfe_r = Some(1.2);
        rows[1].mae_r = Some(-0.2);
        rows[1].net_r = Some(0.9);
        rows[2].mfe_r = Some(0.8);
        rows[2].mae_r = Some(-1.2);
        rows[2].net_r = Some(-1.1);
        rows[3].mfe_r = Some(0.5);
        rows[3].mae_r = Some(-2.5);
        rows[3].net_r = Some(-2.1);

        let o = &aggregate_stats(&rows, GroupBy::ScoreBand).overall;
        assert_eq!(o.avg_win_r, Some(1.5));
        assert_eq!(o.avg_loss_r, Some(-1.5));
        assert_eq!(o.payoff, Some(1.0));
        assert_eq!(o.profit_factor, Some(1.0));
        assert_eq!(o.r_ge1_rate, Some(0.5));
        assert_eq!(o.r_ge2_rate, Some(0.25));
        assert_eq!(o.mfe_ge1_rate, Some(0.5));
        assert_eq!(o.avg_r_mfe_ge1, Some(1.5));
        assert_eq!(o.mae_le_neg1_rate, Some(0.5));
        assert_eq!(o.avg_r_mae_le_neg1, Some(-1.5));
        assert!((o.avg_net_r.unwrap() + 0.1).abs() < 1e-9);
    }

    #[test]
    fn stats_zero_loss_denominators_stay_null() {
        let rows = vec![
            mk_stat(1, Some(Outcome::Win), Some(1.0)),
            mk_stat(2, Some(Outcome::Win), Some(1.0)),
        ];
        let o = &aggregate_stats(&rows, GroupBy::ScoreBand).overall;
        assert_eq!(o.avg_loss_r, None);
        assert_eq!(o.payoff, None);
        assert_eq!(o.profit_factor, None);
        assert_eq!(o.mae_le_neg1_rate, Some(0.0));
    }

    #[test]
    fn stats_target_hierarchy_conversion() {
        let mut rows = vec![
            mk_stat(1, Some(Outcome::Win), Some(1.6)),
            mk_stat(2, Some(Outcome::Win), Some(1.0)),
            mk_stat(3, Some(Outcome::Loss), Some(-1.0)),
            mk_stat(4, Some(Outcome::Win), Some(1.0)),
            mk_stat(5, Some(Outcome::Loss), Some(-1.0)),
        ];
        rows[0].extended_target = true;
        rows[0].target_tier = Some("tp2".to_string());
        rows[1].extended_target = true;
        rows[1].target_tier = Some("tp1".to_string());
        rows[2].extended_target = true;
        rows[3].target_tier = Some("tp1".to_string());

        let o = &aggregate_stats(&rows, GroupBy::ScoreBand).overall;
        assert_eq!(o.ext_target_n, 3);
        assert_eq!(o.tp1_exits, 2);
        assert_eq!(o.tp2_exits, 1);
        assert!((o.tp2_conversion.unwrap() - 1.0 / 3.0).abs() < 1e-9);
        assert!((o.tp2_of_ext_rate.unwrap() - 1.0 / 3.0).abs() < 1e-9);
    }

    #[test]
    fn stats_new_dimension_bucket_order() {
        let mut vol = (0..6)
            .map(|i| mk_stat(i + 1, Some(Outcome::Win), Some(1.0)))
            .collect::<Vec<_>>();
        for (row, v) in vol.iter_mut().zip([2.5, 1.5, 1.1, 0.9, 0.5, f64::NAN]) {
            row.vol_ratio = if v.is_nan() { None } else { Some(v) };
        }
        assert_eq!(
            group_keys(&aggregate_stats(&vol, GroupBy::VolRatioBand)),
            vec!["≥2.0", "1.3-2.0", "1.0-1.3", "0.8-1.0", "<0.8", "数据缺失"]
        );

        let mut b_vol = (0..5)
            .map(|i| mk_stat(i + 1, Some(Outcome::Win), Some(1.0)))
            .collect::<Vec<_>>();
        for (row, v) in b_vol.iter_mut().zip([1.5, 1.1, 0.9, 0.5, f64::NAN]) {
            row.b_vol_ratio = if v.is_nan() { None } else { Some(v) };
        }
        assert_eq!(
            group_keys(&aggregate_stats(&b_vol, GroupBy::BVolRatioBand)),
            vec!["≥1.3", "1.0-1.3", "0.8-1.0", "<0.8", "数据缺失"]
        );

        let mut retr = (0..6)
            .map(|i| mk_stat(i + 1, Some(Outcome::Win), Some(1.0)))
            .collect::<Vec<_>>();
        for (row, v) in retr.iter_mut().zip([0.7, 0.6, 0.4, 0.25, 0.1, f64::NAN]) {
            row.retracement = if v.is_nan() { None } else { Some(v) };
        }
        assert_eq!(
            group_keys(&aggregate_stats(&retr, GroupBy::RetracementBand)),
            vec![">66%", "50-66%", "33-50%", "20-33%", "<20%", "数据缺失"]
        );

        let mut speed = (0..5)
            .map(|i| mk_stat(i + 1, Some(Outcome::Win), Some(1.0)))
            .collect::<Vec<_>>();
        for (row, v) in speed.iter_mut().zip([1.2, 1.0, 0.6, 0.3, f64::NAN]) {
            row.a_move = Some(10.0);
            row.a_bars = Some(5);
            if v.is_nan() {
                row.b_move = None;
                row.b_bars = None;
            } else {
                row.b_move = Some(10.0 * v);
                row.b_bars = Some(5);
            }
        }
        assert_eq!(
            group_keys(&aggregate_stats(&speed, GroupBy::BaSpeedBand)),
            vec!["≥1.2", "0.8-1.2", "0.5-0.8", "<0.5", "数据缺失"]
        );

        let mut strength = (0..5)
            .map(|i| mk_stat(i + 1, Some(Outcome::Win), Some(1.0)))
            .collect::<Vec<_>>();
        for (row, v) in strength.iter_mut().zip([12.0, 8.0, 4.0, 2.0, f64::NAN]) {
            row.a_move_atr = if v.is_nan() { None } else { Some(v) };
        }
        assert_eq!(
            group_keys(&aggregate_stats(&strength, GroupBy::AStrengthBand)),
            vec!["≥10", "6-10", "3-6", "<3", "数据缺失"]
        );

        let mut lag = (0..5)
            .map(|i| mk_stat(i + 1, Some(Outcome::Win), Some(1.0)))
            .collect::<Vec<_>>();
        for (row, v) in lag.iter_mut().zip([1_i64, 3, 6, 10, i64::MIN]) {
            row.trigger_lag_bars = if v == i64::MIN {
                None
            } else {
                Some(v as usize)
            };
        }
        assert_eq!(
            group_keys(&aggregate_stats(&lag, GroupBy::TriggerLagBand)),
            vec!["1", "2-3", "4-6", "7-12", "数据缺失"]
        );

        let mut overshoot = (0..5)
            .map(|i| mk_stat(i + 1, Some(Outcome::Win), Some(1.0)))
            .collect::<Vec<_>>();
        for (row, v) in overshoot.iter_mut().zip([0.8, 0.4, 0.2, 0.05, f64::NAN]) {
            row.trigger_overshoot_r = if v.is_nan() { None } else { Some(v) };
        }
        assert_eq!(
            group_keys(&aggregate_stats(&overshoot, GroupBy::OvershootBand)),
            vec!["<0.1", "0.1-0.3", "0.3-0.6", "≥0.6", "数据缺失"]
        );

        let mut tier = vec![
            mk_stat(1, Some(Outcome::Win), Some(1.0)),
            mk_stat(2, Some(Outcome::Win), Some(1.0)),
            mk_stat(3, Some(Outcome::Loss), Some(-1.0)),
        ];
        tier[0].target_tier = Some("tp2".to_string());
        tier[1].target_tier = Some("tp1".to_string());
        assert_eq!(
            group_keys(&aggregate_stats(&tier, GroupBy::TpTier)),
            vec!["TP2扩展止盈", "TP1止盈", "其他"]
        );

        let mut gap = vec![
            mk_stat(1, Some(Outcome::Win), Some(1.0)),
            mk_stat(2, Some(Outcome::Win), Some(1.0)),
            mk_stat(3, Some(Outcome::Win), Some(1.0)),
            mk_stat(4, Some(Outcome::Win), Some(1.0)),
        ];
        gap[0].gap_crossed_entry = true;
        gap[0].gap_crossed_exit = true;
        gap[1].gap_crossed_entry = true;
        gap[2].gap_crossed_exit = true;
        assert_eq!(
            group_keys(&aggregate_stats(&gap, GroupBy::GapCombo)),
            vec!["双跳空", "入场跳空", "出场跳空", "无跳空"]
        );

        let mut exit = vec![
            mk_stat(1, Some(Outcome::Win), Some(1.0)),
            mk_stat(2, Some(Outcome::Loss), Some(-1.0)),
            mk_stat(3, Some(Outcome::Loss), Some(-0.5)),
            mk_stat(4, Some(Outcome::Loss), Some(-0.2)),
            mk_stat(5, Some(Outcome::Rollover), None),
            mk_stat(6, Some(Outcome::Open), None),
        ];
        exit[0].exit_reason = Some(ExitReason::Target);
        exit[1].exit_reason = Some(ExitReason::Stop);
        exit[2].exit_reason = Some(ExitReason::NoFollow);
        exit[3].exit_reason = Some(ExitReason::TimeExit);
        exit[4].exit_reason = Some(ExitReason::Rollover);
        exit[5].exit_reason = Some(ExitReason::None);
        assert_eq!(
            group_keys(&aggregate_stats(&exit, GroupBy::ExitReason)),
            vec!["止盈", "止损", "无跟随", "时间退出", "换月", "其他"]
        );

        for (gb, idx) in [
            (GroupBy::DimTrend, 0usize),
            (GroupBy::DimALeg, 1),
            (GroupBy::DimBLeg, 2),
            (GroupBy::DimTrigger, 3),
            (GroupBy::DimRr, 4),
            (GroupBy::DimMomentum, 5),
        ] {
            let mut rows = (0..3)
                .map(|i| mk_stat(i + 1, Some(Outcome::Win), Some(1.0)))
                .collect::<Vec<_>>();
            for (row, v) in rows.iter_mut().zip([3.6, 2.5, 1.0]) {
                let mut dims = [0.0; 6];
                dims[idx] = v;
                row.dims = Some(dims);
            }
            assert_eq!(
                group_keys(&aggregate_stats(&rows, gb)),
                vec!["≥3.5", "2.0-3.5", "<2.0"]
            );
        }
    }

    #[test]
    fn stats_scope_filters_score_bands() {
        let mk = |id: i64, score: f64, outcome: Outcome, r: f64| StatRow {
            signal_id: id,
            symbol: format!("RB{id}"),
            logic_version: "1".to_string(),
            direction: "up".to_string(),
            level: "fine".to_string(),
            grade: "A级".to_string(),
            score,
            created_at: "2026-08-03 09:15".to_string(),
            warning_ts: Some("2026-08-03 09:15".to_string()),
            s0_ts: Some("2026-08-03 08:30".to_string()),
            s1_ts: Some("2026-08-03 08:45".to_string()),
            s2_ts: Some(format!("2026-08-03 09:{id:02}")),
            trigger_bar_ts: None,
            entry: None,
            risk: 0.0,
            outcome: Some(outcome),
            r_multiple: Some(r),
            mfe_r: Some(1.5),
            mae_r: Some(-1.0),
            bars_held: Some(3),
            vol_ratio: Some(1.0),
            oi_increase: Some(false),
            trend60_score: Some(2.0),
            atr_percentile: Some(0.4),
            exit_reason: None,
            target_tier: None,
            extended_target: false,
            b_vol_ratio: None,
            a_move_atr: None,
            trigger_lag_bars: None,
            trigger_overshoot_r: None,
            a_move: None,
            b_move: None,
            a_bars: None,
            b_bars: None,
            retracement: None,
            dims: None,
            net_r: None,
            rollover_crossed: false,
            gap_crossed_entry: false,
            gap_crossed_exit: false,
        };
        let rows = vec![
            mk(1, 1.5, Outcome::Loss, -1.0),
            mk(2, 2.0, Outcome::Win, 1.0),
            mk(3, 2.6, Outcome::Loss, -1.0),
            mk(4, 3.0, Outcome::Win, 2.0),
            mk(5, 3.6, Outcome::Loss, -1.0),
            mk(6, 4.0, Outcome::Win, 1.5),
        ];

        let all = aggregate_stats_scoped(&rows, GroupBy::ScoreBand, StatsScope::All);
        assert_eq!(all.overall.n, 6);
        assert_eq!(all.overall.wins, 3);
        assert_eq!(all.overall.losses, 3);

        let tradable = aggregate_stats_scoped(&rows, GroupBy::ScoreBand, StatsScope::Tradable);
        assert_eq!(tradable.overall.n, 4);
        assert_eq!(tradable.overall.wins, 2);
        assert_eq!(tradable.overall.win_rate, Some(0.5));

        let standard = aggregate_stats_scoped(&rows, GroupBy::ScoreBand, StatsScope::Standard);
        assert_eq!(standard.overall.n, 2);
        assert_eq!(standard.overall.wins, 1);
        assert_eq!(standard.overall.losses, 1);
        assert_eq!(standard.overall.avg_r, Some(0.25)); // (-1.0 + 1.5) / 2

        assert_eq!(StatsScope::parse("all"), StatsScope::All);
        assert_eq!(StatsScope::parse("tradable"), StatsScope::Tradable);
        assert_eq!(StatsScope::parse("standard"), StatsScope::Standard);
        assert_eq!(StatsScope::parse(""), StatsScope::All);
        assert_eq!(StatsScope::parse("unknown"), StatsScope::All);
    }
}
