use n_core::{fetch::kline::Kline,v2::replay::ReplayEvent,v3::ablation::{evaluate_research,EntryMode,Experiment,Policy,ResearchRules}};
fn event(short:bool)->ReplayEvent {
    let s=if short {-1.}else{1.};
    let mut e=ReplayEvent{symbol:"RB0".into(),direction:if short{"down"}else{"up"}.into(),warning_ts:"2026-01-05 09:45:00".into(),entry:100.,stop:100.-s*26.,target:100.+s*50.,risk:26.,tick_size:2.,..Default::default()};
    e.setup_features.setup_quality=4.;e
}
fn bar(m:usize,o:f64,h:f64,l:f64,c:f64,short:bool)->Kline {
    let cv=|p|if short{200.-p}else{p};
    Kline{datetime:format!("2026-01-05 09:{m:02}:00"),open:cv(o),high:cv(if short{l}else{h}),low:cv(if short{h}else{l}),close:cv(c),volume:1.,hold:1.}
}
fn eval(b:&[Kline],rules:ResearchRules,short:bool)->n_core::v3::ablation::ResultRow {
    evaluate_research(&event(short),b,&[],&Default::default(),Experiment{policy:Policy::OriginalS1,no_follow_bar:None,slip_ticks:1.,fee_ticks:0.},10.,None,rules)
}
#[test]
fn body_veto_only_before_fill() {
    let rules=ResearchRules{pending_body_invalidation:Some(88.),..Default::default()};
    let r=eval(&[bar(46,90.,92.,86.,90.,false)],rules,false);
    assert_eq!(r.evaluation.status,"invalidated_before_entry_body");assert!(r.evaluation.fill.is_none());
    let r=eval(&[bar(46,100.,104.,100.,102.,false),bar(47,90.,92.,86.,90.,false)],rules,false);
    assert!(r.evaluation.fill.is_some());assert_ne!(r.evaluation.status,"invalidated_before_entry_body");
    assert_eq!(r.evaluation.risk,28.);
}
#[test]
fn close_confirmation_waits_for_next_open_both_directions() {
    for short in [false,true] {
        let b=[bar(46,98.,104.,96.,102.,short),bar(47,102.,104.,102.,102.,short)];
        let r=eval(&b,ResearchRules{entry_mode:EntryMode::CloseConfirmNextOpen,..Default::default()},short);
        assert_eq!(r.evaluation.entry_ts.as_deref(),Some("2026-01-05 09:47:00"));
        assert_eq!(r.evaluation.fill,Some(if short{96.}else{104.}));
    }
}
#[test]
fn reclaim_requires_later_retest_and_next_open_both_directions() {
    for short in [false,true] {
        let b=[bar(46,98.,104.,92.,102.,short),bar(47,96.,98.,94.,96.,short),bar(48,96.,98.,96.,98.,short)];
        let r=eval(&b,ResearchRules{entry_mode:EntryMode::BreakoutRetestReclaim,..Default::default()},short);
        assert_eq!(r.evaluation.entry_ts.as_deref(),Some("2026-01-05 09:48:00"));
        assert_eq!(r.evaluation.fill,Some(if short{102.}else{98.}));
    }
}
#[test]
fn reclaim_does_not_fill_from_breakout_bar_or_unreclaimed_close() {
    let rules=ResearchRules{entry_mode:EntryMode::BreakoutRetestReclaim,..Default::default()};
    let r=eval(&[bar(46,98.,104.,92.,102.,false)],rules,false);assert!(r.evaluation.fill.is_none());
    let r=eval(&[bar(46,98.,104.,96.,102.,false),bar(47,96.,98.,90.,92.,false),bar(48,92.,94.,90.,92.,false)],rules,false);
    assert!(r.evaluation.fill.is_none());
}
#[test]
fn prefill_target_or_stop_cancels_reclaim() {
    let rules=ResearchRules{entry_mode:EntryMode::BreakoutRetestReclaim,..Default::default()};
    for b in [bar(46,98.,152.,96.,102.,false),bar(46,98.,104.,72.,102.,false)] {
        let r=eval(&[b],rules,false);assert!(r.evaluation.fill.is_none());
        assert!(["missed_target","invalidated"].contains(&r.evaluation.status.as_str()));
    }
}
#[test]
fn reclaim_stress_adds_one_minute_after_trigger() {
    let b=[bar(46,98.,104.,96.,102.,false),bar(47,96.,98.,94.,96.,false),bar(48,96.,98.,96.,98.,false),bar(49,98.,100.,98.,100.,false)];
    let r=eval(&b,ResearchRules{entry_mode:EntryMode::BreakoutRetestReclaim,next_minute_open:true,..Default::default()},false);
    assert_eq!(r.evaluation.trigger_ts.as_deref(),Some("2026-01-05 09:48:00"));
    assert_eq!(r.evaluation.entry_ts.as_deref(),Some("2026-01-05 09:49:00"));
}
