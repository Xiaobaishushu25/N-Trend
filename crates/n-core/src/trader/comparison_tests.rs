use super::super::execution::ConditionState;
use super::*;
use crate::trader::PositionTier;
use std::collections::HashMap;

fn ts(s: &str) -> i64 {
    Local
        .from_local_datetime(
            &chrono::NaiveDateTime::parse_from_str(s, "%Y-%m-%d %H:%M:%S").unwrap(),
        )
        .single()
        .unwrap()
        .timestamp()
}
fn signal(id: &str, now: i64, risk: f64) -> ConditionOrder {
    ConditionOrder {
        origin: Default::default(),
        frozen: None,
        id: id.into(),
        event_id: Some(1),
        symbol: "PB0".into(),
        instrument_id: "pb2611".into(),
        direction: "up".into(),
        tier: PositionTier::Small,
        entry_price: 100.0,
        stop_price: 100.0 - risk,
        target_price: 140.0,
        risk,
        score: 3.0,
        volume: 1,
        warning_bar_ts: "2026-10-20 10:00:00".into(),
        trigger_ts: None,
        bars_waited: 0,
        max_wait_bars: 12,
        state: ConditionState::Pending,
        created_at: now,
        note: "test".into(),
    }
}
fn quote(now: i64, last: f64) -> HashMap<String, ExecutionQuote> {
    [(
        "PB2611".into(),
        ExecutionQuote {
            instrument_id: "pb2611".into(),
            exchange_id: "SHFE".into(),
            underlying_symbol: String::new(),
            last,
            bid: last - 0.5,
            ask: last + 0.5,
            tick: 1.0,
            lower: 1.0,
            upper: 2000.0,
            timestamp: now,
        },
    )]
    .into_iter()
    .collect()
}
fn record(book: &ComparisonBook) -> &PairedSignal {
    book.runs[0].signals.values().next().unwrap()
}

#[test]
fn four_lanes_freeze_inputs_skip_narrow_entries_and_keep_zero_exposure_distinct() {
    let now = ts("2026-10-20 10:00:00");
    let mut b = ComparisonBook::default();
    let mut s = signal("one", now, 5.0);
    b.register(&s, &ExecutionConfig::default(), now);
    s.risk = 99.0;
    b.register(&s, &ExecutionConfig::default(), now + 1);
    b.advance(&quote(now, 100.0), now);
    assert_eq!(record(&b).condition.risk, 5.0);
    assert_eq!(record(&b).variants[0].status, "open");
    assert_eq!(record(&b).variants[1].status, "filtered");
    assert_eq!(record(&b).variants[3].status, "filtered");
    b.advance(&quote(now + 1, 107.0), now + 1);
    let summary = b.summary();
    assert_eq!(summary["runs"][0]["groups"][0]["closed"], 1);
    assert_eq!(summary["runs"][0]["groups"][1]["closed"], 0);
    assert!(summary["runs"][0]["groups"][1]["win_rate_pct"].is_null());
    assert_eq!(summary["runs"][0]["pairs"][0]["matched_signals"], 1);
    assert!(
        summary["runs"][0]["pairs"][0]["estimated_net_delta"]
            .as_f64()
            .unwrap()
            < 0.0
    );
}

#[test]
fn quote_replay_and_restart_do_not_duplicate_entries_or_exit_fills() {
    let now = ts("2026-10-20 10:00:00");
    let mut b = ComparisonBook::default();
    b.register(&signal("one", now, 10.0), &ExecutionConfig::default(), now);
    b.advance(&quote(now, 100.0), now);
    let raw = serde_json::to_vec(&b).unwrap();
    let mut b: ComparisonBook = serde_json::from_slice(&raw).unwrap();
    b.advance(&quote(now, 100.0), now);
    assert!(record(&b)
        .variants
        .iter()
        .all(|v| v.remaining == 1 && v.fills.is_empty()));
    b.advance(&quote(now + 1, 112.0), now + 1);
    for _ in 0..5 {
        b.advance(&quote(now + 1, 112.0), now + 1);
    }
    assert!(record(&b)
        .variants
        .iter()
        .all(|v| v.fills.len() == 1 && v.status == "closed"));
    assert_eq!(b.summary()["runs"][0]["groups"][0]["closed"], 1);
}

#[test]
fn changed_parameters_create_a_new_batch_and_evidence_is_immutable() {
    let now = ts("2026-10-20 10:00:00");
    let mut b = ComparisonBook::default();
    let mut cfg = ExecutionConfig::default();
    cfg.min_entry_risk_ticks = 0;
    b.register(&signal("one", now, 10.0), &cfg, now);
    b.attach_evidence(
        "one",
        serde_json::json!({"event_logic_version":"6","score":3.0}),
    );
    b.attach_evidence(
        "one",
        serde_json::json!({"event_logic_version":"7","score":4.0}),
    );
    cfg.min_entry_risk_ticks = 10;
    b.register(&signal("two", now + 1, 10.0), &cfg, now + 1);
    assert_eq!(b.runs.len(), 2);
    assert_ne!(b.runs[0].definition.id, b.runs[1].definition.id);
    assert_eq!(
        b.runs[0].definition.execution_config.min_entry_risk_ticks,
        0
    );
    assert_eq!(
        record(&b).signal_evidence.as_ref().unwrap()["event_logic_version"],
        "6"
    );
}

#[test]
fn continuous_trailing_shadow_matches_live_policy_for_longs_and_shorts() {
    let now = ts("2026-10-20 10:00:00");
    for sign in [1.0, -1.0] {
        let mut s = signal("continuous", now, 10.0);
        s.direction = if sign > 0.0 { "up" } else { "down" }.into();
        s.stop_price = 100.0 - sign * 10.0;
        s.target_price = 100.0 + sign * 100.0;
        s.tier = PositionTier::Large;
        s.volume = 2;
        let mut book = ComparisonBook::default();
        book.register(&s, &ExecutionConfig::default(), now);
        book.advance(&quote(now, 100.0), now);
        let lane = &record(&book).variants[0];
        let fill = lane.fill_price.unwrap();
        let risk = lane.initial_risk.unwrap();
        book.advance(&quote(now + 1, fill + sign * 1.1 * risk), now + 1);
        assert!(record(&book).variants[0].first_leg_closed);
        assert_eq!(record(&book).variants[0].stop, fill);
        for (index, (mfe, locked)) in [(1.59, 0.0), (1.6, 0.8), (2.08, 1.28), (2.09, 1.29)]
            .into_iter()
            .enumerate()
        {
            let at = now + 2 + index as i64;
            book.advance(&quote(at, fill + sign * mfe * risk), at);
            let lane = &record(&book).variants[0];
            assert!((lane.stop - (fill + sign * locked * risk)).abs() < 1e-8);
            assert_eq!(lane.remaining, 1);
        }
        let stop = record(&book).variants[0].stop;
        let mut restored: ComparisonBook =
            serde_json::from_slice(&serde_json::to_vec(&book).unwrap()).unwrap();
        restored.advance(&quote(now + 10, fill + sign * 2.0 * risk), now + 10);
        assert!((record(&restored).variants[0].stop - stop).abs() < 1e-8);
        restored.advance(&quote(now + 11, stop - sign * 0.01), now + 11);
        let lane = &record(&restored).variants[0];
        assert_eq!(lane.status, "closed");
        assert_eq!(lane.reason, "trailing");
        assert_eq!(lane.fills.len(), 2);
    }
}

#[test]
fn continuous_policy_version_starts_new_batches_without_recalculating_old_observations() {
    let now = ts("2026-10-20 10:00:00");
    let mut book = ComparisonBook::default();
    book.register(&signal("old", now, 10.0), &ExecutionConfig::default(), now);
    book.advance(&quote(now, 100.0), now);
    book.runs[0].definition.version = "paired-quote-v1".into();
    book.runs[0].definition.config_hash = "old-policy".into();
    book.register(
        &signal("new", now + 1, 10.0),
        &ExecutionConfig::default(),
        now + 1,
    );
    book.advance(&quote(now + 1, 100.0), now + 1);
    assert_eq!(book.runs.len(), 2);
    assert_eq!(book.runs[1].definition.version, COMPARISON_VERSION);
    for lane in &book.runs[0].signals["old"].variants {
        assert_eq!(lane.status, "missing_data");
        assert!(lane.fills.is_empty());
    }
    assert_eq!(book.runs[1].signals["new"].variants[0].status, "open");
}

#[test]
fn archived_observations_keep_totals_after_restart_and_crash_replay_uses_stable_keys() {
    let now = ts("2026-10-20 10:00:00");
    let mut b = ComparisonBook::default();
    b.register(&signal("one", now, 10.0), &ExecutionConfig::default(), now);
    b.advance(&quote(now, 100.0), now);
    b.advance(&quote(now + 1, 112.0), now + 1);
    let before = b.summary();
    let before_commit = serde_json::to_vec(&b).unwrap();
    let directory =
        std::env::temp_dir().join(format!("ntrend-comparison-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir(&directory).unwrap();
    let path = directory.join("comparison.jsonl");
    b.archive_completed(&path).unwrap();
    assert!(b.runs[0].signals.is_empty());
    assert_eq!(b.summary(), before);
    let mut restored: ComparisonBook =
        serde_json::from_slice(&serde_json::to_vec(&b).unwrap()).unwrap();
    restored.archive_completed(&path).unwrap();
    assert_eq!(std::fs::read_to_string(&path).unwrap().lines().count(), 1);
    // Log was durable but hot-state replacement was lost in the simulated crash.
    let mut old: ComparisonBook = serde_json::from_slice(&before_commit).unwrap();
    old.archive_completed(&path).unwrap();
    assert_eq!(old.summary(), before);
    let rows: Vec<serde_json::Value> = std::fs::read_to_string(&path)
        .unwrap()
        .lines()
        .map(|l| serde_json::from_str(l).unwrap())
        .collect();
    assert_eq!(rows.len(), 2);
    assert_eq!(rows[0]["archive_id"], rows[1]["archive_id"]);
    assert_eq!(rows[0]["signal"]["variants"].as_array().unwrap().len(), 4);
    use std::io::Write;
    std::fs::OpenOptions::new()
        .append(true)
        .open(&path)
        .unwrap()
        .write_all(b"{\"torn\":")
        .unwrap();
    let mut retry: ComparisonBook = serde_json::from_slice(&before_commit).unwrap();
    retry.archive_completed(&path).unwrap();
    let rows: Vec<serde_json::Value> = std::fs::read_to_string(&path)
        .unwrap()
        .lines()
        .map(|l| serde_json::from_str(l).unwrap())
        .collect();
    assert_eq!(rows.len(), 3);
    assert!(rows
        .iter()
        .all(|row| row["archive_id"] == rows[0]["archive_id"]));
    std::fs::remove_dir_all(directory).unwrap();
}

#[test]
fn failed_archive_keeps_full_observations_for_retry() {
    let now = ts("2026-10-20 10:00:00");
    let mut b = ComparisonBook::default();
    b.register(&signal("one", now, 10.0), &ExecutionConfig::default(), now);
    b.advance(&quote(now, 100.0), now);
    b.advance(&quote(now + 1, 112.0), now + 1);
    let directory =
        std::env::temp_dir().join(format!("ntrend-comparison-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir(&directory).unwrap();
    assert!(b.archive_completed(&directory).is_err());
    assert_eq!(b.runs[0].signals.len(), 1);
    std::fs::remove_dir(directory).unwrap();
}

#[test]
fn known_outcomes_at_first_observation_are_not_forward_test_results() {
    let now = ts("2026-10-20 10:00:00");
    let mut b = ComparisonBook::default();
    b.register(&signal("one", now, 10.0), &ExecutionConfig::default(), now);
    b.attach_evidence("one", serde_json::json!({"state":"closed","outcome":"win"}));
    b.advance(&quote(now, 100.0), now);
    b.advance(&quote(now + 1, 112.0), now + 1);
    assert_eq!(b.summary()["runs"][0]["groups"][0]["closed"], 0);
    assert_eq!(b.summary()["runs"][0]["groups"][0]["invalid"], 1);
}

#[test]
fn partial_exits_count_once_and_short_trades_use_opposite_book_prices() {
    let now = ts("2026-10-20 10:00:00");
    let mut b = ComparisonBook::default();
    let mut s = signal("one", now, 10.0);
    s.tier = PositionTier::Large;
    s.volume = 2;
    b.register(&s, &ExecutionConfig::default(), now);
    b.advance(&quote(now, 100.0), now);
    b.advance(&quote(now + 1, 112.0), now + 1);
    assert_eq!(b.summary()["runs"][0]["groups"][0]["closed"], 0);
    b.advance(&quote(now + 2, 100.5), now + 2);
    assert_eq!(record(&b).variants[0].fills.len(), 2);
    assert_eq!(b.summary()["runs"][0]["groups"][0]["closed"], 1);
    let mut b = ComparisonBook::default();
    let mut s = signal("short", now, 10.0);
    s.direction = "down".into();
    s.stop_price = 110.0;
    s.target_price = 60.0;
    b.register(&s, &ExecutionConfig::default(), now);
    b.advance(&quote(now, 100.0), now);
    assert_eq!(record(&b).variants[0].fill_price, Some(99.5));
    b.advance(&quote(now + 1, 88.0), now + 1);
    assert_eq!(record(&b).variants[0].fills[0].price, 88.5);
    assert!(
        b.summary()["runs"][0]["groups"][0]["gross_amount"]
            .as_f64()
            .unwrap()
            > 0.0
    );
}

#[test]
fn flat_before_close_is_independent_and_pairing_waits_for_the_other_exit() {
    let now = ts("2026-10-20 14:55:00");
    let mut b = ComparisonBook::default();
    b.register(&signal("one", now, 10.0), &ExecutionConfig::default(), now);
    b.advance(&quote(now, 100.0), now);
    let close = ts("2026-10-20 14:58:00");
    b.advance(&quote(close - 1, 102.0), close);
    assert_eq!(record(&b).variants[2].status, "open");
    assert!(record(&b).variants[2].fills.is_empty());
    b.advance(&quote(close, 102.0), close);
    assert_eq!(record(&b).variants[2].status, "closed");
    assert_eq!(record(&b).variants[2].reason, "pre_close");
    assert_eq!(record(&b).variants[0].status, "open");
    assert_eq!(b.summary()["runs"][0]["pairs"][1]["matched_signals"], 0);
    b.advance(&quote(close + 1, 112.0), close + 1);
    assert_eq!(b.summary()["runs"][0]["pairs"][1]["matched_signals"], 1);
}

#[test]
fn missing_close_quote_never_becomes_a_fake_next_session_fill() {
    let now = ts("2026-10-20 14:55:00");
    let mut b = ComparisonBook::default();
    b.register(&signal("one", now, 10.0), &ExecutionConfig::default(), now);
    b.advance(&quote(now, 100.0), now);
    b.advance(&HashMap::new(), ts("2026-10-20 15:01:00"));
    assert_eq!(record(&b).variants[2].status, "missing_data");
    assert!(record(&b).variants[2].fills.is_empty());
    let next = ts("2026-10-20 21:00:00");
    b.advance(&quote(next, 112.0), next);
    assert_eq!(b.summary()["runs"][0]["groups"][2]["closed"], 0);
    assert_eq!(b.summary()["runs"][0]["groups"][2]["invalid"], 1);
}

#[test]
fn stale_wrong_contract_or_pre_observation_quotes_cannot_seed_a_trade() {
    let now = ts("2026-10-20 10:00:00");
    let mut b = ComparisonBook::default();
    b.register(&signal("one", now, 10.0), &ExecutionConfig::default(), now);
    b.advance(&quote(now - 1, 100.0), now);
    b.advance(&quote(now, 100.0), now + 20);
    let mut wrong = quote(now, 100.0);
    wrong.get_mut("PB2611").unwrap().instrument_id = "pb2612".into();
    b.advance(&wrong, now);
    assert!(record(&b).variants.iter().all(|v| v.status == "pending"));
    assert!(record(&b).first_quote_at.is_none());
}

#[test]
fn bar_replay_does_not_accelerate_no_follow_and_pending_signals_expire() {
    let now = ts("2026-10-20 10:00:00");
    let mut b = ComparisonBook::default();
    b.register(&signal("one", now, 10.0), &ExecutionConfig::default(), now);
    b.advance(&quote(now, 100.0), now);
    for i in 1..=5 {
        let at = now + i * 900;
        let bar = Local
            .timestamp_opt(at, 0)
            .unwrap()
            .format("%Y-%m-%d %H:%M:%S")
            .to_string();
        b.on_bar("PB0", &bar);
        b.on_bar("PB0", &bar);
    }
    let at = now + 4500;
    b.advance(&quote(at, 100.0), at);
    assert_eq!(record(&b).variants[0].bars_held, 5);
    assert_eq!(record(&b).variants[0].reason, "no_follow");
    let mut b = ComparisonBook::default();
    b.register(&signal("two", now, 10.0), &ExecutionConfig::default(), now);
    for i in 1..=13 {
        b.on_bar(
            "PB0",
            &Local
                .timestamp_opt(now + i * 900, 0)
                .unwrap()
                .format("%Y-%m-%d %H:%M:%S")
                .to_string(),
        );
    }
    assert!(record(&b).variants.iter().all(|v| v.status == "expired"));
}

#[test]
fn quote_gaps_are_retained_but_excluded_from_ranked_results() {
    let now = ts("2026-10-20 10:00:00");
    let mut b = ComparisonBook::default();
    b.register(&signal("one", now, 10.0), &ExecutionConfig::default(), now);
    b.advance(&quote(now, 100.0), now);
    b.mark_quote_gap();
    b.advance(&quote(now + 1, 112.0), now + 1);
    assert_eq!(record(&b).variants[0].status, "closed");
    assert_eq!(b.summary()["runs"][0]["groups"][0]["closed"], 0);
    assert_eq!(b.summary()["runs"][0]["groups"][0]["quote_gap_signals"], 1);
    assert_eq!(b.summary()["runs"][0]["pairs"][0]["matched_signals"], 0);
}

#[test]
fn shadow_contracts_remain_subscribed_after_a_real_filter_would_skip_the_signal() {
    let now = ts("2026-10-20 10:00:00");
    let mut b = ComparisonBook::default();
    let mut cfg = ExecutionConfig::default();
    cfg.min_entry_risk_ticks = 10;
    b.register(&signal("one", now, 5.0), &cfg, now);
    b.advance(&quote(now, 100.0), now);
    assert!(b.contracts().contains("pb2611"));
    assert_eq!(record(&b).variants[1].status, "filtered");
    assert_eq!(record(&b).variants[0].status, "open");
}
