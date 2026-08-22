use std::io::{self, Write};

use crate::analyze::model::{Bar, Dir, NPattern, SignalCheck, Swing, Trend60};

fn yn(v: bool) -> &'static str {
    if v {
        "是"
    } else {
        "否"
    }
}

pub fn level_label(level: &str) -> &'static str {
    match level {
        "fine" => "精细",
        "large" => "较大",
        "box" => "箱体",
        _ => "自定义",
    }
}

pub fn direction_label(direction: &str) -> &'static str {
    match direction {
        "STRONG_UP" => "强多",
        "STRONG_DOWN" => "强空",
        "WEAK_UP" => "弱多",
        "WEAK_DOWN" => "弱空",
        "RANGE" | "NEUTRAL" => "震荡",
        "UP" => "上涨",
        "DOWN" => "下跌",
        _ => "震荡",
    }
}

pub fn num_label(n: usize) -> String {
    const NUMS: [&str; 10] = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩"];
    if n >= 1 && n <= NUMS.len() {
        NUMS[n - 1].to_string()
    } else {
        format!("[{}]", n)
    }
}

fn write_pattern_body(out: &mut dyn Write, bars: &[Bar], p: &NPattern) -> io::Result<()> {
    if p.level == "box" {
        let upper = p.s0.price.max(p.s1.price);
        let lower = p.s0.price.min(p.s1.price);
        writeln!(
            out,
            "箱体: 上轨 {:.1} | 下轨 {:.1} | 高度 {:.1}点",
            upper, lower, p.a_move
        )?;
        writeln!(out, "触碰: 上轨 {}次 | 下轨 {}次", p.b_bars, p.a_bars)?;
        writeln!(out, "预警K线: {}", bars[p.s2.index].dt)?;
        return Ok(());
    }
    writeln!(
        out,
        "a段: {} {:.1} -> {} {:.1} | {}根K | {:.1}点",
        bars[p.s0.index].dt, p.s0.price, bars[p.s1.index].dt, p.s1.price, p.a_bars, p.a_move
    )?;
    writeln!(
        out,
        "b段: {} {:.1} -> {} {:.1} | {}根K | {:.1}点 | 回撤{:.1}% | {}",
        bars[p.s1.index].dt,
        p.s1.price,
        bars[p.s2.index].dt,
        p.s2.price,
        p.b_bars,
        p.b_move,
        p.retracement * 100.0,
        p.grade.label()
    )?;
    writeln!(
        out,
        "质量: 硬失效={} a段过长={} b段过长={} b段过快={} b段动能衰减={} 强反向K={}",
        yn(p.hard_failure),
        yn(p.a_too_long),
        yn(p.b_too_long),
        yn(p.b_fast),
        yn(p.b_weakening),
        p.b_strong_reverse
    )?;
    writeln!(
        out,
        "c段: {:.1}点 / {}根K | 过度延伸={}",
        p.c_move,
        p.c_bars,
        yn(p.c_extended)
    )
}

fn write_signal_body(
    out: &mut dyn Write,
    bars: &[Bar],
    sc: &SignalCheck,
    show_dims: bool,
    spacing: bool,
) -> io::Result<()> {
    match (sc.warning, sc.trigger) {
        (Some(w), Some(t)) => {
            writeln!(out, "预警K线: {}", bars[w].dt)?;
            writeln!(out, "触发K线: {} | 距今{}根K线", bars[t].dt, sc.trigger_age)?;
            writeln!(
                out,
                "入场: {:.1} | 止损: {:.1} | 决策点(前低/前高): {:.1}",
                sc.entry, sc.stop, sc.decision_target
            )?;
            writeln!(
                out,
                "风险: {:.1} | 决策点空间: {:.1} | 决策点RR: {:.2}",
                sc.risk, sc.space, sc.rr
            )?;
        }
        (Some(w), None) => {
            writeln!(out, "预警K线: {}", bars[w].dt)?;
            writeln!(out, "触发K线: 尚未触发 | 等待突破预警K线")?;
            writeln!(
                out,
                "准备入场: {:.1} | 止损: {:.1} | 决策点(前低/前高): {:.1}",
                sc.entry, sc.stop, sc.decision_target
            )?;
            writeln!(
                out,
                "风险: {:.1} | 决策点空间: {:.1} | 决策点RR: {:.2}",
                sc.risk, sc.space, sc.rr
            )?;
        }
        _ => {}
    }

    if sc.entry_block_count > 0 {
        writeln!(
            out,
            "进场阻力: {}（{}项）",
            sc.entry_block_detail, sc.entry_block_count
        )?;
    }
    if show_dims && sc.category != "BOX" {
        writeln!(out, "入场评分构成:")?;
        writeln!(out, "  A段质量: {:.1}", sc.dim_a)?;
        writeln!(out, "  B段质量: {:.1}", sc.dim_b)?;
        writeln!(out, "  预警K线质量: {:.1}", sc.dim_warning)?;
    }
    if spacing {
        writeln!(out)?;
    }
    writeln!(out, "综合评分: {:.2} / 5.0", sc.total)?;
    writeln!(out, "评分建议: {}", sc.category)?;
    writeln!(out, "备注: {}", sc.note)
}

fn write_pattern(out: &mut dyn Write, bars: &[Bar], p: &NPattern, number: usize) -> io::Result<()> {
    if p.level == "box" {
        writeln!(out, "--- {} 箱体 {} ---", num_label(number), p.dir.label())?;
    } else {
        writeln!(
            out,
            "--- {} {} {} N ---",
            num_label(number),
            level_label(p.level),
            p.dir.label()
        )?;
    }
    write_pattern_body(out, bars, p)
}

fn write_signal(out: &mut dyn Write, bars: &[Bar], sc: &SignalCheck) -> io::Result<()> {
    writeln!(out, "--- 信号评分 ---")?;
    if sc.warning.is_none() {
        writeln!(out, "状态: {} | {}", sc.category, sc.note)?;
        return Ok(());
    }
    write_signal_body(out, bars, sc, true, false)
}

pub fn write_signal_summary(
    out: &mut dyn Write,
    symbol: &str,
    bars: &[Bar],
    number: usize,
    p: &NPattern,
    sc: &SignalCheck,
) -> io::Result<()> {
    if p.level == "box" {
        writeln!(
            out,
            "--- {} 箱体形态：{} {} | 信号状态: {} ---",
            symbol,
            num_label(number),
            p.dir.label(),
            sc.state
        )?;
    } else {
        writeln!(
            out,
            "--- {} {} 形态：{} {} N | 信号状态: {} ---",
            symbol,
            num_label(number),
            level_label(p.level),
            p.dir.label(),
            sc.state
        )?;
    }
    write_pattern_body(out, bars, p)?;
    writeln!(out)?;
    write_signal_body(out, bars, sc, false, true)
}

pub fn is_active_signal(sc: &SignalCheck) -> bool {
    is_active_signal_with_min(sc, 0.0)
}

/// 活跃信号判定：达到最低总分且状态仍在关注窗口内。
pub fn is_active_signal_with_min(sc: &SignalCheck, min_total: f64) -> bool {
    if sc.total <= 0.0 {
        return false;
    }
    sc.total >= min_total && matches!(sc.state, "即将触发" | "当前已触发" | "已触发，接近时效边界")
}

#[allow(clippy::too_many_arguments)]
pub fn write_full_report(
    out: &mut dyn Write,
    symbol: &str,
    bars15: &[Bar],
    bars60: &[Bar],
    trend60: &Trend60,
    atr15: &[Option<f64>],
    up_count: usize,
    down_count: usize,
    swings_fine: &[Swing],
    swings_large: &[Swing],
    signals: &[(usize, &NPattern, SignalCheck)],
) -> io::Result<()> {
    writeln!(out, "=== 品种 {} ===", symbol)?;
    writeln!(out)?;
    writeln!(out, "=== 输入 ===")?;
    writeln!(
        out,
        "15分钟: {}根K线，最早 {} 收盘 {:.1}，最新 {} 收盘 {:.1}",
        bars15.len(),
        bars15.first().unwrap().dt,
        bars15.first().unwrap().close,
        bars15.last().unwrap().dt,
        bars15.last().unwrap().close
    )?;
    writeln!(
        out,
        "60分钟: {}根K线，最早 {} 收盘 {:.1}，最新 {} 收盘 {:.1}",
        bars60.len(),
        bars60.first().unwrap().dt,
        bars60.first().unwrap().close,
        bars60.last().unwrap().dt,
        bars60.last().unwrap().close
    )?;

    writeln!(out)?;
    writeln!(out, "=== 60分钟趋势 ===")?;
    writeln!(out, "方向: {}", direction_label(&trend60.direction))?;
    writeln!(
        out,
        "最新收盘 {:.1}，MA20 {:.1}，斜率 {:.2}，收盘-MA20 {:.1}",
        bars60.last().unwrap().close,
        trend60.ma20,
        trend60.slope,
        trend60.price_vs_ma
    )?;
    if trend60.direction.contains("DOWN") {
        writeln!(
            out,
            "高点降低: {}，低点降低: {}",
            yn(trend60.lower_highs),
            yn(trend60.lower_lows)
        )?;
    } else {
        writeln!(
            out,
            "高点抬高: {}，低点抬高: {}",
            yn(trend60.higher_highs),
            yn(trend60.higher_lows)
        )?;
    }

    let atr_last = atr15.last().and_then(|x| *x).unwrap_or(0.0);
    writeln!(out)?;
    writeln!(out, "=== 15分钟结构 ===")?;
    writeln!(
        out,
        "ATR20: {:.1} | 强上涨K: {} | 强下跌K: {}",
        atr_last, up_count, down_count
    )?;
    writeln!(out, "精细摆动数: {}", swings_fine.len())?;
    writeln!(out, "较大摆动数: {}", swings_large.len())?;
    if let Some(s) = swings_fine.last() {
        writeln!(
            out,
            "最近精细摆动: {} {} {}",
            bars15[s.index].dt,
            if s.is_high { "高点" } else { "低点" },
            s.price
        )?;
    }
    if let Some(s) = swings_large.last() {
        writeln!(
            out,
            "最近较大摆动: {} {} {}",
            bars15[s.index].dt,
            if s.is_high { "高点" } else { "低点" },
            s.price
        )?;
    }
    for (number, p, _) in signals {
        write_pattern(out, bars15, p, *number)?;
    }

    writeln!(out)?;
    for (number, p, sc) in signals {
        if p.level == "box" {
            writeln!(
                out,
                "--- {} 箱体形态：{} | 信号状态: {} ---",
                num_label(*number),
                p.dir.label(),
                sc.state
            )?;
        } else {
            writeln!(
                out,
                "--- {} 形态：{} {} N | 信号状态: {} ---",
                num_label(*number),
                level_label(p.level),
                p.dir.label(),
                sc.state
            )?;
        }
        write_signal(out, bars15, sc)?;
    }

    writeln!(out)?;
    writeln!(out, "=== 关键位置 ===")?;
    writeln!(out, "最新15分钟低点: {:.1}", bars15.last().unwrap().low)?;
    writeln!(out, "最新15分钟高点: {:.1}", bars15.last().unwrap().high)?;
    writeln!(out, "60分钟MA20: {:.1}", trend60.ma20)?;
    let large_down = signals
        .iter()
        .find(|(_, p, _)| p.level == "large" && p.dir == Dir::Down);
    let fine_down = signals
        .iter()
        .find(|(_, p, _)| p.level == "fine" && p.dir == Dir::Down);
    if let Some((_, p, _)) = large_down {
        writeln!(out, "较大N a段终点(前低): {:.1}", p.s1.price)?;
        writeln!(out, "较大N b段终点(前高): {:.1}", p.s2.price)?;
    }
    if let Some((_, p, _)) = fine_down {
        writeln!(out, "精细N a段终点(前低): {:.1}", p.s1.price)?;
        writeln!(out, "精细N b段终点(前高): {:.1}", p.s2.price)?;
    }

    writeln!(out)?;
    writeln!(out, "=== 综合结论 ===")?;
    writeln!(out, "60分钟方向: {}", direction_label(&trend60.direction))?;
    let active: Vec<_> = signals
        .iter()
        .filter(|(_, _, sc)| is_active_signal(sc))
        .collect();
    if active.is_empty() {
        writeln!(out, "当前无关注信号")?;
    } else {
        for (number, p, sc) in active {
            writeln!(out)?;
            write_signal_summary(out, symbol, bars15, *number, p, sc)?;
        }
    }
    Ok(())
}
