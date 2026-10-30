use lru::LruCache;
use n_protocol::dto::{ChartKlineResponse, KlineDto};
use std::num::NonZeroUsize;
use std::sync::Mutex;
use std::time::Instant;

const MAX_SERIES: usize = 24;
const MAX_BARS_PER_SERIES: usize = 2000;

#[derive(Clone, Debug)]
pub struct CachedSeries {
    pub symbol: String,
    pub timeframe: String,
    pub bars: Vec<KlineDto>,
    pub partial_bar: Option<KlineDto>,
    pub latest_closed_ts: Option<i64>,
    pub series_revision: Option<i64>,
    pub status: String,
    pub message: String,
    pub fetched_at: Instant,
}

pub struct KlineMemoryCache {
    cache: Mutex<LruCache<(String, String), CachedSeries>>,
}

impl KlineMemoryCache {
    pub fn new() -> Self {
        Self {
            cache: Mutex::new(LruCache::new(NonZeroUsize::new(MAX_SERIES).unwrap())),
        }
    }

    pub fn get_fresh(
        &self,
        symbol: &str,
        timeframe: &str,
        max_age: std::time::Duration,
    ) -> Option<ChartKlineResponse> {
        let mut lock = self.cache.lock().unwrap();
        let key = (symbol.to_string(), timeframe.to_string());
        let series = lock.get(&key)?;
        if series.fetched_at.elapsed() > max_age {
            return None;
        }

        let rows = series.bars.clone();

        Some(ChartKlineResponse {
            rows,
            status: series.status.clone(),
            // 三秒内的新鲜缓存只是正常快速路径，保留数据源消息，不显示降级提示。
            message: series.message.clone(),
            latest_closed_ts: series.latest_closed_ts,
            series_revision: series.series_revision,
            server_time: Some(chrono::Utc::now().timestamp_millis()),
            has_more: Some(false),
            next_before: None,
            full_reload_required: Some(false),
            partial_bar: series.partial_bar.clone(),
        })
    }

    pub fn get(&self, symbol: &str, timeframe: &str) -> Option<ChartKlineResponse> {
        let mut lock = self.cache.lock().unwrap();
        let key = (symbol.to_string(), timeframe.to_string());
        let series = lock.get(&key)?;

        let rows = series.bars.clone();

        Some(ChartKlineResponse {
            rows,
            status: series.status.clone(),
            message: format!(
                "从内存缓存加载（{} 根）。{}",
                series.bars.len(),
                series.message
            ),
            latest_closed_ts: series.latest_closed_ts,
            series_revision: series.series_revision,
            server_time: Some(chrono::Utc::now().timestamp_millis()),
            has_more: Some(false),
            next_before: None,
            full_reload_required: Some(false),
            partial_bar: series.partial_bar.clone(),
        })
    }

    pub fn put(&self, symbol: &str, timeframe: &str, resp: &ChartKlineResponse) {
        let mut lock = self.cache.lock().unwrap();
        let key = (symbol.to_string(), timeframe.to_string());

        let mut bars = resp.rows.clone();
        if bars.len() > MAX_BARS_PER_SERIES {
            let skip_count = bars.len() - MAX_BARS_PER_SERIES;
            bars.drain(0..skip_count);
        }

        let series = CachedSeries {
            symbol: symbol.to_string(),
            timeframe: timeframe.to_string(),
            bars,
            partial_bar: resp.partial_bar.clone(),
            latest_closed_ts: resp.latest_closed_ts,
            series_revision: resp.series_revision,
            status: resp.status.clone(),
            message: resp.message.clone(),
            fetched_at: Instant::now(),
        };

        lock.put(key, series);
    }

    pub fn merge_closed_bar(&self, symbol: &str, timeframe: &str, bar: KlineDto) {
        let mut lock = self.cache.lock().unwrap();
        let key = (symbol.to_string(), timeframe.to_string());
        if let Some(series) = lock.get_mut(&key) {
            let closed_ts = bar.ts.clone();
            if let Some(pos) = series.bars.iter().position(|b| b.ts == bar.ts) {
                series.bars[pos] = bar;
            } else {
                series.bars.push(bar);
                if series.bars.len() > MAX_BARS_PER_SERIES {
                    series.bars.remove(0);
                }
            }
            series.bars.sort_by(|a, b| a.ts.cmp(&b.ts));
            series.latest_closed_ts = series.bars.last().and_then(|b| {
                chrono::NaiveDateTime::parse_from_str(&b.ts, "%Y-%m-%d %H:%M:%S")
                    .ok()
                    .and_then(|dt| dt.and_local_timezone(chrono::Local).single())
                    .map(|dt| dt.timestamp_millis())
            });
            if series
                .partial_bar
                .as_ref()
                .is_some_and(|partial| partial.ts <= closed_ts)
            {
                series.partial_bar = None;
            }
        }
    }

    pub fn update_partial_bar(&self, symbol: &str, timeframe: &str, bar: KlineDto) {
        let mut lock = self.cache.lock().unwrap();
        let key = (symbol.to_string(), timeframe.to_string());
        if let Some(series) = lock.get_mut(&key) {
            if series.bars.last().is_some_and(|closed| bar.ts <= closed.ts)
                || series
                    .partial_bar
                    .as_ref()
                    .is_some_and(|partial| bar.ts < partial.ts)
            {
                return;
            }
            series.partial_bar = Some(bar);
        }
    }

    pub fn invalidate(&self, symbol: &str, timeframe: &str) {
        let mut lock = self.cache.lock().unwrap();
        let key = (symbol.to_string(), timeframe.to_string());
        lock.pop(&key);
    }

    pub fn clear(&self) {
        let mut lock = self.cache.lock().unwrap();
        lock.clear();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    fn bar() -> KlineDto {
        KlineDto {
            symbol: "EG0".into(),
            timeframe: "15m".into(),
            ts: "2026-10-19 21:15:00".into(),
            open: 4976.0,
            high: 4976.0,
            low: 4920.0,
            close: 4934.0,
            volume: 1.0,
            hold: 1.0,
            source: "derived_partial".into(),
            rollover: false,
        }
    }

    #[test]
    fn partial_tick_does_not_refresh_history_or_hide_source_warning() {
        let cache = KlineMemoryCache::new();
        cache.put(
            "EG0",
            "15m",
            &ChartKlineResponse {
                rows: vec![],
                status: "tq_unavailable".into(),
                message: "仅显示本地历史数据".into(),
                latest_closed_ts: None,
                series_revision: None,
                server_time: None,
                has_more: None,
                next_before: None,
                full_reload_required: None,
                partial_bar: None,
            },
        );
        {
            let mut lock = cache.cache.lock().unwrap();
            lock.get_mut(&(String::from("EG0"), String::from("15m")))
                .unwrap()
                .fetched_at = Instant::now() - Duration::from_secs(10);
        }
        cache.update_partial_bar("EG0", "15m", bar());
        assert!(cache
            .get_fresh("EG0", "15m", Duration::from_secs(3))
            .is_none());
        let response = cache.get("EG0", "15m").unwrap();
        assert_eq!(response.status, "tq_unavailable");
        assert!(response.message.contains("仅显示本地历史数据"));
    }

    #[test]
    fn fresh_cache_keeps_source_message_without_fallback_notice() {
        let cache = KlineMemoryCache::new();
        let mut response = ChartKlineResponse {
            rows: vec![bar()],
            status: "tqsdk".into(),
            message: String::new(),
            latest_closed_ts: None,
            series_revision: None,
            server_time: None,
            has_more: None,
            next_before: None,
            full_reload_required: None,
            partial_bar: None,
        };
        cache.put("EG0", "15m", &response);
        let fresh = cache
            .get_fresh("EG0", "15m", Duration::from_secs(3))
            .unwrap();
        assert!(fresh.message.is_empty());
        assert!(cache
            .get("EG0", "15m")
            .unwrap()
            .message
            .contains("从内存缓存加载"));

        response.status = "tq_unavailable".into();
        response.message = "仅显示本地历史数据".into();
        cache.put("EG0", "15m", &response);
        let fresh = cache
            .get_fresh("EG0", "15m", Duration::from_secs(3))
            .unwrap();
        assert_eq!(fresh.status, "tq_unavailable");
        assert_eq!(fresh.message, response.message);
    }

    #[test]
    fn finalized_cache_rows_are_separate_and_reject_late_partial_updates() {
        let cache = KlineMemoryCache::new();
        let response = ChartKlineResponse {
            rows: vec![],
            status: "tqsdk".into(),
            message: String::new(),
            latest_closed_ts: None,
            series_revision: None,
            server_time: None,
            has_more: None,
            next_before: None,
            full_reload_required: None,
            partial_bar: Some(bar()),
        };
        cache.put("EG0", "15m", &response);
        assert!(cache.get("EG0", "15m").unwrap().rows.is_empty());
        assert!(cache
            .get_fresh("EG0", "15m", Duration::from_secs(3))
            .unwrap()
            .rows
            .is_empty());
        let mut final_bar = bar();
        final_bar.close = 4950.0;
        cache.merge_closed_bar("EG0", "15m", final_bar.clone());
        cache.update_partial_bar("EG0", "15m", bar());
        let got = cache.get("EG0", "15m").unwrap();
        assert_eq!(got.rows[0].close, 4950.0);
        assert!(got.partial_bar.is_none());
        assert!(got.latest_closed_ts.is_some());
        let mut next = bar();
        next.ts = "2026-10-19 21:30:00".into();
        cache.update_partial_bar("EG0", "15m", next);
        cache.merge_closed_bar("EG0", "15m", final_bar);
        assert_eq!(
            cache.get("EG0", "15m").unwrap().partial_bar.unwrap().ts,
            "2026-10-19 21:30:00"
        );
    }
}
