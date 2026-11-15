//! Injectable time for the production executor. Live constructors retain system time.
use chrono::{DateTime, FixedOffset, Local, Utc};
use std::sync::atomic::{AtomicI64, Ordering};

pub trait ExecutionClock: Send + Sync {
    fn now_utc(&self) -> DateTime<Utc>;
    fn now_local(&self) -> DateTime<FixedOffset> {
        self.now_utc().with_timezone(&Local).fixed_offset()
    }
}
pub(super) struct SystemClock;
impl ExecutionClock for SystemClock {
    fn now_utc(&self) -> DateTime<Utc> {
        Utc::now()
    }
}
pub struct ReplayClock(AtomicI64);
impl ReplayClock {
    pub fn new(timestamp: i64) -> Self {
        Self(AtomicI64::new(timestamp))
    }
    pub fn advance(&self, timestamp: i64) -> anyhow::Result<()> {
        anyhow::ensure!(
            timestamp >= self.0.load(Ordering::SeqCst),
            "Replay clock cannot move backwards"
        );
        anyhow::ensure!(
            DateTime::from_timestamp(timestamp, 0).is_some(),
            "Invalid replay time"
        );
        self.0.store(timestamp, Ordering::SeqCst);
        Ok(())
    }
}
impl ExecutionClock for ReplayClock {
    fn now_utc(&self) -> DateTime<Utc> {
        DateTime::from_timestamp(self.0.load(Ordering::SeqCst), 0).unwrap()
    }
    fn now_local(&self) -> DateTime<FixedOffset> {
        self.now_utc()
            .with_timezone(&FixedOffset::east_opt(28800).unwrap())
    }
}
