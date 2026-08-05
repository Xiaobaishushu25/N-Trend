//! 调度状态机（纯逻辑，可单测）：数据刷新与扫描动作的时机判定。
//!
//! 默认节奏：每 5 分钟增量刷新 5m 数据；每 15 分钟边界（:00/:15/:30/:45）跑一次分析。
//! 节奏从“上次尝试的开始时间”起算，拉取/分析耗时不会把下一次任务向后推。
//! 交易时段过滤默认开启：仅在国内期货日盘/夜盘窗口内触发，避免无效请求。

use chrono::{DateTime, Datelike, Local, NaiveTime, Timelike};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SchedulerConfig {
    pub refresh_interval_secs: u64,
    pub scan_interval_secs: u64,
    pub trading_only: bool,
}

impl Default for SchedulerConfig {
    fn default() -> Self {
        Self {
            refresh_interval_secs: 300,
            scan_interval_secs: 900,
            trading_only: true,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SchedulerAction {
    None,
    Refresh,
    Scan,
    RefreshAndScan,
}

/// 判定当前 tick 应执行的动作。
pub fn next_action(
    now: DateTime<Local>,
    cfg: &SchedulerConfig,
    last_refresh: Option<DateTime<Local>>,
    last_scan: Option<DateTime<Local>>,
) -> SchedulerAction {
    if cfg.trading_only && !is_trading_time(&now) {
        return SchedulerAction::None;
    }

    let refresh_due = last_refresh.map_or(true, |t| {
        (now - t).num_seconds() >= cfg.refresh_interval_secs as i64
    });
    let scan_due = now.minute() % 15 == 0
        && last_scan.map_or(true, |t| {
            (now - t).num_seconds() >= cfg.scan_interval_secs as i64
        });

    match (refresh_due, scan_due) {
        (true, true) => SchedulerAction::RefreshAndScan,
        (true, false) => SchedulerAction::Refresh,
        (false, true) => SchedulerAction::Scan,
        (false, false) => SchedulerAction::None,
    }
}

/// 国内期货常见交易窗口（近似）：
/// - 日盘 09:00-10:15 / 10:30-11:30 / 13:30-15:00
/// - 夜盘 21:00-23:30（周五夜盘顺延至周六 02:30）
pub fn is_trading_time(now: &DateTime<Local>) -> bool {
    let weekday = now.weekday().num_days_from_monday(); // 0=周一 .. 6=周日
    let t = now.time();
    let fri_night = weekday == 4 && t >= NaiveTime::from_hms_opt(21, 0, 0).unwrap();
    let early_sat = weekday == 5 && t <= NaiveTime::from_hms_opt(2, 30, 0).unwrap();
    if fri_night || early_sat {
        return true;
    }
    if weekday >= 5 {
        return false; // 周六/周日白天
    }
    in_day_window(t) || in_night_window(t)
}

fn in_day_window(t: NaiveTime) -> bool {
    let open = |h: u32, m: u32| NaiveTime::from_hms_opt(h, m, 0).unwrap();
    (t >= open(9, 0) && t < open(10, 15))
        || (t >= open(10, 30) && t < open(11, 30))
        || (t >= open(13, 30) && t < open(15, 0))
}

fn in_night_window(t: NaiveTime) -> bool {
    let open = |h: u32, m: u32| NaiveTime::from_hms_opt(h, m, 0).unwrap();
    t >= open(21, 0) && t < open(23, 30)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn dt(y: i32, m: u32, d: u32, h: u32, min: u32) -> DateTime<Local> {
        chrono::NaiveDate::from_ymd_opt(y, m, d)
            .unwrap()
            .and_hms_opt(h, min, 0)
            .unwrap()
            .and_local_timezone(Local)
            .unwrap()
    }

    #[test]
    fn trading_window_weekday_day() {
        assert!(is_trading_time(&dt(2026, 8, 3, 9, 30))); // 周一早盘
        assert!(is_trading_time(&dt(2026, 8, 3, 14, 0))); // 周一下午盘
        assert!(!is_trading_time(&dt(2026, 8, 3, 10, 20))); // 早盘休市
        assert!(!is_trading_time(&dt(2026, 8, 3, 12, 0))); // 午休
    }

    #[test]
    fn trading_window_night_and_weekend() {
        assert!(is_trading_time(&dt(2026, 8, 3, 21, 30))); // 周一夜盘
        assert!(is_trading_time(&dt(2026, 8, 7, 22, 0))); // 周五夜盘
        assert!(is_trading_time(&dt(2026, 8, 8, 1, 0))); // 周六凌晨（周五夜盘延续）
        assert!(!is_trading_time(&dt(2026, 8, 8, 3, 0))); // 周六凌晨休市
        assert!(!is_trading_time(&dt(2026, 8, 8, 10, 0))); // 周六白天
        assert!(!is_trading_time(&dt(2026, 8, 9, 21, 0))); // 周日无夜盘
    }

    #[test]
    fn scan_only_on_boundaries() {
        let cfg = SchedulerConfig::default();
        assert_eq!(
            next_action(dt(2026, 8, 3, 9, 15), &cfg, None, None),
            SchedulerAction::RefreshAndScan
        );
        assert_eq!(
            next_action(dt(2026, 8, 3, 9, 20), &cfg, Some(dt(2026, 8, 3, 9, 16)), Some(dt(2026, 8, 3, 9, 15))),
            SchedulerAction::None
        );
        assert_eq!(
            next_action(dt(2026, 8, 3, 9, 30), &cfg, Some(dt(2026, 8, 3, 9, 20)), Some(dt(2026, 8, 3, 9, 15))),
            SchedulerAction::RefreshAndScan
        );
    }

    #[test]
    fn trading_filter_blocks_off_hours() {
        let cfg = SchedulerConfig::default();
        assert_eq!(
            next_action(dt(2026, 8, 3, 16, 0), &cfg, None, None),
            SchedulerAction::None
        );
        let mut off = cfg.clone();
        off.trading_only = false;
        assert_eq!(
            next_action(dt(2026, 8, 3, 16, 0), &off, None, None),
            SchedulerAction::RefreshAndScan
        );
    }
}

