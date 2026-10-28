use n_core::{fetch::kline::Kline,v2::replay::ReplayEvent,v3::ablation::{evaluate_research,Experiment,Policy,ResearchRules}};
fn event(short:bool)->ReplayEvent{
    let s=if short{-1.}else{1.};let mut e=ReplayEvent{symbol:"RB0".into(),direction:if short{"down"}else{"up"}.into(),warning_ts:"2026-01-05 11:00:00".into(),entry:100.,stop:100.-s*26.,target:100.+s*50.,risk:26.,tick_size:2.,..Default::default()};e.setup_features.setup_quality=4.;e
}
#[test]
fn losing_trade_exits_at_next_quote_open_before_lunch_symmetrically(){
    for short in [false,true]{
        let cv=|p|if short{200.-p}else{p};let e=event(short);let mut bars=vec![];
        for minute in 1..=21{
            let o=if minute==21{98.}else{100.};let c=if minute==1{102.}else{100.};
            bars.push(Kline{datetime:format!("2026-01-05 11:{minute:02}:00"),open:cv(o),high:cv(if short{96.}else{104.}),low:cv(if short{104.}else{96.}),close:cv(c),volume:1.,hold:1.});
        }
        let r=evaluate_research(&e,&bars,&[],&Default::default(),Experiment{policy:Policy::OriginalS1,no_follow_bar:None,slip_ticks:1.,fee_ticks:0.},10.,None,ResearchRules{session_failure_exit:true,skip_prebreak_entries:true,..Default::default()});
        assert_eq!(r.evaluation.exit_reason.as_deref(),Some("prebreak_failed_start"));assert_eq!(r.evaluation.exit_ts.as_deref(),Some("2026-01-05 11:21:00"));
        assert_eq!(r.evaluation.final_price,Some(cv(96.)));
    }
}
#[test]
fn profitable_position_is_not_flattened_by_loss_rule(){
    let e=event(false);let bars:Vec<_>=(1..=21).map(|minute|Kline{datetime:format!("2026-01-05 11:{minute:02}:00"),open:if minute==1{100.}else{104.},high:108.,low:if minute==1{100.}else{104.},close:106.,volume:1.,hold:1.}).collect();
    let r=evaluate_research(&e,&bars,&[],&Default::default(),Experiment{policy:Policy::OriginalS1,no_follow_bar:None,slip_ticks:1.,fee_ticks:0.},10.,None,ResearchRules{session_failure_exit:true,..Default::default()});
    assert_ne!(r.evaluation.exit_reason.as_deref(),Some("prebreak_failed_start"));assert_eq!(r.evaluation.status,"unfinished");
}
