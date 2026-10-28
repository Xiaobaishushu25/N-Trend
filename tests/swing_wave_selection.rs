use n_core::analyze::{pattern::{research_priority,research_set_selector},event::replay_warnings,model::{Bar,DT}};
use chrono::{Duration,NaiveDateTime};
#[test]
fn priority_separates_recent_start_and_recent_turn(){
    assert_eq!(research_priority(0,9,10,2,20),None);assert_eq!(research_priority(1,9,10,2,20),None);
    assert_eq!(research_priority(2,9,10,2,20),Some(true));assert_eq!(research_priority(3,9,10,2,20),Some(false));
}
#[test]
fn all_selectors_are_prefix_causal_on_closed_bars(){
    let origin=NaiveDateTime::parse_from_str("2026-01-05 09:00:00","%Y-%m-%d %H:%M:%S").unwrap();let mut close:f64=1000.;let mut bars=vec![];
    for i in 0..168{let phase=i%28;let delta=if phase<=12{3.}else if phase<=18{-3.}else if phase==19{6.}else{1.};let open=close;close+=delta;bars.push(Bar{dt:DT::from_bar_ts(&(origin+Duration::minutes(i as i64*15)).format("%Y-%m-%d %H:%M:%S").to_string()).unwrap(),open,high:open.max(close)+0.4,low:open.min(close)-0.4,close,volume:100.,hold:1.,rollover:false});}
    for mode in 0..=3{research_set_selector(mode);let full=replay_warnings("RB0",&bars,1.);let prefix=replay_warnings("RB0",&bars[..112],1.);
        let snap=|xs:Vec<n_core::analyze::event::WarningCandidate>|xs.into_iter().filter(|e|e.warning_index<112).map(|e|format!("{}|{}|{}|{}|{:?}|{}|{}|{}|{}",e.s0_index,e.s1_index,e.s2_index,e.warning_index,e.direction,e.entry,e.stop,e.target,e.entry_score)).collect::<Vec<_>>();
        assert!(!full.is_empty(),"empty fixture mode{mode}");assert_eq!(snap(full),snap(prefix),"future bars affected mode{mode}");
    }research_set_selector(0);
}
