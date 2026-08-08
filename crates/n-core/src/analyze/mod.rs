pub mod dto;
pub mod indicators;
pub mod io;
pub mod model;
pub mod outcome;
pub mod pattern;
pub mod report;
pub mod scoring;
pub mod time;

use anyhow::{Context, Result};

use crate::analyze::dto::{AnalysisDetail, SignalOutcome};
use crate::analyze::model::{Bar, Dir, NPattern, SignalCheck, ATR_PERIOD};

type SignalTuple<'a> = (usize, &'a NPattern, SignalCheck);

/// 单品种分析结果：完整报告文本 + 结构化信号 + 摘要文本。
pub struct AnalysisOutcome {
    pub full_report: String,
    pub summary: Option<String>,
    pub detail: AnalysisDetail,
}

/// 对一组 15m/60m bar 运行完整分析（核心逻辑，与旧 CSV 路径共用）。
/// `tick` 为品种最小变动价位，用于入场价偏移。
pub fn analyze_bars(
    symbol: &str,
    bars15: &[Bar],
    bars60: &[Bar],
    tick: f64,
) -> Result<AnalysisOutcome> {
    let atr15 = indicators::atr(bars15, ATR_PERIOD);
    let trend_k = indicators::trend_flags(bars15, &atr15);
    let trend60 = indicators::analyze_60m(bars60);

    let up_count = trend_k.iter().filter(|x| x.0).count();
    let down_count = trend_k.iter().filter(|x| x.1).count();
    let swings_fine = indicators::find_swings(bars15, &atr15, 2, 8);
    let swings_large = indicators::find_swings(bars15, &atr15, 5, 10);
    let fine = pattern::analyze_level(
        "fine",
        bars15,
        &swings_fine,
        &trend_k,
        pattern::FINE_MAX_A_BARS,
        pattern::FINE_MAX_B_BARS,
    );
    let large = pattern::analyze_level(
        "large",
        bars15,
        &swings_large,
        &trend_k,
        pattern::LARGE_MAX_A_BARS,
        pattern::LARGE_MAX_B_BARS,
    );

    // a段有效性校验：过滤微型N、过滤a段没有同向趋势K线的伪结构
    let trend_k_relaxed = indicators::trend_flags_relaxed(bars15, &atr15);
    let keep_valid = |p: &NPattern| {
        !pattern::is_small_n(p, &atr15)
            && pattern::a_leg_strong_count(p, &trend_k_relaxed) >= pattern::MIN_STRONG_A_LEG
    };
    let fine: Vec<NPattern> = fine.into_iter().filter(|p| keep_valid(p)).collect();
    let large: Vec<NPattern> = large.into_iter().filter(|p| keep_valid(p)).collect();

    let latest_down_fine = pattern::latest_pattern(&fine, Dir::Down);
    let latest_down_large = pattern::latest_pattern(&large, Dir::Down);
    let latest_up_fine = pattern::latest_pattern(&fine, Dir::Up);
    // 上涨大级别此前未进入候选：识别阶段会算出并保留合法的“较大”上涨N，
    // 但这里没有对应的 latest_up_large，导致这类结构（常见于a段较长、
    // 超过精细16根上限的行情）在生成信号前被静默丢弃，K线页与列表页都看不到。
    // 现补上该槽位；与上涨精细同起止点时由 dedup_signals 按评分保留更优者。
    let latest_up_large = pattern::latest_pattern(&large, Dir::Up);

    let mut candidates: Vec<SignalTuple> = Vec::new();
    if let Some(p) = latest_down_large {
        candidates.push((0, p, scoring::evaluate_signal_with_tick(bars15, &atr15, p, &trend60, tick)));
    }
    if let Some(p) = latest_down_fine {
        candidates.push((0, p, scoring::evaluate_signal_with_tick(bars15, &atr15, p, &trend60, tick)));
    }
    if let Some(p) = latest_up_fine {
        candidates.push((0, p, scoring::evaluate_signal_with_tick(bars15, &atr15, p, &trend60, tick)));
    }
    if let Some(p) = latest_up_large {
        candidates.push((0, p, scoring::evaluate_signal_with_tick(bars15, &atr15, p, &trend60, tick)));
    }
    let signals = dedup_signals(candidates);

    let mut full = Vec::new();
    report::write_full_report(
        &mut full,
        symbol,
        bars15,
        bars60,
        &trend60,
        &atr15,
        up_count,
        down_count,
        &swings_fine,
        &swings_large,
        &signals,
    )?;
    let full = String::from_utf8(full).context("完整报告不是合法UTF-8")?;

    let mut blocks = Vec::new();
    for (number, p, sc) in &signals {
        if report::is_active_signal(sc) {
            let mut text = Vec::new();
            report::write_signal_summary(&mut text, symbol, bars15, *number, p, sc)?;
            let text = String::from_utf8(text).context("信号摘要不是合法UTF-8")?;
            blocks.push(SummaryBlock {
                priority: signal_priority(sc),
                score: sc.total,
                text,
            });
        }
    }
    blocks.sort_by(|a, b| {
        a.priority
            .cmp(&b.priority)
            .then(b.score.total_cmp(&a.score))
    });

    let summary = if blocks.is_empty() {
        None
    } else {
        Some(render_single_summary(symbol, bars15, bars60, &trend60, &blocks))
    };

    let detail = dto::build_detail(symbol, bars15, &trend60, &signals, &full);
    Ok(AnalysisOutcome {
        full_report: full,
        summary,
        detail,
    })
}

/// 旧 CSV 路径：保持与历史行为一致，供回归测试与交叉校验。
pub fn analyze_csv_pair(
    symbol: &str,
    path15: &std::path::Path,
    path60: &std::path::Path,
) -> Result<AnalysisOutcome> {
    let bars15 = io::load_csv(&path15.to_string_lossy())?;
    let bars60 = io::load_csv(&path60.to_string_lossy())?;
    analyze_bars(symbol, &bars15, &bars60, 1.0)
}

pub struct SummaryBlock {
    priority: u8,
    score: f64,
    text: String,
}

pub fn render_single_summary(
    symbol: &str,
    bars15: &[model::Bar],
    bars60: &[model::Bar],
    trend60: &model::Trend60,
    blocks: &[SummaryBlock],
) -> String {
    let mut out = String::new();
    out.push_str("=== 综合结论 ===\n");
    out.push_str(&format!("扫描时间: {}\n", time::now_display()));
    out.push('\n');
    out.push_str(&format!(
        "{} | 60分钟方向: {} | 15m截至 {} | 60m截至 {}\n",
        symbol,
        report::direction_label(&trend60.direction),
        bars15.last().map(|b| b.dt.to_string()).unwrap_or_default(),
        bars60.last().map(|b| b.dt.to_string()).unwrap_or_default()
    ));
    for block in blocks {
        out.push_str(&block.text);
        if !block.text.ends_with('\n') {
            out.push('\n');
        }
    }
    out
}

fn pattern_quality_flags(p: &NPattern) -> usize {
    p.a_too_long as usize + p.b_too_long as usize + p.b_fast as usize
}

fn level_rank(level: &str) -> u8 {
    if level == "fine" {
        0
    } else {
        1
    }
}

/// 同一方向、a/b 段端点完全相同的信号只保留质量最高的一个。
fn dedup_signals(candidates: Vec<SignalTuple<'_>>) -> Vec<SignalTuple<'_>> {
    let mut candidates = candidates;
    candidates.sort_by(|a, b| {
        b.2.total
            .total_cmp(&a.2.total)
            .then(pattern_quality_flags(&a.1).cmp(&pattern_quality_flags(&b.1)))
            .then((a.1.a_bars + a.1.b_bars).cmp(&(b.1.a_bars + b.1.b_bars)))
            .then(level_rank(a.1.level).cmp(&level_rank(b.1.level)))
    });

    let mut seen: Vec<(Dir, usize, usize)> = Vec::new();
    let mut out = Vec::new();
    for (_, p, sc) in candidates {
        let key = (p.dir, p.s1.index, p.s2.index);
        if seen.contains(&key) {
            continue;
        }
        seen.push(key);
        out.push((out.len() + 1, p, sc));
    }
    out
}

fn signal_priority(sc: &SignalCheck) -> u8 {
    match sc.state {
        "即将触发" => 0,
        "当前已触发" => 1,
        "已触发，接近时效边界" => 2,
        _ => 3,
    }
}

/// 从分析结果中收集活跃信号（供扫描持久化与事件广播）。
pub fn collect_active(outcome: &AnalysisOutcome) -> Vec<SignalOutcome> {
    outcome
        .detail
        .signals
        .iter()
        .filter(|s| s.active)
        .map(|s| SignalOutcome {
            symbol: outcome.detail.symbol.clone(),
            signal: s.clone(),
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::analyze::model::{Grade, Swing};

    fn mk(
        level: &'static str,
        dir: Dir,
        s1: usize,
        s2: usize,
        score: f64,
    ) -> (NPattern, SignalCheck) {
        let p = NPattern {
            level,
            dir,
            s0: Swing {
                index: 0,
                price: 100.0,
                is_high: dir == Dir::Down,
            },
            s1: Swing {
                index: s1,
                price: 110.0,
                is_high: dir == Dir::Up,
            },
            s2: Swing {
                index: s2,
                price: 106.0,
                is_high: dir == Dir::Down,
            },
            a_bars: s1,
            b_bars: s2 - s1,
            a_move: 20.0,
            b_move: 10.0,
            retracement: 0.5,
            grade: Grade::A,
            hard_failure: false,
            a_too_long: false,
            b_too_long: false,
            b_fast: false,
            a_strong_trend: 0,
            b_strong_reverse: 0,
            c_move: 0.0,
            c_bars: 0,
            c_extended: false,
            c_hard_failure: false,
        };
        let mut sc = SignalCheck::new();
        sc.total = score;
        (p, sc)
    }

    #[test]
    fn dedup_keeps_higher_score() {
        let (p1, sc1) = mk("large", Dir::Down, 5, 9, 3.0);
        let (p2, sc2) = mk("fine", Dir::Down, 5, 9, 4.0);
        let out = dedup_signals(vec![(0, &p1, sc1), (0, &p2, sc2)]);
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].0, 1);
        assert_eq!(out[0].1.level, "fine");
        assert_eq!(out[0].1.s1.index, 5);
        assert_eq!(out[0].1.s2.index, 9);
    }

    #[test]
    fn dedup_keeps_fine_on_score_tie() {
        let (p1, sc1) = mk("large", Dir::Down, 5, 9, 4.0);
        let (p2, sc2) = mk("fine", Dir::Down, 5, 9, 4.0);
        let out = dedup_signals(vec![(0, &p1, sc1), (0, &p2, sc2)]);
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].1.level, "fine");
    }

    #[test]
    fn dedup_keeps_different_b_legs() {
        let (p1, sc1) = mk("fine", Dir::Down, 5, 9, 4.0);
        let (p2, sc2) = mk("fine", Dir::Down, 6, 9, 2.0);
        let out = dedup_signals(vec![(0, &p1, sc1), (0, &p2, sc2)]);
        assert_eq!(out.len(), 2);
        assert_eq!(out[0].0, 1);
        assert_eq!(out[1].0, 2);
    }

    #[test]
    fn different_directions_are_not_deduped() {
        let (p1, sc1) = mk("fine", Dir::Down, 5, 9, 4.0);
        let (p2, sc2) = mk("fine", Dir::Up, 5, 9, 4.0);
        let out = dedup_signals(vec![(0, &p1, sc1), (0, &p2, sc2)]);
        assert_eq!(out.len(), 2);
    }
}

