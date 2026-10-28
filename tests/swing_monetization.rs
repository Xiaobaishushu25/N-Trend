use n_core::{fetch::kline::Kline,v2::replay::ReplayEvent,v3::ablation::{evaluate_research,Experiment,Policy,ResearchRules}};
#[test]
fn capped_target_rounds_toward_fill_and_never_claims_fractional_tick() {
    for short in [false,true] {
        let sign=if short{-1.}else{1.};let cv=|p|if short{200.-p}else{p};
        let mut e=ReplayEvent{symbol:"RB0".into(),direction:if short{"down"}else{"up"}.into(),warning_ts:"2026-01-05 09:45:00".into(),entry:100.,stop:100.-sign*26.,target:100.+sign*50.,risk:26.,tick_size:2.,..Default::default()};e.setup_features.setup_quality=4.;
        let bar=Kline{datetime:"2026-01-05 09:46:00".into(),open:100.,high:cv(if short{100.}else{140.}),low:cv(if short{140.}else{100.}),close:cv(140.),volume:1.,hold:1.};
        let r=evaluate_research(&e,&[bar],&[],&Default::default(),Experiment{policy:Policy::OriginalS1,no_follow_bar:None,slip_ticks:1.,fee_ticks:0.},10.,None,ResearchRules{target_cap_r:Some(1.25),..Default::default()});
        assert_eq!(r.evaluation.status,"closed");assert_eq!(r.evaluation.final_price,Some(cv(134.)));
        assert_eq!(r.evaluation.risk,28.);
        for f in r.fills {assert_eq!(f.price/2.,(f.price/2.).round());}
    }
}
