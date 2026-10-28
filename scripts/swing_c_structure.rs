//! Closed post-entry5m C proxy. No B pivots, future bars or entry changes.
use crate::fetch::kline::Kline;
use chrono::{Duration,NaiveDateTime,Timelike};
use serde::Serialize;
use std::cell::RefCell;

#[derive(Clone,Debug,Serialize)]
pub struct Trace{pub kind:&'static str,pub ts:String,pub pivot_ts:Option<String>,pub previous_pivot_ts:Option<String>,pub opposite_ts:Option<String>,pub price:Option<f64>,pub previous_price:Option<f64>,pub trigger:Option<f64>,pub observed:Option<f64>}
thread_local!{static TRACE:RefCell<Vec<Trace>>=const{RefCell::new(Vec::new())};}
pub fn clear_trace(){TRACE.with(|x|x.borrow_mut().clear());}
pub fn take_trace()->Vec<Trace>{TRACE.with(|x|std::mem::take(&mut *x.borrow_mut()))}
fn record(kind:&'static str,ts:&str,p:Option<&Pivot>,prev:Option<&Pivot>,opp:Option<&Pivot>,trigger:Option<f64>,observed:Option<f64>){TRACE.with(|x|x.borrow_mut().push(Trace{kind,ts:ts.into(),pivot_ts:p.map(|p|p.ts.clone()),previous_pivot_ts:prev.map(|p|p.ts.clone()),opposite_ts:opp.map(|p|p.ts.clone()),price:p.map(|p|p.price),previous_price:prev.map(|p|p.price),trigger,observed}));}

#[derive(Clone,Debug)]
struct Pivot{ts:String,price:f64}
#[derive(Clone,Debug)]
struct Support{pivot:Pivot,known_at:String,trigger:f64}
#[derive(Clone,Debug)]
struct Accum{label:String,last:String,n:usize,bar:Kline}
#[derive(Clone,Default)]
pub struct Structure{accum:Option<Accum>,closed:Vec<Kline>,adverse:Option<Pivot>,favorable:Option<Pivot>,candidate:Option<Support>,active:Option<Support>}
fn label(ts:&str)->Option<String>{let d=NaiveDateTime::parse_from_str(ts,"%Y-%m-%d %H:%M:%S").ok()?;Some((d+Duration::minutes(((5-d.minute()%5)%5)as i64)).format("%Y-%m-%d %H:%M:%S").to_string())}
fn apart(a:&str,b:&str,n:i64)->bool{let parse=|s:&str|NaiveDateTime::parse_from_str(s,"%Y-%m-%d %H:%M:%S").unwrap();(parse(b)-parse(a)).num_seconds()==n*60}
impl Structure{
    fn install_signed(&mut self,s:Support,now:&str,close:f64,sign:f64){
        if self.active.as_ref().is_none_or(|a|(s.pivot.price-a.pivot.price)*sign>0.){
            record("activated",now,Some(&s.pivot),None,None,Some(s.trigger),Some(close));self.active=Some(Support{known_at:now.into(),..s});
        }
    }
    fn aggregate(&mut self,b:&Kline)->Option<Kline>{
        let end=label(&b.datetime)?;
        if self.accum.as_ref().is_none_or(|a|a.label!=end||!apart(&a.last,&b.datetime,1)){
            self.accum=Some(Accum{label:end.clone(),last:b.datetime.clone(),n:1,bar:Kline{datetime:end.clone(),..b.clone()}});
        }else{let a=self.accum.as_mut().unwrap();a.last=b.datetime.clone();a.n+=1;a.bar.high=a.bar.high.max(b.high);a.bar.low=a.bar.low.min(b.low);a.bar.close=b.close;a.bar.volume+=b.volume;}
        if b.datetime==end{let a=self.accum.take().unwrap();if a.n==5{return Some(a.bar);}}
        None
    }
    /// Return failure only from information completed AFTER actual entry and
    /// AFTER support confirmation/activation. Fill remains next observed open.
    pub fn observe(&mut self,b:&Kline,entry:&str,sign:f64,tick:f64,require_push:bool,wick:bool)->bool{
        if b.datetime.as_str()<=entry{return false;}
        if b.volume<=0.{self.accum=None;self.closed.clear();return false;}
        let observed=if wick{if sign>0.{b.low}else{b.high}}else{b.close};
        if let Some(a)=&self.active{
            if b.datetime>a.known_at&&(observed-(a.pivot.price-sign*tick))*sign<=0.{record("failed",&b.datetime,Some(&a.pivot),None,None,Some(a.pivot.price-sign*tick),Some(observed));return true;}
        }
        if let Some(a)=&self.candidate{
            if b.datetime>a.known_at&&(observed-(a.pivot.price-sign*tick))*sign<=0.{record("candidate_invalidated",&b.datetime,Some(&a.pivot),None,None,Some(a.pivot.price-sign*tick),Some(observed));self.candidate=None;}
        }
        if let Some(a)=self.candidate.clone(){
            if b.datetime>a.known_at&&(b.close-a.trigger)*sign>=0.{self.install_signed(a,&b.datetime,b.close,sign);self.candidate=None;}
        }
        if let Some(c)=self.aggregate(b){
            self.closed.push(c);if self.closed.len()>3{self.closed.remove(0);}
            if self.closed.len()==3{
                let a=self.closed[0].clone();let p=self.closed[1].clone();let c=self.closed[2].clone();
                if apart(&a.datetime,&p.datetime,5)&&apart(&p.datetime,&c.datetime,5){
                    let adverse=if sign>0.{p.low<a.low&&p.low<c.low}else{p.high>a.high&&p.high>c.high};
                    let favorable=if sign>0.{p.high>a.high&&p.high>c.high}else{p.low<a.low&&p.low<c.low};
                    let pivot=Pivot{ts:p.datetime.clone(),price:if sign>0.{p.low}else{p.high}};
                    if adverse{
                        record("adverse_pivot",&b.datetime,Some(&pivot),None,None,None,None);
                        if let (Some(prev),Some(opp))=(&self.adverse,&self.favorable){
                            if prev.ts<opp.ts&&opp.ts<pivot.ts&&(pivot.price-prev.price)*sign>0.{
                                let s=Support{pivot:pivot.clone(),known_at:b.datetime.clone(),trigger:opp.price+sign*tick};
                                record("candidate",&b.datetime,Some(&pivot),Some(prev),Some(opp),Some(s.trigger),Some(b.close));
                                if require_push{self.candidate=Some(s);}else{self.install_signed(s,&b.datetime,b.close,sign);}
                            }
                        }
                        self.adverse=Some(pivot);
                    }
                    if favorable{let pivot=Pivot{ts:p.datetime.clone(),price:if sign>0.{p.high}else{p.low}};record("favorable_pivot",&b.datetime,Some(&pivot),None,None,None,None);self.favorable=Some(pivot);}
                }
            }
        }
        false
    }
}
