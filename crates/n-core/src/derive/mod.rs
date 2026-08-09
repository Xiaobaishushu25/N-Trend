//! K-line aggregation: derive higher timeframes from the 5m base series.
//!
//! 规则：
//! - 分钟级别按日历桶对齐（floor 到目标分钟数），桶的 ts 记为桶末（与源站语义一致）；
//! - 相邻原始 bar 间隔超过 5 分钟视为会话断裂，即使落入同一日历桶也另起一桶；
//! - 会话首尾不足整桶的 bar 保留为部分桶；
//! - 日线按交易日分组，夜盘（20:00 后）计入次日。

use chrono::{Days, NaiveDate, NaiveDateTime, Timelike};

use crate::fetch::kline::Kline;

pub mod rollover;

pub const BASE_TIMEFRAME: &str = "5m";
pub const BASE_STEP_MINUTES: i64 = 5;
const NIGHT_THRESHOLD_HOUR: u32 = 20;
const TS_FORMAT: &str = "%Y-%m-%d %H:%M:%S";

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Timeframe {
    M5,
    M15,
    M30,
    M60,
    M120,
    M240,
    Day,
}

impl Timeframe {
    pub fn parse(s: &str) -> Option<Self> {
        match s {
            "5m" => Some(Self::M5),
            "15m" => Some(Self::M15),
            "30m" => Some(Self::M30),
            "60m" => Some(Self::M60),
            "120m" => Some(Self::M120),
            "240m" => Some(Self::M240),
            "1d" | "day" => Some(Self::Day),
            _ => None,
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::M5 => "5m",
            Self::M15 => "15m",
            Self::M30 => "30m",
            Self::M60 => "60m",
            Self::M120 => "120m",
            Self::M240 => "240m",
            Self::Day => "1d",
        }
    }

    pub fn minutes(self) -> Option<i64> {
        match self {
            Self::M5 => Some(5),
            Self::M15 => Some(15),
            Self::M30 => Some(30),
            Self::M60 => Some(60),
            Self::M120 => Some(120),
            Self::M240 => Some(240),
            Self::Day => None,
        }
    }

    pub fn supported() -> Vec<Self> {
        vec![
            Self::M5,
            Self::M15,
            Self::M30,
            Self::M60,
            Self::M120,
            Self::M240,
            Self::Day,
        ]
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum BucketKey {
    Minute(NaiveDateTime),
    Day(NaiveDate),
}

/// 把升序 5m 序列聚合为目标级别（输入需已按时间排序）。
pub fn aggregate(bars: &[Kline], target: Timeframe) -> Vec<Kline> {
    if bars.is_empty() {
        return Vec::new();
    }
    if target == Timeframe::M5 {
        return bars.to_vec();
    }

    let mut out: Vec<Kline> = Vec::new();
    let mut cur: Option<Bucket> = None;
    let mut prev_dt: Option<NaiveDateTime> = None;

    for bar in bars {
        let Some(dt) = parse_ts(&bar.datetime) else {
            continue;
        };
        let key = bucket_key(dt, target);
        let session_break = target.minutes().is_some()
            && prev_dt
                .map(|prev| (dt - prev).num_minutes() > BASE_STEP_MINUTES)
                .unwrap_or(false);
        let bucket_break = cur.as_ref().map(|b| b.key != key).unwrap_or(true);

        if bucket_break || session_break {
            if let Some(bucket) = cur.take() {
                out.push(bucket.finish(target));
            }
            cur = Some(Bucket::new(key, bar));
        } else if let Some(bucket) = cur.as_mut() {
            bucket.add(bar);
        }
        prev_dt = Some(dt);
    }
    if let Some(bucket) = cur.take() {
        out.push(bucket.finish(target));
    }
    out
}

struct Bucket {
    key: BucketKey,
    open: f64,
    high: f64,
    low: f64,
    close: f64,
    volume: f64,
    hold: f64,
}

impl Bucket {
    fn new(key: BucketKey, bar: &Kline) -> Self {
        Self {
            key,
            open: bar.open,
            high: bar.high,
            low: bar.low,
            close: bar.close,
            volume: bar.volume,
            hold: bar.hold,
        }
    }

    fn add(&mut self, bar: &Kline) {
        self.high = self.high.max(bar.high);
        self.low = self.low.min(bar.low);
        self.close = bar.close;
        self.volume += bar.volume;
        self.hold = bar.hold;
    }

    fn finish(self, target: Timeframe) -> Kline {
        let datetime = bucket_ts(self.key, target);
        Kline {
            datetime,
            open: self.open,
            high: self.high,
            low: self.low,
            close: self.close,
            volume: self.volume,
            hold: self.hold,
        }
    }
}

fn bucket_key(dt: NaiveDateTime, target: Timeframe) -> BucketKey {
    match target.minutes() {
        Some(minutes) => {
            let day_start = dt.date().and_hms_opt(0, 0, 0).unwrap();
            let elapsed_min = (dt - day_start).num_minutes();
            // 源站 5m bar 时间戳为桶末语义：bar 归入“结束时间 >= 自身时间戳”的桶
            let end_min = (((elapsed_min + minutes - 1) / minutes).max(1)) * minutes;
            BucketKey::Minute(day_start + chrono::Duration::minutes(end_min - minutes))
        }
        None => BucketKey::Day(trading_day(dt)),
    }
}

fn bucket_ts(key: BucketKey, target: Timeframe) -> String {
    match key {
        BucketKey::Minute(start) => {
            // 桶末时间戳：start + 目标分钟数；会话首尾的部分桶也按该桶应有的结束时间标记
            let end = start + chrono::Duration::minutes(target.minutes().unwrap_or(5));
            end.format(TS_FORMAT).to_string()
        }
        BucketKey::Day(date) => date
            .and_hms_opt(15, 0, 0)
            .unwrap_or_default()
            .format(TS_FORMAT)
            .to_string(),
    }
}

/// 交易日：20:00 之后的夜盘归入下一交易日。
pub fn trading_day(dt: NaiveDateTime) -> NaiveDate {
    if dt.hour() >= NIGHT_THRESHOLD_HOUR {
        dt.date() + Days::new(1)
    } else {
        dt.date()
    }
}

fn parse_ts(s: &str) -> Option<NaiveDateTime> {
    NaiveDateTime::parse_from_str(s, TS_FORMAT).ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn bar(
        datetime: &str,
        open: f64,
        high: f64,
        low: f64,
        close: f64,
        volume: f64,
        hold: f64,
    ) -> Kline {
        Kline {
            datetime: datetime.to_string(),
            open,
            high,
            low,
            close,
            volume,
            hold,
        }
    }

    fn m15_bars() -> Vec<Kline> {
        // 09:05~09:30，前两桶各3根bar，最后一个桶只有一根bar（部分桶）
        vec![
            bar(
                "2026-08-03 09:05:00",
                100.0,
                105.0,
                99.0,
                104.0,
                100.0,
                1000.0,
            ),
            bar(
                "2026-08-03 09:10:00",
                104.0,
                106.0,
                103.0,
                105.0,
                200.0,
                1100.0,
            ),
            bar(
                "2026-08-03 09:15:00",
                105.0,
                108.0,
                104.0,
                107.0,
                150.0,
                1200.0,
            ),
            bar(
                "2026-08-03 09:20:00",
                107.0,
                107.5,
                106.0,
                106.5,
                80.0,
                1250.0,
            ),
            bar(
                "2026-08-03 09:25:00",
                106.5,
                109.0,
                106.0,
                108.5,
                120.0,
                1300.0,
            ),
            bar(
                "2026-08-03 09:30:00",
                108.5,
                110.0,
                108.0,
                109.5,
                90.0,
                1350.0,
            ),
        ]
    }

    #[test]
    fn minute_bucket_alignment_and_ohlcv() {
        let out = aggregate(&m15_bars(), Timeframe::M15);
        assert_eq!(out.len(), 2);
        assert_eq!(out[0].datetime, "2026-08-03 09:15:00");
        assert_eq!(out[0].open, 100.0);
        assert_eq!(out[0].high, 108.0);
        assert_eq!(out[0].low, 99.0);
        assert_eq!(out[0].close, 107.0);
        assert_eq!(out[0].volume, 450.0);
        assert_eq!(out[0].hold, 1200.0);
        assert_eq!(out[1].datetime, "2026-08-03 09:30:00");
        assert_eq!(out[1].open, 107.0);
        assert_eq!(out[1].close, 109.5);
        assert_eq!(out[1].volume, 290.0);
    }

    #[test]
    fn session_gap_splits_same_calendar_bucket() {
        // 09:10 与 09:55 相差45分钟，floor(60m) 相同，但必须按会话断裂拆成两桶
        let bars = vec![
            bar(
                "2026-08-03 09:10:00",
                100.0,
                102.0,
                99.0,
                101.0,
                100.0,
                1000.0,
            ),
            bar(
                "2026-08-03 09:55:00",
                101.0,
                103.0,
                100.0,
                102.0,
                200.0,
                1100.0,
            ),
        ];
        let out = aggregate(&bars, Timeframe::M60);
        assert_eq!(out.len(), 2);
        assert_eq!(out[0].datetime, "2026-08-03 10:00:00");
        assert_eq!(out[1].datetime, "2026-08-03 10:00:00");
        assert_eq!(out[0].open, 100.0);
        assert_eq!(out[1].open, 101.0);
    }

    #[test]
    fn daily_night_session_counts_to_next_trading_day() {
        assert_eq!(
            trading_day(NaiveDateTime::parse_from_str("2026-08-03 21:05:00", TS_FORMAT).unwrap()),
            NaiveDate::from_ymd_opt(2026, 8, 4).unwrap()
        );
        assert_eq!(
            trading_day(NaiveDateTime::parse_from_str("2026-08-03 15:00:00", TS_FORMAT).unwrap()),
            NaiveDate::from_ymd_opt(2026, 8, 3).unwrap()
        );
    }

    #[test]
    fn daily_bucket_merges_night_into_next_day() {
        // 周一(8/3)夜盘 21:05 + 周二(8/4)早盘 09:05 → 同属 2026-08-04 的日线
        let bars = vec![
            bar(
                "2026-08-03 21:05:00",
                3000.0,
                3020.0,
                2990.0,
                3010.0,
                500.0,
                10000.0,
            ),
            bar(
                "2026-08-04 09:05:00",
                3010.0,
                3050.0,
                3005.0,
                3040.0,
                700.0,
                10500.0,
            ),
        ];
        let out = aggregate(&bars, Timeframe::Day);
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].datetime, "2026-08-04 15:00:00");
        assert_eq!(out[0].open, 3000.0);
        assert_eq!(out[0].high, 3050.0);
        assert_eq!(out[0].low, 2990.0);
        assert_eq!(out[0].close, 3040.0);
        assert_eq!(out[0].volume, 1200.0);
        assert_eq!(out[0].hold, 10500.0);
    }

    #[test]
    fn empty_and_m5_passthrough() {
        assert!(aggregate(&[], Timeframe::M15).is_empty());
        let bars = m15_bars();
        assert_eq!(aggregate(&bars, Timeframe::M5).len(), bars.len());
    }
}
