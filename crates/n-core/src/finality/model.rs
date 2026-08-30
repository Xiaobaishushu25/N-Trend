//! Data models for the Finality observation and shadow validation system.

use chrono::NaiveDateTime;
use serde::{Deserialize, Serialize};

/// 默认观测哨兵品种集合（涵盖交易所、类别、活跃度、不同夜盘时间、历史异常品种）。
pub const DEFAULT_SENTINELS: [&str; 7] = [
    "CJ0", // 郑商所红枣，日盘无夜盘，历史曾发生 11:30 T+65s 异常
    "JD0", // 大商所鸡蛋，日盘无夜盘，农产品
    "RB0", // 上期所螺纹钢，夜盘 23:00 收盘，极高活跃度黑色金属
    "MA0", // 郑商所甲醇，夜盘 23:00 收盘，活跃化工
    "PB0", // 上期所铅，夜盘 01:00 收盘，有色金属（历史曾有 11:30 临时值）
    "AU0", // 上期所黄金，夜盘 02:30 收盘，贵金属
    "SC0", // 能源中心原油，夜盘 02:30 收盘，原油能源
];

/// K 线严格指纹：采用规范化数值字符串比对，完全避免浮点精度引起假变化。
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct BarFingerprint {
    pub bar_ts: String,
    pub open: String,
    pub high: String,
    pub low: String,
    pub close: String,
    pub volume: String,
    pub hold: String,
}

impl BarFingerprint {
    pub fn new(
        bar_ts: impl Into<String>,
        open: impl AsRef<str>,
        high: impl AsRef<str>,
        low: impl AsRef<str>,
        close: impl AsRef<str>,
        volume: impl AsRef<str>,
        hold: impl AsRef<str>,
    ) -> Self {
        Self {
            bar_ts: bar_ts.into(),
            open: normalize_num_str(open.as_ref()),
            high: normalize_num_str(high.as_ref()),
            low: normalize_num_str(low.as_ref()),
            close: normalize_num_str(close.as_ref()),
            volume: normalize_num_str(volume.as_ref()),
            hold: normalize_num_str(hold.as_ref()),
        }
    }

    /// 便于数据库记录与日志打印的易读签名。
    pub fn signature(&self) -> String {
        format!(
            "O:{} H:{} L:{} C:{} V:{} P:{}",
            self.open, self.high, self.low, self.close, self.volume, self.hold
        )
    }
}

impl From<&crate::fetch::kline::Kline> for BarFingerprint {
    fn from(k: &crate::fetch::kline::Kline) -> Self {
        Self::new(
            &k.datetime,
            format_f64(k.open),
            format_f64(k.high),
            format_f64(k.low),
            format_f64(k.close),
            format_f64(k.volume),
            format_f64(k.hold),
        )
    }
}

fn format_f64(v: f64) -> String {
    if v.fract() == 0.0 && v.abs() < 1e15 {
        format!("{:.0}", v)
    } else {
        let s = format!("{:.4}", v);
        let trimmed = s.trim_end_matches('0').trim_end_matches('.');
        trimmed.to_string()
    }
}

/// 归一化数值字符串：去除小数末尾无意义的 0，例如 "8260.00" -> "8260"，"8260.50" -> "8260.5"。
pub fn normalize_num_str(raw: &str) -> String {
    let s = raw.trim();
    if s.is_empty() {
        return "0".to_string();
    }
    if let Some((int_part, frac_part)) = s.split_once('.') {
        let trimmed_frac = frac_part.trim_end_matches('0');
        if trimmed_frac.is_empty() {
            if int_part.is_empty() || int_part == "-0" || int_part == "+" {
                "0".to_string()
            } else {
                int_part.to_string()
            }
        } else {
            let int_clean = if int_part.is_empty() { "0" } else { int_part };
            format!("{}.{}", int_clean, trimmed_frac)
        }
    } else {
        s.to_string()
    }
}

/// 交易时段类型分类。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum SessionType {
    Normal,
    Close1015,
    Close1130,
    Close1500,
    NightClose2300,
    NightClose0100,
    NightClose0230,
    NightCloseOther,
}

impl SessionType {
    pub fn as_str(&self) -> &'static str {
        match self {
            SessionType::Normal => "normal",
            SessionType::Close1015 => "10:15",
            SessionType::Close1130 => "11:30",
            SessionType::Close1500 => "15:00",
            SessionType::NightClose2300 => "night_2300",
            SessionType::NightClose0100 => "night_0100",
            SessionType::NightClose0230 => "night_0230",
            SessionType::NightCloseOther => "night_other",
        }
    }

    pub fn from_str_name(s: &str) -> Self {
        match s {
            "normal" => SessionType::Normal,
            "10:15" => SessionType::Close1015,
            "11:30" => SessionType::Close1130,
            "15:00" => SessionType::Close1500,
            "night_2300" => SessionType::NightClose2300,
            "night_0100" => SessionType::NightClose0100,
            "night_0230" => SessionType::NightClose0230,
            _ => SessionType::NightCloseOther,
        }
    }

    /// 根据品种与 K 线时间戳分类。
    pub fn classify(_symbol: &str, bar_ts: &str) -> Self {
        if let Ok(dt) = NaiveDateTime::parse_from_str(bar_ts, "%Y-%m-%d %H:%M:%S") {
            use chrono::Timelike;
            let (h, m) = (dt.hour(), dt.minute());
            match (h, m) {
                (10, 15) => SessionType::Close1015,
                (11, 30) => SessionType::Close1130,
                (15, 0) => SessionType::Close1500,
                (23, 0) | (23, 30) => SessionType::NightClose2300,
                (1, 0) => SessionType::NightClose0100,
                (2, 30) => SessionType::NightClose0230,
                _ => SessionType::Normal,
            }
        } else {
            SessionType::Normal
        }
    }
}

/// 单次探测记录（存储于 bar_observations）。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ObservationRecord {
    pub id: Option<i64>,
    pub symbol: String,
    pub bar_ts: String,
    pub observed_at: String,
    pub elapsed_ms: i64,
    pub probe_index: i32,
    pub open: f64,
    pub high: f64,
    pub low: f64,
    pub close: f64,
    pub volume: f64,
    pub hold: f64,
    pub fingerprint: String,
    pub session_type: String,
    pub is_revision: bool,
    pub raw_response: Option<String>,
}

/// 单根 Bar 的 Finality 试验汇总（存储于 bar_finality_trials）。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FinalityTrial {
    pub id: Option<i64>,
    pub symbol: String,
    pub bar_ts: String,
    pub session_type: String,
    pub first_seen_at: Option<String>,
    pub first_seen_delay_ms: Option<i64>,
    pub candidate_final_at: Option<String>,
    pub candidate_delay_ms: Option<i64>,
    pub revision_count: i32,
    pub last_revision_at: Option<String>,
    pub last_revision_delay_ms: Option<i64>,
    pub false_final: bool,
    pub candidate_fingerprint: Option<String>,
    pub final_fingerprint: Option<String>,
    pub probe_count: i32,
    pub completed: bool,
    pub created_at: String,
    pub updated_at: String,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_normalize_num_str() {
        assert_eq!(normalize_num_str("8260.00"), "8260");
        assert_eq!(normalize_num_str("8260.500"), "8260.5");
        assert_eq!(normalize_num_str("8260"), "8260");
        assert_eq!(normalize_num_str("0.00"), "0");
        assert_eq!(normalize_num_str(""), "0");
    }

    #[test]
    fn test_fingerprint_equality() {
        let fp1 = BarFingerprint::new("2026-08-28 10:45:00", "8260.00", "8265.0", "8250", "8255.000", "100.0", "5000");
        let fp2 = BarFingerprint::new("2026-08-28 10:45:00", "8260", "8265", "8250.00", "8255", "100", "5000.0");
        assert_eq!(fp1, fp2);
        assert_eq!(fp1.signature(), fp2.signature());
    }

    #[test]
    fn test_session_type_classify() {
        assert_eq!(SessionType::classify("RB0", "2026-08-28 10:15:00"), SessionType::Close1015);
        assert_eq!(SessionType::classify("CJ0", "2026-08-28 11:30:00"), SessionType::Close1130);
        assert_eq!(SessionType::classify("CJ0", "2026-08-28 15:00:00"), SessionType::Close1500);
        assert_eq!(SessionType::classify("RB0", "2026-08-28 23:00:00"), SessionType::NightClose2300);
        assert_eq!(SessionType::classify("PB0", "2026-08-28 01:00:00"), SessionType::NightClose0100);
        assert_eq!(SessionType::classify("AU0", "2026-08-28 02:30:00"), SessionType::NightClose0230);
        assert_eq!(SessionType::classify("RB0", "2026-08-28 10:45:00"), SessionType::Normal);
    }
}
