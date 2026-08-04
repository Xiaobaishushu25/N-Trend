use crate::analyze::model::{ATR_PERIOD, Bar, Swing, Trend60};

pub fn atr(bars: &[Bar], period: usize) -> Vec<Option<f64>> {
    let n = bars.len();
    let mut tr = vec![0.0; n];
    for i in 0..n {
        tr[i] = if i == 0 {
            bars[i].high - bars[i].low
        } else {
            let pc = bars[i - 1].close;
            (bars[i].high - bars[i].low)
                .max((bars[i].high - pc).abs())
                .max((bars[i].low - pc).abs())
        };
    }

    let mut out = vec![None; n];
    let mut sum = 0.0;
    for i in 0..n {
        sum += tr[i];
        if i >= period {
            sum -= tr[i - period];
        }
        if i + 1 >= period {
            out[i] = Some(sum / period as f64);
        }
    }
    out
}

pub fn trend_flags(bars: &[Bar], atr20: &[Option<f64>]) -> Vec<(bool, bool)> {
    let mut out = Vec::with_capacity(bars.len());
    for i in 0..bars.len() {
        let mut up = false;
        let mut down = false;
        if let Some(a) = atr20[i] {
            let range = bars[i].high - bars[i].low;
            if range > 0.0 {
                let body = (bars[i].close - bars[i].open).abs();
                let body_ratio = body / range;
                let up_pos = (bars[i].close - bars[i].low) / range;
                let down_pos = (bars[i].high - bars[i].close) / range;
                let upper = bars[i].high - bars[i].open.max(bars[i].close);
                let lower = bars[i].open.min(bars[i].close) - bars[i].low;

                if bars[i].close > bars[i].open
                    && body_ratio >= 0.75
                    && up_pos >= 0.80
                    && range >= 0.8 * a
                    && upper <= body
                {
                    up = true;
                }
                if bars[i].close < bars[i].open
                    && body_ratio >= 0.75
                    && down_pos >= 0.80
                    && range >= 0.8 * a
                    && lower <= body
                {
                    down = true;
                }
            }
        }
        out.push((up, down));
    }
    out
}

// a段有效性校验用的放宽版趋势K线阈值
pub const A_LEG_BODY_RATIO_MIN: f64 = 0.55;
pub const A_LEG_CLOSE_POS_MIN: f64 = 0.65;
pub const A_LEG_RANGE_ATR_MIN: f64 = 0.65;
pub const A_LEG_WICK_RATIO_MAX: f64 = 0.90;

pub fn trend_flags_relaxed(bars: &[Bar], atr20: &[Option<f64>]) -> Vec<(bool, bool)> {
    let mut out = Vec::with_capacity(bars.len());
    for i in 0..bars.len() {
        let mut up = false;
        let mut down = false;
        if let Some(a) = atr20[i] {
            let range = bars[i].high - bars[i].low;
            if range > 0.0 {
                let body = (bars[i].close - bars[i].open).abs();
                let body_ratio = body / range;
                let up_pos = (bars[i].close - bars[i].low) / range;
                let down_pos = (bars[i].high - bars[i].close) / range;
                let upper = bars[i].high - bars[i].open.max(bars[i].close);
                let lower = bars[i].open.min(bars[i].close) - bars[i].low;

                if bars[i].close > bars[i].open
                    && body_ratio >= A_LEG_BODY_RATIO_MIN
                    && up_pos >= A_LEG_CLOSE_POS_MIN
                    && range >= A_LEG_RANGE_ATR_MIN * a
                    && upper <= A_LEG_WICK_RATIO_MAX * body
                {
                    up = true;
                }
                if bars[i].close < bars[i].open
                    && body_ratio >= A_LEG_BODY_RATIO_MIN
                    && down_pos >= A_LEG_CLOSE_POS_MIN
                    && range >= A_LEG_RANGE_ATR_MIN * a
                    && lower <= A_LEG_WICK_RATIO_MAX * body
                {
                    down = true;
                }
            }
        }
        out.push((up, down));
    }
    out
}

pub fn find_swings(
    bars: &[Bar],
    atr20: &[Option<f64>],
    window: usize,
    merge_gap: usize,
) -> Vec<Swing> {
    let n = bars.len();
    if n < window * 2 + 1 {
        return Vec::new();
    }

    let mut raw = Vec::new();
    for i in window..n - window {
        let h = bars[i].high;
        let l = bars[i].low;
        let mut high_ok = true;
        let mut low_ok = true;

        for j in i - window..i {
            if h < bars[j].high {
                high_ok = false;
            }
            if l > bars[j].low {
                low_ok = false;
            }
        }
        for j in i + 1..=i + window {
            if h < bars[j].high {
                high_ok = false;
            }
            if l > bars[j].low {
                low_ok = false;
            }
        }

        if high_ok {
            raw.push(Swing {
                index: i,
                price: h,
                is_high: true,
            });
        }
        if low_ok {
            raw.push(Swing {
                index: i,
                price: l,
                is_high: false,
            });
        }
    }

    let mut merged: Vec<Swing> = Vec::new();
    for s in raw {
        let mut skip = false;
        if let Some(last) = merged.last_mut() {
            if last.is_high == s.is_high && s.index.saturating_sub(last.index) <= merge_gap {
                let atr_now = atr20[s.index].unwrap_or(1.0);
                let tol = (0.5 * atr_now).max(2.0);
                if (last.price - s.price).abs() <= tol {
                    if (s.is_high && s.price > last.price)
                        || (!s.is_high && s.price < last.price)
                    {
                        *last = s;
                    }
                    skip = true;
                }
            }
        }
        if !skip {
            merged.push(s);
        }
    }
    merged
}

pub fn analyze_60m(bars: &[Bar]) -> Trend60 {
    let n = bars.len();
    let mut sum = 0.0;
    for i in n - ATR_PERIOD..n {
        sum += bars[i].close;
    }
    let ma20 = sum / ATR_PERIOD as f64;

    let mut prev_sum = 0.0;
    for i in n - ATR_PERIOD - 1..n - 1 {
        prev_sum += bars[i].close;
    }
    let prev_ma = prev_sum / ATR_PERIOD as f64;
    let slope = ma20 - prev_ma;
    let close = bars[n - 1].close;

    let atr20 = atr(bars, ATR_PERIOD);
    let swings = find_swings(bars, &atr20, 3, 8);
    let highs: Vec<&Swing> = swings.iter().filter(|s| s.is_high).collect();
    let lows: Vec<&Swing> = swings.iter().filter(|s| !s.is_high).collect();
    let higher_highs =
        highs.len() >= 2 && highs[highs.len() - 1].price > highs[highs.len() - 2].price;
    let higher_lows = lows.len() >= 2 && lows[lows.len() - 1].price > lows[lows.len() - 2].price;
    let lower_highs =
        highs.len() >= 2 && highs[highs.len() - 1].price < highs[highs.len() - 2].price;
    let lower_lows = lows.len() >= 2 && lows[lows.len() - 1].price < lows[lows.len() - 2].price;

    let direction = if close > ma20 && slope > 0.0 && higher_highs && higher_lows {
        "UP"
    } else if close < ma20 && slope < 0.0 && !higher_highs && !higher_lows {
        "DOWN"
    } else if close > ma20 && slope > 0.0 {
        "WEAK_UP"
    } else if close < ma20 && slope < 0.0 {
        "WEAK_DOWN"
    } else {
        "NEUTRAL"
    };

    Trend60 {
        direction: direction.to_string(),
        ma20,
        slope,
        price_vs_ma: close - ma20,
        higher_highs,
        higher_lows,
        lower_highs,
        lower_lows,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::analyze::model::DT;

    fn bar(open: f64, high: f64, low: f64, close: f64) -> Bar {
        Bar {
            dt: DT {
                year: 2026,
                month: 7,
                day: 31,
                hour: 13,
                minute: 30,
            },
            open,
            high,
            low,
            close,
            volume: 0.0,
            hold: 0.0,
        }
    }

    #[test]
    fn relaxed_flags_accept_moderate_trend_candle() {
        // 实体66%，收盘在上77%位，振幅0.9ATR，上影短
        let bars = vec![bar(100.0, 108.0, 99.0, 106.0)];
        let atr = vec![Some(10.0)];
        assert!(trend_flags_relaxed(&bars, &atr)[0].0);
        // 严格版要求实体>=75%，不认可该K线
        assert!(!trend_flags(&bars, &atr)[0].0);
    }

    #[test]
    fn relaxed_flags_reject_weak_candle() {
        // 实体20%，影线长，不构成趋势K线
        let bars = vec![bar(100.0, 108.0, 99.0, 101.6)];
        let atr = vec![Some(10.0)];
        assert!(!trend_flags_relaxed(&bars, &atr)[0].0);
        assert!(!trend_flags_relaxed(&bars, &atr)[0].1);
    }
}
