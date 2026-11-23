//! Persistent work queue for automatic 15m scans.

use anyhow::Result;
use sea_orm::{ConnectionTrait, DatabaseConnection, DbBackend, Statement, Value};

use crate::v2::version::EVENT_LOGIC_VERSION;

pub struct Job {
    id: i64,
    pub attempt: i64,
    revision: i64,
    pub symbol: String,
    pub bar_end: String,
    pub queued_at: i64,
}

#[derive(Clone, Copy, Debug)]
pub enum Work {
    Scan,
    Repair,
}

impl Work {
    fn states(self) -> (&'static str, &'static str) {
        match self {
            Self::Scan => ("pending", "running"),
            Self::Repair => ("repair", "repairing"),
        }
    }
}

pub enum Next {
    Done,
    Skipped,
    ScanAt(i64),
    RepairAt(i64),
}

pub(super) async fn ensure(db: &DatabaseConnection) -> Result<()> {
    db.execute_unprepared(
        "CREATE TABLE IF NOT EXISTS scan_jobs (
            id INTEGER PRIMARY KEY,
            symbol TEXT NOT NULL,
            bar_end TEXT NOT NULL,
            logic_version TEXT NOT NULL,
            revision INTEGER NOT NULL,
            attempt INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL DEFAULT 'pending',
            available_at INTEGER NOT NULL DEFAULT 0,
            queued_at INTEGER NOT NULL,
            UNIQUE(symbol, bar_end, logic_version)
        );
        CREATE INDEX IF NOT EXISTS idx_scan_jobs_due
            ON scan_jobs(logic_version, state, available_at, bar_end);
        CREATE INDEX IF NOT EXISTS idx_scan_jobs_round
            ON scan_jobs(logic_version, bar_end, state);
        CREATE INDEX IF NOT EXISTS idx_scan_jobs_symbol_active
            ON scan_jobs(logic_version, symbol, state, bar_end, available_at)
            WHERE state IN ('pending','repair','running','repairing');",
    )
    .await?;
    Ok(())
}

/// A new history revision reopens completed work. A running worker observes
/// the revision change on acknowledgement; an unchanged duplicate is a no-op.
pub async fn enqueue(
    db: &impl ConnectionTrait,
    symbol: &str,
    bar_end: &str,
    now: i64,
    expedite: bool,
) -> Result<bool> {
    let result = db.execute(Statement::from_sql_and_values(
        DbBackend::Sqlite,
        "INSERT INTO scan_jobs(symbol, bar_end, logic_version, revision, queued_at)
         VALUES (?1, ?2, ?3, COALESCE(
             (SELECT revision FROM market_history_revisions WHERE symbol=?1), 0
         ), ?4)
         ON CONFLICT(symbol, bar_end, logic_version) DO UPDATE SET
            revision=excluded.revision,
            state=CASE WHEN scan_jobs.state IN ('running','repairing') THEN scan_jobs.state ELSE 'pending' END,
            available_at=CASE WHEN scan_jobs.state IN ('running','repairing') THEN scan_jobs.available_at ELSE 0 END,
            queued_at=CASE WHEN scan_jobs.state='done' THEN excluded.queued_at ELSE scan_jobs.queued_at END
         WHERE scan_jobs.state='skipped' OR excluded.revision <> scan_jobs.revision
            OR (?5 AND scan_jobs.state IN ('pending','repair') AND scan_jobs.available_at > ?4)",
        [
            symbol.into(),
            bar_end.into(),
            EVENT_LOGIC_VERSION.into(),
            now.into(),
            expedite.into(),
        ],
    ))
    .await?;
    Ok(result.rows_affected() != 0)
}

pub async fn enqueue_scheduled(
    db: &DatabaseConnection,
    symbols: &[&str],
    bar_end: &str,
    now: i64,
) -> Result<bool> {
    if symbols.is_empty() {
        return Ok(false);
    }
    let values = vec!["(?)"; symbols.len()].join(",");
    let mut params = symbols.iter().map(|symbol| Value::from(*symbol)).collect::<Vec<_>>();
    params.extend([bar_end.into(), EVENT_LOGIC_VERSION.into()]);
    // The scheduler probes every five seconds. Already settled rounds should
    // remain readers; the upsert below still rechecks the predicate after a race.
    let due = db.query_one(Statement::from_sql_and_values(
        DbBackend::Sqlite,
        format!(
            "WITH due(symbol) AS (VALUES {values})
             SELECT 1 FROM due LEFT JOIN market_history_revisions history ON history.symbol=due.symbol
             LEFT JOIN scan_jobs job ON job.symbol=due.symbol AND job.bar_end=? AND job.logic_version=?
             WHERE job.id IS NULL OR job.state='skipped'
                OR job.revision<>COALESCE(history.revision,0) LIMIT 1"
        ),
        params.clone(),
    )).await?;
    if due.is_none() {
        return Ok(false);
    }
    params.push(now.into());
    let result = db.execute(Statement::from_sql_and_values(
        DbBackend::Sqlite,
        format!(
            "WITH due(symbol) AS (VALUES {values})
             INSERT INTO scan_jobs(symbol, bar_end, logic_version, revision, queued_at)
             SELECT due.symbol, ?, ?, COALESCE(history.revision, 0), ?
             FROM due LEFT JOIN market_history_revisions history ON history.symbol=due.symbol
             WHERE true
             ON CONFLICT(symbol, bar_end, logic_version) DO UPDATE SET
                revision=excluded.revision,
                state=CASE WHEN scan_jobs.state IN ('running','repairing') THEN scan_jobs.state ELSE 'pending' END,
                available_at=CASE WHEN scan_jobs.state IN ('running','repairing') THEN scan_jobs.available_at ELSE 0 END,
                queued_at=CASE WHEN scan_jobs.state='done' THEN excluded.queued_at ELSE scan_jobs.queued_at END
             WHERE scan_jobs.state='skipped' OR excluded.revision <> scan_jobs.revision"
        ),
        params,
    ))
    .await?;
    Ok(result.rows_affected() != 0)
}

/// The server owns a process lock. Work left
/// running by the previous process can be made immediately available.
pub async fn recover(db: &DatabaseConnection) -> Result<()> {
    db.execute(Statement::from_sql_and_values(
        DbBackend::Sqlite,
        "UPDATE scan_jobs SET state=CASE WHEN state='running' THEN 'pending' ELSE 'repair' END,
             available_at=0 WHERE state IN ('running','repairing') AND logic_version=?",
        [EVENT_LOGIC_VERSION.into()],
    ))
    .await?;
    Ok(())
}

pub async fn claim(db: &DatabaseConnection, work: Work, now: i64) -> Result<Option<Job>> {
    let (queued, running) = work.states();
    // Ready tasks for the same symbol run in close-time order. A task in
    // retry backoff can yield to a later close; the scan cursor never rewinds.
    const NEXT_JOB: &str = "SELECT job.id FROM scan_jobs job
        WHERE job.logic_version=?1 AND job.state=?2 AND job.available_at<=?3
          AND NOT EXISTS (
            SELECT 1 FROM scan_jobs other
            WHERE other.logic_version=job.logic_version AND other.symbol=job.symbol
              AND other.state IN ('pending','repair','running','repairing')
              AND (other.state IN ('running','repairing')
                   OR (other.bar_end < job.bar_end AND other.available_at<=?3))
          )
        ORDER BY job.bar_end, job.queued_at, job.id LIMIT 1";
    let args = [EVENT_LOGIC_VERSION.into(), queued.into(), now.into()];
    if db
        .query_one(Statement::from_sql_and_values(
            DbBackend::Sqlite,
            NEXT_JOB,
            args.clone(),
        ))
        .await?
        .is_none()
    {
        return Ok(None);
    }
    // Re-select under the write statement's SQLite lock. The first read
    // avoids empty writes while idle; it does not reserve a job.
    let row = db
        .query_one(Statement::from_sql_and_values(
            DbBackend::Sqlite,
            format!(
                "UPDATE scan_jobs SET state=?4, attempt=attempt+1
                WHERE id=({NEXT_JOB})
                RETURNING id, symbol, bar_end, revision, attempt, queued_at"
            ),
            args.into_iter().chain([running.into()]),
        ))
        .await?;
    row.map(|r| {
        Ok(Job {
            id: r.try_get("", "id")?,
            symbol: r.try_get("", "symbol")?,
            bar_end: r.try_get("", "bar_end")?,
            revision: r.try_get("", "revision")?,
            attempt: r.try_get("", "attempt")?,
            queued_at: r.try_get("", "queued_at")?,
        })
    })
    .transpose()
}

pub async fn finish(db: &DatabaseConnection, job: &Job, next: &Next) -> Result<()> {
    let (state, available_at) = match next {
        Next::Done => ("done", 0),
        Next::Skipped => ("skipped", 0),
        Next::ScanAt(at) => ("pending", *at),
        Next::RepairAt(at) => ("repair", *at),
    };
    db.execute(Statement::from_sql_and_values(
        DbBackend::Sqlite,
        "UPDATE scan_jobs SET
            state=CASE WHEN revision=?3 THEN ?4 ELSE 'pending' END,
            available_at=CASE WHEN revision=?3 THEN ?5 ELSE 0 END
         WHERE id=?1 AND attempt=?2 AND state IN ('running','repairing')",
        [
            job.id.into(),
            job.attempt.into(),
            job.revision.into(),
            state.into(),
            available_at.into(),
        ],
    ))
    .await?;
    Ok(())
}

pub async fn remaining_for_bar(db: &DatabaseConnection, bar_end: &str) -> Result<i64> {
    let row = db
        .query_one(Statement::from_sql_and_values(
            DbBackend::Sqlite,
            "SELECT COUNT(*) AS remaining FROM scan_jobs
             WHERE logic_version=? AND bar_end=? AND state NOT IN ('done','skipped')",
            [EVENT_LOGIC_VERSION.into(), bar_end.into()],
        ))
        .await?
        .expect("COUNT(*) returns one row");
    Ok(row.try_get("", "remaining")?)
}

pub async fn prune_completed(db: &DatabaseConnection, before_bar: &str) -> Result<()> {
    db.execute(Statement::from_sql_and_values(
        DbBackend::Sqlite,
        "DELETE FROM scan_jobs WHERE bar_end < ? AND state IN ('done','skipped')",
        [before_bar.into()],
    ))
    .await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    const END: &str = "2026-11-20 14:45:00";

    #[tokio::test]
    async fn event_and_timer_merge_and_recover_an_interrupted_claim() {
        let path =
            std::env::temp_dir().join(format!("ntrend-scan-queue-{}.sqlite", uuid::Uuid::new_v4()));
        let db = crate::storage::connect(&path).await.unwrap();
        assert!(enqueue(&db, "EG0", END, 100, true).await.unwrap());
        assert!(enqueue_scheduled(&db, &["EG0", "MA0", "FG0"], END, 140)
            .await
            .unwrap());
        assert!(!enqueue(&db, "EG0", END, 141, true).await.unwrap());
        assert_eq!(remaining_for_bar(&db, END).await.unwrap(), 3);
        let first = claim(&db, Work::Scan, 141).await.unwrap().unwrap();
        assert_eq!(first.symbol, "EG0");
        db.close().await.unwrap();
        let db = crate::storage::connect(&path).await.unwrap();
        recover(&db).await.unwrap();
        let resumed = claim(&db, Work::Scan, 142).await.unwrap().unwrap();
        assert_eq!(first.id, resumed.id);
        assert_eq!(resumed.attempt, first.attempt + 1);
        finish(&db, &first, &Next::Done).await.unwrap();
        assert_eq!(remaining_for_bar(&db, END).await.unwrap(), 3);
        finish(&db, &resumed, &Next::Done).await.unwrap();
        assert!(!enqueue(&db, "EG0", END, 143, true).await.unwrap());
        assert_eq!(remaining_for_bar(&db, END).await.unwrap(), 2);
        db.close().await.unwrap();
        let _ = std::fs::remove_file(path);
    }

    #[tokio::test]
    async fn delayed_repair_yields_to_another_symbol_and_resumes_after_restart() {
        let db = crate::storage::connect(std::path::Path::new(":memory:"))
            .await
            .unwrap();
        enqueue_scheduled(&db, &["MA0", "FG0"], END, 100)
            .await
            .unwrap();
        let missing = claim(&db, Work::Scan, 100).await.unwrap().unwrap();
        assert_eq!(missing.symbol, "MA0");
        finish(&db, &missing, &Next::RepairAt(110)).await.unwrap();
        assert!(claim(&db, Work::Repair, 109).await.unwrap().is_none());
        let repair = claim(&db, Work::Repair, 110).await.unwrap().unwrap();
        let ready = claim(&db, Work::Scan, 110).await.unwrap().unwrap();
        assert_eq!(ready.symbol, "FG0");
        finish(&db, &ready, &Next::Done).await.unwrap();
        recover(&db).await.unwrap();
        let resumed = claim(&db, Work::Repair, 111).await.unwrap().unwrap();
        assert_eq!(resumed.id, repair.id);
        finish(&db, &resumed, &Next::RepairAt(115)).await.unwrap();
        assert!(claim(&db, Work::Repair, 112).await.unwrap().is_none());
        assert!(enqueue(&db, "MA0", END, 112, true).await.unwrap());
        assert_eq!(
            claim(&db, Work::Scan, 112).await.unwrap().unwrap().symbol,
            "MA0"
        );
    }

    #[tokio::test]
    async fn revision_changed_during_scan_is_requeued_on_acknowledgement() {
        let db = crate::storage::connect(std::path::Path::new(":memory:"))
            .await
            .unwrap();
        enqueue(&db, "FG0", END, 100, true).await.unwrap();
        let old = claim(&db, Work::Scan, 100).await.unwrap().unwrap();
        db.execute_unprepared("INSERT INTO market_history_revisions VALUES ('FG0', 1)")
            .await
            .unwrap();
        assert!(enqueue_scheduled(&db, &["FG0"], END, 101).await.unwrap());
        assert!(claim(&db, Work::Scan, 101).await.unwrap().is_none());
        finish(&db, &old, &Next::Done).await.unwrap();
        let revised = claim(&db, Work::Scan, 101).await.unwrap().unwrap();
        assert_eq!(revised.revision, 1);
        finish(&db, &revised, &Next::Done).await.unwrap();
        assert!(!enqueue_scheduled(&db, &["FG0"], END, 102).await.unwrap());
        assert_eq!(remaining_for_bar(&db, END).await.unwrap(), 0);
    }

    #[tokio::test]
    async fn concurrent_claims_use_distinct_symbols_and_preserve_ready_bar_order() {
        let path = std::env::temp_dir().join(format!(
            "ntrend-scan-workers-{}.sqlite",
            uuid::Uuid::new_v4()
        ));
        let db = crate::storage::connect(&path).await.unwrap();
        let later = "2026-11-20 15:00:00";
        enqueue(&db, "MA0", END, 100, true).await.unwrap();
        enqueue(&db, "MA0", later, 101, true).await.unwrap();
        enqueue(&db, "FG0", END, 102, true).await.unwrap();
        let (first, second) =
            tokio::join!(claim(&db, Work::Scan, 110), claim(&db, Work::Scan, 110));
        let mut jobs = [first.unwrap().unwrap(), second.unwrap().unwrap()];
        jobs.sort_unstable_by(|a, b| a.symbol.cmp(&b.symbol));
        let [fg, ma] = jobs;
        assert_eq!((&*fg.symbol, &*fg.bar_end), ("FG0", END));
        assert_eq!((&*ma.symbol, &*ma.bar_end), ("MA0", END));
        assert!(claim(&db, Work::Scan, 110).await.unwrap().is_none());

        finish(&db, &ma, &Next::RepairAt(110)).await.unwrap();
        assert!(claim(&db, Work::Scan, 110).await.unwrap().is_none());
        let repair = claim(&db, Work::Repair, 110).await.unwrap().unwrap();
        assert_eq!((&*repair.symbol, &*repair.bar_end), ("MA0", END));
        assert!(claim(&db, Work::Scan, 110).await.unwrap().is_none());
        finish(&db, &repair, &Next::Done).await.unwrap();
        let next = claim(&db, Work::Scan, 110).await.unwrap().unwrap();
        assert_eq!((&*next.symbol, &*next.bar_end), ("MA0", later));
        finish(&db, &fg, &Next::Done).await.unwrap();
        finish(&db, &next, &Next::Done).await.unwrap();
        db.close().await.unwrap();
        let _ = std::fs::remove_file(path);
    }
}
