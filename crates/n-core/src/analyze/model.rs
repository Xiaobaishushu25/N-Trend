use std::fmt;
use chrono::{Datelike, Timelike};

pub const ATR_PERIOD: usize = 20;

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct DT {
    pub year: i32,
    pub month: i32,
    pub day: i32,
    pub hour: i32,
    pub minute: i32,
}

impl fmt::Display for DT {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "{:04}-{:02}-{:02} {:02}:{:02}",
            self.year, self.month, self.day, self.hour, self.minute
        )
    }
}


impl DT {
    /// 统一的 bar 时间字符串（带秒），与 DB / ScanResult 保持一致
    pub fn to_bar_ts(&self) -> String {
        format!(
            "{:04}-{:02}-{:02} {:02}:{:02}:00",
            self.year, self.month, self.day, self.hour, self.minute
        )
    }
    /// 从 "YYYY-MM-DD HH:MM:SS" 解析 DT，兼容无秒的 "YYYY-MM-DD HH:MM"
    pub fn from_bar_ts(s: &str) -> Option<Self> {
        if s.len() < 16 { return None; }
        let y = s[0..4].parse().ok()?;
        let m = s[5..7].parse().ok()?;
        let d = s[8..10].parse().ok()?;
        let h = s[11..13].parse().ok()?;
        let min = s[14..16].parse().ok()?;
        Some(Self { year: y, month: m, day: d, hour: h, minute: min })
    }
    /// 增加分钟数，使用 chrono 处理跨天/月/年，避免手工 days_in_month 重复
    pub fn add_minutes(&self, mins: i32) -> Self {
        if let Some(date) = chrono::NaiveDate::from_ymd_opt(self.year, self.month as u32, self.day as u32) {
            if let Some(dt) = date.and_hms_opt(self.hour as u32, self.minute as u32, 0) {
                let exp = dt + chrono::Duration::minutes(mins as i64);
                return Self {
                    year: exp.year(),
                    month: exp.month() as i32,
                    day: exp.day() as i32,
                    hour: exp.hour() as i32,
                    minute: exp.minute() as i32,
                };
            }
        }
        let mut y = self.year; let mut m = self.month; let mut d = self.day; let mut h = self.hour; let mut min = self.minute + mins;
        while min >= 60 { min -= 60; h += 1; }
        while min < 0 { min += 60; h -= 1; }
        while h >= 24 { h -= 24; d += 1; let dim = Self::days_in_month(y, m); if d > dim { d = 1; m += 1; if m > 12 { m = 1; y += 1; } } }
        while h < 0 { h += 24; d -= 1; if d < 1 { m -= 1; if m < 1 { m = 12; y -= 1; } d = Self::days_in_month(y, m); } }
        Self { year: y, month: m, day: d, hour: h, minute: min }
    }
    fn days_in_month(y: i32, m: i32) -> i32 {
        match m {
            1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
            4 | 6 | 9 | 11 => 30,
            2 => if (y % 4 == 0 && y % 100 != 0) || (y % 400 == 0) { 29 } else { 28 },
            _ => 30,
        }
    }
}
#[derive(Clone, Debug)]
#[allow(dead_code)]
pub struct Bar {
    pub dt: DT,
    pub open: f64,
    pub high: f64,
    pub low: f64,
    pub close: f64,
    pub volume: f64,
    pub hold: f64,
    /// 该 bar 是连续合约换月后的第一根（跨合约跳空，不计入真实行情）。
    pub rollover: bool,
}

#[derive(Clone, Copy, Debug)]
pub struct Swing {
    pub index: usize,
    pub price: f64,
    pub is_high: bool,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Dir {
    Up,
    Down,
}

impl Dir {
    pub fn label(self) -> &'static str {
        match self {
            Dir::Up => "做多",
            Dir::Down => "做空",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Grade {
    A,
    B,
    C,
    TooShallow,
    TooDeep,
    Invalid,
}

impl Grade {
    pub fn label(self) -> &'static str {
        match self {
            Grade::A => "A级",
            Grade::B => "B级",
            Grade::C => "C级",
            Grade::TooShallow => "回撤过浅",
            Grade::TooDeep => "回撤过深",
            Grade::Invalid => "结构无效",
        }
    }

    pub fn rank(self) -> u8 {
        match self {
            Grade::A => 4,
            Grade::B => 3,
            Grade::C => 2,
            Grade::TooShallow | Grade::TooDeep => 1,
            Grade::Invalid => 0,
        }
    }

    pub fn score_base(self) -> f64 {
        match self {
            Grade::A => 5.0,
            Grade::B => 4.3,
            Grade::C => 2.5,
            Grade::TooShallow => 2.0,
            Grade::TooDeep => 1.0,
            Grade::Invalid => 0.0,
        }
    }
}

pub struct NPattern {
    pub level: &'static str,
    pub dir: Dir,
    pub s0: Swing,
    pub s1: Swing,
    pub s2: Swing,
    pub a_bars: usize,
    pub b_bars: usize,
    pub a_move: f64,
    pub b_move: f64,
    pub retracement: f64,
    pub grade: Grade,
    /// b段端点或中间路径跌破/突破 a 段起点，结构硬失效。
    pub hard_failure: bool,
    pub a_too_long: bool,
    pub b_too_long: bool,
    pub b_fast: bool,
    /// b段反向K线实体是否整体收敛（回调动能衰减）。
    pub b_weakening: bool,
    /// 后/前半段反向K线平均实体比，样本不足时为 None。
    pub b_weakening_ratio: Option<f64>,
    pub a_strong_trend: usize,
    pub b_strong_reverse: usize,
    pub c_move: f64,
    pub c_bars: usize,
    pub c_extended: bool,
    pub c_hard_failure: bool,
}

pub struct Trend60 {
    pub direction: String,
    pub ma20: f64,
    pub slope: f64,
    pub price_vs_ma: f64,
    pub higher_highs: bool,
    pub higher_lows: bool,
    pub lower_highs: bool,
    pub lower_lows: bool,
}

impl Trend60 {
    pub fn is_up(&self) -> bool {
        matches!(self.direction.as_str(), "UP" | "WEAK_UP" | "STRONG_UP")
    }

    pub fn is_down(&self) -> bool {
        matches!(self.direction.as_str(), "DOWN" | "WEAK_DOWN" | "STRONG_DOWN")
    }

    pub fn aligned_with(&self, dir: Dir) -> bool {
        (dir == Dir::Up && self.is_up()) || (dir == Dir::Down && self.is_down())
    }

    pub fn opposite_to(&self, dir: Dir) -> bool {
        (dir == Dir::Up && self.is_down()) || (dir == Dir::Down && self.is_up())
    }

    pub fn strong(&self) -> bool {
        matches!(self.direction.as_str(), "UP" | "DOWN" | "STRONG_UP" | "STRONG_DOWN")
    }

    pub fn is_weak_up(&self) -> bool { self.direction == "WEAK_UP" }
    pub fn is_weak_down(&self) -> bool { self.direction == "WEAK_DOWN" }
    pub fn is_strong_up(&self) -> bool { self.direction == "STRONG_UP" }
    pub fn is_strong_down(&self) -> bool { self.direction == "STRONG_DOWN" }
    pub fn is_range(&self) -> bool { self.direction == "RANGE" || self.direction == "NEUTRAL" }
}

#[derive(Clone)]
pub struct SignalCheck {
    pub warning: Option<usize>,
    pub warning_kind: &'static str,
    pub trigger: Option<usize>,
    pub trigger_age: usize,
    pub state: &'static str,
    pub entry_block_count: u8,
    pub entry_block_detail: String,
    pub entry: f64,
    pub stop: f64,
    pub decision_target: f64,
    pub risk: f64,
    pub space: f64,
    pub rr: f64,
    pub dim_a: f64,
    pub dim_b: f64,
    pub dim_warning: f64,
    pub total: f64,
    pub category: &'static str,
    pub note: String,
    pub trend_bonus: f64,
    pub trend_state: String,
}

impl SignalCheck {
    pub fn new() -> Self {
        Self {
            warning: None,
            warning_kind: "",
            trigger: None,
            trigger_age: 0,
            state: "",
            entry_block_count: 0,
            entry_block_detail: String::new(),
            entry: 0.0,
            stop: 0.0,
            decision_target: 0.0,
            risk: 0.0,
            space: 0.0,
            rr: 0.0,
            dim_a: 0.0,
            dim_b: 0.0,
            dim_warning: 0.0,
            total: 0.0,
            category: "",
            note: String::new(),
            trend_bonus: 0.0,
            trend_state: String::new(),
        }
    }

    /// 2026-08-16：预警K线质量改为真实综合评分项。
    /// 强反转（干净吞没）/长影线统一计入 0.3 分，
    /// 累计覆盖/无预警不计分；历史 fast 记录同样不计分。
    pub const WARNING_QUALITY_POINTS: f64 = 0.3;

    pub fn warning_quality_points(&self) -> f64 {
        Self::warning_quality_points_for(self.warning_kind)
    }

    pub fn warning_quality_points_for(kind: &str) -> f64 {
        if matches!(kind, "strong" | "engulf" | "wick") {
            Self::WARNING_QUALITY_POINTS
        } else {
            0.0
        }
    }
}


#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct SingleBarAlert {
    pub symbol: String,
    pub timeframe: String,
    pub kind: String,
    pub label: String,
    pub trigger_bar_ts: String,
    pub expire_bar_ts: String,
    pub price: f64,
    pub high: f64,
    pub low: f64,
}
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn warning_quality_points_are_real_scoring_bonus() {
        let mut sc = SignalCheck::new();
        for (kind, points) in [
            ("strong", 0.3),
            // 历史落盘仍可能为 engulf，按同一强反转口径计分。
            ("engulf", 0.3),
            ("wick", 0.3),
            // 历史落盘兼容；新扫描不再产生 fast。
            ("fast", 0.0),
            ("cumulative", 0.0),
            ("none", 0.0),
            ("", 0.0),
        ] {
            sc.warning_kind = kind;
            assert_eq!(sc.warning_quality_points(), points, "kind={kind}");
        }
    }
}




