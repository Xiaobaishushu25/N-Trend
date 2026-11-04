//! R5a - low-frequency database refresh; lock-free-epoch hot-path validation.
//!
//! Normal quote ticks clone only Arc handles. Same-process repository writes
//! bump atomic epochs; external connections are observed via SQLite revisions
//! no later than the next periodic probe. Never serve stale data if a refresh
//! fails: propagate the error and retry on the next tick.
use anyhow::{anyhow, Result};
use sea_orm::DatabaseConnection;
use std::{future::Future, sync::Arc, time::{Duration, Instant}};
use tokio::sync::{Mutex, RwLock};

use crate::storage::{entities::{pattern_events, symbols}, repo};

const EXTERNAL_REVISION_POLL: Duration = Duration::from_secs(10);

struct Snapshot<T> {
    rows: Arc<Vec<T>>,
    revision: i64,
    local_epoch: u64,
    checked_at: Instant,
}

struct RevisionCache<T> {
    current: RwLock<Option<Snapshot<T>>>,
    refresh_lock: Mutex<()>,
}

impl<T> Default for RevisionCache<T> {
    fn default() -> Self {
        Self { current: RwLock::new(None), refresh_lock: Mutex::new(()) }
    }
}

impl<T> RevisionCache<T> {
    async fn get<F, Fut>(
        &self,
        db: &DatabaseConnection,
        scope: &str,
        epoch: fn() -> u64,
        load: F,
    ) -> Result<Arc<Vec<T>>>
    where
        F: Fn() -> Fut,
        Fut: Future<Output = Result<Vec<T>>>,
    {
        // No SQL, allocation of row vectors or long-held lock on a normal tick.
        let now_epoch = epoch();
        if let Some(cached) = self.current.read().await.as_ref() {
            if cached.local_epoch == now_epoch && cached.checked_at.elapsed() < EXTERNAL_REVISION_POLL {
                let rows = cached.rows.clone();
                if epoch() == now_epoch { return Ok(rows); }
            }
        }
        // All concurrent readers share one refresh rather than stampeding SQLite.
        let _refresh = self.refresh_lock.lock().await;
        let now_epoch = epoch();
        {
            let guard = self.current.read().await;
            if let Some(cached) = guard.as_ref() {
                if cached.local_epoch == now_epoch && cached.checked_at.elapsed() < EXTERNAL_REVISION_POLL {
                    let rows = cached.rows.clone();
                    if epoch() == now_epoch { return Ok(rows); }
                }
            }
        }
        // Revision-only probe after 10 seconds, not on every 2s quote tick.
        let db_revision = repo::get_revision(db, scope).await?;
        {
            let mut guard = self.current.write().await;
            if let Some(cached) = guard.as_mut() {
                if cached.local_epoch == now_epoch && cached.revision == db_revision
                    && epoch() == now_epoch
                {
                    cached.checked_at = Instant::now();
                    return Ok(cached.rows.clone());
                }
            }
        }
        // Observe revisions before and after materializing rows. If a write
        // raced with refresh, retry rather than install an inconsistent cache.
        for _ in 0..3 {
            let before_epoch = epoch();
            let before_revision = repo::get_revision(db, scope).await?;
            let rows = load().await?;
            let after_revision = repo::get_revision(db, scope).await?;
            if before_epoch != epoch() || before_revision != after_revision {
                continue;
            }
            let rows = Arc::new(rows);
            *self.current.write().await = Some(Snapshot {
                rows: rows.clone(),
                revision: after_revision,
                local_epoch: before_epoch,
                checked_at: Instant::now(),
            });
            tracing::debug!(scope, count = rows.len(), "[R5A_CACHE] 刷新缓存");
            return Ok(rows);
        }
        Err(anyhow!("R5a cache refresh raced with repeated {scope} changes"))
    }
}

#[derive(Default)]
pub(super) struct MarketCache {
    pending: RevisionCache<pattern_events::Model>,
    symbols: RevisionCache<symbols::Model>,
}

impl MarketCache {
    pub(super) async fn pending(&self, db: &DatabaseConnection) -> Result<Arc<Vec<pattern_events::Model>>> {
        self.pending.get(db, "signals", repo::r5a_signal_epoch,
            || repo::pending_entry_events(db)).await
    }

    pub(super) async fn symbols(&self, db: &DatabaseConnection) -> Result<Arc<Vec<symbols::Model>>> {
        self.symbols.get(db, "symbols", repo::r5a_symbol_epoch,
            || repo::list_symbols(db, false)).await
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use sea_orm::ConnectionTrait;

    fn test_warning() -> &'static str {
        r#"INSERT INTO pattern_events(
             symbol,direction,grade,level,s0_ts,s0_price,s1_ts,s1_price,
             s2_ts,s2_price,warning_ts,detected_at,warning_kind,entry_score,
             entry,stop,target,risk,rr,state,created_at,updated_at
           ) VALUES (
             'MA0','up','A','fine','2026-08-11 09:15:00',1,
             '2026-08-11 09:30:00',2,'2026-08-11 09:45:00',1,
             '2026-08-11 10:00:00','2026-08-11 10:00:00','wick',4,
             2,1,4,1,2,'pending','2026-08-11 10:00:00','2026-08-11 10:00:00'
           )"#
    }

    #[tokio::test]
    async fn r5a_no_repeat_full_select_and_external_revision_probe() {
        let db = crate::storage::connect(std::path::Path::new(":memory:")).await.unwrap();
        let cache = MarketCache::default();
        let first = cache.pending(&db).await.unwrap();
        assert!(first.is_empty());
        // Another test can advance a process-wide epoch concurrently. The
        // initial read must remain valid regardless of those external writes.
        let _same = cache.pending(&db).await.unwrap();

        // A direct database write bypasses local atomic invalidation.
        db.execute_unprepared(test_warning()).await.unwrap();
        cache.pending.current.write().await.as_mut().unwrap().checked_at =
            Instant::now() - EXTERNAL_REVISION_POLL;
        let refreshed = cache.pending(&db).await.unwrap();
        assert_eq!(refreshed.len(), 1);
        assert!(!Arc::ptr_eq(&first, &refreshed));
    }

    #[tokio::test]
    async fn r5a_repository_mutation_invalidates_immediately() {
        let db = crate::storage::connect(std::path::Path::new(":memory:")).await.unwrap();
        db.execute_unprepared(test_warning()).await.unwrap();
        let cache = MarketCache::default();
        assert_eq!(cache.pending(&db).await.unwrap().len(), 1);
        let mut row = repo::pending_entry_events(&db).await.unwrap().pop().unwrap();
        row.state = "triggered".into();
        row.trigger_ts = Some("2026-08-11 10:01:00".into());
        repo::update_pattern_event(&db, row).await.unwrap();
        // No timer tick or polling wait required after a local update.
        assert!(cache.pending(&db).await.unwrap().is_empty());
    }
}
