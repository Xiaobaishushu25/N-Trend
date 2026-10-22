//! Shared pure quote policy. Orders and fills remain the adapter's responsibility.
use super::{trailing::continuous_stop_price, PositionTier};
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct ExitIntent {
    pub reason: &'static str,
    pub all: bool,
}
#[derive(Clone, Copy, Debug)]
pub struct PolicyUpdate {
    pub stop: f64,
    pub intent: Option<ExitIntent>,
}
#[allow(clippy::too_many_arguments)]
pub fn on_price(
    tier: PositionTier,
    fill: f64,
    risk: f64,
    sign: f64,
    price: f64,
    target: f64,
    old_stop: f64,
    max_mfe: f64,
    first_closed: bool,
) -> PolicyUpdate {
    let mut stop = old_stop;
    let reason = if (price - stop) * sign <= 0.0 {
        Some(ExitIntent {
            reason: if !first_closed {
                "stop"
            } else if (stop - fill) * sign > 0.0 {
                "trailing"
            } else {
                "breakeven"
            },
            all: true,
        })
    } else if !first_closed && (price - fill) * sign / risk >= 1.0 {
        Some(ExitIntent {
            reason: "target_1r",
            all: tier == PositionTier::Small,
        })
    } else if first_closed {
        stop = continuous_stop_price(fill, risk, sign, max_mfe, old_stop);
        if (price - stop) * sign <= 0.0 {
            Some(ExitIntent {
                reason: if (stop - fill) * sign > 0.0 {
                    "trailing"
                } else {
                    "breakeven"
                },
                all: true,
            })
        } else if (target - fill) * sign > risk + 1e-6 && (price - target) * sign >= 0.0 {
            Some(ExitIntent {
                reason: "target_s1",
                all: true,
            })
        } else {
            None
        }
    } else {
        None
    };
    PolicyUpdate {
        stop,
        intent: reason,
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn protection_requires_confirmed_first_fill() {
        let before = on_price(
            PositionTier::Large,
            100.,
            10.,
            1.,
            117.,
            130.,
            90.,
            1.7,
            false,
        );
        assert_eq!(before.stop, 90.);
        assert!(!before.intent.unwrap().all);
        let after = on_price(
            PositionTier::Large,
            100.,
            10.,
            1.,
            117.,
            130.,
            100.,
            1.7,
            true,
        );
        assert_eq!(after.stop, 109.);
        assert!(after.intent.is_none());
    }
    #[test]
    fn mirrored_short_and_long() {
        for sign in [1., -1.] {
            let u = on_price(
                PositionTier::Large,
                100.,
                10.,
                sign,
                100. + sign * 9.,
                100. + sign * 30.,
                100.,
                1.7,
                true,
            );
            assert_eq!(u.intent.unwrap().reason, "trailing");
        }
    }
}
