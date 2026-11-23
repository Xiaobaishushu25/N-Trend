use super::*;
use std::collections::HashMap;

#[test]
fn month_end_night_uses_trading_day_and_flattens_before_the_day_session_closes() {
    let strategy = P3Strategy::new(parameters()).unwrap();
    let cutoff = parse_time("2026-07-31 14:50:00").unwrap();
    assert_eq!(strategy.contract_block("pb2608", cutoff - 1), None);
    assert_eq!(
        strategy.contract_block("pb2608", cutoff),
        Some("contract_expiry_window")
    );
    assert_eq!(
        strategy.contract_block("pb2608", parse_time("2026-07-31 21:30:00").unwrap()),
        Some("delivery_month")
    );
    let mut p = position(false);
    p.instrument_id = "pb2608".into();
    p.open_ts = "2026-07-31 10:00:00".into();
    let actions = run(
        &strategy,
        StrategyEvent::Timer { timestamp: cutoff },
        &mut Value::Null,
        &[p],
        &[],
    );
    assert!(actions.iter().any(
        |a| matches!(a,TradeIntent::Close{reason,volume:None,..} if reason=="contract_expiry")
    ));
}

#[test]
fn calendar_expiry_blocks_new_entries_and_flattens_existing_positions() {
    let p = parameters();
    let cut = parse_time(&p.calendar.coverage_end).unwrap() - 600;
    let strategy = P3Strategy::new(p).unwrap();
    assert_eq!(
        strategy.contract_block("pb2703", cut),
        Some("calendar_coverage_ending")
    );
    let mut pos = position(false);
    pos.instrument_id = "pb2703".into();
    let actions = run(
        &strategy,
        StrategyEvent::Timer { timestamp: cut },
        &mut Value::Null,
        &[pos],
        &[],
    );
    assert!(actions.iter().any(
        |a| matches!(a,TradeIntent::Close{reason,volume:None,..} if reason=="pre_calendar_end")
    ));
}

#[test]
fn monetary_sizing_rejects_a_plan_whose_stop_disagrees_with_its_snapshot() {
    let mut p = make_plan(signal());
    p.stop_price = Some(900.);
    assert!(RiskBudget
        .volume(
            &p,
            &ExecutionConfig::default(),
            &serde_json::to_value(parameters()).unwrap()
        )
        .is_err());
}

#[test]
fn holding_mfe_rebases_when_partial_entry_changes_cost_and_risk() {
    let mut clock = Clock::default();
    let mut p = position(false);
    p.fill_price = 1000.;
    p.risk = 10.;
    clock.align_holding(&p);
    clock.buckets.insert("first".into(), 0.6);
    p.fill_price = 1005.;
    p.risk = 15.;
    clock.align_holding(&p);
    assert!((clock.buckets["first"] - 1. / 15.).abs() < 1e-9);
}

#[test]
fn friday_night_flattening_preempts_an_inflight_partial_exit() {
    let strategy = P3Strategy::new(parameters()).unwrap();
    let p = position(false);
    let cutoff = parse_time("2026-11-14 00:55:00").unwrap();
    let cond=serde_json::from_value(json!({"id":"position","symbol":"PB0","instrument_id":"pb2701","direction":"up",
        "tier":"small","entry_price":1000.,"stop_price":980.,"target_price":1100.,"risk":20.,"score":2.5,"volume":2,
        "warning_bar_ts":"2026-11-12 09:15:00","bars_waited":0,"max_wait_bars":12,"state":"closing",
        "created_at":parse_time("2026-11-12 09:15:00").unwrap(),"note":"first S1 working"})).unwrap();
    for (timestamp, should_flatten) in [(cutoff - 1, false), (cutoff, true)] {
        let actions = run(
            &strategy,
            StrategyEvent::Timer { timestamp },
            &mut Value::Null,
            std::slice::from_ref(&p),
            std::slice::from_ref(&cond),
        );
        assert_eq!(
            actions.iter().any(
                |a| matches!(a, TradeIntent::Close { reason, volume: None, .. } if reason == "pre_weekend")
            ),
            should_flatten
        );
    }
    assert_eq!(
        strategy.comparison_flat_boundary(&p.instrument_id, parse_time(&p.open_ts).unwrap()),
        Some((cutoff, "pre_weekend"))
    );
    assert_eq!(
        strategy
            .base
            .parameters
            .block(&p.instrument_id, cutoff - 5 * 60),
        Some("filtered_weekend_window")
    );
}

fn parameters() -> P3Parameters {
    let mut p = P3Parameters::reference_snapshot();
    p.fees.products.insert(
        "PB".into(),
        serde_json::from_value(json!({"roundtrip_fixed_yuan_per_lot":6.})).unwrap(),
    );
    p
}
fn signal() -> Value {
    json!({"symbol":"PB0","instrument_id":"pb2701","direction":"up",
        "s0_ts":"s0","s1_ts":"s1","s2_ts":"s2","warning_ts":"2026-11-12 09:15:00",
        "created_at_epoch":parse_time("2026-11-12 09:15:00").unwrap(),"entry":1000.,"stop":980.,
        "target":1100.,"risk":20.,"tick_size":1.,"multiplier":10.,"entry_score":2.5,
        "warning_context":{"as_of":"2026-11-12 09:15:00","complete":true,"atr14":30.}})
}
fn run(
    strategy: &dyn Strategy,
    event: StrategyEvent,
    state: &mut Value,
    positions: &[ManagedPosition],
    conditions: &[super::super::super::ConditionOrder],
) -> Vec<TradeIntent> {
    let cfg = ExecutionConfig::default();
    let quotes = HashMap::new();
    strategy
        .on_event(
            &event,
            &StrategyContext {
                instance_id: "test",
                positions,
                conditions,
                orders: &[],
                account: None,
                quotes: &quotes,
                config: &cfg,
                allow_entries: true,
            },
            state,
        )
        .unwrap()
}
fn make_plan(p: Value) -> EntryPlan {
    let s = P3Strategy::new(parameters()).unwrap();
    let actions = run(
        &s,
        StrategyEvent::Signal {
            source: N_STRATEGY.into(),
            payload: p,
        },
        &mut Value::Null,
        &[],
        &[],
    );
    match actions.into_iter().next().unwrap() {
        TradeIntent::Open(p) => p,
        _ => panic!("expected plan"),
    }
}
fn position(short: bool) -> ManagedPosition {
    serde_json::from_value(json!({"id":"position","condition_order_id":"position","symbol":"PB0","instrument_id":"pb2701",
        "direction":if short{"down"}else{"up"},"tier":"small","fill_price":1000.,"initial_stop_price":if short{1020.}else{980.},
        "target_price":if short{900.}else{1100.},"risk":20.,"volume":2,"open_ts":"2026-11-12 09:16:00",
        "bars_held":0,"max_mfe_r":0.,"max_mae_r":0.,"trailing_grade":0,"current_stop_price":if short{1020.}else{980.},
        "lot1_closed":false,"is_closed":false})).unwrap()
}
fn quote(price: f64) -> ExecutionQuote {
    ExecutionQuote {
        instrument_id: "pb2701".into(),
        exchange_id: "SHFE".into(),
        underlying_symbol: "PB0".into(),
        last: price,
        bid: price - 1.,
        ask: price + 1.,
        tick: 1.,
        lower: 500.,
        upper: 2000.,
        timestamp: parse_time("2026-11-12 09:20:00").unwrap(),
    }
}

#[test]
fn registry_preset_is_paired_and_invalid_parameters_are_rejected() {
    let r = StrategyRegistry::default();
    let mut d = definition(parameters());
    r.validate(&d).unwrap();
    assert_eq!(d.strategy_id, ID);
    assert_eq!(d.exit_policy, EXIT);
    assert_eq!(d.sizing_policy, SIZING);
    assert_eq!(d.parameters["exit_mode"], "fixed_s1");
    d.sizing_policy = ONE_LOT.into();
    assert!(r.validate(&d).is_err());
    let mut mixed = StrategyDefinition::default();
    mixed.sizing_policy = SIZING.into();
    assert!(r.validate(&mixed).is_err());
    for value in [json!(0), json!(-1), json!(3)] {
        let mut p = parameters();
        p.max_lots = value.as_i64().unwrap() as i32;
        assert!(p.validate().is_err());
    }
    for budget in [0., -1., f64::NAN, f64::INFINITY] {
        let mut p = parameters();
        p.risk_budget_yuan = budget;
        assert!(p.validate().is_err());
    }
    let mut bad = serde_json::to_value(parameters()).unwrap();
    bad["exit_mode"] = json!("typo");
    assert!(serde_json::from_value::<P3Parameters>(bad).is_err());
}

#[test]
fn score_two_point_five_is_admitted_and_upper_atr_limit_remains() {
    let plan = make_plan(signal());
    assert_eq!(plan.requested_volume, 2);
    assert_eq!(plan.target_price, Some(1100.));
    let strategy = P3Strategy::new(parameters()).unwrap();
    for (field, value, reason) in [
        ("entry_score", json!(2.49), "score_below_min"),
        (
            "warning_context",
            json!({"as_of":"2026-11-12 09:15:00","complete":true,"atr14":7.}),
            "atr_out_of_range",
        ),
    ] {
        let mut p = signal();
        p[field] = value;
        let mut state = Value::Null;
        assert!(run(
            &strategy,
            StrategyEvent::Signal {
                source: N_STRATEGY.into(),
                payload: p
            },
            &mut state,
            &[],
            &[]
        )
        .is_empty());
        let d = state["decisions"]
            .as_object()
            .unwrap()
            .values()
            .next()
            .unwrap();
        assert_eq!(d["reason"], reason);
        assert_eq!(d["values"]["min_score"], 2.5);
    }
}

fn signal_decision(strategy: &dyn Strategy, payload: Value) -> (Vec<TradeIntent>, Value) {
    let mut state = Value::Null;
    let actions = run(
        strategy,
        StrategyEvent::Signal {
            source: N_STRATEGY.into(),
            payload,
        },
        &mut state,
        &[],
        &[],
    );
    let decision = state["decisions"]
        .as_object()
        .unwrap()
        .values()
        .next()
        .unwrap()
        .clone();
    (actions, decision)
}

#[test]
fn v2_removes_only_atr_floor_for_both_exit_modes_and_directions() {
    for mode in [ExitMode::FixedS1, ExitMode::HybridS1] {
        let mut params = parameters();
        params.exit_mode = mode;
        let current = P3Strategy::for_version(params.clone(), PREVIOUS_VERSION).unwrap();
        let previous = P3Strategy::for_version(params.clone(), ATR_FLOOR_VERSION).unwrap();
        let p2 = CandidateStrategy::new(params.common()).unwrap();
        for short in [false, true] {
            let mut payload = signal();
            payload["entry_score"] = json!(4.);
            payload["warning_context"]["atr14"] = json!(40.);
            if short {
                payload["direction"] = json!("down");
                payload["stop"] = json!(1020.);
                payload["target"] = json!(900.);
            }
            let (actions, audit) = signal_decision(&current, payload.clone());
            assert!(matches!(&actions[0], TradeIntent::Open(plan) if plan.requested_volume == 2));
            assert_eq!(audit["reason"], "accepted_warning");
            assert_eq!(audit["values"]["min_risk_atr"], 0.);
            assert_eq!(
                signal_decision(&previous, payload.clone()).1["reason"],
                "atr_out_of_range"
            );
            assert_eq!(
                signal_decision(&p2, payload).1["reason"],
                "atr_out_of_range"
            );
        }
    }
}

#[test]
fn v3_filters_large_planned_targets_in_both_directions_without_changing_v1_v2_or_p2() {
    let registry = StrategyRegistry::default();
    let mut legacy = definition(parameters());
    let p2 = CandidateStrategy::new(parameters().common()).unwrap();
    for mode in [ExitMode::FixedS1, ExitMode::HybridS1] {
        let mut params = parameters();
        params.exit_mode = mode;
        let current = registry.strategy(&definition(params)).unwrap();
        for sign in [-1., 1.] {
            let mut payload = signal();
            payload["entry_score"] = json!(4.);
            payload["direction"] = json!(if sign > 0. { "up" } else { "down" });
            payload["stop"] = json!(1000. - sign * 20.);
            payload["warning_context"]["atr14"] = json!(20.);
            for distance in [69., 70.] {
                payload["target"] = json!(1000. + sign * distance);
                let (intents, audit) = signal_decision(current.as_ref(), payload.clone());
                assert_eq!(audit["reason"], "accepted_warning");
                assert!(matches!(&intents[..], [TradeIntent::Open(plan)]
                    if plan.target_price == Some(1000. + sign * distance)));
            }
            payload["target"] = json!(1000. + sign * 71.);
            let (intents, audit) = signal_decision(current.as_ref(), payload.clone());
            assert!(intents.is_empty());
            assert_eq!(audit["reason"], "target_atr_above_max");
            assert_eq!(audit["values"]["target_atr"], 3.55);
            assert_eq!(audit["values"]["max_target_atr"], 3.5);
            for version in [ATR_FLOOR_VERSION, PREVIOUS_VERSION] {
                legacy.code_version = version.into();
                let strategy = registry.strategy(&legacy).unwrap();
                assert_eq!(
                    signal_decision(strategy.as_ref(), payload.clone()).1["reason"],
                    "accepted_warning"
                );
            }
            assert_eq!(
                signal_decision(&p2, payload).1["reason"],
                "accepted_warning"
            );
        }
    }
}

#[test]
fn configurable_rr_is_enforced_at_signal_and_proposed_price_in_both_modes_and_directions() {
    for mode in [ExitMode::FixedS1, ExitMode::HybridS1] {
        for short in [false, true] {
            let sign = if short { -1. } else { 1. };
            let mut payload = signal();
            payload["warning_context"]["atr14"] = json!(40.);
            payload["direction"] = json!(if short { "down" } else { "up" });
            payload["stop"] = json!(1000. - sign * 20.);
            payload["target"] = json!(1000. + sign * 23.);
            let mut params = parameters();
            params.exit_mode = mode;
            let strict = P3Strategy::new(params.clone()).unwrap();
            assert_eq!(
                signal_decision(&strict, payload.clone()).1["reason"],
                "filtered_net_rr_plan"
            );
            params.min_net_rr = 0.8;
            let relaxed = P3Strategy::new(params.clone()).unwrap();
            let (actions, decision) = signal_decision(&relaxed, payload);
            assert_eq!(decision["values"]["min_net_rr"], 0.8);
            assert!(decision["values"]["planned_net_rr"].as_f64().unwrap() < 1.);
            let TradeIntent::Open(plan) = &actions[0] else {
                panic!("expected plan")
            };
            let favorable = 1000. + sign * 2.;
            let q = quote(favorable);
            assert!(matches!(
                relaxed.check_entry(plan, &q, favorable, q.timestamp),
                EntryDecision::Allow
            ));
            let adverse = 1000. + sign * 3.;
            let q = quote(adverse);
            assert!(
                matches!(relaxed.check_entry(plan, &q, adverse, q.timestamp), EntryDecision::Reject(r) if r == "filtered_net_rr_fill")
            );
            params.min_net_rr = 1.2;
            let higher = P3Strategy::new(params).unwrap();
            assert!(
                matches!(higher.check_entry(plan, &quote(favorable), favorable, q.timestamp), EntryDecision::Reject(r) if r == "filtered_net_rr_fill")
            );
        }
    }
}

#[test]
fn v3_keeps_atr_validity_friction_and_structural_risk_guards() {
    let strategy = P3Strategy::new(parameters()).unwrap();
    for atr in [json!(0), json!(null)] {
        let mut payload = signal();
        payload["warning_context"]["atr14"] = atr;
        assert_eq!(
            signal_decision(&strategy, payload).1["reason"],
            "atr_unavailable"
        );
    }
    let mut payload = signal();
    payload["warning_context"]["complete"] = json!(false);
    assert_eq!(
        signal_decision(&strategy, payload).1["reason"],
        "atr_unavailable"
    );
    let mut payload = signal();
    payload["stop"] = json!(991.);
    payload["risk"] = json!(9.);
    assert_eq!(
        signal_decision(&strategy, payload).1["reason"],
        "risk_ticks_below_min"
    );
    let mut params = parameters();
    params.fees.products.insert(
        "PB".into(),
        serde_json::from_value(json!({"roundtrip_fixed_yuan_per_lot":21.})).unwrap(),
    );
    assert_eq!(
        signal_decision(&P3Strategy::new(params).unwrap(), signal()).1["reason"],
        "friction_above20"
    );
}

#[test]
fn frozen_v1_definitions_keep_their_floor_and_fixed_rr_after_roundtrip() {
    let registry = StrategyRegistry::default();
    let mut old = serde_json::to_value(definition(parameters())).unwrap();
    old["code_version"] = json!(ATR_FLOOR_VERSION);
    old["parameters"]
        .as_object_mut()
        .unwrap()
        .remove("min_net_rr");
    let old: StrategyDefinition =
        serde_json::from_slice(&serde_json::to_vec(&old).unwrap()).unwrap();
    registry.validate(&old).unwrap();
    let mut payload = signal();
    payload["warning_context"]["atr14"] = json!(40.);
    assert_eq!(
        signal_decision(registry.strategy(&old).unwrap().as_ref(), payload.clone()).1["reason"],
        "atr_out_of_range"
    );
    let current = definition(parameters());
    assert_eq!(current.code_version, "3");
    assert_eq!(
        signal_decision(registry.strategy(&current).unwrap().as_ref(), payload).1["reason"],
        "accepted_warning"
    );
    let mut altered_old = old;
    altered_old.parameters["min_net_rr"] = json!(0.8);
    assert!(registry.validate(&altered_old).is_err());
    let mut unknown = current;
    unknown.code_version = "4".into();
    assert!(registry.validate(&unknown).is_err());
    for bad in [0., -0.1, f64::NAN, f64::INFINITY] {
        let mut params = parameters();
        params.min_net_rr = bad;
        assert!(params.validate().is_err());
    }
}

#[test]
fn monetary_boundaries_include_slippage_and_fees_in_both_directions() {
    for short in [false, true] {
        let mut p = signal();
        p["stop"] = json!(if short { 1020. } else { 980. });
        p["direction"] = json!(if short { "down" } else { "up" });
        p["target"] = json!(if short { 900. } else { 1100. });
        let plan = make_plan(p);
        let mut snapshot: Snapshot =
            serde_json::from_value(plan.evidence["candidate"].clone()).unwrap();
        // 20 structure points + 2 adverse ticks, times 10, plus 6 fee = 226.
        assert_eq!(
            risk_budget::unit_loss(&snapshot, 1000. + snapshot.sign()).unwrap(),
            22600
        );
        for (unit, expected) in [(300., 2), (300.01, 1), (600., 1), (600.01, 0)] {
            snapshot.fees =
                serde_json::from_value(json!({"roundtrip_fixed_yuan_per_lot":unit-220.})).unwrap();
            assert_eq!(
                risk_budget::initial_volume(&snapshot, &BudgetSettings::new(600., 2).unwrap())
                    .unwrap(),
                expected
            );
        }
    }
}

#[test]
fn budget_rejection_is_a_visible_terminal_signal_not_an_instance_error() {
    let mut p = parameters();
    p.risk_budget_yuan = 200.;
    let strategy = P3Strategy::new(p).unwrap();
    let mut state = Value::Null;
    let event = StrategyEvent::Signal {
        source: N_STRATEGY.into(),
        payload: signal(),
    };
    assert!(run(&strategy, event.clone(), &mut state, &[], &[]).is_empty());
    let before = state.clone();
    assert!(run(&strategy, event, &mut state, &[], &[]).is_empty());
    assert_eq!(state, before);
    assert_eq!(
        state["decisions"]
            .as_object()
            .unwrap()
            .values()
            .next()
            .unwrap()["reason"],
        "risk_budget_exceeded"
    );
}

#[test]
fn sizing_rechecks_actual_notional_multiplier_and_remaining_budget() {
    let plan = make_plan(signal());
    let parameters = serde_json::to_value(parameters()).unwrap();
    let q = quote(1000.);
    let mut c = SizingContext {
        phase: SizingPhase::ProposedOrder,
        quote: Some(&q),
        contract_multiplier: 10.,
        proposed_price: 1013.,
        approved_volume: 2,
        filled_volume: 0,
        filled_notional: 0.,
    };
    assert_eq!(
        RiskBudget
            .volume_at_price(&plan, &ExecutionConfig::default(), &parameters, &c)
            .unwrap(),
        1
    );
    c.proposed_price = 1001.;
    c.filled_volume = 1;
    c.filled_notional = 1020.;
    assert_eq!(
        RiskBudget
            .volume_at_price(&plan, &ExecutionConfig::default(), &parameters, &c)
            .unwrap(),
        1
    );
    c.contract_multiplier = 5.;
    assert!(RiskBudget
        .volume_at_price(&plan, &ExecutionConfig::default(), &parameters, &c)
        .is_err());
    c.contract_multiplier = 10.;
    c.filled_volume = 2;
    c.filled_notional = 2100.;
    assert_eq!(
        RiskBudget
            .volume_at_price(&plan, &ExecutionConfig::default(), &parameters, &c)
            .unwrap(),
        0
    );
}

#[test]
fn entry_target_has_one_r_floor_for_one_and_two_lots_preserving_native_s1() {
    for short in [false, true] {
        let sign = if short { -1. } else { 1. };
        for mode in [ExitMode::FixedS1, ExitMode::HybridS1] {
            let mut params = parameters();
            params.exit_mode = mode;
            let params = serde_json::to_value(params).unwrap();
            for native_r in [0.4, 1., 1.5] {
                for volume in [1, 2] {
                    let mut p = position(short);
                    p.volume = volume;
                    let mut plan = make_plan(signal());
                    let native_target = p.fill_price + sign * native_r * p.risk;
                    plan.target_price = Some(native_target);
                    p.target_price = P3Exit.target_on_fill(&p, &plan).unwrap();
                    assert_eq!(plan.target_price, Some(native_target));
                    let expected_target = p.fill_price + sign * native_r.max(1.) * p.risk;
                    assert_eq!(p.target_price, expected_target);
                    if native_r < 1. {
                        assert!(P3Exit
                            .on_quote(&p, &quote(native_target), &params)
                            .close
                            .is_none());
                        assert!(P3Exit
                            .on_quote(&p, &quote(p.fill_price + sign * (p.risk - 1.)), &params)
                            .close
                            .is_none());
                    }
                    let reason = if volume == 2 && mode == ExitMode::HybridS1 {
                        (
                            if native_r <= 1. { FIRST_1R } else { FIRST_S1 }.into(),
                            Some(1),
                        )
                    } else if native_r <= 1. {
                        ("target_1r".into(), None)
                    } else {
                        ("target_s1".into(), None)
                    };
                    assert_eq!(
                        P3Exit
                            .on_quote(&p, &quote(expected_target), &params)
                            .close
                            .unwrap(),
                        reason
                    );
                    let stop_exit = P3Exit.on_quote(&p, &quote(p.initial_stop_price), &params);
                    assert_eq!(stop_exit.close.unwrap(), ("stop".into(), None));
                }
            }
        }
    }
}

#[test]
fn near_target_remainder_keeps_target_and_protection_after_later_fill_hook() {
    for short in [false, true] {
        let mut p = position(short);
        let sign = if short { -1. } else { 1. };
        p.volume = 1;
        p.lot1_closed = true;
        p.target_price = p.fill_price + sign * p.risk;
        p.current_stop_price = p.fill_price;
        let mut plan = make_plan(signal());
        plan.target_price = Some(p.fill_price + sign * 0.4 * p.risk);
        assert_eq!(P3Exit.target_on_fill(&p, &plan), Some(p.target_price));
        let mut params = parameters();
        params.exit_mode = ExitMode::HybridS1;
        let params = serde_json::to_value(params).unwrap();
        assert!(P3Exit
            .on_quote(&p, &quote(p.target_price), &params)
            .close
            .is_none());
    }
}

#[test]
fn fixed_and_hybrid_exits_mirror_and_protection_requires_first_fill() {
    for short in [false, true] {
        let mut p = position(short);
        let sign = if short { -1. } else { 1. };
        let mut params = serde_json::to_value(parameters()).unwrap();
        let q = quote(p.target_price);
        assert_eq!(P3Exit.on_quote(&p, &q, &params).close.unwrap().1, None);
        params["exit_mode"] = json!("hybrid_s1");
        assert_eq!(P3Exit.on_quote(&p, &q, &params).close.unwrap().1, Some(1));
        p.volume = 1;
        assert_eq!(P3Exit.on_quote(&p, &q, &params).close.unwrap().1, None);
        p.volume = 2;
        p.max_mfe_r = 3.;
        assert_eq!(
            P3Exit
                .on_quote(&p, &quote(1000. + sign * 50.), &params)
                .stop,
            p.initial_stop_price
        );
        let confirmed = P3Exit.on_partial_close(&p, FIRST_S1, 1, &params, Some(&q));
        assert!(confirmed.first_target_completed);
        assert_eq!(confirmed.stop, 1000.);
        p.volume = 1;
        p.lot1_closed = true;
        p.current_stop_price = 1000.;
        p.max_mfe_r = 1.59;
        assert_eq!(
            P3Exit
                .on_quote(&p, &quote(1000. + sign * 20.), &params)
                .stop,
            1000.
        );
        p.max_mfe_r = 1.6;
        let u = P3Exit.on_quote(&p, &quote(1000. + sign * 30.), &params);
        assert_eq!(u.stop, 1000. + sign * 16.);
        p.current_stop_price = u.stop;
        p.max_mfe_r = 2.69;
        let u = P3Exit.on_quote(&p, &quote(1000. + sign * 30.), &params);
        assert!(u.close.is_some());
        assert_eq!(u.close.unwrap().0, "trailing");
    }
}

#[test]
fn fractional_cost_breakeven_is_aligned_and_not_mislabelled_as_trailing() {
    let mut params = serde_json::to_value(parameters()).unwrap();
    params["exit_mode"] = json!("hybrid_s1");
    for short in [false, true] {
        let mut p = position(short);
        p.fill_price = 1000.5;
        let q = quote(1010.);
        let update = P3Exit.on_partial_close(&p, FIRST_S1, 1, &params, Some(&q));
        assert_eq!(update.stop, if short { 1000. } else { 1001. });
        p.current_stop_price = update.stop;
        p.lot1_closed = true;
        p.volume = 1;
        let u = P3Exit.on_quote(&p, &quote(update.stop), &params);
        assert_eq!(u.close.unwrap().0, "breakeven");
        assert_eq!(p.risk, 20.);
    }
}

#[test]
fn delivery_month_and_explicit_cutoff_are_enforced_without_trading_day_labels() {
    let mut params = parameters();
    params
        .entry_cutoffs
        .insert("PB2701".into(), "2026-11-12 10:00:00".into());
    let strategy = P3Strategy::new(params).unwrap();
    let before = parse_time("2026-11-12 09:59:59").unwrap();
    assert_eq!(strategy.contract_block("pb2701", before), None);
    assert_eq!(
        strategy.contract_block("pb2701", before + 1),
        Some("contract_entry_cutoff")
    );
    assert_eq!(
        strategy.contract_block("pb2611", before),
        Some("delivery_month")
    );
    assert_eq!(
        strategy.contract_block("pb2610", before),
        Some("delivery_month")
    );
    assert_eq!(strategy.contract_block("MA701", before), None);
    assert_eq!(
        strategy.contract_block("pb2713", before),
        Some("invalid_actual_contract")
    );
}
