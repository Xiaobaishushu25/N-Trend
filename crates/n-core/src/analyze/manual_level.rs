//! 关键区域的支撑、压力和突破判定。
//!
//! 该模块独立于 N 字形、垂线和自动箱体，只使用用户给出的单一价格区域
//! 与已收盘 K 线完成判定。

use serde::{Deserialize, Serialize};

use crate::analyze::model::Bar;

pub const ROLE_WINDOW: usize = 30;
pub const MIN_ROLE_BARS: usize = 10;
/// 最近窗口中收盘价位于区域一侧的最低比例；放宽后更容易识别持续在上方/下方的区域。
pub const ROLE_RATIO: f64 = 0.60;
pub const MIN_TESTS: usize = 2;
pub const BREAKOUT_ATR: f64 = 0.15;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ManualLevelDefinition {
    pub zone_low: f64,
    pub zone_high: f64,
    pub start_ts: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum ManualLevelRole {
    Unknown,
    Support,
    Resistance,
}

impl ManualLevelRole {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Unknown => "unknown",
            Self::Support => "support",
            Self::Resistance => "resistance",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum ManualLevelEventKind {
    Approach,
    Test,
    Rejection,
    BreakoutUp,
    BreakoutDown,
    RetestSupport,
    RetestResistance,
    Reentry,
}

impl ManualLevelEventKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Approach => "approach",
            Self::Test => "testing",
            Self::Rejection => "rejection",
            Self::BreakoutUp => "breakout_up",
            Self::BreakoutDown => "breakout_down",
            Self::RetestSupport => "retest_support",
            Self::RetestResistance => "retest_resistance",
            Self::Reentry => "reentry",
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ManualLevelEvaluation {
    pub phase: String,
    pub event: Option<ManualLevelEventKind>,
    pub role: ManualLevelRole,
    pub role_confidence: f64,
    pub price: Option<f64>,
    pub reason: String,
    pub test_count: usize,
    pub volume_ratio: Option<f64>,
}

fn in_zone(price: f64, low: f64, high: f64) -> bool {
    price >= low.min(high) && price <= low.max(high)
}

fn overlaps_zone(low: f64, high: f64, zone_low: f64, zone_high: f64) -> bool {
    high >= zone_low.min(zone_high) && low <= zone_low.max(zone_high)
}

fn atr20(bars: &[Bar]) -> f64 {
    let start = bars.len().saturating_sub(20);
    let values: Vec<f64> = bars[start..]
        .iter()
        .map(|bar| bar.high - bar.low)
        .filter(|range| *range > 0.0)
        .collect();
    if values.is_empty() {
        0.0
    } else {
        values.iter().sum::<f64>() / values.len() as f64
    }
}

fn volume_ratio(bars: &[Bar], index: usize) -> Option<f64> {
    let current = bars.get(index)?.volume;
    if current <= 0.0 || index == 0 {
        return None;
    }
    let values: Vec<f64> = bars[index.saturating_sub(20)..index]
        .iter()
        .map(|bar| bar.volume)
        .filter(|volume| *volume > 0.0)
        .collect();
    if values.is_empty() {
        return None;
    }
    let average = values.iter().sum::<f64>() / values.len() as f64;
    (average > 0.0).then_some(current / average)
}

fn rejection_candle(bar: &Bar, resistance: bool) -> bool {
    let range = bar.high - bar.low;
    if range <= 0.0 {
        return false;
    }
    let body = (bar.close - bar.open).abs();
    let upper_wick = bar.high - bar.open.max(bar.close);
    let lower_wick = bar.open.min(bar.close) - bar.low;
    if resistance {
        bar.close < bar.open && (body / range >= 0.35 || upper_wick / range >= 0.4)
    } else {
        bar.close > bar.open && (body / range >= 0.35 || lower_wick / range >= 0.4)
    }
}

fn role_from_window(
    window: &[Bar],
    low: f64,
    high: f64,
) -> (ManualLevelRole, f64, usize, usize) {
    if window.len() < MIN_ROLE_BARS {
        return (ManualLevelRole::Unknown, 0.0, 0, 0);
    }
    let below = window.iter().filter(|bar| bar.close < low).count();
    let above = window.iter().filter(|bar| bar.close > high).count();
    let pressure_tests = window
        .iter()
        .filter(|bar| bar.high >= low && bar.close < low)
        .count();
    let support_tests = window
        .iter()
        .filter(|bar| bar.low <= high && bar.close > high)
        .count();
    let below_ratio = below as f64 / window.len() as f64;
    let above_ratio = above as f64 / window.len() as f64;
    if below_ratio >= ROLE_RATIO {
        (
            ManualLevelRole::Resistance,
            below_ratio,
            pressure_tests,
            support_tests,
        )
    } else if above_ratio >= ROLE_RATIO {
        (
            ManualLevelRole::Support,
            above_ratio,
            pressure_tests,
            support_tests,
        )
    } else {
        (
            ManualLevelRole::Unknown,
            below_ratio.max(above_ratio),
            pressure_tests,
            support_tests,
        )
    }
}

/// 使用最近 30 根已收盘 K 线判定角色和事件；latest_price 仅用于盘中接近提醒。
pub fn evaluate(
    definition: &ManualLevelDefinition,
    bars: &[Bar],
    latest_price: Option<f64>,
    previous_phase: &str,
    previous_role: &str,
    role_override: &str,
) -> ManualLevelEvaluation {
    let forced_role = match role_override {
        "support" => ManualLevelRole::Support,
        "resistance" => ManualLevelRole::Resistance,
        _ => ManualLevelRole::Unknown,
    };
    let scoped: Vec<Bar> = bars
        .iter()
        .filter(|bar| bar.dt.to_bar_ts() >= definition.start_ts)
        .cloned()
        .collect();
    let Some(last) = scoped.last() else {
        return ManualLevelEvaluation {
            phase: "pending".to_string(),
            event: None,
            role: forced_role,
            role_confidence: if forced_role == ManualLevelRole::Unknown { 0.0 } else { 1.0 },
            price: latest_price,
            reason: if forced_role == ManualLevelRole::Unknown {
                "暂无足够K线".to_string()
            } else {
                "已按用户指定角色标记，等待关键区域事件".to_string()
            },
            test_count: 0,
            volume_ratio: None,
        };
    };

    let low = definition.zone_low.min(definition.zone_high);
    let high = definition.zone_low.max(definition.zone_high);
    let start = scoped.len().saturating_sub(ROLE_WINDOW);
    let window = &scoped[start..];
    let (detected_role, confidence, resistance_tests, support_tests) =
        role_from_window(window, low, high);
    let mut role = if forced_role == ManualLevelRole::Unknown {
        detected_role
    } else {
        forced_role
    };
    let atr = atr20(&scoped);
    let buffer = atr * BREAKOUT_ATR;
    let volume = volume_ratio(&scoped, scoped.len() - 1);
    let upper_break = last.close > high + buffer && last.close > last.open;
    let lower_break = last.close < low - buffer && last.close < last.open;

    if upper_break && role != ManualLevelRole::Support {
        // 未确认角色也允许记录突破；将突破前的未知角色暂时映射为
        // 压力，后续回踩并重新站稳时即可完成压力→支撑转换。
        let breakout_role = if role == ManualLevelRole::Unknown {
            ManualLevelRole::Resistance
        } else {
            role
        };
        return ManualLevelEvaluation {
            phase: "breakout_confirmed".to_string(),
            event: (previous_phase != "breakout_confirmed")
                .then_some(ManualLevelEventKind::BreakoutUp),
            role: breakout_role,
            role_confidence: confidence,
            price: Some(last.close),
            reason: "收盘站上价位区域外侧，实体确认向上突破".to_string(),
            test_count: resistance_tests.max(support_tests),
            volume_ratio: volume,
        };
    }
    if lower_break && role != ManualLevelRole::Resistance {
        let breakout_role = if role == ManualLevelRole::Unknown {
            ManualLevelRole::Support
        } else {
            role
        };
        return ManualLevelEvaluation {
            phase: "breakout_confirmed".to_string(),
            event: (previous_phase != "breakout_confirmed")
                .then_some(ManualLevelEventKind::BreakoutDown),
            role: breakout_role,
            role_confidence: confidence,
            price: Some(last.close),
            reason: "收盘跌破价位区域外侧，实体确认向下突破".to_string(),
            test_count: resistance_tests.max(support_tests),
            volume_ratio: volume,
        };
    }

    if previous_phase == "breakout_confirmed" {
        if previous_role == "resistance" && last.low <= high && last.close > high {
            role = ManualLevelRole::Support;
            return ManualLevelEvaluation {
                phase: "retest_confirmed".to_string(),
                event: Some(ManualLevelEventKind::RetestSupport),
                role,
                role_confidence: 1.0,
                price: Some(last.close),
                reason: "向上突破后回踩区域并重新站稳，压力转换为支撑".to_string(),
                test_count: support_tests,
                volume_ratio: volume,
            };
        }
        if previous_role == "support" && last.high >= low && last.close < low {
            role = ManualLevelRole::Resistance;
            return ManualLevelEvaluation {
                phase: "retest_confirmed".to_string(),
                event: Some(ManualLevelEventKind::RetestResistance),
                role,
                role_confidence: 1.0,
                price: Some(last.close),
                reason: "向下突破后回踩区域并重新受阻，支撑转换为压力".to_string(),
                test_count: resistance_tests,
                volume_ratio: volume,
            };
        }
    }

    if role == ManualLevelRole::Resistance
        && resistance_tests >= MIN_TESTS
        && overlaps_zone(last.low, last.high, low, high)
        && last.close < low
        && rejection_candle(last, true)
    {
        return ManualLevelEvaluation {
            phase: "rejection_confirmed".to_string(),
            event: (previous_phase != "rejection_confirmed")
                .then_some(ManualLevelEventKind::Rejection),
            role,
            role_confidence: confidence,
            price: Some(last.close),
            reason: "压力区域至少两次测试后收盘回落，并出现阴线或长上影".to_string(),
            test_count: resistance_tests,
            volume_ratio: volume,
        };
    }
    if role == ManualLevelRole::Support
        && support_tests >= MIN_TESTS
        && overlaps_zone(last.low, last.high, low, high)
        && last.close > high
        && rejection_candle(last, false)
    {
        return ManualLevelEvaluation {
            phase: "rejection_confirmed".to_string(),
            event: (previous_phase != "rejection_confirmed")
                .then_some(ManualLevelEventKind::Rejection),
            role,
            role_confidence: confidence,
            price: Some(last.close),
            reason: "支撑区域至少两次测试后收盘回升，并出现阳线或长下影".to_string(),
            test_count: support_tests,
            volume_ratio: volume,
        };
    }

    let last_touches = overlaps_zone(last.low, last.high, low, high);
    if last_touches {
        let tests = resistance_tests.max(support_tests);
        return ManualLevelEvaluation {
            phase: "testing".to_string(),
            event: (previous_phase != "testing").then_some(ManualLevelEventKind::Test),
            role,
            role_confidence: confidence,
            price: Some(last.close),
            reason: "收盘K线触碰关键区域，等待方向确认".to_string(),
            test_count: tests,
            volume_ratio: volume,
        };
    }

    let live = latest_price.unwrap_or(last.close);
    let distance = (atr * 0.25).max((high - low).abs());
    if live >= low - distance && live <= high + distance {
        return ManualLevelEvaluation {
            phase: "approaching".to_string(),
            event: (previous_phase != "approaching").then_some(ManualLevelEventKind::Approach),
            role,
            role_confidence: confidence,
            price: Some(live),
            reason: "最新价接近关键区域，等待收盘确认".to_string(),
            test_count: resistance_tests.max(support_tests),
            volume_ratio: None,
        };
    }

    let reentry = previous_phase == "breakout_confirmed" && in_zone(last.close, low, high);
    ManualLevelEvaluation {
        phase: if role == ManualLevelRole::Unknown {
            "pending".to_string()
        } else {
            "pending".to_string()
        },
        event: reentry.then_some(ManualLevelEventKind::Reentry),
        role,
        role_confidence: confidence,
        price: Some(last.close),
        reason: if reentry {
            "突破后价格重新回到价位区域".to_string()
        } else {
            "当前没有新的价位事件".to_string()
        },
        test_count: resistance_tests.max(support_tests),
        volume_ratio: None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::analyze::model::DT;

    fn bar(i: usize, open: f64, high: f64, low: f64, close: f64) -> Bar {
        Bar {
            dt: DT {
                year: 2026,
                month: 1,
                day: 1,
                hour: 9,
                minute: i as i32,
            },
            open,
            high,
            low,
            close,
            volume: 100.0,
            hold: 0.0,
            rollover: false,
        }
    }

    fn definition() -> ManualLevelDefinition {
        ManualLevelDefinition {
            zone_low: 99.0,
            zone_high: 100.0,
            start_ts: "2026-01-01 09:00:00".into(),
        }
    }

    #[test]
    fn detects_resistance_from_recent_closes_and_rejection() {
        let mut bars = (0..27)
            .map(|i| bar(i, 95.0, 97.0, 93.0, 95.0))
            .collect::<Vec<_>>();
        bars.push(bar(27, 98.0, 100.0, 96.0, 97.0));
        bars.push(bar(28, 98.0, 100.0, 96.0, 97.0));
        bars.push(bar(29, 99.0, 101.0, 96.0, 97.0));
        let result = evaluate(&definition(), &bars, None, "pending", "unknown", "auto");
        assert_eq!(result.role, ManualLevelRole::Resistance);
        assert_eq!(result.event, Some(ManualLevelEventKind::Rejection));
    }

    #[test]
    fn detects_support_from_recent_closes() {
        let mut bars = (0..28)
            .map(|i| bar(i, 105.0, 107.0, 103.0, 105.0))
            .collect::<Vec<_>>();
        bars.push(bar(28, 101.0, 104.0, 99.0, 103.0));
        bars.push(bar(29, 101.0, 104.0, 99.0, 103.0));
        let result = evaluate(&definition(), &bars, None, "pending", "unknown", "auto");
        assert_eq!(result.role, ManualLevelRole::Support);
        assert!(result.test_count >= 2);
    }

    #[test]
    fn does_not_reject_when_last_candle_is_far_from_zone() {
        let mut bars = (0..27)
            .map(|i| bar(i, 95.0, 97.0, 93.0, 95.0))
            .collect::<Vec<_>>();
        bars.push(bar(27, 98.0, 100.0, 96.0, 97.0));
        bars.push(bar(28, 98.0, 100.0, 96.0, 97.0));
        bars.push(bar(29, 96.0, 98.0, 93.0, 94.0));

        let result = evaluate(&definition(), &bars, None, "pending", "unknown", "auto");
        assert_eq!(result.role, ManualLevelRole::Resistance);
        assert_ne!(result.event, Some(ManualLevelEventKind::Rejection));
    }

    #[test]
    fn bearish_long_lower_wick_is_not_support_rejection() {
        let mut bars = (0..27)
            .map(|i| bar(i, 105.0, 107.0, 103.0, 105.0))
            .collect::<Vec<_>>();
        bars.push(bar(27, 101.0, 104.0, 99.0, 103.0));
        bars.push(bar(28, 101.0, 104.0, 99.0, 103.0));
        bars.push(bar(29, 105.0, 105.0, 98.0, 101.0));

        let result = evaluate(&definition(), &bars, None, "pending", "unknown", "auto");
        assert_eq!(result.role, ManualLevelRole::Support);
        assert_eq!(result.phase, "testing");
        assert_eq!(result.event, Some(ManualLevelEventKind::Test));
    }

    #[test]
    fn wick_only_breakout_is_not_confirmed() {
        let bars = vec![
            bar(0, 95.0, 101.0, 94.0, 99.0),
            bar(1, 99.0, 102.0, 98.0, 99.5),
        ];
        let result = evaluate(&definition(), &bars, None, "pending", "resistance", "auto");
        assert_ne!(result.event, Some(ManualLevelEventKind::BreakoutUp));
    }

    #[test]
    fn confirms_up_breakout_and_support_retest() {
        let mut bars = (0..28)
            .map(|i| bar(i, 95.0, 97.0, 93.0, 95.0))
            .collect::<Vec<_>>();
        bars.push(bar(28, 101.0, 104.0, 100.0, 103.0));
        bars.push(bar(29, 102.0, 103.0, 99.5, 102.0));
        let first = evaluate(&definition(), &bars[..29], None, "pending", "resistance", "auto");
        assert_eq!(first.event, Some(ManualLevelEventKind::BreakoutUp));
        let second = evaluate(&definition(), &bars, None, "breakout_confirmed", "resistance", "auto");
        assert_eq!(second.event, Some(ManualLevelEventKind::RetestSupport));
        assert_eq!(second.role, ManualLevelRole::Support);
    }
}
