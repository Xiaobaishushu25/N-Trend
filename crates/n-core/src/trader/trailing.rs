//! Live/shadow exit policy B: breakeven after lot 1, then continuous 0.8R trailing.

pub(super) const TRAIL_ACTIVATE_R: f64 = 1.6;
pub(super) const TRAIL_DISTANCE_R: f64 = 0.8;

/// Called only after the first profit-taking fill. Preserve any tighter saved stop.
pub(super) fn continuous_stop_price(
    fill_price: f64,
    risk: f64,
    sign: f64,
    max_mfe_r: f64,
    current_stop: f64,
) -> f64 {
    let lock_r = if max_mfe_r + 1e-9 >= TRAIL_ACTIVATE_R {
        max_mfe_r - TRAIL_DISTANCE_R
    } else {
        0.0
    };
    let candidate = fill_price + sign * lock_r * risk;
    if sign > 0.0 {
        current_stop.max(candidate)
    } else {
        current_stop.min(candidate)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn eg_peak_5127_trails_by_point_eight_r_in_both_directions() {
        let risk = 55.295;
        let mfe = (5127.0 - 4978.0) / risk;
        let long = continuous_stop_price(4978.0, risk, 1.0, mfe, 5066.472);
        assert!((long - 5082.764).abs() < 1e-9);
        let short = continuous_stop_price(4978.0, risk, -1.0, mfe, 4889.528);
        assert!((short - 4873.236).abs() < 1e-9);
        // A restored tighter stop is never widened, even if its saved MFE is lower.
        assert_eq!(
            continuous_stop_price(4978.0, risk, 1.0, 2.0, 5100.0),
            5100.0
        );
        assert_eq!(
            continuous_stop_price(4978.0, risk, -1.0, 2.0, 4850.0),
            4850.0
        );
    }
}
