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
async fn p3_timeout_distinguishes_a_delayed_report_from_an_actual_late_fill() {
    for (risk, terminal_status, trade_time, market_gap, expected_exit) in [
        (60., "all_traded", "10:00:00", false, None),
        (20., "canceled", "10:00:02", false, None),
        (
            60.,
            "all_traded",
            "10:00:03",
            false,
            Some("late_entry_fill"),
        ),
        (60., "all_traded", "", false, Some("late_entry_fill")),
        (60., "all_traded", "10:00:00", true, Some("market_gap")),
    ] {
        let counter = Counter::new();
        let e = engine(&counter, ExitMode::HybridS1).await;
        send(&e, signal("entry-timeout", true, risk, 2. * risk)).await;
        quote(&e, 1000.).await;
        age(&e).await;
        quote(&e, 999.).await;
        let open = counter.inserts()[0].clone();
        let id = e.list_conditions().await[0].id.clone();
        let submitted = candidates::parse_time("2026-10-16 10:00:00").unwrap();
        {
            let mut s = e.state.lock().await;
            s.orders.values_mut().next().unwrap().submitted_at = submitted;
        }
        e.drive().await.unwrap();
        assert_eq!(
            counter.requests.lock().unwrap().last().unwrap().0,
            "/order/cancel"
        );
        assert!(e.state.lock().await.entry_timeouts.contains_key(&id));
        let order_status = vec![order(&open, terminal_status, 1)];
        // The terminal order report may arrive before its trade, even across a restart.
        ready(&e, order_status.clone(), vec![]).await;
        assert!(e.state.lock().await.entry_timeouts.contains_key(&id));
        if market_gap {
            e.quote_gap().await.unwrap();
        }
        drop(e);

        let mut e = counter.engine(ExecutionMode::FullAuto, 2);
        e.registry = registry();
        let mut fill = trade(&open, "delayed-open-report", 1, 998.);
        fill["trade_time"] = json!(trade_time);
        ready(&e, order_status.clone(), vec![fill.clone()]).await;
        ready(&e, order_status, vec![fill]).await;
        {
            let s = e.state.lock().await;
            assert_eq!(s.positions[&id].volume, 1);
            assert_eq!(
                s.closes.get(&id).map(|task| task.reason.as_str()),
                expected_exit
            );
            assert!(!s.entry_timeouts.contains_key(&id));
        }
        quote(&e, 999.).await;
        assert_eq!(
            counter.inserts().len(),
            if expected_exit.is_some() { 2 } else { 1 }
        );
        if expected_exit.is_none() {
            assert_eq!(e.list_conditions().await[0].volume, 1);
            age(&e).await;
            quote(&e, 1000. + risk).await;
            assert_eq!(e.state.lock().await.closes[&id].reason, "stop");
            assert_eq!(counter.inserts()[1]["volume"], 1);
        }
    }
}

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
async fn p3_chase_rejection_keeps_the_checked_price_and_never_submits() {
    for short in [false, true] {
        let sign = if short { -1.0 } else { 1.0 };
        for (phase, trigger_first, last_offset) in [
            ("trigger_quote", false, 4.0),
            ("pre_submit_quote", true, 4.0),
            ("proposed_limit", true, 3.0),
        ] {
            let counter = Counter::new();
            let e = engine(&counter, ExitMode::HybridS1).await;
            let mut cfg = e.get_config().await;
            cfg.slippage_ticks = 1;
            e.update_config(cfg).await.unwrap();
            send(&e, signal("chase-limit", short, 22.5, 60.0)).await;
            let id = e.list_conditions().await[0].id.clone();
            if trigger_first {
                quote(&e, 1000.0).await;
                assert_eq!(
                    e.list_conditions().await[0].state,
                    ConditionState::Triggered
                );
            }
            quote(&e, 1000.0 + sign * last_offset).await;
            let condition = &e.list_conditions().await[0];
            assert_eq!(condition.state, ConditionState::Rejected);
            assert_eq!(condition.note, "unfilled");
            let decision = e.strategy_diagnostics().await["entry_decisions"][&id].clone();
            let audit = &decision["chase_limit"];
            assert_eq!(decision["reason"], "unfilled");
            assert_eq!(audit["phase"], phase);
            assert_eq!(audit["side"], if short { "sell" } else { "buy" });
            let book_side = if short { "bid" } else { "ask" };
            let price_source = if phase == "proposed_limit" {
                "limit"
            } else {
                book_side
            };
            assert_eq!(audit["price_source"], price_source);
            assert_eq!(audit["reference_price"], 1000.0);
            assert_eq!(audit["checked_price"], 1000.0 + sign * 5.0);
            assert_eq!(audit["limit_price"], 1000.0 + sign * 4.5);
            assert_eq!(audit["max_deviation"], 4.5);
            assert_eq!(audit["excess"], 0.5);
            assert_eq!(
                audit["quote"][book_side],
                1000.0 + sign * (last_offset + 1.0)
            );
            assert!(audit["quote_sequence"].as_u64().unwrap() > 0);

            quote(&e, 1000.0).await;
            assert!(counter.inserts().is_empty());
            drop(e);
            let restored = counter.engine(ExecutionMode::FullAuto, 2);
            assert_eq!(
                restored.strategy_diagnostics().await["entry_decisions"][&id],
                decision
            );
        }
    }
}

#[tokio::test]
async fn p3_single_lot_skips_narrow_s1_and_exits_at_actual_one_r_after_restart() {
    for mode in [ExitMode::FixedS1, ExitMode::HybridS1] {
        for short in [false, true] {
            let counter = Counter::new();
            let mut e = engine(&counter, mode).await;
            let mut params = parameters(mode);
            params.min_net_rr = 0.1;
            e.configure_strategy(p3::definition(params), e.get_config().await)
                .await
                .unwrap();
            send(&e, signal("one-lot-narrow-s1", short, 60., 24.)).await;
            let sign = if short { -1. } else { 1. };
            let native_target = 1000. + sign * 24.;
            let conditions = e.list_conditions().await;
            assert_eq!(conditions[0].volume, 1);
            assert_eq!(conditions[0].target_price, native_target);
            assert_eq!(
                conditions[0].frozen.as_ref().unwrap().plan.target_price,
                Some(native_target)
            );
            quote(&e, 1000.).await;
            age(&e).await;
            quote(&e, 1000. + sign).await;
            let open = counter.inserts()[0].clone();
            assert_eq!(open["volume"], 1);
            // Slippage changes both the entry cost and the initial risk.
            let cost = 1000. + sign * 2.;
            let fill = trade(&open, "one-lot-entry", 1, cost);
            let orders = vec![order(&open, "all_traded", 1)];
            ready(&e, orders.clone(), vec![fill.clone()]).await;
            let position = e.list_positions().await.remove(0);
            let id = position.id.clone();
            let one_r = cost + sign * 62.;
            assert_eq!(position.risk, 62.);
            assert_eq!(position.target_price, one_r);
            age(&e).await;
            quote(&e, native_target).await;
            assert_eq!(
                counter.inserts().len(),
                1,
                "native S1 must not close the single lot"
            );

            drop(e);
            e = counter.engine(ExecutionMode::FullAuto, 2);
            e.registry = registry();
            ready(&e, orders, vec![fill.clone()]).await;
            assert_eq!(e.list_positions().await[0].target_price, one_r);
            age(&e).await;
            quote(&e, one_r - sign).await;
            assert_eq!(counter.inserts().len(), 1);
            quote(&e, one_r).await;
            let close = counter.inserts()[1].clone();
            assert_eq!(close["volume"], 1);
            assert_eq!(e.state.lock().await.closes[&id].reason, "target_1r");
            ready(
                &e,
                vec![
                    order(&open, "all_traded", 1),
                    order(&close, "all_traded", 1),
                ],
                vec![fill, trade(&close, "one-r-exit", 1, one_r)],
            )
            .await;
            let history = e.get_trade_history().await;
            assert_eq!(history.len(), 1);
            assert_eq!(history[0].exit_reason, "target_1r");
            assert_eq!(history[0].r_multiple, 1.);
            assert_eq!(history[0].target_price, one_r);
        }
    }
}

#[tokio::test]
async fn p3_second_entry_fill_reprices_one_r_floor_and_hybrid_remainder_keeps_tracking() {
    for mode in [ExitMode::FixedS1, ExitMode::HybridS1] {
        for short in [false, true] {
            let counter = Counter::new();
            let mut e = engine(&counter, mode).await;
            let mut params = parameters(mode);
            params.min_net_rr = 0.1;
            e.configure_strategy(p3::definition(params), e.get_config().await)
                .await
                .unwrap();
            send(&e, signal("two-lot-narrow-s1", short, 40., 20.)).await;
            let sign = if short { -1. } else { 1. };
            let native_target = 1000. + sign * 20.;
            quote(&e, 1000.).await;
            age(&e).await;
            quote(&e, 1000. + sign).await;
            let open = counter.inserts()[0].clone();
            assert_eq!(open["volume"], 2);
            let first = trade(&open, "first-entry", 1, 1000. + sign * 2.);
            ready(
                &e,
                vec![order(&open, "part_traded", 1)],
                vec![first.clone()],
            )
            .await;
            assert_eq!(e.list_positions().await[0].target_price, 1000. + sign * 44.);
            age(&e).await;
            quote(&e, native_target).await;
            assert_eq!(counter.inserts().len(), 1);
            let second = trade(&open, "second-entry", 1, 1000. + sign * 4.);
            ready(
                &e,
                vec![order(&open, "all_traded", 2)],
                vec![first.clone(), second.clone()],
            )
            .await;
            assert_eq!(e.list_positions().await[0].volume, 2);
            let one_r = 1000. + sign * 46.; // average cost 1000 +/- 3, initial R = 43
            assert_eq!(e.list_positions().await[0].risk, 43.);
            assert_eq!(e.list_positions().await[0].target_price, one_r);
            quote(&e, native_target).await;
            assert_eq!(
                counter.inserts().len(),
                1,
                "second fill must not restore a near S1"
            );
            drop(e);
            e = counter.engine(ExecutionMode::FullAuto, 2);
            e.registry = registry();
            ready(
                &e,
                vec![order(&open, "all_traded", 2)],
                vec![first.clone(), second.clone()],
            )
            .await;
            assert_eq!(e.list_positions().await[0].target_price, one_r);
            age(&e).await;
            quote(&e, one_r - sign).await;
            assert_eq!(counter.inserts().len(), 1);
            quote(&e, one_r).await;
            let close = counter.inserts()[1].clone();
            let exit_volume = if mode == ExitMode::HybridS1 { 1 } else { 2 };
            assert_eq!(close["volume"], exit_volume);
            let reason = if mode == ExitMode::HybridS1 {
                "p3_first_1r"
            } else {
                "target_1r"
            };
            let id = e.list_positions().await[0].id.clone();
            assert_eq!(e.state.lock().await.closes[&id].reason, reason);
            assert!(
                !e.list_positions().await[0].lot1_closed,
                "protection waits for actual fill"
            );
            ready(
                &e,
                vec![
                    order(&open, "all_traded", 2),
                    order(&close, "all_traded", exit_volume),
                ],
                vec![
                    first,
                    second,
                    trade(&close, "one-r-exit", exit_volume, one_r),
                ],
            )
            .await;
            let history = e.get_trade_history().await;
            assert_eq!(history[0].exit_reason, reason);
            assert_eq!(history[0].r_multiple, 1.);
            assert_eq!(history[0].target_price, one_r);
            let remaining = e.list_positions().await.remove(0);
            if mode == ExitMode::HybridS1 {
                assert_eq!(remaining.volume, 1);
                assert!(remaining.lot1_closed);
                assert_eq!(remaining.target_price, one_r);
                assert_eq!(remaining.current_stop_price, 1000. + sign * 3.);
                age(&e).await;
                quote(&e, one_r + sign).await;
                assert_eq!(
                    counter.inserts().len(),
                    2,
                    "remaining lot must keep its hybrid exit"
                );
                quote(&e, remaining.current_stop_price).await;
                assert_eq!(
                    e.state.lock().await.closes[&remaining.id].reason,
                    "breakeven"
                );
            } else {
                assert!(remaining.is_closed);
                assert_eq!(e.get_trade_history().await[0].exit_reason, "target_1r");
            }
        }
    }
}

#[tokio::test]
async fn p3_fixed_mode_uses_money_not_score_for_zero_one_two_lots() {
    for (risk, qty) in [(20., 2), (60., 1), (120., 0)] {
        let counter = Counter::new();
        let e = engine(&counter, ExitMode::FixedS1).await;
        send(&e, signal("sizing", false, risk, 3. * risk)).await;
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
async fn p3_runner_comparison_continues_both_lanes_after_external_close() {
    use crate::fetch::kline::Kline;

    let counter = Counter::new();
    let mut e = engine(&counter, ExitMode::HybridS1).await;
    let at = candidates::parse_time("2026-10-16 10:00:00").unwrap();
    let clock = Arc::new(ReplayClock::new(at));
    e.clock = clock.clone();
    e.state.lock().await.entry_funds = None;
    let book = |price, timestamp| {
        [(
            "PB2611".into(),
            ExecutionQuote {
                instrument_id: "pb2611".into(),
                exchange_id: "SHFE".into(),
                underlying_symbol: "PB0".into(),
                last: price,
                bid: price - 1.,
                ask: price + 1.,
                tick: 1.,
                lower: 500.,
                upper: 2000.,
                timestamp,
            },
        )]
        .into_iter()
        .collect()
    };
    let snapshot = |orders, fills| {
        json!({"bridge_online": true,
        "health":{"ready":true,"logged_in":true,"sync_pending":false,"trading_day":"20261016","broker_id":"fake","user_id":"offline-test"},
        "account":{"available":50000.,"curr_margin":0.,"updated_at":at},
        "orders":orders,"trades":fills,"broker_positions":[]})
    };
    e.reconcile(&snapshot(Vec::<Value>::new(), Vec::<Value>::new()))
        .await
        .unwrap();
    e.set_p3_runner_comparison(true).await.unwrap();
    let mut payload = signal("shadow-manual", true, 20., 30.);
    let warning = candidates::label(at - 900);
    payload["created_at_epoch"] = json!(at);
    payload["warning_ts"] = json!(warning);
    payload["warning_context"]["as_of"] = json!(warning);
    send(&e, payload).await;
    e.on_quotes(book(1000., at)).await.unwrap();
    e.drive().await.unwrap();
    clock.advance(at + 1).unwrap();
    age(&e).await;
    e.on_quotes(book(999., at + 1)).await.unwrap();
    e.drive().await.unwrap();
    assert_eq!(
        counter.inserts().len(),
        1,
        "{:?}",
        e.list_conditions().await
    );
    let open = counter.inserts()[0].clone();
    assert_eq!(open["volume"], 2);
    let entry = trade(&open, "runner-entry", 2, 998.);
    let orders = vec![order(&open, "all_traded", 2)];
    e.reconcile(&snapshot(orders.clone(), vec![entry.clone()]))
        .await
        .unwrap();
    assert_eq!(e.comparison_summary().await["p3_runner"]["active"], 1);
    let mut disconnected = e.state.lock().await.p3_runner.clone();
    disconnected.invalidate_active("quote_gap");
    assert_eq!(disconnected.summary(true)["unavailable"], 1);
    assert_eq!(disconnected.summary(true)["paired"], 0);

    let bars: Vec<Kline> = (0..46)
        .rev()
        .map(|n| Kline {
            datetime: candidates::label(at - 300 * n),
            open: 1000.,
            high: 1020.,
            low: 980.,
            close: 1000.,
            volume: 1.,
            hold: 1.,
        })
        .collect();
    e.on_p3_runner_history("SHFE.pb2611", &bars).await.unwrap();
    clock.advance(at + 2).unwrap();
    let mut external = trade(
        &json!({"order_ref":"external", "direction":"buy", "offset":"close"}),
        "phone-exit",
        2,
        988.,
    );
    external["trade_time"] = json!("10:00:02");
    let interrupted = snapshot(orders, vec![entry, external]);
    e.reconcile(&interrupted).await.unwrap();
    e.reconcile(&interrupted).await.unwrap();
    assert!(e.list_positions().await[0].is_closed);
    assert_eq!(e.get_trade_history().await.len(), 1);
    assert!(e.requested_contracts().await.iter().any(|s| s == "pb2611"));

    // Disabling registration retains the already armed observation, through both exits.
    e.set_p3_runner_comparison(false).await.unwrap();
    for (seconds, price) in [(3, 970.), (4, 930.), (5, 950.), (6, 850.), (7, 931.)] {
        clock.advance(at + seconds).unwrap();
        e.on_quotes(book(price, at + seconds)).await.unwrap();
        e.drive().await.unwrap();
    }
    let comparison = &e.comparison_summary().await["p3_runner"];
    assert_eq!(comparison["paired"], 0);
    assert_eq!(comparison["interrupted_pairs"], 1);
    assert_eq!(comparison["interrupted_delta_yuan"], 95.);
    let row = &comparison["recent"][0];
    assert_eq!(row["interventions"].as_array().unwrap().len(), 1);
    assert_eq!(row["interventions"][0]["price"], 988.);
    assert_eq!(row["first_fill"]["price"], 971.);
    assert_eq!(row["base"]["price"], 951.);
    assert_eq!(row["dynamic"]["price"], 932.);
    assert!(e.p3_runner_history_symbols().await.is_empty());
    assert_eq!(counter.inserts().len(), 1);
    drop(e);
    let restarted = counter.engine(ExecutionMode::FullAuto, 2);
    assert_eq!(
        restarted.comparison_summary().await["p3_runner"],
        *comparison
    );
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
    send(&e, signal("repriced", false, 56., 190.)).await;
    assert_eq!(e.list_conditions().await[0].volume, 2); // 296 yuan per forecast lot
    quote(&e, 1000.).await;
    age(&e).await;
    quote(&e, 1002.).await;
    assert_eq!(counter.inserts().len(), 1);
    assert_eq!(counter.inserts()[0]["volume"], 1); // proposed ask -> 306 yuan per lot
    assert_eq!(e.list_conditions().await[0].volume, 1);
}

#[tokio::test]
async fn p3_v3_filters_a_far_target_without_creating_a_condition_or_order() {
    let counter = Counter::new();
    let e = engine(&counter, ExitMode::FixedS1).await;
    send(&e, signal("far-target", true, 20., 71.)).await;
    quote(&e, 1000.).await;
    age(&e).await;
    quote(&e, 999.).await;
    assert!(counter.inserts().is_empty());
    assert!(e.list_conditions().await.is_empty());
    assert!(!e.paused().await);

    let definition = e.definition().await;
    let diagnostics = e.strategy_diagnostics().await;
    let decisions = diagnostics["states"][&definition.revision]["decisions"]
        .as_object()
        .unwrap();
    let decision = decisions.values().next().unwrap();
    assert_eq!(decisions.len(), 1);
    assert_eq!(decision["reason"], "target_atr_above_max");
    assert_eq!(decision["values"]["target_atr"], 3.55);
    assert_eq!(decision["values"]["max_target_atr"], 3.5);

    send(&e, signal("at-target-limit", true, 20., 70.)).await;
    let conditions = e.list_conditions().await;
    assert_eq!(conditions.len(), 1);
    assert_eq!(conditions[0].target_price, 930.);
}

#[tokio::test]
async fn p3_low_risk_atr_signal_reaches_broker_and_keeps_s1_exit_modes() {
    for mode in [ExitMode::FixedS1, ExitMode::HybridS1] {
        let counter = Counter::new();
        let e = engine(&counter, mode).await;
        let mut payload = signal("low-atr", false, 20., 30.);
        payload["warning_context"]["atr14"] = json!(40.);
        send(&e, payload).await;
        assert_eq!(e.list_conditions().await.len(), 1);
        quote(&e, 1000.).await;
        age(&e).await;
        quote(&e, 1001.).await;
        let open = counter.inserts()[0].clone();
        assert_eq!(open["volume"], 2);
        let fill = trade(&open, "low-atr-open", 2, 1002.);
        ready(&e, vec![order(&open, "all_traded", 2)], vec![fill]).await;
        age(&e).await;
        quote(&e, 1030.).await;
        assert_eq!(
            counter.inserts()[1]["volume"],
            if mode == ExitMode::HybridS1 { 1 } else { 2 }
        );
    }
}

#[tokio::test]
async fn p3_custom_rr_is_frozen_when_new_signals_switch_to_a_stricter_threshold() {
    let counter = Counter::new();
    let e = engine(&counter, ExitMode::FixedS1).await;
    let cfg = e.get_config().await;
    let mut params = parameters(ExitMode::FixedS1);
    params.min_net_rr = 0.8;
    e.configure_strategy(p3::definition(params), cfg.clone())
        .await
        .unwrap();
    let mut payload = signal("custom-rr", false, 20., 24.);
    payload["warning_context"]["atr14"] = json!(40.);
    send(&e, payload).await;
    let conditions = e.list_conditions().await;
    assert_eq!(conditions.len(), 1);
    assert_eq!(
        conditions[0].frozen.as_ref().unwrap().definition.parameters["min_net_rr"],
        0.8
    );
    e.configure_strategy(p3::definition(parameters(ExitMode::FixedS1)), cfg)
        .await
        .unwrap();
    quote(&e, 1000.).await;
    age(&e).await;
    quote(&e, 1001.).await;
    assert_eq!(
        counter.inserts().len(),
        1,
        "the old approved condition must use its own 0.8 threshold"
    );
    assert_eq!(counter.inserts()[0]["volume"], 2);
}

#[tokio::test]
async fn p3_point_value_mismatch_cannot_open_and_invalid_execution_timeout_is_rejected() {
    let counter = Counter::new();
    let e = engine(&counter, ExitMode::FixedS1).await;
    let mut bad = signal("metadata", false, 20., 60.);
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
