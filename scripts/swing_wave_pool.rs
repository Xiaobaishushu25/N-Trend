//! Full forward wave extraction and actual service dedup, no outcome labels.
use anyhow::{ensure,Result};
use n_core::{derive::rollover::RolloverRecord,fetch::kline::Kline,trader::candidates,v2::replay::ReplayEngine,v3::geometry_research as geom};
use serde_json::{json,Value};
use std::{collections::{BTreeMap,BTreeSet},fs,path::Path};
fn read(p:impl AsRef<Path>)->Result<Value>{Ok(serde_json::from_slice(&fs::read(p)?)?) }
fn key(e:&n_core::v2::replay::ReplayEvent)->String{format!("{}|{}|{}|{}|{}|{}",e.symbol,e.direction,e.s0_ts,e.s1_ts,e.s2_ts,e.warning_ts)}
fn friction(e:&n_core::v2::replay::ReplayEvent,fee:&Value,mult:f64)->f64{
    let sign=if e.direction=="up"{1.}else{-1.};let entry=e.entry+sign*e.tick_size;let stop=e.stop-sign*e.tick_size;
    let commission=if let Some(x)=fee.get("roundtrip_fixed_yuan_per_lot"){x.as_f64().unwrap()}else{
        let charge=|k:&str,p:f64|fee[k]["fixed_yuan_per_lot"].as_f64().unwrap()+fee[k]["notional_rate"].as_f64().unwrap()*p*mult;
        charge("open",entry)+charge("close_today",stop).max(charge("close_yesterday",stop))
    };(2.*e.tick_size*mult+commission)/(e.risk*mult)
}
fn main()->Result<()>{
    let a:Vec<_>=std::env::args().collect();ensure!(a.len()==5,"protocol period symbol-dir new-pool");ensure!(!Path::new(&a[4]).exists(),"immutable pool exists");
    let p=read(&a[1])?;let period=&a[2];let selector=p["selector"].as_u64().unwrap() as u8;n_core::analyze::pattern::research_set_selector(selector);
    let inp=read(p["periods"][period]["input"].as_str().unwrap())?;let fees=read(p["periods"][period]["fees"].as_str().unwrap())?;let checkpoint=Path::new(&a[3]);fs::create_dir_all(checkpoint)?;let engine=ReplayEngine::default_engine();let mut all=vec![];
    for(symbol,v)in inp["symbols"].as_object().unwrap(){
        let saved=checkpoint.join(format!("{symbol}.json"));if saved.exists(){let z=read(&saved)?;ensure!(z["selector"]==selector&&z["input_hash"]==p["hashes"][period]["input"],"stale checkpoint");all.extend(z["pool"].as_array().unwrap().iter().cloned());println!("{period} mode{selector} {symbol} cached");continue;}
        let raw:Vec<Kline>=serde_json::from_value(v["bars"].clone())?;let rolls:Vec<RolloverRecord>=v["rollovers"].as_array().unwrap().iter().map(|r|RolloverRecord{symbol:symbol.clone(),ts:r["ts"].as_str().unwrap().into(),from_contract:r["from_contract"].as_str().unwrap().into(),to_contract:r["to_contract"].as_str().unwrap().into(),confirmed:r["confirmed"].as_bool().unwrap()}).collect();
        println!("{period} mode{selector} {symbol} extracting {} source bars",raw.len());
        let events=engine.replay_history_with_rollovers(symbol,&raw,v["tick_size"].as_f64().unwrap(),&rolls)?;
        ensure!(events.iter().all(|e|e.evaluation.is_none()&&e.outcome.is_none()&&e.entry_ts.is_none()),"future labels leaked into generator");
        let (bars,_) =engine.aggregate_with_rollovers(&raw,&rolls);let kept=n_core::service::offline_forward_warning_keys(&bars,&events)?;let allowed:BTreeSet<_>=kept.iter().cloned().collect();
        let completed=candidates::completed_bars(&raw,candidates::parse_time(&raw.last().unwrap().datetime)?)?;let mut atrs=BTreeMap::new();let mut trs=vec![];let mut last:Option<f64>=None;
        for b in &completed{trs.push(last.map_or(b.high-b.low,|c|(b.high-b.low).max((b.high-c).abs()).max((b.low-c).abs())));last=Some(b.close);if trs.len()>=14{atrs.insert(b.datetime.clone(),trs[trs.len()-14..].iter().sum::<f64>()/14.);}}
        let mut pool=vec![];let rawkeys:Vec<_>=events.iter().map(key).collect();
        for mut e in events{
            let k=key(&e);if !allowed.contains(&k)||e.setup_features.setup_quality<2.5{continue;}
            if (period=="early"&&!("2026-01-01 00:00:00"..="2026-06-30 15:00:00").contains(&e.warning_ts.as_str()))||(period=="recent"&&!("2026-07-01 00:00:00"..="2026-10-23 11:05:00").contains(&e.warning_ts.as_str())){continue;}
            geom::normalize(&mut e);let Some(atr)=atrs.get(&e.warning_ts)else{continue;};let end=completed.partition_point(|b|b.datetime<e.warning_ts);let Some(warning)=completed.get(end).filter(|b|b.datetime==e.warning_ts)else{continue;};let prior=&completed[end.saturating_sub(12)..end];
            let at=bars.iter().position(|b|b.dt.to_bar_ts()==e.warning_ts).unwrap();let s1=bars.iter().position(|b|b.dt.to_bar_ts()==e.s1_ts).unwrap();let sign=if e.direction=="up"{1.}else{-1.};
            let bodies:Vec<f64>=bars[s1+1..at].iter().map(|b|(-sign*(b.close-b.open)).max(0.)).filter(|x|*x>0.).collect();let weak=if bodies.len()>=2{let m=bodies.len()/2;Some((bodies[m..].iter().sum::<f64>()/(bodies.len()-m)as f64)/(bodies[..m].iter().sum::<f64>()/m as f64))}else{None};
            let risk_atr=e.risk/atr;let mult=if symbol=="JD0"{10.}else{n_core::precision::effective_multiplier(symbol,v["variety"].as_str().unwrap())};
            let product=symbol.trim_end_matches(|c:char|c.is_ascii_digit()).to_uppercase();let f=friction(&e,&fees["products"][&product],mult);
            pool.push(json!({"event_key":k,"event":e,"atr14":atr,"original_friction":f,"original_risk_atr":risk_atr,"warning_candle":warning,"prior":prior,"b_weakening_ex_warning":weak}));
        }
        println!("{period} mode{selector} {symbol}: raw{} service{} pool{}",rawkeys.len(),kept.len(),pool.len());
        fs::write(saved,serde_json::to_vec_pretty(&json!({"selector":selector,"input_hash":p["hashes"][period]["input"],"raw_keys":rawkeys,"kept_keys":kept,"pool":pool}))?)?;all.extend(pool);
    }
    fs::write(&a[4],serde_json::to_vec_pretty(&all)?)?;println!("{period} mode{selector} complete pool{}",all.len());Ok(())
}
