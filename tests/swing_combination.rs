use chrono::{Duration, NaiveDateTime};
use n_core::{fetch::kline::Kline, v3::ablation::swing_adaptive::AdaptiveExit};
fn minute(t: &str, h: f64, l: f64, c: f64) -> Kline {
    Kline {
        datetime: t.into(),
        open: c,
        high: h,
        low: l,
        close: c,
        volume: 1.,
        hold: 1.,
    }
}
fn candle(
    state: &mut AdaptiveExit,
    start: &str,
    h: f64,
    l: f64,
    c: f64,
    peak: f64,
    sign: f64,
    a: bool,
    b: bool,
) -> n_core::v3::ablation::swing_adaptive::Action {
    let start = NaiveDateTime::parse_from_str(start, "%Y-%m-%d %H:%M:%S").unwrap();
    let mut action = Default::default();
    for i in 1..=15 {
        let t = (start + Duration::minutes(i))
            .format("%Y-%m-%d %H:%M:%S")
            .to_string();
        action = state.observe(
            &minute(&t, h, l, c),
            "2026-01-05 09:45:00",
            peak,
            100.,
            10.,
            sign,
            1.,
            if sign > 0. { 99. } else { 101. },
            100. - sign * 10.,
            100. - sign * 10.,
            a,
            b,
        );
    }
    action
}
#[test]
fn startup_needs_two_observed_closes_and_is_symmetric() {
    for sign in [1., -1.] {
        let mut s = AdaptiveExit::default();
        let cv = |p: f64| 100. + sign * (p - 100.);
        let mut vals = [cv(99.), cv(98.), cv(98.5)];
        if sign < 0. {
            vals.swap(0, 1);
        }
        assert!(candle(
            &mut s,
            "2026-01-05 09:45:00",
            vals[0],
            vals[1],
            vals[2],
            0.1,
            sign,
            true,
            false
        )
        .close
        .is_none());
        let mut vals = [cv(99.), cv(97.), cv(98.)];
        if sign < 0. {
            vals.swap(0, 1);
        }
        assert_eq!(
            candle(
                &mut s,
                "2026-01-05 10:00:00",
                vals[0],
                vals[1],
                vals[2],
                0.1,
                sign,
                true,
                false
            )
            .close,
            Some("adaptive_failed_start")
        );
    }
}
#[test]
fn stall_is_continuous_after_prior_profit_and_resets_on_new_progress() {
    let mut s = AdaptiveExit::default();
    assert!(candle(
        &mut s,
        "2026-01-05 09:45:00",
        107.,
        98.,
        99.,
        0.7,
        1.,
        true,
        false
    )
    .close
    .is_none());
    for t in [
        "2026-01-05 10:00:00",
        "2026-01-05 10:15:00",
        "2026-01-05 10:30:00",
        "2026-01-05 10:45:00",
    ] {
        assert!(candle(&mut s, t, 104., 98., 99., 0.7, 1., true, false)
            .close
            .is_none());
    }
    assert_eq!(
        candle(
            &mut s,
            "2026-01-05 11:00:00",
            104.,
            98.,
            99.,
            0.7,
            1.,
            true,
            false
        )
        .close,
        Some("adaptive_stall_return")
    );
    assert!(candle(
        &mut s,
        "2026-01-05 11:15:00",
        108.,
        98.,
        99.,
        0.8,
        1.,
        true,
        false
    )
    .close
    .is_none());
}
#[test]
fn structure_waits_for_right_bar_after_one_r_and_is_symmetric() {
    for sign in [1., -1.] {
        let mut s = AdaptiveExit::default();
        let cv = |p: f64| 100. + sign * (p - 100.);
        let feed = |s: &mut AdaptiveExit, t: &str, h: f64, l: f64, c: f64| {
            let mut z = [cv(h), cv(l), cv(c)];
            if sign < 0. {
                z.swap(0, 1);
            }
            candle(s, t, z[0], z[1], z[2], 1.1, sign, false, true)
        };
        assert!(feed(&mut s, "2026-01-05 09:45:00", 111., 106., 108.)
            .stop
            .is_none());
        assert!(feed(&mut s, "2026-01-05 10:00:00", 110., 104., 108.)
            .stop
            .is_none());
        assert_eq!(
            feed(&mut s, "2026-01-05 10:15:00", 112., 107., 110.).stop,
            Some(cv(103.))
        );
    }
}
#[test]
fn structure_does_not_arm_before_one_r() {
    let mut s = AdaptiveExit::default();
    candle(
        &mut s,
        "2026-01-05 09:45:00",
        108.,
        106.,
        107.,
        0.8,
        1.,
        false,
        true,
    );
    candle(
        &mut s,
        "2026-01-05 10:00:00",
        108.,
        104.,
        107.,
        0.8,
        1.,
        false,
        true,
    );
    assert!(candle(
        &mut s,
        "2026-01-05 10:15:00",
        108.,
        107.,
        108.,
        0.8,
        1.,
        false,
        true
    )
    .stop
    .is_none());
}
#[test]
fn pivot_before_arm_is_not_retroactively_protected() {
    let mut s = AdaptiveExit::default();
    candle(
        &mut s,
        "2026-01-05 09:45:00",
        108.,
        106.,
        107.,
        0.8,
        1.,
        false,
        true,
    );
    candle(
        &mut s,
        "2026-01-05 10:00:00",
        108.,
        104.,
        107.,
        0.8,
        1.,
        false,
        true,
    );
    assert!(candle(
        &mut s,
        "2026-01-05 10:15:00",
        112.,
        107.,
        110.,
        1.2,
        1.,
        false,
        true
    )
    .stop
    .is_none());
}
#[test]
fn partial_entry_bucket_never_supplies_a_pivot() {
    let mut s = AdaptiveExit::default();
    for i in 47..=59 {
        let t = format!("2026-01-05 09:{i}:00");
        assert!(s
            .observe(
                &minute(&t, 112., 105., 110.),
                "2026-01-05 09:46:00",
                1.2,
                100.,
                10.,
                1.,
                1.,
                99.,
                90.,
                90.,
                false,
                true
            )
            .stop
            .is_none());
    }
    assert!(s
        .observe(
            &minute("2026-01-05 10:00:00", 112., 105., 110.),
            "2026-01-05 09:46:00",
            1.2,
            100.,
            10.,
            1.,
            1.,
            99.,
            90.,
            90.,
            false,
            true
        )
        .stop
        .is_none());
}

fn event(short: bool) -> n_core::v2::replay::ReplayEvent {
    let sign = if short { -1. } else { 1. };
    let mut e = n_core::v2::replay::ReplayEvent {
        symbol: "RB0".into(),
        direction: if short { "down" } else { "up" }.into(),
        warning_ts: "2026-01-05 09:30:00".into(),
        entry: 100.,
        stop: 100. - sign * 10.,
        target: 100. + sign * 30.,
        risk: 10.,
        tick_size: 1.,
        warning_extreme: 100. - sign,
        s2_price: 100. - sign * 9.,
        ..Default::default()
    };
    e.setup_features.setup_quality = 4.;
    e
}
fn evaluate(
    e: &n_core::v2::replay::ReplayEvent,
    bars: &[Kline],
    a: bool,
    b: bool,
) -> n_core::v3::ablation::ResultRow {
    use n_core::{
        trader::ExecutionConfig,
        v3::ablation::{evaluate_research, Experiment, Policy, ResearchRules},
    };
    evaluate_research(
        e,
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
        ResearchRules {
            adaptive_ongoing: a,
            adaptive_structure: b,
            ..Default::default()
        },
    )
}
#[test]
fn close_decision_fills_next_minute_and_unavailable_fill_remains_unknown() {
    let start = NaiveDateTime::parse_from_str("2026-01-05 09:30:00", "%Y-%m-%d %H:%M:%S").unwrap();
    let mut bars = vec![];
    for i in 1..=46 {
        let t = (start + Duration::minutes(i))
            .format("%Y-%m-%d %H:%M:%S")
            .to_string();
        let p = if i == 1 {
            100.
        } else if i <= 30 {
            98.
        } else if i <= 45 {
            97.
        } else {
            96.
        };
        bars.push(minute(&t, p, p, p));
    }
    let e = event(false);
    let r = evaluate(&e, &bars, true, false);
    assert_eq!(r.evaluation.exit_ts.as_deref(), Some("2026-01-05 10:16:00"));
    assert_eq!(r.evaluation.final_price, Some(96.));
    assert_eq!(
        r.evaluation.exit_reason.as_deref(),
        Some("adaptive_failed_start")
    );
    let missing = evaluate(&e, &bars[..45], true, false);
    assert_eq!(missing.evaluation.status, "unfinished");
    assert!(missing.evaluation.net_amount.is_none());
}
#[test]
fn confirmed_stop_is_not_applied_inside_confirmation_bar_and_honors_next_gap() {
    for short in [false, true] {
        let sign = if short { -1. } else { 1. };
        let cv = |p: f64| 100. + sign * (p - 100.);
        let start =
            NaiveDateTime::parse_from_str("2026-01-05 09:30:00", "%Y-%m-%d %H:%M:%S").unwrap();
        let mut bars = vec![];
        for i in 1..=61 {
            let t = (start + Duration::minutes(i))
                .format("%Y-%m-%d %H:%M:%S")
                .to_string();
            let (h, l, c) = if i == 1 {
                (100., 100., 100.)
            } else if i <= 15 {
                (101., 101., 101.)
            } else if i <= 30 {
                (111., 106., 108.)
            } else if i <= 45 {
                (110., 104., 108.)
            } else if i <= 60 {
                (112., 107., 110.)
            } else {
                (102., 102., 102.)
            };
            let mut z = [cv(h), cv(l), cv(c)];
            if short {
                z.swap(0, 1);
            }
            bars.push(minute(&t, z[0], z[1], z[2]));
        }
        let r = evaluate(&event(short), &bars, false, true);
        assert_eq!(r.evaluation.exit_ts.as_deref(), Some("2026-01-05 10:31:00"));
        assert_eq!(r.evaluation.final_price, Some(cv(102.)));
        assert_eq!(
            r.evaluation.exit_reason.as_deref(),
            Some("structure_protection")
        );
    }
}
