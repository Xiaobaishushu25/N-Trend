//! P3 through the real executor, with offline counter callbacks and paper fills.
use super::*;
use crate::trader::{
    broker::*,
    candidates::{
        self,
        p3::{self, ExitMode, P3Parameters, P3Strategy},
    },
};
use std::collections::BTreeMap;

#[tokio::test]
async fn p3_delayed_first_fill_uses_latest_quote_and_exits_an_already_crossed_breakeven() {
    let counter = Counter::new();
    let e = engine(&counter, ExitMode::HybridS1).await;
    let (id, open, fill) = opened(&counter, &e, false).await;
    age(&e).await;
    quote(&e, 1030.).await;
    let first = counter.inserts()[1].clone();
    quote(&e, 999.).await;
    assert!(!e.state.lock().await.positions[&id].lot1_closed);
    ready(
        &e,
        vec![
            order(&open, "all_traded", 2),
            order(&first, "all_traded", 1),
        ],
        vec![fill, trade(&first, "delayed-first", 1, 1030.)],
    )
    .await;
    assert_eq!(e.state.lock().await.positions[&id].volume, 1);
    assert_eq!(e.state.lock().await.closes[&id].reason, "breakeven");
    e.drive().await.unwrap();
    assert_eq!(counter.inserts().len(), 3);
    assert_eq!(counter.inserts()[2]["volume"], 1);
    assert!(counter.inserts()[2]["price"].as_f64().unwrap() < 1002.);
}

#[tokio::test]
async fn p3_fractional_cost_breakeven_keeps_grade_one_and_valid_tick_price() {
    let counter = Counter::new();
    let e = engine(&counter, ExitMode::HybridS1).await;
    send(&e, signal("fractional", false, 20., 30.)).await;
    quote(&e, 1000.).await;
    age(&e).await;
    quote(&e, 1001.).await;
    let open = counter.inserts()[0].clone();
    let mut fills = vec![trade(&open, "one", 1, 1001.), trade(&open, "two", 1, 1002.)];
    ready(&e, vec![order(&open, "all_traded", 2)], fills.clone()).await;
    age(&e).await;
    quote(&e, 1030.).await;
    let first = counter.inserts()[1].clone();
    fills.push(trade(&first, "first-s1", 1, 1030.));
    ready(
        &e,
        vec![
            order(&open, "all_traded", 2),
            order(&first, "all_traded", 1),
        ],
        fills,
    )
    .await;
    let p = e.list_positions().await.remove(0);
    assert_eq!(p.fill_price, 1001.5);
    assert_eq!(p.current_stop_price, 1002.);
    assert_eq!(p.trailing_grade, 1);
    quote(&e, 1002.).await;
    assert_eq!(e.state.lock().await.closes[&p.id].reason, "breakeven");
    assert_eq!(e.state.lock().await.positions[&p.id].risk, 21.5);
}

fn parameters(mode: ExitMode) -> P3Parameters {
    let mut p = P3Parameters::reference_snapshot();
    p.exit_mode = mode;
    p.fees.products.insert(
        "PB".into(),
        serde_json::from_value(json!({"roundtrip_fixed_yuan_per_lot":6.})).unwrap(),
    );
    p
}
fn registry() -> Arc<StrategyRegistry> {
    let mut r = StrategyRegistry::default();
    r.register_strategy(p3::ID, p3::VERSION, |p| {
        Ok(Box::new(
            P3Strategy::new(serde_json::from_value(p.clone())?)?
                .with_test_calendar_time(candidates::parse_time("2026-10-21 10:00:00")?),
        ))
    });
    Arc::new(r)
}
async fn engine(counter: &Counter, mode: ExitMode) -> ExecutionEngine {
    let mut e = counter.engine(ExecutionMode::FullAuto, 2);
    e.registry = registry();
    let mut cfg = e.get_config().await;
    cfg.slippage_ticks = 0;
    e.configure_strategy(p3::definition(parameters(mode)), cfg)
        .await
        .unwrap();
    ready(&e, vec![], vec![]).await;
    e
}
fn signal(key: &str, short: bool, risk: f64, target_risk: f64) -> Value {
    let now = chrono::Utc::now().timestamp();
    let warning = candidates::label(now - now % 900 - 900);
    let sign = if short { -1. } else { 1. };
    json!({"symbol":"PB0","instrument_id":"pb2611","direction":if short{"down"}else{"up"},
        "s0_ts":key,"s1_ts":"s1","s2_ts":"s2","warning_ts":warning,"created_at_epoch":now,
        "entry":1000.,"stop":1000.-sign*risk,"target":1000.+sign*target_risk,"risk":risk,
        "tick_size":1.,"multiplier":5.,"entry_score":2.5,
        "warning_context":{"as_of":warning,"complete":true,"atr14":risk}})
}
async fn send(e: &ExecutionEngine, payload: Value) {
    e.dispatch(StrategyEvent::Signal {
        source: N_STRATEGY.into(),
        payload,
    })
    .await
    .unwrap();
}
async fn opened(counter: &Counter, e: &ExecutionEngine, short: bool) -> (String, Value, Value) {
    send(e, signal("trade", short, 20., 30.)).await;
    quote(e, 1000.).await;
    assert!(counter.inserts().is_empty());
    age(e).await;
    quote(e, if short { 999. } else { 1001. }).await;
    let open = counter.inserts()[0].clone();
    assert_eq!(open["volume"], 2);
    let fill = trade(&open, "opened", 2, if short { 998. } else { 1002. });
    ready(e, vec![order(&open, "all_traded", 2)], vec![fill.clone()]).await;
    let id = e.list_positions().await[0].id.clone();
    (id, open, fill)
}

#[tokio::test]
async fn p3_fixed_mode_uses_money_not_score_for_zero_one_two_lots() {
    for (risk, qty) in [(20., 2), (60., 1), (120., 0)] {
        let counter = Counter::new();
        let e = engine(&counter, ExitMode::FixedS1).await;
        send(&e, signal("sizing", false, risk, 200.)).await;
        quote(&e, 1000.).await;
        age(&e).await;
        quote(&e, 1001.).await;
        if qty == 0 {
            assert!(counter.inserts().is_empty());
            assert!(e.list_conditions().await.is_empty());
            let diagnostics = e.strategy_diagnostics().await;
            assert!(diagnostics["states"]
                .as_object()
                .unwrap()
                .values()
                .any(|s| s["decisions"]
                    .as_object()
                    .unwrap()
                    .values()
                    .any(|d| d["reason"] == "risk_budget_exceeded")));
        } else {
            assert_eq!(counter.inserts()[0]["volume"], qty)
        }
        assert!(!e.paused().await);
    }
}

#[tokio::test]
async fn p3_fixed_mode_closes_all_at_original_s1_in_both_directions() {
    for short in [false, true] {
        let counter = Counter::new();
        let e = engine(&counter, ExitMode::FixedS1).await;
        let (id, open, fill) = opened(&counter, &e, short).await;
        let target = if short { 970. } else { 1030. };
        assert_eq!(e.list_positions().await[0].target_price, target);
        age(&e).await;
        quote(&e, target).await;
        let close = counter.inserts()[1].clone();
        assert_eq!(close["volume"], 2);
        ready(
            &e,
            vec![
                order(&open, "all_traded", 2),
                order(&close, "all_traded", 2),
            ],
            vec![fill, trade(&close, "exit", 2, target)],
        )
        .await;
        assert!(e.state.lock().await.positions[&id].is_closed);
        assert_eq!(e.get_trade_history().await[0].exit_reason, "target_s1");
    }
}

#[tokio::test]
async fn p3_hybrid_confirms_first_s1_then_tracks_and_restores_frozen_mode() {
    for short in [false, true] {
        let counter = Counter::new();
        let mut e = engine(&counter, ExitMode::HybridS1).await;
        let (id, open, fill) = opened(&counter, &e, short).await;
        let sign = if short { -1. } else { 1. };
        let cost = if short { 998. } else { 1002. };
        age(&e).await;
        quote(&e, 1000. + sign * 30.).await;
        let first = counter.inserts()[1].clone();
        assert_eq!(first["volume"], 1);
        quote(&e, 1000. + sign * 33.).await;
        {
            let s = e.state.lock().await;
            assert!(!s.positions[&id].lot1_closed);
            assert_eq!(s.positions[&id].current_stop_price, 1000. - sign * 20.);
        }
        let orders = vec![
            order(&open, "all_traded", 2),
            order(&first, "all_traded", 1),
        ];
        let fills = vec![fill, trade(&first, "first-s1", 1, 1000. + sign * 30.)];
        ready(&e, orders.clone(), fills.clone()).await;
        {
            let s = e.state.lock().await;
            assert!(s.positions[&id].lot1_closed);
            assert_eq!(s.positions[&id].current_stop_price, cost);
            assert!(!s.closes.contains_key(&id));
        }
        // Configuring mode A cannot replace a held position's frozen mode B.
        let revision = e.state.lock().await.positions[&id]
            .origin
            .strategy_version
            .clone();
        e.set_definition(p3::definition(parameters(ExitMode::FixedS1)))
            .await
            .unwrap();
        drop(e);
        e = counter.engine(ExecutionMode::FullAuto, 2);
        e.registry = registry();
        ready(&e, orders, fills).await;
        quote(&e, 1000. + sign * 40.).await;
        let stop = e.state.lock().await.positions[&id].current_stop_price;
        assert!((stop - cost) * sign > 0.);
        assert_eq!(
            e.state.lock().await.positions[&id].origin.strategy_version,
            revision
        );
        assert_eq!(e.state.lock().await.positions[&id].volume, 1);
        quote(&e, stop - sign).await;
        assert_eq!(counter.inserts()[2]["volume"], 1);
        assert_eq!(e.state.lock().await.closes[&id].reason, "trailing");
    }
}

#[tokio::test]
async fn p3_one_actual_lot_at_s1_never_waits_for_a_second_lot() {
    let counter = Counter::new();
    let e = engine(&counter, ExitMode::HybridS1).await;
    send(&e, signal("partial", false, 20., 30.)).await;
    quote(&e, 1000.).await;
    age(&e).await;
    quote(&e, 1001.).await;
    let open = counter.inserts()[0].clone();
    let first = trade(&open, "one-entry", 1, 1002.);
    ready(
        &e,
        vec![order(&open, "part_traded", 1)],
        vec![first.clone()],
    )
    .await;
    quote(&e, 1030.).await;
    let close = counter.inserts()[1].clone();
    assert_eq!(close["volume"], 1);
    ready(
        &e,
        vec![order(&open, "canceled", 1), order(&close, "all_traded", 1)],
        vec![first, trade(&close, "single-s1", 1, 1030.)],
    )
    .await;
    assert!(e.list_positions().await[0].is_closed);
    assert!(!e.list_positions().await[0].lot1_closed);
    age(&e).await;
    quote(&e, 1040.).await;
    assert_eq!(counter.inserts().len(), 2);
}

#[tokio::test]
async fn p3_proposed_price_reduces_lots_before_any_counter_submission() {
    let counter = Counter::new();
    let e = engine(&counter, ExitMode::FixedS1).await;
    send(&e, signal("repriced", false, 56., 200.)).await;
    assert_eq!(e.list_conditions().await[0].volume, 2); // 296 yuan per forecast lot
    quote(&e, 1000.).await;
    age(&e).await;
    quote(&e, 1002.).await;
    assert_eq!(counter.inserts().len(), 1);
    assert_eq!(counter.inserts()[0]["volume"], 1); // proposed ask -> 306 yuan per lot
    assert_eq!(e.list_conditions().await[0].volume, 1);
}

#[tokio::test]
async fn p3_point_value_mismatch_cannot_open_and_invalid_execution_timeout_is_rejected() {
    let counter = Counter::new();
    let e = engine(&counter, ExitMode::FixedS1).await;
    let mut bad = signal("metadata", false, 20., 200.);
    bad["multiplier"] = json!(10.);
    send(&e, bad).await;
    quote(&e, 1000.).await;
    age(&e).await;
    quote(&e, 1001.).await;
    assert!(counter.inserts().is_empty());
    assert!(e.paused().await);
    assert!(e
        .state
        .lock()
        .await
        .strategy_error
        .as_deref()
        .unwrap()
        .contains("点值"));
    let before = e.definition().await.revision;
    let mut cfg = e.get_config().await;
    cfg.order_wait_seconds = 5;
    assert!(e
        .configure_strategy(p3::definition(parameters(ExitMode::FixedS1)), cfg)
        .await
        .is_err());
    assert_eq!(e.definition().await.revision, before);
}

#[tokio::test]
async fn p3_main_contract_roll_cancels_stale_pending_plan() {
    let counter = Counter::new();
    let e = engine(&counter, ExitMode::FixedS1).await;
    send(&e, signal("roll", false, 20., 30.)).await;
    let mut q = ExecutionQuote {
        instrument_id: "pb2612".into(),
        exchange_id: "SHFE".into(),
        underlying_symbol: "PB0".into(),
        last: 1000.,
        bid: 999.,
        ask: 1001.,
        tick: 1.,
        lower: 500.,
        upper: 2000.,
        timestamp: chrono::Utc::now().timestamp(),
    };
    e.on_quotes([("PB0".into(), q.clone())].into_iter().collect())
        .await
        .unwrap();
    assert_eq!(e.list_conditions().await[0].note, "contract_rollover");
    q.instrument_id = "pb2611".into();
    e.on_quotes([("PB2611".into(), q)].into_iter().collect())
        .await
        .unwrap();
    e.drive().await.unwrap();
    assert!(counter.inserts().is_empty());
}

#[tokio::test]
async fn p3_paper_s1_account_balance_matches_gross_less_actual_fees() {
    let counter = Counter::new();
    let paper = Arc::new(
        PaperBroker::open(
            "p3-paper".into(),
            PaperConfig {
                initial_balance: 50000.,
                contracts: BTreeMap::from([(
                    "PB2611".into(),
                    PaperContract {
                        multiplier: 5.,
                        margin_rate: 0.1,
                        open_fee_per_lot: 3.,
                        close_fee_per_lot: 3.,
                        close_today_fee_per_lot: 3.,
                    },
                )]),
            },
            counter.directory.join("paper.json"),
        )
        .unwrap(),
    );
    let mut e = ExecutionEngine::with_config_path(
        BrokerClient::Paper(paper.clone()),
        ExecutionConfig {
            mode: ExecutionMode::FullAuto,
            slippage_ticks: 0,
            ..Default::default()
        },
        Some(counter.directory.join("trader_config.json")),
    );
    e.registry = registry();
    e.set_definition(p3::definition(parameters(ExitMode::FixedS1)))
        .await
        .unwrap();
    e.reconcile(&paper.snapshot()).await.unwrap();
    send(&e, signal("paper", false, 20., 30.)).await;
    for price in [1000., 1001., 1030.] {
        let q = ExecutionQuote {
            instrument_id: "pb2611".into(),
            exchange_id: "SHFE".into(),
            underlying_symbol: "PB0".into(),
            last: price,
            bid: price - 1.,
            ask: price + 1.,
            tick: 1.,
            lower: 500.,
            upper: 2000.,
            timestamp: chrono::Utc::now().timestamp(),
        };
        let batch = vec![("PB0".into(), q)];
        paper.on_quotes(&batch).unwrap();
        e.reconcile(&paper.snapshot()).await.unwrap();
        age(&e).await;
        e.on_quote_batch(batch).await.unwrap();
        e.reconcile(&paper.snapshot()).await.unwrap();
    }
    let history = e.get_trade_history().await;
    assert_eq!(history.iter().map(|t| t.volume).sum::<i32>(), 2);
    let gross = history.iter().map(|t| t.realized_pnl_amount).sum::<f64>();
    assert_eq!(
        paper.snapshot()["account"]["balance"].as_f64().unwrap() - 50000.,
        gross - 12.
    );
    assert!(e.list_positions().await[0].is_closed);
}
