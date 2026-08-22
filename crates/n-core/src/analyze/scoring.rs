use crate::analyze::indicators;
use crate::analyze::model::{Bar, Dir, Grade, NPattern, SignalCheck, Trend60};

const STOP_FOLLOW_MIN_AGE: usize = 3;
const STOP_FOLLOW_MAX_AGE: usize = 6;
const STOP_FOLLOW_DISTANCE_RISK: f64 = 1.0;
// 未触发信号的预警K线最大存活根数，超过视为过时
const PENDING_MAX_AGE: usize = 12;
// 触发K线受阻的影线门槛：只用于“触发受阻”扣分，不等同于预警K线长影线门槛。
const ENTRY_BLOCK_WICK_ATR_MIN: f64 = 0.25;
const ENTRY_BLOCK_WICK_RANGE_MIN: f64 = 0.50;
const OPPOSING_PREV_RANGE_ATR_MIN: f64 = 0.80;
const OPPOSING_PREV_BODY_ATR_MIN: f64 = 0.50;
// ===== a段质量分标定参数 =====
// dim_a = 0.5 + 4.5 × 幅度因子 × 速度因子 × 干净因子 - 跳空扣分。
// 三个因子分别设甜区，乘法短板结构：任何一个不合格都会按比例压分。
// 长度不再单独扣分，长腿由速度因子和干净因子共同把关。
const A_LEG_CORE_SCORE_WEIGHT: f64 = 4.5;
// 推进速度甜区：0.25~0.55 ATR/根满档；0.15~0.25 线性升温；
// 0.55~0.90 过快段线性降权，超过 0.90 保留 0.6 下限。
const A_LEG_SPEED_ATR_MIN: f64 = 0.15;
const A_LEG_SPEED_ATR_RAMP_FULL: f64 = 0.25;
const A_LEG_SPEED_ATR_FULL: f64 = 0.55;
const A_LEG_SPEED_ATR_FAST_START: f64 = 0.90;
const A_LEG_SPEED_FAST_PENALTY_MAX: f64 = 0.4;
const A_LEG_SPEED_FLOOR: f64 = 0.6;
/// A段单根K线在干净度中的归类，所有K都参与加权。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ALegBarKind {
    Clean,
    PlainSame,
    BigSame,
    Wick,
    ReverseWick,
    Doji,
    FavWick,
    SmallReverse,
    Reverse,
    BigReverse,
    Neutral,
}
// A段逐根K线质量分（ATR 加权）：整条腿所有K都参与，不再只挑“显著K”平均。
// 同色小K给轻分，速度因子负责惩罚“全是小K”的慢腿；
// 大K、逆势长影、普通/大反向K直接扣分。
const A_LEG_BAR_CLEAN: f64 = 1.0;
const A_LEG_BAR_PLAIN_SAME: f64 = 0.6;
const A_LEG_BAR_BIG_SAME: f64 = -0.6;
const A_LEG_BAR_WICK: f64 = -0.5;
const A_LEG_BAR_REVERSE_WICK: f64 = -0.9;
const A_LEG_BAR_DOJI: f64 = -0.1;
const A_LEG_BAR_FAVORABLE_WICK: f64 = 0.2;
const A_LEG_BAR_SMALL_REVERSE: f64 = -0.4;
const A_LEG_BAR_REVERSE: f64 = -1.2;
const A_LEG_BAR_BIG_REVERSE: f64 = -1.8;
// 干净因子归一：加权平均 -2~+1 映射到 0~1。
const A_LEG_CLEAN_SCORE_SHIFT: f64 = 2.0;
const A_LEG_CLEAN_SCORE_SCALE: f64 = 3.0;
// 长影线门槛：逆势影达到 0.4 ATR 且超过实体即判长影，先于 Doji 和小反向，
// 小实体长影不会再被实体大小分类吞掉。顺向影达到 2 倍实体且收盘有利才奖励。
const A_LEG_WICK_ATR_MIN: f64 = 0.4;
const A_LEG_WICK_BODY_RATIO: f64 = 1.0;
const A_LEG_FAV_WICK_BODY_RATIO: f64 = 2.0;
const A_LEG_DOJI_BODY_RATIO_MAX: f64 = 0.15;
// 大K门槛：同色振幅超过 2.5 ATR 判大同色K；反向实体 0.8 ATR
// 或振幅 1.5 ATR 判大反向K。
const A_LEG_BAR_BIG_SAME_ATR_MIN: f64 = 2.5;
const A_LEG_BAR_BIG_REVERSE_BODY_ATR_MIN: f64 = 0.8;
const A_LEG_BAR_BIG_REVERSE_RANGE_ATR_MIN: f64 = 1.5;
const A_LEG_BAR_REVERSE_BODY_ATR_MIN: f64 = 0.25;
const A_LEG_BAR_REVERSE_RANGE_ATR_MIN: f64 = 0.8;
// 干净同色K：实体占比≥0.55、逆势影≤0.4 实体、振幅 0.4~2.0 ATR。
const A_LEG_CLEAN_BODY_RATIO_MIN: f64 = 0.55;
const A_LEG_CLEAN_WICK_BODY_RATIO_MAX: f64 = 0.4;
const A_LEG_CLEAN_RANGE_ATR_MIN: f64 = 0.4;
const A_LEG_CLEAN_RANGE_ATR_MAX: f64 = 2.0;
// 逐根权重：振幅/ATR 夹在 0.25~2.0，S1 固定 1。
const A_LEG_BAR_WEIGHT_MIN_ATR: f64 = 0.25;
const A_LEG_BAR_WEIGHT_MAX_ATR: f64 = 2.0;
// 幅度甜区：1.5 ATR 以下线性升温，2.5 ATR 起满档；
// 上限随形态尺度浮动：8 + max(0, N_ATR - 12) × 0.5 ATR，
// 超过上限后 4 ATR 内线性降到 0.6，避免大形态被同尺度误伤。
const A_LEG_AMPLITUDE_ATR_RAMP_MIN: f64 = 1.5;
const A_LEG_AMPLITUDE_ATR_FULL_MIN: f64 = 2.5;
const A_LEG_AMPLITUDE_CAP_BASE_ATR: f64 = 8.0;
const A_LEG_AMPLITUDE_CAP_N_BASE_ATR: f64 = 12.0;
const A_LEG_AMPLITUDE_CAP_PER_N_ATR: f64 = 0.5;
const A_LEG_AMPLITUDE_CAP_DECAY_ATR: f64 = 4.0;
const A_LEG_AMPLITUDE_CAP_DECAY_MAX: f64 = 0.4;
const A_LEG_AMPLITUDE_FLOOR: f64 = 0.6;
// A段跳空处理：向上、向下跳空都统计，单根缺口达到 1 倍 ATR 才算大跳空。
// 大跳空先剔除出 a_move，不再贡献幅度和推进速度；再按
// “每根 0.15 + 超出 1 倍 ATR 部分每 ATR 0.20”计惩罚，封顶 0.5。
// 跨合约换月的 rollover bar 不构成真实跳空，不参与统计。
const A_LEG_GAP_MIN_ATR: f64 = 1.0;
const A_LEG_GAP_PENALTY_PER_GAP: f64 = 0.15;
const A_LEG_GAP_PENALTY_PER_EXCESS_ATR: f64 = 0.20;
const A_LEG_GAP_PENALTY_MAX: f64 = 0.5;
// 多K累积覆盖的入场分上限：只允许小仓试错，不进入标准仓区间
const CUMULATIVE_ENTRY_SCORE_MAX: f64 = 3.9;
// 长影线预警的入场分上限：全分档胜率都弱，不允许进入 3.5+ 标准仓区间。
pub(crate) const WICK_ENTRY_SCORE_MAX: f64 = 3.0;
// 预警K线长影线硬门槛（2026-08-15），七条同时满足才识别：
// 实体 > 0；主影线 ≥ 3 倍实体；主影线 ≥ 60% 振幅且 ≥ 0.5 倍 ATR20；
// 收盘位于反向一端 25% 振幅内；反向影线 ≤ 10% 振幅；
// 主影线 ≥ 50% 前一根 b 向K线振幅。
const WICK_BODY_RATIO_MIN: f64 = 3.0;
const WICK_ATR_MIN: f64 = 0.50;
const WICK_RANGE_MIN: f64 = 0.60;
const WICK_CLOSE_POS_MAX: f64 = 0.25;
const WICK_REVERSE_SHADOW_MAX_RATIO: f64 = 0.10;
const WICK_PREV_BAR_RANGE_MIN: f64 = 0.50;
// 强反转（干净吞没）硬门槛：反向影线必须严格小于 50% 振幅。
// 等于或超过 50% 说明收盘只回到振幅中点或更差，吞没质量不足，不再识别为 strong。
const STRONG_REVERSE_SHADOW_MAX_RATIO: f64 = 0.5;
// 吞没型强反转的实体硬门槛：实体至少达到 0.25 倍 ATR20，否则只是“包住前一根”的
// 小实体K线，收盘位置再合格也不作为强反转预警，避免 L0 944 22:45 这类
// 实体仅 2 点、约 0.06 ATR 的弱吞没虚高预警质量。
const STRONG_ENGULF_BODY_ATR_MIN: f64 = 0.25;
/// 长影线收盘方向微调：做多长下影收阴、做空长上影收阳时，预警K线质量略扣 0.1 分。
/// 只影响评分，不改变七条识别门槛。
const WICK_DIRECTION_PENALTY: f64 = 0.1;
// 预警K线体量扣分：振幅达到 2 倍 ATR20 的预警K线视为“特别巨大”，
// 会消耗较多动能并拉大止损空间，预警质量按比例下降。
// 是否覆盖到 S1 目标空间不作为扣分依据，破位预期下覆盖目标空间是正常现象。
const WARNING_SIZE_ATR_START: f64 = 2.0;
const WARNING_SIZE_ATR_FULL: f64 = 3.5;
const WARNING_SIZE_PENALTY_MAX: f64 = 1.0;

fn clamp(v: f64) -> f64 {
    v.clamp(0.0, 5.0)
}

pub(crate) fn atr_at(atr20: &[Option<f64>], index: usize) -> f64 {
    atr20.get(index).and_then(|x| *x).unwrap_or(1.0)
}

/// A段逐K质量与净推进结构明细，供复盘卡片直接展示。
#[derive(Debug, Clone, Copy)]
pub(crate) struct ALegDetail {
    pub q: f64,
    pub gap_sum: f64,
    pub gap_count: usize,
    pub gap_penalty: f64,
    pub net_move: f64,
    pub atr: f64,
}

pub(crate) fn a_leg_detail(bars: &[Bar], atr20: &[Option<f64>], p: &NPattern) -> ALegDetail {
    let atr = atr_at(atr20, p.s1.index);
    let q = a_leg_clean_fit(bars, atr20, p);
    let (gap_sum, gap_count, gap_penalty) = a_leg_gap_info(bars, p, atr);
    ALegDetail {
        q,
        gap_sum,
        gap_count,
        gap_penalty,
        net_move: (p.a_move - gap_sum).max(0.0),
        atr,
    }
}

/// a段质量分：衡量“推动腿”的幅度与K线质量，采用乘法短板结构。
///
/// 公式：
///   dim_a = 0.5 + 4.5 × 幅度因子 × 速度因子 × 干净因子 - 跳空扣分
///
/// 设计要点（避免“各项中等、凑满高分”的加法漏洞）：
/// 1. 幅度、速度、干净度三项相乘，任何一项弱都会按比例压低总分；
/// 2. 推进速度 = (净推进/根数)/ATR：0.25~0.55 满档，0.15 以下零分，
///    0.55 以上过快段降权，0.90 以上保留 0.6 下限；
/// 3. 干净因子由整条腿所有K参与，逐根按 K 线方向、实体大小、影线打分，
///    大反向K、逆势长影、大同色K直接扣分；普通小K给轻分，速度因子惩罚小K慢腿；
///    S1 最后一根固定权重 1，其余权重 = clamp(振幅/ATR, 0.25, 2.0)；
/// 4. 幅度因子带甜区：1.5~2.5 ATR 过渡，2.5 ATR 起满档，上限随
///    N_ATR（A+B 净幅度）浮动到 8+（N-12）×0.5，超过上限线性降到 0.6；
/// 5. A段内部大跳空按次数和超出 1 倍 ATR 的部分单独扣分，
///    向上、向下都统计，且不再参与幅度和速度收益。
pub(crate) fn score_a(bars: &[Bar], atr20: &[Option<f64>], p: &NPattern) -> f64 {
    let d = a_leg_detail(bars, atr20, p);
    if d.atr <= 0.0 {
        return 0.0;
    }
    let leg_atr = d.net_move / d.atr;
    let n_atr = (d.net_move + p.b_move) / d.atr;
    let amplitude = a_leg_amplitude_factor(leg_atr, n_atr);
    let speed_atr = d.net_move / p.a_bars.max(1) as f64 / d.atr;
    let speed = a_leg_speed_factor(speed_atr);
    a_leg_score_formula(amplitude, speed, d.q, d.gap_penalty)
}

/// A段干净因子（ATR 加权）：整条腿所有K都参与，逐根打质量分后做
/// “S1 权重 1、其余权重 = clamp(振幅/ATR, 0.25, 2.0)”加权平均。
/// 加权平均 -2~+1 映射到 0..1；没有有效K线时返回 0。
fn a_leg_clean_fit(bars: &[Bar], atr20: &[Option<f64>], p: &NPattern) -> f64 {
    let mut weight_sum = 0.0;
    let mut total_sum = 0.0;
    for i in p.s0.index..=p.s1.index {
        let Some(bar) = bars.get(i) else {
            continue;
        };
        let atr = atr20.get(i).copied().flatten();
        let (score, _) = a_leg_bar_score_detail(bar, atr, p.dir);
        let weight = if i == p.s1.index {
            1.0
        } else {
            match atr {
                Some(a) if a > 0.0 => ((bar.high - bar.low) / a)
                    .clamp(A_LEG_BAR_WEIGHT_MIN_ATR, A_LEG_BAR_WEIGHT_MAX_ATR),
                _ => 1.0,
            }
        };
        weight_sum += weight;
        total_sum += score * weight;
    }
    if weight_sum > 0.0 {
        ((total_sum / weight_sum + A_LEG_CLEAN_SCORE_SHIFT) / A_LEG_CLEAN_SCORE_SCALE)
            .clamp(0.0, 1.0)
    } else {
        0.0
    }
}

/// 单根K线在 A 段方向上的质量分（干净度用）。
/// 同色：干净 +1.0、普通同色 +0.6、大同色 -0.6、逆势长影 -0.5；
/// 反向：顺向长影 +0.2、小反向 -0.4、普通反向 -1.2、大反向 -1.8、
/// 逆势长影 -0.9；十字星 -0.1；无效K记 0。
#[cfg(test)]
fn a_leg_bar_score(bar: &Bar, atr: Option<f64>, dir: Dir) -> f64 {
    a_leg_bar_score_detail(bar, atr, dir).0
}

/// 单根K线质量分，同时返回干净度归类。
fn a_leg_bar_score_detail(bar: &Bar, atr: Option<f64>, dir: Dir) -> (f64, ALegBarKind) {
    let Some(atr) = atr else {
        return (0.0, ALegBarKind::Neutral);
    };
    if atr <= 0.0 {
        return (0.0, ALegBarKind::Neutral);
    }
    let range = bar.high - bar.low;
    if range <= 0.0 {
        return (0.0, ALegBarKind::Neutral);
    }
    let body = (bar.close - bar.open).abs();
    let upper = bar.high - bar.open.max(bar.close);
    let lower = bar.open.min(bar.close) - bar.low;
    let body_ratio = body / range;
    let pos = match dir {
        Dir::Up => (bar.close - bar.low) / range,
        Dir::Down => (bar.high - bar.close) / range,
    };
    let same = match dir {
        Dir::Up => bar.close > bar.open,
        Dir::Down => bar.close < bar.open,
    };
    let oppose_wick = match dir {
        Dir::Up => upper,
        Dir::Down => lower,
    };
    let favor_wick = match dir {
        Dir::Up => lower,
        Dir::Down => upper,
    };
    let favorable_close = match dir {
        Dir::Up => pos <= 0.30,
        Dir::Down => pos >= 0.70,
    };

    // 反向K带顺向长影（做多下影/做空上影）且收盘有利：拒绝/供应信号，给温和正分。
    // 这类K先于反向大小实体判断，避免把“好影线”误伤成大反向。
    if !same
        && range >= A_LEG_BAR_REVERSE_RANGE_ATR_MIN * atr
        && favor_wick >= A_LEG_FAV_WICK_BODY_RATIO * body
        && favorable_close
    {
        return (A_LEG_BAR_FAVORABLE_WICK, ALegBarKind::FavWick);
    }

    // 逆势长影优先于 Doji、小反向和大实体分类：小实体长影不会再被吞掉。
    if oppose_wick >= A_LEG_WICK_ATR_MIN * atr && oppose_wick >= A_LEG_WICK_BODY_RATIO * body {
        if !same
            && (body >= A_LEG_BAR_BIG_REVERSE_BODY_ATR_MIN * atr
                || range >= A_LEG_BAR_BIG_REVERSE_RANGE_ATR_MIN * atr)
        {
            return (A_LEG_BAR_BIG_REVERSE, ALegBarKind::BigReverse);
        }
        if same && range > A_LEG_BAR_BIG_SAME_ATR_MIN * atr {
            return (A_LEG_BAR_BIG_SAME, ALegBarKind::BigSame);
        }
        if same {
            return (A_LEG_BAR_WICK, ALegBarKind::Wick);
        }
        return (A_LEG_BAR_REVERSE_WICK, ALegBarKind::ReverseWick);
    }

    if body <= 0.0 || body_ratio < A_LEG_DOJI_BODY_RATIO_MAX {
        return (A_LEG_BAR_DOJI, ALegBarKind::Doji);
    }

    if same {
        if range > A_LEG_BAR_BIG_SAME_ATR_MIN * atr {
            return (A_LEG_BAR_BIG_SAME, ALegBarKind::BigSame);
        }
        if body_ratio >= A_LEG_CLEAN_BODY_RATIO_MIN
            && oppose_wick <= A_LEG_CLEAN_WICK_BODY_RATIO_MAX * body
            && range >= A_LEG_CLEAN_RANGE_ATR_MIN * atr
            && range <= A_LEG_CLEAN_RANGE_ATR_MAX * atr
        {
            return (A_LEG_BAR_CLEAN, ALegBarKind::Clean);
        }
        return (A_LEG_BAR_PLAIN_SAME, ALegBarKind::PlainSame);
    }

    if body >= A_LEG_BAR_BIG_REVERSE_BODY_ATR_MIN * atr
        || range >= A_LEG_BAR_BIG_REVERSE_RANGE_ATR_MIN * atr
    {
        return (A_LEG_BAR_BIG_REVERSE, ALegBarKind::BigReverse);
    }
    if body >= A_LEG_BAR_REVERSE_BODY_ATR_MIN * atr
        || range >= A_LEG_BAR_REVERSE_RANGE_ATR_MIN * atr
    {
        return (A_LEG_BAR_REVERSE, ALegBarKind::Reverse);
    }
    (A_LEG_BAR_SMALL_REVERSE, ALegBarKind::SmallReverse)
}

/// A段大跳空信息：返回（缺口合计、跳空根数、扣分）。
/// 单根缺口低于 1 倍 ATR 视为可接受的正常波动，不参与扣分。
fn a_leg_gap_info(bars: &[Bar], p: &NPattern, atr: f64) -> (f64, usize, f64) {
    if atr <= 0.0 || p.a_move <= 0.0 {
        return (0.0, 0, 0.0);
    }
    let mut gap_sum = 0.0;
    let mut gap_count = 0usize;
    let mut excess_atr = 0.0;
    for i in p.s0.index + 1..=p.s1.index {
        let Some(prev) = bars.get(i.saturating_sub(1)) else {
            continue;
        };
        let Some(cur) = bars.get(i) else {
            continue;
        };
        if prev.rollover || cur.rollover {
            continue;
        }
        let gap_up = (cur.low - prev.high).max(0.0);
        let gap_down = (prev.low - cur.high).max(0.0);
        let gap = gap_up.max(gap_down);
        if gap >= A_LEG_GAP_MIN_ATR * atr {
            gap_sum += gap;
            gap_count += 1;
            excess_atr += (gap / atr - A_LEG_GAP_MIN_ATR).max(0.0);
        }
    }
    let penalty = (gap_count as f64 * A_LEG_GAP_PENALTY_PER_GAP
        + excess_atr * A_LEG_GAP_PENALTY_PER_EXCESS_ATR)
        .min(A_LEG_GAP_PENALTY_MAX);
    (gap_sum, gap_count, penalty)
}

/// a段质量分的纯公式（抽出便于单测与标定）：三个因子相乘后扣跳空。
fn a_leg_score_formula(amplitude: f64, speed: f64, clean: f64, gap_penalty: f64) -> f64 {
    clamp(0.5 + A_LEG_CORE_SCORE_WEIGHT * amplitude * speed * clean - gap_penalty)
}

/// 幅度因子：1.5 ATR 以下线性升温，2.5 ATR 起满档；
/// 上限随形态尺度 N_ATR 浮动，超过上限后线性衰减到 0.6。
fn a_leg_amplitude_factor(leg_atr: f64, n_atr: f64) -> f64 {
    if leg_atr < A_LEG_AMPLITUDE_ATR_RAMP_MIN {
        return leg_atr / A_LEG_AMPLITUDE_ATR_RAMP_MIN;
    }
    if leg_atr < A_LEG_AMPLITUDE_ATR_FULL_MIN {
        return 0.5 + 0.5 * (leg_atr - A_LEG_AMPLITUDE_ATR_RAMP_MIN)
            / (A_LEG_AMPLITUDE_ATR_FULL_MIN - A_LEG_AMPLITUDE_ATR_RAMP_MIN);
    }
    let cap = A_LEG_AMPLITUDE_CAP_BASE_ATR
        + (n_atr - A_LEG_AMPLITUDE_CAP_N_BASE_ATR).max(0.0) * A_LEG_AMPLITUDE_CAP_PER_N_ATR;
    if leg_atr <= cap {
        return 1.0;
    }
    if leg_atr <= cap + A_LEG_AMPLITUDE_CAP_DECAY_ATR {
        return 1.0 - (leg_atr - cap) / A_LEG_AMPLITUDE_CAP_DECAY_ATR * A_LEG_AMPLITUDE_CAP_DECAY_MAX;
    }
    A_LEG_AMPLITUDE_FLOOR
}

/// 速度因子：0.15 以下零分，0.25~0.55 满档，
/// 0.55~0.90 过快段线性降权，0.90 以上保留 0.6。
fn a_leg_speed_factor(speed_atr: f64) -> f64 {
    if speed_atr < A_LEG_SPEED_ATR_MIN {
        return 0.0;
    }
    if speed_atr < A_LEG_SPEED_ATR_RAMP_FULL {
        return (speed_atr - A_LEG_SPEED_ATR_MIN)
            / (A_LEG_SPEED_ATR_RAMP_FULL - A_LEG_SPEED_ATR_MIN);
    }
    if speed_atr <= A_LEG_SPEED_ATR_FULL {
        return 1.0;
    }
    if speed_atr <= A_LEG_SPEED_ATR_FAST_START {
        return 1.0 - (speed_atr - A_LEG_SPEED_ATR_FULL)
            / (A_LEG_SPEED_ATR_FAST_START - A_LEG_SPEED_ATR_FULL)
            * A_LEG_SPEED_FAST_PENALTY_MAX;
    }
    A_LEG_SPEED_FLOOR
}

pub(crate) fn score_b(p: &NPattern) -> f64 {
    let mut s = p.grade.score_base();
    if p.b_fast && p.grade != Grade::C {
        s -= 0.5;
    }
    if p.b_too_long {
        s -= 0.5;
    }
    // b段反向K线整体收敛：回调动能在衰减，对反转结构是加分项。
    // 但长b本身已扣过动能消耗分，不能再用“后半段变小”补回来。
    if p.b_weakening && !p.b_too_long {
        s += 0.3;
    }
    // 反向强K扣分：健康回撤里第一根反向强K是正常的，不惩罚；
    // 从第 2 根起每根扣 0.3（至多按 2 根计），避免把正常回撤误判为弱结构。
    s -= (p.b_strong_reverse.saturating_sub(1).min(2) as f64) * 0.3;
    clamp(s)
}

pub(crate) fn strong_opposite_body_at(
    bars: &[Bar],
    atr20: &[Option<f64>],
    dir: Dir,
    index: usize,
) -> Option<f64> {
    let bar = bars.get(index)?;
    let atr = atr_at(atr20, index);
    if atr <= 0.0 {
        return None;
    }
    let range = bar.high - bar.low;
    let body = (bar.close - bar.open).abs();
    if range <= 0.0 || body <= 0.0 {
        return None;
    }

    let (dir_ok, close_ok) = match dir {
        Dir::Up => {
            let bearish = bar.close < bar.open;
            let close_near_low = bar.close - bar.low <= 0.5 * body;
            (bearish, close_near_low)
        }
        Dir::Down => {
            let bullish = bar.close > bar.open;
            let close_near_high = bar.high - bar.close <= 0.5 * body;
            (bullish, close_near_high)
        }
    };

    if dir_ok
        && close_ok
        && range >= OPPOSING_PREV_RANGE_ATR_MIN * atr
        && body >= OPPOSING_PREV_BODY_ATR_MIN * atr
        && body >= 0.5 * range
    {
        Some(body)
    } else {
        None
    }
}

fn entry_block_flags(
    bars: &[Bar],
    atr20: &[Option<f64>],
    dir: Dir,
    trigger: usize,
) -> (bool, bool) {
    let atr = atr_at(atr20, trigger);
    let wick_block = bars.get(trigger).is_some_and(|b| {
        if atr <= 0.0 {
            return false;
        }
        let range = b.high - b.low;
        if range <= 0.0 {
            return false;
        }
        let body = (b.close - b.open).abs();
        let upper = b.high - b.open.max(b.close);
        let lower = b.open.min(b.close) - b.low;
        let wick = match dir {
            Dir::Up => upper,
            Dir::Down => lower,
        };
        wick > body
            && wick >= ENTRY_BLOCK_WICK_ATR_MIN * atr
            && wick >= ENTRY_BLOCK_WICK_RANGE_MIN * range
    });

    let prev_block =
        trigger > 0 && strong_opposite_body_at(bars, atr20, dir, trigger - 1).is_some();

    (wick_block, prev_block)
}

#[derive(Clone, Copy, PartialEq)]
pub(crate) enum WarnKind {
    Single,
    Cumulative,
}

/// 单根反转形态的具体类型：强反转即干净吞没（必须吞没前一根K线实体），
/// 长影线预警必须通过七条硬门槛。
#[derive(Clone, Copy, Debug, PartialEq)]
pub(crate) enum SingleReversalKind {
    Strong,
    Wick,
}

impl SingleReversalKind {
    pub(crate) fn as_str(self) -> &'static str {
        match self {
            SingleReversalKind::Strong => "strong",
            SingleReversalKind::Wick => "wick",
        }
    }
}

pub(crate) fn is_opposite_close(bar: &Bar, dir: Dir) -> bool {
    match dir {
        Dir::Up => bar.close > bar.open,
        Dir::Down => bar.close < bar.open,
    }
}

/// b段方向上的严格趋势K线（做多对应强阴线，做空对应强阳线）
pub(crate) fn strong_b_dir_trend_candle(trend_k: &[(bool, bool)], i: usize, dir: Dir) -> bool {
    trend_k.get(i).is_some_and(|&(up, down)| match dir {
        Dir::Up => down,
        Dir::Down => up,
    })
}

/// 预警K线长影线硬门槛（2026-08-15）：做多看下影、做空看上影。
/// 七条同时满足才算长影线预警，任何一条不满足都不产生 wick 信号。
/// `prev` 是预警K线前面的 b 向K线；缺失或振幅不足都算不通过。
pub(crate) fn is_wick_warning_bar(bar: &Bar, atr: f64, dir: Dir, prev: Option<&Bar>) -> bool {
    let range = bar.high - bar.low;
    if range <= 0.0 || atr <= 0.0 {
        return false;
    }
    let body = (bar.close - bar.open).abs();
    if body <= 0.0 {
        return false;
    }
    let upper = bar.high - bar.open.max(bar.close);
    let lower = bar.open.min(bar.close) - bar.low;
    let wick = match dir {
        Dir::Up => lower,
        Dir::Down => upper,
    };
    let reverse_shadow = match dir {
        Dir::Up => upper,
        Dir::Down => lower,
    };
    let close_ok = match dir {
        Dir::Up => (bar.high - bar.close) / range <= WICK_CLOSE_POS_MAX,
        Dir::Down => (bar.close - bar.low) / range <= WICK_CLOSE_POS_MAX,
    };
    wick >= WICK_BODY_RATIO_MIN * body
        && wick >= WICK_RANGE_MIN * range
        && wick >= WICK_ATR_MIN * atr
        && close_ok
        && reverse_shadow <= WICK_REVERSE_SHADOW_MAX_RATIO * range
        && prev
            .map(|p| wick >= WICK_PREV_BAR_RANGE_MIN * (p.high - p.low))
            .unwrap_or(false)
}

/// 长影线收盘方向契合度：做多长下影收阴、做空长上影收阳时轻微扣分。
/// 识别阶段仍允许这类K线成为 wick 预警，只是质量分略低。
pub(crate) fn wick_direction_penalty(bar: &Bar, dir: Dir) -> f64 {
    match dir {
        Dir::Up if bar.close < bar.open => WICK_DIRECTION_PENALTY,
        Dir::Down if bar.close > bar.open => WICK_DIRECTION_PENALTY,
        _ => 0.0,
    }
}

/// 预警K线体量扣分：按振幅相对 ATR20 的倍数衡量是否“特别巨大”，方向无关。
pub(crate) fn warning_size_penalty(
    bars: &[Bar],
    atr20: &[Option<f64>],
    w: usize,
) -> f64 {
    let Some(bar) = bars.get(w) else {
        return 0.0;
    };
    let atr = atr_at(atr20, w);
    if atr <= 0.0 {
        return 0.0;
    }
    let ratio = (bar.high - bar.low) / atr;
    if ratio <= WARNING_SIZE_ATR_START {
        return 0.0;
    }
    let t = ((ratio - WARNING_SIZE_ATR_START)
        / (WARNING_SIZE_ATR_FULL - WARNING_SIZE_ATR_START))
        .clamp(0.0, 1.0);
    WARNING_SIZE_PENALTY_MAX * t
}

fn warning_base(kind: &str) -> f64 {
    match kind {
        // engulf 仅保留给历史落盘记录，新识别统一为 strong。
        "strong" | "engulf" | "wick" => 3.5,
        "cumulative" => 3.0,
        _ => 2.0,
    }
}

/// 预警K线质量分：强反转/长影线基准 3.5，多K累积覆盖基准 3.0，
/// 再扣除长影线收盘方向与预警K线体量扣分。
pub(crate) fn dim_warning(
    bars: &[Bar],
    atr20: &[Option<f64>],
    w: usize,
    kind: &str,
    dir: Dir,
) -> f64 {
    let direction_penalty = if kind == "wick" {
        bars.get(w)
            .map(|bar| wick_direction_penalty(bar, dir))
            .unwrap_or(0.0)
    } else {
        0.0
    };
    clamp(warning_base(kind) - direction_penalty - warning_size_penalty(bars, atr20, w))
}

/// 入场分 = 0.60×A段 + 0.20×B段 + 0.20×预警K线。
/// 多K累积覆盖总分封顶 3.9，只允许小仓试错；
/// 长影线预警总分封顶 3.0，历史分档胜率全面偏弱，不进 3.5+ 标准仓区间。
pub(crate) fn entry_score(
    dim_a: f64,
    dim_b: f64,
    dim_warning: f64,
    kind: &str,
) -> f64 {
    let score = 0.60 * dim_a + 0.20 * dim_b + 0.20 * dim_warning;
    match kind {
        "cumulative" => score.min(CUMULATIVE_ENTRY_SCORE_MAX),
        "wick" => score.min(WICK_ENTRY_SCORE_MAX),
        _ => score,
    }
}

/// 单根K线构成的反转形态：只有干净吞没判为强反转，反向长影线单独识别。
/// 强趋势K不再单独作为 strong：用户口径是“强反转必须吞没前一根K线，
/// 没吞没说明强度不够”，SA0 1154 这类无吞没的强趋势K只作为 b 段锚点，
/// 不再直接产生预警。
pub(crate) fn single_reversal_pattern(
    bars: &[Bar],
    atr20: &[Option<f64>],
    dir: Dir,
    w: usize,
    run_start: usize,
) -> Option<SingleReversalKind> {
    let Some(bar) = bars.get(w) else {
        return None;
    };
    let range = bar.high - bar.low;
    if range <= 0.0 {
        return None;
    }

    // 阴包阳/阳包阴：仅对反转段的第一根反向K线检查，目标实体是它前面的b向K线。
    // 要求前面是b向实体（做空为阳线、做多为阴线），新K线为反向收盘并包住前一根实体，
    // 且至少一侧严格超过前实体——避免"实体完全相同的镜像小K线"被误判为吞没。
    // 2026-08-16：strong 只保留干净吞没，反向影线必须严格小于 50% 振幅。
    let engulf = w == run_start
        && w > 0
        && match dir {
            Dir::Up => {
                let prev = &bars[w - 1];
                prev.close <= prev.open
                    && bar.close > bar.open
                    && bar.close >= prev.open
                    && bar.open <= prev.close
                    && (bar.close > prev.open || bar.open < prev.close)
            }
            Dir::Down => {
                let prev = &bars[w - 1];
                prev.close >= prev.open
                    && bar.close < bar.open
                    && bar.open >= prev.close
                    && bar.close <= prev.open
                    && (bar.open > prev.close || bar.close < prev.open)
            }
        };
    let reverse_shadow = match dir {
        Dir::Up => bar.high - bar.open.max(bar.close),
        Dir::Down => bar.open.min(bar.close) - bar.low,
    };
    let body = (bar.close - bar.open).abs();
    let atr = atr_at(atr20, w);
    if engulf
        && reverse_shadow < STRONG_REVERSE_SHADOW_MAX_RATIO * range
        && body >= STRONG_ENGULF_BODY_ATR_MIN * atr
    {
        return Some(SingleReversalKind::Strong);
    }

    is_wick_warning_bar(bar, atr, dir, bars.get(w.saturating_sub(1)))
        .then_some(SingleReversalKind::Wick)
}

/// 多K累积覆盖：连续反向收盘至少2根，且最后一根收盘越过强b向K线的开盘价（吞没其实体）
pub(crate) fn cumulative_coverage(
    bars: &[Bar],
    run_start: usize,
    j: usize,
    anchor_open: f64,
    dir: Dir,
) -> bool {
    if j < run_start + 1 {
        return false;
    }
    let last_close = bars[j].close;
    match dir {
        Dir::Up => last_close > anchor_open,
        Dir::Down => last_close < anchor_open,
    }
}

fn weak_confirm_prefix(weak: bool) -> &'static str {
    if weak {
        "反转仅靠多K累积确认，信号降级为小仓试错；"
    } else {
        ""
    }
}

fn apply_entry_score(
    sc: &mut SignalCheck,
    bars: &[Bar],
    atr20: &[Option<f64>],
    p: &NPattern,
    w: usize,
) {
    sc.dim_a = score_a(bars, atr20, p);
    sc.dim_b = score_b(p);
    sc.dim_warning = dim_warning(bars, atr20, w, sc.warning_kind, p.dir);
    sc.total = entry_score(sc.dim_a, sc.dim_b, sc.dim_warning, sc.warning_kind);
    sc.category = score_category(sc.total);
}

fn score_category(total: f64) -> &'static str {
    if total >= 4.5 {
        "标准仓，可分批加仓候选"
    } else if total >= 3.5 {
        "标准仓，按结构等级调整"
    } else if total >= 2.5 {
        "小仓试错，约标准仓25%-50%"
    } else if total >= 1.5 {
        "默认观察，不主动开仓"
    } else {
        "放弃本次结构"
    }
}

fn build_note(p: &NPattern, rr: f64) -> String {
    let mut parts = Vec::new();
    if p.grade == Grade::C {
        parts.push("C级默认半仓，需更严格触发".to_string());
    }
    if p.a_too_long {
        parts.push("a段偏长".to_string());
    }
    if p.b_fast {
        parts.push("b段偏快".to_string());
    }
    if p.b_weakening {
        parts.push("b段动能衰减".to_string());
    }
    if p.c_extended {
        parts.push("c段已透支".to_string());
    }
    if rr > 0.0 && rr < 1.0 {
        parts.push("决策点RR不足1，可按破位预期评估".to_string());
    }
    parts.push("前低/前高为决策点，不必然止盈".to_string());
    parts.join("；")
}

/// 使用默认 tick（1.0）评估信号（测试与旧路径共用）。
pub fn evaluate_signal(
    bars: &[Bar],
    atr20: &[Option<f64>],
    p: &NPattern,
    trend: &Trend60,
) -> SignalCheck {
    evaluate_signal_with_tick(bars, atr20, p, trend, 1.0)
}

/// 按品种最小变动价位（tick）评估信号：入场价在预警K线极值基础上偏移一个 tick。
pub fn evaluate_signal_with_tick(
    bars: &[Bar],
    atr20: &[Option<f64>],
    p: &NPattern,
    trend: &Trend60,
    tick: f64,
) -> SignalCheck {
    evaluate_signal_inner(bars, atr20, p, trend, tick, false)
}

/// 2.0 严格版：预警K线只接受强反转/长影线两类单K自证形态，
/// 关闭 B/C 级多K累积覆盖通道。
pub fn evaluate_signal_v2_strict_with_tick(
    bars: &[Bar],
    atr20: &[Option<f64>],
    p: &NPattern,
    trend: &Trend60,
    tick: f64,
) -> SignalCheck {
    evaluate_signal_inner(bars, atr20, p, trend, tick, true)
}

fn evaluate_signal_inner(
    bars: &[Bar],
    atr20: &[Option<f64>],
    p: &NPattern,
    trend: &Trend60,
    tick: f64,
    v2_strict: bool,
) -> SignalCheck {
    let mut sc = SignalCheck::new();
    sc.trend_state = trend.direction.clone();
    sc.trend_bonus = 0.0; // 回撤：仅展示趋势标签，不计入触发分，保持分数区分度

    let end = bars.len().min(p.s2.index + 6);
    if p.s2.index + 1 >= end {
        sc.category = "结构未完成";
        sc.state = "等待后续K线";
        sc.note = "b端后没有可用于预警的K线".to_string();
        return sc;
    }

    // 方案B：B/C级结构按系统文档§6.3要求更严格的反转确认——预警必须是
    // 强反转/长影线/多K累积覆盖之一；A级普通反向K线同样不再单独放行。
    let strict_confirm = if v2_strict {
        true
    } else {
        matches!(p.grade, Grade::B | Grade::C)
    };
    // b段终点确认：当反转段前一根K线是强b向趋势K时，单根弱反向K线不足以
    // 确认b段结束，必须出现强反转/长影线/多K累积覆盖；A级普通反向K线
    // 同样不再有快速路径兜底，只能等自证形态。
    let trend_k = indicators::trend_flags(bars, atr20);
    let mut warning = None;
    let mut warning_kind = "";
    let mut warn_kind = WarnKind::Single;
    let mut gate_active = false;
    let mut gate_anchor_strong = false;
    // s2 本身构成合格反转形态（长影线/强反转）时，s2 就是预警K线。
    // 长影线不要求反向收盘：做空时上影线够长即使收阳也算预警，做多方向对称。
    let s2_single = single_reversal_pattern(bars, atr20, p.dir, p.s2.index, p.s2.index);
    if let Some(kind) = s2_single {
        warning = Some(p.s2.index);
        warning_kind = kind.as_str();
    }
    let mut i = p.s2.index + 1;
    while warning.is_none() && i < end {
        if !is_opposite_close(&bars[i], p.dir) {
            i += 1;
            continue;
        }
        let run_start = i;
        let anchor = run_start - 1;
        // 强锚判定统一口径：严格趋势K 或 放宽口径的“强反向实体”
        // （strong_opposite_body_at 与触发受阻检测共用同一判定），
        // 避免“差一点到严格线”的强反向K线在预警门被放行、却在受阻检测被拦截。
        let anchor_strong = strong_b_dir_trend_candle(&trend_k, anchor, p.dir)
            || strong_opposite_body_at(bars, atr20, p.dir, anchor).is_some();
        gate_active = anchor_strong || strict_confirm;
        gate_anchor_strong = anchor_strong;
        let anchor_open = bars[anchor].open;

        let mut j = run_start;
        let mut found = false;
        while j < end && is_opposite_close(&bars[j], p.dir) {
            // 单根弱反向K线不再单独放行，只有强反转/长影线自证形态，
            // 或需要严格确认的路径满足多K累积覆盖时，才产生预警。
            let single_now = single_reversal_pattern(bars, atr20, p.dir, j, run_start);
            let single_ok = single_now.is_some();
            // 多K累积覆盖同样只对需要严格确认的路径开放（连续反向收盘吞没b向实体）。
            let cumul_ok = !v2_strict
                && (anchor_strong || strict_confirm)
                && cumulative_coverage(bars, run_start, j, anchor_open, p.dir);
            if single_ok || cumul_ok {
                warning = Some(j);
                let is_cumulative = cumul_ok && !single_ok;
                warning_kind = if is_cumulative {
                    "cumulative"
                } else if let Some(kind) = single_now {
                    kind.as_str()
                } else {
                    unreachable!("single_ok 只在 single_now 存在时为 true")
                };
                warn_kind = if is_cumulative {
                    WarnKind::Cumulative
                } else {
                    WarnKind::Single
                };
                found = true;
                break;
            }
            j += 1;
        }
        if found {
            break;
        }
        i = j;
    }

    let Some(w) = warning else {
        sc.warning_kind = "none";
        sc.category = "无预警K线";
        sc.state = "等待预警";
        sc.note = if gate_active {
            if v2_strict {
                "2.0要求预警K线必须为强反转/长影线形态，等待更强反转确认".to_string()
            } else if gate_anchor_strong {
                "b段末为强反向实体（强趋势K或大实体），当前反向K线未形成强反转/累积覆盖形态，等待更强反转确认"
                    .to_string()
            } else {
                "B/C级结构要求反转预警具备强反转/长影线/累积覆盖形态，等待更强反转确认".to_string()
            }
        } else {
            "b端后尚未出现与原方向一致的反转预警".to_string()
        };
        return sc;
    };
    let weak_confirm = !v2_strict && warn_kind == WarnKind::Cumulative;
    sc.warning = Some(w);
    sc.warning_kind = warning_kind;

    let mut trigger = None;
    for j in w + 1..bars.len() {
        let ok = match p.dir {
            Dir::Down => bars[j].low <= bars[w].low && bars[j].close < bars[w].low,
            Dir::Up => bars[j].high >= bars[w].high && bars[j].close > bars[w].high,
        };
        if ok {
            trigger = Some(j);
            break;
        }
    }

    let Some(t) = trigger else {
        let atr_now = atr_at(atr20, p.s2.index);
        let buffer = (0.1 * atr_now).max(1.0);
        sc.entry = match p.dir {
            // 入场价 = 预警K线极值再偏移一个 tick：做空=低点-tick，做多=高点+tick
            Dir::Down => bars[w].low - tick,
            Dir::Up => bars[w].high + tick,
        };
        sc.stop = match p.dir {
            Dir::Down => p.s2.price + buffer,
            Dir::Up => p.s2.price - buffer,
        };
        sc.decision_target = p.s1.price;
        sc.risk = match p.dir {
            Dir::Down => sc.stop - sc.entry,
            Dir::Up => sc.entry - sc.stop,
        };
        sc.space = match p.dir {
            Dir::Down => sc.entry - sc.decision_target,
            Dir::Up => sc.decision_target - sc.entry,
        };
        sc.rr = if sc.risk > 0.0 {
            sc.space / sc.risk
        } else {
            0.0
        };

        if p.hard_failure || p.grade == Grade::Invalid {
            sc.state = "结构失效";
            sc.total = 0.0;
            sc.category = "结构硬失效，不参与";
            sc.note = "b段已破位或深V折返，结构事实失效".to_string();
            return sc;
        }
        if sc.risk <= 0.0 || sc.space <= 0.0 {
            sc.state = "空间异常";
            sc.total = 0.0;
            sc.category = "空间异常，暂不参与";
            sc.note = "止损或决策点空间无法正常计算".to_string();
            return sc;
        }

        // 未触发时，b段终点被反向收盘价突破，结构视为失效
        let b_leg_broken = bars[p.s2.index + 1..].iter().any(|b| match p.dir {
            Dir::Up => b.close < p.s2.price,
            Dir::Down => b.close > p.s2.price,
        });
        if b_leg_broken {
            sc.state = "结构失效";
            sc.total = 0.0;
            sc.category = "结构失效，不参与";
            sc.note = match p.dir {
                Dir::Up => "b段终点已被收盘价跌破，结构失效，放弃等待".to_string(),
                Dir::Down => "b段终点已被收盘价上破，结构失效，放弃等待".to_string(),
            };
            return sc;
        }

        sc.state = "即将触发";
        apply_entry_score(&mut sc, bars, atr20, p, w);

        // 未触发信号的时效检查：触发位距现价过远，或预警后等待过久
        let last_close = bars.last().map(|b| b.close).unwrap_or(0.0);
        let entry_distance = (last_close - sc.entry).abs();
        let pending_age = bars.len().saturating_sub(w + 1);
        let base_note = format!(
            "{}{}",
            weak_confirm_prefix(weak_confirm),
            build_note(p, sc.rr)
        );
        if entry_distance >= STOP_FOLLOW_DISTANCE_RISK * sc.risk {
            sc.state = "已过时，仅复盘";
            sc.note = format!(
                "{}；预警后尚未触发，现价距入场{:.1}点，触发位已偏离，仅用于复盘",
                base_note, entry_distance
            );
        } else if pending_age > PENDING_MAX_AGE {
            sc.state = "已过时，仅复盘";
            sc.note = format!(
                "{}；预警后{}根K线未触发，信号已过时，仅用于复盘",
                base_note, pending_age
            );
        } else {
            sc.note = format!("{}；预警后尚未完成突破，继续等待", base_note);
        }
        return sc;
    };
    sc.trigger = Some(t);
    let (wick_block, prev_block) = entry_block_flags(bars, atr20, p.dir, t);
    let b_end_block = strong_opposite_body_at(bars, atr20, p.dir, p.s2.index).is_some();
    let entry_block_count = wick_block as u8 + prev_block as u8 + b_end_block as u8;
    if entry_block_count > 0 {
        sc.entry_block_count = entry_block_count;
        let mut flags = Vec::new();
        if b_end_block {
            flags.push(match p.dir {
                Dir::Up => "b段末K大实体阴线",
                Dir::Down => "b段末K大实体阳线",
            });
        }
        if prev_block {
            flags.push(match p.dir {
                Dir::Up => "前一根大实体阴线",
                Dir::Down => "前一根大实体阳线",
            });
        }
        if wick_block {
            flags.push(match p.dir {
                Dir::Up => "触发K线长上影",
                Dir::Down => "触发K线长下影",
            });
        }
        sc.entry_block_detail = flags.join(" + ");
    }
    sc.trigger_age = bars.len().saturating_sub(t + 1);
    sc.state = if p.hard_failure {
        "结构失效"
    } else if sc.trigger_age <= 2 {
        "当前已触发"
    } else if sc.trigger_age <= 6 {
        "已触发，接近时效边界"
    } else {
        "已过时，仅复盘"
    };

    let atr_now = atr_at(atr20, p.s2.index);
    let buffer = (0.1 * atr_now).max(1.0);

    let (entry, stop, decision_target) = match p.dir {
        Dir::Down => (bars[w].low - tick, p.s2.price + buffer, p.s1.price),
        Dir::Up => (bars[w].high + tick, p.s2.price - buffer, p.s1.price),
    };

    sc.entry = entry;
    sc.stop = stop;
    sc.decision_target = decision_target;
    sc.risk = match p.dir {
        Dir::Down => stop - entry,
        Dir::Up => entry - stop,
    };
    sc.space = match p.dir {
        Dir::Down => entry - decision_target,
        Dir::Up => decision_target - entry,
    };
    sc.rr = if sc.risk > 0.0 {
        sc.space / sc.risk
    } else {
        0.0
    };
    apply_entry_score(&mut sc, bars, atr20, p, w);

    if p.hard_failure || p.grade == Grade::Invalid {
        sc.total = 0.0;
        sc.category = "结构硬失效，不参与";
        sc.note = "b段已破位或深V折返，结构事实失效".to_string();
        return sc;
    }

    if sc.risk <= 0.0 || sc.space <= 0.0 {
        sc.total = 0.0;
        sc.category = "空间异常，暂不参与";
        sc.note = "止损或决策点空间无法正常计算".to_string();
        return sc;
    }

    let last_close = bars.last().map(|b| b.close).unwrap_or(0.0);
    let entry_distance = (last_close - sc.entry).abs();
    let stale_by_price = (STOP_FOLLOW_MIN_AGE..=STOP_FOLLOW_MAX_AGE).contains(&sc.trigger_age)
        && entry_distance >= STOP_FOLLOW_DISTANCE_RISK * sc.risk;
    if stale_by_price {
        sc.state = "已过时，仅复盘";
    }

    sc.note = format!(
        "{}{}",
        weak_confirm_prefix(weak_confirm),
        build_note(p, sc.rr)
    );
    if sc.entry_block_count > 0 {
        let verb = match p.dir {
            Dir::Up => "追多",
            Dir::Down => "追空",
        };
        sc.note = format!("{}；触发受阻，不宜急于{}", sc.note, verb);
    }
    if stale_by_price {
        sc.note = format!(
            "{}；触发已过{}根K线，现价距入场{:.1}点",
            sc.note, sc.trigger_age, entry_distance
        );
    } else if sc.trigger_age > 6 {
        sc.note = format!("{}；该信号已过时，仅用于复盘", sc.note);
    } else if sc.trigger_age > 2 {
        sc.note = format!("{}；触发已过数根K线，谨慎评估", sc.note);
    }
    sc
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::analyze::model::{Bar, Dir, Swing, DT};
    use crate::analyze::report::is_active_signal;

    fn bar(open: f64, high: f64, low: f64, close: f64) -> Bar {
        Bar {
            dt: DT {
                year: 2026,
                month: 7,
                day: 31,
                hour: 13,
                minute: 30,
            },
            open,
            high,
            low,
            close,
            volume: 0.0,
            hold: 0.0,
            rollover: false,
        }
    }

    fn atr20(v: f64) -> Vec<Option<f64>> {
        vec![Some(v), Some(v)]
    }

    fn atrs(n: usize, v: f64) -> Vec<Option<f64>> {
        vec![Some(v); n]
    }

    fn pattern() -> NPattern {
        NPattern {
            level: "fine",
            dir: Dir::Up,
            s0: Swing {
                index: 0,
                price: 0.0,
                is_high: false,
            },
            s1: Swing {
                index: 1,
                price: 1.0,
                is_high: true,
            },
            s2: Swing {
                index: 2,
                price: 0.5,
                is_high: false,
            },
            a_bars: 1,
            b_bars: 1,
            a_move: 1.0,
            b_move: 0.5,
            retracement: 0.5,
            grade: Grade::A,
            hard_failure: false,
            a_too_long: false,
            b_too_long: false,
            b_fast: false,
            b_weakening: false,
            b_weakening_ratio: None,
            a_strong_trend: 1,
            b_strong_reverse: 0,
            c_move: 0.0,
            c_bars: 0,
            c_extended: false,
            c_hard_failure: false,
        }
    }

    fn trend60() -> Trend60 {
        Trend60 {
            direction: "NEUTRAL".to_string(),
            ma20: 0.0,
            slope: 0.0,
            price_vs_ma: 0.0,
            higher_highs: false,
            higher_lows: false,
            lower_highs: false,
            lower_lows: false,
        }
    }

    #[test]
    fn detects_opposing_trigger_shapes_for_both_directions() {
        let atr = atr20(30.0);

        let bars_long = vec![
            bar(4580.0, 4581.0, 4551.0, 4558.0),
            bar(4558.0, 4577.0, 4557.0, 4562.0),
        ];
        assert_eq!(
            entry_block_flags(&bars_long, &atr, Dir::Up, 1),
            (true, true)
        );

        let bars_short = vec![
            bar(4518.0, 4542.0, 4517.0, 4537.0),
            bar(4537.0, 4538.0, 4520.0, 4533.0),
        ];
        assert_eq!(
            entry_block_flags(&bars_short, &atr, Dir::Down, 1),
            (true, true)
        );
    }

    #[test]
    fn clean_trigger_is_not_blocked() {
        let bars = vec![
            bar(4550.0, 4560.0, 4549.0, 4558.0),
            bar(4558.0, 4562.0, 4557.0, 4561.5),
        ];
        let atr = atr20(30.0);

        assert_eq!(entry_block_flags(&bars, &atr, Dir::Up, 1), (false, false));
    }

    #[test]
    fn pending_signal_fills_entry_score_components() {
        let mut p = pattern();
        // 入场价偏移一档后，决策点需高于预警K线极值，这里把价格平移到正常量级
        p.s0.price = 4500.0;
        p.s1.price = 4550.0;
        p.s2.price = 4500.5;
        let trend = trend60();
        let atr = vec![Some(20.0); 6];
        let bars = vec![
            bar(4500.0, 4501.0, 4500.0, 4500.5),
            bar(4500.5, 4501.0, 4500.4, 4500.6),
            bar(4500.8, 4501.0, 4500.5, 4500.7), // s2 阴线（吞没的前一根）
            bar(4500.6, 4516.6, 4500.6, 4515.6), // 吞没阳线 → 预警
            bar(4500.7, 4500.95, 4500.65, 4500.9),
            bar(4500.9, 4500.9, 4500.8, 4500.85),
        ];

        let sc = evaluate_signal(&bars, &atr, &p, &trend);
        assert_eq!(sc.state, "即将触发");
        assert_eq!(sc.trigger, None);
        assert_eq!(sc.warning_kind, "strong");
        assert_eq!(
            sc.total,
            entry_score(sc.dim_a, sc.dim_b, sc.dim_warning, sc.warning_kind)
        );
        assert!(sc.note.contains("继续等待"));
    }

    #[test]
    fn a_leg_score_uses_amplitude_speed_clean_and_gap_penalty() {
        // 三因子乘法：全部满档时 5.0，任一折半都按比例压分。
        assert!((a_leg_score_formula(1.0, 1.0, 1.0, 0.0) - 5.0).abs() < 1e-9);
        assert!((a_leg_score_formula(0.5, 1.0, 1.0, 0.0) - 2.75).abs() < 1e-9);
        assert!((a_leg_score_formula(1.0, 0.5, 1.0, 0.0) - 2.75).abs() < 1e-9);
        assert!((a_leg_score_formula(1.0, 1.0, 0.5, 0.0) - 2.75).abs() < 1e-9);
        // 跳空扣分直接作用于总分，0.5 上限时总分至少降 0.5。
        assert!((a_leg_score_formula(1.0, 1.0, 1.0, 0.5) - 4.5).abs() < 1e-9);
        // 速度为零时只剩地板分。
        assert!((a_leg_score_formula(1.0, 0.0, 1.0, 0.0) - 0.5).abs() < 1e-9);
    }

    #[test]
    fn a_leg_speed_factor_has_sweet_spot_and_fast_penalty() {
        assert_eq!(a_leg_speed_factor(0.10), 0.0);
        assert!((a_leg_speed_factor(0.20) - 0.5).abs() < 1e-9);
        assert_eq!(a_leg_speed_factor(0.30), 1.0);
        assert_eq!(a_leg_speed_factor(0.55), 1.0);
        // 过快段线性降权，0.90 及以上保留 0.6。
        assert!((a_leg_speed_factor(0.70) - 0.828571428571).abs() < 1e-9);
        assert_eq!(a_leg_speed_factor(0.90), 0.6);
        assert_eq!(a_leg_speed_factor(1.20), 0.6);
    }

    #[test]
    fn a_leg_amplitude_factor_ramps_and_tracks_scale() {
        // 1.5 ATR 以下线性升温。
        assert!((a_leg_amplitude_factor(1.0, 10.0) - 1.0 / 1.5).abs() < 1e-9);
        // 2 ATR 在过渡带中点：0.5 + 0.5×(0.5/1)。
        assert!((a_leg_amplitude_factor(2.0, 10.0) - 0.75).abs() < 1e-9);
        // 2.5 ATR 起满档，基础上限 8 ATR。
        assert_eq!(a_leg_amplitude_factor(3.0, 10.0), 1.0);
        assert!((a_leg_amplitude_factor(9.0, 10.0) - 0.9).abs() < 1e-9);
        assert_eq!(a_leg_amplitude_factor(12.0, 10.0), 0.6);
        // 大形态浮动上限：N=16 时上限升到 10 ATR。
        assert_eq!(a_leg_amplitude_factor(10.0, 16.0), 1.0);
    }

    #[test]
    fn a_leg_bar_score_ranks_same_direction_candles() {
        // 干净同色：实体占比≥0.55、逆势影≤0.4 实体、振幅 0.4~2.0 ATR。
        assert!((a_leg_bar_score(&bar(100.0, 113.0, 98.0, 112.0), Some(10.0), Dir::Up) - 1.0).abs() < 1e-9);
        // 普通同色：实体占比不足，给轻分。
        assert!((a_leg_bar_score(&bar(100.0, 104.0, 97.0, 102.0), Some(10.0), Dir::Up) - 0.6).abs() < 1e-9);
        // 大同色：振幅超过 2.5 ATR 直接扣分。
        assert!((a_leg_bar_score(&bar(100.0, 130.0, 98.0, 128.0), Some(10.0), Dir::Up) - -0.6).abs() < 1e-9);
        // 无长影的十字星只轻扣。
        assert!((a_leg_bar_score(&bar(100.0, 101.5, 98.5, 100.1), Some(10.0), Dir::Up) - -0.1).abs() < 1e-9);
    }

    #[test]
    fn a_leg_bar_score_handles_wicks_and_reverse_candles() {
        // 顺向长影：做空A段小阳线带长上影（14:30 场景）应给正分，不再当小反向。
        assert!((a_leg_bar_score(&bar(90.0, 110.0, 89.0, 92.0), Some(10.0), Dir::Down) - 0.2).abs() < 1e-9);
        // 顺向长影：做多A段阴线带长下影，同样给正分。
        assert!((a_leg_bar_score(&bar(93.0, 102.0, 90.0, 92.0), Some(10.0), Dir::Up) - 0.2).abs() < 1e-9);
        // 同色小实体长上影（向上腿）判逆势长影，不再被 Doji 吞掉。
        assert!((a_leg_bar_score(&bar(95.0, 110.0, 94.0, 100.0), Some(10.0), Dir::Up) - -0.5).abs() < 1e-9);
        // 同色小实体长下影（向下腿）镜像扣分。
        assert!((a_leg_bar_score(&bar(95.0, 98.0, 80.0, 90.0), Some(10.0), Dir::Down) - -0.5).abs() < 1e-9);
        // 反向小K带逆势长影：重扣但不到大反向档。
        // 注意收盘要落在反向不利位置，否则会先被“顺向长影+收盘有利”救成 FavWick。
        assert!((a_leg_bar_score(&bar(100.0, 105.0, 92.0, 97.0), Some(10.0), Dir::Up) - -0.9).abs() < 1e-9);
        // 小反向、普通反向、大反向逐级加重。
        assert!((a_leg_bar_score(&bar(100.0, 101.0, 96.0, 98.0), Some(10.0), Dir::Up) - -0.4).abs() < 1e-9);
        assert!((a_leg_bar_score(&bar(100.0, 102.0, 93.0, 96.0), Some(10.0), Dir::Up) - -1.2).abs() < 1e-9);
        assert!((a_leg_bar_score(&bar(100.0, 102.0, 88.0, 90.0), Some(10.0), Dir::Up) - -1.8).abs() < 1e-9);
    }

    #[test]
    fn a_leg_clean_fit_uses_all_bars_weighted() {
        let atr = atrs(2, 10.0);
        let bars = vec![
            bar(100.0, 113.0, 98.0, 112.0), // Clean，权重 1.5
            bar(100.0, 113.0, 98.0, 112.0), // S1 Clean，固定权重 1
        ];
        let p = NPattern {
            dir: Dir::Up,
            s0: Swing {
                index: 0,
                price: 100.0,
                is_high: false,
            },
            s1: Swing {
                index: 1,
                price: 112.0,
                is_high: true,
            },
            ..pattern()
        };
        assert!((a_leg_clean_fit(&bars, &atr, &p) - 1.0).abs() < 1e-9);
    }

    #[test]
    fn a_leg_clean_fit_penalizes_dirty_leg() {
        let atr = atrs(2, 10.0);
        let bars = vec![
            bar(100.0, 113.0, 98.0, 112.0), // Clean，权重 1.5，得分 1
            bar(100.0, 102.0, 88.0, 90.0),  // S1 大反向，固定权重 1，得分 -1.8
        ];
        let p = NPattern {
            dir: Dir::Up,
            s0: Swing {
                index: 0,
                price: 100.0,
                is_high: false,
            },
            s1: Swing {
                index: 1,
                price: 90.0,
                is_high: true,
            },
            ..pattern()
        };
        // avg=(1×1.5-1.8×1)/2.5=-0.12，fit=(-0.12+2)/3。
        assert!((a_leg_clean_fit(&bars, &atr, &p) - 0.6266666667).abs() < 1e-9);
    }

    #[test]
    fn a_leg_clean_fit_floor_with_single_reverse() {
        let atr = atrs(1, 10.0);
        let bars = vec![bar(100.0, 102.0, 93.0, 96.0)];
        let p = NPattern {
            dir: Dir::Up,
            s0: Swing {
                index: 0,
                price: 100.0,
                is_high: false,
            },
            s1: Swing {
                index: 0,
                price: 96.0,
                is_high: true,
            },
            ..pattern()
        };
        // 普通反向 -1.2，fit=(-1.2+2)/3。
        assert!((a_leg_clean_fit(&bars, &atr, &p) - 0.2666666667).abs() < 1e-9);
    }

    #[test]
    fn a_leg_gap_info_counts_both_directions_and_ignores_small_gaps() {
        let p = NPattern {
            s0: Swing {
                index: 0,
                price: 0.0,
                is_high: false,
            },
            s1: Swing {
                index: 2,
                price: 100.0,
                is_high: true,
            },
            a_bars: 2,
            a_move: 100.0,
            ..pattern()
        };
        // 向上跳空12点、向下跳空12点，都达到 1 倍 ATR（10）门槛，
        // 缺口合计 24，扣 2×0.15 + 0.4×0.20 = 0.38。
        let both = vec![
            bar(0.0, 100.0, 100.0, 100.0),
            bar(110.0, 112.0, 112.0, 112.0),
            bar(90.0, 100.0, 90.0, 90.0),
        ];
        let (gap_sum, gap_count, penalty) = a_leg_gap_info(&both, &p, 10.0);
        assert!((gap_sum - 24.0).abs() < 1e-9);
        assert_eq!(gap_count, 2);
        assert!((penalty - 0.38).abs() < 1e-9);

        // 单根缺口 8 点小于 1 倍 ATR（10），小跳空不扣分。
        let small = vec![
            bar(0.0, 100.0, 100.0, 100.0),
            bar(106.0, 108.0, 108.0, 108.0),
            bar(90.0, 100.0, 90.0, 90.0),
        ];
        assert_eq!(a_leg_gap_info(&small, &p, 10.0), (0.0, 0, 0.0));

        // rollover bar 的跨合约跳空不参与统计。
        let mut rollover_gap = both.clone();
        rollover_gap[1].rollover = true;
        assert_eq!(a_leg_gap_info(&rollover_gap, &p, 10.0), (0.0, 0, 0.0));

        // 单根缺口 3 倍 ATR：0.15 + 2×0.20 = 0.55，封顶 0.5。
        let huge = vec![
            bar(0.0, 100.0, 100.0, 100.0),
            bar(130.0, 140.0, 130.0, 138.0),
            bar(138.0, 160.0, 138.0, 158.0),
        ];
        let (huge_sum, huge_count, huge_penalty) = a_leg_gap_info(&huge, &p, 10.0);
        assert!((huge_sum - 30.0).abs() < 1e-9);
        assert_eq!(huge_count, 1);
        assert!((huge_penalty - 0.5).abs() < 1e-9);
    }

    #[test]
    fn score_a_excludes_big_gaps_from_move_and_applies_penalty() {
        // a_move 账面 50，其中第一根K带着 1 倍 ATR 大跳空 10；
        // 净推进只有 40，幅度按 4/4.5 折算，速度仍满档，另扣 0.15；
        // S1 用一般同色K把 q 压到 23/30，避免总分封顶后看不出跳空扣分。
        let atr = atrs(3, 10.0);
        let bars = vec![
            bar(100.0, 100.0, 100.0, 100.0), // S0 十字星，不计质量
            bar(110.0, 130.0, 110.0, 128.0), // 跳空 + 完美阳线
            bar(130.0, 142.0, 130.0, 139.0), // S1 一般同色
        ];
        let p = NPattern {
            dir: Dir::Up,
            s0: Swing {
                index: 0,
                price: 100.0,
                is_high: false,
            },
            s1: Swing {
                index: 2,
                price: 139.0,
                is_high: true,
            },
            a_bars: 2,
            a_move: 50.0,
            ..pattern()
        };
        // S0 是平盘K：干净因子里记 0 分、权重 0.25；
        // 两根干净同色K权重 2 和 1，avg=(1×2+1×1+0×0.25)/3.25=12/13，
        // clean=(12/13+2)/3=38/39；速度 2.0 ATR/根已进入过快降权 0.6；
        // dim_a=0.5+4.5×1×0.6×(38/39)-0.15=2.980769231。
        assert!((score_a(&bars, &atr, &p) - 2.980769231).abs() < 1e-9);
    }

    #[test]
    fn b_leg_weakening_adds_to_b_score() {
        let clean = NPattern {
            grade: Grade::B,
            ..pattern()
        };
        assert!((score_b(&clean) - 4.3).abs() < 1e-9);

        let weakening = NPattern {
            grade: Grade::B,
            b_weakening: true,
            b_weakening_ratio: Some(0.5),
            ..pattern()
        };
        assert!((score_b(&weakening) - 4.6).abs() < 1e-9);

        // 长b已经扣过动能消耗分，后半段变小不再叠加衰减加分。
        let long_weakening = NPattern {
            grade: Grade::B,
            b_too_long: true,
            b_weakening: true,
            b_weakening_ratio: Some(0.5),
            ..pattern()
        };
        assert!((score_b(&long_weakening) - 3.8).abs() < 1e-9);
    }

    #[test]
    fn pending_signal_invalidated_when_b_leg_broken() {
        let mut p = pattern();
        p.s0.price = 4500.0;
        p.s1.price = 4550.0;
        p.s2.price = 4500.5;
        let trend = trend60();
        let atr = vec![Some(20.0); 6];
        let bars = vec![
            bar(4500.0, 4501.0, 4500.0, 4500.5),
            bar(4500.5, 4501.0, 4500.4, 4500.6),
            bar(4500.8, 4501.0, 4500.5, 4500.7), // s2 阴线（吞没的前一根）
            bar(4500.6, 4516.6, 4500.6, 4515.6), // 吞没阳线 → 预警
            bar(4500.7, 4500.9, 4500.55, 4500.75),
            bar(4500.6, 4500.7, 4500.4, 4500.45),
        ];

        let sc = evaluate_signal(&bars, &atr, &p, &trend);
        assert_eq!(sc.state, "结构失效");
        assert_eq!(sc.total, 0.0);
        assert!(sc.note.contains("跌破"));
        assert!(!is_active_signal(&sc));
    }

    #[test]
    fn pending_signal_goes_stale_after_too_many_bars() {
        let mut p = pattern();
        p.s0.price = 4500.0;
        p.s1.price = 4550.0;
        p.s2.price = 4500.5;
        let trend = trend60();
        let atr = vec![Some(20.0); 20];
        let mut bars = vec![
            bar(4500.0, 4501.0, 4500.0, 4500.5),
            bar(4500.5, 4501.0, 4500.4, 4500.6),
            bar(4500.8, 4501.0, 4500.5, 4500.7), // s2 阴线（吞没的前一根）
            bar(4500.6, 4516.6, 4500.6, 4515.6), // 吞没阳线 → 预警
            bar(4500.7, 4500.9, 4500.6, 4500.75),
        ];
        for _ in 0..14 {
            bars.push(bar(4500.6, 4500.8, 4500.55, 4500.65));
        }

        let sc = evaluate_signal(&bars, &atr, &p, &trend);
        assert_eq!(sc.state, "已过时，仅复盘");
        assert!(sc.note.contains("过时"));
        assert!(!is_active_signal(&sc));
    }

    #[test]
    fn strong_b_end_requires_qualified_reversal_warning() {
        // 做空：b段末是强趋势阳线，其后单根弱阴线/更弱阴线都不能确认b段结束
        let atr = atrs(6, 70.0);
        let p = NPattern {
            dir: Dir::Down,
            s1: Swing {
                index: 1,
                price: 14805.0,
                is_high: false,
            },
            s2: Swing {
                index: 2,
                price: 14945.0,
                is_high: true,
            },
            ..pattern()
        };
        let bars = vec![
            bar(15090.0, 15090.0, 15080.0, 15085.0), // s0 高点
            bar(14900.0, 14910.0, 14805.0, 14810.0), // s1 低点
            bar(14870.0, 14945.0, 14870.0, 14940.0), // s2 强趋势阳线
            bar(14940.0, 14940.0, 14895.0, 14900.0), // 弱阴线
            bar(14895.0, 14900.0, 14880.0, 14890.0), // 更弱阴线
            bar(14890.0, 14900.0, 14885.0, 14892.0), // 阳线打断反转段
        ];

        let sc = evaluate_signal(&bars, &atr, &p, &trend60());
        assert_eq!(sc.state, "等待预警");
        assert_eq!(sc.warning, None);
        assert!(sc.note.contains("强趋势K"));
    }

    #[test]
    fn cumulative_reversal_confirms_but_downgrades_to_small_position() {
        // 做空：第二根阴线收盘越过强趋势阳线开盘价 → 多K累积覆盖，允许预警但降级为小仓。
        // 故意把 a 段设成幅度足、强K够的形态（否则新评分会把 a 段质量分压得很低，
        // 测不到“弱确认封顶 3.9 仍落小仓”的本意）。
        let atr = atrs(6, 70.0);
        let p = NPattern {
            dir: Dir::Down,
            s1: Swing {
                index: 1,
                price: 14805.0,
                is_high: false,
            },
            s2: Swing {
                index: 2,
                price: 14945.0,
                is_high: true,
            },
            a_move: 600.0,
            a_bars: 5,
            a_strong_trend: 3,
            ..pattern()
        };
        let bars = vec![
            bar(15090.0, 15090.0, 15080.0, 15085.0),
            bar(14900.0, 14910.0, 14805.0, 14810.0),
            bar(14870.0, 14945.0, 14870.0, 14940.0), // s2 强趋势阳线
            bar(14940.0, 14940.0, 14895.0, 14900.0), // 弱阴线
            bar(14900.0, 14910.0, 14860.0, 14865.0), // 第二根阴线，收盘越过阳线开盘
            bar(14865.0, 14870.0, 14830.0, 14840.0), // 触发
        ];

        let sc = evaluate_signal(&bars, &atr, &p, &trend60());
        assert_eq!(sc.warning, Some(4));
        assert_eq!(sc.warning_kind, "cumulative");
        assert_eq!(sc.trigger, Some(5));
        assert_eq!(sc.state, "当前已触发");
        assert!(sc.total <= 3.49);
        assert!(sc.category.contains("小仓试错"));
        assert!(sc.note.contains("累积确认"));
    }

    #[test]
    fn strong_single_reversal_at_b_end_is_not_downgraded() {
        // 做空：b段末强阳线后直接出现吞没阴线，属于合格反转，不降级
        let atr = atrs(5, 40.0);
        let p = NPattern {
            dir: Dir::Down,
            s1: Swing {
                index: 1,
                price: 14805.0,
                is_high: false,
            },
            s2: Swing {
                index: 2,
                price: 14945.0,
                is_high: true,
            },
            ..pattern()
        };
        let bars = vec![
            bar(15090.0, 15090.0, 15080.0, 15085.0),
            bar(14900.0, 14910.0, 14805.0, 14810.0),
            bar(14870.0, 14945.0, 14870.0, 14940.0), // s2 强趋势阳线
            bar(14940.0, 14942.0, 14860.0, 14865.0), // 吞没阴线 → 合格预警
            bar(14855.0, 14860.0, 14820.0, 14830.0), // 触发
        ];

        let sc = evaluate_signal(&bars, &atr, &p, &trend60());
        assert_eq!(sc.warning, Some(3));
        assert_eq!(sc.warning_kind, "strong");
        assert_eq!(sc.trigger, Some(4));
        assert!(!sc.note.contains("累积确认"));
        assert!(!sc.note.contains("触发分已相应扣减"));
    }

    #[test]
    fn merged_strong_rejects_reverse_shadow_at_or_over_half_range() {
        let atr = atrs(2, 100.0);
        let prev_up = bar(100.0, 110.0, 90.0, 91.0);
        let prev_down = bar(82.0, 100.0, 80.0, 100.0);

        // 实体 25 = 0.25 ATR，恰好过吞没强反转实体门槛。
        let up_clean = vec![prev_up.clone(), bar(88.0, 113.0, 87.0, 113.0)];
        assert_eq!(
            single_reversal_pattern(&up_clean, &atr, Dir::Up, 1, 1),
            Some(SingleReversalKind::Strong)
        );
        let up_exact = vec![prev_up.clone(), bar(90.0, 111.0, 89.0, 100.0)];
        assert_eq!(
            single_reversal_pattern(&up_exact, &atr, Dir::Up, 1, 1),
            None
        );
        let up_over = vec![prev_up, bar(90.0, 112.0, 89.0, 100.0)];
        assert_eq!(single_reversal_pattern(&up_over, &atr, Dir::Up, 1, 1), None);

        let down_clean = vec![prev_down.clone(), bar(110.0, 110.0, 61.0, 80.0)];
        assert_eq!(
            single_reversal_pattern(&down_clean, &atr, Dir::Down, 1, 1),
            Some(SingleReversalKind::Strong)
        );
        let down_exact = vec![prev_down.clone(), bar(110.0, 110.0, 50.0, 80.0)];
        assert_eq!(
            single_reversal_pattern(&down_exact, &atr, Dir::Down, 1, 1),
            None
        );
        let down_over = vec![prev_down, bar(110.0, 110.0, 40.0, 80.0)];
        assert_eq!(
            single_reversal_pattern(&down_over, &atr, Dir::Down, 1, 1),
            None
        );
    }

    #[test]
    fn strong_trend_candle_without_engulf_is_not_strong() {
        // SA0 1154 14:00 对照：O971 H971 L966 C967，ATR20=5，
        // 收盘 967 高于前一根开盘 964，方向性强但没有吞没前一根实体，
        // 不再单独作为 strong 预警。
        let atr = atrs(2, 5.0);
        let bars = vec![
            bar(964.0, 971.0, 960.0, 970.0), // 前一根 b 向阳线
            bar(971.0, 971.0, 966.0, 967.0), // 强趋势阴线，未吞没
        ];
        assert_eq!(single_reversal_pattern(&bars, &atr, Dir::Down, 1, 1), None);
    }

    #[test]
    fn weak_engulf_body_is_not_strong_reversal() {
        // L0 944 22:45 这类：几何上吞没前一根，但实体只有约 0.06 ATR，
        // 不能因为“包住前实体”就按强反转计。
        let atr = atrs(2, 36.0);
        let prev_down = bar(8012.0, 8019.0, 7997.0, 8011.0);
        let weak_up = vec![prev_down, bar(8011.0, 8015.0, 8002.0, 8013.0)];
        assert_eq!(single_reversal_pattern(&weak_up, &atr, Dir::Up, 1, 1), None);

        // 同样干净的吞没形状，实体达到 0.25 ATR 后仍识别为 strong。
        let strong_up = vec![bar(100.0, 110.0, 90.0, 91.0), bar(88.0, 136.0, 87.0, 135.0)];
        assert_eq!(
            single_reversal_pattern(&strong_up, &atr, Dir::Up, 1, 1),
            Some(SingleReversalKind::Strong)
        );

        // 做空镜像：小实体吞没同样不识别为 strong。
        let prev_up = bar(100.0, 101.0, 99.0, 100.9);
        let weak_down = vec![prev_up, bar(100.9, 101.0, 99.7, 99.9)];
        assert_eq!(
            single_reversal_pattern(&weak_down, &atr, Dir::Down, 1, 1),
            None
        );
    }

    #[test]
    fn weak_b_end_keeps_original_warning_behavior() {
        // b段末普通阳线后直接出现干净吞没阴线，仍作为 strong 预警。
        let atr = atrs(4, 40.0);
        let p = NPattern {
            dir: Dir::Down,
            s1: Swing {
                index: 1,
                price: 14805.0,
                is_high: false,
            },
            s2: Swing {
                index: 2,
                price: 14900.0,
                is_high: true,
            },
            ..pattern()
        };
        let bars = vec![
            bar(15090.0, 15090.0, 15080.0, 15085.0),
            bar(14900.0, 14910.0, 14805.0, 14810.0),
            bar(14870.0, 14900.0, 14870.0, 14890.0), // s2 普通阳线（非强趋势）
            bar(14890.0, 14890.0, 14860.0, 14865.0), // 吞没阴线 → 预警
        ];

        let sc = evaluate_signal(&bars, &atr, &p, &trend60());
        assert_eq!(sc.warning, Some(3));
        assert_eq!(sc.warning_kind, "strong");
    }

    #[test]
    fn long_upper_wick_at_b_end_is_the_warning_itself() {
        // 做空（JD0场景）：b段末长上影线（收阴）本身就是预警，入场参考该K线低点
        let atr = atrs(4, 15.4);
        let p = NPattern {
            dir: Dir::Down,
            s1: Swing {
                index: 1,
                price: 3996.0,
                is_high: false,
            },
            s2: Swing {
                index: 2,
                price: 4030.0,
                is_high: true,
            },
            ..pattern()
        };
        let bars = vec![
            bar(4066.0, 4066.0, 4055.0, 4060.0), // s0 高点
            bar(4050.0, 4060.0, 4000.0, 4005.0), // s1 低点，振幅足够支撑上影线
            bar(4023.0, 4055.0, 4020.0, 4021.0), // s2 长上影线，反向影线仅占振幅约3%
            bar(4021.0, 4021.0, 4002.0, 4007.0), // 触发：跌破s2低点
        ];

        let sc = evaluate_signal(&bars, &atr, &p, &trend60());
        assert_eq!(sc.warning, Some(2));
        assert_eq!(sc.warning_kind, "wick");
        assert_eq!(sc.trigger, Some(3));
        assert_eq!(sc.entry, 4019.0);
        assert_eq!(sc.state, "当前已触发");
        assert_eq!(sc.warning_kind, "wick");
        assert_eq!(
            sc.total,
            entry_score(sc.dim_a, sc.dim_b, sc.dim_warning, sc.warning_kind)
        );
        assert!(!sc.note.contains("累积确认"));
    }

    #[test]
    fn wick_warning_requires_all_hard_gates() {
        // 前一根 b 向K线振幅足够，主影线正好为其 50% 时放行
        let prev = bar(100.0, 118.0, 100.0, 117.0);
        // 反向影线正好 10% 时放行（上下影方向对称）
        let short_clean = bar(100.0, 109.0, 99.0, 99.0);
        let long_clean = bar(100.0, 101.0, 91.0, 101.0);
        assert!(is_wick_warning_bar(
            &short_clean,
            10.0,
            Dir::Down,
            Some(&prev)
        ));
        assert!(is_wick_warning_bar(&long_clean, 10.0, Dir::Up, Some(&prev)));

        // 反向影线超过 10% 直接不识别
        let short_reverse_heavy = bar(100.0, 107.0, 97.0, 99.0);
        let long_reverse_heavy = bar(100.0, 103.0, 93.0, 101.0);
        assert!(!is_wick_warning_bar(
            &short_reverse_heavy,
            10.0,
            Dir::Down,
            Some(&prev)
        ));
        assert!(!is_wick_warning_bar(
            &long_reverse_heavy,
            10.0,
            Dir::Up,
            Some(&prev)
        ));

        // 实体为 0 的十字星不识别
        let doji = bar(100.0, 110.0, 90.0, 100.0);
        assert!(!is_wick_warning_bar(&doji, 10.0, Dir::Up, Some(&prev)));
        assert!(!is_wick_warning_bar(&doji, 10.0, Dir::Down, Some(&prev)));

        // 主影线不足 3 倍实体不识别
        let short_body_heavy = bar(100.0, 120.0, 88.0, 90.0);
        let long_body_heavy = bar(90.0, 102.0, 80.0, 100.0);
        assert!(!is_wick_warning_bar(
            &short_body_heavy,
            30.0,
            Dir::Down,
            Some(&prev)
        ));
        assert!(!is_wick_warning_bar(
            &long_body_heavy,
            30.0,
            Dir::Up,
            Some(&prev)
        ));
    }

    #[test]
    fn wick_requires_prev_b_bar_range_at_least_half_amplitude() {
        // PB0 178 复盘对照：21:45 大阴线振幅 60，22:00 下影 20，
        // 主影线只到前一根振幅的 1/3，不再识别为 wick。
        let prev_small = bar(15715.0, 15715.0, 15655.0, 15675.0);
        let pb0_178 = bar(15675.0, 15680.0, 15655.0, 15680.0);
        assert!(!is_wick_warning_bar(
            &pb0_178,
            32.0,
            Dir::Up,
            Some(&prev_small)
        ));

        // 前一根振幅放大到 40（主影线恰好为其 50%）时，其他门槛全过则识别
        let prev_ok = bar(15715.0, 15715.0, 15675.0, 15695.0);
        assert!(is_wick_warning_bar(&pb0_178, 32.0, Dir::Up, Some(&prev_ok)));

        // 前一根缺失同样不识别
        assert!(!is_wick_warning_bar(&pb0_178, 32.0, Dir::Up, None));
    }

    #[test]
    fn wick_close_direction_is_a_small_quality_downgrade_not_a_hard_gate() {
        // 方向契合不扣分：做多收阳、做空收阴
        assert_eq!(
            wick_direction_penalty(&bar(100.0, 101.0, 91.0, 101.0), Dir::Up),
            0.0
        );
        assert_eq!(
            wick_direction_penalty(&bar(100.0, 109.0, 99.0, 99.0), Dir::Down),
            0.0
        );
        // 方向不契合只轻扣 0.1：做多收阴、做空收阳
        assert_eq!(
            wick_direction_penalty(&bar(100.0, 101.0, 91.0, 99.0), Dir::Up),
            0.1
        );
        assert_eq!(
            wick_direction_penalty(&bar(100.0, 109.0, 99.0, 101.0), Dir::Down),
            0.1
        );
    }

    #[test]
    fn dim_warning_applies_wick_direction_penalty_once() {
        let atr = atrs(4, 15.0);
        let p = pattern();
        let clean_bars = vec![
            bar(0.0, 1.0, 0.0, 0.5),
            bar(0.5, 1.0, 0.4, 0.6),
            bar(100.0, 110.0, 90.0, 101.0),
            bar(101.0, 102.0, 100.0, 101.5),
        ];
        let penalized_bars = vec![
            bar(0.0, 1.0, 0.0, 0.5),
            bar(0.5, 1.0, 0.4, 0.6),
            bar(100.0, 110.0, 90.0, 99.0),
            bar(101.0, 102.0, 100.0, 101.5),
        ];

        let clean = dim_warning(&clean_bars, &atr, 2, "wick", p.dir);
        let penalized = dim_warning(&penalized_bars, &atr, 2, "wick", p.dir);
        assert!((clean - penalized - 0.1).abs() < 1e-9);
        assert_eq!(wick_direction_penalty(&penalized_bars[2], p.dir), 0.1);
    }

    #[test]
    fn warning_size_penalty_uses_atr_relative_range() {
        let atr20 = atrs(3, 10.0);
        let bars = vec![
            bar(0.0, 10.0, 0.0, 5.0),
            bar(0.0, 10.0, 0.0, 5.0),
            bar(0.0, 10.0, 0.0, 5.0),
        ];
        let mut start = bars.clone();
        start[2] = bar(0.0, 20.0, 0.0, 10.0);
        let mut half = bars.clone();
        half[2] = bar(0.0, 27.5, 0.0, 10.0);
        let mut full = bars.clone();
        full[2] = bar(0.0, 35.0, 0.0, 10.0);
        assert!((warning_size_penalty(&bars, &atr20, 2) - 0.0).abs() < 1e-9);
        assert!((warning_size_penalty(&start, &atr20, 2) - 0.0).abs() < 1e-9);
        assert!((warning_size_penalty(&half, &atr20, 2) - 0.5).abs() < 1e-9);
        assert!((warning_size_penalty(&full, &atr20, 2) - 1.0).abs() < 1e-9);
    }

    #[test]
    fn long_upper_wick_with_bullish_close_counts_as_short_warning() {
        // 做空：b段末长上影线即使收阳，也算做空预警
        let atr = atrs(4, 15.4);
        let p = NPattern {
            dir: Dir::Down,
            s1: Swing {
                index: 1,
                price: 3996.0,
                is_high: false,
            },
            s2: Swing {
                index: 2,
                price: 4030.0,
                is_high: true,
            },
            ..pattern()
        };
        let bars = vec![
            bar(4066.0, 4066.0, 4055.0, 4060.0),
            bar(4000.0, 4010.0, 3999.0, 4008.0), // 上涨K线
            bar(4010.0, 4030.0, 4009.0, 4012.0), // s2 收阳长上影线，收盘贴近低点
            bar(4015.0, 4016.0, 3995.0, 4000.0), // 触发：跌破s2低点
        ];

        let sc = evaluate_signal(&bars, &atr, &p, &trend60());
        assert_eq!(sc.warning, Some(2));
        assert_eq!(sc.trigger, Some(3));
        assert_eq!(sc.entry, 4008.0);
    }

    #[test]
    fn long_lower_wick_with_bullish_close_counts_as_long_warning() {
        // 做多：b段末长下影线即使收阳，也算做多预警
        let atr = atrs(4, 15.4);
        let p = NPattern {
            dir: Dir::Up,
            s1: Swing {
                index: 1,
                price: 4025.0,
                is_high: true,
            },
            s2: Swing {
                index: 2,
                price: 3995.0,
                is_high: false,
            },
            ..pattern()
        };
        let bars = vec![
            bar(3985.0, 3990.0, 3985.0, 3988.0), // s0 低点
            bar(4025.0, 4026.0, 4020.0, 4021.0), // s1 高点
            bar(4010.0, 4015.0, 3995.0, 4013.0), // s2 收阳长下影线
            bar(4013.0, 4025.0, 4012.0, 4022.0), // 触发：突破s2高点
        ];

        let sc = evaluate_signal(&bars, &atr, &p, &trend60());
        assert_eq!(sc.warning, Some(2));
        assert_eq!(sc.trigger, Some(3));
        assert_eq!(sc.entry, 4016.0);
        assert_eq!(sc.state, "当前已触发");
    }

    #[test]
    fn long_lower_wick_with_bearish_close_counts_as_long_warning() {
        // 做多：b段末长下影线即使收阴，也算做多预警
        let atr = atrs(4, 15.4);
        let p = NPattern {
            dir: Dir::Up,
            s1: Swing {
                index: 1,
                price: 4025.0,
                is_high: true,
            },
            s2: Swing {
                index: 2,
                price: 3990.0,
                is_high: false,
            },
            ..pattern()
        };
        let bars = vec![
            bar(3985.0, 3990.0, 3985.0, 3988.0),
            bar(4010.0, 4015.0, 4008.0, 4014.0), // 上涨K线
            bar(4012.0, 4013.0, 3990.0, 4011.0), // s2 收阴锤子线（收盘贴近高点、长下影）
            bar(4011.0, 4020.0, 4010.0, 4018.0), // 触发：突破s2高点
        ];

        let sc = evaluate_signal(&bars, &atr, &p, &trend60());
        assert_eq!(sc.warning, Some(2));
        assert_eq!(sc.trigger, Some(3));
        assert_eq!(sc.entry, 4014.0);
    }

    #[test]
    fn b_grade_requires_qualified_reversal_warning() {
        // 方案B：B级结构即使b段顶不是强趋势K，小阴线也不能直接当预警
        let atr = atrs(5, 40.0);
        let p = NPattern {
            dir: Dir::Down,
            grade: Grade::B,
            s1: Swing {
                index: 1,
                price: 14805.0,
                is_high: false,
            },
            s2: Swing {
                index: 2,
                price: 14900.0,
                is_high: true,
            },
            ..pattern()
        };
        let bars = vec![
            bar(15090.0, 15090.0, 15080.0, 15085.0),
            bar(14900.0, 14910.0, 14805.0, 14810.0),
            bar(14870.0, 14900.0, 14870.0, 14890.0), // s2 普通阳线（非强趋势）
            bar(14890.0, 14892.0, 14870.0, 14880.0), // 小阴线，不构成任何反转形态
            bar(14880.0, 14900.0, 14878.0, 14890.0), // 阳线打断反转段
        ];

        let sc = evaluate_signal(&bars, &atr, &p, &trend60());
        assert_eq!(sc.state, "等待预警");
        assert_eq!(sc.warning, None);
        assert!(sc.note.contains("B/C级"));
    }

    #[test]
    fn b_grade_accepts_later_strong_reversal() {
        // 方案B：B级结构等到干净吞没阴线出现后才出预警，拒绝前面的小阴线
        let atr = atrs(7, 40.0);
        let p = NPattern {
            dir: Dir::Down,
            grade: Grade::B,
            s1: Swing {
                index: 1,
                price: 14700.0,
                is_high: false,
            },
            s2: Swing {
                index: 2,
                price: 14900.0,
                is_high: true,
            },
            ..pattern()
        };
        let bars = vec![
            bar(15090.0, 15090.0, 15080.0, 15085.0),
            bar(14900.0, 14910.0, 14805.0, 14810.0),
            bar(14870.0, 14900.0, 14870.0, 14890.0), // s2 普通阳线
            bar(14890.0, 14892.0, 14870.0, 14880.0), // 小阴线，不合格
            bar(14880.0, 14890.0, 14878.0, 14886.0), // 阳线打断反转段
            bar(14886.0, 14886.0, 14860.0, 14865.0), // 干净吞没阴线 → 合格预警
            bar(14805.0, 14810.0, 14760.0, 14770.0), // 触发
        ];

        let sc = evaluate_signal(&bars, &atr, &p, &trend60());
        assert_eq!(sc.warning, Some(5));
        assert_eq!(sc.warning_kind, "strong");
        assert_eq!(sc.trigger, Some(6));
        assert_eq!(sc.entry, 14859.0);
        assert!(!sc.note.contains("累积确认"));
    }

    #[test]
    fn b_grade_cumulative_reversal_downgrades_to_small_position() {
        // 方案B：B级结构靠多K累积覆盖确认时允许预警，但总分封顶在小仓试错
        let atr = atrs(6, 40.0);
        let p = NPattern {
            dir: Dir::Down,
            grade: Grade::B,
            s1: Swing {
                index: 1,
                price: 14700.0,
                is_high: false,
            },
            s2: Swing {
                index: 2,
                price: 14900.0,
                is_high: true,
            },
            ..pattern()
        };
        let bars = vec![
            bar(15090.0, 15090.0, 15080.0, 15085.0),
            bar(14900.0, 14910.0, 14805.0, 14810.0),
            bar(14870.0, 14900.0, 14870.0, 14890.0), // s2 普通阳线
            bar(14890.0, 14892.0, 14870.0, 14880.0), // 小阴线1
            bar(14880.0, 14885.0, 14850.0, 14855.0), // 小阴线2，收盘越过s2开盘 → 累积覆盖
            bar(14855.0, 14860.0, 14810.0, 14820.0), // 触发
        ];

        let sc = evaluate_signal(&bars, &atr, &p, &trend60());
        assert_eq!(sc.warning, Some(4));
        assert_eq!(sc.trigger, Some(5));
        assert!(sc.total <= 3.49);
        assert!(sc.note.contains("累积确认"));
    }

    #[test]
    fn a_grade_weak_bullish_candles_do_not_warn_until_strong_reversal() {
        // A级不接受"小实体+长上影"的阳线做多预警，收盘位置合格但
        // 未达强反转/长影线门槛的普通K线同样不再单独放行，继续等干净吞没。
        let atr = atrs(7, 30.0);
        let p = NPattern {
            dir: Dir::Up,
            s1: Swing {
                index: 1,
                price: 15090.0,
                is_high: true,
            },
            s2: Swing {
                index: 2,
                price: 14852.0,
                is_high: false,
            },
            ..pattern()
        };
        let bars = vec![
            bar(14900.0, 14910.0, 14890.0, 14900.0),
            bar(15080.0, 15090.0, 15070.0, 15085.0), // s1 高点
            bar(14885.0, 14895.0, 14852.0, 14865.0), // s2 普通阴线（b段低点，不构成强反向实体）
            bar(14835.0, 14855.0, 14830.0, 14840.0), // 小阳线+长上影，不再单独预警
            bar(14855.0, 14860.0, 14840.0, 14845.0), // 小阴线：非强反转/长影线
            bar(14840.0, 14860.0, 14835.0, 14860.0), // 干净吞没阳线 → 预警
            bar(14855.0, 14890.0, 14853.0, 14885.0), // 触发
        ];

        let sc = evaluate_signal(&bars, &atr, &p, &trend60());
        assert_eq!(sc.warning, Some(5));
        assert_eq!(sc.warning_kind, "strong");
        assert_eq!(sc.trigger, Some(6));
        assert_eq!(sc.entry, 14861.0);
    }

    #[test]
    fn strong_anchor_blocks_weak_reversal_candles() {
        // SF0场景：b端大阴线实体/振幅够大但收盘位0.75（未达严格趋势K的0.80），
        // 强锚口径统一后仍判为“强反向实体”，小阳线不能单独预警。
        let atr = atrs(6, 40.0);
        let p = NPattern {
            dir: Dir::Up,
            s1: Swing {
                index: 1,
                price: 5910.0,
                is_high: true,
            },
            s2: Swing {
                index: 2,
                price: 5856.0,
                is_high: false,
            },
            ..pattern()
        };
        let bars = vec![
            bar(5600.0, 5610.0, 5590.0, 5605.0), // s0 低点
            bar(5900.0, 5910.0, 5890.0, 5905.0), // s1 高点
            bar(5896.0, 5896.0, 5856.0, 5866.0), // s2 大阴线：实体75%、收盘位0.75（未到严格线）
            bar(5864.0, 5878.0, 5862.0, 5874.0), // 小阳线：弱K不再单独预警，强锚门持续生效
            bar(5874.0, 5884.0, 5872.0, 5878.0), // 小阳线：无合格反转
            bar(5878.0, 5882.0, 5874.0, 5880.0), // 小阳线：无合格反转，未收复锚定开盘5896
        ];

        let sc = evaluate_signal(&bars, &atr, &p, &trend60());
        assert_eq!(sc.warning, None);
        assert_eq!(sc.state, "等待预警");
        assert!(sc.note.contains("强反向实体"));
    }

    #[test]
    fn a_grade_weak_reversal_candles_do_not_warn() {
        // bu0案例：s2为十字星、后续弱阳线不再单独预警，等待真正的反转K线
        let atr = atrs(5, 15.0);
        let p = NPattern {
            dir: Dir::Up,
            s1: Swing {
                index: 1,
                price: 4175.0,
                is_high: true,
            },
            s2: Swing {
                index: 2,
                price: 4141.0,
                is_high: false,
            },
            ..pattern()
        };
        let bars = vec![
            bar(4137.0, 4140.0, 4136.0, 4138.0),
            bar(4172.0, 4175.0, 4168.0, 4173.0), // s1 高点
            bar(4145.0, 4153.0, 4141.0, 4145.0), // s2 十字星
            bar(4146.0, 4158.0, 4144.0, 4148.0), // 小阳线+长上影 → 不合格
            bar(4149.0, 4151.0, 4143.0, 4144.0), // 阴线打断反转段
        ];

        let sc = evaluate_signal(&bars, &atr, &p, &trend60());
        assert_eq!(sc.warning, None);
        assert_eq!(sc.warning_kind, "none");
        assert_eq!(sc.state, "等待预警");
    }

    #[test]
    fn a_grade_weak_candle_waits_for_strong_reversal() {
        // A级普通b端：弱阳线/阴线不算强反转/长影线时不再预警，
        // 等到干净吞没阳线出现才出 strong 预警。
        let atr = atrs(6, 40.0);
        let p = NPattern {
            dir: Dir::Up,
            s1: Swing {
                index: 1,
                price: 5910.0,
                is_high: true,
            },
            s2: Swing {
                index: 2,
                price: 5856.0,
                is_high: false,
            },
            ..pattern()
        };
        let bars = vec![
            bar(5600.0, 5610.0, 5590.0, 5605.0), // s0 低点
            bar(5900.0, 5910.0, 5890.0, 5905.0), // s1 高点
            bar(5880.0, 5890.0, 5856.0, 5866.0), // s2 普通阴线（非强锚）
            bar(5866.0, 5878.0, 5862.0, 5864.0), // 小阴线：不再单独预警
            bar(5862.0, 5900.0, 5856.0, 5896.0), // 干净吞没阳线 → 预警
            bar(5896.0, 5912.0, 5894.0, 5908.0), // 触发
        ];

        let sc = evaluate_signal(&bars, &atr, &p, &trend60());
        assert_eq!(sc.warning, Some(4));
        assert_eq!(sc.warning_kind, "strong");
        assert_eq!(sc.warning_quality_points(), 0.3);
        assert_eq!(sc.trigger, Some(5));
    }

    #[test]
    fn weak_engulf_does_not_warn_without_strong_form() {
        // L0 944 22:45 对照：小实体吞没不再按 strong 计，
        // 也不再有快速路径兜底，继续等强反转/长影线。
        let atr = atrs(5, 36.0);
        let p = NPattern {
            dir: Dir::Up,
            s1: Swing {
                index: 1,
                price: 8091.0,
                is_high: true,
            },
            s2: Swing {
                index: 2,
                price: 8002.0,
                is_high: false,
            },
            ..pattern()
        };
        let bars = vec![
            bar(7806.0, 7824.0, 7800.0, 7820.0), // s0 低点
            bar(8012.0, 8019.0, 7997.0, 8011.0), // s1 高点后普通阴线（弱锚）
            bar(8011.0, 8015.0, 8002.0, 8011.0), // s2 普通阴线（弱锚）
            bar(8008.0, 8017.0, 8008.0, 8016.0), // 小实体吞没：不够 strong，也不走快速路径
            bar(8015.0, 8023.0, 8013.0, 8021.0), // 突破预警高点
        ];

        let sc = evaluate_signal(&bars, &atr, &p, &trend60());
        assert_eq!(sc.warning, None);
        assert_eq!(sc.warning_kind, "none");
        assert_eq!(sc.state, "等待预警");
    }

    #[test]
    fn weak_reversal_candle_waits_for_strong_confirm() {
        // 同一根小阳线：b段偏长时仍保持等待，只有强反转/长影线等自证形态才能预警。
        let atr = atrs(6, 40.0);
        let p = NPattern {
            dir: Dir::Up,
            s1: Swing {
                index: 1,
                price: 5910.0,
                is_high: true,
            },
            s2: Swing {
                index: 2,
                price: 5856.0,
                is_high: false,
            },
            b_bars: 12,
            b_too_long: true,
            ..pattern()
        };
        let bars = vec![
            bar(5600.0, 5610.0, 5590.0, 5605.0), // s0 低点
            bar(5900.0, 5910.0, 5890.0, 5905.0), // s1 高点
            bar(5880.0, 5890.0, 5856.0, 5866.0), // s2 普通阴线（非强锚）
            bar(5864.0, 5878.0, 5862.0, 5874.0), // 小阳线：不再单独预警
            bar(5874.0, 5878.0, 5870.0, 5872.0), // 阴线打断反转段
        ];

        let sc = evaluate_signal(&bars, &atr, &p, &trend60());
        assert_eq!(sc.warning, None);
        assert_eq!(sc.warning_kind, "none");
        assert_eq!(sc.state, "等待预警");
    }

    #[test]
    fn entry_score_uses_60_20_20_and_caps_cumulative() {
        let base = entry_score(5.0, 5.0, 3.0, "cumulative");
        assert!((base - CUMULATIVE_ENTRY_SCORE_MAX).abs() < 1e-9);

        let strong = entry_score(4.0, 4.0, 3.5, "strong");
        assert!((strong - 3.9).abs() < 1e-9);

        // 长影线预警总分封顶 3.0，不进 3.5+ 标准仓区间。
        let wick = entry_score(4.0, 4.0, 3.5, "wick");
        assert!((wick - WICK_ENTRY_SCORE_MAX).abs() < 1e-9);

        // 强反转按原权重计算，不额外封顶。
        let weighted = entry_score(3.0, 4.0, 3.0, "strong");
        assert!((weighted - 3.2).abs() < 1e-9);
    }

    #[test]
    fn entry_offsets_by_symbol_tick() {
        let atr = atrs(4, 15.4);
        let p = NPattern {
            dir: Dir::Up,
            s1: Swing {
                index: 1,
                price: 4025.0,
                is_high: true,
            },
            s2: Swing {
                index: 2,
                price: 3995.0,
                is_high: false,
            },
            ..pattern()
        };
        let bars = vec![
            bar(3985.0, 3990.0, 3985.0, 3988.0),
            bar(4025.0, 4026.0, 4020.0, 4021.0), // s1 高点
            bar(4010.0, 4015.0, 3995.0, 4013.0), // s2 收阳长下影线（预警）
            bar(4013.0, 4025.0, 4012.0, 4022.0), // 触发：突破s2高点
        ];
        // 默认 tick=1：入场 = 预警高点 + 1
        assert_eq!(evaluate_signal(&bars, &atr, &p, &trend60()).entry, 4016.0);
        // 0.5 tick：入场 = 预警高点 + 0.5
        assert_eq!(
            evaluate_signal_with_tick(&bars, &atr, &p, &trend60(), 0.5).entry,
            4015.5
        );
    }
}
