use n_core::{fetch::kline::Kline,v2::replay::ReplayEvent,v3::ablation::{evaluate_research,Experiment,Policy,ResearchRules,warning_failure::BodyFailure}};
use chrono::{Duration,NaiveDateTime};
fn b(t:&str,o:f64,h:f64,l:f64,c:f64)->Kline{Kline{datetime:t.into(),open:o,high:h,low:l,close:c,volume:1.,hold:1.}}
fn stamp(m:i64)->String{(NaiveDateTime::parse_from_str("2026-01-05 09:45:00","%Y-%m-%d %H:%M:%S").unwrap()+Duration::minutes(m)).format("%Y-%m-%d %H:%M:%S").to_string()}
#[test]
fn completed_body_failure_fills_next_open_symmetrically() {
    for short in [false,true] {
        let sign=if short{-1.}else{1.};let cv=|p|if short{200.-p}else{p};
        let mut e=ReplayEvent{symbol:"RB0".into(),direction:if short{"down"}else{"up"}.into(),warning_ts:stamp(0),entry:100.,stop:100.-sign*26.,target:100.+sign*50.,risk:26.,tick_size:2.,..Default::default()};e.setup_features.setup_quality=4.;
        let mut bars=vec![];
        for m in 1..=31 {
            let (o,h,l,c)=if m<=15{(100.,104.,98.,102.)}else if m<=30{(90.,92.,84.,86.)}else{(84.,86.,82.,84.)};
            bars.push(b(&stamp(m),cv(o),cv(if short{l}else{h}),cv(if short{h}else{l}),cv(c)));
        }
        let r=evaluate_research(&e,&bars,&[],&Default::default(),Experiment{policy:Policy::OriginalS1,no_follow_bar:None,slip_ticks:1.,fee_ticks:0.},10.,None,ResearchRules{body_failure_level:Some(cv(88.)),body_failure_count:Some(1),..Default::default()});
        assert_eq!(r.evaluation.exit_reason.as_deref(),Some("warning_body_failed_close"));
        assert_eq!(r.evaluation.exit_ts.as_deref(),Some(stamp(31).as_str()));assert_eq!(r.evaluation.final_price,Some(cv(82.)));
    }
}
#[test]
fn partial_entry_candle_cannot_trigger_close() {
    let mut observer=BodyFailure::default();
    for m in 1..=15 {assert!(!observer.observe(&b(&stamp(m),90.,92.,84.,86.),&stamp(1),88.,1.,1));}
}
#[test]
fn wick_breach_recovered_close_is_not_failure() {
    let mut observer=BodyFailure::default();
    for m in 1..=15 {assert!(!observer.observe(&b(&stamp(m),90.,94.,84.,92.),&stamp(0),88.,1.,1));}
}
#[test]
fn two_close_confirmation_requires_consecutive_full_candles() {
    let mut observer=BodyFailure::default();
    for m in 1..=15 {assert!(!observer.observe(&b(&stamp(m),90.,92.,84.,86.),&stamp(0),88.,1.,2));}
    // Missing an entire bucket never counts as a confirming consecutive bar.
    for m in 31..=45 {assert!(!observer.observe(&b(&stamp(m),90.,92.,84.,86.),&stamp(0),88.,1.,2));}
    for m in 46..=60 {assert_eq!(observer.observe(&b(&stamp(m),90.,92.,84.,86.),&stamp(0),88.,1.,2),m==60);}
}
