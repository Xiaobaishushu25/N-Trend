use crate::analyze::indicators;
use crate::analyze::model::{Bar, Dir, Grade, NPattern, SignalCheck, Trend60};

const STOP_FOLLOW_MIN_AGE: usize = 3;
const STOP_FOLLOW_MAX_AGE: usize = 6;
const STOP_FOLLOW_DISTANCE_RISK: f64 = 1.0;
// 未触发信号的预警K线最大存活根数，超过视为过时
const PENDING_MAX_AGE: usize = 12;
const OPPOSING_WICK_ATR_MIN: f64 = 0.25;
const OPPOSING_WICK_RANGE_MIN: f64 = 0.50;
const OPPOSING_PREV_RANGE_ATR_MIN: f64 = 0.80;
const OPPOSING_PREV_BODY_ATR_MIN: f64 = 0.50;
const ENTRY_BLOCK_TRIGGER_PENALTY: f64 = 0.70;
const ENTRY_BLOCK_MOMENTUM_PENALTY: f64 = 0.35;
const TRIGGER_OPPOSITION_PENALTY_MAX: f64 = 2.00;
// ===== a段质量分标定参数 =====
// 幅度满档：a_move 达到 10 倍 ATR 即得满分。
// 幅度不足的短腿自然低分，不另设“最少根数”之类的硬门槛，避免针对个案。
const A_LEG_AMPLITUDE_ATR_FULL: f64 = 10.0;
// 强趋势K密度基准：长腿按 35% 密度折算；短腿保底 2 根。
// 这样“长而干净”（大量光头阳线/光脚阴线）的腿仍能得高分，
// 只有强K密度低的震荡/重叠腿被压低。
const A_LEG_STRONG_DENSITY_BASE: f64 = 0.35;
const A_LEG_STRONG_MIN_COUNT: f64 = 2.0;
// 长度扣分：a 段过长消耗动能，分两档轻扣；只扣“动能”，不与质量混为一谈。
const A_LEG_LONG_PENALTY_MIN_BARS: usize = 24;
const A_LEG_LONG_PENALTY_LEVEL1: f64 = -0.4;
const A_LEG_LONG_PENALTY_MAX_BARS: usize = 32;
const A_LEG_LONG_PENALTY_LEVEL2: f64 = -0.8;
// b段终点确认：当反转段前一根K线是强b向趋势K时，单根弱反向K线不能确认b段结束
const WEAK_CONFIRM_TRIGGER_PENALTY: f64 = 1.0;
const WEAK_CONFIRM_MOMENTUM_PENALTY: f64 = 1.0;
// 弱确认信号总分上限：只允许小仓试错，不进入标准仓区间
const WEAK_CONFIRM_TOTAL_MAX: f64 = 3.49;
// 影线预警的收盘位置上限：做空上影线须收盘于振幅下1/3内，做多下影线须收盘于上1/3内
const WICK_CLOSE_POS_MAX: f64 = 0.35;
// 长影线预警的“反向影线”占比门槛：做空看下影，做多看上影。
// 反向影线过短视为光脚长影线，不扣；偏长会削弱反转推力，触发维度相应扣分。
const WICK_REVERSE_SHADOW_MEDIUM_RATIO: f64 = 0.10;
const WICK_REVERSE_SHADOW_HEAVY_RATIO: f64 = 0.20;
const WICK_REVERSE_SHADOW_MEDIUM_PENALTY: f64 = 0.30;
const WICK_REVERSE_SHADOW_HEAVY_PENALTY: f64 = 0.50;

fn clamp(v: f64) -> f64 {
    v.clamp(0.0, 5.0)
}

fn atr_at(atr20: &[Option<f64>], index: usize) -> f64 {
    atr20.get(index).and_then(|x| *x).unwrap_or(1.0)
}

fn score_60m(trend: &Trend60, dir: Dir) -> f64 {
    if trend.aligned_with(dir) {
        if trend.strong() {
            4.5
        } else {
            3.0
        }
    } else if trend.opposite_to(dir) {
        if trend.strong() {
            0.5
        } else {
            1.0
        }
    } else {
        2.0
    }
}

/// a段质量分：衡量“推动腿”的幅度与K线质量，采用乘法短板结构。
///
/// 公式：
///   dim_a = 0.5 + 4.5 × min(1, a_move/(10·ATR)) × min(1, 强趋势K/密度基准) + 长度扣分
///
/// 设计要点（避免“各项中等、凑满高分”的加法漏洞）：
/// 1. 幅度与强K质量相乘，任何一项弱都会按比例压低总分；
/// 2. 强趋势K按密度折算：长腿按 35% 基准放宽，短腿保底 2 根——
///    长而干净的腿（大量光头阳线/光脚阴线）仍得高分，震荡/重叠腿被压低；
/// 3. 长度单独作为“动能消耗”轻扣，不与质量混为一谈。
fn score_a(bars: &[Bar], atr20: &[Option<f64>], p: &NPattern) -> f64 {
    let atr = atr_at(atr20, p.s1.index);
    let strong = a_leg_relaxed_strong(bars, atr20, p);
    a_leg_score_formula(p.a_move, p.a_bars, strong, atr)
}

/// a段内“形态方向强趋势K”的数量（宽松口径，与识别阶段的 a_leg_strong_count 一致）。
///
/// 注意：不能直接使用 NPattern.a_strong_trend——那是 make_pattern 里按严格趋势K
/// 统计的，会把大量“光头阳线/光脚阴线”级别的干净推进漏掉，导致长而干净的腿被低估。
fn a_leg_relaxed_strong(bars: &[Bar], atr20: &[Option<f64>], p: &NPattern) -> usize {
    let flags = indicators::trend_flags_relaxed(bars, atr20);
    let mut count = 0;
    for i in p.s0.index + 1..=p.s1.index {
        match p.dir {
            Dir::Down if flags.get(i).is_some_and(|f| f.1) => count += 1,
            Dir::Up if flags.get(i).is_some_and(|f| f.0) => count += 1,
            _ => {}
        }
    }
    count
}

/// a段质量分的纯公式（抽出便于单测与标定）。
fn a_leg_score_formula(a_move: f64, a_bars: usize, strong: usize, atr: f64) -> f64 {
    if atr <= 0.0 {
        return 0.0;
    }
    // 幅度分：a_move 达到 10 倍 ATR 视为满档（1.0）。
    let amplitude = (a_move / (A_LEG_AMPLITUDE_ATR_FULL * atr)).min(1.0);
    // 强趋势K密度分：短腿保底 2 根，长腿按 35% 密度折算。
    let density_floor = (A_LEG_STRONG_DENSITY_BASE * a_bars as f64).max(A_LEG_STRONG_MIN_COUNT);
    let quality = (strong as f64 / density_floor).min(1.0);
    // 长度扣分：a 段过长消耗动能，分两档轻扣。
    let length_penalty = if a_bars > A_LEG_LONG_PENALTY_MAX_BARS {
        A_LEG_LONG_PENALTY_LEVEL2
    } else if a_bars > A_LEG_LONG_PENALTY_MIN_BARS {
        A_LEG_LONG_PENALTY_LEVEL1
    } else {
        0.0
    };
    clamp(0.5 + 4.5 * amplitude * quality + length_penalty)
}

fn score_b(p: &NPattern) -> f64 {
    let mut s = p.grade.score_base();
    if p.b_fast && p.grade != Grade::C {
        s -= 0.5;
    }
    if p.b_too_long {
        s -= 0.5;
    }
    // 反向强K扣分：健康回撤里第一根反向强K是正常的，不惩罚；
    // 从第 2 根起每根扣 0.3（至多按 2 根计），避免把正常回撤误判为弱结构。
    s -= (p.b_strong_reverse.saturating_sub(1).min(2) as f64) * 0.3;
    clamp(s)
}

fn score_trigger(
    bars: &[Bar],
    atr20: &[Option<f64>],
    warning: Option<usize>,
    trigger: Option<usize>,
    p: &NPattern,
    local_block_count: u8,
    wick_penalty: f64,
) -> f64 {
    match (warning, trigger) {
        (None, _) => 0.0,
        (Some(_), None) => 1.0,
        (Some(w), Some(t)) => {
            let mut s = 3.0;
            let delay = t.saturating_sub(w);
            if delay <= 1 {
                s += 1.0;
            } else if delay <= 3 {
                s += 0.5;
            } else {
                s -= 0.5;
            }
            if p.grade == Grade::A {
                s += 0.2;
            }
            if p.c_extended {
                s -= 0.5;
            }
            s -= ENTRY_BLOCK_TRIGGER_PENALTY * local_block_count as f64;
            s -= trigger_opposition_penalty(bars, atr20, p.dir, p.s2.index, t);
            s -= wick_penalty;
            clamp(s)
        }
    }
}

fn score_rr(rr: f64, momentum: f64, c_extended: bool) -> f64 {
    let base = if rr <= 0.0 { 0.0 } else { (rr * 2.5).min(5.0) };
    if !c_extended && momentum >= 3.5 {
        base.max(2.0)
    } else {
        base
    }
}

fn score_momentum(
    p: &NPattern,
    trend: &Trend60,
    atr20: &[Option<f64>],
    entry_block_count: u8,
) -> f64 {
    let atr = atr_at(atr20, p.s2.index);
    let mut s = 2.5;
    if p.c_extended {
        s -= 1.0;
    } else {
        s += 0.5;
    }
    if trend.aligned_with(p.dir) && trend.strong() {
        s += 0.5;
    }
    if p.a_move >= 1.5 * atr {
        s += 0.3;
    }
    if p.b_strong_reverse == 0 {
        s += 0.2;
    }
    s -= ENTRY_BLOCK_MOMENTUM_PENALTY * entry_block_count as f64;
    clamp(s)
}

fn strong_opposite_body_at(
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
            && wick >= OPPOSING_WICK_ATR_MIN * atr
            && wick >= OPPOSING_WICK_RANGE_MIN * range
    });

    let prev_block =
        trigger > 0 && strong_opposite_body_at(bars, atr20, dir, trigger - 1).is_some();

    (wick_block, prev_block)
}

fn trigger_opposition_penalty(
    bars: &[Bar],
    atr20: &[Option<f64>],
    dir: Dir,
    b_end: usize,
    trigger: usize,
) -> f64 {
    let Some(opposing_body) = strong_opposite_body_at(bars, atr20, dir, b_end) else {
        return 0.0;
    };
    let Some(trigger_bar) = bars.get(trigger) else {
        return 0.0;
    };
    let trigger_body = (trigger_bar.close - trigger_bar.open).abs();
    if trigger_body <= 0.0 {
        return TRIGGER_OPPOSITION_PENALTY_MAX;
    }
    let ratio = opposing_body / trigger_body;
    (ratio - 1.0).clamp(0.0, 3.0) * (TRIGGER_OPPOSITION_PENALTY_MAX / 3.0)
}

/// 长影线预警的反向影线质量扣分：做空看下影，做多看上影。
fn long_wick_reverse_shadow_penalty(bars: &[Bar], dir: Dir, w: usize) -> f64 {
    let Some(bar) = bars.get(w) else {
        return 0.0;
    };
    let range = bar.high - bar.low;
    if range <= 0.0 {
        return 0.0;
    }
    let reverse_shadow = match dir {
        Dir::Down => bar.open.min(bar.close) - bar.low,
        Dir::Up => bar.high - bar.open.max(bar.close),
    };
    let ratio = reverse_shadow / range;
    if ratio <= WICK_REVERSE_SHADOW_MEDIUM_RATIO {
        0.0
    } else if ratio <= WICK_REVERSE_SHADOW_HEAVY_RATIO {
        WICK_REVERSE_SHADOW_MEDIUM_PENALTY
    } else {
        WICK_REVERSE_SHADOW_HEAVY_PENALTY
    }
}

#[derive(Clone, Copy, PartialEq)]
enum WarnKind {
    Single,
    Cumulative,
}

/// 单根反转形态的具体类型：区分长影线预警，用于反向影线质量扣分。
#[derive(Clone, Copy, PartialEq)]
enum SingleReversalKind {
    Engulf,
    Strong,
    Wick,
}

fn is_opposite_close(bar: &Bar, dir: Dir) -> bool {
    match dir {
        Dir::Up => bar.close > bar.open,
        Dir::Down => bar.close < bar.open,
    }
}

/// b段方向上的严格趋势K线（做多对应强阴线，做空对应强阳线）
fn strong_b_dir_trend_candle(trend_k: &[(bool, bool)], i: usize, dir: Dir) -> bool {
    trend_k.get(i).is_some_and(|&(up, down)| match dir {
        Dir::Up => down,
        Dir::Down => up,
    })
}

/// 单根K线构成的反转形态：吞没反转段前的b向实体 / 强反向趋势K / 反向长影线
fn single_reversal_pattern(
    bars: &[Bar],
    atr20: &[Option<f64>],
    trend_k: &[(bool, bool)],
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
    let engulf = w == run_start
        && w > 0
        && match dir {
            Dir::Up => {
                let prev = &bars[w - 1];
                prev.close < prev.open
                    && bar.close > bar.open
                    && bar.close >= prev.open
                    && bar.open <= prev.close
                    && (bar.close > prev.open || bar.open < prev.close)
            }
            Dir::Down => {
                let prev = &bars[w - 1];
                prev.close > prev.open
                    && bar.close < bar.open
                    && bar.open >= prev.close
                    && bar.close <= prev.open
                    && (bar.open > prev.close || bar.close < prev.open)
            }
        };
    if engulf {
        return Some(SingleReversalKind::Engulf);
    }

    let strong = match dir {
        Dir::Up => trend_k.get(w).is_some_and(|x| x.0),
        Dir::Down => trend_k.get(w).is_some_and(|x| x.1),
    };
    if strong {
        return Some(SingleReversalKind::Strong);
    }

    let body = (bar.close - bar.open).abs();
    let atr = atr_at(atr20, w);
    let upper = bar.high - bar.open.max(bar.close);
    let lower = bar.open.min(bar.close) - bar.low;
    let wick = match dir {
        Dir::Up => lower,
        Dir::Down => upper,
    };
    // 影线预警还要求收盘位置在反向一端：避免把十字星/中位收盘的小K线误判为长影线
    let close_ok = match dir {
        Dir::Up => (bar.high - bar.close) / range <= WICK_CLOSE_POS_MAX,
        Dir::Down => (bar.close - bar.low) / range <= WICK_CLOSE_POS_MAX,
    };
    (wick > body
        && wick >= OPPOSING_WICK_ATR_MIN * atr
        && wick >= OPPOSING_WICK_RANGE_MIN * range
        && close_ok)
        .then_some(SingleReversalKind::Wick)
}

/// 多K累积覆盖：连续反向收盘至少2根，且最后一根收盘越过强b向K线的开盘价（吞没其实体）
fn cumulative_coverage(
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

/// A级快速路径的最低质量门槛：反向收盘必须落在K线振幅的反向一半内。
/// 排除"小实体+长反向影线"（十字星、射击之星变体）这类收盘被反向力量
/// 压制的K线直接当预警，避免任意一根小阳线/小阴线都被放行。
fn fast_path_close_ok(bars: &[Bar], dir: Dir, i: usize) -> bool {
    let Some(bar) = bars.get(i) else {
        return false;
    };
    let range = bar.high - bar.low;
    if range <= 0.0 {
        return false;
    }
    match dir {
        // 做多预警要求收盘在振幅上半部分（上影不占主导）
        Dir::Up => (bar.high - bar.close) / range <= 0.5,
        // 做空预警要求收盘在振幅下半部分（下影不占主导）
        Dir::Down => (bar.close - bar.low) / range <= 0.5,
    }
}

fn weak_confirm_prefix(weak: bool) -> &'static str {
    if weak {
        "反转仅靠多K累积确认，信号降级为小仓试错；"
    } else {
        ""
    }
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
    if p.c_extended {
        parts.push("c段已透支".to_string());
    }
    if rr > 0.0 && rr < 1.0 {
        parts.push("决策点RR不足1，可按破位预期评估".to_string());
    }
    parts.push("前低/前高为决策点，不必然止盈".to_string());
    parts.join("；")
}

fn compute_scores(
    bars: &[Bar],
    atr20: &[Option<f64>],
    p: &NPattern,
    trend: &Trend60,
    rr: f64,
    warning: Option<usize>,
    trigger: Option<usize>,
    local_block_count: u8,
    entry_block_count: u8,
    weak_confirm: bool,
    wick_penalty: f64,
) -> ([f64; 6], f64) {
    let dim_trend = score_60m(trend, p.dir);
    let dim_a = score_a(bars, atr20, p);
    let dim_b = score_b(p);
    let mut dim_trigger = score_trigger(
        bars,
        atr20,
        warning,
        trigger,
        p,
        local_block_count,
        wick_penalty,
    );
    let mut dim_momentum = score_momentum(p, trend, atr20, entry_block_count);
    if weak_confirm {
        dim_trigger = (dim_trigger - WEAK_CONFIRM_TRIGGER_PENALTY).max(0.0);
        dim_momentum = (dim_momentum - WEAK_CONFIRM_MOMENTUM_PENALTY).max(0.0);
    }
    let dim_rr = score_rr(rr, dim_momentum, p.c_extended);

    let mut total = 0.10 * dim_trend
        + 0.40 * dim_a
        + 0.20 * dim_b
        + 0.15 * dim_trigger
        + 0.05 * dim_rr
        + 0.10 * dim_momentum;
    if weak_confirm {
        total = total.min(WEAK_CONFIRM_TOTAL_MAX);
    }

    (
        [dim_trend, dim_a, dim_b, dim_trigger, dim_rr, dim_momentum],
        total,
    )
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
    let mut sc = SignalCheck::new();

    let end = bars.len().min(p.s2.index + 6);
    if p.s2.index + 1 >= end {
        sc.category = "结构未完成";
        sc.state = "等待后续K线";
        sc.note = "b端后没有可用于预警的K线".to_string();
        return sc;
    }

    // 方案B：B/C级结构按系统文档§6.3要求更严格的反转确认——预警必须是
    // 吞没/强反向趋势K/长影线/多K累积覆盖之一；A级保留快速预警路径，
    // 避免浅回调结构因等待确认而错过入场。
    let strict_confirm = matches!(p.grade, Grade::B | Grade::C);
    // b段终点确认：当反转段前一根K线是强b向趋势K时，单根弱反向K线不足以
    // 确认b段结束，必须出现吞没/长影线/强反向趋势K/多K累积覆盖。
    let trend_k = indicators::trend_flags(bars, atr20);
    let mut warning = None;
    let mut warn_kind = WarnKind::Single;
    let mut warning_is_wick = false;
    let mut gate_active = false;
    let mut gate_anchor_strong = false;
    // s2 本身构成合格反转形态（长影线/吞没/强反向趋势K）时，s2 就是预警K线。
    // 长影线不要求反向收盘：做空时上影线够长即使收阳也算预警，做多方向对称。
    let s2_single = single_reversal_pattern(bars, atr20, &trend_k, p.dir, p.s2.index, p.s2.index);
    if let Some(kind) = s2_single {
        warning = Some(p.s2.index);
        warning_is_wick = kind == SingleReversalKind::Wick;
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
            // A级允许首根反向收盘直接作为预警，但仍要求收盘在反向一半内，
            // 排除小实体+长反向影线的K线；其余情况（强b向趋势K顶、B/C级）
            // 都要求单根反转形态自证。
            let single_now = single_reversal_pattern(bars, atr20, &trend_k, p.dir, j, run_start);
            let single_ok =
                (!anchor_strong && !strict_confirm && fast_path_close_ok(bars, p.dir, j))
                    || single_now.is_some();
            // 多K累积覆盖同样只对需要严格确认的路径开放（连续反向收盘吞没b向实体）。
            let cumul_ok = (anchor_strong || strict_confirm)
                && cumulative_coverage(bars, run_start, j, anchor_open, p.dir);
            if single_ok || cumul_ok {
                warning = Some(j);
                let is_cumulative = cumul_ok && !single_ok;
                warn_kind = if is_cumulative {
                    WarnKind::Cumulative
                } else {
                    WarnKind::Single
                };
                warning_is_wick = !is_cumulative && single_now == Some(SingleReversalKind::Wick);
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
        sc.category = "无预警K线";
        sc.state = "等待预警";
        sc.note = if gate_active {
            if gate_anchor_strong {
                "b段末为强反向实体（强趋势K或大实体），当前反向K线未形成吞没/强反转/累积覆盖形态，等待更强反转确认"
                    .to_string()
            } else {
                "B/C级结构要求反转预警具备吞没/强反向K/长影线/累积覆盖形态，等待更强反转确认"
                    .to_string()
            }
        } else {
            "b端后尚未出现与原方向一致的反转预警".to_string()
        };
        return sc;
    };
    let wick_penalty = if warning_is_wick {
        long_wick_reverse_shadow_penalty(bars, p.dir, w)
    } else {
        0.0
    };
    let weak_confirm = warn_kind == WarnKind::Cumulative;
    sc.warning = Some(w);

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
            sc.note = "b段已经突破a段起点，结构事实失效".to_string();
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
        let (dims, total) = compute_scores(
            bars,
            atr20,
            p,
            trend,
            sc.rr,
            Some(w),
            None,
            0,
            0,
            weak_confirm,
            wick_penalty,
        );
        sc.dims = dims;
        sc.total = total;
        sc.category = score_category(total);

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
    let local_block_count = wick_block as u8 + prev_block as u8;
    let entry_block_count = local_block_count + b_end_block as u8;
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
    let (dims, total) = compute_scores(
        bars,
        atr20,
        p,
        trend,
        sc.rr,
        Some(w),
        Some(t),
        local_block_count,
        entry_block_count,
        weak_confirm,
        wick_penalty,
    );
    sc.dims = dims;

    if p.hard_failure || p.grade == Grade::Invalid {
        sc.total = 0.0;
        sc.category = "结构硬失效，不参与";
        sc.note = "b段已经突破a段起点，结构事实失效".to_string();
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

    sc.total = total;
    sc.category = score_category(total);
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
    if wick_penalty > 0.0 {
        let shadow_note = match p.dir {
            Dir::Up => "做多预警K线为长下影，上影偏长",
            Dir::Down => "做空预警K线为长上影，下影偏长",
        };
        sc.note = format!("{}；{}，触发分已相应扣减", sc.note, shadow_note);
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
    fn entry_block_lowers_trigger_and_momentum_scores() {
        let p = pattern();
        let trend = trend60();
        let atr = vec![Some(10.0), Some(10.0), Some(10.0)];
        let bars = vec![
            bar(0.0, 1.0, 0.0, 0.5),
            bar(0.5, 1.0, 0.4, 0.6),
            bar(0.6, 1.0, 0.5, 0.7),
        ];

        let clean_trigger = score_trigger(&bars, &atr, Some(1), Some(2), &p, 0, 0.0);
        let blocked_trigger = score_trigger(&bars, &atr, Some(1), Some(2), &p, 2, 0.0);
        assert!((blocked_trigger - clean_trigger + 1.4).abs() < 1e-9);

        let clean_momentum = score_momentum(&p, &trend, &atr, 0);
        let blocked_momentum = score_momentum(&p, &trend, &atr, 2);
        assert!((blocked_momentum - clean_momentum + 0.7).abs() < 1e-9);
    }

    #[test]
    fn b_end_opposition_heavily_lowers_trigger_quality() {
        let atr = vec![Some(29.6), Some(29.6)];

        let bars_long = vec![
            bar(4565.6, 4568.2, 4542.4, 4543.2),
            bar(4558.4, 4577.0, 4557.4, 4562.4),
        ];
        assert!(strong_opposite_body_at(&bars_long, &atr, Dir::Up, 0).is_some());
        assert!(trigger_opposition_penalty(&bars_long, &atr, Dir::Up, 0, 1) > 1.5);

        let bars_short = vec![
            bar(4518.0, 4542.0, 4517.0, 4537.0),
            bar(4537.0, 4538.0, 4520.0, 4533.0),
        ];
        assert!(strong_opposite_body_at(&bars_short, &atr, Dir::Down, 0).is_some());
        assert!(trigger_opposition_penalty(&bars_short, &atr, Dir::Down, 0, 1) > 1.5);
    }

    #[test]
    fn pending_signal_uses_default_trigger_score() {
        let mut p = pattern();
        // 入场价偏移一档后，决策点需高于预警K线极值，这里把价格平移到正常量级
        p.s0.price = 4500.0;
        p.s1.price = 4503.0;
        p.s2.price = 4500.5;
        let trend = trend60();
        let atr = vec![Some(10.0); 5];
        let bars = vec![
            bar(4500.0, 4501.0, 4500.0, 4500.5),
            bar(4500.5, 4501.0, 4500.4, 4500.6),
            bar(4500.6, 4501.0, 4500.5, 4500.7),
            bar(4500.7, 4500.95, 4500.65, 4500.9),
            bar(4500.9, 4500.9, 4500.8, 4500.85),
        ];

        let sc = evaluate_signal(&bars, &atr, &p, &trend);
        assert_eq!(sc.state, "即将触发");
        assert_eq!(sc.trigger, None);
        assert!((sc.dims[3] - 1.0).abs() < 1e-9);
        assert!(sc.total > 0.0);
        assert!(sc.note.contains("继续等待"));
    }

    #[test]
    fn a_leg_score_uses_amplitude_quality_and_length() {
        // a段质量分=乘法短板：幅度×强K密度，长度单独轻扣（纯公式单测）。
        // 幅度：10倍ATR满档；5倍ATR只有一半。
        assert!((a_leg_score_formula(100.0, 8, 5, 10.0) - 5.0).abs() < 1e-9);
        assert!((a_leg_score_formula(50.0, 8, 5, 10.0) - 2.75).abs() < 1e-9);
        // 强K密度：0根只有地板分。
        assert!((a_leg_score_formula(100.0, 8, 0, 10.0) - 0.5).abs() < 1e-9);
        // 长腿动能扣分：33根、强K密度满也扣0.8。
        assert!((a_leg_score_formula(100.0, 33, 12, 10.0) - 4.2).abs() < 1e-9);
        // 短腿保底2根：3根腿只有1根强K → 质量0.5。
        assert!((a_leg_score_formula(100.0, 3, 1, 10.0) - 2.75).abs() < 1e-9);
    }

    #[test]
    fn a_leg_relaxed_strong_counts_direction_candles() {
        // 宽松强趋势K计数：实体占比、收盘位置、振幅、影线四条件全过才算。
        let atr = vec![Some(10.0); 8];
        let bars = vec![
            bar(100.0, 108.0, 99.0, 106.0),  // 强阳：实体67%、收盘位0.78、上影小 ✓
            bar(100.0, 104.0, 95.0, 96.0),   // 弱阳：实体44% ✗
            bar(96.0, 104.0, 96.0, 102.0),   // 强阳：实体75%、收盘位0.75 ✓
            bar(102.0, 108.0, 101.0, 107.0), // 强阳：实体71%、收盘位0.86 ✓
        ];
        let p = NPattern {
            dir: Dir::Up,
            s0: Swing {
                index: 0,
                price: 100.0,
                is_high: false,
            },
            s1: Swing {
                index: 3,
                price: 102.0,
                is_high: true,
            },
            ..pattern()
        };
        assert_eq!(a_leg_relaxed_strong(&bars, &atr, &p), 2);
    }

    #[test]
    fn weak_trends_are_scored_by_direction() {
        let weak_up = Trend60 {
            direction: "WEAK_UP".to_string(),
            ..trend60()
        };
        let weak_down = Trend60 {
            direction: "WEAK_DOWN".to_string(),
            ..trend60()
        };
        assert_eq!(score_60m(&weak_up, Dir::Up), 3.0);
        assert_eq!(score_60m(&weak_up, Dir::Down), 1.0);
        assert_eq!(score_60m(&weak_down, Dir::Down), 3.0);
        assert_eq!(score_60m(&weak_down, Dir::Up), 1.0);
    }

    #[test]
    fn pending_signal_invalidated_when_b_leg_broken() {
        let mut p = pattern();
        p.s0.price = 4500.0;
        p.s1.price = 4503.0;
        p.s2.price = 4500.5;
        let trend = trend60();
        let atr = vec![Some(10.0); 10];
        let bars = vec![
            bar(4500.0, 4501.0, 4500.0, 4500.5),
            bar(4500.5, 4501.0, 4500.4, 4500.6),
            bar(4500.6, 4501.0, 4500.5, 4500.7),
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
        p.s1.price = 4503.0;
        p.s2.price = 4500.5;
        let trend = trend60();
        let atr = vec![Some(10.0); 20];
        let mut bars = vec![
            bar(4500.0, 4501.0, 4500.0, 4500.5),
            bar(4500.5, 4501.0, 4500.4, 4500.6),
            bar(4500.6, 4501.0, 4500.5, 4500.7),
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
        // 测不到“弱确认封顶 3.49 仍落小仓”的本意）。
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
        assert_eq!(sc.trigger, Some(5));
        assert_eq!(sc.state, "当前已触发");
        assert!(sc.total <= 3.49);
        assert!(sc.category.contains("小仓试错"));
        assert!(sc.note.contains("累积确认"));
    }

    #[test]
    fn strong_single_reversal_at_b_end_is_not_downgraded() {
        // 做空：b段末强阳线后直接出现强趋势阴线，属于合格反转，不降级
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
            bar(14940.0, 14940.0, 14850.0, 14855.0), // 强趋势阴线
            bar(14855.0, 14860.0, 14820.0, 14830.0), // 触发
        ];

        let sc = evaluate_signal(&bars, &atr, &p, &trend60());
        assert_eq!(sc.warning, Some(3));
        assert_eq!(sc.trigger, Some(4));
        assert!(!sc.note.contains("累积确认"));
        assert!(!sc.note.contains("触发分已相应扣减"));
    }

    #[test]
    fn weak_b_end_keeps_original_warning_behavior() {
        // b段末不是强趋势K时，首根反向收盘K线仍直接作为预警（保持原逻辑）
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
            bar(14890.0, 14890.0, 14860.0, 14865.0), // 首根阴线直接作为预警
        ];

        let sc = evaluate_signal(&bars, &atr, &p, &trend60());
        assert_eq!(sc.warning, Some(3));
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
            bar(4050.0, 4055.0, 3996.0, 4000.0), // s1 低点
            bar(4023.0, 4030.0, 4019.0, 4021.0), // s2 长上影线
            bar(4021.0, 4021.0, 4002.0, 4007.0), // 触发：跌破s2低点
        ];

        let sc = evaluate_signal(&bars, &atr, &p, &trend60());
        assert_eq!(sc.warning, Some(2));
        assert_eq!(sc.trigger, Some(3));
        assert_eq!(sc.entry, 4018.0);
        assert_eq!(sc.state, "当前已触发");
        assert!((sc.dims[3] - 3.9).abs() < 1e-9);
        assert!(sc.note.contains("下影偏长"));
        assert!(!sc.note.contains("累积确认"));
    }

    #[test]
    fn long_wick_reverse_shadow_penalty_follows_ratio_thresholds() {
        // 做空：长上影的反向影线是下影；≤10%不扣，10%-20%扣0.3，>20%扣0.5
        let short_clean = vec![bar(100.0, 109.0, 99.0, 99.0)];
        let short_boundary_low = vec![bar(100.0, 108.0, 98.0, 99.0)];
        let short_medium = vec![bar(100.0, 107.5, 97.5, 99.0)];
        let short_boundary_high = vec![bar(100.0, 107.0, 97.0, 99.0)];
        let short_heavy = vec![bar(100.0, 106.9, 96.9, 99.0)];
        assert_eq!(
            long_wick_reverse_shadow_penalty(&short_clean, Dir::Down, 0),
            0.0
        );
        assert_eq!(
            long_wick_reverse_shadow_penalty(&short_boundary_low, Dir::Down, 0),
            0.0
        );
        assert_eq!(
            long_wick_reverse_shadow_penalty(&short_medium, Dir::Down, 0),
            0.3
        );
        assert_eq!(
            long_wick_reverse_shadow_penalty(&short_boundary_high, Dir::Down, 0),
            0.3
        );
        assert_eq!(
            long_wick_reverse_shadow_penalty(&short_heavy, Dir::Down, 0),
            0.5
        );

        // 做多：长下影的反向影线是上影，扣分口径对称
        let long_clean = vec![bar(100.0, 101.0, 91.0, 101.0)];
        let long_medium = vec![bar(100.0, 102.5, 92.5, 101.0)];
        let long_heavy = vec![bar(100.0, 103.1, 93.1, 101.0)];
        assert_eq!(
            long_wick_reverse_shadow_penalty(&long_clean, Dir::Up, 0),
            0.0
        );
        assert_eq!(
            long_wick_reverse_shadow_penalty(&long_medium, Dir::Up, 0),
            0.3
        );
        assert_eq!(
            long_wick_reverse_shadow_penalty(&long_heavy, Dir::Up, 0),
            0.5
        );
    }

    #[test]
    fn long_wick_warning_with_reverse_shadow_lowers_trigger_score() {
        // 做空：s2长上影本身就是预警，下影占振幅20%，触发分3.5降为3.2
        let atr = atrs(5, 15.4);
        let p = NPattern {
            dir: Dir::Down,
            s1: Swing {
                index: 1,
                price: 3996.0,
                is_high: false,
            },
            s2: Swing {
                index: 2,
                price: 4029.0,
                is_high: true,
            },
            grade: Grade::C,
            ..pattern()
        };
        let bars = vec![
            bar(4066.0, 4066.0, 4055.0, 4060.0), // s0 高点
            bar(4050.0, 4055.0, 3996.0, 4000.0), // s1 低点
            bar(4023.0, 4029.0, 4019.0, 4021.0), // s2 长上影，下影占振幅20%
            bar(4021.0, 4022.0, 4020.0, 4021.5), // 延迟K线，未跌破s2低点
            bar(4019.0, 4020.0, 3998.0, 4002.0), // 触发：跌破s2低点
        ];

        let sc = evaluate_signal(&bars, &atr, &p, &trend60());
        assert_eq!(sc.warning, Some(2));
        assert_eq!(sc.trigger, Some(4));
        assert!((sc.dims[3] - 3.2).abs() < 1e-9);
        assert!(sc.note.contains("下影偏长"));
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
            bar(4010.0, 4030.0, 4009.0, 4015.0), // s2 收阳长上影线
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
        // 方案B：B级结构等到强趋势阴线出现后才出预警，拒绝前面的小阴线
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
            bar(14890.0, 14892.0, 14870.0, 14880.0), // 小阴线，不合格
            bar(14880.0, 14880.0, 14800.0, 14805.0), // 强趋势阴线 → 合格预警
            bar(14805.0, 14810.0, 14760.0, 14770.0), // 触发
        ];

        let sc = evaluate_signal(&bars, &atr, &p, &trend60());
        assert_eq!(sc.warning, Some(4));
        assert_eq!(sc.trigger, Some(5));
        assert_eq!(sc.entry, 14799.0);
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
    fn a_grade_fast_path_rejects_small_bullish_with_long_upper_wick() {
        // A级快速路径不接受"小实体+长上影"的阳线做多预警，
        // 等到收盘位置合格的反向K线才出预警（b端为普通阴线，未触发强锚门）。
        let atr = atrs(6, 30.0);
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
            bar(14835.0, 14855.0, 14830.0, 14840.0), // 小阳线+长上影 → 不合格
            bar(14840.0, 14870.0, 14835.0, 14860.0), // 收盘位置合格的反转阳线 → 预警
            bar(14860.0, 14890.0, 14858.0, 14885.0), // 触发
        ];

        let sc = evaluate_signal(&bars, &atr, &p, &trend60());
        assert_eq!(sc.warning, Some(4));
        assert_eq!(sc.trigger, Some(5));
        assert_eq!(sc.entry, 14871.0);
    }

    #[test]
    fn relaxed_strong_anchor_blocks_fast_path_warning() {
        // SF0场景：b端大阴线实体/振幅够大但收盘位0.75（未达严格趋势K的0.80），
        // 强锚口径统一后仍判为“强反向实体”，禁用A级快速路径，小阳线不能直接预警。
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
            bar(5864.0, 5878.0, 5862.0, 5874.0), // 小阳线：快速路径可过，但被强锚拦截
            bar(5874.0, 5884.0, 5872.0, 5878.0), // 小阳线：无合格反转
            bar(5878.0, 5882.0, 5874.0, 5880.0), // 小阳线：无合格反转，未收复锚定开盘5896
        ];

        let sc = evaluate_signal(&bars, &atr, &p, &trend60());
        assert_eq!(sc.warning, None);
        assert_eq!(sc.state, "等待预警");
        assert!(sc.note.contains("强反向实体"));
    }

    #[test]
    fn a_grade_fast_path_waits_when_only_cross_star_and_weak_candles() {
        // bu0案例：s2为十字星、后续阳线收盘位置不合格时不出预警，等待真正的反转K线
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
        assert_eq!(sc.state, "等待预警");
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
