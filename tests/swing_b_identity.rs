use n_core::{fetch::kline::Kline,v2::replay::ReplayEvent,v3::ablation::{evaluate_research,EntryMode,Experiment,Policy,ResearchRules}};
fn bar(m:usize,o:f64,h:f64,l:f64,c:f64,short:bool)->Kline{let cv=|p|if short{200.-p}else{p};Kline{datetime:format!("2026-01-05 09:{m:02}:00"),open:cv(o),high:cv(if short{l}else{h}),low:cv(if short{h}else{l}),close:cv(c),volume:1.,hold:1.}}
fn eval(short:bool,raised:bool,confirm:bool,bars:&[Kline])->n_core::v3::ablation::ResultRow{
    let sign=if short{-1.}else{1.};let entry=100.+if raised{sign*10.}else{0.};let mut e=ReplayEvent{symbol:"RB0".into(),direction:if short{"down"}else{"up"}.into(),warning_ts:"2026-01-05 09:45:00".into(),entry,stop:100.-sign*26.,target:100.+sign*50.,risk:(entry-(100.-sign*26.)).abs(),tick_size:2.,..Default::default()};e.setup_features.setup_quality=4.;
    evaluate_research(&e,bars,&[],&Default::default(),Experiment{policy:Policy::OriginalS1,no_follow_bar:None,slip_ticks:1.,fee_ticks:0.},10.,None,ResearchRules{entry_mode:if confirm{EntryMode::CloseConfirmNextOpen}else{EntryMode::Touch},..Default::default()})
}
#[test]
fn raised_micro_level_cannot_fill_from_old_warning_trigger(){for short in [false,true]{let b=[bar(46,98.,104.,98.,102.,short)];assert!(eval(short,false,false,&b).evaluation.fill.is_some());assert!(eval(short,true,false,&b).evaluation.fill.is_none());}}
#[test]
fn micro_close_requires_close_then_next_open_and_increases_cash_risk(){for short in [false,true]{let b=[bar(46,98.,104.,98.,102.,short),bar(47,108.,114.,108.,112.,short),bar(48,112.,114.,112.,112.,short)];let a=eval(short,true,false,&b);let c=eval(short,true,true,&b);assert_eq!(a.evaluation.entry_ts.as_deref(),Some("2026-01-05 09:47:00"));assert_eq!(c.evaluation.entry_ts.as_deref(),Some("2026-01-05 09:48:00"));assert_eq!(c.evaluation.risk,40.);assert_eq!(c.evaluation.fill,Some(if short{86.}else{114.}));}}
#[test]
fn stop_before_micro_confirmation_cancels_without_trade(){for short in [false,true]{let b=[bar(46,98.,104.,72.,98.,short),bar(47,108.,114.,108.,112.,short),bar(48,112.,114.,112.,112.,short)];let c=eval(short,true,true,&b);assert_eq!(c.evaluation.status,"invalidated");assert!(c.evaluation.fill.is_none());}}
