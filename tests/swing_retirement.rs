use n_core::analyze::{pattern::{research_set_selector,ResearchOriginState,research_trace_take},event::replay_warnings,model::{Bar,DT}};
use chrono::{Duration,NaiveDateTime};
#[test]
fn failed_newer_impulse_is_detected_before_validity_filter(){
    use n_core::analyze::{model::{Swing,Dir},pattern::{research_impulse_owner,research_owner_failed}};
    let mut bars=(0..36).map(|_|Bar{dt:DT::from_bar_ts("2026-01-05 09:00:00").unwrap(),open:120.,high:122.,low:118.,close:121.,volume:100.,hold:1.,rollover:false}).collect::<Vec<_>>();
    bars[20].low=100.;bars[22].low=110.;bars[24].open=118.;bars[24].high=135.;bars[24].low=117.;bars[24].close=134.;bars[28].high=140.;bars[35].low=109.;
    let starts=vec![Swing{index:20,price:100.,is_high:false},Swing{index:22,price:110.,is_high:false}];let flags=vec![(true,true);36];
    let end=Swing{index:35,price:109.,is_high:false};
    let older=research_impulse_owner("large",Dir::Up,end,&bars,&starts[..1],&flags,48,32).unwrap();
    let newest=research_impulse_owner("large",Dir::Up,end,&bars,&starts,&flags,48,32).unwrap();
    assert!(!research_owner_failed(&older));assert_eq!(newest.s0.index,22);assert!(research_owner_failed(&newest));
    let mirror=bars.iter().map(|b|Bar{dt:b.dt,open:240.-b.open,high:240.-b.low,low:240.-b.high,close:240.-b.close,volume:b.volume,hold:b.hold,rollover:false}).collect::<Vec<_>>();
    let starts=starts.iter().map(|s|Swing{index:s.index,price:240.-s.price,is_high:true}).collect::<Vec<_>>();
    let newest=research_impulse_owner("large",Dir::Down,Swing{index:35,price:131.,is_high:true},&mirror,&starts,&flags,48,32).unwrap();
    assert_eq!(newest.s0.index,22);assert!(research_owner_failed(&newest));
}
#[test]
fn retired_origin_never_revives_without_a_later_origin(){
    let mut s=ResearchOriginState::default();s.observe(6,Some((12,18,true)));
    assert!(!s.allows(6,Some((12,24,false)),12,24));
    assert!(!s.allows(6,None,4,18));
    assert!(s.allows(6,Some((19,24,false)),19,24));
    s.observe(6,Some((8,20,true)));assert_eq!(s.retired,Some(12));
    s.observe(6,Some((19,24,true)));assert!(!s.allows(6,None,19,28));
}
#[test]
fn current_failure_and_owner_have_distinct_reset_rules(){
    let s=ResearchOriginState::default();
    assert!(!s.allows(5,Some((12,18,true)),4,18));
    assert!(s.allows(5,Some((12,18,false)),4,18));
    assert!(s.allows(5,None,4,18));
    assert!(!s.allows(7,Some((12,18,false)),4,18));
    assert!(s.allows(7,Some((12,18,false)),12,18));
    assert!(!s.allows(7,None,12,18));
}
#[test]
fn full_generator_and_retirement_trace_are_prefix_causal(){
    let origin=NaiveDateTime::parse_from_str("2026-01-05 09:00:00","%Y-%m-%d %H:%M:%S").unwrap();let mut close:f64=1000.;let mut bars=vec![];
    for i in 0..196{let phase=i%28;let delta=if phase<=12{3.}else if phase<=18{-3.}else if phase==19{6.}else{1.};let open=close;close+=delta;bars.push(Bar{dt:DT::from_bar_ts(&(origin+Duration::minutes(i as i64*15)).format("%Y-%m-%d %H:%M:%S").to_string()).unwrap(),open,high:open.max(close)+0.4,low:open.min(close)-0.4,close,volume:100.,hold:1.,rollover:false});}
    let snap=|xs:Vec<n_core::analyze::event::WarningCandidate>|xs.into_iter().filter(|e|e.warning_index<140).map(|e|format!("{}|{}|{}|{}|{:?}|{}|{}|{}|{}",e.s0_index,e.s1_index,e.s2_index,e.warning_index,e.direction,e.entry,e.stop,e.target,e.entry_score)).collect::<Vec<_>>();
    for mode in [2,5,6,7]{research_set_selector(mode);let full=replay_warnings("RB0",&bars,1.);let ft=research_trace_take().into_iter().filter(|e|e.w<140).collect::<Vec<_>>();let prefix=replay_warnings("RB0",&bars[..140],1.);let pt=research_trace_take();
        assert!(!full.is_empty(),"empty fixture mode{mode}");assert_eq!(snap(full),snap(prefix),"future bars affected warnings mode{mode}");assert_eq!(serde_json::to_value(ft).unwrap(),serde_json::to_value(pt).unwrap(),"future bars affected state mode{mode}");
    }research_set_selector(0);
}
