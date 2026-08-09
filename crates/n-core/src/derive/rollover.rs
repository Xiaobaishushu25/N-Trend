//! 连续合约换月识别（轻量版）。
//!
//! 不做“拉全品种所有月合约按持仓量对齐选主力”的重方案，而是：
//! 1. 在本地连续 5m 序列上找可疑断点：相邻 bar 间隔超过 5 分钟（会话断裂）、
//!    跳空幅度达到 ATR 的若干倍、且持仓量变化显著；
//! 2. 只对可疑点附近的少量月合约做确认：断点前旧合约、断点后新合约，
//!    且“旧合约前收 → 新合约后开”能解释连续序列的跳空；
//! 3. 无法确认的候选落库为 unconfirmed，后续刷新时重试。

use anyhow::Result;
use chrono::NaiveDateTime;

use crate::fetch::kline::Kline;

/// 相邻 5m bar 间隔超过该值视为会话断裂（分钟级别聚合也使用同一口径）。
pub const SESSION_BREAK_MINUTES: i64 = 5;
/// 断点跳空 / ATR 的最小倍数：太小会把普通消息跳空误判为换月。
pub const GAP_ATR_MIN: f64 = 6.0;
/// 断点前后持仓量相对变化的最小比例：换月时主力持仓通常发生明显迁移。
pub const HOLD_CHANGE_MIN: f64 = 0.08;
/// ATR 计算窗口（与策略分析一致）。
pub const ATR_WINDOW: usize = 20;
/// 确认时容忍的价格偏差比例。
pub const PRICE_TOLERANCE: f64 = 0.05;

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

/// 在升序 5m 连续序列中找出可疑换月断点。
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
        if (dt - prev_dt).num_minutes() <= SESSION_BREAK_MINUTES {
            continue;
        }
        let Some(atr) = local_atr(&bars[..i]) else {
            continue;
        };
        let gap = (after.open - before.close).abs();
        let gap_r = gap / atr.max(1e-9);
        if gap_r < GAP_ATR_MIN {
            continue;
        }
        let hold_change = if before.hold > 0.0 {
            (after.hold - before.hold).abs() / before.hold
        } else {
            0.0
        };
        if hold_change < HOLD_CHANGE_MIN {
            continue;
        }
        out.push(RolloverCandidate {
            symbol: symbol.to_string(),
            ts: after.datetime.clone(),
            before: before.clone(),
            after: after.clone(),
            gap_r,
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

/// 用少量月合约确认断点：返回 (from_contract, to_contract)。
/// `month_bars` 为按代码升序传入的月合约 5m K 线切片（键为合约代码）。
pub fn confirm_candidate(
    candidate: &RolloverCandidate,
    month_bars: &std::collections::HashMap<String, Vec<Kline>>,
) -> Result<Option<(String, String)>> {
    let Some(before_ts) = parse_ts(&candidate.before.datetime) else {
        return Ok(None);
    };
    let Some(after_ts) = parse_ts(&candidate.after.datetime) else {
        return Ok(None);
    };
    let mut scored: Vec<(f64, String, String)> = Vec::new();

    for (old_code, old_rows) in month_bars {
        let Some(old_close) = close_before(old_rows, before_ts) else {
            continue;
        };
        let old_close_ratio = price_ratio(old_close, candidate.before.close);
        for (new_code, new_rows) in month_bars {
            if new_code == old_code {
                continue;
            }
            if contract_month(new_code) <= contract_month(old_code) {
                continue;
            }
            let Some(new_open) = open_after(new_rows, after_ts) else {
                continue;
            };
            let new_open_ratio = price_ratio(new_open, candidate.after.open);
            let old_ok = old_close_ratio <= PRICE_TOLERANCE;
            let new_ok = new_open_ratio <= PRICE_TOLERANCE;
            if !old_ok || !new_ok {
                continue;
            }
            // 新合约价格应在旧合约价格附近（同一品种相邻月价差远小于换月跳空）。
            let cross_ratio = price_ratio(new_open, old_close);
            let score = old_close_ratio + new_open_ratio + cross_ratio;
            scored.push((score, old_code.clone(), new_code.clone()));
        }
    }

    scored.sort_by(|a, b| a.0.total_cmp(&b.0));
    Ok(scored.into_iter().next().map(|(_, from, to)| (from, to)))
}

/// 指定时间之前（含）最近一根的收盘价。
fn close_before(rows: &[Kline], ts: NaiveDateTime) -> Option<f64> {
    rows.iter()
        .filter_map(|k| parse_ts(&k.datetime).map(|dt| (dt, k.close)))
        .take_while(|(dt, _)| *dt <= ts)
        .last()
        .map(|(_, close)| close)
}

/// 指定时间之后（含）第一根的开盘价。
fn open_after(rows: &[Kline], ts: NaiveDateTime) -> Option<f64> {
    rows.iter()
        .filter_map(|k| parse_ts(&k.datetime).map(|dt| (dt, k.open)))
        .find(|(dt, _)| *dt >= ts)
        .map(|(_, open)| open)
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
            let h = format!("{day} {:02}:{:02}:00", 9, 5 + i * 5);
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
    fn ignores_normal_session_break() {
        // 15:00 收 100，夜盘 21:05 开 101：有会话断裂但跳空太小
        let mut bars = session("2026-08-05", 100.0, 100.0, 10000.0);
        bars.push(bar("2026-08-05 15:00:00", 100.0, 100.0, 10000.0));
        bars.push(bar("2026-08-05 21:05:00", 101.0, 101.0, 10000.0));
        assert!(detect_candidates("BU0", &bars).is_empty());
    }

    #[test]
    fn requires_hold_shift() {
        // 跳空很大但持仓没迁移：疑似消息跳空而非换月
        let mut bars = session("2026-08-05", 100.0, 100.0, 10000.0);
        bars.push(bar("2026-08-05 15:00:00", 100.0, 100.0, 10000.0));
        bars.push(bar("2026-08-05 21:05:00", 80.0, 81.0, 10010.0));
        assert!(detect_candidates("BU0", &bars).is_empty());
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

        let pair = confirm_candidate(&cands[0], &month_bars).unwrap();
        assert_eq!(pair, Some(("BU2609".to_string(), "BU2610".to_string())));
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
        let pair = confirm_candidate(&cands[0], &month_bars).unwrap();
        assert!(pair.is_none());
    }
}
