//! Trading session specifications for domestic futures symbols.

use chrono::{DateTime, Datelike, Local, NaiveDate, NaiveDateTime, Timelike, Weekday};

/// 夜盘时段类型。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum NightSessionType {
    /// 无夜盘（农产品、部分化工、金融期货等）
    None,
    /// 23:00 收盘（黑色系、大部分能化：螺纹、铁矿、焦煤、焦炭、甲醇、PTA等）
    Close2300,
    /// 23:30 收盘（纯碱、玻璃等）
    Close2330,
    /// 01:00 收盘（有色金属：铜、铝、锌、铅、镍、锡等）
    Close0100,
    /// 02:30 收盘（贵金属、原油：黄金、白银、原油等）
    Close0230,
}

/// 单品种交易时段规约。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TradingSessionSpec {
    pub symbol: String,
    pub night_type: NightSessionType,
}

impl TradingSessionSpec {
    pub fn new(symbol: &str, night_type: NightSessionType) -> Self {
        Self {
            symbol: symbol.to_string(),
            night_type,
        }
    }

    /// 该品种所有法定收盘时点（时, 分）。
    /// 日盘固定：(10, 15), (11, 30), (15, 0)。
    /// 夜盘依据品种各异。
    pub fn session_close_times(&self) -> Vec<(u32, u32)> {
        let mut closes = vec![(10, 15), (11, 30), (15, 0)];
        match self.night_type {
            NightSessionType::None => {}
            NightSessionType::Close2300 => closes.push((23, 0)),
            NightSessionType::Close2330 => closes.push((23, 30)),
            NightSessionType::Close0100 => closes.push((1, 0)),
            NightSessionType::Close0230 => closes.push((2, 30)),
        }
        closes
    }

    /// 判断给定时间（时, 分）是否为该品种的某个收盘时刻。
    pub fn is_session_close(&self, hour: u32, minute: u32) -> bool {
        self.session_close_times().contains(&(hour, minute))
    }

    /// 判断当前时刻是否处于该品种的有效交易时间窗口内（含收盘边界分钟）。
    pub fn is_in_trading_time(&self, dt: &DateTime<Local>) -> bool {
        let weekday = dt.weekday();
        let (h, m) = (dt.hour(), dt.minute());
        let t_mins = h * 60 + m; // 0..1439

        // 日盘时段（周一至周五）：
        // 09:00 - 10:15 (含 10:15)  => [540, 615]
        // 10:30 - 11:30 (含 11:30)  => [630, 690]
        // 13:30 - 15:00 (含 15:00)  => [810, 900]
        let is_day_window = (540..=615).contains(&t_mins)
            || (630..=690).contains(&t_mins)
            || (810..=900).contains(&t_mins);

        if is_day_window {
            return matches!(
                weekday,
                Weekday::Mon | Weekday::Tue | Weekday::Wed | Weekday::Thu | Weekday::Fri
            );
        }

        // 无夜盘品种
        if self.night_type == NightSessionType::None {
            return false;
        }

        // 夜盘 21:00 - 23:00 => [1260, 1380]（周一至周五晚）
        if (1260..=1380).contains(&t_mins) {
            return matches!(
                weekday,
                Weekday::Mon | Weekday::Tue | Weekday::Wed | Weekday::Thu | Weekday::Fri
            );
        }

        // 23:01 - 23:30 => [1381, 1410]
        if (1381..=1410).contains(&t_mins) {
            if matches!(
                self.night_type,
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

        // 23:31 - 24:00 => [1411, 1439]
        if (1411..=1439).contains(&t_mins) {
            if matches!(
                self.night_type,
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
        // 00:00 - 01:00 => [0, 60]
        if (0..=60).contains(&t_mins) {
            if matches!(
                self.night_type,
                NightSessionType::Close0100 | NightSessionType::Close0230
            ) {
                return matches!(
                    weekday,
                    Weekday::Tue | Weekday::Wed | Weekday::Thu | Weekday::Fri | Weekday::Sat
                );
            }
            return false;
        }

        // 01:01 - 02:30 => [61, 150]
        if (61..=150).contains(&t_mins) {
            if matches!(self.night_type, NightSessionType::Close0230) {
                return matches!(
                    weekday,
                    Weekday::Tue | Weekday::Wed | Weekday::Thu | Weekday::Fri | Weekday::Sat
                );
            }
            return false;
        }

        false
    }

    /// 判断当前时刻是否处于收盘后宽限期内（用于收盘后 5 分钟内执行最后一次结算、补拉和扫描）。
    pub fn is_in_close_grace(&self, dt: &DateTime<Local>, grace_secs: u64) -> bool {
        let now = dt.naive_local();
        let date = now.date();
        let closes = self.session_close_times();

        for (h, m) in closes {
            if let Some(close_time) = date.and_hms_opt(h, m, 0) {
                let diff = (now - close_time).num_seconds();
                if (0..=grace_secs as i64).contains(&diff) {
                    return true;
                }
            }
        }
        false
    }

    /// 综合判断该品种当前是否处于活跃刷新状态（在交易时段中，或者处于收盘宽限期内）。
    pub fn is_active_for_refresh(&self, dt: &DateTime<Local>) -> bool {
        self.is_in_trading_time(dt) || self.is_in_close_grace(dt, 300)
    }

    /// 对应的扫描结算等待秒数（收盘时点 80 秒，普通时点 40 秒）。
    pub fn scan_settle_secs(&self, hour: u32, minute: u32) -> u32 {
        if self.is_session_close(hour, minute) {
            80
        } else {
            40
        }
    }

    /// 归属交易日：夜盘（20:00 之后及跨日凌晨）归入下一个工作日/交易日；周五夜盘归入周一。
    pub fn trading_day(&self, dt: &NaiveDateTime) -> NaiveDate {
        let date = dt.date();
        let hour = dt.hour();
        if hour >= 20 {
            // 夜盘开始，归入下一交易日
            match date.weekday() {
                Weekday::Fri => date + chrono::Duration::days(3),
                Weekday::Sat => date + chrono::Duration::days(2),
                _ => date + chrono::Duration::days(1),
            }
        } else if hour < 4 && self.night_type != NightSessionType::None {
            // 凌晨跨日的夜盘延伸（周二至周六凌晨），实际属于当天的早盘交易日（周六凌晨属于周一交易日）
            match date.weekday() {
                Weekday::Sat => date + chrono::Duration::days(2),
                _ => date,
            }
        } else {
            date
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    fn local_dt(y: i32, m: u32, d: u32, h: u32, min: u32) -> DateTime<Local> {
        Local.with_ymd_and_hms(y, m, d, h, min, 0).unwrap()
    }

    #[test]
    fn test_spec_session_close_moments() {
        // RB0: 23:00 收盘
        let rb = TradingSessionSpec::new("RB0", NightSessionType::Close2300);
        assert!(rb.is_session_close(10, 15));
        assert!(rb.is_session_close(11, 30));
        assert!(rb.is_session_close(15, 0));
        assert!(rb.is_session_close(23, 0));
        assert!(!rb.is_session_close(23, 30));

        // CU0: 01:00 收盘
        let cu = TradingSessionSpec::new("CU0", NightSessionType::Close0100);
        assert!(cu.is_session_close(1, 0));
        assert!(!cu.is_session_close(23, 0));

        // CJ0: 无夜盘
        let cj = TradingSessionSpec::new("CJ0", NightSessionType::None);
        assert!(cj.is_session_close(15, 0));
        assert!(!cj.is_session_close(23, 0));
    }

    #[test]
    fn test_spec_is_in_trading_time() {
        let rb = TradingSessionSpec::new("RB0", NightSessionType::Close2300);
        let cj = TradingSessionSpec::new("CJ0", NightSessionType::None);

        // 周一 09:30 (日盘)
        let t1 = local_dt(2026, 8, 24, 9, 30);
        assert!(rb.is_in_trading_time(&t1));
        assert!(cj.is_in_trading_time(&t1));

        // 周一 21:30 (夜盘)
        let t2 = local_dt(2026, 8, 24, 21, 30);
        assert!(rb.is_in_trading_time(&t2));
        assert!(!cj.is_in_trading_time(&t2)); // 无夜盘品种不处于交易时段

        // 周一 23:10 (RB0 已收盘)
        let t3 = local_dt(2026, 8, 24, 23, 10);
        assert!(!rb.is_in_trading_time(&t3));
    }
}
