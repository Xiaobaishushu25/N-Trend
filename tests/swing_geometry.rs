use n_core::{
    fetch::kline::Kline,
    v2::replay::ReplayEvent,
    v3::{
        ablation::{evaluate_research, EntryMode, Experiment, Policy, ResearchRules},
        geometry_research as g,
    },
};
fn event(short: bool) -> ReplayEvent {
    let s = if short { -1. } else { 1. };
    let mut e = ReplayEvent {
        symbol: "RB0".into(),
        direction: if short { "down" } else { "up" }.into(),
        warning_ts: "2026-01-05 09:45:00".into(),
        entry: 100.,
        stop: 100. - s * 26.,
        target: 100. + s * 50.,
        risk: 26.,
        tick_size: 2.,
        ..Default::default()
    };
    e.setup_features.setup_quality = 4.;
    e
}
fn b(t: &str, o: f64, h: f64, l: f64, c: f64) -> Kline {
    Kline {
        datetime: t.into(),
        open: o,
        high: h,
        low: l,
        close: c,
        volume: 1.,
        hold: 1.,
    }
}
#[test]
fn retest_grid_is_toward_breakout_and_symmetric() {
    for short in [false, true] {
        let e = event(short);
        let s = if short { -1. } else { 1. };
        let price = g::retest_price(&e, s);
        assert_eq!(price, if short { 106. } else { 94. });
        assert_eq!(price / 2., (price / 2.).round());
    }
}
#[test]
fn body_stop_tightens_without_changing_anchor_or_future_label() {
    let mut e = event(false);
    let anchor = e.s0_ts.clone();
    let w = b("2026-01-05 09:45:00", 90., 98., 80., 96.);
    g::body_stop(&mut e, &w);
    assert_eq!(e.stop, 88.);
    assert_eq!(e.risk, 12.);
    assert_eq!(e.s0_ts, anchor);
    assert!(e.evaluation.is_none());
    let old = e.stop;
    g::body_stop(&mut e, &b("2026-01-05 09:45:00", 80., 99., 74., 90.));
    assert_eq!(e.stop, old);
}
#[test]
fn local_pivot_requires_right_confirmation_and_ignores_future() {
    let mut e = event(false);
    e.tick_size = 1.;
    e.entry = 110.;
    e.stop = 90.;
    e.risk = 20.;
    e.target = 150.;
    let prior = vec![
        b("2026-01-05 09:00:00", 110., 120., 100., 110.),
        b("2026-01-05 09:15:00", 110., 125., 100., 110.),
        b("2026-01-05 09:30:00", 110., 121., 100., 110.),
        b("2026-01-05 10:00:00", 110., 119., 100., 110.),
    ];
    assert_eq!(
        g::local_target(&mut e, &prior).as_deref(),
        Some("2026-01-05 09:15:00")
    );
    assert_eq!(e.target, 125.);
    let mut unconfirmed = e.clone();
    unconfirmed.target = 150.;
    assert!(g::local_target(&mut unconfirmed, &prior[..2]).is_none());
    assert_eq!(unconfirmed.target, 150.);
}
#[test]
fn local_target_does_not_join_pivots_across_session_gap() {
    let mut e = event(false);
    e.warning_ts = "2026-01-05 14:15:00".into();
    let prior = vec![
        b("2026-01-05 11:30:00", 110., 120., 100., 110.),
        b("2026-01-05 13:45:00", 110., 125., 100., 110.),
        b("2026-01-05 14:00:00", 110., 121., 100., 110.),
    ];
    let target = e.target;
    assert!(g::local_target(&mut e, &prior).is_none());
    assert_eq!(e.target, target);
}
#[test]
fn retest_is_not_filled_in_breakout_minute_and_preserves_grid() {
    for short in [false, true] {
        let e = event(short);
        let s = if short { -1. } else { 1. };
        let cv = |p: f64| 100. + s * (p - 100.);
        let make = |t: &str, o: f64, h: f64, l: f64, c: f64| {
            if !short {
                b(t, o, h, l, c)
            } else {
                b(t, cv(o), cv(l), cv(h), cv(c))
            }
        };
        let bars = vec![
            make("2026-01-05 09:46:00", 100., 102., 100., 102.),
            make("2026-01-05 09:47:00", 94., 94., 94., 94.),
        ];
        let r = evaluate_research(
            &e,
            &bars,
            &[],
            &Default::default(),
            Experiment {
                policy: Policy::OriginalS1,
                no_follow_bar: None,
                slip_ticks: 1.,
                fee_ticks: 0.,
            },
            10.,
            None,
            ResearchRules {
                entry_mode: EntryMode::BreakoutRetestQuarter,
                min_actual_risk_ticks: Some(10.),
                ..Default::default()
            },
        );
        assert_eq!(
            r.evaluation.entry_ts.as_deref(),
            Some("2026-01-05 09:47:00")
        );
        assert_eq!(r.evaluation.fill, Some(cv(96.)));
        assert_eq!(r.evaluation.risk, 22.);
    }
}

#[test]
fn body_stop_has_symmetric_short_invalidation() {
    let mut e = event(true);
    g::body_stop(&mut e, &b("2026-01-05 09:45:00", 110., 120., 102., 104.));
    assert_eq!(e.stop, 112.);
    assert_eq!(e.risk, 12.);
}

#[test]
fn local_support_has_symmetric_short_confirmation() {
    let mut e = event(true);
    e.tick_size = 1.;
    e.entry = 90.;
    e.stop = 110.;
    e.risk = 20.;
    e.target = 50.;
    let prior = vec![
        b("2026-01-05 09:00:00", 90., 100., 80., 90.),
        b("2026-01-05 09:15:00", 90., 100., 75., 90.),
        b("2026-01-05 09:30:00", 90., 100., 79., 90.),
    ];
    assert_eq!(
        g::local_target(&mut e, &prior).as_deref(),
        Some("2026-01-05 09:15:00")
    );
    assert_eq!(e.target, 75.);
}
