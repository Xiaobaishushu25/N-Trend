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
    pub note: String,
    pub active: bool,
}

impl PatternDto {
    pub fn from_parts(bars: &[Bar], number: usize, p: &NPattern, sc: &SignalCheck) -> Self {
        let ts = |i: usize| bars.get(i).map(|b| b.dt.to_string()).unwrap_or_default();
        let (vol_ratio, vol_confirmed) = match sc.trigger {
            Some(ec) => (vol_ratio_at(bars, ec), ec + 1 < bars.len()),
            None => (None, false),
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
