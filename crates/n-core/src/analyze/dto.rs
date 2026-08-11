//! 面向前端展示的结构化分析结果 DTO。

use serde::{Deserialize, Serialize};

use crate::analyze::model::{Bar, Dir, NPattern, SignalCheck, Trend60};
use crate::analyze::outcome::vol_ratio_at;
use crate::analyze::report;

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct SwingDto {
    pub index: usize,
    pub price: f64,
    pub is_high: bool,
    pub ts: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct PatternDto {
    pub number: usize,
    pub level: String,
    pub direction: String,
    pub grade: String,
    pub s0: SwingDto,
    pub s1: SwingDto,
    pub s2: SwingDto,
    pub a_bars: usize,
    pub b_bars: usize,
    pub a_move: f64,
    pub b_move: f64,
    pub retracement: f64,
    pub state: String,
    pub category: String,
    pub entry: f64,
    pub stop: f64,
    pub target: f64,
    pub risk: f64,
    pub space: f64,
    pub rr: f64,
    pub score: f64,
    pub dims: [f64; 6],
    pub warning_ts: Option<String>,
    pub trigger_ts: Option<String>,
    #[serde(default)]
    pub vol_ratio: Option<f64>,
    #[serde(default)]
    pub vol_confirmed: bool,
    /// 触发K线相对入场价的追价深度（按R归一化），触发K线收盘前只有实时值
    #[serde(default)]
    pub trigger_overshoot_r: Option<f64>,
    pub note: String,
    pub active: bool,
}

impl PatternDto {
    pub fn from_parts(bars: &[Bar], number: usize, p: &NPattern, sc: &SignalCheck) -> Self {
        let ts = |i: usize| bars.get(i).map(|b| b.dt.to_string()).unwrap_or_default();
        let (vol_ratio, vol_confirmed, trigger_overshoot_r) = match sc.trigger {
            Some(ec) => {
                let overshoot = if sc.risk > 0.0 {
                    bars.get(ec).map(|b| match p.dir {
                        Dir::Up => (b.high - sc.entry) / sc.risk,
                        Dir::Down => (sc.entry - b.low) / sc.risk,
                    })
                } else {
                    None
                };
                (vol_ratio_at(bars, ec), ec + 1 < bars.len(), overshoot)
            }
            None => (None, false, None),
        };
        Self {
            number,
            level: p.level.to_string(),
            direction: match p.dir {
                Dir::Up => "up",
                Dir::Down => "down",
            }
            .to_string(),
            grade: p.grade.label().to_string(),
            s0: SwingDto {
                index: p.s0.index,
                price: p.s0.price,
                is_high: p.s0.is_high,
                ts: ts(p.s0.index),
            },
            s1: SwingDto {
                index: p.s1.index,
                price: p.s1.price,
                is_high: p.s1.is_high,
                ts: ts(p.s1.index),
            },
            s2: SwingDto {
                index: p.s2.index,
                price: p.s2.price,
                is_high: p.s2.is_high,
                ts: ts(p.s2.index),
            },
            a_bars: p.a_bars,
            b_bars: p.b_bars,
            a_move: p.a_move,
            b_move: p.b_move,
            retracement: p.retracement,
            state: sc.state.to_string(),
            category: sc.category.to_string(),
            entry: sc.entry,
            stop: sc.stop,
            target: sc.decision_target,
            risk: sc.risk,
            space: sc.space,
            rr: sc.rr,
            score: sc.total,
            dims: sc.dims,
            warning_ts: sc.warning.map(ts),
            trigger_ts: sc.trigger.map(ts),
            vol_ratio,
            vol_confirmed,
            trigger_overshoot_r,
            note: sc.note.clone(),
            active: report::is_active_signal(sc),
        }
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct TrendDto {
    pub direction: String,
    pub direction_label: String,
    pub ma20: f64,
    pub slope: f64,
    pub price_vs_ma: f64,
    pub higher_highs: bool,
    pub higher_lows: bool,
    pub lower_highs: bool,
    pub lower_lows: bool,
}

impl TrendDto {
    pub fn from_trend(t: &Trend60) -> Self {
        Self {
            direction: t.direction.clone(),
            direction_label: report::direction_label(&t.direction).to_string(),
            ma20: t.ma20,
            slope: t.slope,
            price_vs_ma: t.price_vs_ma,
            higher_highs: t.higher_highs,
            higher_lows: t.higher_lows,
            lower_highs: t.lower_highs,
            lower_lows: t.lower_lows,
        }
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct AnalysisDetail {
    pub symbol: String,
    pub trend60: TrendDto,
    pub signals: Vec<PatternDto>,
    pub full_report: String,
}

/// 一个品种的一条信号（扫描结果持久化与事件广播用）。
#[derive(Clone, Debug, Serialize)]
pub struct SignalOutcome {
    pub symbol: String,
    #[serde(flatten)]
    pub signal: PatternDto,
}

pub fn build_detail(
    symbol: &str,
    bars15: &[Bar],
    trend60: &Trend60,
    signals: &[(usize, &NPattern, SignalCheck)],
    full_report: &str,
) -> AnalysisDetail {
    AnalysisDetail {
        symbol: symbol.to_string(),
        trend60: TrendDto::from_trend(trend60),
        signals: signals
            .iter()
            .map(|(number, p, sc)| PatternDto::from_parts(bars15, *number, p, sc))
            .collect(),
        full_report: full_report.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::analyze::model::{Grade, Swing, DT};

    fn bar(dt: DT, high: f64, low: f64) -> Bar {
        Bar {
            dt,
            open: 100.0,
            high,
            low,
            close: 100.0,
            volume: 100.0,
            hold: 0.0,
            rollover: false,
        }
    }

    fn pattern(dir: Dir) -> NPattern {
        NPattern {
            level: "fine",
            dir,
            s0: Swing {
                index: 0,
                price: 100.0,
                is_high: false,
            },
            s1: Swing {
                index: 1,
                price: 101.0,
                is_high: true,
            },
            s2: Swing {
                index: 2,
                price: 100.0,
                is_high: false,
            },
            a_bars: 2,
            b_bars: 2,
            a_move: 1.0,
            b_move: 1.0,
            retracement: 0.5,
            grade: Grade::A,
            hard_failure: false,
            a_too_long: false,
            b_too_long: false,
            b_fast: true,
            a_strong_trend: 1,
            b_strong_reverse: 0,
            c_move: 0.0,
            c_bars: 0,
            c_extended: false,
            c_hard_failure: false,
        }
    }

    #[test]
    fn pattern_dto_exposes_trigger_overshoot_and_confirmation() {
        let bars = vec![
            bar(
                DT {
                    year: 2026,
                    month: 8,
                    day: 3,
                    hour: 9,
                    minute: 0,
                },
                100.0,
                100.0,
            ),
            bar(
                DT {
                    year: 2026,
                    month: 8,
                    day: 3,
                    hour: 9,
                    minute: 15,
                },
                101.0,
                99.0,
            ),
            bar(
                DT {
                    year: 2026,
                    month: 8,
                    day: 3,
                    hour: 9,
                    minute: 30,
                },
                100.0,
                100.0,
            ),
        ];

        let mut sc = SignalCheck::new();
        sc.warning = Some(0);
        sc.trigger = Some(1);
        sc.entry = 100.0;
        sc.risk = 2.0;
        sc.stop = 99.0;
        sc.decision_target = 102.0;
        sc.space = 2.0;
        sc.rr = 1.0;
        sc.total = 3.5;
        sc.state = "当前已触发";
        sc.category = "fine";

        let up = PatternDto::from_parts(&bars, 1, &pattern(Dir::Up), &sc);
        assert_eq!(up.trigger_overshoot_r, Some(0.5));
        assert!(up.vol_confirmed);

        let down = PatternDto::from_parts(&bars, 1, &pattern(Dir::Down), &sc);
        assert_eq!(down.trigger_overshoot_r, Some(0.5));
    }
}
