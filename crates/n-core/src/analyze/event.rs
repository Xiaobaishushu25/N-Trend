use crate::analyze::indicators;
use crate::analyze::model::{Bar, Dir, Grade, NPattern, Swing, ATR_PERIOD};
use crate::analyze::{pattern, scoring};

#[derive(Clone, Debug)]
pub struct WarningCandidate {
    pub direction: Dir,
    pub grade: String,
    pub level: &'static str,
    pub s0_index: usize,
    pub s1_index: usize,
    pub s2_index: usize,
    pub a_move: f64,
    pub b_move: f64,
    pub a_bars: usize,
    pub b_bars: usize,
    pub retracement: f64,
    pub warning_index: usize,
    pub warning_kind: &'static str,
    pub entry_score: f64,
    pub dim_a: f64,
    pub dim_b: f64,
    pub dim_warning: f64,
    pub entry: f64,
    pub stop: f64,
    pub target: f64,
    pub risk: f64,
    pub rr: f64,
}

fn clamp_score(v: f64) -> f64 {
    v.clamp(0.0, 5.0)
}

// A 腿是形态的推动主力，权重最高；B 腿满分不应掩盖过弱的 A 腿。
fn composite_entry_score(dim_a: f64, dim_b: f64, dim_warning: f64, kind: &str) -> f64 {
    let mut score = 0.60 * dim_a + 0.20 * dim_b + 0.20 * dim_warning;
    if kind == "cumulative" {
        score = score.min(3.49);
    }
    score
}

fn warning_base(kind: &str) -> f64 {
    match kind {
        // engulf 仅保留给历史落盘记录，新识别统一为 strong。
        "strong" | "engulf" | "wick" => 3.5,
        "fast" => 2.5,
        _ => 2.0,
    }
}

fn is_opposite_close(bar: &Bar, dir: Dir) -> bool {
    match dir {
        Dir::Up => bar.close > bar.open,
        Dir::Down => bar.close < bar.open,
    }
}

fn anchor_is_strong(
    bars: &[Bar],
    atr20: &[Option<f64>],
    trend_k: &[(bool, bool)],
    dir: Dir,
    anchor: usize,
) -> bool {
    scoring::strong_b_dir_trend_candle(trend_k, anchor, dir)
        || scoring::strong_opposite_body_at(bars, atr20, dir, anchor).is_some()
}

/// Detect the warning kind for the current bar.  Only already-closed bars are
/// used; cumulative needs at least one preceding opposite-close bar so the
/// current bar can complete the run.
fn warning_kind_at(
    bars: &[Bar],
    atr20: &[Option<f64>],
    trend_k: &[(bool, bool)],
    p: &NPattern,
    w: usize,
) -> Option<&'static str> {
    if let Some(kind) = scoring::single_reversal_pattern(bars, atr20, trend_k, p.dir, w, w) {
        return Some(kind.as_str());
    }
    if !is_opposite_close(&bars[w], p.dir) || w == 0 {
        return None;
    }

    let mut run_start = w;
    while run_start > p.s1.index + 1 && is_opposite_close(&bars[run_start - 1], p.dir) {
        run_start -= 1;
    }
    let anchor = run_start - 1;
    let anchor_strong = anchor_is_strong(bars, atr20, trend_k, p.dir, anchor);
    let strict_confirm = matches!(p.grade, Grade::B | Grade::C);

    if run_start == w
        && !anchor_strong
        && !strict_confirm
        && !p.b_too_long
        && scoring::fast_path_close_ok(bars, p.dir, w)
    {
        return Some("fast");
    }
    if w > run_start
        && (anchor_strong || strict_confirm)
        && scoring::cumulative_coverage(bars, run_start, w, bars[anchor].open, p.dir)
    {
        return Some("cumulative");
    }
    None
}

fn pattern_for_current_bar(
    level: &'static str,
    dir: Dir,
    bars: &[Bar],
    starts: &[Swing],
    trend_k: &[(bool, bool)],
    w: usize,
    max_a_bars: usize,
    max_b_bars: usize,
) -> Option<NPattern> {
    let b_end = Swing {
        index: w,
        price: if dir == Dir::Up {
            bars[w].low
        } else {
            bars[w].high
        },
        is_high: dir == Dir::Down,
    };
    pattern::best_pattern_for_b_end(
        level, dir, b_end, bars, starts, trend_k, max_a_bars, max_b_bars,
    )
}

fn candidate_for(
    bars: &[Bar],
    atr20: &[Option<f64>],
    trend_k: &[(bool, bool)],
    trend_k_relaxed: &[(bool, bool)],
    p: &NPattern,
    w: usize,
    tick: f64,
) -> Option<WarningCandidate> {
    if !matches!(p.grade, Grade::A | Grade::B | Grade::C)
        || pattern::is_small_n(p, atr20)
        || pattern::a_leg_strong_count(p, trend_k_relaxed) < pattern::MIN_STRONG_A_LEG
        || crate::analyze::pattern_window_has_rollover_until(bars, p, w)
    {
        return None;
    }
    let kind = warning_kind_at(bars, atr20, trend_k, p, w)?;

    let dim_a = scoring::score_a(bars, atr20, p);
    let dim_b = scoring::score_b(p);
    let dim_warning = warning_base(kind)
        - if kind == "wick" {
            scoring::wick_direction_penalty(&bars[w], p.dir)
        } else {
            0.0
        }
        - scoring::warning_space_overrun_penalty(bars, p, w);
    let entry_score = composite_entry_score(dim_a, dim_b, dim_warning, kind);

    let buffer = (0.1 * scoring::atr_at(atr20, w)).max(1.0);
    let (entry, stop) = match p.dir {
        Dir::Up => (bars[w].high + tick, p.s2.price - buffer),
        Dir::Down => (bars[w].low - tick, p.s2.price + buffer),
    };
    let target = p.s1.price;
    let risk = (stop - entry).abs();
    let space = match p.dir {
        Dir::Up => target - entry,
        Dir::Down => entry - target,
    };
    if risk <= 0.0 || space <= 0.0 {
        return None;
    }

    Some(WarningCandidate {
        direction: p.dir,
        grade: p.grade.label().to_string(),
        level: p.level,
        s0_index: p.s0.index,
        s1_index: p.s1.index,
        s2_index: p.s2.index,
        a_move: p.a_move,
        b_move: p.b_move,
        a_bars: p.a_bars,
        b_bars: p.b_bars,
        retracement: p.retracement,
        warning_index: w,
        warning_kind: kind,
        entry_score: clamp_score(entry_score),
        dim_a,
        dim_b,
        dim_warning,
        entry,
        stop,
        target,
        risk,
        rr: space / risk,
    })
}

/// Forward-only replay.  Every truncation ends at an already-closed bar, so no
/// event references a future bar.  For each warning bar only the best-scoring
/// AB candidate is returned.
pub fn replay_warnings(symbol: &str, bars: &[Bar], tick: f64) -> Vec<WarningCandidate> {
    let mut best_by_warning: std::collections::HashMap<(Dir, usize), WarningCandidate> =
        std::collections::HashMap::new();

    for w in 0..bars.len() {
        let prefix = &bars[..=w];
        if prefix.len() < ATR_PERIOD + 2 {
            continue;
        }
        let atr20 = indicators::atr(prefix, ATR_PERIOD);
        let trend_k = indicators::trend_flags(prefix, &atr20);
        let trend_k_relaxed = indicators::trend_flags_relaxed(prefix, &atr20);
        let swings_fine = indicators::find_swings(prefix, &atr20, 2, 8);
        let swings_large = indicators::find_swings(prefix, &atr20, 5, 10);

        let mut patterns: Vec<NPattern> = Vec::new();
        for dir in [Dir::Up, Dir::Down] {
            let starts = if dir == Dir::Up {
                swings_fine
                    .iter()
                    .filter(|s| !s.is_high)
                    .copied()
                    .collect::<Vec<_>>()
            } else {
                swings_fine
                    .iter()
                    .filter(|s| s.is_high)
                    .copied()
                    .collect::<Vec<_>>()
            };
            if let Some(p) = pattern_for_current_bar(
                "fine",
                dir,
                prefix,
                &starts,
                &trend_k,
                w,
                pattern::FINE_MAX_A_BARS,
                pattern::FINE_MAX_B_BARS,
            ) {
                patterns.push(p);
            }

            let starts = if dir == Dir::Up {
                swings_large
                    .iter()
                    .filter(|s| !s.is_high)
                    .copied()
                    .collect::<Vec<_>>()
            } else {
                swings_large
                    .iter()
                    .filter(|s| s.is_high)
                    .copied()
                    .collect::<Vec<_>>()
            };
            if let Some(p) = pattern_for_current_bar(
                "large",
                dir,
                prefix,
                &starts,
                &trend_k,
                w,
                pattern::LARGE_MAX_A_BARS,
                pattern::LARGE_MAX_B_BARS,
            ) {
                patterns.push(p);
            }
        }

        for p in patterns {
            if let Some(c) = candidate_for(prefix, &atr20, &trend_k, &trend_k_relaxed, &p, w, tick)
            {
                let key = (c.direction, c.warning_index);
                match best_by_warning.get(&key) {
                    Some(existing) if existing.entry_score >= c.entry_score => {}
                    _ => {
                        best_by_warning.insert(key, c);
                    }
                }
            }
        }
    }

    let _ = symbol;
    let mut out: Vec<WarningCandidate> = best_by_warning.into_values().collect();
    out.sort_by(|a, b| {
        a.warning_index.cmp(&b.warning_index).then_with(|| {
            let au = matches!(a.direction, Dir::Up);
            let bu = matches!(b.direction, Dir::Up);
            bu.cmp(&au)
        })
    });
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::analyze::model::DT;

    fn bar(dt: DT, o: f64, h: f64, l: f64, c: f64) -> Bar {
        Bar {
            dt,
            open: o,
            high: h,
            low: l,
            close: c,
            volume: 0.0,
            hold: 0.0,
            rollover: false,
        }
    }

    fn dt(y: i32, mo: i32, d: i32, h: i32, mi: i32) -> DT {
        DT {
            year: y,
            month: mo,
            day: d,
            hour: h,
            minute: mi,
        }
    }

    fn pattern_for(dir: Dir, s0_price: f64, s1_price: f64, s2_price: f64) -> NPattern {
        NPattern {
            level: "large",
            dir,
            s0: Swing {
                index: 0,
                price: s0_price,
                is_high: dir == Dir::Down,
            },
            s1: Swing {
                index: 1,
                price: s1_price,
                is_high: dir == Dir::Up,
            },
            s2: Swing {
                index: 2,
                price: s2_price,
                is_high: dir == Dir::Down,
            },
            a_bars: 1,
            b_bars: 1,
            a_move: (s0_price - s1_price).abs(),
            b_move: (s1_price - s2_price).abs(),
            retracement: 0.76,
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

    // BU0-style 15m replay series. The 2026-08-14 11:30 bar is a clean long
    // lower shadow under the six-gate criteria and must be recognized the
    // moment it closes, without waiting for later bars.
    fn bu0_series() -> Vec<Bar> {
        vec![
            bar(dt(2026, 8, 12, 9, 15), 4128.0, 4151.0, 4111.0, 4148.0),
            bar(dt(2026, 8, 12, 9, 30), 4148.0, 4160.0, 4147.0, 4147.0),
            bar(dt(2026, 8, 12, 9, 45), 4147.0, 4156.0, 4146.0, 4150.0),
            bar(dt(2026, 8, 12, 10, 0), 4151.0, 4152.0, 4146.0, 4151.0),
            bar(dt(2026, 8, 12, 10, 15), 4154.0, 4160.0, 4151.0, 4153.0),
            bar(dt(2026, 8, 12, 10, 45), 4152.0, 4163.0, 4147.0, 4158.0),
            bar(dt(2026, 8, 12, 11, 0), 4160.0, 4164.0, 4156.0, 4161.0),
            bar(dt(2026, 8, 12, 11, 15), 4162.0, 4162.0, 4159.0, 4160.0),
            bar(dt(2026, 8, 12, 11, 30), 4161.0, 4163.0, 4151.0, 4153.0),
            bar(dt(2026, 8, 12, 13, 45), 4148.0, 4160.0, 4144.0, 4152.0),
            bar(dt(2026, 8, 12, 14, 0), 4151.0, 4159.0, 4150.0, 4159.0),
            bar(dt(2026, 8, 12, 14, 15), 4159.0, 4160.0, 4154.0, 4156.0),
            bar(dt(2026, 8, 12, 14, 30), 4156.0, 4162.0, 4155.0, 4159.0),
            bar(dt(2026, 8, 12, 14, 45), 4159.0, 4160.0, 4147.0, 4156.0),
            bar(dt(2026, 8, 12, 15, 0), 4155.0, 4156.0, 4149.0, 4151.0),
            bar(dt(2026, 8, 12, 21, 15), 4146.0, 4154.0, 4137.0, 4151.0),
            bar(dt(2026, 8, 12, 21, 30), 4151.0, 4156.0, 4148.0, 4154.0),
            bar(dt(2026, 8, 12, 21, 45), 4154.0, 4158.0, 4153.0, 4154.0),
            bar(dt(2026, 8, 12, 22, 0), 4153.0, 4156.0, 4145.0, 4146.0),
            bar(dt(2026, 8, 12, 22, 15), 4146.0, 4151.0, 4142.0, 4151.0),
            bar(dt(2026, 8, 12, 22, 30), 4151.0, 4154.0, 4149.0, 4153.0),
            bar(dt(2026, 8, 12, 22, 45), 4153.0, 4153.0, 4141.0, 4148.0),
            bar(dt(2026, 8, 12, 23, 0), 4148.0, 4154.0, 4147.0, 4151.0),
            bar(dt(2026, 8, 13, 9, 15), 4138.0, 4139.0, 4128.0, 4138.0),
            bar(dt(2026, 8, 13, 9, 30), 4140.0, 4150.0, 4140.0, 4150.0),
            bar(dt(2026, 8, 13, 9, 45), 4147.0, 4150.0, 4145.0, 4147.0),
            bar(dt(2026, 8, 13, 10, 0), 4147.0, 4147.0, 4139.0, 4140.0),
            bar(dt(2026, 8, 13, 10, 15), 4141.0, 4145.0, 4137.0, 4137.0),
            bar(dt(2026, 8, 13, 10, 45), 4139.0, 4143.0, 4138.0, 4140.0),
            bar(dt(2026, 8, 13, 11, 0), 4139.0, 4148.0, 4139.0, 4146.0),
            bar(dt(2026, 8, 13, 11, 15), 4143.0, 4146.0, 4142.0, 4143.0),
            bar(dt(2026, 8, 13, 11, 30), 4144.0, 4162.0, 4142.0, 4160.0),
            bar(dt(2026, 8, 13, 13, 45), 4180.0, 4180.0, 4157.0, 4162.0),
            bar(dt(2026, 8, 13, 14, 0), 4162.0, 4172.0, 4162.0, 4169.0),
            bar(dt(2026, 8, 13, 14, 15), 4170.0, 4173.0, 4166.0, 4167.0),
            bar(dt(2026, 8, 13, 14, 30), 4166.0, 4172.0, 4165.0, 4171.0),
            bar(dt(2026, 8, 13, 14, 45), 4170.0, 4174.0, 4168.0, 4171.0),
            bar(dt(2026, 8, 13, 15, 0), 4171.0, 4178.0, 4170.0, 4173.0),
            bar(dt(2026, 8, 13, 21, 15), 4152.0, 4166.0, 4143.0, 4164.0),
            bar(dt(2026, 8, 13, 21, 30), 4164.0, 4169.0, 4161.0, 4165.0),
            bar(dt(2026, 8, 13, 21, 45), 4165.0, 4174.0, 4165.0, 4170.0),
            bar(dt(2026, 8, 13, 22, 0), 4171.0, 4172.0, 4157.0, 4158.0),
            bar(dt(2026, 8, 13, 22, 15), 4158.0, 4162.0, 4155.0, 4161.0),
            bar(dt(2026, 8, 13, 22, 30), 4161.0, 4167.0, 4160.0, 4167.0),
            bar(dt(2026, 8, 13, 22, 45), 4168.0, 4178.0, 4167.0, 4173.0),
            bar(dt(2026, 8, 13, 23, 0), 4173.0, 4177.0, 4170.0, 4171.0),
            bar(dt(2026, 8, 14, 9, 15), 4169.0, 4190.0, 4163.0, 4187.0),
            bar(dt(2026, 8, 14, 9, 30), 4187.0, 4202.0, 4187.0, 4199.0),
            bar(dt(2026, 8, 14, 9, 45), 4199.0, 4202.0, 4184.0, 4188.0),
            bar(dt(2026, 8, 14, 10, 0), 4188.0, 4194.0, 4187.0, 4192.0),
            bar(dt(2026, 8, 14, 10, 15), 4191.0, 4205.0, 4188.0, 4203.0),
            bar(dt(2026, 8, 14, 10, 45), 4201.0, 4216.0, 4199.0, 4213.0),
            bar(dt(2026, 8, 14, 11, 0), 4213.0, 4218.0, 4209.0, 4212.0),
            bar(dt(2026, 8, 14, 11, 15), 4212.0, 4214.0, 4202.0, 4206.0),
            bar(dt(2026, 8, 14, 11, 30), 4207.0, 4210.0, 4198.0, 4209.0),
            bar(dt(2026, 8, 14, 13, 45), 4211.0, 4225.0, 4208.0, 4221.0),
            bar(dt(2026, 8, 14, 14, 0), 4221.0, 4233.0, 4219.0, 4233.0),
            bar(dt(2026, 8, 14, 14, 15), 4233.0, 4242.0, 4233.0, 4238.0),
            bar(dt(2026, 8, 14, 14, 30), 4238.0, 4241.0, 4221.0, 4229.0),
            bar(dt(2026, 8, 14, 14, 45), 4228.0, 4230.0, 4214.0, 4218.0),
            bar(dt(2026, 8, 14, 15, 0), 4218.0, 4221.0, 4212.0, 4212.0),
            bar(dt(2026, 8, 14, 21, 15), 4191.0, 4210.0, 4184.0, 4190.0),
            bar(dt(2026, 8, 14, 21, 30), 4190.0, 4192.0, 4184.0, 4188.0),
            bar(dt(2026, 8, 14, 21, 45), 4189.0, 4190.0, 4176.0, 4180.0),
            bar(dt(2026, 8, 14, 22, 0), 4180.0, 4188.0, 4180.0, 4181.0),
            bar(dt(2026, 8, 14, 22, 15), 4181.0, 4186.0, 4179.0, 4180.0),
            bar(dt(2026, 8, 14, 22, 30), 4180.0, 4192.0, 4180.0, 4186.0),
            bar(dt(2026, 8, 14, 22, 45), 4186.0, 4189.0, 4184.0, 4186.0),
            bar(dt(2026, 8, 14, 23, 0), 4186.0, 4187.0, 4180.0, 4180.0),
        ]
    }

    #[test]
    fn replay_keeps_same_warning_bar_but_does_not_reference_future() {
        let dt = DT {
            year: 2026,
            month: 8,
            day: 14,
            hour: 11,
            minute: 30,
        };
        // Short synthetic series: no realistic pattern, but the call should
        // terminate and only use bars up to each cut-off.
        let mut bars = Vec::new();
        for i in 0..40 {
            let base = 4000.0 + i as f64;
            bars.push(bar(dt, base, base + 5.0, base - 5.0, base + 1.0));
        }
        let events = replay_warnings("TST", &bars, 1.0);
        for e in &events {
            assert!(e.warning_index < bars.len());
            assert!(e.s2_index <= e.warning_index);
        }
    }

    #[test]
    fn bu0_1130_wick_is_forward_and_immutable() {
        let all = bu0_series();
        let w = all
            .iter()
            .position(|b| b.dt == dt(2026, 8, 14, 11, 30))
            .expect("BU0 11:30 bar exists");

        let first = replay_warnings("BU0", &all[..=w], 1.0);
        let ev = first
            .iter()
            .find(|e| e.warning_index == w)
            .expect("11:30 bar is recognized as a warning immediately");
        assert_eq!(ev.direction, Dir::Up);
        assert_eq!(ev.warning_kind, "wick");

        let snapshot = (
            ev.s0_index,
            ev.s1_index,
            ev.s2_index,
            ev.warning_index,
            ev.entry_score,
            ev.entry,
            ev.stop,
            ev.target,
            ev.dim_a,
            ev.dim_b,
            ev.dim_warning,
        );

        for end in w + 1..all.len() {
            let later = replay_warnings("BU0", &all[..=end], 1.0);
            let later_ev = later
                .iter()
                .find(|e| e.warning_index == w)
                .expect("warning stays present after later bars");
            assert_eq!(
                snapshot,
                (
                    later_ev.s0_index,
                    later_ev.s1_index,
                    later_ev.s2_index,
                    later_ev.warning_index,
                    later_ev.entry_score,
                    later_ev.entry,
                    later_ev.stop,
                    later_ev.target,
                    later_ev.dim_a,
                    later_ev.dim_b,
                    later_ev.dim_warning,
                ),
                "11:30 event must never be rewritten by later bars at end {end}"
            );
        }
    }

    #[test]
    fn wick_close_direction_lowers_replay_dim_warning() {
        let all = bu0_series();
        let w = all
            .iter()
            .position(|b| b.dt == dt(2026, 8, 14, 11, 30))
            .expect("BU0 11:30 bar exists");

        let base = replay_warnings("BU0", &all[..=w], 1.0);
        let bullish = base
            .iter()
            .find(|e| e.warning_index == w)
            .expect("11:30 bar is recognized as a warning immediately");
        assert_eq!(bullish.direction, Dir::Up);
        assert_eq!(bullish.warning_kind, "wick");
        assert!((bullish.dim_warning - 3.5).abs() < 1e-9);

        // 同一根长下影改为收阴：识别门槛仍通过，但 dim_warning 轻扣 0.1
        let mut bearish = all[..=w].to_vec();
        bearish[w] = bar(dt(2026, 8, 14, 11, 30), 4209.8, 4210.0, 4198.0, 4208.6);
        let events = replay_warnings("BU0", &bearish, 1.0);
        let ev = events
            .iter()
            .find(|e| e.warning_index == w)
            .expect("modified 11:30 bar still forms a wick warning");
        assert_eq!(ev.direction, Dir::Up);
        assert_eq!(ev.warning_kind, "wick");
        assert!((ev.dim_warning - 3.4).abs() < 1e-9);
        // 收盘改阴让b段出现“大阴+小阴”，新判定的b段动能衰减使 dim_b +0.3，
        // 部分抵消预警方向微调的 -0.1（dim_warning权重0.2、dim_b权重0.2）。
        assert!((bullish.entry_score - ev.entry_score + 0.04).abs() < 1e-9);
    }

    #[test]
    fn low_a_full_b_stays_below_2_5() {
        // C0 1251 对照：A 腿 1.207、B 腿满分 5.0、fast 预警 2.5。
        let score = composite_entry_score(1.207, 5.0, 2.5, "fast");
        assert!((score - 2.2243).abs() < 1e-4);
        assert!(score < 2.5);
    }

    #[test]
    fn bu0_1381_half_range_reverse_shadow_is_rejected_by_b_grade_gate() {
        // BU0 1381 复盘对照：22:30 阳线 O4180 H4192 L4180 C4186，
        // 实体 6、上影 6、振幅 12，反向影线正好 50%，B级不识别为强反转。
        let atr20 = vec![Some(10.0); 3];
        let trend_k = vec![(false, false); 3];
        let bars = vec![
            bar(dt(2026, 8, 14, 21, 0), 4233.0, 4242.0, 4233.0, 4238.0),
            bar(dt(2026, 8, 14, 21, 15), 4181.0, 4186.0, 4179.0, 4180.0),
            bar(dt(2026, 8, 14, 21, 30), 4180.0, 4192.0, 4180.0, 4186.0),
        ];
        let p = NPattern {
            grade: Grade::B,
            s1: Swing {
                index: 0,
                price: 4242.0,
                is_high: true,
            },
            s2: Swing {
                index: 1,
                price: 4180.0,
                is_high: false,
            },
            ..pattern_for(Dir::Up, 4143.0, 4242.0, 4180.0)
        };
        assert_eq!(warning_kind_at(&bars, &atr20, &trend_k, &p, 2), None);
    }

    #[test]
    fn oversized_warning_body_lowers_realtime_dim_warning_for_both_directions() {
        let atr20 = vec![Some(10.0), Some(10.0), Some(10.0)];
        let t = dt(2026, 8, 7, 21, 15);

        // 做多镜像：S1=150，预警K线高点148、实体140→147，实体覆盖剩余空间3.5倍。
        let up_trend = vec![(false, false), (false, false), (true, false)];
        let up_relaxed = vec![(false, false), (true, false), (false, false)];
        let up_bars = vec![
            bar(t, 100.0, 100.0, 100.0, 100.0),
            bar(t, 110.0, 150.0, 109.0, 149.0),
            bar(t, 140.0, 148.0, 138.0, 147.0),
        ];
        let up_small_bars = vec![
            bar(t, 100.0, 100.0, 100.0, 100.0),
            bar(t, 110.0, 150.0, 109.0, 149.0),
            bar(t, 140.0, 148.0, 138.0, 141.0),
        ];
        let p_up = pattern_for(Dir::Up, 100.0, 150.0, 138.0);
        let big_up = candidate_for(&up_bars, &atr20, &up_trend, &up_relaxed, &p_up, 2, 1.0)
            .expect("big bullish warning is recognized");
        let small_up = candidate_for(
            &up_small_bars,
            &atr20,
            &up_trend,
            &up_relaxed,
            &p_up,
            2,
            1.0,
        )
        .expect("small bullish warning is recognized");
        assert_eq!(big_up.warning_kind, "strong");
        assert!((big_up.dim_warning - 2.5).abs() < 1e-9);
        assert!((small_up.dim_warning - 3.5).abs() < 1e-9);
        assert!((small_up.entry_score - big_up.entry_score - 0.2).abs() < 1e-9);

        // 做空镜像：S1=100，预警K线低点102、实体110→103，同样扣满1.0。
        let down_trend = vec![(false, false), (false, false), (false, true)];
        let down_relaxed = vec![(false, false), (false, true), (false, false)];
        let down_bars = vec![
            bar(t, 150.0, 150.0, 149.0, 149.0),
            bar(t, 110.0, 112.0, 100.0, 101.0),
            bar(t, 110.0, 112.0, 102.0, 103.0),
        ];
        let down_small_bars = vec![
            bar(t, 150.0, 150.0, 149.0, 149.0),
            bar(t, 110.0, 112.0, 100.0, 101.0),
            bar(t, 110.0, 112.0, 102.0, 109.0),
        ];
        let p_down = pattern_for(Dir::Down, 150.0, 100.0, 112.0);
        let big_down = candidate_for(
            &down_bars,
            &atr20,
            &down_trend,
            &down_relaxed,
            &p_down,
            2,
            1.0,
        )
        .expect("big bearish warning is recognized");
        let small_down = candidate_for(
            &down_small_bars,
            &atr20,
            &down_trend,
            &down_relaxed,
            &p_down,
            2,
            1.0,
        )
        .expect("small bearish warning is recognized");
        assert_eq!(big_down.warning_kind, "strong");
        assert!((big_down.dim_warning - 2.5).abs() < 1e-9);
        assert!((small_down.dim_warning - 3.5).abs() < 1e-9);
        assert!((small_down.entry_score - big_down.entry_score - 0.2).abs() < 1e-9);
    }

    #[test]
    fn long_b_blocks_fast_warning_kind() {
        let atr20 = vec![Some(30.0); 3];
        let trend_k = vec![(false, false); 3];
        let bars = vec![
            bar(dt(2026, 8, 13, 21, 0), 90.0, 91.0, 89.0, 90.0), // s0
            bar(dt(2026, 8, 13, 21, 15), 95.0, 100.0, 94.0, 99.0), // s1
            bar(dt(2026, 8, 13, 21, 30), 96.0, 98.0, 95.0, 97.5), // 普通小阳线
        ];
        let p = NPattern {
            b_bars: 12,
            b_too_long: true,
            ..pattern_for(Dir::Up, 90.0, 100.0, 95.0)
        };

        assert_eq!(warning_kind_at(&bars, &atr20, &trend_k, &p, 2), None);

        let short_b = NPattern {
            b_bars: 3,
            b_too_long: false,
            ..p
        };
        assert_eq!(
            warning_kind_at(&bars, &atr20, &trend_k, &short_b, 2),
            Some("fast")
        );
    }
}
