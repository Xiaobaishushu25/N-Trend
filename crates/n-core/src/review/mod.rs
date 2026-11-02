//! Precomputed review service, separate from legacy event progression and UI.
mod query;
pub mod replay;
pub mod store;
#[cfg(test)]
mod tests;

use anyhow::Result;
use n_protocol::review::*;
use sea_orm::DatabaseConnection;
use std::{collections::HashMap, sync::Arc, time::Instant};
use tokio::sync::Mutex;

#[derive(Debug)]
pub struct ReviewError {
    pub code: &'static str,
    pub message: String,
}
impl std::fmt::Display for ReviewError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.message)
    }
}
impl std::error::Error for ReviewError {}
pub(crate) fn error(code: &'static str, message: &str) -> anyhow::Error {
    ReviewError {
        code,
        message: message.into(),
    }
    .into()
}

#[derive(Clone)]
struct Snapshot {
    owner: String,
    created: Instant,
    expires_at: String,
    revision: i64,
    key: String,
    rows: Arc<Vec<ReviewSummary>>,
    raw_count: usize,
    deduplicated_count: usize,
    metric: String,
    overview: ReviewStatistics,
    groups: std::collections::BTreeMap<String, Vec<ReviewGroup>>,
    equity: Vec<ReviewEquityPoint>,
    bytes: usize,
}

pub const SNAPSHOT_TTL_SECS: u64 = 3600;

pub struct ReviewEngine {
    pub(crate) db: DatabaseConnection,
    pub(crate) worker: Mutex<()>,
    query_slots: tokio::sync::Semaphore,
    snapshots: Mutex<HashMap<String, Arc<Snapshot>>>,
}
impl ReviewEngine {
    pub fn new(db: DatabaseConnection) -> Self {
        Self {
            db,
            worker: Mutex::new(()),
            query_slots: tokio::sync::Semaphore::new(4),
            snapshots: Mutex::new(HashMap::new()),
        }
    }
    async fn snapshot(&self, owner: &str, id: &str) -> Result<Arc<Snapshot>> {
        let mut snapshots = self.snapshots.lock().await;
        snapshots.retain(|_, s| s.created.elapsed().as_secs() < SNAPSHOT_TTL_SECS);
        snapshots
            .get(id)
            .filter(|s| s.owner == owner)
            .cloned()
            .ok_or_else(|| {
                error(
                    "SNAPSHOT_EXPIRED",
                    "query snapshot expired or unavailable; create a new query",
                )
            })
    }
}
