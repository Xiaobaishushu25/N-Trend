//! Exercise the portfolio gate through the real executor, with no broker access.
use super::*;
use crate::trader::{
    b5,
    broker::{PaperBroker, PaperConfig, PaperContract},
};
use serde_json::json;
use std::collections::BTreeMap;

fn plan(symbol: &str, key: &str) -> EntryPlan {
    let now = chrono::Utc::now().timestamp();
    let instrument = format!(
        "{}2701",
        crate::session::SessionCalendar::contract_prefix(symbol).to_lowercase()
    );
    let warning = crate::trader::candidates::label(now - 60);
    EntryPlan {
        signal_id: key.into(),
        symbol: symbol.into(),
        instrument_id: instrument.clone(),
        direction: Direction::Long,
        reference_price: 1000.,
        trigger: EntryTrigger::Breakout { price: 1000. },
        stop_price: Some(980.),
        target_price: Some(1100.),
        risk_per_unit: 20.,
        requested_volume: 1,
        created_at: now - 10,
        expires_at: None,
        reason: "test".into(),
        evidence: json!({"warning_ts":warning,"entry_score":3.,"b5":{"key":key,"cluster":format!("{symbol}|up|a|b"),
            "symbol":symbol,"instrument":instrument,"direction":"long","warning_ts":warning,"available_at":now-10,
            "entry":1000.,"stop":980.,"target":1100.,"risk":20.,"tick":1.,"multiplier":10.,"score":3.,
            "hour":0.,"confirm_minute":false,"body_net":10.,"fees":{"roundtrip_fixed_yuan_per_lot":6.}}}),
    }
}

#[tokio::test]
async fn unknown_group_occupancy_survives_executor_restart_and_rejects_third_trigger() {
    let root = std::env::temp_dir().join(format!("ntrend-b5-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir_all(&root).unwrap();
    let broker = Arc::new(
        PaperBroker::open(
            "b5-test".into(),
            PaperConfig {
                initial_balance: 50000.,
                contracts: BTreeMap::from([(
                    "V2701".into(),
                    PaperContract {
                        multiplier: 5.,
                        margin_rate: 0.1,
                        open_fee_per_lot: 1.,
                        close_fee_per_lot: 1.,
                        close_today_fee_per_lot: 1.,
                    },
                )]),
            },
            root.join("paper.json"),
        )
        .unwrap(),
    );
    let config = ExecutionConfig {
        mode: ExecutionMode::FullAuto,
        slippage_ticks: 0,
        ..Default::default()
    };
    let path = root.join("trader_config.json");
    let e = ExecutionEngine::with_config_path(
        BrokerClient::Paper(broker.clone()),
        config.clone(),
        Some(path.clone()),
    );
    e.bind("b5-test", "b5-test").await.unwrap();
    e.set_definition(b5::definition()).await.unwrap();
    e.pause(false).await.unwrap();
    let d = e.definition().await;
    for (symbol, key) in [("EG0", "eg"), ("PP0", "pp")] {
        let id = e
            .register_plan(plan(symbol, key), d.clone(), config.clone())
            .await
            .unwrap();
        let mut s = e.state.lock().await;
        let o = s.conditions.get_mut(&id).unwrap();
        o.state = ConditionState::Unknown;
        o.trigger_ts = Some(crate::trader::candidates::label(
            chrono::Utc::now().timestamp() - 30,
        ));
        e.save(&mut s).unwrap();
    }
    drop(e);
    let e = ExecutionEngine::with_config_path(
        BrokerClient::Paper(broker.clone()),
        config.clone(),
        Some(path),
    );
    e.bind("b5-test", "b5-test").await.unwrap();
    e.pause(false).await.unwrap();
    let id = e
        .register_plan(plan("V0", "third"), e.definition().await, config)
        .await
        .unwrap();
    let q = ExecutionQuote {
        instrument_id: "v2701".into(),
        exchange_id: "DCE".into(),
        underlying_symbol: "DCE.v2701".into(),
        last: 1000.,
        bid: 999.,
        ask: 1001.,
        tick: 1.,
        lower: 500.,
        upper: 2000.,
        timestamp: chrono::Utc::now().timestamp(),
    };
    e.on_quotes(HashMap::from([("V0".into(), q)]))
        .await
        .unwrap();
    let s = e.state.lock().await;
    assert_eq!(s.conditions[&id].state, ConditionState::Rejected);
    assert!(s.conditions[&id].note.contains("sector_counter_slots_full"));
    assert!(
        s.orders.is_empty(),
        "portfolio rejection must precede any broker order"
    );
    assert_eq!(
        s.conditions
            .values()
            .filter(|o| o.state == ConditionState::Unknown)
            .count(),
        2
    );
    drop(s);
    drop(e);
    drop(broker);
    std::fs::remove_dir_all(root).unwrap();
}
