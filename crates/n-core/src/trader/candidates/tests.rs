use super::*;
use crate::trader::{model::ExecutionQuote, ExecutionConfig};
use std::collections::HashMap;

pub(super) fn parameters() -> Parameters {
    serde_json::from_value(json!({"fees":{"id":"test-fees","products":{"PB":{
        "open":{"fixed_yuan_per_lot":3.,"notional_rate":0.},
        "close_today":{"fixed_yuan_per_lot":3.,"notional_rate":0.},
        "close_yesterday":{"fixed_yuan_per_lot":2.,"notional_rate":0.}}}},
        "calendar":{"id":"test-calendar","coverage_start":"2026-07-01 00:00:00",
        "coverage_end":"2026-10-31 23:59:59","windows":[{"name":"national",
        "block_from":"2026-09-30 14:30:00","flat_at":"2026-09-30 14:45:00",
        "last_close":"2026-09-30 15:00:00","reopen":"2026-10-09 09:00:00"}]}}))
    .unwrap()
}
pub(super) fn signal() -> Value {
    json!({"symbol":"PB0","instrument_id":"pb2611","direction":"up",
        "s0_ts":"2026-10-20 09:15:00","s1_ts":"2026-10-20 10:15:00","s2_ts":"2026-10-20 14:15:00",
        "warning_ts":"2026-10-21 09:15:00","entry":1000.,"stop":980.,"target":1100.,
        "risk":20.,"tick_size":1.,"multiplier":10.,"entry_score":4.,
        "created_at_epoch":parse_time("2026-10-21 09:15:00").unwrap(),
        "warning_context":{"as_of":"2026-10-21 09:15:00","complete":true,"atr14":20.}})
}
fn run(
    s: &CandidateStrategy,
    event: StrategyEvent,
    state: &mut Value,
    positions: &[ManagedPosition],
) -> Vec<TradeIntent> {
    let config = ExecutionConfig::default();
    let quotes = HashMap::new();
    let context = StrategyContext {
        instance_id: "test",
        positions,
        conditions: &[],
        orders: &[],
        account: None,
        quotes: &quotes,
        config: &config,
        allow_entries: true,
    };
    s.on_event(&event, &context, state).unwrap()
}
fn plan(s: &CandidateStrategy, p: Value) -> EntryPlan {
    let intents = run(
        s,
        StrategyEvent::Signal {
            source: N_STRATEGY.into(),
            payload: p,
        },
        &mut Value::Null,
        &[],
    );
    match intents.into_iter().next().unwrap() {
        TradeIntent::Open(p) => p,
        _ => panic!("expected entry"),
    }
}
fn rejection(s: &CandidateStrategy, p: Value) -> String {
    let mut state = Value::Null;
    assert!(run(
        s,
        StrategyEvent::Signal {
            source: N_STRATEGY.into(),
            payload: p
        },
        &mut state,
        &[]
    )
    .is_empty());
    state["decisions"]
        .as_object()
        .unwrap()
        .values()
        .next()
        .unwrap()["reason"]
        .as_str()
        .unwrap()
        .into()
}
fn quote(ts: i64, price: f64) -> ExecutionQuote {
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
        timestamp: ts,
    }
}
fn position() -> ManagedPosition {
    serde_json::from_value(json!({"id":"position","condition_order_id":"position","symbol":"PB0","instrument_id":"pb2611",
        "direction":"up","tier":"large","fill_price":1001.,"initial_stop_price":980.,"target_price":1043.,"risk":21.,
        "volume":1,"open_ts":"2026-10-21 09:16:00","bars_held":0,"max_mfe_r":0.,"max_mae_r":0.,
        "trailing_grade":0,"current_stop_price":980.,"lot1_closed":false,"is_closed":false})).unwrap()
}
fn closed(ts: &str) -> StrategyEvent {
    StrategyEvent::BarClosed {
        symbol: "PB0".into(),
        timeframe: "candidate15m".into(),
        timestamp: ts.into(),
        high: 2000.,
        low: 500.,
        close: 1000.,
    }
}

#[test]
fn reference_snapshots_are_valid_and_missing_fee_is_rejected() {
    Parameters::reference_snapshot().validate().unwrap();
    let mut p = parameters();
    p.fees
        .products
        .insert("PB".into(), serde_json::from_value(json!({})).unwrap());
    assert!(p.validate().is_err());
    p.fees.products.insert(
        "PB".into(),
        serde_json::from_value(json!({"roundtrip_fixed_yuan_per_lot":2.})).unwrap(),
    );
    assert_eq!(p.fees.products["PB"].bound(1000., 1001., 10.), 2.);
    p.validate().unwrap();
}

#[test]
fn registry_exposes_only_current_strategies_and_exit_modules() {
    let registry = StrategyRegistry::default();
    registry.validate(&definition(P2, parameters())).unwrap();
    let catalog = registry.catalog();
    let ids = |field: &str| -> BTreeSet<String> {
        catalog[field]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().or_else(|| v["id"].as_str()).unwrap().to_owned())
            .collect()
    };
    assert_eq!(
        ids("strategies"),
        [N_STRATEGY, P2, crate::trader::b5::ID, p3::ID]
            .into_iter()
            .map(str::to_owned)
            .collect()
    );
    assert_eq!(
        ids("exit_policies"),
        [
            "n_exit_v1",
            "protect_only_v1",
            "p2_fixed_exit_v1",
            p3::EXIT,
            crate::trader::b5::EXIT
        ]
        .into_iter()
        .map(str::to_owned)
        .collect()
    );
}

#[test]
fn calendar_warns_three_days_before_expiry_and_preserves_frozen_versions() {
    let definition = definition(P2, parameters());
    let original = serde_json::to_value(&definition).unwrap();
    let end = parse_time("2026-10-31 23:59:59").unwrap();
    assert_eq!(
        calendar_health(&definition, end - 259201).unwrap()["status"],
        "ok"
    );
    assert_eq!(
        calendar_health(&definition, end - 259200).unwrap()["status"],
        "expiring"
    );
    assert_eq!(
        calendar_health(&definition, end).unwrap()["status"],
        "expiring"
    );
    assert_eq!(
        calendar_health(&definition, end + 1).unwrap()["status"],
        "uncovered"
    );
    assert_eq!(
        calendar_health(&definition, parse_time("2026-06-30 23:59:59").unwrap()).unwrap()["status"],
        "uncovered"
    );
    assert_eq!(original, serde_json::to_value(&definition).unwrap());
    // The fresh preset covers November; an old saved calendar still fails closed.
    let now = parse_time("2026-11-03 13:45:00").unwrap();
    assert_eq!(parameters().block("JD0", now), Some("calendar_uncovered"));
    let fresh = Parameters::reference_snapshot();
    assert_eq!(fresh.block("JD0", now), None);
    assert_eq!(fresh.calendar.id, "holiday-windows-2026-jul-dec-v2");
    assert_eq!(
        fresh.block("JD0", parse_time("2026-12-31 09:00:00").unwrap()),
        Some("calendar_uncovered")
    );
    let mut invalid = definition;
    invalid.parameters["calendar"]["coverage_end"] = json!("bad timestamp");
    assert_eq!(calendar_health(&invalid, now).unwrap()["status"], "invalid");
}
#[test]
fn warning_filters_are_frozen_and_deduplicated_across_serialization() {
    let s = CandidateStrategy::new(parameters()).unwrap();
    let p = signal();
    let mut state = Value::Null;
    assert_eq!(
        run(
            &s,
            StrategyEvent::Signal {
                source: N_STRATEGY.into(),
                payload: p.clone()
            },
            &mut state,
            &[]
        )
        .len(),
        1
    );
    let mut restored = serde_json::from_slice(&serde_json::to_vec(&state).unwrap()).unwrap();
    assert!(run(
        &s,
        StrategyEvent::Signal {
            source: N_STRATEGY.into(),
            payload: p.clone()
        },
        &mut restored,
        &[]
    )
    .is_empty());
    let mut p = p.clone();
    p["entry_score"] = json!(2.99);
    assert_eq!(rejection(&s, p), "score_below_min");
    let mut p = signal();
    p["warning_context"]["complete"] = json!(false);
    assert_eq!(rejection(&s, p), "atr_unavailable");
    for atr in [8., 20. / 0.75] {
        let mut p = signal();
        p["warning_context"]["atr14"] = json!(atr);
        plan(&s, p);
    }
    let mut p = signal();
    p["warning_context"]["atr14"] = json!(7.99);
    assert_eq!(rejection(&s, p), "atr_out_of_range");
    let mut p = signal();
    p["stop"] = json!(991.);
    p["risk"] = json!(9.);
    assert_eq!(rejection(&s, p), "risk_ticks_below_min");
    let mut expensive = parameters();
    expensive
        .fees
        .products
        .get_mut("PB")
        .unwrap()
        .roundtrip_fixed_yuan_per_lot = Some(21.);
    assert_eq!(
        rejection(&CandidateStrategy::new(expensive).unwrap(), signal()),
        "friction_above20"
    );
    let mut p = signal();
    p["target"] = json!(1020.);
    assert_eq!(rejection(&s, p), "filtered_net_rr_plan");
}
#[test]
fn proposed_fill_uses_net_space_chase_fresh_book_and_actual_risk() {
    let s = CandidateStrategy::new(parameters()).unwrap();
    let p = plan(&s, signal());
    let now = p.created_at + 1;
    let q = quote(now, 1001.);
    assert!(matches!(
        s.check_entry(&p, &q, 1002., now),
        EntryDecision::Allow
    ));
    assert!(matches!(s.check_entry(&p,&q,1005.,now),EntryDecision::Reject(r) if r=="unfilled"));
    assert!(
        matches!(s.check_entry(&p,&q,989.,now),EntryDecision::Reject(r) if r=="filtered_actual_risk")
    );
    assert!(matches!(
        s.check_entry(&p, &q, 1002., now + 3),
        EntryDecision::Wait(_)
    ));
    let mut q = q;
    q.bid = q.ask + 1.;
    assert!(matches!(
        s.check_entry(&p, &q, 1002., now),
        EntryDecision::Wait(_)
    ));
    let mut payload = signal();
    payload["target"] = json!(1026.);
    let p = plan(&s, payload);
    assert!(
        matches!(s.check_entry(&p,&quote(now,1003.),1004.,now),EntryDecision::Reject(r) if r=="filtered_net_rr_fill")
    );
}

#[test]
fn saved_egg_plan_with_old_point_value_cannot_open_but_is_not_rewritten() {
    let mut params = parameters();
    let fees = params.fees.products["PB"].clone();
    params.fees.products.insert("JD".into(), fees);
    let strategy = CandidateStrategy::new(params).unwrap();
    let mut payload = signal();
    payload["symbol"] = json!("JD0");
    payload["instrument_id"] = json!("jd2701");
    let mut frozen = plan(&strategy, payload.clone());
    let now = frozen.created_at + 1;
    let mut q = quote(now, 1001.);
    q.instrument_id = "jd2701".into();
    q.underlying_symbol = "JD0".into();
    assert!(matches!(
        strategy.check_entry(&frozen, &q, 1002., now),
        EntryDecision::Allow
    ));
    frozen.evidence["candidate"]["multiplier"] = json!(5.);
    let original = frozen.evidence.clone();
    assert!(matches!(strategy.check_entry(&frozen, &q, 1002., now),
        EntryDecision::Reject(reason) if reason == "contract_metadata_mismatch"));
    assert_eq!(frozen.evidence, original);
    payload["multiplier"] = json!(5.);
    assert!(rejection(&strategy, payload).contains("stale_contract_point_value"));
}
#[test]
fn fixed_s1_target_does_not_move_with_fill_and_keeps_structure_stop() {
    let strategy = CandidateStrategy::new(parameters()).unwrap();
    for short in [false, true] {
        let mut payload = signal();
        if short {
            payload["direction"] = json!("down");
            payload["stop"] = json!(1020.);
            payload["target"] = json!(900.);
        }
        let plan = plan(&strategy, payload);
        let mut pos = position();
        pos.fill_price = if short { 998. } else { 1002. };
        assert_eq!(
            FixedExit.target_on_fill(&pos, &plan),
            Some(if short { 900. } else { 1100. })
        );
    }
    let pos = position();
    let update = FixedExit.on_quote(
        &pos,
        &quote(parse_time(&pos.open_ts).unwrap(), 1023.),
        &Value::Null,
    );
    assert!(update.close.is_none());
    assert_eq!(update.stop, 980.);
    assert!(FixedExit.on_bar(&pos, &Value::Null).is_none());
}

#[test]
fn atr_uses_only_complete_prefix_and_groups_midnight() {
    let start = parse_time("2026-10-20 20:35:00").unwrap();
    let raw = (0..45)
        .map(|i| Kline {
            datetime: label(start + i * 300),
            open: 100.,
            high: 105.,
            low: 95.,
            close: 100.,
            volume: 1.,
            hold: 0.,
        })
        .collect::<Vec<_>>();
    let warning = "2026-10-21 00:00:00";
    let c = warning_context(&raw, warning).unwrap().unwrap();
    assert!(c.complete);
    assert_eq!(c.atr14, Some(10.));
    let mut missing = raw.clone();
    missing.retain(|b| b.datetime != "2026-10-20 23:55:00");
    assert!(
        !warning_context(&missing, warning)
            .unwrap()
            .unwrap()
            .complete
    );
    let b = completed_bars(&raw, parse_time(warning).unwrap()).unwrap();
    assert_eq!(b.last().unwrap().datetime, warning);
    assert_eq!(b.last().unwrap().volume, 3.);
    let mut future = raw.clone();
    future.last_mut().unwrap().high = 10000.;
    assert_eq!(
        warning_context(&future, warning).unwrap().unwrap().atr14,
        c.atr14
    );
    let mut bad = raw.clone();
    bad.swap(0, 1);
    assert!(completed_bars(&bad, parse_time(warning).unwrap()).is_err());
}
#[test]
fn holding_buckets_ignore_lunch_and_future_mfe() {
    let s = CandidateStrategy::new(parameters()).unwrap();
    let p = position();
    let mut state = Value::Null;
    // A later quote must not rescue the fifth completed bucket while catching up after a gap.
    run(
        &s,
        StrategyEvent::Quote(quote(parse_time("2026-10-21 13:01:00").unwrap(), 1040.)),
        &mut state,
        &[p.clone()],
    );
    for ts in ["09:30:00", "09:45:00", "10:00:00", "10:15:00"] {
        assert!(run(
            &s,
            closed(&format!("2026-10-21 {ts}")),
            &mut state,
            &[p.clone()]
        )
        .is_empty());
    }
    let actions = run(&s, closed("2026-10-21 10:30:00"), &mut state, &[p.clone()]);
    assert!(matches!(&actions[0],TradeIntent::Close{reason,..} if reason=="no_follow"));
    let mut state = Value::Null;
    run(
        &s,
        StrategyEvent::Quote(quote(parse_time("2026-10-21 09:20:00").unwrap(), 1011.5)),
        &mut state,
        &[p.clone()],
    );
    let start = parse_time("2026-10-21 09:30:00").unwrap();
    for n in 0..60 {
        let actions = run(
            &s,
            closed(&label(start + n * 900)),
            &mut state,
            &[p.clone()],
        );
        if n == 59 {
            assert!(matches!(&actions[0],TradeIntent::Close{reason,..} if reason=="time_exit"));
        } else {
            assert!(actions.is_empty());
        }
    }
}
#[test]
fn holiday_and_weekend_cutoffs_hold_until_actual_close_even_without_quotes() {
    let params = parameters();
    assert_eq!(
        params.block("PB0", parse_time("2026-09-30 14:30:00").unwrap()),
        Some("filtered_holiday_window")
    );
    assert_eq!(
        params.block("PB0", parse_time("2026-11-02 09:00:00").unwrap()),
        Some("calendar_uncovered")
    );
    assert!(params
        .block("PB0", parse_time("2026-10-23 14:50:00").unwrap())
        .is_none());
    assert_eq!(
        params.block("SF0", parse_time("2026-10-23 14:50:00").unwrap()),
        Some("filtered_weekend_window")
    );
    assert_eq!(
        params.block("PB0", parse_time("2026-10-24 00:50:00").unwrap()),
        Some("filtered_weekend_window")
    );
    let s = CandidateStrategy::new(params).unwrap();
    let mut p = position();
    p.open_ts = "2026-09-30 10:00:00".into();
    let actions = run(
        &s,
        StrategyEvent::Timer {
            timestamp: parse_time("2026-10-09 09:01:00").unwrap(),
        },
        &mut Value::Null,
        &[p],
    );
    assert!(matches!(&actions[0],TradeIntent::Close{reason,..} if reason=="pre_holiday"));
    let mut p = position();
    p.open_ts = "2026-09-30 14:46:00".into();
    assert!(
        matches!(&run(&s,StrategyEvent::Timer{timestamp:parse_time("2026-09-30 14:46:01").unwrap()},&mut Value::Null,&[p])[0],TradeIntent::Close{reason,..} if reason=="pre_holiday")
    );
}

#[test]
fn weekend_cutoffs_follow_actual_contract_sessions_in_shanghai_time() {
    let params = parameters();
    for (instrument, before, cutoff) in [
        ("jd2701", "2026-10-23 14:49:59", "2026-10-23 14:50:00"),
        ("rb2701", "2026-10-23 22:49:59", "2026-10-23 22:50:00"),
        ("pb2611", "2026-10-24 00:49:59", "2026-10-24 00:50:00"),
        ("au2612", "2026-10-24 02:19:59", "2026-10-24 02:20:00"),
    ] {
        assert_eq!(params.block(instrument, parse_time(before).unwrap()), None);
        assert_eq!(
            params.block(instrument, parse_time(cutoff).unwrap()),
            Some("filtered_weekend_window"),
            "{instrument}: {cutoff}"
        );
        assert_eq!(
            params.block(instrument, parse_time("2026-10-26 08:59:59").unwrap()),
            Some("filtered_weekend_window")
        );
        assert_eq!(
            params.block(instrument, parse_time("2026-10-26 09:00:00").unwrap()),
            None
        );
    }
    // The same instant must give the same decision on a UTC-configured host.
    let utc = chrono::DateTime::parse_from_rfc3339("2026-10-23T16:50:00Z").unwrap();
    assert_eq!(
        params.block("pb2611", utc.timestamp()),
        Some("filtered_weekend_window")
    );
    // A stale alias must not select the session for a position's bound contract.
    let s = CandidateStrategy::new(params.clone()).unwrap();
    let mut p = position();
    p.symbol = "JD0".into();
    p.open_ts = "2026-10-23 14:00:00".into();
    assert!(run(
        &s,
        StrategyEvent::Timer {
            timestamp: parse_time("2026-10-23 14:50:00").unwrap()
        },
        &mut Value::Null,
        &[p.clone()]
    )
    .is_empty());
    let actions = run(
        &s,
        StrategyEvent::Timer {
            timestamp: utc.timestamp(),
        },
        &mut Value::Null,
        &[p],
    );
    assert!(matches!(&actions[0], TradeIntent::Close {reason, ..} if reason == "pre_weekend"));
    // Missed exits remain due on Monday; a newly opened Monday position is not overdue.
    let monday = parse_time("2026-10-26 09:01:00").unwrap();
    assert_eq!(
        params.flat_reason("pb2611", parse_time("2026-10-23 14:00:00").unwrap(), monday),
        Some("pre_weekend")
    );
    assert_eq!(params.flat_reason("pb2611", monday, monday), None);
}

#[test]
fn frozen_holiday_suppresses_a_nonexistent_friday_night_close() {
    let mut params = parameters();
    params.calendar.windows.push(HolidayWindow {
        name: "test-friday-night-off".into(),
        block_from: "2026-10-23 14:50:00".into(),
        flat_at: "2026-10-23 14:50:00".into(),
        last_close: "2026-10-23 15:00:00".into(),
        reopen: "2026-10-26 09:00:00".into(),
    });
    params.validate().unwrap();
    let now = parse_time("2026-10-23 14:50:00").unwrap();
    assert!(params
        .weekend_windows("pb2611", now)
        .contains(&(now, parse_time("2026-10-26 09:00:00").unwrap())));
    assert_eq!(
        params.flat_reason("pb2611", now - 3600, now),
        Some("pre_holiday")
    );
    assert!(!params
        .weekend_windows("pb2611", now)
        .iter()
        .any(|(cut, _)| *cut == parse_time("2026-10-24 00:50:00").unwrap()));
}

#[test]
fn no_follow_uses_first_five_bucket_mfe_once_even_if_close_events_arrive_late() {
    let s = CandidateStrategy::new(parameters()).unwrap();
    for short in [false, true] {
        let mut p = position();
        p.direction = if short { "down" } else { "up" }.into();
        let sign = if short { -1. } else { 1. };
        let mut state = Value::Null;
        for (i, time) in [
            "09:20:00", "09:35:00", "09:50:00", "10:05:00", "10:35:00", "10:50:00",
        ]
        .iter()
        .enumerate()
        {
            let r = if i < 5 { 0.1 } else { 0.8 };
            run(
                &s,
                StrategyEvent::Quote(quote(
                    parse_time(&format!("2026-10-21 {time}")).unwrap(),
                    p.fill_price + sign * p.risk * r,
                )),
                &mut state,
                &[p.clone()],
            );
        }
        // Pre-change state files have no no_follow_checked field.
        state["holding"]["position"]
            .as_object_mut()
            .unwrap()
            .remove("no_follow_checked");
        let actions = run(&s, closed("2026-10-21 11:00:00"), &mut state, &[p.clone()]);
        assert!(matches!(&actions[0], TradeIntent::Close {reason, ..} if reason == "no_follow"));
        assert!(run(&s, closed("2026-10-21 11:15:00"), &mut state, &[p]).is_empty());
    }
}

#[test]
fn no_follow_keeps_a_position_that_reached_half_r_then_retraced() {
    let s = CandidateStrategy::new(parameters()).unwrap();
    for short in [false, true] {
        let mut p = position();
        p.direction = if short { "down" } else { "up" }.into();
        let sign = if short { -1. } else { 1. };
        let mut state = Value::Null;
        run(
            &s,
            StrategyEvent::Quote(quote(
                parse_time("2026-10-21 09:20:00").unwrap(),
                p.fill_price + sign * p.risk * NO_FOLLOW_MFE_R,
            )),
            &mut state,
            &[p.clone()],
        );
        run(
            &s,
            StrategyEvent::Quote(quote(
                parse_time("2026-10-21 10:35:00").unwrap(),
                p.fill_price + sign * p.risk * 0.1,
            )),
            &mut state,
            &[p.clone()],
        );
        for time in ["09:30:00", "09:45:00", "10:00:00", "10:15:00", "10:45:00"] {
            assert!(run(
                &s,
                closed(&format!("2026-10-21 {time}")),
                &mut state,
                &[p.clone()]
            )
            .is_empty());
        }
    }
}

#[test]
fn pending_expiry_counts_observed_buckets_without_filling_session_gaps() {
    let s = CandidateStrategy::new(parameters()).unwrap();
    let mut state = Value::Null;
    let config = ExecutionConfig::default();
    let quotes = HashMap::new();
    let cond=serde_json::from_value(json!({"id":"pending","signal_id":"source","symbol":"PB0","instrument_id":"pb2611","direction":"up","tier":"small",
        "entry_price":1000.,"stop_price":980.,"target_price":1100.,"risk":20.,"score":4.,"volume":1,
        "warning_bar_ts":"2026-10-21 09:15:00","bars_waited":0,"max_wait_bars":12,"state":"pending",
        "created_at":parse_time("2026-10-21 09:15:00").unwrap(),"note":"test"})).unwrap();
    let conditions = [cond];
    let context = StrategyContext {
        instance_id: "test",
        positions: &[],
        orders: &[],
        conditions: &conditions,
        account: None,
        quotes: &quotes,
        config: &config,
        allow_entries: true,
    };
    let labels = [
        "09:30:00", "09:45:00", "10:00:00", "10:15:00", "10:45:00", "11:00:00", "11:15:00",
        "11:30:00", "13:45:00", "14:00:00", "14:15:00", "14:30:00", "14:45:00",
    ];
    for (n, time) in labels.into_iter().enumerate() {
        let actions = s
            .on_event(&closed(&format!("2026-10-21 {time}")), &context, &mut state)
            .unwrap();
        if n == 12 {
            assert!(
                matches!(&actions[0],TradeIntent::CancelEntry{reason,..} if reason=="untriggered:12_buckets")
            );
        } else {
            assert!(actions.is_empty());
        }
    }
}
