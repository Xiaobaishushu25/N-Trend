//! 调度状态机（纯逻辑，可单测）：数据刷新与扫描动作的时机判定。
//!
//! 默认节奏：每 5 分钟增量刷新 5m 数据，刷新按分钟网格边界对齐（间隔/60 的整数倍分钟，含整点）；
//! 每 15 分钟边界顺延 40 秒（普通）或 80 秒（收盘 10:15/11:30/15:00/23:30/02:30）跑一次分析，
//! 确保延迟补拉（普通 35秒、收盘 75秒）已完成后再扫，避免沿用临时值。
//! 扫描按墙钟格子去重（而非 now-last_scan >=900），避免首扫漂移导致下一档 11:15 被跳过。
//! 拉取/分析耗时不会把下一次任务向后推；App 启动时的首次独立补拉由调度循环层处理。
//! 交易时段过滤默认开启：仅在国内期货日盘/夜盘窗口内触发，避免无效请求。

use chrono::{DateTime, Local, NaiveDateTime, Timelike};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
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
    let settled_scan_due = is_settled_scan_due(now, last_scan);
    if cfg.trading_only && !is_trading_time(&now) && !is_close_grace_due(now, last_scan) {
        return SchedulerAction::None;
    }
    let align_min = (cfg.refresh_interval_secs / 60).max(1) as u32;
    let normal_refresh_due = now.minute() % align_min == 0
        && last_refresh.map_or(true, |t| {
            (now - t).num_seconds() >= cfg.refresh_interval_secs as i64
        });
    let refresh_due = normal_refresh_due || settled_scan_due;
    let scan_due = settled_scan_due;
    match (refresh_due, scan_due) {
        (true, true) => SchedulerAction::RefreshAndScan,
        (true, false) => SchedulerAction::Refresh,
        (false, true) => SchedulerAction::Scan,
        (false, false) => SchedulerAction::None,
    }
}

const SCAN_WINDOW_SECS: i64 = 75;

fn scan_settle_secs(hour: u32, minute: u32) -> u32 {
    crate::session::SessionCalendar::global_scan_settle_secs(hour, minute)
}

fn is_settled_scan_due(now: DateTime<Local>, last_scan: Option<DateTime<Local>>) -> bool {
    let minute = now.minute();
    let grid_min = minute / 15 * 15;
    let hour = now.hour();
    if let Some(scan_time) = scan_time_for_grid(now, hour, grid_min) {
        let window = chrono::Duration::seconds(SCAN_WINDOW_SECS);
        if now.naive_local() >= scan_time && now.naive_local() < scan_time + window {
            if last_scan.map_or(true, |t| t.naive_local() < scan_time) {
                return true;
            }
        }
    }
    false
}

fn is_close_grace_due(now: DateTime<Local>, last_scan: Option<DateTime<Local>>) -> bool {
    // 允许收盘格子的 scan_time+window 落到交易窗口外仍触发，例如 15:00+80=15:01:20
    // 仅对收盘格子放行，避免非交易时段被普通格子误触发
    let minute = now.minute();
    let grid_min = minute / 15 * 15;
    let hour = now.hour();
    // 当前 floor 格子
    if let Some(scan_time) = scan_time_for_grid(now, hour, grid_min) {
        if crate::session::SessionCalendar::is_any_close_moment(hour, grid_min) {
            let window = chrono::Duration::seconds(SCAN_WINDOW_SECS);
            if now.naive_local() >= scan_time && now.naive_local() < scan_time + window {
                if last_scan.map_or(true, |t| t.naive_local() < scan_time) {
                    return true;
                }
            }
        }
    }
    // 处理溢出到下一分钟的场景：例如 now=11:31:20 floor=30 需检查 11:30 格子（已覆盖），
    // 但 15:01:20 floor=0 的前一个格子是 15:00，需额外检查前一格是否为收盘
    // 统一再检查前一个 15 分钟格子（避免跨小时/跨天复杂，仅对收盘格子检查）
    let prev = now - chrono::Duration::minutes(15);
    let ph = prev.hour();
    let pm = prev.minute() / 15 * 15;
    if crate::session::SessionCalendar::is_any_close_moment(ph, pm) {
        if let Some(scan_time) = scan_time_for_grid(prev, ph, pm) {
            let window = chrono::Duration::seconds(SCAN_WINDOW_SECS);
            if now.naive_local() >= scan_time && now.naive_local() < scan_time + window {
                if last_scan.map_or(true, |t| t.naive_local() < scan_time) {
                    return true;
                }
            }
        }
    }
    false
}

fn scan_time_for_grid(now: DateTime<Local>, hour: u32, grid_min: u32) -> Option<NaiveDateTime> {
    let date = now.date_naive();
    let grid_time = date.and_hms_opt(hour, grid_min, 0)?;
    let settle = scan_settle_secs(hour, grid_min) as i64;
    Some(grid_time + chrono::Duration::seconds(settle))
}

/// 国内期货交易窗口判断（统一委托至 SessionCalendar）：
/// - 支持按品种查询；
/// - 全局模式以全市场最大交易时段（含夜盘至 02:30 的贵金属）为准。
pub fn is_trading_time(now: &DateTime<Local>) -> bool {
    crate::session::SessionCalendar::is_global_trading_time(now)
}

/// 查询特定品种在当前时刻是否处于交易时段。
pub fn is_symbol_trading_time(symbol: &str, now: &DateTime<Local>) -> bool {
    crate::session::SessionCalendar::is_trading_time(symbol, now)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn dt(y: i32, m: u32, d: u32, h: u32, min: u32) -> DateTime<Local> {
        chrono::NaiveDate::from_ymd_opt(y, m, d).unwrap().and_hms_opt(h, min, 0).unwrap().and_local_timezone(Local).unwrap()
    }
    fn dt_sec(y: i32, m: u32, d: u32, h: u32, min: u32, s: u32) -> DateTime<Local> {
        chrono::NaiveDate::from_ymd_opt(y, m, d).unwrap().and_hms_opt(h, min, s).unwrap().and_local_timezone(Local).unwrap()
    }
    #[test]
    fn trading_window_weekday_day() {
        assert!(is_trading_time(&dt(2026, 8, 3, 9, 30)));
        assert!(!is_trading_time(&dt(2026, 8, 3, 10, 16)));
        assert!(is_trading_time(&dt(2026, 8, 3, 10, 30)));
    }
    #[test]
    fn trading_window_closing_minute() {
        assert!(is_trading_time(&dt(2026, 8, 3, 10, 15)));
        assert!(is_trading_time(&dt(2026, 8, 3, 11, 30)));
        assert!(is_trading_time(&dt(2026, 8, 3, 15, 0)));
        assert!(is_trading_time(&dt(2026, 8, 3, 23, 30)));
        assert!(is_trading_time(&dt(2026, 8, 8, 2, 30)));
        assert!(!is_trading_time(&dt(2026, 8, 3, 10, 16)));
        assert!(!is_trading_time(&dt(2026, 8, 3, 11, 31)));
        assert!(!is_trading_time(&dt(2026, 8, 3, 15, 1)));
        assert!(!is_trading_time(&dt(2026, 8, 8, 2, 31)));

        // 品种级时段精确测试
        assert!(is_symbol_trading_time("RB0", &dt(2026, 8, 3, 23, 0)));
        assert!(!is_symbol_trading_time("RB0", &dt(2026, 8, 3, 23, 1)));
        assert!(!is_symbol_trading_time("CJ0", &dt(2026, 8, 3, 21, 5)));
        assert!(is_symbol_trading_time("CU0", &dt(2026, 8, 8, 1, 0)));
        assert!(!is_symbol_trading_time("CU0", &dt(2026, 8, 8, 1, 1)));
    }
    #[test]
    fn refresh_fires_at_session_close() {
        let cfg = SchedulerConfig::default();
        assert_eq!(next_action(dt(2026, 8, 3, 15, 0), &cfg, Some(dt(2026, 8, 3, 14, 55)), Some(dt(2026, 8, 3, 14, 45))), SchedulerAction::Refresh);
        assert_eq!(next_action(dt_sec(2026, 8, 3, 15, 1, 20), &cfg, Some(dt(2026, 8, 3, 15, 0)), Some(dt(2026, 8, 3, 14, 45))), SchedulerAction::RefreshAndScan);
    }
    #[test]
    fn scan_runs_after_settle_with_refresh() {
        let cfg = SchedulerConfig::default();
        assert_eq!(next_action(dt(2026, 8, 3, 9, 15), &cfg, None, None), SchedulerAction::Refresh);
        assert_eq!(next_action(dt_sec(2026, 8, 3, 9, 15, 39), &cfg, Some(dt(2026, 8, 3, 9, 15)), Some(dt(2026, 8, 3, 9, 0))), SchedulerAction::None);
        assert_eq!(next_action(dt_sec(2026, 8, 3, 9, 15, 40), &cfg, Some(dt(2026, 8, 3, 9, 15)), Some(dt(2026, 8, 3, 9, 0))), SchedulerAction::RefreshAndScan);
        assert_eq!(next_action(dt(2026, 8, 3, 9, 20), &cfg, Some(dt(2026, 8, 3, 9, 16)), Some(dt_sec(2026, 8, 3, 9, 15, 40))), SchedulerAction::None);
        assert_eq!(next_action(dt_sec(2026, 8, 3, 9, 30, 40), &cfg, Some(dt(2026, 8, 3, 9, 25)), Some(dt_sec(2026, 8, 3, 9, 15, 40))), SchedulerAction::RefreshAndScan);
        assert_eq!(next_action(dt_sec(2026, 8, 3, 11, 30, 40), &cfg, Some(dt(2026, 8, 3, 11, 30)), Some(dt_sec(2026, 8, 3, 11, 15, 40))), SchedulerAction::None);
        assert_eq!(next_action(dt_sec(2026, 8, 3, 11, 31, 20), &cfg, Some(dt(2026, 8, 3, 11, 30)), Some(dt_sec(2026, 8, 3, 11, 15, 40))), SchedulerAction::RefreshAndScan);
    }
    #[test]
    fn scan_no_drift_after_delayed_first_scan() {
        let cfg = SchedulerConfig::default();
        let first_scan = dt_sec(2026, 8, 26, 11, 1, 1);
        assert_eq!(next_action(dt_sec(2026, 8, 26, 11, 15, 40), &cfg, Some(dt_sec(2026, 8, 26, 11, 15, 0)), Some(first_scan)), SchedulerAction::RefreshAndScan);
        assert_eq!(next_action(dt_sec(2026, 8, 26, 11, 15, 55), &cfg, Some(dt_sec(2026, 8, 26, 11, 15, 40)), Some(dt_sec(2026, 8, 26, 11, 15, 40))), SchedulerAction::None);
    }
    #[test]
    fn refresh_aligned_to_minute_grid() {
        let cfg = SchedulerConfig::default();
        assert_eq!(next_action(dt(2026, 8, 3, 9, 17), &cfg, None, None), SchedulerAction::None);
        assert_eq!(next_action(dt(2026, 8, 3, 9, 20), &cfg, None, None), SchedulerAction::Refresh);
        assert_eq!(next_action(dt(2026, 8, 3, 9, 30), &cfg, None, None), SchedulerAction::Refresh);
        assert_eq!(next_action(dt_sec(2026, 8, 3, 9, 30, 40), &cfg, Some(dt(2026, 8, 3, 9, 30)), None), SchedulerAction::RefreshAndScan);
        assert_eq!(next_action(dt(2026, 8, 3, 10, 0), &cfg, None, None), SchedulerAction::Refresh);
        assert_eq!(next_action(dt(2026, 8, 3, 9, 20), &cfg, Some(dt(2026, 8, 3, 9, 20)), None), SchedulerAction::None);
    }
    #[test]
    fn trading_filter_blocks_off_hours() {
        let cfg = SchedulerConfig::default();
        assert_eq!(next_action(dt(2026, 8, 3, 16, 0), &cfg, None, None), SchedulerAction::None);
        let mut off = cfg.clone();
        off.trading_only = false;
        assert_eq!(next_action(dt_sec(2026, 8, 3, 16, 0, 40), &off, None, None), SchedulerAction::RefreshAndScan);
    }
}
