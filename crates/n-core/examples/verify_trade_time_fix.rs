//! Public-API regression used for the memory-bounded Linux deployment build.
use n_core::trader::{trade_time::resolve, OvernightSnapshot};
use serde_json::json;

fn broker_dates_use_the_actual_order_and_real_session_calendar() {
    let anchor = n_core::trader::candidates::parse_time("2026-11-16 21:45:59").unwrap();
    for raw in ["20261116", "20261117"] {
        let time = resolve("pp2701", raw, "21:45:59", "20261117", Some(anchor));
        assert_eq!(time.timestamp.as_deref(), Some("2026-11-16 21:45:59"));
        assert_eq!(time.raw_date, raw);
    }
    assert_eq!(
        resolve("au2702", "20261123", "00:30:00", "20261123", None)
            .timestamp
            .as_deref(),
        Some("2026-11-21 00:30:00")
    );
}

fn snapshot(version: u32) -> OvernightSnapshot {
    serde_json::from_value(json!({"id":"s","position_id":"p","symbol":"PP0","instrument_id":"pp2701","direction":"down",
        "volume":1,"fill_price":9087.,"current_stop_price":9154.755,"risk":67.755,"open_ts":"2026-11-16 21:45:59",
        "trading_day":"20261117","checkpoint_type":"night_close","checkpoint_ts":"2026-11-16 22:58:00",
        "checkpoint_last":9083.,"checkpoint_bid":9083.,"checkpoint_ask":9084.,"hypo_exit_price":9084.,"hypo_pnl_pts":3.,"hypo_pnl_r":3./67.755,
        "session_close_price":null,"late_drift_pts":null,"next_open_ts":null,"next_open_price":null,"gap_pts":null,"gap_r":null,
        "gap_penetrated_stop":null,"actual_exit_ts":null,"actual_exit_price":null,"actual_exit_reason":null,
        "actual_realized_pnl_pts":null,"actual_realized_pnl_r":null,"overnight_delta_pts":null,"overnight_delta_r":null,
        "created_at":1794841080_i64,"open_observation_version":version,"open_observation_status":"pending"})).unwrap()
}

fn late_recovery_is_not_a_fake_open_and_contract_backfill_preserves_actual_profit() {
    let mut s = snapshot(1);
    let now = n_core::trader::candidates::parse_time("2026-11-17 10:59:50").unwrap();
    s.observe_next_session(None, now, now, None);
    assert_eq!(s.open_observation_status, "missed_open");
    assert!(s.next_open_price.is_none());
    s.finalize_actual_exit(9040., "2026-11-17 10:59:51", "target_s1", 47., 47. / 67.755);
    assert!(s.backfill_contract_open("pp2701", "2026-11-17 09:00:00", 9000.));
    assert!(s.backfill_contract_close("pp2701", "2026-11-16 23:00:00", 9076.));
    assert_eq!(s.gap_pts, Some(76.));
    assert_eq!(s.open_vs_checkpoint_pts, Some(84.));
    assert_eq!(s.hold_vs_checkpoint_delta_pts, Some(44.));
    assert_eq!(s.actual_realized_pnl_pts, Some(47.));
    assert_eq!(s.overnight_delta_pts, None);
}

fn migration_does_not_mark_a_future_session_as_already_missed() {
    let mut s = snapshot(0);
    let now = n_core::trader::candidates::parse_time("2026-11-17 08:59:00").unwrap();
    s.observe_next_session(None, now, now, None);
    assert_eq!(s.open_observation_status, "pending");
    assert!(!s.execution_gap);
}

fn main() {
    broker_dates_use_the_actual_order_and_real_session_calendar();
    late_recovery_is_not_a_fake_open_and_contract_backfill_preserves_actual_profit();
    migration_does_not_mark_a_future_session_as_already_missed();
    println!("3 public API regressions passed");
}
#[cfg(test)]
mod tests {
    #[test]
    fn public_api_regressions() {
        super::main();
    }
}
