use super::super::{execution::*, strategy::FrozenPlan};
use super::*;
use std::collections::HashMap;

fn strategy() -> B5 {
    B5::new(serde_json::from_value(definition().parameters).unwrap()).unwrap()
}
fn snap(symbol: &str) -> Snapshot {
    Snapshot {
        key: format!("{symbol}|up|a|b|w|w"),
        cluster: format!("{symbol}|up|a|b"),
        symbol: symbol.into(),
        instrument: format!(
            "{}2701",
            crate::session::SessionCalendar::contract_prefix(symbol).to_lowercase()
        ),
        direction: Direction::Long,
        warning_ts: "2026-10-27 10:00:00".into(),
        available_at: candidates::parse_time("2026-10-27 10:00:00").unwrap(),
        entry: 1000.,
        stop: 980.,
        target: 1100.,
        risk: 20.,
        tick: 1.,
        multiplier: 10.,
        score: 3.,
        hour: Some(0.),
        confirm_minute: false,
        body_net: 10.,
        fees: serde_json::from_value(json!({"roundtrip_fixed_yuan_per_lot":6.})).unwrap(),
    }
}
fn plan(s: Snapshot) -> EntryPlan {
    EntryPlan {
        signal_id: s.key.clone(),
        symbol: s.symbol.clone(),
        instrument_id: s.instrument.clone(),
        direction: s.direction,
        reference_price: s.entry,
        trigger: EntryTrigger::Breakout { price: s.entry },
        stop_price: Some(s.stop),
        target_price: Some(s.target),
        risk_per_unit: s.risk,
        requested_volume: 1,
        created_at: s.available_at,
        expires_at: None,
        reason: "test".into(),
        evidence: json!({"b5":s}),
    }
}
fn condition(id: &str, s: Snapshot, state: ConditionState) -> ConditionOrder {
    let p = plan(s.clone());
    ConditionOrder {
        frozen: Some(FrozenPlan {
            definition: definition(),
            execution: ExecutionConfig::default(),
            plan: p,
        }),
        origin: TradeOrigin {
            strategy_id: ID.into(),
            strategy_version: "1".into(),
            signal_id: s.key,
            ..Default::default()
        },
        id: id.into(),
        event_id: None,
        symbol: s.symbol,
        instrument_id: s.instrument,
        direction: "up".into(),
        tier: PositionTier::Small,
        entry_price: s.entry,
        stop_price: s.stop,
        target_price: s.target,
        risk: s.risk,
        score: s.score,
        volume: 1,
        warning_bar_ts: s.warning_ts,
        trigger_ts: Some("2026-10-27 10:01:00".into()),
        bars_waited: 0,
        max_wait_bars: usize::MAX,
        state,
        created_at: s.available_at,
        note: String::new(),
    }
}
fn closed(id: &str, exit: &str, reason: &str) -> TradeRecord {
    serde_json::from_value(json!({"id":id,"condition_order_id":id,"symbol":"EG0","instrument_id":"eg2701",
        "direction":"up","tier":"small","volume":1,"fill_price":1000.,"exit_price":980.,"risk":20.,
        "initial_stop_price":980.,"target_price":1100.,"open_ts":"2026-10-27 09:15:00","exit_ts":exit,
        "exit_reason":reason,"realized_pnl_pts":-20.,"r_multiple":-1.,"max_mfe_r":0.,"max_mae_r":-1.,"bars_held":1})).unwrap()
}
fn decision(
    s: &B5,
    plan: &EntryPlan,
    conditions: &HashMap<String, ConditionOrder>,
    history: &[TradeRecord],
    state: &Value,
    now: i64,
) -> EntryDecision {
    s.check_portfolio(
        plan,
        &EntryPortfolio {
            conditions,
            positions: &HashMap::new(),
            orders: &HashMap::new(),
            history,
            state,
        },
        now,
    )
}
fn assert_reason(d: EntryDecision, expected: &str) {
    match d {
        EntryDecision::Reject(s) | EntryDecision::Wait(s) => assert_eq!(s, expected),
        _ => panic!("expected {expected}"),
    }
}

#[test]
fn frozen_research_geometry_and_selection_match_24_warnings() {
    let fixtures: Value = serde_json::from_str(include_str!("research-fixture.json")).unwrap();
    for f in fixtures.as_array().unwrap() {
        let raw: Vec<Kline> = serde_json::from_value(f["raw"].clone()).unwrap();
        let symbol = f["symbol"].as_str().unwrap();
        let tick = f["tick"].as_f64().unwrap();
        let all = generator::bars(&raw, Timeframe::M15, &[]);
        let completed = candidates::completed_bars(
            &raw,
            candidates::parse_time(&raw.last().unwrap().datetime).unwrap(),
        )
        .unwrap();
        let mut state = vec![];
        let got = generator::extract(symbol, &all, tick, 0, &mut state).unwrap();
        let got: Vec<_> = got.iter().filter(|c| c.entry_score >= 2.5).collect();
        assert_eq!(
            got.len(),
            f["expected"].as_array().unwrap().len(),
            "{symbol}"
        );
        for (c, r) in got.iter().zip(f["expected"].as_array().unwrap()) {
            let up = c.direction == crate::analyze::model::Dir::Up;
            let ts = all[c.warning_index].dt.to_bar_ts();
            assert_eq!(ts, r["event"]["warning_ts"]);
            assert_eq!(all[c.s0_index].dt.to_bar_ts(), r["event"]["s0_ts"]);
            assert_eq!(all[c.s1_index].dt.to_bar_ts(), r["event"]["s1_ts"]);
            assert!(
                (c.entry_score
                    - r["event"]["setup_features"]["setup_quality"]
                        .as_f64()
                        .unwrap())
                .abs()
                    < 1e-9
            );
            assert_eq!(
                generator::grid(c.entry, tick, up),
                r["plan"]["planned_entry"].as_f64().unwrap()
            );
            assert_eq!(
                generator::grid(c.stop, tick, !up),
                r["plan"]["planned_stop"].as_f64().unwrap()
            );
            assert_eq!(
                generator::local_target(c, &completed, &ts, tick),
                r["plan"]["planned_target"].as_f64().unwrap()
            );
            let body = if up { 1. } else { -1. }
                * all[c.s0_index + 1..=c.s1_index]
                    .iter()
                    .map(|b| b.close - b.open)
                    .sum::<f64>();
            assert!((body - r["feature"]["a_body_net_ex_s0"].as_f64().unwrap()).abs() < 1e-9);
        }
        // Chunk boundaries and restart serialization must not change service dedup.
        let mut incremental = vec![];
        let mut seen = vec![];
        let mut from = 0;
        while from < all.len() {
            let end = (from + 17).min(all.len());
            incremental
                .extend(generator::extract(symbol, &all[..end], tick, from, &mut seen).unwrap());
            seen = serde_json::from_value(serde_json::to_value(&seen).unwrap()).unwrap();
            from = end;
        }
        let keys = |v: Vec<crate::analyze::event::WarningCandidate>| {
            v.into_iter()
                .map(|c| (c.warning_index, c.s0_index, c.s1_index, c.direction))
                .collect::<Vec<_>>()
        };
        assert_eq!(
            keys(incremental),
            keys(generator::extract(symbol, &all, tick, 0, &mut vec![]).unwrap())
        );
    }
}

#[test]
fn midnight_endpoint_is_independent_of_legacy_aggregation() {
    let raw: Vec<Kline> = (0..3)
        .map(|i| Kline {
            datetime: format!("2026-10-27 {}", ["23:50:00", "23:55:00", "00:00:00"][i]),
            open: 100.,
            high: 102.,
            low: 99.,
            close: 101.,
            volume: 1.,
            hold: 1.,
        })
        .collect();
    let mut raw = raw;
    raw[2].datetime = "2026-10-28 00:00:00".into();
    let b = generator::bars(&raw, Timeframe::M15, &[]);
    assert_eq!(b.len(), 1);
    assert_eq!(b[0].dt.to_bar_ts(), "2026-10-28 00:00:00");
    assert_eq!(b[0].volume, 3.);
}

#[test]
fn selective_sector_reservations_ignore_untriggered_and_aligned_but_keep_unknown() {
    let strategy = strategy();
    let p = plan(snap("V0"));
    let now = candidates::parse_time("2026-10-27 10:05:00").unwrap();
    let mut c = HashMap::from([
        (
            "eg".into(),
            condition("eg", snap("EG0"), ConditionState::Unknown),
        ),
        (
            "pp".into(),
            condition("pp", snap("PP0"), ConditionState::Working),
        ),
    ]);
    assert_reason(
        decision(&strategy, &p, &c, &[], &Value::Null, now),
        "sector_counter_slots_full",
    );
    c.get_mut("pp").unwrap().trigger_ts = None;
    c.get_mut("pp").unwrap().state = ConditionState::Pending;
    assert!(matches!(
        decision(&strategy, &p, &c, &[], &Value::Null, now),
        EntryDecision::Allow
    ));
    c.get_mut("pp").unwrap().trigger_ts = Some("2026-10-27 10:01:00".into());
    let mut aligned = snap("V0");
    aligned.hour = Some(1.);
    assert!(matches!(
        decision(&strategy, &plan(aligned), &c, &[], &Value::Null, now),
        EntryDecision::Allow
    ));
    assert!(matches!(
        decision(&strategy, &plan(snap("JD0")), &c, &[], &Value::Null, now),
        EntryDecision::Allow
    ));
    let mut next = snap("EG0");
    next.key = "new-warning".into();
    assert_reason(
        decision(&strategy, &plan(next), &c, &[], &Value::Null, now),
        "blocked_symbol_slot",
    );
}

#[test]
fn only_two_realized_stops_strictly_before_trigger_minute_retire_same_cluster() {
    let strategy = strategy();
    let p = plan(snap("EG0"));
    let now = candidates::parse_time("2026-10-27 10:05:20").unwrap();
    let mut c = HashMap::new();
    for id in ["one", "two"] {
        let mut s = snap("EG0");
        s.key = id.into();
        c.insert(id.into(), condition(id, s, ConditionState::Closed));
    }
    let mut h = vec![
        closed("one", "2026-10-27 10:03:00", "stop"),
        closed("two", "2026-10-27 10:04:00", "no_follow"),
    ];
    assert!(matches!(
        decision(&strategy, &p, &c, &h, &Value::Null, now),
        EntryDecision::Allow
    ));
    h[1].exit_reason = "stop".into();
    assert_reason(
        decision(&strategy, &p, &c, &h, &Value::Null, now),
        "cluster_two_realized_stops",
    );
    h[1].exit_ts = "2026-10-27 10:05:00".into();
    assert_reason(
        decision(&strategy, &p, &c, &h, &Value::Null, now),
        "blocked_symbol_slot",
    );
    h[1].exit_ts = "2026-10-27 10:04:00".into();
    let mut new = snap("EG0");
    new.cluster = "new-a".into();
    assert!(matches!(
        decision(&strategy, &plan(new), &c, &h, &Value::Null, now),
        EntryDecision::Allow
    ));
}

#[test]
fn minute_confirmation_waits_for_observed_close_and_survives_restart() {
    let strategy = strategy();
    let mut s = snap("EG0");
    s.confirm_minute = true;
    let o = condition("order", s.clone(), ConditionState::Pending);
    let conditions = [o.clone()];
    let cfg = ExecutionConfig::default();
    let quotes = HashMap::new();
    let context = StrategyContext {
        instance_id: "test",
        positions: &[],
        orders: &[],
        conditions: &conditions,
        account: None,
        quotes: &quotes,
        config: &cfg,
        allow_entries: true,
    };
    let start = s.available_at + 60;
    let mut state = Value::Null;
    let q = |timestamp| ExecutionQuote {
        instrument_id: s.instrument.clone(),
        exchange_id: "DCE".into(),
        underlying_symbol: "EG0".into(),
        last: 1001.,
        bid: 1000.,
        ask: 1002.,
        tick: 1.,
        lower: 500.,
        upper: 2000.,
        timestamp,
    };
    for timestamp in [start, start + 20, start + 50, start + 59] {
        strategy
            .on_event(&StrategyEvent::Quote(q(timestamp)), &context, &mut state)
            .unwrap();
    }
    assert!(state["confirmed"][&s.key].is_null());
    state = serde_json::from_str(&serde_json::to_string(&state).unwrap()).unwrap();
    strategy
        .on_event(&StrategyEvent::Quote(q(start + 60)), &context, &mut state)
        .unwrap();
    assert_eq!(state["confirmed"][&s.key], start + 60);
    let c = HashMap::from([("order".into(), o)]);
    assert!(matches!(
        decision(&strategy, &plan(s.clone()), &c, &[], &state, start + 60),
        EntryDecision::Allow
    ));
    assert_reason(
        decision(&strategy, &plan(s), &c, &[], &state, start + 120),
        "confirmation_fill_window_missed",
    );
}

#[test]
fn frozen_modules_universe_economic_guards_and_calendar_are_enforced() {
    let registry = StrategyRegistry::default();
    let d = definition();
    registry.validate(&d).unwrap();
    let s = registry.strategy(&d).unwrap();
    assert_eq!(
        s.subscriptions()
            .iter()
            .map(|s| s.symbol.clone())
            .collect::<Vec<_>>(),
        SYMBOLS
    );
    let mut bad = d.clone();
    bad.sizing_policy = "fixed_v1".into();
    assert!(registry.validate(&bad).is_err());
    let mut bad = d;
    bad.exit_policy = "protect_only_v1".into();
    assert!(registry.validate(&bad).is_err());
    let b = strategy();
    let mut s = snap("EG0");
    let now = s.available_at;
    assert_eq!(b.plan_reason(&s, now, true), None);
    s.body_net = 0.;
    assert_eq!(b.plan_reason(&s, now, true), Some("a_body_net_nonpositive"));
    s.body_net = 10.;
    s.target = 1010.;
    assert_eq!(b.plan_reason(&s, now, true), Some("target_cost_above10"));
    s = snap("EG0");
    s.hour = Some(-1.);
    assert!(!pre_long_break(
        "EG0",
        candidates::parse_time("2026-10-27 10:10:00").unwrap()
    ));
    assert!(pre_long_break(
        "EG0",
        candidates::parse_time("2026-10-27 11:20:00").unwrap()
    ));
    assert_eq!(
        b.entry_block(&s, candidates::parse_time("2026-12-30 14:50:00").unwrap()),
        Some("calendar_coverage_ending")
    );
}

#[test]
fn history_warms_without_backfill_then_emits_once_using_main_symbol_quote_key() {
    let fixtures: Value = serde_json::from_str(include_str!("research-fixture.json")).unwrap();
    let f = &fixtures[0];
    let raw: Vec<Kline> = serde_json::from_value(f["raw"].clone()).unwrap();
    let ts = "2026-07-20 14:15:00";
    let now = candidates::parse_time(ts).unwrap() + 5;
    let before: Vec<_> = raw
        .iter()
        .filter(|b| b.datetime.as_str() < ts)
        .cloned()
        .collect();
    let through: Vec<_> = raw
        .iter()
        .filter(|b| b.datetime.as_str() <= ts)
        .cloned()
        .collect();
    let quote = |timestamp| ExecutionQuote {
        instrument_id: "eg2609".into(),
        exchange_id: "DCE".into(),
        underlying_symbol: "DCE.eg2609".into(),
        last: 4600.,
        bid: 4599.,
        ask: 4601.,
        tick: 1.,
        lower: 2000.,
        upper: 8000.,
        timestamp,
    };
    let mut quotes = HashMap::from([("EG0".into(), quote(now - 900))]);
    let cfg = ExecutionConfig::default();
    let mut state = Value::Null;
    let b = strategy();
    let run =
        |bars: Vec<Kline>, time, quotes: &HashMap<String, ExecutionQuote>, state: &mut Value| {
            let c = StrategyContext {
                instance_id: "test",
                positions: &[],
                orders: &[],
                conditions: &[],
                account: None,
                quotes,
                config: &cfg,
                allow_entries: true,
            };
            b.on_event(
                &StrategyEvent::History5m {
                    symbol: "EG0".into(),
                    bars,
                    rollovers: vec![],
                    timestamp: time,
                },
                &c,
                state,
            )
            .unwrap()
        };
    assert!(run(before, now - 900, &quotes, &mut state).is_empty());
    quotes.insert("EG0".into(), quote(now));
    let intents = run(through.clone(), now, &quotes, &mut state);
    assert_eq!(intents.len(), 1, "{state}");
    let TradeIntent::Open(p) = &intents[0] else {
        panic!("expected new warning")
    };
    assert_eq!(p.symbol, "EG0");
    assert_eq!(p.instrument_id, "eg2609");
    assert_eq!(p.requested_volume, 1);
    assert_eq!(p.evidence["warning_ts"], ts);
    state = serde_json::from_str(&serde_json::to_string(&state).unwrap()).unwrap();
    assert!(run(through.clone(), now, &quotes, &mut state).is_empty());
    // Future input is excluded before feature generation and before raw cursors.
    let cursor = state["raw_cursor"]["EG0"].clone();
    assert!(run(raw, now, &quotes, &mut state).is_empty());
    assert_eq!(state["raw_cursor"]["EG0"], cursor);
}

#[test]
fn quote_gap_cannot_supply_a_fabricated_confirmation() {
    let b = strategy();
    let mut s = snap("EG0");
    s.confirm_minute = true;
    let conditions = [condition("gap", s.clone(), ConditionState::Pending)];
    let cfg = ExecutionConfig::default();
    let quotes = HashMap::new();
    let c = StrategyContext {
        instance_id: "test",
        positions: &[],
        orders: &[],
        conditions: &conditions,
        account: None,
        quotes: &quotes,
        config: &cfg,
        allow_entries: true,
    };
    let mut state = Value::Null;
    for timestamp in [s.available_at + 60, s.available_at + 120] {
        let q = ExecutionQuote {
            instrument_id: s.instrument.clone(),
            exchange_id: "DCE".into(),
            underlying_symbol: "EG0".into(),
            last: 1001.,
            bid: 1000.,
            ask: 1002.,
            tick: 1.,
            lower: 500.,
            upper: 2000.,
            timestamp,
        };
        b.on_event(&StrategyEvent::Quote(q), &c, &mut state)
            .unwrap();
    }
    assert!(state["confirmed"][&s.key].is_null());
}
