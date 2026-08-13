//! 信号结局回填与复盘统计。
//!
//! 简化出场规则（SIM_VERSION = 9）：
//! - 入场价 = 信号落库时的 entry（预警K线极值 ± tick），不重算、不回看；
//! - 入场/止损被跳空穿越时按更保守的 current.open 成交，并标记 gap_crossed；
//! - 在 15m 上逐根模拟：先到 stop → −1R；
//! - 第一止盈位 TP1 = 1R（entry ± 1R）；
//! - 触及 TP1 后采用推动止盈：每达到 0.8×2R、0.8×3R、0.8×4R…… 就把该阈值作为新的止盈位，
//!   随后回落触到该止盈位即平仓；未到更高阈值前回落 1R 则按 1R 止盈；
//! - 同一根 bar 双触按止损优先（保守）；
//! - 入场后第 5 根 bar（含入场bar共 6 根）若 MFE < 0.5R → 按该 bar 收盘平仓；
//! - 60 根 bar 内未决 → 按第 60 根收盘平仓（时间退出）；
//! - 预警后 12 根内未触及 entry → no_trigger；
//! - 数据不足 → open / insufficient_data。
//!
//! 首批诊断特征只落库、不改评分：vol_ratio（触发量能）、oi_increase（增仓）、
//! trend60_score（60m 连续趋势分）、b_vol_ratio（b段相对a段量能）、
//! a_move_atr（a段强度）、trigger_lag_bars（预警到触发延迟）、
//! trigger_overshoot_r（触发K线追价深度）、target_tier（止盈层级），
//! 供复盘页按特征分组统计，为 v2 权重校准提供证据。

use std::collections::BTreeMap;
use std::collections::HashMap;

use chrono::{Datelike, Timelike};
use serde::Serialize;

use crate::analyze::indicators;
use crate::analyze::model::{Bar, Dir, ATR_PERIOD};

pub const SIM_VERSION: i64 = 9;
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
/// 入场后至少几根 bar 才允许判定为 open（更少视为数据不足）
pub const MIN_BARS_FOR_SETTLED: usize = 3;
/// 量能确认窗口：触发 bar 前 20 根 15m 均量
pub const VOL_AVG_WINDOW: usize = 20;
/// 量能确认阈值：触发 bar 成交量 ≥ 前 20 根均量的 2.0 倍（复盘分桶显示 ≥2.0 才有明显区分度）
pub const VOL_CONFIRM_RATIO: f64 = 2.0;
/// ATR 分位窗口：触发 bar 的 ATR20 与之前 60 根 15m bar 比较
pub const ATR_PERCENTILE_WINDOW: usize = 60;

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

/// 模拟所需的信号快照（由 service 从 signals 表 + detail JSON 组装）。
#[derive(Debug, Clone)]
pub struct SignalInput {
    pub symbol: String,
    pub direction: String,
    pub level: String,
    pub entry: f64,
    pub stop: f64,
    pub target: f64,
    pub risk: f64,
    pub created_at: String,
    pub warning_ts: Option<String>,
    /// 已落盘信号明确记录触发K线时，直接用该根K线做入场模拟。
    pub trigger_ts: Option<String>,
    pub s0_ts: Option<String>,
    pub s1_ts: Option<String>,
    pub s2_ts: Option<String>,
    pub a_move: Option<f64>,
}

/// 单条信号的结局 + 特征（对应 signal_outcomes 一行）。
#[derive(Debug, Clone)]
pub struct SignalAnnotation {
    pub sim_version: i64,
    pub outcome: Outcome,
    pub exit_reason: ExitReason,
    /// 模拟回放找到的入场触达时间（入场价被触及的那根 15m bar）
    pub entry_ts: Option<String>,
    pub exit_ts: Option<String>,
    pub exit_price: Option<f64>,
    pub r_multiple: Option<f64>,
    pub mfe_r: Option<f64>,
    pub mae_r: Option<f64>,
    pub bars_held: Option<usize>,
    pub vol_ratio: Option<f64>,
    pub oi_increase: Option<bool>,
    pub trend60_score: Option<f64>,
    pub atr_percentile: Option<f64>,
    /// 止盈时记录 tp1 / tp2，其他出场为空
    pub target_tier: Option<String>,
    /// b段均量 / a段均量（15m）
    pub b_vol_ratio: Option<f64>,
    /// a_move / 触发bar ATR20
    pub a_move_atr: Option<f64>,
    /// 预警K线到触发K线的根数差
    pub trigger_lag_bars: Option<usize>,
    /// 触发K线超出入场价的深度（按R归一化）
    pub trigger_overshoot_r: Option<f64>,
    pub rollover_crossed: bool,
    pub gap_crossed_entry: bool,
    pub gap_crossed_exit: bool,
}

fn dt_minute(b: &Bar) -> (i32, i32, i32, i32, i32) {
    (b.dt.year, b.dt.month, b.dt.day, b.dt.hour, b.dt.minute)
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

fn signal_dir(direction: &str) -> Dir {
    if direction == "down" {
        Dir::Down
    } else {
        Dir::Up
    }
}

/// 推动止盈第 n 级对应的 R 倍数：第 1 级 = 1R，之后为 0.8×nR。
fn trail_r_for(grade: usize) -> f64 {
    if grade == 1 {
        TRAIL_START_R
    } else {
        TRAIL_STEP_R * grade as f64
    }
}

fn empty_annotation(outcome: Outcome, trend60_score: Option<f64>) -> SignalAnnotation {
    SignalAnnotation {
        sim_version: SIM_VERSION,
        outcome,
        exit_reason: ExitReason::None,
        entry_ts: None,
        exit_ts: None,
        exit_price: None,
        r_multiple: None,
        mfe_r: None,
        mae_r: None,
        bars_held: None,
        vol_ratio: None,
        oi_increase: None,
        trend60_score,
        atr_percentile: None,
        target_tier: None,
        b_vol_ratio: None,
        a_move_atr: None,
        trigger_lag_bars: None,
        trigger_overshoot_r: None,
        rollover_crossed: false,
        gap_crossed_entry: false,
        gap_crossed_exit: false,
    }
}

fn rollover_annotation(
    trend60_score: Option<f64>,
    entry_ts: Option<String>,
    exit_ts: Option<String>,
) -> SignalAnnotation {
    SignalAnnotation {
        sim_version: SIM_VERSION,
        outcome: Outcome::Rollover,
        exit_reason: ExitReason::Rollover,
        entry_ts,
        exit_ts,
        exit_price: None,
        r_multiple: None,
        mfe_r: None,
        mae_r: None,
        bars_held: None,
        vol_ratio: None,
        oi_increase: None,
        trend60_score,
        atr_percentile: None,
        target_tier: None,
        b_vol_ratio: None,
        a_move_atr: None,
        trigger_lag_bars: None,
        trigger_overshoot_r: None,
        rollover_crossed: true,
        gap_crossed_entry: false,
        gap_crossed_exit: false,
    }
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

/// 闭区间 `[start, end]` 内成交量 > 0 的 K 线均量；区间为空或全为 0 时返回 None。
fn avg_volume_between(bars: &[Bar], start: usize, end: usize) -> Option<f64> {
    if start > end || end >= bars.len() {
        return None;
    }
    let mut sum = 0.0;
    let mut count = 0usize;
    for b in &bars[start..=end] {
        if b.volume > 0.0 {
            sum += b.volume;
            count += 1;
        }
    }
    if count == 0 {
        return None;
    }
    Some(sum / count as f64)
}

/// b段均量 / a段均量：a段取 s0 后到 s1，b段取 s1 后到 s2。
fn b_vol_ratio_at(
    bars: &[Bar],
    s0_ts: Option<&str>,
    s1_ts: Option<&str>,
    s2_ts: Option<&str>,
) -> Option<f64> {
    let (s0, s1, s2) = (s0_ts?, s1_ts?, s2_ts?);
    let s0 = parse_minute(s0)?;
    let s1 = parse_minute(s1)?;
    let s2 = parse_minute(s2)?;
    let i0 = bars.iter().position(|b| dt_minute(b) >= s0)?;
    let i1 = bars.iter().position(|b| dt_minute(b) >= s1)?;
    let i2 = bars.iter().position(|b| dt_minute(b) >= s2)?;
    let a_avg = avg_volume_between(bars, i0, i1)?;
    let b_avg = avg_volume_between(bars, i1, i2)?;
    if a_avg <= 0.0 {
        return None;
    }
    Some(b_avg / a_avg)
}

/// 触发 bar 持仓量较前一根增加；持仓量为 0（数据缺失）时返回 None。
fn oi_increase_at(bars: &[Bar], ec: usize) -> Option<bool> {
    let cur = bars.get(ec)?.hold;
    let prev = bars.get(ec.checked_sub(1)?)?.hold;
    if cur <= 0.0 || prev <= 0.0 {
        return None;
    }
    Some(cur > prev)
}

/// 触发 bar 的 ATR20 在当前品种近 60 根 15m bar 中的分位（0~1）。
fn atr_percentile_at(atr20: &[Option<f64>], ec: usize) -> Option<f64> {
    let cur = atr20.get(ec).copied().flatten()?;
    let lo = ec.saturating_sub(ATR_PERCENTILE_WINDOW);
    let mut lower = 0usize;
    let mut total = 0usize;
    for i in lo..ec {
        if let Some(v) = atr20[i] {
            total += 1;
            if v <= cur {
                lower += 1;
            }
        }
    }
    if total == 0 {
        return None;
    }
    Some(lower as f64 / total as f64)
}

/// 60m 连续趋势分 0~5：按信号时刻截断的 60m 序列计算。
/// 方向基础分 + ATR 归一化离均线距离 + 斜率强度 + HH/HL 摆动结构。
fn trend60_score_at(bars60: &[Bar], created_at: &str, dir: Dir) -> Option<f64> {
    let end = parse_minute(created_at)?;
    let slice: Vec<&Bar> = bars60.iter().filter(|b| dt_minute(b) <= end).collect();
    if slice.len() < ATR_PERIOD + 1 {
        return None;
    }
    let owned: Vec<Bar> = slice.into_iter().cloned().collect();
    let trend = indicators::analyze_60m(&owned);
    let atr = indicators::atr(&owned, ATR_PERIOD)
        .last()
        .copied()
        .flatten()
        .unwrap_or(1.0)
        .max(1e-9);

    let aligned = trend.aligned_with(dir);
    let opposite = trend.opposite_to(dir);
    let mut s = if aligned {
        2.75
    } else if opposite {
        1.25
    } else {
        2.0
    };

    let dist = (trend.price_vs_ma / atr).clamp(-2.0, 2.0) * 0.5;
    s += match dir {
        Dir::Up => dist,
        Dir::Down => -dist,
    };

    let slope = (trend.slope / atr).clamp(-1.0, 1.0) * 0.5;
    s += match dir {
        Dir::Up => slope,
        Dir::Down => -slope,
    };

    let bonus = |cond: bool, mult: f64| if cond { 0.25 * mult } else { 0.0 };
    match dir {
        Dir::Up => {
            s += bonus(trend.higher_highs, 1.0);
            s += bonus(trend.higher_lows, 1.0);
            s += bonus(trend.lower_highs, -1.0);
            s += bonus(trend.lower_lows, -1.0);
        }
        Dir::Down => {
            s += bonus(trend.lower_highs, 1.0);
            s += bonus(trend.lower_lows, 1.0);
            s += bonus(trend.higher_highs, -1.0);
            s += bonus(trend.higher_lows, -1.0);
        }
    }

    Some(s.clamp(0.0, 5.0))
}

/// 对一条信号做结局模拟 + 特征计算。
/// 返回 None 表示信号数据不可用（entry/risk 异常），不落库。
pub fn annotate(input: &SignalInput, bars15: &[Bar], bars60: &[Bar]) -> Option<SignalAnnotation> {
    let risk = input.risk;
    if risk <= 0.0 || input.entry <= 0.0 {
        return None;
    }
    let dir = signal_dir(&input.direction);
    let warning_ts = input
        .warning_ts
        .as_deref()
        .filter(|s| !s.is_empty())
        .unwrap_or(&input.created_at);
    let warning_minute = parse_minute(warning_ts)?;
    let start = bars15.iter().position(|b| dt_minute(b) >= warning_minute);
    let trend60_score = trend60_score_at(bars60, &input.created_at, dir);
    let Some(start) = start else {
        return Some(empty_annotation(Outcome::InsufficientData, trend60_score));
    };
    let atr15 = indicators::atr(bars15, ATR_PERIOD);
    let mut ec = None;
    if let Some(trigger_ts) = input.trigger_ts.as_deref() {
        if let Some(t_minute) = parse_minute(trigger_ts) {
            if let Some(j) = (start + 1..bars15.len()).find(|&j| dt_minute(&bars15[j]) >= t_minute)
            {
                if dt_minute(&bars15[j]) == t_minute {
                    ec = Some(j);
                }
            }
        }
    }
    if let Some(ec_known) = ec {
        if let Some(rb) = (start + 1..=ec_known).find(|&j| bars15[j].rollover) {
            return Some(rollover_annotation(
                trend60_score,
                None,
                Some(bars15[rb].dt.to_string()),
            ));
        }
    } else {
        // 预警后 12 根内寻找入场触发（做多突破 entry、做空跌破 entry）
        let scan_end = (start + 1 + PENDING_BARS).min(bars15.len());
        for j in start + 1..scan_end {
            if bars15[j].rollover {
                return Some(SignalAnnotation {
                    sim_version: SIM_VERSION,
                    outcome: Outcome::Rollover,
                    exit_reason: ExitReason::Rollover,
                    entry_ts: None,
                    exit_ts: Some(bars15[j].dt.to_string()),
                    exit_price: None,
                    r_multiple: None,
                    mfe_r: None,
                    mae_r: None,
                    bars_held: None,
                    vol_ratio: vol_ratio_at(bars15, j),
                    oi_increase: oi_increase_at(bars15, j),
                    trend60_score,
                    atr_percentile: atr_percentile_at(&atr15, j),
                    target_tier: None,
                    b_vol_ratio: None,
                    a_move_atr: None,
                    trigger_lag_bars: None,
                    trigger_overshoot_r: None,
                    rollover_crossed: true,
                    gap_crossed_entry: false,
                    gap_crossed_exit: false,
                });
            }
            let hit = match dir {
                Dir::Up => bars15[j].high >= input.entry,
                Dir::Down => bars15[j].low <= input.entry,
            };
            if hit {
                ec = Some(j);
                break;
            }
        }
    }

    let Some(ec) = ec else {
        return Some(if bars15.len() - 1 >= start + PENDING_BARS {
            empty_annotation(Outcome::NoTrigger, trend60_score)
        } else {
            empty_annotation(Outcome::InsufficientData, trend60_score)
        });
    };
    let vol_ratio = vol_ratio_at(bars15, ec);
    let oi_increase = oi_increase_at(bars15, ec);
    let atr_percentile = atr_percentile_at(&atr15, ec);
    let b_vol_ratio = b_vol_ratio_at(
        bars15,
        input.s0_ts.as_deref(),
        input.s1_ts.as_deref(),
        input.s2_ts.as_deref(),
    );
    let a_move_atr = input.a_move.and_then(|a| {
        atr15
            .get(ec)
            .copied()
            .flatten()
            .filter(|atr| *atr > 0.0)
            .map(|atr| a / atr)
    });
    let trigger_lag_bars = Some(ec.saturating_sub(start));
    let trigger_overshoot_r = Some(match dir {
        Dir::Up => (bars15[ec].high - input.entry) / risk,
        Dir::Down => (input.entry - bars15[ec].low) / risk,
    });
    let mut entry_fill = input.entry;
    let mut gap_crossed_entry = false;
    let mut gap_crossed_exit = false;
    if ec > 0 && !bars15[ec - 1].rollover {
        let prev_close = bars15[ec - 1].close;
        let cur_open = bars15[ec].open;
        let crossed = match dir {
            Dir::Up => prev_close < input.entry && cur_open > input.entry,
            Dir::Down => prev_close > input.entry && cur_open < input.entry,
        };
        if crossed {
            entry_fill = cur_open;
            gap_crossed_entry = true;
        }
    }

    // 第一止盈位 TP1 = 1R；此后按 0.8×2R、0.8×3R…… 逐级推动止盈位
    let base_tp = match dir {
        Dir::Up => input.entry + risk,
        Dir::Down => input.entry - risk,
    };

    let mut mfe = 0.0_f64;
    let mut mae = 0.0_f64;
    let mut trail_grade: Option<usize> = None;
    let mut target_tier: Option<&'static str> = None;
    let mut result: Option<(ExitReason, f64, usize, usize)> = None;

    for i in ec..bars15.len() {
        let bar = &bars15[i];
        if bar.rollover {
            return Some(SignalAnnotation {
                sim_version: SIM_VERSION,
                outcome: Outcome::Rollover,
                exit_reason: ExitReason::Rollover,
                entry_ts: Some(bars15[ec].dt.to_string()),
                exit_ts: Some(bar.dt.to_string()),
                exit_price: None,
                r_multiple: None,
                mfe_r: None,
                mae_r: None,
                bars_held: None,
                vol_ratio,
                oi_increase,
                trend60_score,
                atr_percentile,
                target_tier: None,
                b_vol_ratio,
                a_move_atr,
                trigger_lag_bars,
                trigger_overshoot_r,
                rollover_crossed: true,
                gap_crossed_entry: false,
                gap_crossed_exit: false,
            });
        }
        let held = i - ec + 1;
        let mfe_contrib = match dir {
            Dir::Up => (bar.high - entry_fill) / risk,
            Dir::Down => (entry_fill - bar.low) / risk,
        };
        let mae_contrib = match dir {
            Dir::Up => (bar.low - entry_fill) / risk,
            Dir::Down => (entry_fill - bar.high) / risk,
        };
        mfe = mfe.max(mfe_contrib);
        mae = mae.min(mae_contrib);

        let stop_hit = match dir {
            Dir::Up => bar.low <= input.stop,
            Dir::Down => bar.high >= input.stop,
        };
        // 同一根 bar 双触按止损优先（保守）
        if stop_hit {
            let stop_gap = i > 0
                && !bars15[i - 1].rollover
                && match dir {
                    Dir::Up => bars15[i - 1].close > input.stop && bar.open < input.stop,
                    Dir::Down => bars15[i - 1].close < input.stop && bar.open > input.stop,
                };
            if stop_gap {
                gap_crossed_exit = true;
            }
            let exit_price = if stop_gap { bar.open } else { input.stop };
            result = Some((ExitReason::Stop, exit_price, i, held));
            break;
        }

        let reached_tp1 = match dir {
            Dir::Up => bar.high >= base_tp,
            Dir::Down => bar.low <= base_tp,
        };
        if trail_grade.is_none() && reached_tp1 {
            trail_grade = Some(1);
            target_tier = Some("tp1");
        }
        if let Some(mut grade) = trail_grade {
            // 单根 bar 可一次跨过多个档位：一直推进到该 bar 触及的最高档
            loop {
                let next_grade = grade + 1;
                let next_r = trail_r_for(next_grade);
                let next_price = match dir {
                    Dir::Up => input.entry + next_r * risk,
                    Dir::Down => input.entry - next_r * risk,
                };
                let next_hit = match dir {
                    Dir::Up => bar.high >= next_price,
                    Dir::Down => bar.low <= next_price,
                };
                if !next_hit {
                    break;
                }
                grade = next_grade;
                target_tier = Some("tp2");
            }
            trail_grade = Some(grade);
            let trail_r = trail_r_for(grade);
            let trail_price = match dir {
                Dir::Up => input.entry + trail_r * risk,
                Dir::Down => input.entry - trail_r * risk,
            };
            let fell_back = match dir {
                Dir::Up => bar.low <= trail_price,
                Dir::Down => bar.high >= trail_price,
            };
            if fell_back {
                result = Some((ExitReason::Target, trail_price, i, held));
                break;
            }
        }
        if i == ec + NO_FOLLOW_BAR && mfe < NO_FOLLOW_MFE_R {
            result = Some((ExitReason::NoFollow, bar.close, i, held));
            break;
        }
        if held >= TIME_HORIZON_BARS {
            result = Some((ExitReason::TimeExit, bar.close, i, held));
            break;
        }
    }

    let (outcome, exit_reason, exit_price, exit_ts, r_multiple, bars_held) = match result {
        Some((reason, price, i, held)) => {
            let r = match dir {
                Dir::Up => (price - entry_fill) / risk,
                Dir::Down => (entry_fill - price) / risk,
            };
            let outcome = if r > 0.0 { Outcome::Win } else { Outcome::Loss };
            (
                outcome,
                reason,
                Some(price),
                Some(bars15[i].dt.to_string()),
                Some(r),
                Some(held),
            )
        }
        None => {
            if bars15.len() - ec < MIN_BARS_FOR_SETTLED {
                (
                    Outcome::InsufficientData,
                    ExitReason::None,
                    None,
                    None,
                    None,
                    None,
                )
            } else {
                (Outcome::Open, ExitReason::None, None, None, None, None)
            }
        }
    };

    Some(SignalAnnotation {
        sim_version: SIM_VERSION,
        outcome,
        exit_reason,
        entry_ts: Some(bars15[ec].dt.to_string()),
        exit_ts,
        exit_price,
        r_multiple,
        mfe_r: Some(mfe),
        mae_r: Some(mae),
        bars_held,
        vol_ratio,
        oi_increase,
        trend60_score,
        atr_percentile,
        target_tier: target_tier.map(str::to_string),
        b_vol_ratio,
        a_move_atr,
        trigger_lag_bars,
        trigger_overshoot_r,
        rollover_crossed: false,
        gap_crossed_entry,
        gap_crossed_exit,
    })
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
    /// 分析版本：1 = 原逻辑，2 = 严格N字 + 箱体；旧记录默认 1。
    pub logic_version: String,
    pub direction: String,
    pub level: String,
    pub grade: String,
    pub score: f64,
    pub created_at: String,
    pub warning_ts: Option<String>,
    pub s1_ts: Option<String>,
    pub s2_ts: Option<String>,
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

/// 结构键去重：同一演变结构跨扫描的重复信号只保留首条（min signal_id）。
fn dedup_first_seen(rows: &[StatRow]) -> Vec<&StatRow> {
    let mut seen: HashMap<String, &StatRow> = HashMap::new();
    for r in rows {
        let key = match (&r.s1_ts, &r.s2_ts) {
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
        };
        seen.entry(key)
            .and_modify(|cur| {
                if r.signal_id < cur.signal_id {
                    *cur = r;
                }
            })
            .or_insert(r);
    }
    let mut out: Vec<&StatRow> = seen.into_values().collect();
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
    let dedup: Vec<&StatRow> = dedup_first_seen(rows)
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
    use crate::analyze::model::DT;

    fn bar(open: f64, high: f64, low: f64, close: f64) -> Bar {
        Bar {
            dt: DT {
                year: 2026,
                month: 8,
                day: 3,
                hour: 9,
                minute: 15,
            },
            open,
            high,
            low,
            close,
            volume: 100.0,
            hold: 1000.0,
            rollover: false,
        }
    }

    fn bar_seq(n: usize) -> Vec<Bar> {
        let mut out = Vec::new();
        for i in 0..n {
            let b = bar(100.0, 100.5, 99.5, 100.0);
            let mut b = b;
            b.dt.minute = 15 + i as i32 * 15;
            out.push(b);
        }
        out
    }

    fn input() -> SignalInput {
        SignalInput {
            symbol: "RB0".to_string(),
            direction: "up".to_string(),
            level: "fine".to_string(),
            entry: 101.0,
            stop: 99.0,
            target: 105.0,
            risk: 2.0,
            created_at: "2026-08-03 09:15".to_string(),
            warning_ts: Some("2026-08-03 09:15".to_string()),
            s0_ts: Some("2026-08-03 09:15".to_string()),
            s1_ts: Some("2026-08-03 08:45".to_string()),
            s2_ts: Some("2026-08-03 09:15".to_string()),
            a_move: Some(4.0),
            trigger_ts: None,
        }
    }

    /// 构造带 dt 序列的 bar：起点 09:15，每根 +15 分钟。
    fn timed_bars(specs: &[(f64, f64, f64, f64)]) -> Vec<Bar> {
        specs
            .iter()
            .enumerate()
            .map(|(i, &(o, h, l, c))| Bar {
                dt: DT {
                    year: 2026,
                    month: 8,
                    day: 3,
                    hour: 9,
                    minute: 15 + i as i32 * 15,
                },
                open: o,
                high: h,
                low: l,
                close: c,
                volume: 100.0,
                hold: 1000.0,
                rollover: false,
            })
            .collect()
    }

    /// 从指定小时/分钟开始生成 15m K线序列，便于覆盖 ATR20 所需的较长窗口。
    fn timed_bars_at(hour: i32, minute: i32, specs: &[(f64, f64, f64, f64)]) -> Vec<Bar> {
        specs
            .iter()
            .enumerate()
            .map(|(i, &(o, h, l, c))| {
                let m = minute + i as i32 * 15;
                Bar {
                    dt: DT {
                        year: 2026,
                        month: 8,
                        day: 3,
                        hour: hour + m / 60,
                        minute: m % 60,
                    },
                    open: o,
                    high: h,
                    low: l,
                    close: c,
                    volume: 100.0,
                    hold: 1000.0,
                    rollover: false,
                }
            })
            .collect()
    }

    #[test]
    fn long_1r_take_profit_instead_of_target() {
        // 落库目标位是 105，但价格只到 103.5；1R 止盈位是 103，仍应按 +1R 平仓
        let bars = timed_bars(&[
            (100.0, 100.0, 100.0, 100.0), // warning bar
            (100.0, 102.0, 100.0, 101.0), // entry-cross
            (101.0, 103.5, 100.5, 103.0), // 1R (103) hit, 原目标 105 未到
        ]);
        let ann = annotate(&input(), &bars, &[]).unwrap();
        assert_eq!(ann.outcome, Outcome::Win);
        assert_eq!(ann.exit_reason, ExitReason::Target);
        assert_eq!(ann.r_multiple, Some(1.0));
        assert_eq!(ann.exit_price, Some(103.0));
        assert_eq!(ann.bars_held, Some(2));
        assert_eq!(ann.mae_r, Some(-0.5));
        // 入场触达时间 = 入场bar（09:30）的时间戳
        assert_eq!(ann.entry_ts.as_deref(), Some("2026-08-03 09:30"));
    }

    #[test]
    fn entry_gap_fills_at_open_and_marks() {
        // 前一根 close=100，当前 open=102 跳过 entry=101：按 open 成交并标记缺口。
        let bars = timed_bars(&[
            (100.0, 100.0, 100.0, 100.0), // warning
            (102.0, 102.5, 101.8, 102.2), // 跳空越过 entry
            (102.2, 104.0, 102.0, 103.5), // 触及 TP1=103 后回落平仓
        ]);
        let ann = annotate(&input(), &bars, &[]).unwrap();
        assert!(ann.gap_crossed_entry);
        assert!(!ann.gap_crossed_exit);
        assert_eq!(ann.exit_reason, ExitReason::Target);
        assert!((ann.exit_price.unwrap() - 103.0).abs() < 1e-9);
        assert!((ann.r_multiple.unwrap() - 0.5).abs() < 1e-9);
    }

    #[test]
    fn long_extends_to_second_target() {
        // 触及 1R 后推动止盈：0.8×2R = 1.6R = 104.2，回落触到该位止盈
        let bars = timed_bars(&[
            (100.0, 100.0, 100.0, 100.0), // warning bar
            (100.0, 102.0, 100.0, 101.0), // entry-cross
            (101.0, 103.2, 103.1, 103.1), // 触及 1R(103) 未回落
            (103.1, 104.5, 103.2, 104.4), // 触及 1.6R(104.2) 并回落，按该位止盈
        ]);
        let ann = annotate(&input(), &bars, &[]).unwrap();
        assert_eq!(ann.outcome, Outcome::Win);
        assert_eq!(ann.exit_reason, ExitReason::Target);
        assert!((ann.r_multiple.unwrap() - 1.6).abs() < 1e-9);
        assert!((ann.exit_price.unwrap() - 104.2).abs() < 1e-9);
        assert_eq!(ann.bars_held, Some(3));
    }

    #[test]
    fn trailing_profit_locks_1r_then_moves_to_1_6r() {
        let bars = timed_bars(&[
            (100.0, 100.0, 100.0, 100.0), // warning bar
            (100.0, 102.0, 100.0, 101.0), // entry-cross
            (101.0, 103.5, 103.1, 103.2), // 触及 1R(103) 未回落
            (103.2, 104.5, 104.3, 104.4), // 触及 1.6R(104.2) 未回落
            (104.4, 104.5, 103.8, 103.9), // 回落到 1.6R 止盈
        ]);
        let ann = annotate(&input(), &bars, &[]).unwrap();
        assert_eq!(ann.exit_reason, ExitReason::Target);
        assert!((ann.r_multiple.unwrap() - 1.6).abs() < 1e-9);
        assert!((ann.exit_price.unwrap() - 104.2).abs() < 1e-9);
        assert_eq!(ann.target_tier.as_deref(), Some("tp2"));
        assert_eq!(ann.bars_held, Some(4));
    }

    #[test]
    fn trailing_profit_advances_to_2_4r() {
        let bars = timed_bars(&[
            (100.0, 100.0, 100.0, 100.0),  // warning bar
            (100.0, 102.0, 100.0, 101.0),  // entry-cross
            (101.0, 103.5, 103.2, 103.3),  // 触及 1R(103)
            (103.3, 104.5, 104.3, 104.4),  // 触及 1.6R(104.2)
            (104.4, 106.0, 105.9, 105.95), // 触及 2.4R(105.8) 未回落
            (105.9, 106.1, 105.6, 105.7),  // 回落到 2.4R 止盈
        ]);
        let ann = annotate(&input(), &bars, &[]).unwrap();
        assert_eq!(ann.exit_reason, ExitReason::Target);
        assert!((ann.r_multiple.unwrap() - 2.4).abs() < 1e-9);
        assert!((ann.exit_price.unwrap() - 105.8).abs() < 1e-9);
        assert_eq!(ann.target_tier.as_deref(), Some("tp2"));
        assert_eq!(ann.bars_held, Some(5));
    }

    #[test]
    fn trailing_profit_skips_levels_on_one_bar() {
        // 单根 bar 从下方跳空直触 1R/1.6R/2.4R 并回落到 2.4R(105.8)：应直接锁到 2.4R
        let bars = timed_bars(&[
            (100.0, 100.0, 100.0, 100.0), // warning bar
            (100.0, 102.0, 100.0, 101.0), // entry-cross
            (105.5, 106.0, 105.5, 105.8), // 触及 1.6R/2.4R，回落到 2.4R 止盈
        ]);
        let ann = annotate(&input(), &bars, &[]).unwrap();
        assert_eq!(ann.exit_reason, ExitReason::Target);
        assert!((ann.r_multiple.unwrap() - 2.4).abs() < 1e-9);
        assert!((ann.exit_price.unwrap() - 105.8).abs() < 1e-9);
        assert_eq!(ann.target_tier.as_deref(), Some("tp2"));
        assert_eq!(ann.bars_held, Some(2));
    }

    #[test]
    fn sub_1r_target_holds_to_1r() {
        // 目标位 102 → 目标R=0.5 ≤ 1：不按目标位止盈，持有到 1R(103) 才平
        let mut inp = input();
        inp.target = 102.0;
        let bars = timed_bars(&[
            (100.0, 100.0, 100.0, 100.0), // warning bar
            (100.0, 102.0, 100.0, 101.0), // entry-cross
            (101.0, 103.5, 101.5, 103.0), // 越过目标位 102，达到 1R 止盈
        ]);
        let ann = annotate(&inp, &bars, &[]).unwrap();
        assert_eq!(ann.outcome, Outcome::Win);
        assert_eq!(ann.exit_reason, ExitReason::Target);
        assert_eq!(ann.r_multiple, Some(1.0));
        assert_eq!(ann.exit_price, Some(103.0));
    }

    #[test]
    fn long_stop_first_loses() {
        let bars = timed_bars(&[
            (100.0, 100.0, 100.0, 100.0),
            (100.0, 102.0, 100.0, 101.0),
            (101.0, 101.5, 98.0, 99.0), // stop hit
        ]);
        let ann = annotate(&input(), &bars, &[]).unwrap();
        assert_eq!(ann.outcome, Outcome::Loss);
        assert_eq!(ann.exit_reason, ExitReason::Stop);
        assert_eq!(ann.r_multiple, Some(-1.0));
    }

    #[test]
    fn stop_gap_fills_at_open_and_marks() {
        // 前一根 close=101.5，当前 open=98.5 跳过 stop=99：按 open 成交并标记止损缺口。
        let bars = timed_bars(&[
            (100.0, 100.0, 100.0, 100.0), // warning
            (100.0, 102.0, 99.5, 101.5),  // entry，无入场缺口
            (98.5, 99.0, 98.0, 98.2),     // 跳空跌破 stop
        ]);
        let ann = annotate(&input(), &bars, &[]).unwrap();
        assert!(!ann.gap_crossed_entry);
        assert!(ann.gap_crossed_exit);
        assert_eq!(ann.outcome, Outcome::Loss);
        assert_eq!(ann.exit_reason, ExitReason::Stop);
        assert!((ann.exit_price.unwrap() - 98.5).abs() < 1e-9);
        assert!((ann.r_multiple.unwrap() + 1.25).abs() < 1e-9);
    }

    #[test]
    fn same_bar_both_hit_stop_wins_conservative() {
        let bars = timed_bars(&[
            (100.0, 100.0, 100.0, 100.0),
            (100.0, 102.0, 100.0, 101.0),
            (101.0, 106.0, 98.0, 100.0), // both stop & target in one bar
        ]);
        let ann = annotate(&input(), &bars, &[]).unwrap();
        assert_eq!(ann.outcome, Outcome::Loss);
        assert_eq!(ann.exit_reason, ExitReason::Stop);
    }

    #[test]
    fn no_follow_exit_at_bar5_when_mfe_low() {
        // 入场后 5 根 bar 都小幅度波动，MFE < 0.5R(1点)
        let mut specs = vec![(100.0, 100.0, 100.0, 100.0)];
        specs.push((100.0, 101.0, 100.0, 100.5)); // entry-cross
        for _ in 0..5 {
            specs.push((100.5, 100.5, 100.0, 100.0));
        }
        let bars = timed_bars(&specs);
        let ann = annotate(&input(), &bars, &[]).unwrap();
        assert_eq!(ann.outcome, Outcome::Loss); // close 100.0 < entry 101.0
        assert_eq!(ann.exit_reason, ExitReason::NoFollow);
        assert_eq!(ann.bars_held, Some(6));
        assert_eq!(ann.r_multiple, Some(-0.5));
    }

    #[test]
    fn no_follow_skipped_when_mfe_enough() {
        let mut specs = vec![(100.0, 100.0, 100.0, 100.0)];
        specs.push((100.0, 101.0, 100.0, 100.5)); // entry-cross
        for _ in 0..5 {
            specs.push((100.5, 102.0, 100.0, 100.5)); // mfe = 1.0R >= 0.5R
        }
        let bars = timed_bars(&specs);
        let ann = annotate(&input(), &bars, &[]).unwrap();
        assert_eq!(ann.outcome, Outcome::Open);
        assert_eq!(ann.exit_reason, ExitReason::None);
    }

    #[test]
    fn time_exit_after_60_bars() {
        let mut specs = vec![(100.0, 100.0, 100.0, 100.0)];
        specs.push((100.0, 101.0, 100.0, 100.5));
        // 59 根：第 5 根 mfe 足够避免 no_follow，且不触 target/stop
        for _ in 0..59 {
            specs.push((100.5, 102.0, 100.0, 100.8));
        }
        let bars = timed_bars(&specs);
        let ann = annotate(&input(), &bars, &[]).unwrap();
        assert_eq!(ann.exit_reason, ExitReason::TimeExit);
        assert_eq!(ann.bars_held, Some(60));
        assert_eq!(ann.outcome, Outcome::Loss); // close 100.8 < entry 101.0
    }

    #[test]
    fn no_trigger_within_12_bars() {
        let mut specs = vec![(100.0, 100.0, 100.0, 100.0)];
        for _ in 0..13 {
            specs.push((100.0, 100.5, 99.5, 100.0)); // never reach 101.0
        }
        let bars = timed_bars(&specs);
        let ann = annotate(&input(), &bars, &[]).unwrap();
        assert_eq!(ann.outcome, Outcome::NoTrigger);
        assert_eq!(ann.entry_ts, None);
        assert_eq!(ann.target_tier, None);
        assert_eq!(ann.b_vol_ratio, None);
        assert_eq!(ann.a_move_atr, None);
        assert_eq!(ann.trigger_lag_bars, None);
        assert_eq!(ann.trigger_overshoot_r, None);
    }

    #[test]
    fn insufficient_data_when_bars_too_short() {
        let bars = timed_bars(&[
            (100.0, 100.0, 100.0, 100.0),
            (100.0, 102.0, 100.0, 101.0),
            (101.0, 101.0, 100.0, 100.5), // only 2 bars after entry-cross
        ]);
        let ann = annotate(&input(), &bars, &[]).unwrap();
        assert_eq!(ann.outcome, Outcome::InsufficientData);
    }

    #[test]
    fn rollover_before_entry_counts_separately() {
        // 预警后尚未触发入场，先跨过换月：直接记为 Rollover，不带盈亏
        let mut bars = timed_bars(&[(100.0, 100.0, 100.0, 100.0), (100.0, 100.5, 99.5, 100.0)]);
        bars[1].rollover = true;
        let ann = annotate(&input(), &bars, &[]).unwrap();
        assert_eq!(ann.outcome, Outcome::Rollover);
        assert_eq!(ann.exit_reason, ExitReason::Rollover);
        assert_eq!(ann.entry_ts, None);
        assert!(ann.mfe_r.is_none());
        assert!(ann.mae_r.is_none());
        assert!(ann.bars_held.is_none());
        assert!(ann.rollover_crossed);
        assert_eq!(ann.target_tier, None);
        assert_eq!(ann.b_vol_ratio, None);
        assert_eq!(ann.a_move_atr, None);
        assert_eq!(ann.trigger_lag_bars, None);
        assert_eq!(ann.trigger_overshoot_r, None);
    }

    #[test]
    fn rollover_after_entry_clears_mfe_mae() {
        let mut bars = timed_bars(&[
            (100.0, 100.0, 100.0, 100.0),
            (100.0, 102.0, 100.0, 101.0), // entry-cross
            (101.0, 101.5, 100.5, 101.0),
        ]);
        bars[2].rollover = true;
        let ann = annotate(&input(), &bars, &[]).unwrap();
        assert_eq!(ann.outcome, Outcome::Rollover);
        assert_eq!(ann.exit_reason, ExitReason::Rollover);
        assert_eq!(ann.entry_ts.as_deref(), Some("2026-08-03 09:30"));
        assert!(ann.r_multiple.is_none());
        assert!(ann.mfe_r.is_none());
        assert!(ann.mae_r.is_none());
        assert!(ann.bars_held.is_none());
        assert!(ann.rollover_crossed);
        assert_eq!(ann.target_tier, None);
        assert_eq!(ann.trigger_lag_bars, Some(1));
        assert_eq!(ann.trigger_overshoot_r, Some(0.5));
    }

    #[test]
    fn short_direction_symmetric() {
        let mut inp = input();
        inp.direction = "down".to_string();
        inp.entry = 100.0;
        inp.stop = 102.0;
        inp.target = 96.0;
        inp.risk = 2.0;
        let bars = timed_bars(&[
            (101.0, 101.0, 101.0, 101.0),
            (101.0, 101.0, 99.0, 100.0), // entry-cross (low <= 100)
            (99.0, 99.5, 95.0, 96.0),    // 触及 2.4R=95.2 并回落，按该位止盈
        ]);
        let ann = annotate(&inp, &bars, &[]).unwrap();
        assert_eq!(ann.outcome, Outcome::Win);
        assert_eq!(ann.exit_reason, ExitReason::Target);
        // 触及 1R 后推动止盈：单根触到 0.8×3R = 2.4R = 95.2，回落到该位止盈
        assert!((ann.r_multiple.unwrap() - 2.4).abs() < 1e-9);
        assert!((ann.exit_price.unwrap() - 95.2).abs() < 1e-9);
        assert_eq!(ann.target_tier.as_deref(), Some("tp2"));
        assert_eq!(ann.trigger_overshoot_r, Some(0.5));
    }

    #[test]
    fn short_entry_gap_fills_at_open_and_marks() {
        let mut inp = input();
        inp.direction = "down".to_string();
        inp.entry = 100.0;
        inp.stop = 102.0;
        inp.target = 96.5;
        inp.risk = 2.0;
        let bars = timed_bars(&[
            (101.0, 101.0, 101.0, 101.0), // warning
            (98.0, 98.5, 98.1, 98.2),     // 跳空跌破 entry，但未触及 TP1
            (98.2, 99.0, 96.5, 97.0),     // 触及推动止盈 1.6R=96.8
        ]);
        let ann = annotate(&inp, &bars, &[]).unwrap();
        assert!(ann.gap_crossed_entry);
        assert!(!ann.gap_crossed_exit);
        assert_eq!(ann.exit_reason, ExitReason::Target);
        assert!((ann.exit_price.unwrap() - 96.8).abs() < 1e-9);
        assert!((ann.r_multiple.unwrap() - 0.6).abs() < 1e-9);
    }

    #[test]
    fn invalid_risk_skipped() {
        let mut inp = input();
        inp.risk = 0.0; // 入场=止损
        let bars = bar_seq(10);
        assert!(annotate(&inp, &bars, &[]).is_none());
    }

    #[test]
    fn features_volume_and_oi() {
        // 触发 bar：成交量 500（前一根 100），持仓量较前一根增加
        let specs = vec![(100.0, 100.0, 100.0, 100.0), (100.0, 102.0, 100.0, 101.0)];
        let mut bars = timed_bars(&specs);
        bars[0].volume = 100.0;
        bars[0].hold = 900.0;
        bars[1].volume = 500.0;
        bars[1].hold = 1200.0;
        let ann = annotate(&input(), &bars, &[]).unwrap();
        assert_eq!(ann.vol_ratio, Some(5.0));
        assert_eq!(ann.oi_increase, Some(true));
    }

    #[test]
    fn diagnostics_capture_tier_volume_lag_overshoot_and_a_strength() {
        // 30 根 15m K线从 05:45 开始，触发 bar 位于 10:45（index=20），保证 ATR20 可用。
        let mut bars = timed_bars_at(5, 45, &[(100.0, 100.5, 99.5, 100.0); 30]);
        // a 段（05:45..=06:15）均量 100；b 段（06:15..=10:30）均量 50，缩量比为 0.5。
        for i in 3..=19 {
            bars[i].volume = 50.0;
        }
        bars[20] = Bar {
            open: 100.0,
            high: 102.0,
            low: 100.0,
            close: 101.0,
            ..bars[20]
        };
        bars[21] = Bar {
            open: 101.0,
            high: 103.2,
            low: 103.1,
            close: 103.1,
            ..bars[21]
        };
        bars[22] = Bar {
            open: 103.1,
            high: 104.5,
            low: 103.2,
            close: 104.4,
            ..bars[22]
        };

        let mut inp = input();
        inp.s0_ts = Some("2026-08-03 05:45".to_string());
        inp.s1_ts = Some("2026-08-03 06:15".to_string());
        inp.s2_ts = Some("2026-08-03 10:30".to_string());
        inp.warning_ts = Some("2026-08-03 10:30".to_string());
        inp.created_at = "2026-08-03 10:30".to_string();

        let ann = annotate(&inp, &bars, &[]).unwrap();
        assert_eq!(ann.outcome, Outcome::Win);
        assert_eq!(ann.target_tier.as_deref(), Some("tp2"));
        // a/b 段共享 s1 那根 K 线：a 均量 100，b 均量 (100 + 17×50)/18
        assert!((ann.b_vol_ratio.unwrap() - 0.5277777777777778).abs() < 1e-9);
        assert_eq!(ann.trigger_lag_bars, Some(1));
        assert_eq!(ann.trigger_overshoot_r, Some(0.5));
        let atr20 = ann.a_move_atr.expect("a段强度应有值");
        assert!(atr20 > 3.0 && atr20 < 5.0, "a_move_atr = {atr20}");
    }

    #[test]
    fn atr_percentile_uses_previous_window() {
        let mut atr20: Vec<Option<f64>> = (0..61).map(|i| Some(i as f64)).collect();
        atr20.push(Some(10.0));
        assert_eq!(
            atr_percentile_at(&atr20, 61),
            Some(10.0 / 60.0),
            "前 60 根中 1~10 共 10 根不高于当前值 10"
        );
        assert_eq!(atr_percentile_at(&atr20, 0), None);

        let sparse = vec![Some(3.0), None, Some(2.0)];
        assert_eq!(atr_percentile_at(&sparse, 2), Some(0.0));
    }

    #[test]
    fn trend60_score_bounds_and_direction() {
        let mut up: Vec<Bar> = Vec::new();
        for i in 0..40 {
            let close = 100.0 + i as f64;
            up.push(Bar {
                dt: DT {
                    year: 2026,
                    month: 8,
                    day: 3,
                    hour: 9,
                    minute: 0 + i as i32,
                },
                open: close - 1.0,
                high: close + 0.5,
                low: close - 1.5,
                close,
                volume: 100.0,
                hold: 1000.0,
                rollover: false,
            });
        }
        let up_score = trend60_score_at(&up, "2026-08-03 10:00", Dir::Up).unwrap();
        assert!(up_score >= 3.0 && up_score <= 5.0);
        let down_score = trend60_score_at(&up, "2026-08-03 10:00", Dir::Down).unwrap();
        assert!(down_score < up_score);

        // 横盘序列：NEUTRAL，两种方向都应落在中间区间
        let flat: Vec<Bar> = (0..40)
            .map(|i| Bar {
                dt: DT {
                    year: 2026,
                    month: 8,
                    day: 3,
                    hour: 9,
                    minute: 0 + i as i32,
                },
                open: 100.0,
                high: 101.0,
                low: 99.0,
                close: 100.0,
                volume: 100.0,
                hold: 1000.0,
                rollover: false,
            })
            .collect();
        let flat_score = trend60_score_at(&flat, "2026-08-03 10:00", Dir::Up).unwrap();
        assert!((flat_score - 2.0).abs() < 0.6);
    }

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
                s1_ts: Some("2026-08-03 08:45".to_string()),
                s2_ts: Some(s2_ts.to_string()),
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
            s1_ts: Some("2026-08-03 08:45".to_string()),
            s2_ts: Some("2026-08-03 09:15".to_string()),
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
                s1_ts: Some("2026-08-03 08:45".to_string()),
                s2_ts: Some(format!("2026-08-03 09:{id:02}")),
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
            s1_ts: Some("2026-08-03 08:45".to_string()),
            s2_ts: Some(format!("2026-08-03 09:{id:02}")),
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
            s1_ts: Some("2026-08-03 08:45".to_string()),
            s2_ts: Some(format!("2026-08-03 09:{id:02}")),
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
            s1_ts: Some("2026-08-03 08:45".to_string()),
            s2_ts: Some(format!("2026-08-03 09:{id:02}")),
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
