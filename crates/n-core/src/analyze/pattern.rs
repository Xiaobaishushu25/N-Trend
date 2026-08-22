use crate::analyze::model::{Bar, Dir, Grade, NPattern, Swing, ATR_PERIOD};
use crate::analyze::{indicators, scoring};

pub const FINE_MAX_A_BARS: usize = 16;
pub const FINE_MAX_B_BARS: usize = 12;
pub const LARGE_MAX_A_BARS: usize = 48;
pub const LARGE_MAX_B_BARS: usize = 32;

// a段内同向趋势K线的最低数量（放宽版趋势K判定）
pub const MIN_STRONG_A_LEG: usize = 1;
// 微型N判定：a段与b段幅度都很小
pub const SMALL_N_A_ATR: f64 = 3.0;
pub const SMALL_N_B_ATR: f64 = 1.0;
pub const SMALL_N_B_MIN_POINTS: f64 = 5.0;
// b段动能衰减判定：反向K线实体少于2根不判；后半段平均实体 <= 前半段75% 视为衰减。
// 用整体均值比而不是逐根比较，中间夹小阳/小阴不会破坏“整体变小”的判定。
const B_WEAKENING_MIN_BODIES: usize = 2;
const B_WEAKENING_RATIO_MAX: f64 = 0.75;
// b段深V判定：做多时内部低点低于 S2 的幅度超过 b 段净幅度一半，
// 视为“先大幅下跌再大幅拉回”的折返路径，而不是沿单一方向运动的健康回撤。
const B_V_SHAPE_REVERSAL_RATIO: f64 = 0.5;

/// a段内同向趋势K线根数（含 S0），用于校验a段是否具备实际推动
pub fn a_leg_strong_count(p: &NPattern, trend_k_relaxed: &[(bool, bool)]) -> usize {
    let mut count = 0;
    for i in p.s0.index..=p.s1.index {
        if p.dir == Dir::Down && trend_k_relaxed[i].1 {
            count += 1;
        }
        if p.dir == Dir::Up && trend_k_relaxed[i].0 {
            count += 1;
        }
    }
    count
}

/// 微型N：a段幅度 < 3倍ATR 且 b段幅度 < max(1倍ATR, 5点)，
/// 避免把"a段强趋势+b段小横盘"这类正常结构误判为小N
pub fn is_small_n(p: &NPattern, atr20: &[Option<f64>]) -> bool {
    let atr_s1 = atr20.get(p.s1.index).and_then(|x| *x).unwrap_or(1.0);
    let atr_s2 = atr20.get(p.s2.index).and_then(|x| *x).unwrap_or(1.0);
    let a_small = p.a_move < SMALL_N_A_ATR * atr_s1;
    let b_small = p.b_move < (SMALL_N_B_ATR * atr_s2).max(SMALL_N_B_MIN_POINTS);
    a_small && b_small
}

/// b段反向K线实体是否整体衰减。
/// 反向K线指与主趋势方向相反的回调K线：做多看阴线实体、做空看阳线实体。
pub(crate) fn b_leg_weakening(
    bars: &[Bar],
    start: usize,
    end: usize,
    dir: Dir,
) -> (bool, Option<f64>) {
    let mut bodies = Vec::new();
    for i in start + 1..=end {
        let body = match dir {
            Dir::Up => (bars[i].open - bars[i].close).max(0.0),
            Dir::Down => (bars[i].close - bars[i].open).max(0.0),
        };
        if body > 0.0 {
            bodies.push(body);
        }
    }
    if bodies.len() < B_WEAKENING_MIN_BODIES {
        return (false, None);
    }

    let mid = bodies.len() / 2;
    let first: f64 = bodies[..mid].iter().sum();
    let second: f64 = bodies[mid..].iter().sum();
    let first_mean = first / mid as f64;
    let second_mean = second / (bodies.len() - mid) as f64;
    if first_mean <= 0.0 {
        return (false, None);
    }
    let ratio = second_mean / first_mean;
    (ratio <= B_WEAKENING_RATIO_MAX, Some(ratio))
}

/// b段路径是否呈深V折返。
/// 做多时 S2 是回调端点，若 b 段内部出现过明显低于 S2 的低点，
/// 说明价格先反向走深再大幅拉回；做空同理看明显高于 S2 的高点。
fn b_leg_deep_reversal(
    bars: &[Bar],
    start: usize,
    end: usize,
    dir: Dir,
    s2_price: f64,
    b_move: f64,
) -> bool {
    if b_move <= 0.0 {
        return false;
    }
    let mut path_low = f64::INFINITY;
    let mut path_high = f64::NEG_INFINITY;
    for i in start + 1..=end {
        path_low = path_low.min(bars[i].low);
        path_high = path_high.max(bars[i].high);
    }
    let threshold = B_V_SHAPE_REVERSAL_RATIO * b_move;
    match dir {
        Dir::Up => path_low < s2_price - threshold,
        Dir::Down => path_high > s2_price + threshold,
    }
}

pub fn make_pattern(
    level: &'static str,
    dir: Dir,
    s0: Swing,
    s1: Swing,
    s2: Swing,
    bars: &[Bar],
    trend_k: &[(bool, bool)],
) -> Option<NPattern> {
    // A段K线根数含 S0 本身：S0 是转折极值K，同时也是新腿的第一根K。
    let a_bars = s1.index.saturating_sub(s0.index) + 1;
    let b_bars = s2.index.saturating_sub(s1.index);
    if a_bars < 3 || b_bars < 2 {
        return None;
    }

    let (a_move, b_move, retracement) = match dir {
        Dir::Down => {
            let a = s0.price - s1.price;
            let b = s2.price - s1.price;
            (a, b, if a > 0.0 { b / a } else { 0.0 })
        }
        Dir::Up => {
            let a = s1.price - s0.price;
            let b = s1.price - s2.price;
            (a, b, if a > 0.0 { b / a } else { 0.0 })
        }
    };

    if a_move <= 0.0 {
        return None;
    }

    let endpoint_failure = match dir {
        Dir::Down => s2.price >= s0.price,
        Dir::Up => s2.price <= s0.price,
    };

    // 做多 b 段中间任意低点触到或跌破 S0、做空任意高点触到或突破 S0，
    // 都按结构破位处理。只看端点会漏掉“先挖坑再收回”的 V 型路径。
    let path_failure = bars.get(s1.index + 1..=s2.index).is_some_and(|leg| {
        leg.iter().any(|b| match dir {
            Dir::Up => b.low <= s0.price,
            Dir::Down => b.high >= s0.price,
        })
    });

    let b_v_shape = b_leg_deep_reversal(bars, s1.index, s2.index, dir, s2.price, b_move);
    let hard_failure = endpoint_failure || path_failure || b_v_shape;

    let grade = if hard_failure {
        Grade::Invalid
    } else if retracement < 0.20 {
        Grade::TooShallow
    } else if retracement <= 0.50 {
        Grade::A
    } else if retracement <= 0.66 {
        Grade::B
    } else if retracement <= 0.80 {
        Grade::C
    } else {
        Grade::TooDeep
    };

    let mut a_strong_trend = 0;
    for i in s0.index..=s1.index {
        if dir == Dir::Down && trend_k[i].1 {
            a_strong_trend += 1;
        }
        if dir == Dir::Up && trend_k[i].0 {
            a_strong_trend += 1;
        }
    }

    let mut b_strong_reverse = 0;
    for i in s1.index + 1..=s2.index {
        if dir == Dir::Down && trend_k[i].0 {
            b_strong_reverse += 1;
        }
        if dir == Dir::Up && trend_k[i].1 {
            b_strong_reverse += 1;
        }
    }

    let (b_weakening, b_weakening_ratio) = b_leg_weakening(bars, s1.index, s2.index, dir);

    let a_speed = a_move / a_bars as f64;
    let b_speed = b_move / b_bars as f64;

    let mut c_move = 0.0;
    let mut c_bars = 0;
    if s2.index + 1 < bars.len() {
        c_bars = bars.len() - 1 - s2.index;
        c_move = match dir {
            Dir::Down => {
                s2.price
                    - bars[s2.index + 1..]
                        .iter()
                        .map(|b| b.low)
                        .fold(f64::INFINITY, f64::min)
            }
            Dir::Up => {
                bars[s2.index + 1..]
                    .iter()
                    .map(|b| b.high)
                    .fold(f64::NEG_INFINITY, f64::max)
                    - s2.price
            }
        };
    }

    let mut c_hard_failure = false;
    for i in s2.index + 1..bars.len() {
        let broken = match dir {
            Dir::Up => bars[i].low < s0.price,
            Dir::Down => bars[i].high > s0.price,
        };
        if broken {
            c_hard_failure = true;
            break;
        }
    }

    Some(NPattern {
        level,
        dir,
        s0,
        s1,
        s2,
        a_bars,
        b_bars,
        a_move,
        b_move,
        retracement,
        grade,
        hard_failure,
        a_too_long: a_bars > 7,
        b_too_long: match grade { Grade::A => b_bars > 8, Grade::B => b_bars > 12, Grade::C => b_bars > 14, _ => b_bars > 8 },
        b_fast: b_speed > 0.8 * a_speed,
        b_weakening,
        b_weakening_ratio,
        a_strong_trend,
        b_strong_reverse,
        c_move,
        c_bars,
        c_extended: c_move >= 1.2 * a_move,
        c_hard_failure,
    })
}

fn extreme_between(bars: &[Bar], start: usize, end: usize, want_high: bool) -> Option<Swing> {
    if start + 1 >= end {
        return None;
    }

    let mut best_index = start + 1;
    let mut best_price = if want_high {
        f64::NEG_INFINITY
    } else {
        f64::INFINITY
    };

    for i in start + 1..end {
        let price = if want_high { bars[i].high } else { bars[i].low };
        let better = if want_high {
            price > best_price
        } else {
            price < best_price
        };
        if better || (price == best_price && i > best_index) {
            best_index = i;
            best_price = price;
        }
    }

    Some(Swing {
        index: best_index,
        price: best_price,
        is_high: want_high,
    })
}

fn better_pattern(a: &NPattern, b: &NPattern) -> bool {
    let ra = a.grade.rank();
    let rb = b.grade.rank();
    if ra != rb {
        return ra > rb;
    }

    let qa = a.a_too_long as u8 + a.b_too_long as u8 + a.b_fast as u8;
    let qb = b.a_too_long as u8 + b.b_too_long as u8 + b.b_fast as u8;
    if qa != qb {
        return qa < qb;
    }

    let ta = a.a_bars + a.b_bars;
    let tb = b.a_bars + b.b_bars;
    if ta != tb {
        return ta < tb;
    }

    if a.s1.index != b.s1.index {
        return a.s1.index > b.s1.index;
    }
    if a.a_move != b.a_move {
        return a.a_move > b.a_move;
    }
    if a.s0.index != b.s0.index {
        return a.s0.index > b.s0.index;
    }
    false
}

pub(crate) fn best_pattern_for_b_end(
    level: &'static str,
    dir: Dir,
    b_end: Swing,
    bars: &[Bar],
    starts: &[Swing],
    trend_k: &[(bool, bool)],
    max_a_bars: usize,
    max_b_bars: usize,
) -> Option<NPattern> {
    let mut best: Option<NPattern> = None;
    let atr20 = indicators::atr(bars, ATR_PERIOD);

    for s0 in starts.iter().rev() {
        if s0.index >= b_end.index {
            continue;
        }
        if b_end.index - s0.index > max_a_bars + max_b_bars {
            break;
        }

        let anchor_ok = match dir {
            Dir::Down => s0.price > b_end.price,
            Dir::Up => s0.price < b_end.price,
        };
        if !anchor_ok {
            continue;
        }

        let Some(s1) = extreme_between(bars, s0.index, b_end.index, dir == Dir::Up) else {
            continue;
        };
        if s1.index - s0.index < 2 || b_end.index - s1.index < 2 {
            continue;
        }
        if s1.index - s0.index > max_a_bars || b_end.index - s1.index > max_b_bars {
            continue;
        }

        if let Some(p) = make_pattern(level, dir, *s0, s1, b_end, bars, trend_k) {
            let cur_score = scoring::score_a(bars, &atr20, &p)*0.6 + scoring::score_b(&p)*0.2;
            let best_score = best.as_ref().map(|b| scoring::score_a(bars, &atr20, b)*0.6 + scoring::score_b(b)*0.2).unwrap_or(f64::NEG_INFINITY);
            if best.as_ref().map_or(true, |b| if p.hard_failure != b.hard_failure { !p.hard_failure } else if cur_score > best_score + 1e-9 { true } else if (cur_score - best_score).abs() <= 1e-9 { better_pattern(&p, b) } else { false }) {
                best = Some(p);
            }
        }
    }

    best
}

pub fn analyze_level(
    level: &'static str,
    bars: &[Bar],
    swings: &[Swing],
    trend_k: &[(bool, bool)],
    max_a_bars: usize,
    max_b_bars: usize,
) -> Vec<NPattern> {
    let highs: Vec<Swing> = swings.iter().copied().filter(|s| s.is_high).collect();
    let lows: Vec<Swing> = swings.iter().copied().filter(|s| !s.is_high).collect();
    let mut out = Vec::new();

    for &s2 in swings {
        let pattern = if s2.is_high {
            best_pattern_for_b_end(
                level,
                Dir::Down,
                s2,
                bars,
                &highs,
                trend_k,
                max_a_bars,
                max_b_bars,
            )
        } else {
            best_pattern_for_b_end(
                level,
                Dir::Up,
                s2,
                bars,
                &lows,
                trend_k,
                max_a_bars,
                max_b_bars,
            )
        };

        if let Some(p) = pattern {
            if !p.c_hard_failure && (p.s2.index + 20 >= bars.len() || p.c_bars <= 40) {
                out.push(p);
            }
        }
    }

    out
}

pub fn latest_pattern<'a>(patterns: &'a [NPattern], dir: Dir) -> Option<&'a NPattern> {
    let mut best: Option<&NPattern> = None;
    for p in patterns {
        if p.dir == dir {
            if best.map_or(true, |b| p.s2.index > b.s2.index) {
                best = Some(p);
            }
        }
    }
    best
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::analyze::model::Grade;

    fn bar(o: f64, h: f64, l: f64, c: f64) -> Bar {
        Bar {
            dt: crate::analyze::model::DT {
                year: 2026,
                month: 8,
                day: 15,
                hour: 10,
                minute: 0,
            },
            open: o,
            high: h,
            low: l,
            close: c,
            volume: 0.0,
            hold: 0.0,
            rollover: false,
        }
    }

    fn np(a_move: f64, b_move: f64) -> NPattern {
        NPattern {
            level: "fine",
            dir: Dir::Up,
            s0: Swing {
                index: 0,
                price: 100.0,
                is_high: false,
            },
            s1: Swing {
                index: 5,
                price: 110.0,
                is_high: true,
            },
            s2: Swing {
                index: 8,
                price: 106.0,
                is_high: false,
            },
            a_bars: 5,
            b_bars: 3,
            a_move,
            b_move,
            retracement: 0.5,
            grade: Grade::A,
            hard_failure: false,
            a_too_long: false,
            b_too_long: false,
            b_fast: false,
            b_weakening: false,
            b_weakening_ratio: None,
            a_strong_trend: 0,
            b_strong_reverse: 0,
            c_move: 0.0,
            c_bars: 0,
            c_extended: false,
            c_hard_failure: false,
        }
    }

    #[test]
    fn small_n_requires_both_legs_small() {
        let atr = vec![Some(10.0); 20];
        // a=2ATR 且 b=0.5ATR -> 微型N
        assert!(is_small_n(&np(20.0, 5.0), &atr));
        // a=5ATR 但 b 很小 -> a段强趋势+b段小横盘，不算小N
        assert!(!is_small_n(&np(50.0, 5.0), &atr));
        // b 足够大 -> 不是小N
        assert!(!is_small_n(&np(20.0, 20.0), &atr));
    }

    #[test]
    fn a_leg_strong_count_counts_directional_flags() {
        let p = np(20.0, 5.0); // dir=Up, s0=0, s1=5
        let flags = vec![
            (false, false), // s0
            (true, false),
            (false, true),
            (true, false),
            (false, false),
            (true, false), // s1
        ];
        assert_eq!(a_leg_strong_count(&p, &flags), 3);
        let down = NPattern {
            dir: Dir::Down,
            ..np(20.0, 5.0)
        };
        assert_eq!(a_leg_strong_count(&down, &flags), 1);
    }

    #[test]
    fn b_leg_weakening_uses_overall_body_shrinkage() {
        // 做多b段：大阴、小阳、小阴 -> 只看阴线实体，整体衰减成立。
        let bars = vec![
            bar(100.0, 100.0, 99.0, 99.5),   // s0
            bar(101.0, 102.0, 100.0, 101.5), // s1
            bar(101.0, 101.5, 99.0, 99.5),   // 大阴
            bar(99.5, 100.0, 99.0, 100.0),   // 小阳
            bar(100.0, 100.1, 99.4, 99.6),   // 小阴
        ];
        let (weakening, ratio) = b_leg_weakening(&bars, 1, 4, Dir::Up);
        assert!(weakening);
        assert!(ratio.unwrap() < 0.5);

        // 做空b段：大阳、小阴、小阳 -> 看阳线实体，同样衰减。
        let down_bars = vec![
            bar(102.0, 102.0, 101.0, 101.5), // s0
            bar(101.0, 101.5, 99.0, 99.5),   // s1
            bar(99.0, 102.0, 99.0, 102.0),   // 大阳
            bar(101.0, 101.5, 100.0, 100.5), // 小阴
            bar(100.5, 101.0, 100.4, 100.9), // 小阳
        ];
        let (weakening, ratio) = b_leg_weakening(&down_bars, 1, 4, Dir::Down);
        assert!(weakening);
        assert!(ratio.unwrap() < 0.5);
    }

    #[test]
    fn b_leg_path_touch_or_break_is_hard_failure() {
        let trend = vec![(false, false); 6];

        // 做多：b 段中间先触到 S0=100，再收回 106，端点本身并未破位。
        let long_bars = vec![
            bar(100.0, 100.0, 100.0, 100.0), // s0
            bar(101.0, 103.0, 100.5, 102.0),
            bar(103.0, 110.0, 103.0, 109.0), // s1
            bar(108.0, 108.0, 100.0, 99.0),  // 路径触到 S0
            bar(99.0, 107.0, 106.0, 106.5),  // s2
            bar(107.0, 112.0, 107.0, 111.0),
        ];
        let long = make_pattern(
            "fine",
            Dir::Up,
            Swing {
                index: 0,
                price: 100.0,
                is_high: false,
            },
            Swing {
                index: 2,
                price: 110.0,
                is_high: true,
            },
            Swing {
                index: 4,
                price: 106.0,
                is_high: false,
            },
            &long_bars,
            &trend,
        )
        .expect("pattern construction should succeed");
        assert!(long.hard_failure);
        assert_eq!(long.grade, Grade::Invalid);

        // 做空：b 段中间先触到 S0=110，再回落 106，端点本身并未破位。
        let short_bars = vec![
            bar(110.0, 110.0, 110.0, 110.0), // s0
            bar(109.0, 109.5, 107.0, 108.0),
            bar(108.0, 108.0, 100.0, 101.0), // s1
            bar(102.0, 110.0, 102.0, 111.0), // 路径触到 S0
            bar(105.0, 106.0, 104.0, 105.5), // s2
            bar(106.0, 105.0, 102.0, 104.0),
        ];
        let short = make_pattern(
            "fine",
            Dir::Down,
            Swing {
                index: 0,
                price: 110.0,
                is_high: true,
            },
            Swing {
                index: 2,
                price: 100.0,
                is_high: false,
            },
            Swing {
                index: 4,
                price: 106.0,
                is_high: true,
            },
            &short_bars,
            &trend,
        )
        .expect("pattern construction should succeed");
        assert!(short.hard_failure);
        assert_eq!(short.grade, Grade::Invalid);
    }

    #[test]
    fn b_leg_deep_reversal_is_hard_failure() {
        let trend = vec![(false, false); 6];

        // 做多：b 段先深跌到 102，再拉回 108，最后 S2=106.5。
        // 深跌幅度 4.5 > 0.5*b_move=1.75，但低点 102 没有触到 S0=100。
        let long_bars = vec![
            bar(100.0, 100.0, 100.0, 100.0), // s0
            bar(102.0, 104.0, 101.0, 103.0),
            bar(103.0, 110.0, 103.0, 109.0), // s1
            bar(108.0, 108.0, 102.0, 103.0), // 深跌
            bar(103.0, 108.0, 102.5, 107.0), // 大幅拉回
            bar(107.0, 107.5, 106.5, 107.0), // s2
        ];
        let long = make_pattern(
            "fine",
            Dir::Up,
            Swing {
                index: 0,
                price: 100.0,
                is_high: false,
            },
            Swing {
                index: 2,
                price: 110.0,
                is_high: true,
            },
            Swing {
                index: 5,
                price: 106.5,
                is_high: false,
            },
            &long_bars,
            &trend,
        )
        .expect("pattern construction should succeed");
        assert!(long.hard_failure);
        assert_eq!(long.grade, Grade::Invalid);

        // 做空：b 段先冲到 111，再回落，最后 S2=106.5。
        // 冲高幅度 4.5 > 0.5*b_move=3.25，但高点 111 没有触到 S0=115。
        let short_bars = vec![
            bar(115.0, 115.0, 115.0, 115.0), // s0
            bar(113.0, 113.0, 111.0, 112.0),
            bar(112.0, 107.0, 100.0, 101.0), // s1
            bar(101.0, 111.0, 101.0, 108.0), // 深涨
            bar(108.0, 108.0, 101.0, 105.0), // 大幅回落
            bar(105.0, 106.5, 104.0, 106.0), // s2
        ];
        let short = make_pattern(
            "fine",
            Dir::Down,
            Swing {
                index: 0,
                price: 115.0,
                is_high: true,
            },
            Swing {
                index: 2,
                price: 100.0,
                is_high: false,
            },
            Swing {
                index: 5,
                price: 106.5,
                is_high: true,
            },
            &short_bars,
            &trend,
        )
        .expect("pattern construction should succeed");
        assert!(short.hard_failure);
        assert_eq!(short.grade, Grade::Invalid);
    }

    #[test]
    fn b_leg_small_recovery_is_not_deep_reversal() {
        let trend = vec![(false, false); 6];

        // 做多：内部低点 105.8 只比 S2=106.5 低 0.7，不到 0.5*b_move=1.75，
        // 属于普通毛刺，不应判深V硬失效。
        let bars = vec![
            bar(100.0, 100.0, 100.0, 100.0), // s0
            bar(102.0, 104.0, 101.0, 103.0),
            bar(103.0, 110.0, 103.0, 109.0), // s1
            bar(108.0, 109.0, 105.8, 106.5), // 小毛刺
            bar(106.5, 108.0, 106.0, 107.0),
            bar(107.0, 107.5, 106.5, 107.0), // s2
        ];
        let p = make_pattern(
            "fine",
            Dir::Up,
            Swing {
                index: 0,
                price: 100.0,
                is_high: false,
            },
            Swing {
                index: 2,
                price: 110.0,
                is_high: true,
            },
            Swing {
                index: 5,
                price: 106.5,
                is_high: false,
            },
            &bars,
            &trend,
        )
        .expect("pattern construction should succeed");
        assert!(!p.hard_failure);
        assert_eq!(p.grade, Grade::A);
    }
}
