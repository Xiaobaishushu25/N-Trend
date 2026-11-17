//! Broker date labels are not necessarily calendar dates. Keep the raw fields
//! and resolve a Shanghai wall-clock instant once, before it reaches strategy state.
use crate::session::{spec::NightSessionType, SessionCalendar};
use chrono::{Days, FixedOffset, NaiveDate, NaiveTime, Timelike};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct TradeTimeEvidence {
    pub raw_date: String,
    pub raw_time: String,
    pub trading_day: String,
    pub timestamp: Option<String>,
    pub source: String,
}

fn date(value: &str) -> Option<NaiveDate> {
    NaiveDate::parse_from_str(value, "%Y%m%d")
        .or_else(|_| NaiveDate::parse_from_str(value, "%Y-%m-%d"))
        .ok()
}

pub fn resolve(
    instrument: &str,
    raw_date: &str,
    raw_time: &str,
    trading_day: &str,
    submitted_at: Option<i64>,
) -> TradeTimeEvidence {
    resolve_with_calendar(
        instrument,
        raw_date,
        raw_time,
        trading_day,
        submitted_at,
        &crate::session::trading_calendar::global_calendar()
            .read()
            .unwrap_or_else(|p| p.into_inner()),
    )
}

pub fn resolve_with_calendar(
    instrument: &str,
    raw_date: &str,
    raw_time: &str,
    trading_day: &str,
    submitted_at: Option<i64>,
    calendar: &crate::session::trading_calendar::TradingCalendar,
) -> TradeTimeEvidence {
    let mut result = TradeTimeEvidence {
        raw_date: raw_date.into(),
        raw_time: raw_time.into(),
        trading_day: trading_day.into(),
        timestamp: None,
        source: "unresolved".into(),
    };
    let Ok(time) = NaiveTime::parse_from_str(raw_time, "%H:%M:%S") else {
        return result;
    };
    // Match the exchange clock to the actual order submission, including midnight
    // and one-second clock skew. A warning or position entry is never an exit anchor.
    if let Some(anchor) = submitted_at.and_then(|t| chrono::DateTime::from_timestamp(t, 0)) {
        let anchor = anchor
            .with_timezone(&FixedOffset::east_opt(28800).unwrap())
            .naive_local();
        let closest = [-1, 0, 1]
            .into_iter()
            .map(|offset| (anchor.date() + chrono::Duration::days(offset)).and_time(time))
            .min_by_key(|candidate| (*candidate - anchor).num_seconds().abs())
            .unwrap();
        if (closest - anchor).num_seconds().abs() <= 900 {
            result.timestamp = Some(closest.format("%Y-%m-%d %H:%M:%S").to_string());
            result.source = "order_submission".into();
            return result;
        }
    }
    if let Some(day) = date(trading_day) {
        let spec = SessionCalendar::get(instrument);
        let natural = if time.hour() >= 20 || time.hour() < 4 {
            if spec.night_type == NightSessionType::None {
                None
            } else {
                (1..=20)
                    .filter_map(|n| day.checked_sub_days(Days::new(n)))
                    .find(|d| calendar.is_trading_day(d))
                    .filter(|d| calendar.has_night_session_tonight(d))
                    .map(|evening| {
                        if time.hour() < 4 {
                            evening + Days::new(1)
                        } else {
                            evening
                        }
                    })
            }
        } else {
            Some(day)
        };
        if let Some(natural) = natural {
            let candidate = natural.and_time(time);
            // Include the evening opening auction and the exact session close.
            if (time.hour() >= 4 && time.hour() < 20 && calendar.is_trading_day(&natural))
                || (time.hour() >= 20 && calendar.has_night_session_tonight(&natural))
                || (time.hour() < 4
                    && calendar.has_night_session_tonight(&(natural - Days::new(1))))
            {
                result.timestamp = Some(candidate.format("%Y-%m-%d %H:%M:%S").to_string());
                result.source = "trading_session".into();
                return result;
            }
        }
    }
    // Older paper/SimNow records without TradingDay already contain natural dates.
    // Do not subtract a day merely because the representation is compact.
    if trading_day.is_empty() {
        if let Some(day) = date(raw_date) {
            result.timestamp = Some(day.and_time(time).format("%Y-%m-%d %H:%M:%S").to_string());
            result.source = "broker_calendar_date".into();
        }
    }
    result
}

pub fn split_timestamp(value: &str) -> (&str, &str) {
    value
        .split_once(' ')
        .or_else(|| value.split_once('T'))
        .unwrap_or((value, ""))
}

#[cfg(test)]
mod tests {
    use super::*;
    fn anchor(s: &str) -> Option<i64> {
        Some(crate::trader::candidates::parse_time(s).unwrap())
    }
    #[test]
    fn real_and_simnow_night_dates_resolve_to_the_same_instant() {
        for raw in ["20261116", "20261117"] {
            let r = resolve(
                "pp2701",
                raw,
                "21:45:59",
                "20261117",
                anchor("2026-11-16 21:45:59"),
            );
            assert_eq!(r.timestamp.as_deref(), Some("2026-11-16 21:45:59"));
            assert_eq!(r.raw_date, raw);
        }
    }
    #[test]
    fn trading_session_handles_weekends_and_midnight_without_entry_date_guessing() {
        assert_eq!(
            resolve("pp2701", "20261123", "21:00:01", "20261123", None)
                .timestamp
                .as_deref(),
            Some("2026-11-20 21:00:01")
        );
        assert_eq!(
            resolve("au2702", "20261123", "00:30:00", "20261123", None)
                .timestamp
                .as_deref(),
            Some("2026-11-21 00:30:00")
        );
        assert_eq!(
            resolve(
                "pp2701",
                "20261118",
                "21:05:00",
                "20261118",
                anchor("2026-11-17 21:04:59")
            )
            .timestamp
            .as_deref(),
            Some("2026-11-17 21:05:00")
        );
    }
    #[test]
    fn correct_natural_dates_and_midnight_clock_skew_are_preserved() {
        assert_eq!(
            resolve("pp2701", "20261116", "21:45:59", "", None)
                .timestamp
                .as_deref(),
            Some("2026-11-16 21:45:59")
        );
        assert_eq!(
            resolve(
                "au2702",
                "20261117",
                "23:59:59",
                "20261117",
                anchor("2026-11-17 00:00:00")
            )
            .timestamp
            .as_deref(),
            Some("2026-11-16 23:59:59")
        );
        assert!(resolve("pp2701", "bad", "bad", "20261117", None)
            .timestamp
            .is_none());
    }
    #[test]
    fn holiday_calendar_does_not_invent_a_preholiday_night_session() {
        use crate::session::trading_calendar::{TradingCalendar, TradingCalendarRecord};
        let mut calendar = TradingCalendar::new();
        calendar.update_records(&[
            TradingCalendarRecord {
                date: "2026-09-30".into(),
                trading: true,
            },
            TradingCalendarRecord {
                date: "2026-10-01".into(),
                trading: false,
            },
            TradingCalendarRecord {
                date: "2026-10-08".into(),
                trading: true,
            },
        ]);
        assert!(resolve_with_calendar(
            "pp2701", "20261008", "21:00:00", "20261008", None, &calendar
        )
        .timestamp
        .is_none());
        // The actual order submission still identifies a legitimate Oct 8 evening
        // fill when an old bridge did not provide the new trading-day label.
        assert_eq!(
            resolve_with_calendar(
                "pp2701",
                "20261008",
                "21:00:00",
                "",
                anchor("2026-10-08 21:00:00"),
                &calendar
            )
            .timestamp
            .as_deref(),
            Some("2026-10-08 21:00:00")
        );
    }
}
