//! 中国期货法定节假日休市日历与交易日判定模块。
//!
//! 整合天勤（TqSdk）交易所法定休市日历，支持本地持久化缓存。
//! 精确处理中国期货市场的特殊节假日规则：
//! 1. 周末及法定节假日（中秋、国庆、春节等）全天休市；
//! 2. 遇法定节假日前一个交易日当晚不进行夜盘交易；
//! 3. 节假日次日凌晨无跨日夜盘延续；
//! 4. 离线/未拉取状态下自动平滑降级为工作日规则。

use std::collections::HashSet;
use std::path::Path;
use std::sync::{OnceLock, RwLock};

use chrono::{Datelike, Days, NaiveDate, Weekday};
use serde::{Deserialize, Serialize};

use crate::fetch::tq_client::TradingCalendarDay;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TradingCalendarRecord {
    pub date: String,
    pub trading: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct TradingCalendarFile {
    pub updated_at: String,
    pub records: Vec<TradingCalendarRecord>,
}

#[derive(Debug, Default)]
pub struct TradingCalendar {
    trading_days: HashSet<NaiveDate>,
    non_trading_days: HashSet<NaiveDate>,
    min_date: Option<NaiveDate>,
    max_date: Option<NaiveDate>,
}

impl TradingCalendar {
    pub fn new() -> Self {
        Self::default()
    }

    /// 更新日历记录（来自天勤 API 或缓存文件）。
    pub fn update_days(&mut self, days: &[TradingCalendarDay]) {
        for d in days {
            if let Ok(parsed) = NaiveDate::parse_from_str(&d.date, "%Y-%m-%d") {
                if d.trading {
                    self.trading_days.insert(parsed);
                    self.non_trading_days.remove(&parsed);
                } else {
                    self.non_trading_days.insert(parsed);
                    self.trading_days.remove(&parsed);
                }
                self.min_date = Some(self.min_date.map_or(parsed, |m| m.min(parsed)));
                self.max_date = Some(self.max_date.map_or(parsed, |m| m.max(parsed)));
            }
        }
    }

    /// 更新通用日历记录。
    pub fn update_records(&mut self, records: &[TradingCalendarRecord]) {
        for r in records {
            if let Ok(parsed) = NaiveDate::parse_from_str(&r.date, "%Y-%m-%d") {
                if r.trading {
                    self.trading_days.insert(parsed);
                    self.non_trading_days.remove(&parsed);
                } else {
                    self.non_trading_days.insert(parsed);
                    self.trading_days.remove(&parsed);
                }
                self.min_date = Some(self.min_date.map_or(parsed, |m| m.min(parsed)));
                self.max_date = Some(self.max_date.map_or(parsed, |m| m.max(parsed)));
            }
        }
    }

    /// 是否已加载有效日历数据。
    pub fn is_loaded(&self) -> bool {
        self.min_date.is_some() && self.max_date.is_some()
    }

    /// 判断特定日期是否为交易日（白盘开盘日）。
    pub fn is_trading_day(&self, date: &NaiveDate) -> bool {
        if let (Some(min), Some(max)) = (self.min_date, self.max_date) {
            if *date >= min && *date <= max {
                return self.trading_days.contains(date);
            }
        }
        // 降级回退：周一至周五为交易日
        !matches!(date.weekday(), Weekday::Sat | Weekday::Sun)
    }

    /// 判断特定日期当晚是否有夜盘交易（20:00起）。
    ///
    /// 规则：
    /// 1. 今天自身必须是交易日；
    /// 2. 找到今天之后的下一个交易日。如果今天与下一个交易日之间包含了周一至周五的法定休市日，
    ///    说明今天为“节前最后一个交易日”，根据交易所规定，当晚不进行夜盘交易；
    /// 3. 如果只是正常的周末间隔（仅隔周六周日），周五晚间正常开启夜盘。
    pub fn has_night_session_tonight(&self, date: &NaiveDate) -> bool {
        if !self.is_trading_day(date) {
            return false;
        }

        // 寻找后继下一个交易日（最多向后看 15 天，覆盖国庆、春节最长长假）
        let mut next_trading_day = None;
        for offset in 1..=15 {
            if let Some(candidate) = date.checked_add_days(Days::new(offset)) {
                if self.is_trading_day(&candidate) {
                    next_trading_day = Some(candidate);
                    break;
                }
            }
        }

        let Some(next_day) = next_trading_day else {
            // 无法确定后继交易日时降级：周末当晚无夜盘，周一至周五有夜盘
            return !matches!(date.weekday(), Weekday::Sat | Weekday::Sun);
        };

        // 检查从 (date + 1) 到 (next_day - 1) 之间是否有工作日节假日休市
        let mut cur = *date + chrono::Duration::days(1);
        while cur < next_day {
            // 若该休市日落在周一至周五，说明存在法定工作日休市（中秋/国庆/春节等）
            if !matches!(cur.weekday(), Weekday::Sat | Weekday::Sun) {
                return false;
            }
            cur += chrono::Duration::days(1);
        }

        true
    }

    /// 导出为持久化文件结构。
    pub fn export_file(&self) -> TradingCalendarFile {
        let mut records = Vec::new();
        if let (Some(min), Some(max)) = (self.min_date, self.max_date) {
            let mut cur = min;
            while cur <= max {
                let d_str = cur.format("%Y-%m-%d").to_string();
                let trading = self.trading_days.contains(&cur);
                records.push(TradingCalendarRecord {
                    date: d_str,
                    trading,
                });
                cur += chrono::Duration::days(1);
            }
        }
        TradingCalendarFile {
            updated_at: chrono::Local::now().to_rfc3339(),
            records,
        }
    }

    pub fn save_to_file(&self, path: &Path) -> anyhow::Result<()> {
        let file = self.export_file();
        let json = serde_json::to_string_pretty(&file)?;
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        std::fs::write(path, json)?;
        Ok(())
    }

    pub fn load_from_file(&mut self, path: &Path) -> anyhow::Result<()> {
        if !path.exists() {
            return Ok(());
        }
        let text = std::fs::read_to_string(path)?;
        let file: TradingCalendarFile = serde_json::from_str(&text)?;
        self.update_records(&file.records);
        Ok(())
    }
}

static GLOBAL_CALENDAR: OnceLock<RwLock<TradingCalendar>> = OnceLock::new();

pub fn global_calendar() -> &'static RwLock<TradingCalendar> {
    GLOBAL_CALENDAR.get_or_init(|| RwLock::new(TradingCalendar::default()))
}

/// 更新全局日历（来自天勤 API）。
pub fn update_global_calendar(days: &[TradingCalendarDay]) {
    if let Ok(mut lock) = global_calendar().write() {
        lock.update_days(days);
    }
}

/// 从文件加载全局日历。
pub fn load_global_calendar_from_file(path: &Path) -> anyhow::Result<()> {
    if path.exists() {
        let text = std::fs::read_to_string(path)?;
        let file: TradingCalendarFile = serde_json::from_str(&text)?;
        if let Ok(mut lock) = global_calendar().write() {
            lock.update_records(&file.records);
        }
    }
    Ok(())
}

/// 将全局日历持久化保存到文件。
pub fn save_global_calendar_to_file(path: &Path) -> anyhow::Result<()> {
    if let Ok(lock) = global_calendar().read() {
        lock.save_to_file(path)?;
    }
    Ok(())
}

/// 查询某日期是否为交易日（白盘）。
pub fn is_trading_day(date: &NaiveDate) -> bool {
    global_calendar().read().map_or_else(
        |_| !matches!(date.weekday(), Weekday::Sat | Weekday::Sun),
        |cal| cal.is_trading_day(date),
    )
}

/// 查询某日期当晚是否有夜盘交易（20:00起）。
pub fn has_night_session_tonight(date: &NaiveDate) -> bool {
    global_calendar().read().map_or_else(
        |_| !matches!(date.weekday(), Weekday::Sat | Weekday::Sun),
        |cal| cal.has_night_session_tonight(date),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse_date(s: &str) -> NaiveDate {
        NaiveDate::parse_from_str(s, "%Y-%m-%d").unwrap()
    }

    #[test]
    fn test_fallback_without_data() {
        let cal = TradingCalendar::default();
        // 2026-09-24 是周四
        assert!(cal.is_trading_day(&parse_date("2026-09-24")));
        // 2026-09-26 是周六
        assert!(!cal.is_trading_day(&parse_date("2026-09-26")));
        // 2026-09-27 是周日
        assert!(!cal.is_trading_day(&parse_date("2026-09-27")));
    }

    #[test]
    fn test_mid_autumn_holiday_and_pre_holiday_night() {
        let mut cal = TradingCalendar::default();
        // 模拟 2026 年中秋节前后的日历
        let days = vec![
            TradingCalendarDay {
                date: "2026-09-21".into(),
                trading: true,
            }, // 周一
            TradingCalendarDay {
                date: "2026-09-22".into(),
                trading: true,
            }, // 周二
            TradingCalendarDay {
                date: "2026-09-23".into(),
                trading: true,
            }, // 周三
            TradingCalendarDay {
                date: "2026-09-24".into(),
                trading: true,
            }, // 周四（节前最后交易日）
            TradingCalendarDay {
                date: "2026-09-25".into(),
                trading: false,
            }, // 周五（中秋法定休市！）
            TradingCalendarDay {
                date: "2026-09-26".into(),
                trading: false,
            }, // 周六
            TradingCalendarDay {
                date: "2026-09-27".into(),
                trading: false,
            }, // 周日
            TradingCalendarDay {
                date: "2026-09-28".into(),
                trading: true,
            }, // 周一（节后首个交易日）
        ];
        cal.update_days(&days);

        // 1. 白盘判断
        assert!(cal.is_trading_day(&parse_date("2026-09-24")));
        assert!(!cal.is_trading_day(&parse_date("2026-09-25"))); // 中秋节当天准确识别为休市！
        assert!(!cal.is_trading_day(&parse_date("2026-09-26")));
        assert!(cal.is_trading_day(&parse_date("2026-09-28")));

        // 2. 夜盘判断
        // 周二、周三晚上正常有夜盘
        assert!(cal.has_night_session_tonight(&parse_date("2026-09-22")));
        assert!(cal.has_night_session_tonight(&parse_date("2026-09-23")));
        // 周四（9月24日，中秋前夕）当晚不进行夜盘交易！
        assert!(!cal.has_night_session_tonight(&parse_date("2026-09-24")));
        // 中秋节（9月25日）当晚不开夜盘
        assert!(!cal.has_night_session_tonight(&parse_date("2026-09-25")));
    }

    #[test]
    fn test_normal_friday_night_session() {
        let mut cal = TradingCalendar::default();
        // 正常周五与下周一之间只有周末，没有节假日
        let days = vec![
            TradingCalendarDay {
                date: "2026-09-18".into(),
                trading: true,
            }, // 正常周五
            TradingCalendarDay {
                date: "2026-09-19".into(),
                trading: false,
            }, // 周六
            TradingCalendarDay {
                date: "2026-09-20".into(),
                trading: false,
            }, // 周日
            TradingCalendarDay {
                date: "2026-09-21".into(),
                trading: true,
            }, // 周一
        ];
        cal.update_days(&days);

        // 正常周五晚上开启夜盘
        assert!(cal.has_night_session_tonight(&parse_date("2026-09-18")));
    }
}
