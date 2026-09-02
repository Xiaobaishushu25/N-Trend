use crate::v2::features::{SetupFeatures, TriggerFeatures};

/// Direction normalization — mirror short samples to long perspective
/// so the model sees a single distribution.
/// For Dir::Down we invert signed quantities.
pub fn normalize_direction(setup: &mut SetupFeatures, trigger: Option<&mut TriggerFeatures>) {
    if setup.direction == "down" {
        // flip signed moves if needed — a_move/b_move are positive by definition, so we keep them
        // but we mark normalized for audit and flip directional trigger features
        setup.normalized = true;
        if let Some(t) = trigger {
            if let Some(v) = t.close_overshoot_r { t.close_overshoot_r = Some(-v); }
            // close_location is mirrored: 1 - loc (high becomes low)
            if let Some(v) = t.close_location { t.close_location = Some(1.0 - v); }
            // chase / body / wick are magnitudes, keep
            t.missing_mask |= 1 << 16; // mark normalized bit
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::v2::features::{SetupFeatures, TriggerFeatures};
    #[test]
    fn normalize_flips_overshoot() {
        let mut s = SetupFeatures{ a_move:10.0,b_move:5.0,a_bars:6,b_bars:3,retracement:0.5,a_speed:1.6,a_move_atr:5.0,b_move_atr:2.0,grade:"A".into(),level:"fine".into(),direction:"down".into(),a_strong_count:2,setup_quality:3.5,trend60_state:"up".into(),warning_close_location:Some(0.2),warning_body_atr:Some(0.5),warning_wick_ratio:Some(1.0),warning_volume_ratio:Some(1.2),normalized:false,missing_mask:0 };
        let mut t = TriggerFeatures{ trigger_bar_ts:"2024-01-01 10:15:00".into(), close_overshoot_r:Some(0.5), close_location:Some(0.8), body_atr:Some(1.0), volume_ratio:Some(1.5), oi_ratio:None, internal_swing_margin_r:Some(0.3), wick_atr:Some(0.2), chase_distance_r:Some(0.5), close_price:100.0, trigger_level:99.5, risk:1.0, missing_mask:0 };
        normalize_direction(&mut s, Some(&mut t));
        assert!(s.normalized);
        assert!((t.close_overshoot_r.unwrap() + 0.5).abs() < 1e-9);
        assert!((t.close_location.unwrap() - 0.2).abs() < 1e-9);
    }
}
