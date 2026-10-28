//! Causal adaptive exits for the independent combination experiment only.
use crate::{
    derive::{current_bucket_end_ts, Timeframe},
    fetch::kline::Kline,
};
use chrono::{Duration, NaiveDateTime};

#[derive(Clone, Debug)]
struct Candle {
    start: String,
    end: String,
    high: f64,
    low: f64,
    close: f64,
}
#[derive(Clone, Debug, Default)]
pub struct Action {
    pub close: Option<&'static str>,
    pub stop: Option<f64>,
}
#[derive(Clone, Debug, Default)]
pub struct AdaptiveExit {
    bucket: String,
    minutes: Vec<Kline>,
    candles: Vec<Candle>,
    armed_at: Option<String>,
    recorded_peak: f64,
    stagnant: usize,
    last_structure: Option<f64>,
}
impl AdaptiveExit {
    /// Only fully observed post-entry 15m candles enter the state. Pivot center
    /// must start AFTER the observed 1R milestone; confirmation uses its right bar.
    #[allow(clippy::too_many_arguments)]
    pub fn observe(
        &mut self,
        bar: &Kline,
        entry_ts: &str,
        peak: f64,
        fill: f64,
        risk: f64,
        sign: f64,
        tick: f64,
        warning_extreme: f64,
        s2: f64,
        current_stop: f64,
        ongoing: bool,
        structure: bool,
    ) -> Action {
        let mut action = Action::default();
        if peak + 1e-9 >= 1. && self.armed_at.is_none() {
            self.armed_at = Some(bar.datetime.clone());
        }
        if bar.datetime.as_str() <= entry_ts {
            return action;
        }
        let dt = NaiveDateTime::parse_from_str(&bar.datetime, "%Y-%m-%d %H:%M:%S").unwrap();
        let bucket = current_bucket_end_ts(dt, Timeframe::M15);
        if self.bucket != bucket {
            self.bucket = bucket.clone();
            self.minutes.clear();
        }
        self.minutes.push(bar.clone());
        if bar.datetime != bucket || self.minutes.len() != 15 {
            return action;
        }
        let end = dt;
        let start = end - Duration::minutes(15);
        if start.format("%Y-%m-%d %H:%M:%S").to_string().as_str() < entry_ts {
            return action;
        }
        if !self.minutes.iter().enumerate().all(|(i, b)| {
            b.datetime
                == (start + Duration::minutes(i as i64 + 1))
                    .format("%Y-%m-%d %H:%M:%S")
                    .to_string()
        }) {
            return action;
        }
        let candle = Candle {
            start: start.format("%Y-%m-%d %H:%M:%S").to_string(),
            end: bucket,
            high: self
                .minutes
                .iter()
                .map(|b| b.high)
                .fold(f64::NEG_INFINITY, f64::max),
            low: self
                .minutes
                .iter()
                .map(|b| b.low)
                .fold(f64::INFINITY, f64::min),
            close: bar.close,
        };
        // Reset only on an actually observed additional tick, not on elapsed time.
        if (peak - self.recorded_peak) * risk + 1e-9 >= tick {
            self.stagnant = 0;
            self.recorded_peak = peak;
        } else {
            self.stagnant += 1;
        }
        self.candles.push(candle);
        let n = self.candles.len();
        let c = &self.candles[n - 1];
        let returned = (c.close - warning_extreme) * sign <= 1e-9;
        if ongoing && n >= 2 {
            let prior = &self.candles[n - 2];
            let consecutive = NaiveDateTime::parse_from_str(&c.end, "%Y-%m-%d %H:%M:%S").unwrap()
                - NaiveDateTime::parse_from_str(&prior.end, "%Y-%m-%d %H:%M:%S").unwrap()
                == Duration::minutes(15);
            if consecutive
                && peak < 0.5
                && returned
                && (prior.close - warning_extreme) * sign <= 1e-9
                && (c.close - prior.close) * sign <= 0.
            {
                action.close = Some("adaptive_failed_start");
            } else if peak >= 0.5 && self.stagnant >= 5 && returned {
                action.close = Some("adaptive_stall_return");
            }
        }
        if structure && n >= 3 && self.armed_at.is_some() {
            let (l, m, r) = (
                &self.candles[n - 3],
                &self.candles[n - 2],
                &self.candles[n - 1],
            );
            let consecutive = [(&l.end, &m.end), (&m.end, &r.end)].iter().all(|(a, b)| {
                NaiveDateTime::parse_from_str(b, "%Y-%m-%d %H:%M:%S").unwrap()
                    - NaiveDateTime::parse_from_str(a, "%Y-%m-%d %H:%M:%S").unwrap()
                    == Duration::minutes(15)
            });
            if consecutive && m.start >= *self.armed_at.as_ref().unwrap() {
                let pivot = if sign > 0. {
                    m.low < l.low && m.low < r.low
                } else {
                    m.high > l.high && m.high > r.high
                };
                let price = if sign > 0. { m.low } else { m.high };
                if pivot && (price - self.last_structure.unwrap_or(s2)) * sign > 1e-9 {
                    self.last_structure = Some(price);
                    let raw = price - sign * tick;
                    let candidate = if sign > 0. {
                        (raw / tick + 1e-9).floor() * tick
                    } else {
                        (raw / tick - 1e-9).ceil() * tick
                    };
                    if (candidate - current_stop) * sign > 1e-9 {
                        // Right bar has already confirmed the pivot; this stop is
                        // armed for the NEXT minute, never applied retrospectively.
                        action.stop = Some(candidate);
                    }
                }
            }
        }
        if self.candles.len() > 3 {
            self.candles.remove(0);
        }
        let _ = fill;
        action
    }
}
