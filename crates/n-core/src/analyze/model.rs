use std::fmt;

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

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
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
            Grade::B => 3.8,
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
    pub hard_failure: bool,
    pub a_too_long: bool,
    pub b_too_long: bool,
    pub b_fast: bool,
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
        self.direction == "UP" || self.direction == "WEAK_UP"
    }

    pub fn is_down(&self) -> bool {
        self.direction == "DOWN" || self.direction == "WEAK_DOWN"
    }

    pub fn aligned_with(&self, dir: Dir) -> bool {
        (dir == Dir::Up && self.is_up()) || (dir == Dir::Down && self.is_down())
    }

    pub fn opposite_to(&self, dir: Dir) -> bool {
        (dir == Dir::Up && self.is_down()) || (dir == Dir::Down && self.is_up())
    }

    pub fn strong(&self) -> bool {
        self.direction == "UP" || self.direction == "DOWN"
    }
}

pub struct SignalCheck {
    pub warning: Option<usize>,
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
    pub dims: [f64; 6],
    pub total: f64,
    pub category: &'static str,
    pub note: String,
}

impl SignalCheck {
    pub fn new() -> Self {
        Self {
            warning: None,
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
            dims: [0.0; 6],
            total: 0.0,
            category: "",
            note: String::new(),
        }
    }
}
