//! Warning-body invalidation from COMPLETE post-entry candles only.
use crate::{derive::{current_bucket_end_ts,Timeframe},fetch::kline::Kline};
use chrono::{Duration,NaiveDateTime};
#[derive(Clone,Default)]
pub struct BodyFailure {
    bucket:String,minutes:Vec<Kline>,last_close:Option<NaiveDateTime>,streak:usize,
}
impl BodyFailure {
    pub fn observe(&mut self,bar:&Kline,entry:&str,level:f64,sign:f64,count:usize)->bool {
        if bar.datetime.as_str()<=entry{return false;}
        let end=NaiveDateTime::parse_from_str(&bar.datetime,"%Y-%m-%d %H:%M:%S").unwrap();
        let bucket=current_bucket_end_ts(end,Timeframe::M15);
        if self.bucket!=bucket {self.bucket=bucket.clone();self.minutes.clear();}
        self.minutes.push(bar.clone());
        if bar.datetime!=bucket || self.minutes.len()!=15{return false;}
        let start=end-Duration::minutes(15);
        if start.format("%Y-%m-%d %H:%M:%S").to_string().as_str()<entry{return false;}
        if !self.minutes.iter().enumerate().all(|(i,b)|b.datetime==(start+Duration::minutes(i as i64+1)).format("%Y-%m-%d %H:%M:%S").to_string()){return false;}
        if (bar.close-level)*sign<=0. {
            self.streak=if self.last_close.is_some_and(|t|end-t==Duration::minutes(15)){self.streak+1}else{1};
        } else {self.streak=0;}
        self.last_close=Some(end);self.streak>=count.max(1)
    }
}
