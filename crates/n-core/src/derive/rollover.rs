//! 连续合约换月识别（增量版）。
//!
//! 识别分两段：
//! 1. 在本地连续 5m 序列上找出所有会话断裂点，跳空/持仓只作为候选信息，
//!    不再按阈值过滤，避免漏掉小跳空换月；
//! 2. 对候选点用月合约确认：断点前旧合约、断点后新合约的价格要贴合连续序列，
//!    并确认断点后的连续序列已经切到更晚的月份。持仓/成交不再作为一票否决，
//!    避免新浪已提前切换但新合约持仓尚未反超时漏标。

use anyhow::Result;
use chrono::NaiveDateTime;

use crate::fetch::kline::Kline;

/// 相邻 5m bar 间隔超过该值视为会话断裂（分钟级别聚合也使用同一口径）。
pub const SESSION_BREAK_MINUTES: i64 = 5;
/// 只有间隔达到该值的断点才可能成为换月候选。
/// 盘中 10:15-10:30、11:30-13:30 等短休不参与，换月只可能发生在收盘到开盘之间。
pub const ROLLOVER_BREAK_MIN_MINUTES: i64 = 180;
/// ATR 计算窗口（与策略分析一致）。
pub const ATR_WINDOW: usize = 20;
/// 确认时容忍的价格偏差比例。
pub const PRICE_TOLERANCE: f64 = 0.02;
/// 月合约确认时，断点两侧数据允许离断点的最大间隔（覆盖周末/长休市）。
pub const MAX_BREAK_SPAN_HOURS: i64 = 48;

const TS_FORMAT: &str = "%Y-%m-%d %H:%M:%S";

#[derive(Debug, Clone)]
pub struct RolloverCandidate {
    pub symbol: String,
    /// 换月后的第一根连续 bar 时间戳（断点 after 侧）。
    pub ts: String,
    /// 断点前最后一根连续 bar（旧合约侧）。
    pub before: Kline,
    /// 断点后第一根连续 bar（新合约侧）。
    pub after: Kline,
    /// 跳空幅度 / ATR。
    pub gap_r: f64,
    /// 断点前后连续合约持仓量相对变化。
    pub hold_change: f64,
    /// 断点前后连续合约成交量相对变化。
    pub volume_change: f64,
}

/// 月合约代码是否形如 BU2609（品种字母 + 4 位年月）。
pub fn is_month_contract(code: &str) -> bool {
    let digits = code.trim_start_matches(|c: char| !c.is_ascii_digit());
    !digits.is_empty() && digits.len() == 4
}

/// 已确认的换月：from_contract → to_contract。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RolloverRecord {
    pub symbol: String,
    pub ts: String,
    pub from_contract: String,
    pub to_contract: String,
    pub confirmed: bool,
}

/// 在升序 5m 连续序列中找出所有会话断裂点。
pub fn detect_candidates(symbol: &str, bars: &[Kline]) -> Vec<RolloverCandidate> {
    let mut out = Vec::new();
    for i in 1..bars.len() {
        let before = &bars[i - 1];
        let after = &bars[i];
        let Some(prev_dt) = parse_ts(&before.datetime) else {
            continue;
        };
        let Some(dt) = parse_ts(&after.datetime) else {
            continue;
        };
        let gap_minutes = (dt - prev_dt).num_minutes();
        if gap_minutes <= SESSION_BREAK_MINUTES || gap_minutes <= ROLLOVER_BREAK_MIN_MINUTES {
            continue;
        }
        let atr = local_atr(&bars[..i]).unwrap_or(0.0);
        let gap = (after.open - before.close).abs();
        let gap_r = gap / atr.max(1e-9);
        let hold_change = if before.hold > 0.0 {
            (after.hold - before.hold).abs() / before.hold
        } else {
            0.0
        };
        let volume_change = if before.volume > 0.0 {
            (after.volume - before.volume).abs() / before.volume
        } else {
            0.0
        };
        out.push(RolloverCandidate {
            symbol: symbol.to_string(),
            ts: after.datetime.clone(),
            before: before.clone(),
            after: after.clone(),
            gap_r,
            hold_change,
            volume_change,
        });
    }
    out
}

/// 断点附近的 ATR：只统计同一会话内相邻 5m bar 的 TR，避免断点本身污染基准。
fn local_atr(bars: &[Kline]) -> Option<f64> {
    let mut trs = Vec::new();
    for i in 0..bars.len() {
        if i == 0 {
            trs.push(bars[i].high - bars[i].low);
            continue;
        }
        let prev_dt = parse_ts(&bars[i - 1].datetime)?;
        let dt = parse_ts(&bars[i].datetime)?;
        if (dt - prev_dt).num_minutes() > SESSION_BREAK_MINUTES {
            continue;
        }
        let pc = bars[i - 1].close;
        trs.push(
            (bars[i].high - bars[i].low)
                .max((bars[i].high - pc).abs())
                .max((bars[i].low - pc).abs()),
        );
    }
    let start = trs.len().saturating_sub(ATR_WINDOW);
    if trs[start..].is_empty() {
        return None;
    }
    let sum: f64 = trs[start..].iter().sum();
    Some(sum / (trs.len() - start) as f64)
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ConfirmResult {
    /// 已确认换月，给出旧合约与新合约代码。
    Confirmed(String, String),
    /// 数据完整，但价格或主力切换证据不足，判定为普通断点。
    NotRollover,
    /// 月合约数据不足以做判断，需要后续重试。
    InsufficientData,
}

/// 用月合约确认断点：价格贴合 + 主力切换双证据。
pub fn confirm_candidate(
    candidate: &RolloverCandidate,
    month_bars: &std::collections::HashMap<String, Vec<Kline>>,
) -> Result<ConfirmResult> {
    let Some(before_ts) = parse_ts(&candidate.before.datetime) else {
        return Ok(ConfirmResult::InsufficientData);
    };
    let Some(after_ts) = parse_ts(&candidate.after.datetime) else {
        return Ok(ConfirmResult::InsufficientData);
    };

    if month_bars.len() < 2 {
        return Ok(ConfirmResult::InsufficientData);
    }
    let old_available = month_bars
        .values()
        .any(|rows| point_before(rows, before_ts).is_some());
    let new_available = month_bars
        .values()
        .any(|rows| point_after(rows, after_ts).is_some());
    if !old_available || !new_available {
        return Ok(ConfirmResult::NotRollover);
    }

    let mut before: Vec<(f64, String, BarPoint)> = Vec::new();
    let mut after: Vec<(f64, String, BarPoint)> = Vec::new();
    for (code, rows) in month_bars {
        if let Some(old) = point_before(rows, before_ts) {
            if hold_close(old.hold, candidate.before.hold)
                && price_ratio(old.price, candidate.before.close) <= PRICE_TOLERANCE
            {
                before.push((
                    price_ratio(old.price, candidate.before.close),
                    code.clone(),
                    old,
                ));
            }
        }
        if let Some(new) = point_after(rows, after_ts) {
            if hold_close(new.hold, candidate.after.hold)
                && price_ratio(new.price, candidate.after.open) <= PRICE_TOLERANCE
            {
                after.push((
                    price_ratio(new.price, candidate.after.open),
                    code.clone(),
                    new,
                ));
            }
        }
    }

    if before.is_empty() || after.is_empty() {
        return Ok(ConfirmResult::NotRollover);
    }

    before.sort_by(|a, b| {
        a.0.total_cmp(&b.0)
            .then(b.2.hold.total_cmp(&a.2.hold))
            .then(a.1.cmp(&b.1))
    });
    after.sort_by(|a, b| {
        a.0.total_cmp(&b.0)
            .then(b.2.hold.total_cmp(&a.2.hold))
            .then(a.1.cmp(&b.1))
    });

    let (_, old_code, _) = &before[0];
    let (new_ratio, new_code, _) = &after[0];
    if new_code == old_code || contract_month(new_code) <= contract_month(old_code) {
        return Ok(ConfirmResult::NotRollover);
    }

    // 普通周末断点里连续序列仍贴着旧合约；只有断点后明显切到更晚月份才确认。
    if let Some(old_rows) = month_bars.get(old_code) {
        if let Some(old_after) = point_after(old_rows, after_ts) {
            let old_after_ratio = price_ratio(old_after.price, candidate.after.open);
            if old_after_ratio < *new_ratio {
                return Ok(ConfirmResult::NotRollover);
            }
        }
    }

    Ok(ConfirmResult::Confirmed(old_code.clone(), new_code.clone()))
}

#[derive(Debug, Clone)]
struct BarPoint {
    price: f64,
    hold: f64,
}

/// 月份合约持仓必须和连续序列同一量级：连续序列的持仓就是当时主力合约的持仓。
fn hold_close(a: f64, b: f64) -> bool {
    if a <= 0.0 || b <= 0.0 {
        return true;
    }
    let ratio = a / b;
    ratio >= 0.25 && ratio <= 4.0
}

/// 指定时间之前（含）最近一根的价格与量能。
fn point_before(rows: &[Kline], ts: NaiveDateTime) -> Option<BarPoint> {
    rows.iter()
        .filter_map(|k| parse_ts(&k.datetime).map(|dt| (dt, k)))
        .take_while(|(dt, _)| *dt <= ts)
        .last()
        .filter(|(dt, _)| (ts - *dt).num_hours() <= MAX_BREAK_SPAN_HOURS)
        .map(|(_, k)| BarPoint {
            price: k.close,
            hold: k.hold,
        })
}

/// 指定时间之后（含）第一根的价格与量能。
fn point_after(rows: &[Kline], ts: NaiveDateTime) -> Option<BarPoint> {
    rows.iter()
        .filter_map(|k| parse_ts(&k.datetime).map(|dt| (dt, k)))
        .find(|(dt, _)| *dt >= ts)
        .filter(|(dt, _)| (*dt - ts).num_hours() <= MAX_BREAK_SPAN_HOURS)
        .map(|(_, k)| BarPoint {
            price: k.open,
            hold: k.hold,
        })
}

fn price_ratio(a: f64, b: f64) -> f64 {
    if b.abs() <= f64::EPSILON {
        f64::MAX
    } else {
        (a - b).abs() / b.abs()
    }
}

/// 合约代码末尾的 YYMM 月序（如 BU2609 → 202609）；解析失败返回 0。
fn contract_month(code: &str) -> i64 {
    let digits = code.trim_start_matches(|c: char| !c.is_ascii_digit());
    if digits.len() != 4 {
        return 0;
    }
    let yy: i64 = digits[..2].parse().unwrap_or(0);
    let mm: i64 = digits[2..].parse().unwrap_or(0);
    yy * 100 + mm
}

fn parse_ts(s: &str) -> Option<NaiveDateTime> {
    NaiveDateTime::parse_from_str(s, TS_FORMAT).ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn bar(dt: &str, open: f64, close: f64, hold: f64) -> Kline {
        Kline {
            datetime: dt.to_string(),
            open,
            high: open.max(close),
            low: open.min(close),
            close,
            volume: 100.0,
            hold,
        }
    }

    fn session(day: &str, open: f64, close: f64, hold: f64) -> Vec<Kline> {
        let mut out = Vec::new();
        for i in 0..5 {
            let h = format!("{day} 14:{:02}:00", 40 + i * 5);
            let c = close - 1.0;
            out.push(bar(&h, open + i as f64, c, hold));
        }
        out
    }

    #[test]
    fn detects_bu_style_rollover_gap() {
        // 前会话 5 根窄幅 bar（ATR≈1），尾收 100；夜盘首开 80：跳空 20ATR + 持仓 10% 迁移
        let mut bars = session("2026-08-05", 100.0, 100.0, 10000.0);
        bars.push(bar("2026-08-05 15:00:00", 100.0, 100.0, 11000.0));
        bars.push(bar("2026-08-05 21:05:00", 80.0, 81.0, 12100.0));
        let cands = detect_candidates("BU0", &bars);
        assert_eq!(cands.len(), 1);
        assert_eq!(cands[0].ts, "2026-08-05 21:05:00");
        assert!(cands[0].gap_r >= 6.0);
    }

    #[test]
    fn treats_small_session_break_as_candidate() {
        // 15:00 收 100，夜盘 21:05 开 101：跳空再小也会成为候选，
        // 是否换月交给月合约确认阶段判断。
        let mut bars = session("2026-08-05", 100.0, 100.0, 10000.0);
        bars.push(bar("2026-08-05 15:00:00", 100.0, 100.0, 10000.0));
        bars.push(bar("2026-08-05 21:05:00", 101.0, 101.0, 10000.0));
        let cands = detect_candidates("BU0", &bars);
        assert_eq!(cands.len(), 1);
    }

    #[test]
    fn large_gap_without_hold_is_candidate_but_not_confirmed() {
        // 跳空很大但持仓没迁移：候选阶段仍保留，月合约价格对不上时判定为消息跳空。
        let mut bars = session("2026-08-05", 100.0, 100.0, 10000.0);
        bars.push(bar("2026-08-05 15:00:00", 100.0, 100.0, 10000.0));
        bars.push(bar("2026-08-05 21:05:00", 80.0, 81.0, 10010.0));
        let cands = detect_candidates("BU0", &bars);
        assert_eq!(cands.len(), 1);

        let mut month_bars = std::collections::HashMap::new();
        month_bars.insert(
            "BU2609".to_string(),
            session("2026-08-05", 100.0, 100.0, 20000.0),
        );
        // 新合约开盘 99，和连续序列的 80 对不上，说明是消息跳空而不是换月
        month_bars.insert(
            "BU2610".to_string(),
            vec![bar("2026-08-05 21:05:00", 99.0, 99.0, 22000.0)],
        );
        let result = confirm_candidate(&cands[0], &month_bars).unwrap();
        assert_eq!(result, ConfirmResult::NotRollover);
    }

    #[test]
    fn detects_rollover_with_moderate_hold_shift() {
        // JD0 8/5 的实测形态：跳空约 19ATR，持仓迁移约 5.91%
        let mut bars = session("2026-08-04", 100.0, 100.0, 100000.0);
        bars.push(bar("2026-08-04 15:00:00", 100.0, 100.0, 100000.0));
        bars.push(bar("2026-08-05 09:05:00", 80.0, 81.0, 105910.0));
        let cands = detect_candidates("JD0", &bars);
        assert_eq!(cands.len(), 1);
        assert_eq!(cands[0].ts, "2026-08-05 09:05:00");
    }

    #[test]
    fn moderate_hold_shift_news_gap_is_not_confirmed() {
        // 持仓迁移到 5.91% 只让它成为候选；月合约价格对不上时不能确认为换月
        let mut bars = session("2026-08-04", 100.0, 100.0, 100000.0);
        bars.push(bar("2026-08-04 15:00:00", 100.0, 100.0, 100000.0));
        bars.push(bar("2026-08-05 09:05:00", 80.0, 81.0, 105910.0));
        let cands = detect_candidates("JD0", &bars);
        assert_eq!(cands.len(), 1);

        let mut month_bars = std::collections::HashMap::new();
        month_bars.insert(
            "JD2609".to_string(),
            session("2026-08-04", 100.0, 100.0, 200000.0),
        );
        // 新合约开盘 99，和连续序列的 80 对不上，说明这是消息跳空而不是换月
        month_bars.insert(
            "JD2610".to_string(),
            vec![bar("2026-08-05 09:05:00", 99.0, 99.0, 205910.0)],
        );
        let result = confirm_candidate(&cands[0], &month_bars).unwrap();
        assert_eq!(result, ConfirmResult::NotRollover);
    }

    #[test]
    fn confirms_with_month_contract_pair() {
        let mut bars = session("2026-08-05", 100.0, 100.0, 10000.0);
        bars.push(bar("2026-08-05 15:00:00", 100.0, 100.0, 11000.0));
        bars.push(bar("2026-08-05 21:05:00", 80.0, 81.0, 12100.0));
        let cands = detect_candidates("BU0", &bars);
        assert_eq!(cands.len(), 1);

        let mut month_bars = std::collections::HashMap::new();
        // BU2609：前会话是主力，15:00 收 100；21:05 已不活跃
        let mut old = session("2026-08-05", 100.0, 100.0, 20000.0);
        old.push(bar("2026-08-05 15:00:00", 100.0, 100.0, 21000.0));
        month_bars.insert("BU2609".to_string(), old);
        // BU2610：夜盘成为主力，21:05 开 80
        let mut new = Vec::new();
        new.push(bar("2026-08-05 21:05:00", 80.0, 81.0, 22000.0));
        month_bars.insert("BU2610".to_string(), new);

        let result = confirm_candidate(&cands[0], &month_bars).unwrap();
        assert_eq!(
            result,
            ConfirmResult::Confirmed("BU2609".to_string(), "BU2610".to_string())
        );
    }

    #[test]
    fn c0_low_hold_shift_confirms_via_month_price() {
        let mut bars = session("2026-08-12", 100.0, 100.0, 100000.0);
        bars.push(bar("2026-08-12 15:00:00", 100.0, 100.0, 100000.0));
        bars.push(bar("2026-08-12 21:05:00", 80.0, 81.0, 104090.0));
        let cands = detect_candidates("C0", &bars);
        assert_eq!(cands.len(), 1);
        assert_eq!(cands[0].ts, "2026-08-12 21:05:00");

        let mut month_bars = std::collections::HashMap::new();
        let mut old = session("2026-08-12", 100.0, 100.0, 200000.0);
        old.push(bar("2026-08-12 15:00:00", 100.0, 100.0, 200000.0));
        month_bars.insert("C2609".to_string(), old);
        month_bars.insert(
            "C2611".to_string(),
            vec![bar("2026-08-12 21:05:00", 80.0, 81.0, 220000.0)],
        );

        let result = confirm_candidate(&cands[0], &month_bars).unwrap();
        assert_eq!(
            result,
            ConfirmResult::Confirmed("C2609".to_string(), "C2611".to_string())
        );
    }

    #[test]
    fn rejects_pair_that_does_not_explain_gap() {
        let mut bars = session("2026-08-05", 100.0, 100.0, 10000.0);
        bars.push(bar("2026-08-05 15:00:00", 100.0, 100.0, 11000.0));
        bars.push(bar("2026-08-05 21:05:00", 80.0, 81.0, 12100.0));
        let cands = detect_candidates("BU0", &bars);

        let mut month_bars = std::collections::HashMap::new();
        month_bars.insert(
            "BU2609".to_string(),
            session("2026-08-05", 100.0, 100.0, 20000.0),
        );
        // 唯一的新合约 21:05 开 99，与连续序列 80 对不上
        month_bars.insert(
            "BU2610".to_string(),
            vec![bar("2026-08-05 21:05:00", 99.0, 99.0, 22000.0)],
        );
        let result = confirm_candidate(&cands[0], &month_bars).unwrap();
        assert_eq!(result, ConfirmResult::NotRollover);
    }

    #[test]
    fn small_gap_rollover_confirms() {
        // 换月本身可能只产生很小跳空：候选 + 月合约价格贴合 + 量能切换仍能确认。
        let mut bars = session("2026-08-05", 100.0, 100.0, 10000.0);
        bars.push(bar("2026-08-05 15:00:00", 100.0, 100.0, 11000.0));
        bars.push(bar("2026-08-05 21:05:00", 101.0, 101.0, 12000.0));
        let cands = detect_candidates("BU0", &bars);
        assert_eq!(cands.len(), 1);

        let mut month_bars = std::collections::HashMap::new();
        let mut old = session("2026-08-05", 100.0, 100.0, 20000.0);
        old.push(bar("2026-08-05 15:00:00", 100.0, 100.0, 21000.0));
        month_bars.insert("BU2609".to_string(), old);
        month_bars.insert(
            "BU2610".to_string(),
            vec![bar("2026-08-05 21:05:00", 101.0, 101.0, 22000.0)],
        );

        let result = confirm_candidate(&cands[0], &month_bars).unwrap();
        assert_eq!(
            result,
            ConfirmResult::Confirmed("BU2609".to_string(), "BU2610".to_string())
        );
    }

    #[test]
    fn insufficient_month_data_returns_insufficient() {
        let mut bars = session("2026-08-05", 100.0, 100.0, 10000.0);
        bars.push(bar("2026-08-05 15:00:00", 100.0, 100.0, 11000.0));
        bars.push(bar("2026-08-05 21:05:00", 80.0, 81.0, 12100.0));
        let cands = detect_candidates("BU0", &bars);

        let mut month_bars = std::collections::HashMap::new();
        month_bars.insert(
            "BU2609".to_string(),
            session("2026-08-05", 100.0, 100.0, 20000.0),
        );
        let result = confirm_candidate(&cands[0], &month_bars).unwrap();
        assert_eq!(result, ConfirmResult::InsufficientData);
    }

    #[test]
    fn stale_old_contract_does_not_confirm() {
        // 候选是 8/14，但旧合约最后一次数据停在 7/24：它早已不是断点附近的合约，
        // 即使价格巧合也不能拿去确认换月。
        let mut bars = session("2026-08-14", 100.0, 100.0, 10000.0);
        bars.push(bar("2026-08-14 15:00:00", 100.0, 100.0, 11000.0));
        bars.push(bar("2026-08-14 21:05:00", 80.0, 81.0, 12100.0));
        let cands = detect_candidates("BU0", &bars);

        let mut month_bars = std::collections::HashMap::new();
        month_bars.insert(
            "BU2607".to_string(),
            session("2026-07-24", 100.0, 100.0, 20000.0),
        );
        month_bars.insert(
            "BU2610".to_string(),
            vec![bar("2026-08-14 21:05:00", 80.0, 81.0, 22000.0)],
        );

        let result = confirm_candidate(&cands[0], &month_bars).unwrap();
        assert_eq!(result, ConfirmResult::NotRollover);
    }

    #[test]
    fn short_intraday_break_is_not_candidate() {
        // 11:30 到 13:35 是午间休息，不属于收盘到开盘的换月窗口。
        let bars = vec![
            bar("2026-08-05 11:30:00", 100.0, 100.0, 10000.0),
            bar("2026-08-05 13:35:00", 100.5, 100.5, 10000.0),
        ];
        let cands = detect_candidates("AU0", &bars);
        assert!(cands.is_empty());
    }

    #[test]
    fn source_switch_confirms_even_if_old_side_peak_contract_price_does_not_match() {
        let mut bars = session("2026-08-05", 100.0, 100.0, 10000.0);
        bars.push(bar("2026-08-05 15:00:00", 100.0, 100.0, 11000.0));
        bars.push(bar("2026-08-05 21:05:00", 100.0, 100.0, 12000.0));
        let cands = detect_candidates("BU0", &bars);
        assert_eq!(cands.len(), 1);

        let mut month_bars = std::collections::HashMap::new();
        // 断点前真正的持仓峰值 BU2610 价格和连续序列对不上，
        // 但连续序列断点前贴 BU2609、断点后贴 BU2611，按数据源切换确认。
        let mut old_2609 = session("2026-08-05", 100.0, 100.0, 20000.0);
        old_2609.push(bar("2026-08-05 15:00:00", 100.0, 100.0, 22000.0));
        month_bars.insert("BU2609".to_string(), old_2609);
        month_bars.insert(
            "BU2610".to_string(),
            vec![
                bar("2026-08-05 15:00:00", 105.0, 105.0, 25000.0),
                bar("2026-08-05 21:05:00", 105.0, 105.0, 25000.0),
            ],
        );
        month_bars.insert(
            "BU2611".to_string(),
            vec![bar("2026-08-05 21:05:00", 100.0, 100.0, 30000.0)],
        );

        let result = confirm_candidate(&cands[0], &month_bars).unwrap();
        assert_eq!(
            result,
            ConfirmResult::Confirmed("BU2609".to_string(), "BU2611".to_string())
        );
    }

    #[test]
    fn ur0_early_switch_confirms_without_new_hold_peak() {
        // UR0 8/17 实测形态：UR2609 仍是持仓峰值，但连续合约已切到 UR2611。
        let mut bars = session("2026-08-14", 1687.0, 1687.0, 143833.0);
        bars.push(bar("2026-08-14 15:00:00", 1686.0, 1687.0, 143833.0));
        bars.push(bar("2026-08-17 09:05:00", 1733.0, 1730.0, 167680.0));
        let cands = detect_candidates("UR0", &bars);
        assert_eq!(cands.len(), 1);

        let mut month_bars = std::collections::HashMap::new();
        let mut old_2609 = session("2026-08-14", 1687.0, 1687.0, 143833.0);
        old_2609.push(bar("2026-08-14 15:00:00", 1686.0, 1687.0, 143833.0));
        old_2609.push(bar("2026-08-17 09:05:00", 1694.0, 1694.0, 125507.0));
        month_bars.insert("UR2609".to_string(), old_2609);
        month_bars.insert(
            "UR2611".to_string(),
            vec![bar("2026-08-17 09:05:00", 1736.0, 1730.0, 93323.0)],
        );

        let result = confirm_candidate(&cands[0], &month_bars).unwrap();
        assert_eq!(
            result,
            ConfirmResult::Confirmed("UR2609".to_string(), "UR2611".to_string())
        );
    }

    #[test]
    fn normal_session_break_stays_on_same_contract() {
        // 普通周末断点：连续序列前后仍贴旧合约，即使更晚月份价格接近也不能确认。
        let mut bars = session("2026-08-14", 100.0, 100.0, 10000.0);
        bars.push(bar("2026-08-14 15:00:00", 100.0, 100.0, 10000.0));
        bars.push(bar("2026-08-17 09:05:00", 100.5, 100.5, 10500.0));
        let cands = detect_candidates("BU0", &bars);
        assert_eq!(cands.len(), 1);

        let mut month_bars = std::collections::HashMap::new();
        let mut old = session("2026-08-14", 100.0, 100.0, 20000.0);
        old.push(bar("2026-08-14 15:00:00", 100.0, 100.0, 20000.0));
        old.push(bar("2026-08-17 09:05:00", 100.5, 100.5, 20000.0));
        month_bars.insert("BU2609".to_string(), old);
        month_bars.insert(
            "BU2610".to_string(),
            vec![bar("2026-08-17 09:05:00", 100.7, 100.7, 21000.0)],
        );

        let result = confirm_candidate(&cands[0], &month_bars).unwrap();
        assert_eq!(result, ConfirmResult::NotRollover);
    }

    #[test]
    fn month_hold_mismatch_does_not_confirm() {
        // 连续序列持仓 10 万，但月合约只有 5 千：拿冷门月份凑数，不能确认换月。
        let mut bars = session("2026-08-05", 100.0, 100.0, 100000.0);
        bars.push(bar("2026-08-05 15:00:00", 100.0, 100.0, 100000.0));
        bars.push(bar("2026-08-05 21:05:00", 101.0, 101.0, 110000.0));
        let cands = detect_candidates("AU0", &bars);
        assert_eq!(cands.len(), 1);

        let mut month_bars = std::collections::HashMap::new();
        let mut old = session("2026-08-05", 100.0, 100.0, 5000.0);
        old.push(bar("2026-08-05 15:00:00", 100.0, 100.0, 5000.0));
        month_bars.insert("AU2608".to_string(), old);
        month_bars.insert(
            "AU2610".to_string(),
            vec![bar("2026-08-05 21:05:00", 101.0, 101.0, 6000.0)],
        );

        let result = confirm_candidate(&cands[0], &month_bars).unwrap();
        assert_eq!(result, ConfirmResult::NotRollover);
    }
}
