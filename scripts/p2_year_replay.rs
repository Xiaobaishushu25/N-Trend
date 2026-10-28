//! Transport-only harness linked against the frozen research n_core library.
use n_core::{derive::rollover::RolloverRecord,fetch::kline::Kline,trader::ExecutionConfig,v2::replay::ReplayEngine,v3::{ablation::{self,EntryGuard,Experiment,HolidayWindow,Policy,ResearchRules},geometry_research as geom}};
use serde_json::{json,Value};
use std::{collections::BTreeMap,error::Error,fs,io::{BufWriter,Write},path::Path};
fn main()->Result<(),Box<dyn Error>>{
 let a:Vec<_>=std::env::args().collect();let out=Path::new(&a[1]);
 let memory:Value=serde_json::from_reader(std::io::stdin().lock())?;let inp=&memory["input"];let p=&memory["protocol"];
 n_core::analyze::pattern::research_set_selector(0);
 let engine=ReplayEngine::default_engine();let mut cfg:ExecutionConfig=serde_json::from_value(inp["execution_config"].clone())?;cfg.min_entry_risk_ticks=10;
 let windows:Vec<HolidayWindow>=serde_json::from_value(p["holidays"]["windows"].clone())?;
 let mut contracts=BTreeMap::<String,Vec<Kline>>::new();for(c,z)in memory["contracts"].as_object().unwrap(){contracts.insert(c.clone(),serde_json::from_value(z["minutes"].clone())?);}
 let mut events=vec![];let mut writer=BufWriter::new(fs::File::create(out.join("p2-evaluations.jsonl"))?);
 for(s,z)in inp["symbols"].as_object().unwrap(){
  let bars:Vec<Kline>=serde_json::from_value(z["bars"].clone())?;
  let rolls:Vec<RolloverRecord>=z["rollovers"].as_array().unwrap().iter().map(|r|RolloverRecord{symbol:s.clone(),ts:r["ts"].as_str().unwrap().into(),from_contract:r["from_contract"].as_str().unwrap().into(),to_contract:r["to_contract"].as_str().unwrap().into(),confirmed:r["confirmed"].as_bool().unwrap()}).collect();
  let all=engine.replay_history_with_rollovers(s,&bars,z["tick_size"].as_f64().unwrap(),&rolls)?;
  for mut e in all {
   if e.warning_ts.as_str()<p["warning_start"].as_str().unwrap()||e.warning_ts.as_str()>p["warning_end"].as_str().unwrap()||e.setup_features.setup_quality<2.5||e.risk<=0.{continue;}
   geom::normalize(&mut e);e.evaluation=None;e.outcome=None;
   let k=format!("{}|{}|{}|{}|{}|{}",s,e.direction,e.s0_ts,e.s1_ts,e.s2_ts,e.warning_ts);
   let seg=p["segments"][s].as_array().unwrap().iter().find(|r|r["first"].as_str().unwrap()<=e.warning_ts.as_str()&&e.warning_ts.as_str()<=r["last"].as_str().unwrap()).unwrap();let code=seg["underlying"].as_str().unwrap();
   let mult=if s=="JD0"{10.}else{n_core::precision::effective_multiplier(s,z["variety"].as_str().unwrap())};
   let prod=s.trim_end_matches(|c:char|c.is_ascii_digit()).to_uppercase();let f=&p["fees"]["products"][&prod];
   let mut g=EntryGuard{min_net_reward_loss:1.,assumed_slip_ticks:1.,open:Default::default(),close_today:Default::default(),close_yesterday:Default::default(),roundtrip_fixed_override:None};
   if let Some(x)=f.get("roundtrip_fixed_yuan_per_lot"){g.roundtrip_fixed_override=x.as_f64();}else{g.open=serde_json::from_value(f["open"].clone())?;g.close_today=serde_json::from_value(f["close_today"].clone())?;g.close_yesterday=serde_json::from_value(f["close_yesterday"].clone())?;}
   for slip in [1.,2.] {
    let rules:ResearchRules=serde_json::from_value(json!({"target_cap_r":null,"min_actual_risk_ticks":10.,"flat_before_weekend":true}))?;
    let all=&contracts[code];let mut result=ablation::evaluate_research_with_holidays(&e,all,&[],&cfg,Experiment{policy:Policy::OriginalS1,no_follow_bar:Some(5),slip_ticks:slip,fee_ticks:0.},mult,Some(g),rules,&windows);
    let zero=result.fills.iter().any(|f|all.binary_search_by(|b|b.datetime.cmp(&f.ts)).ok().is_none_or(|i|all[i].volume<=0.));
    if zero{result.evaluation.status="missing_data".into();result.evaluation.net_amount=None;result.evaluation.net_r=None;}
    writeln!(writer,"{}",json!({"symbol":s,"direction":e.direction,"event_key":k,"warning_ts":e.warning_ts,"scenario":format!("slip{slip}"),"multiplier":mult,"tick_size":e.tick_size,"planned_risk":e.risk,"score":e.setup_features.setup_quality,"execution_contract":code,"zero_volume_execution_unknown":zero,"result":result}))?;
   }
   events.push(e);
  }
 }
 writer.flush()?;fs::write(out.join("p2-events.json"),serde_json::to_vec(&events)?)?;println!("P2 {} events",events.len());Ok(())
}
