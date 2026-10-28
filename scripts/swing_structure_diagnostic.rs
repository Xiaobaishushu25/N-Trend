//! Fixed-entry counterfactuals, never a selectable live strategy.
use anyhow::{ensure,Result};
use chrono::{Duration,NaiveDateTime,TimeZone,Timelike};
use n_core::{fetch::kline::Kline,trader::ExecutionConfig,v2::replay::ReplayEvent,v3::ablation::{self,EntryGuard,Experiment,HolidayWindow,Policy,ResearchRules}};
use serde_json::{json,Value};
use std::{collections::BTreeMap,fs,io::{BufWriter,Write},path::Path};
fn read(p:impl AsRef<Path>)->Result<Value>{Ok(serde_json::from_slice(&fs::read(p)?)?)}
fn time(t:&str)->NaiveDateTime{NaiveDateTime::parse_from_str(t,"%Y-%m-%d %H:%M:%S").unwrap()}
fn grid(p:f64,t:f64,up:bool)->f64{if up{(p/t-1e-9).ceil()*t}else{(p/t+1e-9).floor()*t}}
fn guard(v:&Value)->Result<EntryGuard>{
    let mut g=EntryGuard{min_net_reward_loss:1.,assumed_slip_ticks:1.,open:Default::default(),close_today:Default::default(),close_yesterday:Default::default(),roundtrip_fixed_override:None};
    if let Some(x)=v.get("roundtrip_fixed_yuan_per_lot"){g.roundtrip_fixed_override=x.as_f64();}else{
        g.open=serde_json::from_value(v["open"].clone())?;g.close_today=serde_json::from_value(v["close_today"].clone())?;g.close_yesterday=serde_json::from_value(v["close_yesterday"].clone())?;
    }Ok(g)
}
fn missing(symbol:&str,prior:NaiveDateTime,current:NaiveDateTime,hol:&[HolidayWindow])->bool{
    let mut t=prior+Duration::minutes(1);
    while t<current{
        let label=t.format("%Y-%m-%d %H:%M:%S").to_string();let holiday=hol.iter().any(|h|label>h.last_close&&label<h.reopen);
        let day_only=matches!(symbol.trim_end_matches(|c:char|c.is_ascii_digit()),"SF"|"SM")&&(t.hour()>=20||t.hour()<6);
        let m=t.hour()*60+t.minute();use n_core::session::spec::NightSessionType::*;
        let night=n_core::session::SessionCalendar::get(symbol).night_type;
        let end_label=(541..=615).contains(&m)||(631..=690).contains(&m)||(811..=900).contains(&m)||(!day_only&&match night{None=>false,Close2300=>(1261..=1380).contains(&m),Close2330=>(1261..=1410).contains(&m),Close0100=>m>=1261||m<=60,Close0230=>m>=1261||m<=150});
        if end_label&&!holiday&&!day_only&&chrono::Local.from_local_datetime(&t).single().is_some_and(|x|n_core::session::SessionCalendar::is_trading_time(symbol,&x)){return true;}
        t+=Duration::minutes(1);
    }false
}
fn recovery(row:&Value,ev:&ReplayEvent,all:&[Kline],horizon:usize,hol:&[HolidayWindow])->Value{
    let e=&row["result"]["evaluation"];let exit=e["exit_ts"].as_str().unwrap();let fill=e["fill"].as_f64().unwrap();let risk=e["risk"].as_f64().unwrap();let sign=if ev.direction=="up"{1.}else{-1.};
    let start=all.partition_point(|b|b.datetime.as_str()<=exit);let mut prior=time(exit);let mut last=String::new();let mut n=0;let mut complete=false;let mut quality="ok";let mut cross=false;
    let mut t0:Option<String>=None;let mut t1:Option<String>=None;let mut t2:Option<String>=None;let mut mfe:f64=-1.;let mut mae:f64=1.;
    // The stopped minute may already have crossed a much wider stop at its open.
    if (e["final_price"].as_f64().unwrap()+sign*row["slip"].as_f64().unwrap()*ev.tick_size-fill)*sign/risk<=-2.{t2=Some(exit.into());}
    for b in &all[start..]{
        let t=time(&b.datetime);if missing(&ev.symbol,prior,t,hol){quality="missing_data";break;}if b.volume<=0.{quality="zero_volume";break;}
        let bucket=n_core::derive::current_bucket_end_ts(t,n_core::derive::Timeframe::M15);
        if bucket!=last{n+=1;last=bucket.clone();}if n>horizon{complete=true;break;}
        cross|=(t-prior).num_minutes()>30;prior=t;
        let good=if sign>0.{b.high}else{b.low};let bad=if sign>0.{b.low}else{b.high};let up=(good-fill)*sign/risk;let down=(bad-fill)*sign/risk;mfe=mfe.max(up);mae=mae.min(down);
        if up>=0.&&t0.is_none(){t0=Some(b.datetime.clone());}if up>=1.&&t1.is_none(){t1=Some(b.datetime.clone());}if down<=-2.&&t2.is_none(){t2=Some(b.datetime.clone());}
        if n==horizon&&b.datetime==bucket{complete=true;break;}
    }
    let ordered=|hit:&Option<String>|->&str{match(hit.as_deref(),t2.as_deref()){(Some(a),Some(b))if a==b=>"same_minute_ambiguous",(Some(a),Some(b))if a<b=>"recovery_first",(Some(_),None)=>"recovery_first",(Some(_),Some(_))=>"two_r_adverse_first",(None,Some(_))=>"two_r_adverse_first",(None,None)=>"neither"}};
    json!({"horizon":horizon,"complete":complete,"quality":quality,"crossed_long_break":cross,"entry_recovery":t0,"one_r_recovery":t1,"two_r_adverse":t2,"entry_vs_two_r":ordered(&t0),"one_r_vs_two_r":ordered(&t1),"mfe_original_r":mfe,"mae_original_r":mae})
}
fn main()->Result<()>{
    let args:Vec<_>=std::env::args().collect();ensure!(args.len()==5,"protocol period cohorts new-output");ensure!(!Path::new(&args[4]).exists(),"immutable output exists");
    let p=read(&args[1])?;let paths=&p["periods"][&args[2]];let inp=read(paths["input"].as_str().unwrap())?;let data=read(paths["contracts"].as_str().unwrap())?;let fees=read(paths["fees"].as_str().unwrap())?;let hol=read(paths["holidays"].as_str().unwrap())?;
    let windows:Vec<HolidayWindow>=serde_json::from_value(hol["windows"].clone())?;let mut cfg:ExecutionConfig=serde_json::from_value(inp["execution_config"].clone())?;cfg.min_entry_risk_ticks=10;
    let contracts:BTreeMap<String,Vec<Kline>>=data.as_object().unwrap().iter().map(|(k,v)|Ok((k.clone(),serde_json::from_value(v["minutes"].clone())?))).collect::<Result<_>>()?;drop(data);
    let cohort=read(&args[3])?;let mut out=BufWriter::new(fs::File::create(&args[4])?);let mut n=0;
    for item in cohort.as_array().unwrap(){
        let row=&item["baseline"];let base=&row["result"]["evaluation"];let mut ev:ReplayEvent=serde_json::from_value(item["event"].clone())?;
        ev.target=row["target"].as_f64().unwrap();ev.stop=row["stop"].as_f64().unwrap();ev.risk=(ev.entry-ev.stop).abs();let sign=if ev.direction=="up"{1.}else{-1.};let mult=row["multiplier"].as_f64().unwrap();
        let product=ev.symbol.trim_end_matches(|c:char|c.is_ascii_digit()).to_uppercase();let g=guard(&fees["products"][&product])?;let all=&contracts[row["execution_contract"].as_str().unwrap()];let begin=all.partition_point(|b|b.datetime<=ev.warning_ts);
        let mut rules=ResearchRules{min_actual_risk_ticks:Some(10.),flat_before_weekend:true,..Default::default()};
        if item["label"]=="observation"{if ev.market_context.as_ref().and_then(|m|m.trend_alignment_60).is_some_and(|h|h<=0.){
            if ev.warning_kind!="cumulative"{rules.entry_mode=ablation::EntryMode::CloseConfirmNextOpen;}
            if ev.market_context.as_ref().and_then(|m|m.trend_alignment_60).is_some_and(|h|h<=-1.){rules.skip_prebreak_entries=true;rules.session_failure_exit=true;}
        }}
        let ex=Experiment{policy:Policy::OriginalS1,no_follow_bar:Some(5),slip_ticks:1.,fee_ticks:0.};
        let control=ablation::evaluate_research_with_holidays(&ev,&all[begin..],&[],&cfg,ex,mult,Some(g),rules,&windows);
        ensure!(control.evaluation.entry_ts.as_deref()==base["entry_ts"].as_str(),"control entry {:?}",item["event_key"]);
        writeln!(out,"{}",json!({"kind":"control","label":item["label"],"event_key":item["event_key"],"result":control}))?;
        let fill=base["fill"].as_f64().unwrap();let risk=base["risk"].as_f64().unwrap();let ts=base["entry_ts"].as_str().unwrap();let at=all.binary_search_by(|b|b.datetime.as_str().cmp(ts)).unwrap();ensure!(at>0,"no preceding quote");
        let mut frozen=ev.clone();frozen.warning_ts=all[at-1].datetime.clone();frozen.entry=fill-sign*ev.tick_size;
        let specs:Vec<(String,usize,f64,Option<f64>)>=[8,16,60].into_iter().flat_map(|h|[0.5,1.,2.].into_iter().map(move|t|(format!("target{t}_h{h}"),h,1.,Some(t))))
            .chain([16,60].into_iter().flat_map(|h|[1.,1.5,2.,0.].into_iter().map(move|w|(format!("stop{w}_h{h}"),h,w,None)))).collect();
        for(name,horizon,width,target)in specs{
            let mut e=frozen.clone();e.stop=if width==0.{item["mechanism"]["true_b_stop"].as_f64().unwrap()}else{grid(fill-sign*width*risk,e.tick_size,sign<0.)};
            e.risk=(e.entry-e.stop).abs();e.target=target.map_or(ev.target,|x|grid(fill+sign*x*risk,e.tick_size,sign<0.));
            let r=ablation::evaluate_research_with_holidays(&e,&all[at..],&[],&cfg,Experiment{no_follow_bar:None,..ex},mult,None,ResearchRules{flat_before_weekend:true,max_holding_buckets:Some(horizon),..Default::default()},&windows);
            let check=|v:&n_core::v3::replay::Evaluation|v.fill.is_none()||(v.fill==Some(fill)&&v.entry_ts.as_deref()==Some(ts));
            ensure!(check(&r.evaluation),"frozen entry changed {} {:?}",name,item["event_key"]);
            if let Some(d)=&r.disagreement{ensure!(check(&d.open_high_low_close)&&check(&d.open_low_high_close),"ambiguous path entry changed");}
            writeln!(out,"{}",json!({"kind":"diagnostic","label":item["label"],"event_key":item["event_key"],"scenario":name,"fixed_risk":risk,"stop":e.stop,"target":e.target,"result":r}))?;n+=1;
        }
        if base["exit_reason"]=="stop"{for h in [4,8,16]{writeln!(out,"{}",json!({"kind":"recovery","label":item["label"],"event_key":item["event_key"],"recovery":recovery(row,&ev,all,h,&windows)}))?;}}
    }
    out.flush()?;println!("{} fixed-entry diagnostic rows {n}",args[2]);Ok(())
}
