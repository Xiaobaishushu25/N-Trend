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
fn warning_filters_are_frozen_and_deduplicated_across_serialization() {
    let s = CandidateStrategy::new(parameters(), false).unwrap();
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
        rejection(&CandidateStrategy::new(expensive, false).unwrap(), signal()),
        "friction_above20"
    );
    let mut p = signal();
    p["target"] = json!(1020.);
    assert_eq!(rejection(&s, p), "filtered_net_rr_plan");
}
#[test]
fn proposed_fill_uses_net_space_chase_fresh_book_and_actual_risk() {
    let s = CandidateStrategy::new(parameters(), false).unwrap();
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
fn p3_caps_both_directions_at_actual_fill_and_never_extends_s1() {
    for short in [false, true] {
        let s = CandidateStrategy::new(parameters(), true).unwrap();
        let mut payload = signal();
        if short {
            payload["direction"] = json!("down");
            payload["stop"] = json!(1020.);
            payload["target"] = json!(900.);
        }
        let p = plan(&s, payload);
        let mut pos = position();
        pos.fill_price = if short { 998. } else { 1002. };
        assert_eq!(
            FixedExit { cap: true }.target_on_fill(&pos, &p),
            Some(if short { 954. } else { 1046. })
        );
        assert_eq!(
            FixedExit { cap: false }.target_on_fill(&pos, &p),
            Some(if short { 900. } else { 1100. })
        );
        let mut snap: Snapshot = serde_json::from_value(p.evidence["candidate"].clone()).unwrap();
        snap.target = if short { 970. } else { 1030. };
        assert_eq!(snap.target(pos.fill_price, true), snap.target);
    }
    let p = position();
    let exit = FixedExit { cap: true };
    let update = exit.on_quote(
        &p,
        &quote(parse_time(&p.open_ts).unwrap(), 1023.),
        &Value::Null,
    );
    assert!(update.close.is_none());
    assert_eq!(update.stop, 980.);
    assert!(exit.on_bar(&p, &Value::Null).is_none());
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
    let s = CandidateStrategy::new(parameters(), true).unwrap();
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
    let s = CandidateStrategy::new(params, true).unwrap();
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
fn pending_expiry_counts_observed_buckets_without_filling_session_gaps() {
    let s = CandidateStrategy::new(parameters(), true).unwrap();
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
