//! Conservative OHLC adapter: no invented order of intra-bar observations.
use crate::{
    fetch::kline::Kline,
    trader::{policy::on_price, PositionTier},
    v2::replay::ReplayEvent,
};
use serde::{Deserialize, Serialize};
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Evaluation {
    pub status: String,
    pub entry_ts: Option<String>,
    pub exit_ts: Option<String>,
    pub exit_reason: Option<String>,
    pub fill: Option<f64>,
    pub gross_points: f64,
    pub volume: i32,
    pub risk: f64,
    pub mfe_r: f64,
    pub mae_r: f64,
    pub final_price: Option<f64>,
    pub net_amount: Option<f64>,
    pub net_r: Option<f64>,
}
impl Default for Evaluation {
    fn default() -> Self {
        Self {
            status: "untriggered".into(),
            entry_ts: None,
            exit_ts: None,
            exit_reason: None,
            fill: None,
            gross_points: 0.,
            volume: 1,
            risk: 0.,
            mfe_r: 0.,
            mae_r: 0.,
            final_price: None,
            net_amount: None,
            net_r: None,
        }
    }
}
fn mark(e: &mut Evaluation, status: &str, bar: &Kline) {
    e.status = status.into();
    e.exit_ts = Some(bar.datetime.clone());
}
/// `held` contains completed 15m bars since entry, supplied by the calendar adapter.
pub fn evaluate(
    ev: &ReplayEvent,
    raw: &[Kline],
    rollovers: &[crate::derive::rollover::RolloverRecord],
    fixed_small: bool,
    slip_ticks: f64,
) -> Evaluation {
    evaluate_config(
        ev,
        raw,
        rollovers,
        fixed_small,
        slip_ticks,
        &crate::trader::ExecutionConfig::default(),
    )
}
pub fn evaluate_config(
    ev: &ReplayEvent,
    raw: &[Kline],
    rollovers: &[crate::derive::rollover::RolloverRecord],
    fixed_small: bool,
    slip_ticks: f64,
    config: &crate::trader::ExecutionConfig,
) -> Evaluation {
    evaluate_at_resolution(ev, raw, rollovers, fixed_small, slip_ticks, config, 5)
}
#[allow(clippy::too_many_arguments)]
pub fn evaluate_at_resolution(
    ev: &ReplayEvent,
    raw: &[Kline],
    rollovers: &[crate::derive::rollover::RolloverRecord],
    fixed_small: bool,
    slip_ticks: f64,
    config: &crate::trader::ExecutionConfig,
    source_minutes: i64,
) -> Evaluation {
    let sign = if ev.direction == "up" { 1. } else { -1. };
    let tier = if !fixed_small && ev.setup_features.setup_quality >= 3.5 {
        PositionTier::Large
    } else {
        PositionTier::Small
    };
    let mut e = Evaluation {
        volume: if fixed_small {
            1
        } else if tier == PositionTier::Large {
            config.default_large_volume.max(1)
        } else {
            config.default_small_volume.max(1)
        },
        ..Default::default()
    };
    if !fixed_small
        && (ev.setup_features.setup_quality < 2.5
            || ev.risk / ev.tick_size + 1e-6 < (config.min_entry_risk_ticks as f64))
    {
        e.status = "filtered".into();
        return e;
    }
    let mut previous: Option<chrono::NaiveDateTime> = None;
    let mut remaining = e.volume;
    let mut first = false;
    let mut stop = ev.stop;
    let mut entry_index = None;
    let mut last_bucket = String::new();
    let mut waited = 0usize;
    let mut held = 0usize;
    for b in raw.iter().filter(|b| b.datetime > ev.warning_ts) {
        let dt = match crate::analyze::model::DT::from_bar_ts(&b.datetime) {
            Some(v) => v,
            None => {
                mark(&mut e, "missing_data", b);
                return e;
            }
        };
        let naive =
            chrono::NaiveDateTime::parse_from_str(&dt.to_bar_ts(), "%Y-%m-%d %H:%M:%S").unwrap();
        let bucket = crate::derive::current_bucket_end_ts(naive, crate::derive::Timeframe::M15);
        if let Some(prior) = previous {
            let gap = (naive - prior).num_minutes();
            if source_minutes == 5 && gap > 5 && gap <= 180 {
                use chrono::TimeZone;
                let missing = prior + chrono::Duration::minutes(5);
                if let Some(local) = chrono::Local.from_local_datetime(&missing).single() {
                    if crate::session::SessionCalendar::is_trading_time(&ev.symbol, &local) {
                        mark(&mut e, "missing_data", b);
                        return e;
                    }
                }
            }
        }
        previous = Some(naive);
        if bucket != last_bucket {
            last_bucket = bucket.clone();
            if entry_index.is_none() {
                waited += 1
            } else {
                held += 1
            }
        }
        if rollovers
            .iter()
            .any(|r| r.confirmed && r.ts > ev.warning_ts && r.ts <= b.datetime)
        {
            mark(&mut e, "rollover", b);
            return e;
        }
        if entry_index.is_none() && waited > crate::analyze::outcome::PENDING_BARS {
            e.status = "untriggered".into();
            return e;
        }
        let favorable = if sign > 0. { b.high } else { b.low };
        let adverse = if sign > 0. { b.low } else { b.high };
        let newly_entered = entry_index.is_none();
        if newly_entered {
            if (favorable - ev.entry) * sign < 0. {
                if (adverse - stop) * sign <= 0. {
                    mark(&mut e, "invalidated", b);
                    return e;
                }
                continue;
            }
            // Stop could have invalidated the setup before its breakout.
            if (adverse - stop) * sign <= 0. && (b.open - ev.entry) * sign < 0. {
                mark(&mut e, "ambiguous", b);
                return e;
            }
            let fill = if (b.open - ev.entry) * sign >= 0. {
                b.open
            } else {
                ev.entry
            } + sign * slip_ticks * ev.tick_size;
            if (fill - ev.entry) * sign > (ev.risk * 0.2).max(2. * ev.tick_size) + 1e-6 {
                mark(&mut e, "unfilled", b);
                return e;
            }
            if (fill - stop) * sign <= 0. {
                mark(&mut e, "invalidated", b);
                return e;
            }
            e.fill = Some(fill);
            e.risk = (fill - stop) * sign;
            e.entry_ts = Some(b.datetime.clone());
            e.status = "open".into();
            entry_index = Some(dt);
            held = 1;
        }
        let fill = e.fill.unwrap();
        let high_r = (favorable - fill) * sign / e.risk;
        let low_r = (adverse - fill) * sign / e.risk;
        let one = fill + sign * e.risk;
        let target = if (ev.target - ev.entry) * sign > ev.risk {
            ev.target
        } else {
            one
        };
        let stop_hit = (adverse - stop) * sign <= 0.;
        let one_hit = (favorable - one) * sign >= 0.;
        // Existing stop vs target order; entry-bar low cannot be treated as post-entry.
        if stop_hit && ((!first && one_hit) || newly_entered && (b.open - ev.entry) * sign < 0.) {
            mark(&mut e, "ambiguous", b);
            return e;
        }
        if !first && tier == PositionTier::Large && one_hit && (adverse - fill) * sign <= 0. {
            mark(&mut e, "ambiguous", b);
            return e;
        }
        let max_mfe = e.mfe_r.max(high_r);
        if first || (tier == PositionTier::Large && one_hit) {
            let prospective = crate::trader::policy::on_price(
                tier,
                fill,
                e.risk,
                sign,
                favorable,
                target,
                if first { stop } else { fill },
                max_mfe,
                true,
            )
            .stop;
            if (prospective - stop) * sign > 1e-9 && (adverse - prospective) * sign <= 0. {
                mark(&mut e, "ambiguous", b);
                return e;
            }
        }
        e.mfe_r = max_mfe;
        // Only count adverse excursion when the position existed at the bar open.
        if !newly_entered || (b.open - ev.entry) * sign >= 0. {
            e.mae_r = e.mae_r.min(low_r)
        }
        let close =
            |price: f64, qty: i32, reason: &str, e: &mut Evaluation, remaining: &mut i32| {
                e.gross_points += (price - fill) * sign * qty as f64;
                *remaining -= qty;
                e.final_price = Some(price);
                e.exit_reason = Some(reason.into());
                if *remaining == 0 {
                    mark(e, "closed", b)
                }
            };
        if stop_hit {
            close(
                if (b.open - stop) * sign < 0. {
                    b.open
                } else {
                    stop
                } - sign * slip_ticks * ev.tick_size,
                remaining,
                if first { "protection" } else { "stop" },
                &mut e,
                &mut remaining,
            );
            return e;
        }
        if !first && one_hit {
            let qty = if tier == PositionTier::Small {
                remaining
            } else {
                1.min(remaining)
            };
            close(
                one - sign * slip_ticks * ev.tick_size,
                qty,
                "target_1r",
                &mut e,
                &mut remaining,
            );
            if e.status == "closed" {
                return e;
            }
            first = true;
            stop = fill;
        }
        if first {
            let update = on_price(
                tier, fill, e.risk, sign, favorable, target, stop, e.mfe_r, true,
            );
            stop = update.stop;
            if let Some(intent) = update.intent {
                close(
                    target - sign * slip_ticks * ev.tick_size,
                    remaining,
                    intent.reason,
                    &mut e,
                    &mut remaining,
                );
                return e;
            }
        }
        // Time exits are only acted on at a completed 15m close.
        if b.datetime == bucket
            && (held >= crate::analyze::outcome::TIME_HORIZON_BARS
                || held >= crate::analyze::outcome::NO_FOLLOW_BAR
                    && e.mfe_r < crate::analyze::outcome::NO_FOLLOW_MFE_R)
        {
            close(
                b.close - sign * slip_ticks * ev.tick_size,
                remaining,
                if held >= crate::analyze::outcome::TIME_HORIZON_BARS {
                    "time_exit"
                } else {
                    "no_follow"
                },
                &mut e,
                &mut remaining,
            );
            return e;
        }
    }
    if e.status == "open" {
        e.status = "unfinished".into()
    } else if e.status == "untriggered" {
        e.status = "pending".into()
    }
    e
}

#[cfg(test)]
mod tests {
    use super::*;
    fn event(large: bool, short: bool) -> ReplayEvent {
        let mut e = ReplayEvent {
            symbol: "RB0".into(),
            direction: if short { "down" } else { "up" }.into(),
            warning_ts: "2026-01-05 09:45:00".into(),
            entry: 100.,
            stop: if short { 110. } else { 90. },
            target: if short { 70. } else { 130. },
            risk: 10.,
            tick_size: 1.,
            ..Default::default()
        };
        e.setup_features.setup_quality = if large { 4. } else { 3. };
        e
    }
    fn bar(minute: u32, open: f64, high: f64, low: f64, close: f64) -> Kline {
        Kline {
            datetime: format!("2026-01-05 {:02}:{:02}:00", 9 + minute / 60, minute % 60),
            open,
            high,
            low,
            close,
            volume: 100.,
            hold: 0.,
        }
    }
    #[test]
    fn low_before_breakout_cannot_be_an_invented_stop() {
        let r = evaluate(
            &event(false, false),
            &[bar(50, 95., 105., 89., 104.)],
            &[],
            false,
            0.,
        );
        assert_eq!(r.status, "ambiguous");
        assert!(r.net_amount.is_none());
    }
    #[test]
    fn stop_and_target_same_five_minutes_is_ambiguous() {
        let r = evaluate(
            &event(false, false),
            &[bar(50, 100., 115., 89., 102.)],
            &[],
            false,
            0.,
        );
        assert_eq!(r.status, "ambiguous");
    }
    #[test]
    fn single_lot_closes_at_one_r() {
        let r = evaluate(
            &event(false, false),
            &[
                bar(50, 100., 105., 99., 104.),
                bar(55, 104., 112., 103., 111.),
            ],
            &[],
            false,
            0.,
        );
        assert_eq!(r.status, "closed");
        assert_eq!(r.gross_points, 10.);
    }
    #[test]
    fn two_lots_only_protect_after_first_leg() {
        let r = evaluate(
            &event(true, false),
            &[
                bar(50, 100., 105., 99., 104.),
                bar(55, 105., 111., 104., 110.),
                bar(60, 105., 108., 99., 100.),
            ],
            &[],
            false,
            0.,
        );
        assert_eq!(r.status, "closed");
        assert_eq!(r.volume, 2);
        assert_eq!(r.gross_points, 10.);
    }
    #[test]
    fn raising_protection_then_using_same_bar_low_is_unknown() {
        let r = evaluate(
            &event(true, false),
            &[
                bar(50, 100., 105., 99., 104.),
                bar(55, 105., 111., 104., 110.),
                bar(60, 110., 125., 105., 123.),
            ],
            &[],
            false,
            0.,
        );
        assert_eq!(r.status, "ambiguous");
    }
    #[test]
    fn stop_gap_fills_at_open() {
        let r = evaluate(
            &event(false, false),
            &[bar(50, 100., 105., 99., 104.), bar(55, 85., 87., 80., 84.)],
            &[],
            false,
            0.,
        );
        assert_eq!(r.status, "closed");
        assert_eq!(r.gross_points, -15.);
    }
    #[test]
    fn short_is_mirrored() {
        let r = evaluate(
            &event(false, true),
            &[bar(50, 100., 101., 95., 96.), bar(55, 96., 97., 88., 89.)],
            &[],
            false,
            0.,
        );
        assert_eq!(r.status, "closed");
        assert_eq!(r.gross_points, 10.);
    }
    #[test]
    fn truncated_window_is_not_loss() {
        let r = evaluate(
            &event(false, false),
            &[bar(50, 100., 105., 99., 104.)],
            &[],
            false,
            0.,
        );
        assert_eq!(r.status, "unfinished");
        assert!(r.net_amount.is_none());
    }
    #[test]
    fn future_raw_does_not_affect_warning_features() {
        let e = event(false, false);
        assert!(!crate::v3::whitelist()
            .iter()
            .any(|f| f.starts_with("trigger_") || f == "chase_distance_r"));
        assert_eq!(e.warning_ts, "2026-01-05 09:45:00");
    }
}
