//! Quotes update current chart candles in memory; 5m serials seed and calibrate OHLCV.
//! These display snapshots never become persisted candles or strategy inputs.
use std::collections::{BTreeMap, HashMap};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use chrono::{Local, NaiveDateTime, Timelike};
use tokio::sync::Notify;

use super::{KlineDto, MarketSnapshot};
use crate::derive::{aggregate_endpoints, current_bucket_end_ts, trading_day, Timeframe};
use crate::fetch::kline::Kline;
use crate::session::SessionCalendar;

const FORMAT: &str = "%Y-%m-%d %H:%M:%S";
pub const MAX_AGE: Duration = Duration::from_secs(10);
const REFRESH_INTERVAL: Duration = Duration::from_secs(5);
const CALIBRATION_MAX_AGE: Duration = Duration::from_secs(30);
const INTEREST_TTL: Duration = Duration::from_secs(30);

struct Interest {
    seen: Instant,
    attempted: Option<Instant>,
    rebase_bucket: Option<String>,
}

struct Published {
    received: Instant,
    calibrated: Instant,
    base_end: String,
    bars: HashMap<String, KlineDto>,
    quote: Option<QuoteUpdate>,
}

#[derive(Clone)]
struct QuoteUpdate {
    at: Instant,
    latest: f64,
    high: f64,
    low: f64,
}

fn apply_quote(bars: &mut HashMap<String, KlineDto>, quote: &QuoteUpdate) {
    for bar in bars.values_mut() {
        bar.high = bar.high.max(quote.high);
        bar.low = bar.low.min(quote.low);
        bar.close = quote.latest;
        // The existing open and calibrated volume/hold are preserved, never fabricated.
        bar.source = "live_quote_calibrating".into();
    }
}

#[derive(Default)]
pub struct LiveChartCache {
    interests: Mutex<HashMap<String, Interest>>,
    published: Mutex<HashMap<String, Published>>,
    pub wake: Notify,
}

impl LiveChartCache {
    pub fn request(&self, symbol: &str) {
        let mut interests = self.interests.lock().unwrap();
        let is_new = !interests.contains_key(symbol);
        interests
            .entry(symbol.into())
            .and_modify(|entry| entry.seen = Instant::now())
            .or_insert(Interest {
                seen: Instant::now(),
                attempted: None,
                rebase_bucket: None,
            });
        if is_new {
            self.wake.notify_one();
        }
    }

    /// Bounded work; failed symbols also wait before retrying.
    pub fn due_symbols(&self, limit: usize) -> Vec<String> {
        let mut interests = self.interests.lock().unwrap();
        interests.retain(|_, entry| entry.seen.elapsed() <= INTEREST_TTL);
        self.published
            .lock()
            .unwrap()
            .retain(|symbol, _| interests.contains_key(symbol));
        let mut due: Vec<_> = interests
            .iter()
            .filter(|(_, entry)| {
                entry
                    .attempted
                    .is_none_or(|at| at.elapsed() >= REFRESH_INTERVAL)
            })
            .map(|(symbol, entry)| (symbol.clone(), entry.attempted))
            .collect();
        due.sort_by_key(|(_, attempted)| *attempted);
        due.into_iter()
            .take(limit)
            .map(|(symbol, _)| {
                interests.get_mut(&symbol).unwrap().attempted = Some(Instant::now());
                symbol
            })
            .collect()
    }

    pub fn remove(&self, symbol: &str) {
        self.interests.lock().unwrap().remove(symbol);
        self.published.lock().unwrap().remove(symbol);
    }

    pub fn clear(&self) {
        self.published.lock().unwrap().clear();
    }

    pub fn get(&self, symbol: &str, timeframe: Timeframe, now: NaiveDateTime) -> Option<KlineDto> {
        let data = self.published.lock().unwrap();
        let entry = data.get(symbol)?;
        if entry.received.elapsed() > MAX_AGE
            || entry.calibrated.elapsed() > CALIBRATION_MAX_AGE
            || entry.base_end != live_5m_end(now)
        {
            return None;
        }
        entry.bars.get(timeframe.as_str()).cloned()
    }

    #[cfg(test)]
    pub fn publish(&self, symbol: &str, rows: Vec<Kline>, now: NaiveDateTime) -> Vec<KlineDto> {
        self.publish_after(symbol, rows, now, Instant::now())
    }

    /// Replace atomically. A quote received during the calibration request wins for current price.
    pub fn publish_after(
        &self,
        symbol: &str,
        rows: Vec<Kline>,
        now: NaiveDateTime,
        requested_at: Instant,
    ) -> Vec<KlineDto> {
        let base_end = live_5m_end(now);
        let mut by_ts = BTreeMap::new();
        for row in rows {
            if valid_row(&row) && row.datetime <= base_end {
                by_ts.insert(row.datetime.clone(), row);
            }
        }
        let rows: Vec<_> = by_ts.into_values().collect();
        let mut bars = HashMap::new();
        if rows.last().is_some_and(|last| last.datetime == base_end) {
            for tf in Timeframe::supported() {
                if !complete(symbol, &rows, tf, &base_end) {
                    continue;
                }
                if let Some(bar) = aggregate_endpoints(&rows, tf).pop() {
                    let dto = KlineDto {
                        symbol: symbol.into(),
                        timeframe: tf.as_str().into(),
                        ts: bar.datetime,
                        open: bar.open,
                        high: bar.high,
                        low: bar.low,
                        close: bar.close,
                        volume: bar.volume,
                        hold: bar.hold,
                        source: "tqsdk_partial".into(),
                        rollover: false,
                    };
                    bars.insert(tf.as_str().into(), dto);
                }
            }
        }
        let mut published = self.published.lock().unwrap();
        let quote = published
            .get(symbol)
            .filter(|entry| entry.base_end == base_end)
            .and_then(|entry| entry.quote.clone())
            .filter(|quote| quote.at > requested_at);
        if let Some(quote) = &quote {
            apply_quote(&mut bars, quote);
        }
        let result = bars.values().cloned().collect();
        published.insert(
            symbol.into(),
            Published {
                received: Instant::now(),
                calibrated: Instant::now(),
                base_end,
                bars,
                quote,
            },
        );
        result
    }

    /// O(periods) per quote, no SQL, bridge I/O or historical aggregation.
    pub fn update_prices(&self, snapshots: &[MarketSnapshot], now: NaiveDateTime) -> Vec<KlineDto> {
        let base_end = live_5m_end(now);
        let mut published = self.published.lock().unwrap();
        let mut updated = Vec::new();
        let mut rebase = Vec::new();
        for snapshot in snapshots {
            let Some(latest) = snapshot.latest.filter(|v| v.is_finite() && *v > 0.0) else {
                continue;
            };
            if !is_slice(
                &snapshot.code,
                NaiveDateTime::parse_from_str(&base_end, FORMAT).unwrap(),
            ) {
                continue;
            }
            let Some(entry) = published.get_mut(&snapshot.code) else {
                continue;
            };
            if entry.base_end != base_end || entry.calibrated.elapsed() > CALIBRATION_MAX_AGE {
                rebase.push(snapshot.code.clone());
                continue;
            }
            let quote = entry.quote.get_or_insert(QuoteUpdate {
                at: Instant::now(),
                latest,
                high: latest,
                low: latest,
            });
            quote.at = Instant::now();
            quote.latest = latest;
            quote.high = quote.high.max(latest);
            quote.low = quote.low.min(latest);
            apply_quote(&mut entry.bars, quote);
            entry.received = Instant::now();
            updated.extend(entry.bars.values().cloned());
        }
        drop(published);
        // Keep lock order consistent with due_symbols; a new bucket wakes one calibration.
        let mut interests = self.interests.lock().unwrap();
        for symbol in rebase {
            if let Some(entry) = interests.get_mut(&symbol) {
                if entry.rebase_bucket.as_ref() != Some(&base_end) {
                    entry.rebase_bucket = Some(base_end.clone());
                    entry.attempted = None;
                    self.wake.notify_one();
                }
            }
        }
        updated
    }
}

fn live_5m_end(now: NaiveDateTime) -> String {
    // A tick at 09:05:30 belongs to the candle ending at 09:10, not 09:05.
    let minute = now.with_second(0).unwrap().with_nanosecond(0).unwrap();
    (minute + chrono::Duration::minutes(5 - (now.minute() % 5) as i64))
        .format(FORMAT)
        .to_string()
}

fn valid_row(row: &Kline) -> bool {
    NaiveDateTime::parse_from_str(&row.datetime, FORMAT)
        .is_ok_and(|at| at.minute() % 5 == 0 && at.second() == 0)
        && [row.open, row.high, row.low, row.close, row.volume, row.hold]
            .iter()
            .all(|v| v.is_finite())
        && row.low > 0.0
        && row.high >= row.open.max(row.close)
        && row.low <= row.open.min(row.close)
        && row.volume >= 0.0
        && row.hold >= 0.0
}

fn is_slice(symbol: &str, end: NaiveDateTime) -> bool {
    (end - chrono::Duration::seconds(1))
        .and_local_timezone(Local)
        .single()
        .is_some_and(|at| SessionCalendar::is_trading_time(symbol, &at))
}

fn complete(symbol: &str, rows: &[Kline], tf: Timeframe, base_end: &str) -> bool {
    let end = NaiveDateTime::parse_from_str(base_end, FORMAT).unwrap();
    let has = |dt: NaiveDateTime| {
        let ts = dt.format(FORMAT).to_string();
        rows.binary_search_by(|row| row.datetime.as_str().cmp(&ts))
            .is_ok()
    };
    if !is_slice(symbol, end) {
        return false;
    }
    if let Some(minutes) = tf.minutes() {
        let bucket = NaiveDateTime::parse_from_str(
            &current_bucket_end_ts(end - chrono::Duration::minutes(1), tf),
            FORMAT,
        )
        .unwrap();
        let first = bucket - chrono::Duration::minutes(minutes - 5);
        let mut cursor = end;
        // Aggregation splits at session breaks; require every slice in this session segment.
        while cursor >= first && is_slice(symbol, cursor) {
            if !has(cursor) {
                return false;
            }
            cursor -= chrono::Duration::minutes(5);
        }
    } else {
        let day = trading_day(end);
        let mut cursor = (day - chrono::Days::new(1)).and_hms_opt(20, 5, 0).unwrap();
        while cursor <= end {
            if trading_day(cursor) == day && is_slice(symbol, cursor) && !has(cursor) {
                return false;
            }
            cursor += chrono::Duration::minutes(5);
        }
    }
    true
}

#[cfg(test)]
mod tests {
    use super::*;
    fn dt(ts: &str) -> NaiveDateTime {
        NaiveDateTime::parse_from_str(ts, FORMAT).unwrap()
    }
    fn row(ts: &str, open: f64, high: f64, low: f64, close: f64, volume: f64) -> Kline {
        Kline {
            datetime: ts.into(),
            open,
            high,
            low,
            close,
            volume,
            hold: 123.0,
        }
    }
    #[test]
    fn background_snapshot_preserves_open_extremes_and_volume_for_every_period() {
        let cache = LiveChartCache::default();
        let now = dt("2026-10-29 09:12:30");
        cache.publish(
            "JD0",
            vec![
                row("2026-10-29 09:05:00", 100., 110., 99., 105., 10.),
                row("2026-10-29 09:10:00", 105., 108., 98., 103., 20.),
                row("2026-10-29 09:15:00", 103., 107., 101., 106., 30.),
            ],
            now,
        );
        for tf in Timeframe::supported()
            .into_iter()
            .filter(|tf| *tf != Timeframe::M5)
        {
            let bar = cache.get("JD0", tf, now).unwrap();
            assert_eq!(
                (bar.open, bar.high, bar.low, bar.close, bar.volume, bar.hold),
                (100., 110., 98., 106., 60., 123.)
            );
        }
        assert!(cache
            .get("JD0", Timeframe::M15, dt("2026-10-29 09:15:01"))
            .is_none());
    }
    #[test]
    fn missing_slice_invalidates_previous_long_period_and_repair_replaces_it() {
        let cache = LiveChartCache::default();
        let now = dt("2026-10-29 09:12:30");
        let rows = vec![
            row("2026-10-29 09:05:00", 100., 110., 99., 105., 10.),
            row("2026-10-29 09:15:00", 103., 107., 101., 106., 30.),
        ];
        cache.publish("JD0", rows.clone(), now);
        assert!(cache.get("JD0", Timeframe::M15, now).is_none());
        assert!(cache.get("JD0", Timeframe::M5, now).is_some());
        let mut fixed = rows.clone();
        fixed.push(row("2026-10-29 09:10:00", 105., 108., 98., 103., 20.));
        cache.publish("JD0", fixed, now);
        assert_eq!(cache.get("JD0", Timeframe::M15, now).unwrap().volume, 60.);
        cache.publish("JD0", rows, now);
        assert!(cache.get("JD0", Timeframe::M15, now).is_none());
    }
    #[test]
    fn stale_or_wrong_bucket_never_becomes_a_live_candle() {
        let cache = LiveChartCache::default();
        let now = dt("2026-10-29 09:02:00");
        cache.publish(
            "JD0",
            vec![row("2026-10-29 09:05:00", 100., 101., 99., 100., 1.)],
            now,
        );
        cache
            .published
            .lock()
            .unwrap()
            .get_mut("JD0")
            .unwrap()
            .received = Instant::now() - MAX_AGE - Duration::from_secs(1);
        assert!(cache.get("JD0", Timeframe::M5, now).is_none());
        cache.publish(
            "JD0",
            vec![row("2026-10-28 09:05:00", 100., 101., 99., 100., 1.)],
            now,
        );
        assert!(cache.get("JD0", Timeframe::M5, now).is_none());
    }
    #[test]
    fn session_break_and_midnight_do_not_require_nontrading_slices() {
        let cache = LiveChartCache::default();
        let now = dt("2026-10-29 10:32:00");
        cache.publish(
            "JD0",
            vec![row("2026-10-29 10:35:00", 100., 101., 99., 100., 1.)],
            now,
        );
        assert!(cache.get("JD0", Timeframe::M60, now).is_some());
        let now = dt("2026-10-30 00:02:00");
        cache.publish(
            "AU0",
            vec![row("2026-10-30 00:05:00", 100., 101., 99., 100., 1.)],
            now,
        );
        assert!(cache.get("AU0", Timeframe::M15, now).is_some());
        assert!(cache.get("AU0", Timeframe::Day, now).is_none());
    }

    #[test]
    fn midnight_endpoint_belongs_to_the_previous_minute_bucket() {
        let cache = LiveChartCache::default();
        let rows = vec![
            row("2026-10-29 23:50:00", 100., 110., 99., 105., 10.),
            row("2026-10-29 23:55:00", 105., 108., 98., 103., 20.),
            row("2026-10-30 00:00:00", 103., 107., 101., 106., 30.),
        ];
        let before = dt("2026-10-29 23:58:00");
        cache.publish("AU0", rows.clone(), before);
        let bar = cache.get("AU0", Timeframe::M15, before).unwrap();
        assert_eq!(bar.ts, "2026-10-30 00:00:00");
        assert_eq!(bar.volume, 60.);
        let mut after_rows = rows;
        after_rows.push(row("2026-10-30 00:05:00", 106., 108., 105., 107., 40.));
        let after = dt("2026-10-30 00:02:00");
        cache.publish("AU0", after_rows, after);
        let bar = cache.get("AU0", Timeframe::M15, after).unwrap();
        assert_eq!(bar.ts, "2026-10-30 00:15:00");
        assert_eq!((bar.open, bar.volume), (106., 40.));
    }

    #[test]
    fn refresh_work_is_bounded_and_failed_attempts_are_throttled() {
        let cache = LiveChartCache::default();
        for symbol in ["JD0", "SR0", "CF0", "AU0", "EG0"] {
            cache.request(symbol);
        }
        assert_eq!(cache.due_symbols(4).len(), 4);
        assert_eq!(cache.due_symbols(4).len(), 1);
        assert!(cache.due_symbols(4).is_empty());
        cache.remove("JD0");
        cache.request("JD0");
        assert_eq!(cache.due_symbols(4), vec!["JD0"]);
    }

    fn quote(latest: f64) -> MarketSnapshot {
        MarketSnapshot {
            code: "JD0".into(),
            latest: Some(latest),
            change_pct: None,
        }
    }

    #[test]
    fn quotes_increment_existing_periods_without_fabricating_open_or_volume() {
        let cache = LiveChartCache::default();
        let now = dt("2026-10-29 09:02:00");
        assert!(
            cache.update_prices(&[quote(120.)], now).is_empty(),
            "a quote alone cannot seed a complete candle"
        );
        cache.publish(
            "JD0",
            vec![row("2026-10-29 09:05:00", 100., 110., 99., 105., 10.)],
            now,
        );
        assert_eq!(cache.update_prices(&[quote(120.)], now).len(), 7);
        cache.update_prices(&[quote(98.)], now);
        for tf in Timeframe::supported() {
            let bar = cache.get("JD0", tf, now).unwrap();
            assert_eq!(
                (bar.open, bar.high, bar.low, bar.close, bar.volume, bar.hold),
                (100., 120., 98., 98., 10., 123.)
            );
            assert_eq!(bar.source, "live_quote_calibrating");
        }
        cache.request("JD0");
        cache.due_symbols(4);
        assert!(cache
            .update_prices(&[quote(130.)], dt("2026-10-29 09:05:01"))
            .is_empty());
        assert_eq!(
            cache.due_symbols(4),
            vec!["JD0"],
            "new 5m bucket wakes calibration immediately"
        );
    }

    #[test]
    fn late_calibration_corrects_volume_without_reverting_a_newer_quote() {
        let cache = LiveChartCache::default();
        let now = dt("2026-10-29 09:02:00");
        cache.publish(
            "JD0",
            vec![row("2026-10-29 09:05:00", 100., 110., 99., 105., 10.)],
            now,
        );
        let requested = Instant::now();
        cache.update_prices(&[quote(120.)], now);
        cache.publish_after(
            "JD0",
            vec![row("2026-10-29 09:05:00", 100., 118., 97., 115., 200.)],
            now,
            requested,
        );
        let bar = cache.get("JD0", Timeframe::M15, now).unwrap();
        assert_eq!(
            (bar.open, bar.high, bar.low, bar.close, bar.volume),
            (100., 120., 97., 120., 200.)
        );
    }

    #[test]
    fn quotes_cannot_keep_an_uncalibrated_candle_alive_forever() {
        let cache = LiveChartCache::default();
        let now = dt("2026-10-29 09:02:00");
        cache.publish(
            "JD0",
            vec![row("2026-10-29 09:05:00", 100., 110., 99., 105., 10.)],
            now,
        );
        cache
            .published
            .lock()
            .unwrap()
            .get_mut("JD0")
            .unwrap()
            .calibrated = Instant::now() - CALIBRATION_MAX_AGE - Duration::from_secs(1);
        assert!(cache.update_prices(&[quote(120.)], now).is_empty());
        assert!(cache.get("JD0", Timeframe::M15, now).is_none());
    }
}
