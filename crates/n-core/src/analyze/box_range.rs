//! 箱体识别与触轨信号（2.0 分析分支）。

use crate::analyze::dto::BoxDto;
use crate::analyze::indicators;
use crate::analyze::model::{Bar, Dir, Grade, NPattern, SignalCheck, Swing, Trend60};
use crate::analyze::scoring;

const RAIL_TOUCH_TOLERANCE_ATR: f64 = 0.65;
const WARNING_TOUCH_TOLERANCE_ATR: f64 = 0.40;
const MIN_RAIL_TOUCHES: usize = 2;
const MIN_BOX_HEIGHT_ATR: f64 = 3.0;
const MIN_SPAN_BARS: usize = 8;
const MAX_SPAN_BARS: usize = 80;
const STOP_BUFFER_ATR: f64 = 0.1;
const PENDING_MAX_AGE: usize = 12;
const BOX_BASE_SCORE: f64 = 3.0;
const BOX_TREND_ALIGN_SCORE: f64 = 0.5;
const BOX_ENTRY_RAIL_TOUCH_SCORE: f64 = 0.5;
const BOX_RR_SCORE: f64 = 0.5;
const RECENT_BOX_GAP_BARS: usize = 24;
const MAX_BOXES_PER_DIR: usize = 2;
const BOX_IDENTITY_ATR: f64 = 1.0;

/// 一条箱体触轨信号：内部同时携带展示元数据与选择排序所需字段。
pub struct BoxSignal {
    pub pattern: NPattern,
    pub check: SignalCheck,
    pub meta: BoxDto,
    pub last_touch_index: usize,
    pub touch_count: usize,
}

fn atr_at(atr20: &[Option<f64>], index: usize) -> f64 {
    atr20.get(index).and_then(|x| *x).unwrap_or(1.0)
}

fn median_price(swings: &[Swing]) -> f64 {
    let mut prices: Vec<f64> = swings.iter().map(|s| s.price).collect();
    prices.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let mid = prices.len() / 2;
    if prices.len() % 2 == 0 {
        (prices[mid - 1] + prices[mid]) / 2.0
    } else {
        prices[mid]
    }
}

/// 把同轨摆动点按价格接近程度聚合成候选轨线。
/// 触轨点不必在时间上连续：箱体上下轨之间通常会夹着另一轨或普通摆动，
/// 只要价格再次回到同一水平，就应归入同一候选轨线。
fn cluster_swings(swings: &[Swing], atr20: &[Option<f64>]) -> Vec<Vec<Swing>> {
    let mut clusters: Vec<Vec<Swing>> = Vec::new();
    for s in swings {
        let mut placed = false;
        for cluster in clusters.iter_mut() {
            let median = median_price(cluster);
            let atr = atr_at(atr20, s.index);
            if (median - s.price).abs() <= RAIL_TOUCH_TOLERANCE_ATR * atr {
                cluster.push(*s);
                placed = true;
                break;
            }
        }
        if !placed {
            clusters.push(vec![*s]);
        }
    }
    clusters
}

struct BoxCandidate {
    upper: f64,
    lower: f64,
    upper_touches: usize,
    lower_touches: usize,
    first_index: usize,
    last_index: usize,
    confirm_index: usize,
}

fn box_candidates(
    bars: &[Bar],
    atr20: &[Option<f64>],
    highs: &[Swing],
    lows: &[Swing],
) -> Vec<BoxCandidate> {
    let mut candidates = Vec::new();
    for high_cluster in cluster_swings(highs, atr20) {
        if high_cluster.len() < MIN_RAIL_TOUCHES {
            continue;
        }
        let upper = median_price(&high_cluster);
        for low_cluster in cluster_swings(lows, atr20) {
            if low_cluster.len() < MIN_RAIL_TOUCHES {
                continue;
            }
            let lower = median_price(&low_cluster);
            if lower >= upper {
                continue;
            }
            let high_first = high_cluster[0].index;
            let high_last = high_cluster[high_cluster.len() - 1].index;
            let low_first = low_cluster[0].index;
            let low_last = low_cluster[low_cluster.len() - 1].index;
            // 上下轨触碰时间需要交织，避免把两段互不重叠的行情硬凑成箱体。
            if high_first > low_last || low_first > high_last {
                continue;
            }
            let first_index = high_first.min(low_first);
            let last_index = high_last.max(low_last);
            let span_bars = last_index - first_index + 1;
            if !(MIN_SPAN_BARS..=MAX_SPAN_BARS).contains(&span_bars) {
                continue;
            }
            if bars[first_index..=last_index].iter().any(|b| b.rollover) {
                continue;
            }
            let atr = atr_at(atr20, last_index);
            let height = upper - lower;
            if height < MIN_BOX_HEIGHT_ATR * atr {
                continue;
            }
            let confirm_index = high_cluster[1]
                .index
                .max(low_cluster[1].index)
                .max(first_index + MIN_SPAN_BARS - 1);
            candidates.push(BoxCandidate {
                upper,
                lower,
                upper_touches: high_cluster.len(),
                lower_touches: low_cluster.len(),
                first_index,
                last_index,
                confirm_index,
            });
        }
    }
    candidates
}

/// 在箱体确认后寻找最新一根满足触轨条件的预警K线。
fn find_warning(
    bars: &[Bar],
    atr20: &[Option<f64>],
    dir: Dir,
    upper: f64,
    lower: f64,
    from: usize,
) -> Option<(usize, &'static str)> {
    let mut found = None;
    for j in from..bars.len() {
        let atr = atr_at(atr20, j);
        let ok = match dir {
            Dir::Up => bars[j].low <= lower + WARNING_TOUCH_TOLERANCE_ATR * atr,
            Dir::Down => bars[j].high >= upper - WARNING_TOUCH_TOLERANCE_ATR * atr,
        };
        if !ok {
            continue;
        }
        let kind = match scoring::single_reversal_pattern(bars, atr20, dir, j, j) {
            Some(kind) => kind.as_str(),
            None => continue,
        };
        found = Some((j, kind));
    }
    found
}

#[allow(clippy::too_many_arguments)]
fn build_box_check(
    bars: &[Bar],
    atr20: &[Option<f64>],
    trend60: &Trend60,
    dir: Dir,
    upper: f64,
    lower: f64,
    upper_touches: usize,
    lower_touches: usize,
    warning: usize,
    warning_kind: &'static str,
    tick: f64,
) -> SignalCheck {
    let mut sc = SignalCheck::new();
    sc.category = "BOX";
    sc.warning = Some(warning);
    sc.warning_kind = warning_kind;

    let atr = atr_at(atr20, warning);
    sc.entry = match dir {
        Dir::Up => bars[warning].high + tick,
        Dir::Down => bars[warning].low - tick,
    };
    sc.stop = match dir {
        Dir::Up => lower - STOP_BUFFER_ATR * atr,
        Dir::Down => upper + STOP_BUFFER_ATR * atr,
    };
    sc.decision_target = match dir {
        Dir::Up => upper,
        Dir::Down => lower,
    };
    sc.risk = (sc.entry - sc.stop).abs();
    sc.space = (sc.decision_target - sc.entry).abs();
    sc.rr = if sc.risk > 0.0 {
        sc.space / sc.risk
    } else {
        0.0
    };

    if sc.risk <= 0.0 || sc.space <= 0.0 {
        sc.state = "空间异常";
        sc.note = "止损或目标空间无法正常计算".to_string();
        return sc;
    }

    let mut trigger = None;
    for j in warning + 1..bars.len() {
        let ok = match dir {
            Dir::Up => bars[j].high >= bars[warning].high && bars[j].close > bars[warning].high,
            Dir::Down => bars[j].low <= bars[warning].low && bars[j].close < bars[warning].low,
        };
        if ok {
            trigger = Some(j);
            break;
        }
    }

    if let Some(t) = trigger {
        sc.trigger = Some(t);
        sc.trigger_age = bars.len().saturating_sub(t + 1);
        sc.state = if sc.trigger_age <= 2 {
            "当前已触发"
        } else if sc.trigger_age <= 6 {
            "已触发，接近时效边界"
        } else {
            "已过时，仅复盘"
        };
    } else {
        let structure_broken = bars[warning + 1..].iter().any(|b| match dir {
            Dir::Up => b.close < lower,
            Dir::Down => b.close > upper,
        });
        if structure_broken {
            sc.state = "结构失效";
            sc.note = "箱体止损位已被收盘价突破，结构失效".to_string();
            return sc;
        }
        let pending_age = bars.len().saturating_sub(warning + 1);
        sc.state = if pending_age > PENDING_MAX_AGE {
            "已过时，仅复盘"
        } else {
            "即将触发"
        };
    }

    let entry_rail_touches = match dir {
        Dir::Up => lower_touches,
        Dir::Down => upper_touches,
    };
    let trend_aligned = trend60.aligned_with(dir);
    let trend_score = if trend_aligned {
        BOX_TREND_ALIGN_SCORE
    } else {
        0.0
    };
    let touch_score = if entry_rail_touches >= 3 {
        BOX_ENTRY_RAIL_TOUCH_SCORE
    } else {
        0.0
    };
    let rr_score = if sc.rr >= 1.2 { BOX_RR_SCORE } else { 0.0 };
    sc.dims = [
        trend_score,
        touch_score,
        rr_score,
        if sc.trigger.is_some() { 1.0 } else { 0.0 },
        0.0,
        0.0,
    ];
    // 2026-08-14：箱体与N字共用同一套预警K线质量分，
    // 干净吞没/长影线预警同样计入综合评分（+0.3）。
    sc.total =
        (BOX_BASE_SCORE + trend_score + touch_score + rr_score + sc.warning_quality_points())
            .min(5.0);
    sc.note = build_box_note(dir, entry_rail_touches, sc.rr, sc.trigger.is_some());
    sc
}

fn build_box_pattern(
    dir: Dir,
    upper: f64,
    lower: f64,
    upper_touches: usize,
    lower_touches: usize,
    first_index: usize,
    warning: usize,
    bars: &[Bar],
) -> NPattern {
    NPattern {
        level: "box",
        dir,
        s0: Swing {
            index: first_index,
            price: lower,
            is_high: false,
        },
        s1: Swing {
            index: first_index,
            price: upper,
            is_high: true,
        },
        s2: Swing {
            index: warning,
            price: bars[warning].close,
            is_high: dir == Dir::Down,
        },
        a_bars: lower_touches,
        b_bars: upper_touches,
        a_move: upper - lower,
        b_move: 0.0,
        retracement: 0.0,
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

fn build_box_note(dir: Dir, entry_rail_touches: usize, rr: f64, triggered: bool) -> String {
    let rail = match dir {
        Dir::Up => "下轨",
        Dir::Down => "上轨",
    };
    let mut parts = vec![format!("箱体{}做多/做空信号", rail)];
    if entry_rail_touches >= 3 {
        parts.push(format!("{}触碰{}次", rail, entry_rail_touches));
    }
    if rr >= 1.2 {
        parts.push(format!("RR {:.2}", rr));
    }
    if triggered {
        parts.push("已突破预警K线极值".to_string());
    } else {
        parts.push("等待突破预警K线极值".to_string());
    }
    parts.join("；")
}

fn is_same_box(a: &BoxSignal, b: &BoxSignal, atr20: &[Option<f64>]) -> bool {
    let atr = atr_at(atr20, a.last_touch_index.max(b.last_touch_index));
    (a.meta.upper - b.meta.upper).abs() <= BOX_IDENTITY_ATR * atr
        && (a.meta.lower - b.meta.lower).abs() <= BOX_IDENTITY_ATR * atr
}

/// 每个方向保留两条互不重叠的箱体：最新一条 + 相隔至少 24 根的另一条。
fn select_box_signals(mut signals: Vec<BoxSignal>, atr20: &[Option<f64>]) -> Vec<BoxSignal> {
    signals.sort_by(|a, b| {
        b.last_touch_index
            .cmp(&a.last_touch_index)
            .then_with(|| b.touch_count.cmp(&a.touch_count))
    });
    let mut selected: Vec<BoxSignal> = Vec::new();
    for s in signals {
        if selected.is_empty() {
            selected.push(s);
            continue;
        }
        if selected.len() >= MAX_BOXES_PER_DIR {
            break;
        }
        let age_gap = selected[0]
            .last_touch_index
            .saturating_sub(s.last_touch_index);
        if age_gap >= RECENT_BOX_GAP_BARS && !selected.iter().any(|e| is_same_box(e, &s, atr20)) {
            selected.push(s);
        }
    }
    selected
}

/// 识别箱体并产出每方向最新且质量最高的触轨信号。
pub fn detect_boxes(
    bars: &[Bar],
    atr20: &[Option<f64>],
    trend60: &Trend60,
    tick: f64,
) -> Vec<BoxSignal> {
    let swings = indicators::find_swings(bars, atr20, 2, 8);
    let highs: Vec<Swing> = swings.iter().filter(|s| s.is_high).copied().collect();
    let lows: Vec<Swing> = swings.iter().filter(|s| !s.is_high).copied().collect();
    let candidates = box_candidates(bars, atr20, &highs, &lows);

    let mut up_signals: Vec<BoxSignal> = Vec::new();
    let mut down_signals: Vec<BoxSignal> = Vec::new();
    for c in candidates {
        for dir in [Dir::Up, Dir::Down] {
            let Some((w, warning_kind)) =
                find_warning(bars, atr20, dir, c.upper, c.lower, c.confirm_index)
            else {
                continue;
            };
            let touch_count = c.upper_touches + c.lower_touches;
            let pattern = build_box_pattern(
                dir,
                c.upper,
                c.lower,
                c.upper_touches,
                c.lower_touches,
                c.first_index,
                w,
                bars,
            );
            let check = build_box_check(
                bars,
                atr20,
                trend60,
                dir,
                c.upper,
                c.lower,
                c.upper_touches,
                c.lower_touches,
                w,
                warning_kind,
                tick,
            );
            let meta = BoxDto {
                upper: c.upper,
                lower: c.lower,
                upper_touches: c.upper_touches,
                lower_touches: c.lower_touches,
                first_ts: bars[c.first_index].dt.to_string(),
                last_ts: bars[c.last_index].dt.to_string(),
            };
            let signal = BoxSignal {
                pattern,
                check,
                meta,
                last_touch_index: c.last_index,
                touch_count,
            };
            match dir {
                Dir::Up => up_signals.push(signal),
                Dir::Down => down_signals.push(signal),
            }
        }
    }
    select_box_signals(up_signals, atr20)
        .into_iter()
        .chain(select_box_signals(down_signals, atr20))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::analyze::model::DT;

    fn bar(open: f64, high: f64, low: f64, close: f64, minute: i32) -> Bar {
        Bar {
            dt: DT {
                year: 2026,
                month: 8,
                day: 3,
                hour: 9,
                minute,
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

    fn neutral_trend() -> Trend60 {
        Trend60 {
            direction: "NEUTRAL".to_string(),
            ma20: 95.0,
            slope: 0.0,
            price_vs_ma: 0.0,
            higher_highs: false,
            higher_lows: false,
            lower_highs: false,
            lower_lows: false,
        }
    }

    fn box_series() -> Vec<Bar> {
        let mut bars = Vec::new();
        for i in 0..60 {
            let minute = (i % 60) as i32 + 1;
            let b = match i {
                5 | 20 => bar(99.0, 100.0, 98.5, 99.5, minute),
                35 => bar(99.8, 100.0, 98.2, 98.4, minute),
                12 | 27 | 42 => bar(91.0, 91.5, 90.0, 90.8, minute),
                50 => bar(95.0, 95.2, 90.0, 95.1, minute),
                55 => bar(96.0, 100.0, 94.0, 94.8, minute), // 干净吞没阴线，触上轨
                56 => bar(94.0, 96.0, 90.0, 96.0, minute),  // 干净吞没阳线，触下轨
                57..=59 => bar(95.0, 97.5, 96.5, 95.2, minute),
                _ => bar(94.8, 95.5, 94.7, 95.2, minute),
            };
            bars.push(b);
        }
        bars
    }

    #[test]
    fn detects_both_rail_signals() {
        let bars = box_series();
        let atr20 = indicators::atr(&bars, 20);
        let signals = detect_boxes(&bars, &atr20, &neutral_trend(), 1.0);
        assert_eq!(signals.len(), 2);
        let dirs: Vec<Dir> = signals.iter().map(|s| s.pattern.dir).collect();
        assert!(dirs.contains(&Dir::Up));
        assert!(dirs.contains(&Dir::Down));

        let down = signals
            .iter()
            .find(|s| s.pattern.dir == Dir::Down)
            .expect("should have down signal");
        assert_eq!(down.check.state, "即将触发");
        assert!(down.check.total >= 3.5);
        assert_eq!(down.pattern.level, "box");
        assert!(down.meta.upper_touches >= 2);
        assert!(down.meta.lower_touches >= 2);
        assert_eq!(down.meta.upper, 100.0);
        assert_eq!(down.meta.lower, 90.0);
        assert!(!down.check.warning_kind.is_empty());
    }

    #[test]
    fn rollover_box_is_filtered() {
        let mut bars = box_series();
        bars[10].rollover = true;
        let atr20 = indicators::atr(&bars, 20);
        let signals = detect_boxes(&bars, &atr20, &neutral_trend(), 1.0);
        assert!(signals.is_empty());
    }
}
