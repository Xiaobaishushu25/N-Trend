use crate::analyze::model::{Bar, Swing, Trend60, ATR_PERIOD};

pub fn atr(bars: &[Bar], period: usize) -> Vec<Option<f64>> {
    let n = bars.len();
    let mut tr = vec![0.0; n];
    for i in 0..n {
        tr[i] = if i == 0 || bars[i].rollover {
            // 换月 bar 的“前收盘”来自旧合约，跨合约 gap 不构成真实波动
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

/// 简单移动平均序列；不足 period 根的位置为 None。
pub fn ma_series(bars: &[Bar], period: usize) -> Vec<Option<f64>> {
    let n = bars.len();
    let mut out = vec![None; n];
    if period == 0 {
        return out;
    }
    let mut sum = 0.0;
    for i in 0..n {
        sum += bars[i].close;
        if i >= period {
            sum -= bars[i - period].close;
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
        if !bars[i].rollover {
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
        if !bars[i].rollover {
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
                    if (s.is_high && s.price > last.price) || (!s.is_high && s.price < last.price) {
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

// 5档60m趋势：收紧版阈值 2.0ATR + ADX25/18 + 20根不触EMA20(0.3ATR)
// 对齐实盘体感：FG0->RANGE, PB0->WEAK_UP, AU0->STRONG_UP
const TREND_EMA_FAST: usize = 20;
const TREND_EMA_SLOW: usize = 60;
const TREND_ATR_PERIOD: usize = 20;
const TREND_ADX_PERIOD: usize = 14;
const TREND_STRONG_ATR: f64 = 2.0;
const TREND_ADX_STRONG: f64 = 25.0;
const TREND_ADX_WEAK: f64 = 18.0;
const TREND_TOUCH_WINDOW: usize = 20;
const TREND_TOUCH_THRESH: f64 = 0.3;

fn ema_series(values: &[f64], period: usize) -> Vec<Option<f64>> {
    let n = values.len();
    let mut out = vec![None; n];
    if n < period || period == 0 { return out; }
    let k = 2.0 / (period as f64 + 1.0);
    let mut sma = 0.0;
    for i in 0..period { sma += values[i]; }
    sma /= period as f64;
    out[period-1] = Some(sma);
    let mut prev = sma;
    for i in period..n {
        prev = values[i]*k + prev*(1.0-k);
        out[i] = Some(prev);
    }
    out
}

fn adx_wilder(bars: &[Bar], period: usize) -> (Vec<Option<f64>>, Vec<f64>, Vec<f64>) {
    let n = bars.len();
    let mut adx = vec![None; n];
    let mut pdi = vec![0.0; n];
    let mut mdi = vec![0.0; n];
    if n < period+1 { return (adx,pdi,mdi); }
    let mut tr = vec![0.0; n];
    let mut plus_dm = vec![0.0; n];
    let mut minus_dm = vec![0.0; n];
    for i in 1..n {
        let h = bars[i].high; let l = bars[i].low;
        let ph = bars[i-1].high; let pl = bars[i-1].low; let pc = bars[i-1].close;
        tr[i] = (h - l).max((h - pc).abs()).max((l - pc).abs());
        let up = h - ph; let dn = pl - l;
        if up > dn && up > 0.0 { plus_dm[i] = up; }
        if dn > up && dn > 0.0 { minus_dm[i] = dn; }
    }
    let mut atr_w = 0.0; let mut p_dm_w = 0.0; let mut m_dm_w = 0.0;
    for i in 1..=period { atr_w += tr[i]; p_dm_w += plus_dm[i]; m_dm_w += minus_dm[i]; }
    let mut dx = vec![0.0; n];
    // first dx at period
    if atr_w != 0.0 {
        pdi[period] = 100.0 * p_dm_w / atr_w;
        mdi[period] = 100.0 * m_dm_w / atr_w;
        let s = pdi[period] + mdi[period];
        if s != 0.0 { dx[period] = 100.0 * (pdi[period]-mdi[period]).abs() / s; }
    }
    for i in period+1..n {
        atr_w = atr_w - atr_w/period as f64 + tr[i];
        p_dm_w = p_dm_w - p_dm_w/period as f64 + plus_dm[i];
        m_dm_w = m_dm_w - m_dm_w/period as f64 + minus_dm[i];
        if atr_w != 0.0 {
            pdi[i] = 100.0 * p_dm_w / atr_w;
            mdi[i] = 100.0 * m_dm_w / atr_w;
            let s = pdi[i] + mdi[i];
            if s != 0.0 { dx[i] = 100.0 * (pdi[i]-mdi[i]).abs() / s; }
        }
    }
    if n >= period*2 {
        let mut sum_dx = 0.0;
        for i in period..period+period { sum_dx += dx[i]; }
        adx[period*2 -1] = Some(sum_dx / period as f64);
        for i in period*2..n {
            let prev = adx[i-1].unwrap_or(0.0);
            adx[i] = Some((prev*(period as f64 -1.0) + dx[i]) / period as f64);
        }
    }
    (adx,pdi,mdi)
}

pub fn analyze_60m(bars: &[Bar]) -> Trend60 {
    let n = bars.len();
    if n < ATR_PERIOD {
        return Trend60 { direction: "RANGE".to_string(), ma20: bars.last().map(|b| b.close).unwrap_or(0.0), slope: 0.0, price_vs_ma: 0.0, higher_highs: false, higher_lows: false, lower_highs: false, lower_lows: false };
    }
    let closes: Vec<f64> = bars.iter().map(|b| b.close).collect();
    let e20 = ema_series(&closes, TREND_EMA_FAST);
    let e60 = ema_series(&closes, TREND_EMA_SLOW);
    let a20 = atr(bars, TREND_ATR_PERIOD);
    let (adx, pdi, mdi) = adx_wilder(bars, TREND_ADX_PERIOD);
    // fallback simple ma20/slope for display
    let mut sum = 0.0;
    for i in n-ATR_PERIOD..n { sum += bars[i].close; }
    let ma20 = sum/ATR_PERIOD as f64;
    let mut prev_sum=0.0;
    if n >= ATR_PERIOD+1 {
        for i in n-ATR_PERIOD-1..n-1 { prev_sum+= bars[i].close; }
    } else { prev_sum = sum; }
    let prev_ma = prev_sum/ATR_PERIOD as f64;
    let slope = ma20 - prev_ma;
    let close = bars[n-1].close;
    let atr20 = atr(bars, ATR_PERIOD);
    let swings = find_swings(bars, &atr20, 3, 8);
    let highs: Vec<&Swing> = swings.iter().filter(|s| s.is_high).collect();
    let lows: Vec<&Swing> = swings.iter().filter(|s| !s.is_high).collect();
    let higher_highs = highs.len()>=2 && highs[highs.len()-1].price > highs[highs.len()-2].price;
    let higher_lows = lows.len()>=2 && lows[lows.len()-1].price > lows[lows.len()-2].price;
    let lower_highs = highs.len()>=2 && highs[highs.len()-1].price < highs[highs.len()-2].price;
    let lower_lows = lows.len()>=2 && lows[lows.len()-1].price < lows[lows.len()-2].price;

    // determine 5-tier direction if enough data, else fallback to old weak logic
    let direction = if n >= TREND_EMA_SLOW + TREND_ADX_PERIOD*2 {
        let idx=n-1;
        let e20v = e20[idx]; let e60v = e60[idx]; let atrv = a20[idx]; let adxv = adx[idx];
        if let (Some(ev20), Some(ev60), Some(av), Some(ax)) = (e20v, e60v, atrv, adxv) {
            let pdiv=pdi[idx]; let mdiv=mdi[idx];
            let mut touched=false;
            let start=idx.saturating_sub(TREND_TOUCH_WINDOW-1);
            for j in start..=idx {
                if let (Some(e), Some(a)) = (e20[j], a20[j]) {
                    if (bars[j].close - e).abs() < TREND_TOUCH_THRESH * a { touched=true; break; }
                }
            }
            if close > ev60 + TREND_STRONG_ATR*av && ev20 > ev60 && ax >= TREND_ADX_STRONG && !touched && pdiv > mdiv {
                "STRONG_UP"
            } else if close < ev60 - TREND_STRONG_ATR*av && ev20 < ev60 && ax >= TREND_ADX_STRONG && !touched && mdiv > pdiv {
                "STRONG_DOWN"
            } else if close > ev60 && ev20 > ev60 && ax >= TREND_ADX_WEAK && pdiv > mdiv {
                "WEAK_UP"
            } else if close < ev60 && ev20 < ev60 && ax >= TREND_ADX_WEAK && mdiv > pdiv {
                "WEAK_DOWN"
            } else {
                "RANGE"
            }
        } else { "RANGE" }
    } else {
        // not enough for 5-tier, use simple range
        "RANGE"
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


/// 单K裸K：锤 / 针 —— 仅15m 独立通道，不入 N
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BareKind { Hammer, Needle }

#[derive(Debug, Clone)]
pub struct BareSignal {
    pub kind: BareKind,
    pub bar_ts: String,
    pub price: f64,
    pub high: f64,
    pub low: f64,
}

fn bar_ts_str(dt: &crate::analyze::model::DT) -> String {
    format!("{:04}-{:02}-{:02} {:02}:{:02}:00", dt.year, dt.month, dt.day, dt.hour, dt.minute)
}

fn dt_add_minutes(dt: &crate::analyze::model::DT, add_mins: i32) -> crate::analyze::model::DT {
    let mut y = dt.year;
    let mut m = dt.month;
    let mut d = dt.day;
    let mut h = dt.hour;
    let mut min = dt.minute + add_mins;
    while min >= 60 { min -= 60; h += 1; }
    while h >= 24 {
        h -= 24; d += 1;
        let dim = days_in_month(y, m);
        if d > dim { d = 1; m += 1; if m > 12 { m = 1; y += 1; } }
    }
    crate::analyze::model::DT { year: y, month: m, day: d, hour: h, minute: min }
}

fn days_in_month(y: i32, m: i32) -> i32 {
    match m {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 => if (y % 4 == 0 && y % 100 != 0) || (y % 400 == 0) { 29 } else { 28 },
        _ => 30,
    }
}

fn calc_atr14(bars: &[crate::analyze::model::Bar]) -> Option<f64> {
    if bars.len() < 15 { return None; }
    let mut trs: Vec<f64> = Vec::with_capacity(14);
    let start = bars.len() - 14;
    for i in start..bars.len() {
        let cur = &bars[i];
        let hl = cur.high - cur.low;
        let tr = if i == 0 || cur.rollover { hl } else { let pc = bars[i - 1].close; hl.max((cur.high - pc).abs()).max((cur.low - pc).abs()) };
        trs.push(tr);
    }
    if trs.is_empty() { return None; }
    Some(trs.iter().sum::<f64>() / trs.len() as f64)
}

pub fn detect_bare_prev(bars: &[crate::analyze::model::Bar]) -> Option<BareSignal> {
    if bars.is_empty() { return None; }
    let b = bars.last().unwrap();
    let range = b.high - b.low;
    if range <= 0.0 { return None; }
    let body = (b.close - b.open).abs();
    if body <= 0.0 { return None; }
    let upper = b.high - b.open.max(b.close);
    let lower = b.open.min(b.close) - b.low;
    let atr = calc_atr14(bars).unwrap_or(0.0);
    let is_hammer = b.close > b.open && upper <= 0.05 * range && lower >= 1.5 * body && lower >= 0.40 * range && lower >= 0.7 * atr && body >= 0.25 * range;
    if is_hammer { return Some(BareSignal { kind: BareKind::Hammer, bar_ts: bar_ts_str(&b.dt), price: b.close, high: b.high, low: b.low }); }
    let is_needle = b.close < b.open && lower <= 0.05 * range && upper >= 1.5 * body && upper >= 0.40 * range && upper >= 0.7 * atr && body >= 0.25 * range;
    if is_needle { return Some(BareSignal { kind: BareKind::Needle, bar_ts: bar_ts_str(&b.dt), price: b.close, high: b.high, low: b.low }); }
    None
}

pub fn bare_expire_ts(trigger_ts: &str) -> String {
    if trigger_ts.len() < 16 { return trigger_ts.to_string(); }
    let y: i32 = trigger_ts[0..4].parse().unwrap_or(2000);
    let m: i32 = trigger_ts[5..7].parse().unwrap_or(1);
    let d: i32 = trigger_ts[8..10].parse().unwrap_or(1);
    let h: i32 = trigger_ts[11..13].parse().unwrap_or(0);
    let min: i32 = trigger_ts[14..16].parse().unwrap_or(0);
    let dt = crate::analyze::model::DT { year: y, month: m, day: d, hour: h, minute: min };
    let exp = dt_add_minutes(&dt, 15);
    bar_ts_str(&exp)
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
            rollover: false,
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

    #[test]
    fn rollover_bar_has_no_trend_flags_and_uses_own_range_for_atr() {
        // 换月 bar 高 120、低 80：若把旧合约前收盘 100 计入 TR 会得到 20，
        // 只用自己的 high-low 时应为 40
        let mut bars = vec![
            bar(100.0, 101.0, 99.0, 100.0),
            bar(80.0, 120.0, 80.0, 110.0),
        ];
        bars[1].rollover = true;
        let atr = crate::analyze::indicators::atr(&bars, 2);
        assert_eq!(atr[1], Some(21.0));
        assert!(!trend_flags(&bars, &atr)[1].0);
        assert!(!trend_flags(&bars, &atr)[1].1);
        assert!(!trend_flags_relaxed(&bars, &atr)[1].0);
        assert!(!trend_flags_relaxed(&bars, &atr)[1].1);
    }

    #[test]
    fn ma_series_starts_after_period() {
        let bars: Vec<Bar> = (1..=25)
            .map(|i| {
                let c = i as f64;
                bar(c, c, c, c)
            })
            .collect();
        let ma = ma_series(&bars, 20);
        assert!(ma[18].is_none());
        assert_eq!(ma[19], Some(10.5));
        assert_eq!(ma[20], Some(11.5));
    }
}
