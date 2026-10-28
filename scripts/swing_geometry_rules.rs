//! Shared causal geometry transformations, used only in an independent fork.
use crate::{fetch::kline::Kline, v2::replay::ReplayEvent};
pub fn grid(x: f64, tick: f64, up: bool) -> f64 {
    if up {
        (x / tick - 1e-9).ceil() * tick
    } else {
        (x / tick + 1e-9).floor() * tick
    }
}
pub fn retest_price(ev: &ReplayEvent, sign: f64) -> f64 {
    // Round toward the original breakout: a slightly worse price, never a
    // fractional-tick theoretical fill. This is a market-on-retest trigger.
    grid(ev.entry - sign * 0.25 * ev.risk, ev.tick_size, sign > 0.)
}
pub fn normalize(ev: &mut ReplayEvent) {
    let up = ev.direction == "up";
    ev.entry = grid(ev.entry, ev.tick_size, up);
    ev.stop = grid(ev.stop, ev.tick_size, !up);
    ev.target = grid(ev.target, ev.tick_size, !up);
    ev.risk = (ev.entry - ev.stop).abs();
    ev.rr = if ev.risk > 0. {
        (ev.target - ev.entry).abs() / ev.risk
    } else {
        0.
    };
    ev.evaluation = None;
    ev.outcome = None;
    ev.entry_ts = None;
    ev.entry_price = None;
    ev.trigger_bar_ts = None;
    ev.trigger_features = None;
}
pub fn body_stop(ev: &mut ReplayEvent, warning: &Kline) {
    let up = ev.direction == "up";
    let stop = if up {
        warning.open.min(warning.close) - ev.tick_size
    } else {
        warning.open.max(warning.close) + ev.tick_size
    };
    ev.stop = if up {
        ev.stop.max(stop)
    } else {
        ev.stop.min(stop)
    };
    normalize(ev);
}
pub fn local_target(ev: &mut ReplayEvent, history: &[Kline]) -> Option<String> {
    // Only pivots whose RIGHT confirmation candle closed BEFORE warning close.
    let mut prior: Vec<_> = history
        .iter()
        .filter(|b| b.datetime < ev.warning_ts)
        .collect();
    if prior.len() > 12 {
        prior = prior[prior.len() - 12..].to_vec();
    }
    let up = ev.direction == "up";
    let mut source = None;
    for bs in prior.windows(3) {
        let t =
            chrono::NaiveDateTime::parse_from_str(&bs[1].datetime, "%Y-%m-%d %H:%M:%S").unwrap();
        let a =
            chrono::NaiveDateTime::parse_from_str(&bs[0].datetime, "%Y-%m-%d %H:%M:%S").unwrap();
        let b =
            chrono::NaiveDateTime::parse_from_str(&bs[2].datetime, "%Y-%m-%d %H:%M:%S").unwrap();
        if (t - a).num_minutes() != 15 || (b - t).num_minutes() != 15 {
            continue;
        }
        let pivot = if up {
            bs[1].high > bs[0].high && bs[1].high > bs[2].high
        } else {
            bs[1].low < bs[0].low && bs[1].low < bs[2].low
        };
        let price = if up { bs[1].high } else { bs[1].low };
        if pivot
            && (price - ev.entry) * if up { 1. } else { -1. } > 0.
            && (ev.target - price) * if up { 1. } else { -1. } > 0.
        {
            ev.target = price;
            source = Some(bs[1].datetime.clone());
        }
    }
    normalize(ev);
    source
}
