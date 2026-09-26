use std::num::NonZeroUsize;
use std::sync::Mutex;
use std::time::Instant;
use lru::LruCache;
use n_protocol::dto::{ChartKlineResponse, KlineDto};

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
    pub last_updated: Instant,
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

    pub fn get(&self, symbol: &str, timeframe: &str) -> Option<ChartKlineResponse> {
        let mut lock = self.cache.lock().unwrap();
        let key = (symbol.to_string(), timeframe.to_string());
        let series = lock.get(&key)?;

        let mut rows = series.bars.clone();
        if let Some(partial) = &series.partial_bar {
            if let Some(last) = rows.last_mut() {
                if last.ts == partial.ts {
                    *last = partial.clone();
                } else {
                    rows.push(partial.clone());
                }
            } else {
                rows.push(partial.clone());
            }
        }

        Some(ChartKlineResponse {
            rows,
            status: "cached".to_string(),
            message: format!("Loaded from memory cache ({} bars)", series.bars.len()),
            latest_closed_ts: series.latest_closed_ts,
            series_revision: series.series_revision,
            server_time: Some(chrono::Utc::now().timestamp_millis()),
            has_more: Some(false),
            next_before: None,
            full_reload_required: Some(false),
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
            partial_bar: None,
            latest_closed_ts: resp.latest_closed_ts,
            series_revision: resp.series_revision,
            last_updated: Instant::now(),
        };

        lock.put(key, series);
    }

    pub fn merge_closed_bar(&self, symbol: &str, timeframe: &str, bar: KlineDto) {
        let mut lock = self.cache.lock().unwrap();
        let key = (symbol.to_string(), timeframe.to_string());
        if let Some(series) = lock.get_mut(&key) {
            if let Some(pos) = series.bars.iter().position(|b| b.ts == bar.ts) {
                series.bars[pos] = bar;
            } else {
                series.bars.push(bar);
                if series.bars.len() > MAX_BARS_PER_SERIES {
                    series.bars.remove(0);
                }
            }
            series.partial_bar = None;
            series.last_updated = Instant::now();
        }
    }

    pub fn update_partial_bar(&self, symbol: &str, timeframe: &str, bar: KlineDto) {
        let mut lock = self.cache.lock().unwrap();
        let key = (symbol.to_string(), timeframe.to_string());
        if let Some(series) = lock.get_mut(&key) {
            series.partial_bar = Some(bar);
            series.last_updated = Instant::now();
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
