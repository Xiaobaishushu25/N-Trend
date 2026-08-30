//! Production Finality Judger and Policy Engine (Issue 01).
//!
//! 统一收敛各层硬编码时间防御，提供基于品种交易日历（SessionCalendar）与
//! 最小结算期（minimum settle）的统一 5m Finality 判定核心。

use chrono::{NaiveDateTime, Timelike};
use serde::{Deserialize, Serialize};

use crate::fetch::kline::Kline;
use crate::session::SessionCalendar;

/// 5m K 线定版策略配置。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FinalityPolicy {
    /// 普通 5m K 线的最小结算时间（秒），经验基准值为 30s
    pub ordinary_settle_secs: i64,
    /// 法定收盘 5m K 线的最小结算时间（秒），经验基准值为 75s
    pub close_settle_secs: i64,
    /// 是否启用 Finality 校验（若关闭则所有历史/增量 K 线直接视为 Final）
    pub enabled: bool,
}

impl Default for FinalityPolicy {
    fn default() -> Self {
        Self {
            ordinary_settle_secs: 30,
            close_settle_secs: 75,
            enabled: true,
        }
    }
}

/// K 线的定版状态。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum FinalityStatus {
    /// 候选状态（落在最小结算窗口内，接口数据仍可能在变动）
    Candidate,
    /// 最终定版状态（已满足最小结算期或已确认收盘，可安全进入入库与派生）
    Final,
}

/// 生产级 Finality 判定器。
#[derive(Debug, Clone)]
pub struct FinalityJudger {
    policy: FinalityPolicy,
}

impl Default for FinalityJudger {
    fn default() -> Self {
        Self::new(FinalityPolicy::default())
    }
}

impl FinalityJudger {
    pub fn new(policy: FinalityPolicy) -> Self {
        Self { policy }
    }

    pub fn policy(&self) -> &FinalityPolicy {
        &self.policy
    }

    /// 获取特定品种在特定 K 线时刻所需的最小结算秒数。
    pub fn required_settle_secs(&self, symbol: &str, hour: u32, minute: u32) -> i64 {
        if SessionCalendar::is_session_close(symbol, hour, minute) {
            self.policy.close_settle_secs
        } else {
            self.policy.ordinary_settle_secs
        }
    }

    /// 评估特定品种单根 K 线的时间定版状态。
    pub fn evaluate_bar(
        &self,
        symbol: &str,
        bar_dt: &NaiveDateTime,
        now: &NaiveDateTime,
    ) -> FinalityStatus {
        if !self.policy.enabled {
            return FinalityStatus::Final;
        }

        let elapsed = now.signed_duration_since(*bar_dt).num_seconds();
        let required = self.required_settle_secs(symbol, bar_dt.hour(), bar_dt.minute());

        if elapsed >= required {
            FinalityStatus::Final
        } else {
            FinalityStatus::Candidate
        }
    }

    /// 查询特定品种单根 K 线是否已达到 Final 状态。
    pub fn is_bar_final(
        &self,
        symbol: &str,
        bar_dt: &NaiveDateTime,
        now: &NaiveDateTime,
    ) -> bool {
        self.evaluate_bar(symbol, bar_dt, now) == FinalityStatus::Final
    }

    /// 计算达到 Final 状态尚需等待的秒数（若已定版则返回 0）。
    pub fn remaining_settle_secs(
        &self,
        symbol: &str,
        bar_dt: &NaiveDateTime,
        now: &NaiveDateTime,
    ) -> i64 {
        if !self.policy.enabled {
            return 0;
        }

        let elapsed = now.signed_duration_since(*bar_dt).num_seconds();
        let required = self.required_settle_secs(symbol, bar_dt.hour(), bar_dt.minute());

        (required - elapsed).max(0)
    }

    /// 过滤抓取到的 K 线列表，仅保留已满足 Final 状态的 K 线。
    pub fn filter_final_rows(
        &self,
        symbol: &str,
        rows: Vec<Kline>,
        now: NaiveDateTime,
    ) -> Vec<Kline> {
        if !self.policy.enabled {
            return rows;
        }

        rows.into_iter()
            .filter(|k| {
                match NaiveDateTime::parse_from_str(&k.datetime, "%Y-%m-%d %H:%M:%S") {
                    Ok(bar_dt) => self.is_bar_final(symbol, &bar_dt, &now),
                    // 解析失败保留（由底层管道处理异常数据）
                    Err(_) => true,
                }
            })
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn dt(s: &str) -> NaiveDateTime {
        NaiveDateTime::parse_from_str(s, "%Y-%m-%d %H:%M:%S").unwrap()
    }

    #[test]
    fn test_ordinary_bar_settle_threshold() {
        let judger = FinalityJudger::default();
        let bar_dt = dt("2026-08-31 10:00:00");

        // 29 秒：尚在结算窗口，为 Candidate
        let now_29 = dt("2026-08-31 10:00:29");
        assert_eq!(judger.evaluate_bar("RB0", &bar_dt, &now_29), FinalityStatus::Candidate);
        assert!(!judger.is_bar_final("RB0", &bar_dt, &now_29));
        assert_eq!(judger.remaining_settle_secs("RB0", &bar_dt, &now_29), 1);

        // 30 秒：达到 minimum_settle，为 Final
        let now_30 = dt("2026-08-31 10:00:30");
        assert_eq!(judger.evaluate_bar("RB0", &bar_dt, &now_30), FinalityStatus::Final);
        assert!(judger.is_bar_final("RB0", &bar_dt, &now_30));
        assert_eq!(judger.remaining_settle_secs("RB0", &bar_dt, &now_30), 0);
    }

    #[test]
    fn test_session_close_bar_settle_threshold() {
        let judger = FinalityJudger::default();
        // 11:30 为国内所有商品法定收盘时段
        let close_dt = dt("2026-08-31 11:30:00");

        // 普通 35 秒：对收盘 K 仍然是 Candidate（旧代码在此处误放行）
        let now_35 = dt("2026-08-31 11:30:35");
        assert_eq!(judger.evaluate_bar("RB0", &close_dt, &now_35), FinalityStatus::Candidate);
        assert_eq!(judger.remaining_settle_secs("RB0", &close_dt, &now_35), 40);

        // 74 秒：仍需等待 1 秒
        let now_74 = dt("2026-08-31 11:31:14");
        assert_eq!(judger.evaluate_bar("RB0", &close_dt, &now_74), FinalityStatus::Candidate);

        // 75 秒：收盘 K 线正式 Final
        let now_75 = dt("2026-08-31 11:31:15");
        assert_eq!(judger.evaluate_bar("RB0", &close_dt, &now_75), FinalityStatus::Final);
        assert!(judger.is_bar_final("RB0", &close_dt, &now_75));
        assert_eq!(judger.remaining_settle_secs("RB0", &close_dt, &now_75), 0);
    }

    #[test]
    fn test_night_session_close_variety_recognition() {
        let judger = FinalityJudger::default();
        // 黑色系 RB0 在 23:00 收盘
        let rb_close = dt("2026-08-31 23:00:00");
        assert_eq!(judger.required_settle_secs("RB0", 23, 0), 75);
        assert!(!judger.is_bar_final("RB0", &rb_close, &dt("2026-08-31 23:01:14")));
        assert!(judger.is_bar_final("RB0", &rb_close, &dt("2026-08-31 23:01:15")));

        // 有色金属 CU0 在 01:00 收盘
        let cu_close = dt("2026-09-01 01:00:00");
        assert_eq!(judger.required_settle_secs("CU0", 1, 0), 75);
        assert!(!judger.is_bar_final("CU0", &cu_close, &dt("2026-09-01 01:01:14")));
        assert!(judger.is_bar_final("CU0", &cu_close, &dt("2026-09-01 01:01:15")));

        // 纯碱 SA0 在 23:30 收盘
        assert_eq!(judger.required_settle_secs("SA0", 23, 30), 75);

        // 贵金属 AU0 在 02:30 收盘
        assert_eq!(judger.required_settle_secs("AU0", 2, 30), 75);

        // 无夜盘 CJ0 在 21:00 不需要 75s（非其收盘点）
        assert_eq!(judger.required_settle_secs("CJ0", 21, 0), 30);
    }

    #[test]
    fn test_filter_final_rows() {
        let judger = FinalityJudger::default();
        let rows = vec![
            Kline {
                datetime: "2026-08-31 11:25:00".to_string(),
                open: 3500.0,
                high: 3510.0,
                low: 3495.0,
                close: 3505.0,
                volume: 1000.0,
                hold: 50000.0,
            },
            Kline {
                datetime: "2026-08-31 11:30:00".to_string(),
                open: 3505.0,
                high: 3512.0,
                low: 3500.0,
                close: 3508.0,
                volume: 1200.0,
                hold: 50100.0,
            },
        ];

        // 11:30:35（距 11:30 仅 35 秒）：收盘 K 线被过滤，只保留 11:25
        let now_early = dt("2026-08-31 11:30:35");
        let filtered_early = judger.filter_final_rows("RB0", rows.clone(), now_early);
        assert_eq!(filtered_early.len(), 1);
        assert_eq!(filtered_early[0].datetime, "2026-08-31 11:25:00");

        // 11:31:15（距 11:30 达 75 秒）：收盘 K 线合格通过
        let now_settled = dt("2026-08-31 11:31:15");
        let filtered_settled = judger.filter_final_rows("RB0", rows, now_settled);
        assert_eq!(filtered_settled.len(), 2);
    }
}
