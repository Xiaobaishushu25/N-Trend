//! Futures trading session schedule and expected timestamp calculation.

use chrono::{Datelike, NaiveDateTime, Timelike, Weekday};

/// 夜盘交易时段类型。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum NightSessionType {
    /// 无夜盘
    None,
    /// 23:00 收盘（黑色金属、大多数化工、农产品等）
    Close2300,
    /// 23:30 收盘
    Close2330,
    /// 01:00 收盘（有色金属：铜、铝、锌、铅、镍、锡等）
    Close0100,
    /// 02:30 收盘（贵金属：黄金、白银，以及原油）
    Close0230,
}

/// 根据合约代码提取大写前缀（字母部分）。
pub fn contract_prefix(symbol: &str) -> String {
    symbol
        .chars()
        .take_while(|c| c.is_alphabetic())
        .collect::<String>()
        .to_uppercase()
}

/// 根据合约代码判断夜盘类型。
pub fn classify_night_session(symbol: &str) -> NightSessionType {
    let prefix = contract_prefix(symbol);
    match prefix.as_str() {
        // 02:30 贵金属与原油
        "AU" | "AG" | "SC" => NightSessionType::Close0230,

        // 01:00 有色金属
        "CU" | "AL" | "ZN" | "PB" | "NI" | "SN" | "BC" => NightSessionType::Close0100,

        // 23:30 纯碱、玻璃
        "SA" | "FG" => NightSessionType::Close2330,

        // 无夜盘品种
        "AP" | "CJ" | "JD" | "LH" | "PK" | "SI" | "LC" | "UR" | "WH" | "PM" | "RI" | "JR"
        | "LR" | "BB" | "FB" | "IF" | "IH" | "IC" | "IM" | "TF" | "T" | "TS" | "TL" => {
            NightSessionType::None
        }

        // 默认大多数活跃商品 23:00 收盘
        _ => NightSessionType::Close2300,
    }
}

/// 检查给定 5m 时间戳是否属于该品种的合法交易时段。
pub fn is_valid_5m_slot(symbol: &str, dt: &NaiveDateTime) -> bool {
    // 5m K线秒数必须为 00，分钟必须为 5 的倍数
    if dt.second() != 0 || dt.minute() % 5 != 0 {
        return false;
    }

    let weekday = dt.weekday();
    let (h, m) = (dt.hour(), dt.minute());
    let time_val = h * 60 + m; // 0..1440

    // 日盘时段（仅周一至周五）：
    // 09:05 - 10:15  => [545, 615]
    // 10:35 - 11:30  => [635, 690]
    // 13:35 - 15:00  => [815, 900]
    let is_day_slot = (545..=615).contains(&time_val)
        || (635..=690).contains(&time_val)
        || (815..=900).contains(&time_val);

    if is_day_slot {
        return matches!(
            weekday,
            Weekday::Mon | Weekday::Tue | Weekday::Wed | Weekday::Thu | Weekday::Fri
        );
    }

    // 夜盘时段
    let night_type = classify_night_session(symbol);
    if night_type == NightSessionType::None {
        return false;
    }

    // 21:05 - 23:00 => [1265, 1380]，允许周一至周五晚
    let is_night_part1 = (1265..=1380).contains(&time_val);
    if is_night_part1 {
        return matches!(
            weekday,
            Weekday::Mon | Weekday::Tue | Weekday::Wed | Weekday::Thu | Weekday::Fri
        );
    }

    // 23:05 - 23:30 => [1385, 1410]
    let is_night_part2 = (1385..=1410).contains(&time_val);
    if is_night_part2 {
        if matches!(
            night_type,
            NightSessionType::Close2330
                | NightSessionType::Close0100
                | NightSessionType::Close0230
        ) {
            return matches!(
                weekday,
                Weekday::Mon | Weekday::Tue | Weekday::Wed | Weekday::Thu | Weekday::Fri
            );
        }
        return false;
    }

    // 23:35 - 24:00 => [1415, 1440]
    let is_night_part3 = (1415..=1440).contains(&time_val);
    if is_night_part3 {
        if matches!(
            night_type,
            NightSessionType::Close0100 | NightSessionType::Close0230
        ) {
            return matches!(
                weekday,
                Weekday::Mon | Weekday::Tue | Weekday::Wed | Weekday::Thu | Weekday::Fri
            );
        }
        return false;
    }

    // 跨日凌晨时段（周二至周六凌晨，对应周一至周五晚上的夜盘延伸）：
    // 00:05 - 01:00 => [5, 60]
    let is_night_midnight1 = (5..=60).contains(&time_val) || (h == 0 && m == 0);
    if is_night_midnight1 {
        if matches!(
            night_type,
            NightSessionType::Close0100 | NightSessionType::Close0230
        ) {
            return matches!(
                weekday,
                Weekday::Tue | Weekday::Wed | Weekday::Thu | Weekday::Fri | Weekday::Sat
            );
        }
        return false;
    }

    // 01:05 - 02:30 => [65, 150]
    let is_night_midnight2 = (65..=150).contains(&time_val);
    if is_night_midnight2 {
        if matches!(night_type, NightSessionType::Close0230) {
            return matches!(
                weekday,
                Weekday::Tue | Weekday::Wed | Weekday::Thu | Weekday::Fri | Weekday::Sat
            );
        }
        return false;
    }

    false
}

/// 计算从 `current`（必须是一个合法5m槽位）出发，理论上紧邻的下一个 5m K线槽位。
pub fn next_expected_5m_slot(symbol: &str, current: NaiveDateTime) -> Option<NaiveDateTime> {
    let (h, m) = (current.hour(), current.minute());
    let night_type = classify_night_session(symbol);
    let date = current.date();

    // 1. 早盘小节休市跳转：10:15 -> 10:35
    if h == 10 && m == 15 {
        return Some(date.and_hms_opt(10, 35, 0)?);
    }
    // 2. 午休跳转：11:30 -> 13:35
    if h == 11 && m == 30 {
        return Some(date.and_hms_opt(13, 35, 0)?);
    }
    // 3. 日盘收盘 15:00 跳转：
    if h == 15 && m == 0 {
        if night_type != NightSessionType::None {
            // 如果有夜盘，且当天是周五，跳转到下周一 21:05；若是周一到周四，跳转到今晚 21:05
            let target_date = if current.weekday() == Weekday::Fri {
                date + chrono::Duration::days(3)
            } else {
                date
            };
            return Some(target_date.and_hms_opt(21, 5, 0)?);
        } else {
            // 无夜盘，周五跳到下周一 09:05；周一到周四跳到明天 09:05
            let add_days = if current.weekday() == Weekday::Fri { 3 } else { 1 };
            return Some((date + chrono::Duration::days(add_days)).and_hms_opt(9, 5, 0)?);
        }
    }

    // 4. 夜盘收盘跳转到次日早盘 09:05
    let is_night_close = match night_type {
        NightSessionType::Close2300 => h == 23 && m == 0,
        NightSessionType::Close2330 => h == 23 && m == 30,
        NightSessionType::Close0100 => h == 1 && m == 0,
        NightSessionType::Close0230 => h == 2 && m == 30,
        NightSessionType::None => false,
    };

    if is_night_close {
        // 如果收盘是在 23:xx（周一到周四晚上），跳到明天 09:05；如果是周五晚上，实际周五夜盘通常不设23点单切，跳到周一 09:05
        // 如果收盘是在 01:00 或 02:30，当天日历是周二至周六凌晨：
        // 周二至周五凌晨（周一至周四晚上的夜盘）跳到今天早上的 09:05！
        // 周六凌晨（周五晚上的夜盘）跳到下周一早上的 09:05！
        let target_date = match current.weekday() {
            Weekday::Mon | Weekday::Tue | Weekday::Wed | Weekday::Thu => {
                date + chrono::Duration::days(1)
            }
            Weekday::Fri => {
                // 周五夜盘如果在23:00结束，跳到周一
                date + chrono::Duration::days(3)
            }
            Weekday::Sat => {
                // 周六凌晨结束（01:00或02:30），跳到下周一早盘（+2天）
                date + chrono::Duration::days(2)
            }
            _ => date + chrono::Duration::days(1),
        };
        return Some(target_date.and_hms_opt(9, 5, 0)?);
    }

    // 5. 普通 5 分钟步进
    Some(current + chrono::Duration::minutes(5))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn dt(s: &str) -> NaiveDateTime {
        NaiveDateTime::parse_from_str(s, "%Y-%m-%d %H:%M:%S").unwrap()
    }

    #[test]
    fn test_is_valid_5m_slot() {
        // 日盘有效
        assert!(is_valid_5m_slot("RB0", &dt("2026-08-28 09:05:00")));
        assert!(is_valid_5m_slot("RB0", &dt("2026-08-28 10:15:00")));
        assert!(is_valid_5m_slot("RB0", &dt("2026-08-28 10:35:00")));
        assert!(is_valid_5m_slot("RB0", &dt("2026-08-28 15:00:00")));

        // 早盘休市期间无效
        assert!(!is_valid_5m_slot("RB0", &dt("2026-08-28 10:20:00")));
        assert!(!is_valid_5m_slot("RB0", &dt("2026-08-28 10:30:00")));

        // 午休期间无效
        assert!(!is_valid_5m_slot("RB0", &dt("2026-08-28 12:00:00")));
        assert!(!is_valid_5m_slot("RB0", &dt("2026-08-28 13:30:00")));

        // 无夜盘品种（红枣 CJ0）
        assert!(!is_valid_5m_slot("CJ0", &dt("2026-08-28 21:05:00")));

        // 23:00 收盘品种（螺纹钢 RB0）
        assert!(is_valid_5m_slot("RB0", &dt("2026-08-28 21:05:00")));
        assert!(is_valid_5m_slot("RB0", &dt("2026-08-28 23:00:00")));
        assert!(!is_valid_5m_slot("RB0", &dt("2026-08-28 23:05:00")));

        // 02:30 收盘品种（黄金 AU0）
        assert!(is_valid_5m_slot("AU0", &dt("2026-08-28 23:30:00")));
        assert!(is_valid_5m_slot("AU0", &dt("2026-08-29 01:00:00"))); // 周六凌晨 01:00
        assert!(is_valid_5m_slot("AU0", &dt("2026-08-29 02:30:00"))); // 周六凌晨 02:30
        assert!(!is_valid_5m_slot("AU0", &dt("2026-08-29 02:35:00")));
    }

    #[test]
    fn test_next_expected_5m_slot() {
        // 普通 5m 递增
        assert_eq!(
            next_expected_5m_slot("RB0", dt("2026-08-28 09:05:00")).unwrap(),
            dt("2026-08-28 09:10:00")
        );

        // 早盘休市跳转
        assert_eq!(
            next_expected_5m_slot("RB0", dt("2026-08-28 10:15:00")).unwrap(),
            dt("2026-08-28 10:35:00")
        );

        // 午休跳转
        assert_eq!(
            next_expected_5m_slot("RB0", dt("2026-08-28 11:30:00")).unwrap(),
            dt("2026-08-28 13:35:00")
        );

        // 周五下午 15:00 收盘跳转到周一
        // RB0 有夜盘，周五 15:00 -> 周一 21:05
        assert_eq!(
            next_expected_5m_slot("RB0", dt("2026-08-28 15:00:00")).unwrap(), // 2026-08-28 是周五
            dt("2026-08-31 21:05:00")                                        // 2026-08-31 是周一
        );

        // CJ0 无夜盘，周五 15:00 -> 周一 09:05
        assert_eq!(
            next_expected_5m_slot("CJ0", dt("2026-08-28 15:00:00")).unwrap(),
            dt("2026-08-31 09:05:00")
        );

        // RB0 周四晚上 23:00 收盘 -> 周五早盘 09:05
        assert_eq!(
            next_expected_5m_slot("RB0", dt("2026-08-27 23:00:00")).unwrap(), // 2026-08-27 是周四
            dt("2026-08-28 09:05:00")
        );
    }
}
