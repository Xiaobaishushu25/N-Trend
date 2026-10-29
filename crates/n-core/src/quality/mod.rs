//! Causal, independent display-only quality. Legacy selection and execution never call these scores.
pub mod assistant_review;
pub mod blind;
pub mod qualification;
pub mod report;
pub mod research;
pub mod store;
pub mod structure;
// pub mod structure_study;

use crate::{
    analyze::{indicators, model::Bar},
    storage::entities::pattern_events,
};
use n_protocol::quality::{QualityContribution, SignalQualityDto};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

pub const FEATURE_VERSION: &str = "quality-process-v1";
pub const RULES_JSON: &str = include_str!("quality-rules-v1.json");

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Rules {
    pub version: String,
    pub warmup_min: usize,
    pub warmup_max: usize,
    pub core_min: usize,
    pub slope_lags: [usize; 2],
    pub slope_bounds: [f64; 2],
    pub ma_noise_atr: f64,
    pub trend_weights: [f64; 3],
    pub a_weights: [f64; 4],
    pub drawdown_bounds: [f64; 2],
    pub stall_bounds: [f64; 2],
    pub progress_atr: f64,
    pub overlap_displacement_atr: f64,
    pub amplitude_bounds: [f64; 2],
    pub b_weights: [f64; 4],
    pub retracement_bounds: [f64; 2],
    pub relative_speed_bounds: [f64; 2],
    pub decay_min_bars: usize,
    pub decay_full: f64,
    pub departure_bounds: [f64; 2],
    pub proximity_bounds: [f64; 2],
    pub damage_bounds: [f64; 2],
    pub cross_bounds: [f64; 2],
    pub confirmation_bounds: [f64; 2],
    pub morphology_weights: [f64; 3],
    pub quality_weights: [f64; 2],
}
impl Default for Rules {
    fn default() -> Self {
        serde_json::from_str(RULES_JSON).expect("bundled quality rules")
    }
}
impl Rules {
    pub fn validate(&self) -> anyhow::Result<()> {
        for weights in [
            &self.trend_weights[..],
            &self.a_weights[..],
            &self.b_weights[..],
            &self.morphology_weights[..],
            &self.quality_weights[..],
        ] {
            anyhow::ensure!(
                weights.iter().all(|x| x.is_finite() && *x >= 0.)
                    && (weights.iter().sum::<f64>() - 1.).abs() < 1e-9,
                "quality weights must sum to one"
            );
        }
        for b in [
            self.slope_bounds,
            self.drawdown_bounds,
            self.stall_bounds,
            self.amplitude_bounds,
            self.retracement_bounds,
            self.relative_speed_bounds,
            self.departure_bounds,
            self.proximity_bounds,
            self.damage_bounds,
            self.cross_bounds,
            self.confirmation_bounds,
        ] {
            anyhow::ensure!(
                b[0].is_finite() && b[1].is_finite() && b[0] < b[1],
                "invalid quality bounds"
            );
        }
        anyhow::ensure!(
            self.warmup_min >= 40
                && self.warmup_max >= self.warmup_min
                && self.core_min >= 4
                && self
                    .slope_lags
                    .iter()
                    .all(|x| *x > 0 && *x <= self.warmup_min - 20),
            "invalid quality windows"
        );
        anyhow::ensure!(
            !self.version.is_empty()
                && self.decay_min_bars >= 4
                && [
                    self.ma_noise_atr,
                    self.progress_atr,
                    self.overlap_displacement_atr,
                    self.decay_full
                ]
                .iter()
                .all(|x| x.is_finite() && *x > 0.),
            "invalid quality constants"
        );
        Ok(())
    }
    pub fn hash(&self) -> String {
        hash(&serde_json::to_vec(self).expect("rules serialization"))
    }
}
pub fn hash(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
pub fn up(x: f64, b: [f64; 2]) -> f64 {
    let u = ((x - b[0]) / (b[1] - b[0])).clamp(0., 1.);
    u * u * (3. - 2. * u)
}
fn down(x: f64, b: [f64; 2]) -> f64 {
    1. - up(x, b)
}
fn mean(xs: &[f64]) -> f64 {
    xs.iter().sum::<f64>() / xs.len().max(1) as f64
}
fn median(mut xs: Vec<f64>) -> f64 {
    xs.sort_by(f64::total_cmp);
    if xs.is_empty() {
        return 0.;
    }
    (xs[(xs.len() - 1) / 2] + xs[xs.len() / 2]) / 2.
}
fn weighted(xs: &[f64], ws: &[f64]) -> f64 {
    xs.iter().zip(ws).map(|(x, w)| x * w).sum()
}
pub fn event_key(e: &pattern_events::Model) -> String {
    hash(
        format!(
            "{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}",
            e.symbol,
            e.direction,
            e.s0_ts,
            e.s1_ts,
            e.s2_ts,
            e.warning_ts,
            e.s0_price,
            e.s1_price,
            e.s2_price,
            e.entry,
            e.stop,
            e.target
        )
        .as_bytes(),
    )
}
fn put(q: &mut SignalQualityDto, name: &str, value: f64) {
    q.features
        .insert(name.into(), value.is_finite().then_some(value));
}
fn fail(mut q: SignalQualityDto, status: &str, reason: &str) -> SignalQualityDto {
    q.status = status.into();
    q.explanations.push(reason.into());
    q
}

/// Bars after warning are discarded before any validation or hash is computed.
/// `closed_through` is required: a caller cannot score a partial warning candle.
pub fn evaluate(
    e: &pattern_events::Model,
    input: &[Bar],
    tick: f64,
    closed_through: &str,
    origin: &str,
    rules: &Rules,
) -> SignalQualityDto {
    let mut q = SignalQualityDto {
        event_id: e.id,
        event_key: event_key(e),
        warning_ts: e.warning_ts.clone(),
        timeframe: "15m".into(),
        feature_version: FEATURE_VERSION.into(),
        rules_version: rules.version.clone(),
        parameter_hash: rules.hash(),
        input_hash: String::new(),
        origin: origin.into(),
        status: "ok".into(),
        morphology_score: None,
        space_score: None,
        quality_score: None,
        trend_score: None,
        a_score: None,
        b_score: None,
        features: Default::default(),
        contributions: vec![],
        explanations: vec![],
        flags: vec![],
    };
    if rules.validate().is_err() {
        return fail(q, "invalid_rules", "评分参数不合法");
    }
    if !tick.is_finite() || tick <= 0. {
        return fail(q, "missing_tick", "缺少有效tick");
    }
    if closed_through < e.warning_ts.as_str() {
        return fail(q, "partial_warning", "预警K线尚未收盘");
    }
    let prefix: Vec<_> = input
        .iter()
        .filter(|b| b.dt.to_bar_ts() <= e.warning_ts)
        .collect();
    let Some(start) = prefix.iter().position(|b| b.dt.to_bar_ts() == e.s0_ts) else {
        return fail(q, "insufficient_history", "行情未覆盖S0");
    };
    let bars: Vec<Bar> = prefix[start.saturating_sub(rules.warmup_max)..]
        .iter()
        .map(|b| (*b).clone())
        .collect();
    let s0 = start.min(rules.warmup_max);
    let Some(s1) = bars.iter().position(|b| b.dt.to_bar_ts() == e.s1_ts) else {
        return fail(q, "missing_endpoint", "行情缺少S1");
    };
    let Some(s2) = bars.iter().position(|b| b.dt.to_bar_ts() == e.s2_ts) else {
        return fail(q, "missing_endpoint", "行情缺少S2");
    };
    let Some(w) = bars.iter().position(|b| b.dt.to_bar_ts() == e.warning_ts) else {
        return fail(q, "missing_warning", "行情缺少预警K线");
    };
    let hash_bars: Vec<_> = bars
        .iter()
        .map(|b| {
            (
                b.dt.to_bar_ts(),
                b.open,
                b.high,
                b.low,
                b.close,
                b.volume,
                b.hold,
                b.rollover,
            )
        })
        .collect();
    q.input_hash = hash(
        &serde_json::to_vec(&(
            q.event_key.clone(),
            tick,
            &hash_bars,
            calendar_signature(&e.symbol, &bars),
        ))
        .unwrap_or_default(),
    );
    if s0 < rules.warmup_min {
        return fail(q, "insufficient_history", "S0之前不足40根已完成K线");
    }
    if !(s0 < s1 && s1 < s2 && s2 <= w) {
        return fail(q, "invalid_geometry", "端点顺序异常");
    }
    if bars.iter().any(|b| {
        ![b.open, b.high, b.low, b.close, b.volume, b.hold]
            .iter()
            .all(|x| x.is_finite())
            || b.high < b.open.max(b.close)
            || b.low > b.open.min(b.close)
            || b.high < b.low
    }) {
        return fail(q, "invalid_bars", "K线数值或OHLC关系异常");
    }
    if bars.windows(2).any(|p| p[0].dt >= p[1].dt) {
        return fail(q, "invalid_bars", "K线时间重复或乱序");
    }
    // The warmup is part of trend evidence. Do not bridge a contract switch silently.
    if bars.iter().any(|b| b.rollover) {
        return fail(q, "rollover", "评分窗口包含换月，均线证据不可直接延续");
    }
    if has_missing_session_bar(&e.symbol, &bars) {
        return fail(q, "missing_data", "交易时段存在缺失15分钟K线");
    }
    if ![
        e.s0_price, e.s1_price, e.s2_price, e.entry, e.stop, e.target, e.a_move, e.b_move,
    ]
    .iter()
    .all(|x| x.is_finite())
        || !matches!(e.direction.as_str(), "up" | "down")
        || e.a_move <= 0.
        || e.a_bars <= 0
        || e.b_bars <= 0
    {
        return fail(q, "invalid_geometry", "结构价格、幅度或根数异常");
    }
    let d = if e.direction == "up" { 1. } else { -1. };
    let risk = d * (e.entry - e.stop);
    let rr = d * (e.s1_price - e.entry) / risk;
    if risk <= 0. || !rr.is_finite() || rr <= 0. {
        return fail(q, "invalid_geometry", "入场到止损或S1的空间异常");
    }
    let atr = indicators::atr(&bars, 20);
    let ma = indicators::ma_series(&bars, 20);
    let Some(ar) = atr[s1].filter(|x| *x > 0.) else {
        return fail(q, "invalid_atr", "缺少有效ATR20(S1)");
    };
    if (s0 - 11..=w).any(|i| atr[i].is_none_or(|x| x <= 0.) || ma[i].is_none()) {
        return fail(q, "invalid_atr", "均线或ATR预热不足");
    }
    let distance = |i: usize| d * (bars[i].close - ma[i].unwrap()) / atr[i].unwrap();
    let slope = |i: usize| {
        mean(
            &rules
                .slope_lags
                .map(|k| d * (ma[i].unwrap() - ma[i - k].unwrap()) / atr[i].unwrap()),
        )
    };
    let pre_slope = slope(s0 - 1);
    let a_slope = median((s0..=s1).map(slope).collect());
    let residence = (s0..=s1)
        .filter(|&i| distance(i) >= -rules.ma_noise_atr)
        .count() as f64
        / (s1 - s0 + 1) as f64;
    let a_cross = crosses((s0..=s1).map(distance), rules.ma_noise_atr);
    let b_cross = crosses((s1 + 1..=w).map(distance), rules.ma_noise_atr);
    let residence_clean = residence * (1. - a_cross as f64 / (s1 - s0).max(1) as f64);
    let trend_parts = [
        up(pre_slope, rules.slope_bounds),
        up(a_slope, rules.slope_bounds),
        residence_clean,
    ];
    let t = 100. * weighted(&trend_parts, &rules.trend_weights);
    put(&mut q, "ma20_slope_pre", pre_slope);
    put(&mut q, "ma20_slope_a", a_slope);
    put(&mut q, "ma20_slope_warning", slope(w));
    put(&mut q, "warning_close_ma_atr", distance(w));
    put(&mut q, "ma_residence", residence);
    put(&mut q, "a_ma_crosses", a_cross as f64);
    put(&mut q, "b_ma_crosses", b_cross as f64);
    if pre_slope > 0. {
        q.explanations.push("S0之前均线已有同向背景".into());
    }
    if a_slope > 0. {
        q.explanations
            .push("A段均线向推动方向发展，建立趋势".into());
    }
    if pre_slope < 0. {
        q.explanations
            .push("S0之前均线逆向，包含修复或趋势转向背景".into());
    }
    if a_cross > 1 {
        q.explanations
            .push(format!("A段收盘反复穿越均线{a_cross}次"));
    }
    if distance(w) < -rules.ma_noise_atr {
        q.explanations
            .push("预警时价格处于MA20不利侧，回撤已有破坏".into());
    }
    let (lo, hi) = if s1 - s0 - 1 >= rules.core_min {
        (s0 + 1, s1 - 1)
    } else {
        q.flags.push("endpoint_sensitive".into());
        (s0, s1)
    };
    let core = &bars[lo..=hi];
    let a = path(
        core,
        d,
        tick.max(rules.progress_atr * ar),
        e.a_move,
        ar,
        rules.overlap_displacement_atr,
    );
    let full = path(
        &bars[s0..=s1],
        d,
        tick.max(rules.progress_atr * ar),
        e.a_move,
        ar,
        rules.overlap_displacement_atr,
    );
    let (gap_sum, gap_count, gap_penalty) = legacy_gap(&bars, s0, s1, ar);
    let net_move = (e.a_move - gap_sum).max(0.);
    let amplitude = up(net_move / ar, rules.amplitude_bounds);
    let a_parts = [
        a.efficiency,
        down(a.drawdown, rules.drawdown_bounds),
        down(a.stall_fraction, rules.stall_bounds),
        1. - a.overlap,
    ];
    let a_score = 100. * amplitude * weighted(&a_parts, &rules.a_weights);
    for (name, v) in [
        ("a_core_efficiency", a.efficiency),
        ("a_full_efficiency", full.efficiency),
        ("a_drawdown_ratio", a.drawdown),
        ("a_stall_fraction", a.stall_fraction),
        ("a_stall_bars", a.stall_bars as f64),
        ("a_terminal_stall_bars", a.terminal_stall as f64),
        ("a_overlap_stagnation", a.overlap),
        ("a_net_move", net_move),
        ("a_move_atr", net_move / ar),
        ("atr_s1", ar),
        ("a_speed_atr", net_move / e.a_bars as f64 / ar),
        ("a_gap_sum", gap_sum),
        ("a_gap_count", gap_count as f64),
        ("a_gap_penalty", gap_penalty),
    ] {
        put(&mut q, name, v);
    }
    q.explanations.push(format!(
        "A段内部收盘路径效率{:.0}%，最长{}根、末端{}根未取得有效新进展",
        100. * a.efficiency,
        a.stall_bars,
        a.terminal_stall
    ));
    if a.overlap > 0.25 {
        q.explanations.push(format!(
            "A段重叠且缺少位移的争夺程度{:.0}%",
            100. * a.overlap
        ));
    }
    if gap_count > 0 {
        q.flags.push("large_gap".into());
        q.explanations.push(format!(
            "A段{}处大跳空，净幅度扣除{:.2}点",
            gap_count, gap_sum
        ));
    }
    let retracement = e.b_move / e.a_move;
    let relative_speed = (e.b_move / e.b_bars as f64) / (e.a_move / e.a_bars as f64);
    let pressure: Vec<_> = bars[s1 + 1..w]
        .iter()
        .map(|b| (-d * (b.close - b.open)).max(0.))
        .collect();
    let decay = if pressure.len() >= rules.decay_min_bars {
        let mid = pressure.len() / 2;
        let head = mean(&pressure[..mid]);
        (head > 0.).then(|| (1. - mean(&pressure[mid..]) / head).clamp(0., 1.))
    } else {
        None
    };
    let decay_score = decay.map(|x| up(x, [0., rules.decay_full])).unwrap_or(0.5);
    if decay.is_none() {
        q.flags.push("b_decay_unavailable".into());
        q.explanations
            .push("回撤衰减样本不足，采用中性分，不视为已观察到衰减".into());
    }
    q.features.insert("b_decay".into(), decay);
    let departure = (s0..=s1).map(distance).fold(0., f64::max);
    let proximity = (s1 + 1..=w)
        .map(|i| {
            let m = ma[i].unwrap();
            (bars[i].low - m).max(m - bars[i].high).max(0.) / atr[i].unwrap()
        })
        .fold(f64::INFINITY, f64::min);
    let damage = mean(
        &(s1 + 1..=w)
            .map(|i| (-distance(i)).max(0.))
            .collect::<Vec<_>>(),
    );
    let improvement = d * (bars[w].close - bars[w - 1].close) / atr[w].unwrap();
    let range = bars[w].high - bars[w].low;
    let close_location = if range > 0. {
        if d > 0. {
            (bars[w].close - bars[w].low) / range
        } else {
            (bars[w].high - bars[w].close) / range
        }
    } else {
        0.
    };
    let confirmation = up(improvement, rules.confirmation_bounds) * close_location;
    let retest = up(departure, rules.departure_bounds)
        * down(proximity, rules.proximity_bounds)
        * down(damage, rules.damage_bounds)
        * down(b_cross as f64, rules.cross_bounds)
        * confirmation;
    let b_parts = [
        down(retracement, rules.retracement_bounds),
        down(relative_speed, rules.relative_speed_bounds),
        decay_score,
        retest,
    ];
    let b_score = 100. * weighted(&b_parts, &rules.b_weights);
    for (name, v) in [
        ("retracement", retracement),
        ("b_a_speed_ratio", relative_speed),
        ("ma_departure_atr", departure),
        ("ma_proximity_atr", proximity),
        ("ma_damage_atr", damage),
        ("warning_improvement_atr", improvement),
        ("warning_close_location", close_location),
        ("ma_retest", retest),
        ("rr_to_s1", rr),
    ] {
        put(&mut q, name, v);
    }
    if proximity <= rules.proximity_bounds[0] {
        q.explanations
            .push("回撤返回MA20附近；结合穿线破坏与预警反应评价".into());
    }
    if relative_speed > 1. {
        q.explanations
            .push(format!("B段平均速度为A段的{relative_speed:.2}倍，回撤偏强"));
    }
    if let Some(x) = decay {
        q.explanations
            .push(format!("排除预警K后，回撤末端反向实体衰减{:.0}%", 100. * x));
    }
    let m = weighted(&[t, a_score, b_score], &rules.morphology_weights);
    let space = 100. * rr / (1. + rr);
    let total = weighted(&[m, space], &rules.quality_weights);
    q.morphology_score = Some(m);
    q.space_score = Some(space);
    q.quality_score = Some(total);
    q.trend_score = Some(t);
    q.a_score = Some(a_score);
    q.b_score = Some(b_score);
    q.explanations
        .push(format!("到S1的结构空间为{rr:.2}R；形态分与空间分分别保留"));
    for (name, score, weight) in [
        ("趋势背景", t, rules.morphology_weights[0]),
        ("A段推动", a_score, rules.morphology_weights[1]),
        ("B段回撤", b_score, rules.morphology_weights[2]),
    ] {
        q.contributions.push(QualityContribution {
            name: name.into(),
            score,
            weight,
            contribution: score * weight,
        });
    }
    for (group, parts, weights) in [
        ("T", &trend_parts[..], &rules.trend_weights[..]),
        ("A", &a_parts[..], &rules.a_weights[..]),
        ("B", &b_parts[..], &rules.b_weights[..]),
    ] {
        for (i, (score, weight)) in parts.iter().zip(weights).enumerate() {
            put(
                &mut q,
                &format!("{group}_part_{}_score", i + 1),
                100. * score,
            );
            put(&mut q, &format!("{group}_part_{}_weight", i + 1), *weight);
        }
    }
    // Legacy diagnostics are computed from the very same truncated bars, not outcomes.
    if let Some(p) = legacy_pattern(e, &bars) {
        let legacy = crate::analyze::scoring::a_leg_detail(&bars, &atr, &p);
        put(&mut q, "legacy_a_q", legacy.q);
        put(
            &mut q,
            "legacy_a_big_reverse_count",
            crate::analyze::scoring::a_leg_big_reverse_count(&bars, &atr, &p) as f64,
        );
        put(
            &mut q,
            "legacy_a_score",
            crate::analyze::scoring::score_a(&bars, &atr, &p),
        );
        put(
            &mut q,
            "legacy_b_score",
            crate::analyze::scoring::score_b(&p),
        );
        put(
            &mut q,
            "legacy_warning_score",
            crate::analyze::scoring::dim_warning(&bars, &atr, w, &e.warning_kind, p.dir),
        );
    }
    q
}

fn crosses(xs: impl Iterator<Item = f64>, noise: f64) -> usize {
    let mut side = 0;
    let mut n = 0;
    for x in xs {
        let next = if x > noise {
            1
        } else if x < -noise {
            -1
        } else {
            0
        };
        if next != 0 {
            if side != 0 && next != side {
                n += 1;
            }
            side = next;
        }
    }
    n
}
#[derive(Debug)]
struct Path {
    efficiency: f64,
    drawdown: f64,
    stall_fraction: f64,
    stall_bars: usize,
    terminal_stall: usize,
    overlap: f64,
}
fn path(bs: &[Bar], d: f64, eps: f64, amplitude: f64, atr: f64, displacement: f64) -> Path {
    let xs: Vec<_> = bs.iter().map(|b| d * b.close).collect();
    let travel = xs.windows(2).map(|p| (p[1] - p[0]).abs()).sum::<f64>();
    let efficiency = if travel > 0. {
        ((xs[xs.len() - 1] - xs[0]) / travel).clamp(0., 1.)
    } else {
        0.
    };
    let mut peak = xs[0];
    let mut record = xs[0];
    let mut dd: f64 = 0.;
    let mut stall = 0;
    let mut longest = 0;
    for &x in xs.iter().skip(1) {
        peak = peak.max(x);
        dd = dd.max(peak - x);
        if x - record + 1e-9 >= eps {
            record = x;
            stall = 0;
        } else {
            stall += 1;
            longest = longest.max(stall);
        }
    }
    let overlaps: Vec<_> = bs
        .windows(2)
        .map(|p| {
            let den = (p[0].high - p[0].low).min(p[1].high - p[1].low);
            if den > 0. {
                (p[0].high.min(p[1].high) - p[0].low.max(p[1].low)).max(0.) / den
            } else {
                0.
            }
        })
        .collect();
    let contested: Vec<_> = (2..bs.len())
        .map(|i| {
            (overlaps[i - 2] + overlaps[i - 1]) / 2.
                * (1. - ((xs[i] - xs[i - 2]).max(0.) / (displacement * atr)).clamp(0., 1.))
        })
        .collect();
    Path {
        efficiency,
        drawdown: dd / amplitude,
        stall_fraction: longest as f64 / (bs.len() - 1).max(1) as f64,
        stall_bars: longest,
        terminal_stall: stall,
        overlap: mean(&contested),
    }
}
fn legacy_gap(bs: &[Bar], s0: usize, s1: usize, atr: f64) -> (f64, usize, f64) {
    let mut sum = 0.;
    let mut count = 0;
    let mut excess = 0.;
    for p in bs[s0..=s1].windows(2) {
        let gap = (p[1].low - p[0].high).max(p[0].low - p[1].high).max(0.);
        if gap >= 0.8 * atr {
            sum += gap;
            count += 1;
            excess += gap / atr - 0.8;
        }
    }
    (sum, count, (count as f64 * 0.15 + excess * 0.2).min(0.5))
}

/// Rebuild a diagnostic pattern using frozen endpoints, never choose a new candidate.
pub(crate) fn legacy_pattern(
    e: &pattern_events::Model,
    bars: &[Bar],
) -> Option<crate::analyze::model::NPattern> {
    use crate::analyze::model::{Dir, Grade, NPattern, Swing};
    let ix = |ts: &str| bars.iter().position(|b| b.dt.to_bar_ts() == ts);
    let (s0, s1, s2) = (ix(&e.s0_ts)?, ix(&e.s1_ts)?, ix(&e.s2_ts)?);
    let dir = if e.direction == "up" {
        Dir::Up
    } else {
        Dir::Down
    };
    let grade = if e.grade.starts_with('A') {
        Grade::A
    } else if e.grade.starts_with('B') {
        Grade::B
    } else {
        Grade::C
    };
    let atr = indicators::atr(bars, 20);
    let trend = indicators::trend_flags(bars, &atr);
    let (weak, ratio) = crate::analyze::pattern::b_leg_weakening(bars, s1, s2, dir);
    let reverse = (s1 + 1..=s2)
        .filter(|&i| {
            if dir == Dir::Up {
                trend[i].1
            } else {
                trend[i].0
            }
        })
        .count();
    Some(NPattern {
        level: if e.level == "large" { "large" } else { "fine" },
        dir,
        grade,
        s0: Swing {
            index: s0,
            price: e.s0_price,
            is_high: dir == Dir::Down,
        },
        s1: Swing {
            index: s1,
            price: e.s1_price,
            is_high: dir == Dir::Up,
        },
        s2: Swing {
            index: s2,
            price: e.s2_price,
            is_high: dir == Dir::Down,
        },
        a_move: e.a_move,
        b_move: e.b_move,
        a_bars: e.a_bars as usize,
        b_bars: e.b_bars as usize,
        retracement: e.retracement,
        hard_failure: false,
        a_too_long: e.a_bars > 7,
        b_too_long: match grade {
            Grade::A => e.b_bars > 8,
            Grade::B => e.b_bars > 12,
            Grade::C => e.b_bars > 14,
            _ => e.b_bars > 8,
        },
        b_fast: e.b_move / e.b_bars as f64 > 0.8 * e.a_move / e.a_bars as f64,
        b_weakening: weak,
        b_weakening_ratio: ratio,
        a_strong_trend: 0,
        b_strong_reverse: reverse,
        c_move: 0.,
        c_bars: 0,
        c_extended: false,
        c_hard_failure: false,
    })
}

/// Inspect minute interiors using the exchange calendar in Asia/Shanghai semantics.
/// Midpoint checks avoid treating session-end bars as missing bars after a break.
fn calendar_signature(symbol: &str, bars: &[Bar]) -> Vec<(String, bool, bool)> {
    let mut values = Vec::new();
    let parse = |b: &Bar| {
        chrono::NaiveDateTime::parse_from_str(&b.dt.to_bar_ts(), "%Y-%m-%d %H:%M:%S")
            .ok()
            .map(|t| t.date())
    };
    if let (Some(mut day), Some(end)) = (bars.first().and_then(parse), bars.last().and_then(parse))
    {
        while day <= end {
            values.push((
                format!(
                    "{}|{:?}",
                    day,
                    crate::session::SessionCalendar::get(symbol).night_type
                ),
                crate::session::SessionCalendar::is_trading_day(&day),
                crate::session::SessionCalendar::has_night_session_tonight(&day),
            ));
            day += chrono::Duration::days(1);
        }
    }
    values
}

fn has_missing_session_bar(symbol: &str, bars: &[Bar]) -> bool {
    use chrono::Duration;
    let spec = crate::session::SessionCalendar::get(symbol);
    for p in bars.windows(2) {
        let Some(a) =
            chrono::NaiveDateTime::parse_from_str(&p[0].dt.to_bar_ts(), "%Y-%m-%d %H:%M:%S").ok()
        else {
            return true;
        };
        let Some(b) =
            chrono::NaiveDateTime::parse_from_str(&p[1].dt.to_bar_ts(), "%Y-%m-%d %H:%M:%S").ok()
        else {
            return true;
        };
        let mut x = a + Duration::minutes(15);
        while x < b {
            let mid = x - Duration::minutes(7);
            if spec.is_in_trading_time_naive(&mid) {
                return true;
            }
            x += Duration::minutes(15);
        }
    }
    false
}

#[cfg(test)]
mod tests;
