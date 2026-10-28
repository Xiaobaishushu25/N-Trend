//! Run against isolated fork only: cargo test --test swing_repair.
use n_core::{
    derive::{aggregate, Timeframe},
    fetch::kline::Kline,
    trader::ExecutionConfig,
    v2::replay::ReplayEvent,
    v3::ablation::{evaluate_research, Experiment, Policy, ResearchRules},
};
fn b(t: &str, p: f64) -> Kline {
    Kline {
        datetime: t.into(),
        open: p,
        high: p,
        low: p,
        close: p,
        volume: 1.,
        hold: 1.,
    }
}
fn e(short: bool) -> ReplayEvent {
    let s = if short { -1. } else { 1. };
    let mut ev = ReplayEvent {
        symbol: "RB0".into(),
        direction: if short { "down" } else { "up" }.into(),
        warning_ts: "2026-01-05 09:45:00".into(),
        entry: 100.,
        stop: 100. - s * 10.,
        target: 100. + s * 30.,
        risk: 10.,
        tick_size: 1.,
        ..Default::default()
    };
    ev.setup_features.setup_quality = 4.;
    ev
}
fn eval(ev: &ReplayEvent, bars: &[Kline], rules: ResearchRules) -> n_core::v3::ablation::ResultRow {
    evaluate_research(
        ev,
        bars,
        &[],
        &ExecutionConfig::default(),
        Experiment {
            policy: Policy::OriginalS1,
            no_follow_bar: None,
            slip_ticks: 0.,
            fee_ticks: 0.,
        },
        10.,
        None,
        rules,
    )
}
#[test]
fn midnight_keeps_previous_three_bars_without_duplicate_endpoint() {
    let bs = vec![
        b("2026-01-05 23:50:00", 1.),
        b("2026-01-05 23:55:00", 2.),
        b("2026-01-06 00:00:00", 3.),
        b("2026-01-06 00:05:00", 4.),
        b("2026-01-06 00:10:00", 5.),
        b("2026-01-06 00:15:00", 6.),
    ];
    let out = aggregate(&bs, Timeframe::M15);
    assert_eq!(out.len(), 2);
    assert_eq!(out[0].datetime, "2026-01-06 00:00:00");
    assert_eq!(out[0].volume, 3.);
    assert_eq!(out[1].volume, 3.);
    assert_eq!(out[0].close, 3.);
    assert_eq!(out[1].open, 4.);
    let h = aggregate(&bs, Timeframe::M60);
    assert_eq!(h.len(), 2);
    assert_eq!(h[0].datetime, "2026-01-06 00:00:00");
    assert_eq!(h[0].volume, 3.);
}
#[test]
fn next_minute_is_not_same_minute_and_keeps_trigger_slot() {
    let r = eval(
        &e(false),
        &[
            b("2026-01-05 09:46:00", 100.),
            b("2026-01-05 09:47:00", 101.),
            b("2026-01-05 09:48:00", 130.),
        ],
        ResearchRules {
            next_minute_open: true,
            ..Default::default()
        },
    );
    assert_eq!(
        r.evaluation.trigger_ts.as_deref(),
        Some("2026-01-05 09:46:00")
    );
    assert_eq!(
        r.evaluation.entry_ts.as_deref(),
        Some("2026-01-05 09:47:00")
    );
    assert_eq!(r.evaluation.fill, Some(101.));
    assert_eq!(r.evaluation.status, "closed");
}
#[test]
fn protection_is_symmetric_and_gap_is_not_filled_at_stop() {
    for short in [false, true] {
        let s = if short { -1. } else { 1. };
        let r = eval(
            &e(short),
            &[
                b("2026-01-05 09:46:00", 100.),
                b("2026-01-05 09:47:00", 100. + 10. * s),
                b("2026-01-05 09:48:00", 100. + s),
            ],
            ResearchRules {
                protect_after_r: Some(1.),
                protect_lock_r: Some(0.25),
                ..Default::default()
            },
        );
        assert_eq!(r.evaluation.status, "closed");
        assert_eq!(r.evaluation.exit_reason.as_deref(), Some("protection"));
        assert_eq!(r.evaluation.final_price, Some(100. + s));
        assert_eq!(r.evaluation.gross_points, 1.);
    }
}
#[test]
fn missing_single_minute_and_first_minute_are_unknown() {
    for bs in [
        vec![
            b("2026-01-05 09:46:00", 100.),
            b("2026-01-05 09:48:00", 101.),
        ],
        vec![b("2026-01-05 09:47:00", 100.)],
    ] {
        let r = eval(&e(false), &bs, ResearchRules::default());
        assert_eq!(r.evaluation.status, "missing_data");
        assert!(r.evaluation.net_amount.is_none());
    }
}
#[test]
fn lunch_break_is_allowed_but_missing_reopening_minute_is_unknown() {
    let mut ev = e(false);
    ev.warning_ts = "2026-01-05 11:29:00".into();
    let good = eval(
        &ev,
        &[
            b("2026-01-05 11:30:00", 100.),
            b("2026-01-05 13:31:00", 130.),
        ],
        ResearchRules::default(),
    );
    assert_eq!(good.evaluation.status, "closed");
    let bad = eval(
        &ev,
        &[
            b("2026-01-05 11:30:00", 100.),
            b("2026-01-05 13:32:00", 130.),
        ],
        ResearchRules::default(),
    );
    assert_eq!(bad.evaluation.status, "missing_data");
}
#[test]
fn service_dedup_is_prefix_causal_and_does_not_consult_final_labels() {
    use n_core::analyze::model::{Bar, DT};
    let mk = |t: &str| Bar {
        dt: DT::from_bar_ts(t).unwrap(),
        open: 99.,
        high: 99.,
        low: 95.,
        close: 98.,
        volume: 1.,
        hold: 1.,
        rollover: false,
    };
    let bs = vec![
        mk("2026-01-05 09:45:00"),
        mk("2026-01-05 10:00:00"),
        mk("2026-01-05 10:15:00"),
    ];
    let mut first = e(false);
    first.s2_price = 91.;
    let mut second = first.clone();
    second.warning_ts = "2026-01-05 10:00:00".into();
    let before =
        n_core::service::offline_forward_warning_keys(&bs[..2], &[first.clone(), second.clone()])
            .unwrap();
    let full = n_core::service::offline_forward_warning_keys(&bs, &[first.clone(), second.clone()])
        .unwrap();
    assert_eq!(before, full);
    assert_eq!(full.len(), 1);
    first.entry_ts = Some("2099-01-01 00:00:00".into());
    first.entry_price = Some(1e9);
    let relabeled = n_core::service::offline_forward_warning_keys(&bs, &[first, second]).unwrap();
    assert_eq!(full, relabeled);
}
