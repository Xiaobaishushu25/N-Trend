use super::*;
use serde_json::json;

#[test]
fn legacy_migration_repairs_calendar_dates_without_replaying_money_or_fabricating_mfe() {
    let submitted = super::super::candidates::parse_time("2026-11-16 21:45:59").unwrap();
    let state = json!({
        "unknown_future_field":{"preserve":true},
        "strategy_states":{"rev":{"holding":{}}},
        "positions":{"p":{"id":"p","condition_order_id":"p","symbol":"PP0","instrument_id":"pp2701",
            "direction":"down","tier":"small","fill_price":9087.,"initial_stop_price":9154.755,
            "target_price":9059.,"risk":67.755,"volume":1,"open_ts":"20261117 21:45:59",
            "bars_held":5,"max_mfe_r":0.69,"max_mae_r":-0.57,"trailing_grade":0,
            "current_stop_price":9154.755,"lot1_closed":false,"is_closed":false,
            "exit_price":null,"exit_reason":null,"realized_pnl_pts":null,"r_multiple":null}},
        "orders":{"o":{"request":{"instrument_id":"pp2701","direction":"sell","offset":"open","price":9087.,"volume":1},
            "condition_id":"p","reason":null,"phase":"Terminal","reported_filled":1,"filled":1,
            "action_at":submitted,"submitted_at":submitted,"trading_day":"20261117","note":"done"}},
        "history":[{"id":"20261117:DCE:pp2701:buy:123","condition_order_id":"p","symbol":"PP0","instrument_id":"pp2701",
            "direction":"down","tier":"small","volume":1,"fill_price":9087.,"exit_price":9040.,"risk":67.755,
            "initial_stop_price":9154.755,"target_price":9059.,"open_ts":"20261117 21:45:59","exit_ts":"20261117 22:59:11",
            "exit_reason":"target_s1","realized_pnl_pts":47.,"realized_pnl_amount":235.,"r_multiple":47./67.755,
            "max_mfe_r":0.69,"max_mae_r":-0.57,"bars_held":5}]
    });
    let now = super::super::candidates::parse_time("2026-11-17 15:00:00").unwrap();
    let repaired = normalize_execution_archive(state.clone(), now, &[]).unwrap();
    assert_eq!(repaired["positions"]["p"]["open_ts"], "2026-11-16 21:45:59");
    assert_eq!(repaired["history"][0]["open_ts"], "2026-11-16 21:45:59");
    assert_eq!(repaired["history"][0]["exit_ts"], "2026-11-16 22:59:11");
    for key in [
        "realized_pnl_pts",
        "realized_pnl_amount",
        "r_multiple",
        "exit_price",
        "volume",
    ] {
        assert_eq!(repaired["history"][0][key], state["history"][0][key]);
    }
    assert_eq!(repaired["orders"], state["orders"]);
    assert_eq!(
        repaired["unknown_future_field"],
        state["unknown_future_field"]
    );
    assert_eq!(
        repaired["trade_times"]["legacy-position:p"]["raw_date"],
        "20261117"
    );
    assert_eq!(
        repaired["strategy_states"]["rev"]["holding"]["p"]["no_follow_checked"],
        true
    );
    assert_eq!(
        repaired,
        normalize_execution_archive(repaired.clone(), now, &[]).unwrap()
    );
}
