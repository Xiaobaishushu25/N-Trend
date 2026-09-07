//! Global session calendar and registry for all futures symbols.

use chrono::{DateTime, Local, NaiveDateTime};
use super::spec::{NightSessionType, TradingSessionSpec};

pub struct SessionCalendar;

impl SessionCalendar {
    /// 提取品种合约代码的大写前缀字母。
    pub fn contract_prefix(symbol: &str) -> String {
        symbol
            .chars()
            .take_while(|c| c.is_alphabetic())
            .collect::<String>()
            .to_uppercase()
    }

    /// 根据合约代码自动获取品种时段规约。
    pub fn get(symbol: &str) -> TradingSessionSpec {
        let prefix = Self::contract_prefix(symbol);
        let night_type = classify_night_session(&prefix);
        TradingSessionSpec::new(symbol, night_type)
    }

    /// 查询某品种在特定时间是否处于交易时间。
    pub fn is_trading_time(symbol: &str, now: &DateTime<Local>) -> bool {
        Self::get(symbol).is_in_trading_time(now)
    }

    /// 查询多品种中是否至少有一个品种当前处于交易时段（或兜底全局判断）。
    pub fn is_any_trading_time(symbols: &[String], now: &DateTime<Local>) -> bool {
        if symbols.is_empty() {
            return Self::is_global_trading_time(now);
        }
        symbols.iter().any(|s| Self::is_trading_time(s, now))
    }

    /// 查询某品种在特定时间是否处于活跃刷新状态（交易中或在收盘宽限期内）。
    pub fn is_active_for_refresh(symbol: &str, now: &DateTime<Local>) -> bool {
        Self::get(symbol).is_active_for_refresh(now)
    }

    /// 过滤出当前处于活跃刷新状态的品种列表。
    pub fn active_symbols<'a>(symbols: &'a [String], now: &DateTime<Local>) -> Vec<&'a String> {
        symbols
            .iter()
            .filter(|s| Self::is_active_for_refresh(s, now))
            .collect()
    }

    /// 查询某品种在特定时间戳是否为收盘时刻。
    pub fn is_session_close(symbol: &str, hour: u32, minute: u32) -> bool {
        Self::get(symbol).is_session_close(hour, minute)
    }

    /// 返回当前时刻之后最近的该品种交易时段收盘时间。
    /// 收盘时刻来自品种规约，支持跨午夜夜盘；调用方应先确认品种当前处于交易时段。
    pub fn next_session_close(symbol: &str, now: &NaiveDateTime) -> Option<NaiveDateTime> {
        let spec = Self::get(symbol);
        let mut candidates = Vec::new();
        for day_offset in 0..=2 {
            let date = now.date() + chrono::Days::new(day_offset);
            for (hour, minute) in spec.session_close_times() {
                let Some(candidate) = date.and_hms_opt(hour, minute, 0) else {
                    continue;
                };
                candidates.push(candidate);
            }
        }
        candidates.into_iter().filter(|candidate| *candidate > *now).min()
    }

    /// 国内全市场全部可能的收盘时刻列表（用于调度器全局结算判定）：
    /// 包含 (10, 15), (11, 30), (15, 0), (23, 0), (23, 30), (1, 0), (2, 30)。
    pub fn all_close_moments() -> &'static [(u32, u32)] {
        &[
            (10, 15),
            (11, 30),
            (15, 0),
            (23, 0),
            (23, 30),
            (1, 0),
            (2, 30),
        ]
    }

    /// 判断当前时刻（时, 分）是否属于全市场中任何一个品种的收盘时刻。
    pub fn is_any_close_moment(hour: u32, minute: u32) -> bool {
        Self::all_close_moments().contains(&(hour, minute))
    }

    /// 全局结算等待秒数：如果该时刻是任何品种的收盘时刻则为 80 秒，否则为普通 40 秒。
    pub fn global_scan_settle_secs(hour: u32, minute: u32) -> u32 {
        if Self::is_any_close_moment(hour, minute) {
            80
        } else {
            40
        }
    }

    /// 全局兜底交易时段判断（以最长交易品种黄金 AU0 覆盖全市场）：
    pub fn is_global_trading_time(now: &DateTime<Local>) -> bool {
        Self::get("AU0").is_in_trading_time(now)
    }
}

/// 根据品种代码或前缀分类夜盘类型（自动提取字母前缀并转大写）。
pub fn classify_night_session(symbol_or_prefix: &str) -> NightSessionType {
    let prefix = SessionCalendar::contract_prefix(symbol_or_prefix);
    match prefix.as_str() {
        // 02:30 贵金属 & 原油
        "AU" | "AG" | "SC" => NightSessionType::Close0230,

        // 01:00 有色金属、不锈钢
        "CU" | "AL" | "ZN" | "PB" | "NI" | "SN" | "BC" | "SS" => NightSessionType::Close0100,

        // 23:30 纯碱、玻璃
        "SA" | "FG" => NightSessionType::Close2330,

        // 无夜盘品种（农产品、部分化工、金融期货等）
        "AP" | "CJ" | "JD" | "LH" | "PK" | "SI" | "LC" | "UR" | "WH" | "PM" | "RI" | "JR"
        | "LR" | "BB" | "FB" | "IF" | "IH" | "IC" | "IM" | "TF" | "T" | "TS" | "TL" => {
            NightSessionType::None
        }

        // 其余默认大多数 23:00（黑色系、能化等）
        _ => NightSessionType::Close2300,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::{Local, NaiveDate, TimeZone};

    fn local_dt(y: i32, m: u32, d: u32, h: u32, min: u32) -> DateTime<Local> {
        Local.with_ymd_and_hms(y, m, d, h, min, 0).unwrap()
    }

    #[test]
    fn test_session_calendar_classification() {
        assert_eq!(classify_night_session("RB"), NightSessionType::Close2300);
        assert_eq!(classify_night_session("CU"), NightSessionType::Close0100);
        assert_eq!(classify_night_session("SS0"), NightSessionType::Close0100);
        assert_eq!(classify_night_session("AU"), NightSessionType::Close0230);
        assert_eq!(classify_night_session("CJ"), NightSessionType::None);
    }

    #[test]
    fn test_all_close_moments_includes_2300_and_0100() {
        assert!(SessionCalendar::is_any_close_moment(23, 0));
        assert!(SessionCalendar::is_any_close_moment(1, 0));
        assert!(SessionCalendar::is_any_close_moment(15, 0));
        assert_eq!(SessionCalendar::global_scan_settle_secs(23, 0), 80);
        assert_eq!(SessionCalendar::global_scan_settle_secs(1, 0), 80);
        assert_eq!(SessionCalendar::global_scan_settle_secs(10, 0), 40);
    }

    #[test]
    fn test_active_symbols_filtering() {
        let symbols = vec!["RB0".to_string(), "CJ0".to_string()];
        // 晚上 21:30：RB0 活跃，CJ0 静默
        let night_time = local_dt(2026, 8, 24, 21, 30);
        let active = SessionCalendar::active_symbols(&symbols, &night_time);
        assert_eq!(active.len(), 1);
        assert_eq!(active[0], "RB0");
    }

    #[test]
    fn test_next_session_close_handles_cross_midnight_ordering() {
        let day = NaiveDate::from_ymd_opt(2026, 8, 24).unwrap();
        let at_day_close = day.and_hms_opt(14, 57, 0).unwrap();
        assert_eq!(
            SessionCalendar::next_session_close("RB0", &at_day_close),
            Some(day.and_hms_opt(15, 0, 0).unwrap())
        );

        let at_cu_night_close = day.and_hms_opt(0, 57, 0).unwrap();
        assert_eq!(
            SessionCalendar::next_session_close("CU0", &at_cu_night_close),
            Some(day.and_hms_opt(1, 0, 0).unwrap())
        );

        let at_au_night = day.and_hms_opt(22, 57, 0).unwrap();
        assert_eq!(
            SessionCalendar::next_session_close("AU0", &at_au_night),
            Some((day + chrono::Days::new(1)).and_hms_opt(2, 30, 0).unwrap())
        );
    }
}
