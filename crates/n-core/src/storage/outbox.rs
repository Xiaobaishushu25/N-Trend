//! R4a durable signal delivery. SQLite triggers capture events in the SAME
//! transaction as pattern_events inserts/updates. No in-memory-only scan result
//! can be the sole source of a trading notification.
use anyhow::{Context, Result};
use sea_orm::{ConnectionTrait, DatabaseConnection, DbBackend, Statement, TransactionTrait};

fn sql(query: impl Into<String>, values: Vec<sea_orm::Value>) -> Statement {
    Statement::from_sql_and_values(DbBackend::Sqlite, query, values)
}

/// Set to true only for an explicit compile-time rollback after draining Outbox.
pub const LEGACY_DIRECT_PUBLISH: bool = false;

pub const NOTICE_WARNING: &str = "notice_warning";
pub const NOTICE_TRIGGER: &str = "notice_trigger";
pub const EMAIL_WARNING: &str = "email_warning";
pub const EMAIL_TRIGGER: &str = "email_trigger";
pub const REGISTER_WARNING: &str = "register_warning";

#[derive(Debug, Clone)]
pub struct Delivery {
    pub event_id: i64,
    pub action: String,
    pub attempts: i64,
}

/// Existing historical events are deliberately NOT backfilled on migration.
/// A newly observed event is eligible when its warning was within the last
/// twelve hours; older historical rebuilds are not retroactively announced.
/// `datetime` operates on both event-local timestamps, avoiding UTC offset drift.
pub async fn ensure(db: &DatabaseConnection) -> Result<()> {
    db.execute_unprepared(
        r#"
        CREATE TABLE IF NOT EXISTS signal_delivery_outbox (
            event_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            retry_at INTEGER NOT NULL DEFAULT 0,
            lease_until INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at INTEGER NOT NULL DEFAULT (unixepoch()),
            updated_at INTEGER NOT NULL DEFAULT (unixepoch()),
            PRIMARY KEY(event_id, action)
        );
        CREATE INDEX IF NOT EXISTS idx_signal_delivery_due
          ON signal_delivery_outbox(state, retry_at, created_at);
        CREATE TABLE IF NOT EXISTS signal_delivery_receipts (
            event_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            notification_id INTEGER NOT NULL,
            PRIMARY KEY(event_id, kind),
            UNIQUE(notification_id)
        );
        CREATE TRIGGER IF NOT EXISTS r4a_outbox_warning_insert
        AFTER INSERT ON pattern_events
        WHEN NEW.warning_kind <> 'fast'
          AND NEW.warning_ts >= datetime(NEW.created_at, '-12 hours')
          AND COALESCE((SELECT value FROM settings WHERE key='signal_origin'), '') <> 'historical_rebuild'
        BEGIN
          INSERT OR IGNORE INTO signal_delivery_outbox(event_id,action)
            VALUES(NEW.id,'notice_warning');
          INSERT OR IGNORE INTO signal_delivery_outbox(event_id,action)
            VALUES(NEW.id,'email_warning');
          INSERT OR IGNORE INTO signal_delivery_outbox(event_id,action)
            VALUES(NEW.id,'register_warning');
        END;
        CREATE TRIGGER IF NOT EXISTS r4a_outbox_trigger_update
        AFTER UPDATE OF trigger_ts ON pattern_events
        WHEN OLD.trigger_ts IS NULL AND NEW.trigger_ts IS NOT NULL
          AND NEW.warning_kind <> 'fast'
          AND NEW.warning_ts >= datetime(NEW.updated_at, '-12 hours')
          AND COALESCE((SELECT value FROM settings WHERE key='signal_origin'), '') <> 'historical_rebuild'
        BEGIN
          INSERT OR IGNORE INTO signal_delivery_outbox(event_id,action)
            VALUES(NEW.id,'notice_trigger');
          INSERT OR IGNORE INTO signal_delivery_outbox(event_id,action)
            VALUES(NEW.id,'email_trigger');
        END;
        "#,
    )
    .await
    .context("创建 R4a Outbox 和原子捕获触发器失败")?;
    Ok(())
}

/// Single-worker use is normal. The conditional claim and lease also make a
/// second worker/restart safe: an abandoned claim is retried after 60 seconds.
pub async fn claim_next(db: &DatabaseConnection, emails: bool) -> Result<Option<Delivery>> {
    let now = chrono::Utc::now().timestamp();
    let row = db
        .query_one(sql(
            "SELECT event_id,action,attempts FROM signal_delivery_outbox \
         WHERE ((state='pending' AND retry_at<=?) \
            OR (state='processing' AND lease_until<=?)) \
           AND (action IN ('email_warning','email_trigger'))=? \
         ORDER BY created_at,event_id,action LIMIT 1",
            vec![now.into(), now.into(), (emails as i64).into()],
        ))
        .await?;
    let Some(row) = row else {
        return Ok(None);
    };
    let event_id: i64 = row.try_get("", "event_id")?;
    let action: String = row.try_get("", "action")?;
    let attempts: i64 = row.try_get("", "attempts")?;
    let res = db
        .execute(sql(
            "UPDATE signal_delivery_outbox SET state='processing',attempts=attempts+1, \
         lease_until=?,updated_at=? WHERE event_id=? AND action=? AND \
         ((state='pending' AND retry_at<=?) OR (state='processing' AND lease_until<=?))",
            vec![
                (now + 60).into(),
                now.into(),
                event_id.into(),
                action.clone().into(),
                now.into(),
                now.into(),
            ],
        ))
        .await?;
    if res.rows_affected() == 0 {
        return Ok(None);
    }
    Ok(Some(Delivery {
        event_id,
        action,
        attempts: attempts + 1,
    }))
}

pub async fn complete(db: &DatabaseConnection, task: &Delivery, state: &str) -> Result<()> {
    db.execute(sql(
        "UPDATE signal_delivery_outbox SET state=?,lease_until=0,updated_at=unixepoch(), \
         last_error=NULL WHERE event_id=? AND action=? AND state='processing'",
        vec![
            state.into(),
            task.event_id.into(),
            task.action.clone().into(),
        ],
    ))
    .await?;
    Ok(())
}

pub async fn retry(db: &DatabaseConnection, task: &Delivery, message: &str) -> Result<()> {
    let exponent = task.attempts.clamp(1, 7) as u32;
    let delay = (1_i64 << exponent).min(120);
    let error: String = message.chars().take(1000).collect();
    db.execute(sql(
        "UPDATE signal_delivery_outbox SET state='pending',retry_at=unixepoch()+?, \
         lease_until=0,updated_at=unixepoch(),last_error=? \
         WHERE event_id=? AND action=? AND state='processing'",
        vec![
            delay.into(),
            error.into(),
            task.event_id.into(),
            task.action.clone().into(),
        ],
    ))
    .await?;
    Ok(())
}

/// Commit a history notification, its durable receipt and the Outbox ACK
/// in one SQLite transaction. A restart/retry cannot insert a second notice.
/// The returned Some(...) must be broadcast only AFTER this transaction commits.
pub async fn persist_notice(
    db: &DatabaseConnection,
    task: &Delivery,
    item: &n_protocol::NewNotificationHistoryItem,
) -> Result<Option<n_protocol::NotificationHistoryItem>> {
    let tx = db.begin().await?;
    if tx
        .query_one(sql(
            "SELECT notification_id FROM signal_delivery_receipts WHERE event_id=? AND kind=?",
            vec![task.event_id.into(), item.kind.clone().into()],
        ))
        .await?
        .is_some()
    {
        tx.execute(sql(
            "UPDATE signal_delivery_outbox SET state='done',lease_until=0,updated_at=unixepoch() \
             WHERE event_id=? AND action=?",
            vec![task.event_id.into(), task.action.clone().into()],
        ))
        .await?;
        tx.commit().await?;
        return Ok(None);
    }
    let now = chrono::Local::now().format("%Y-%m-%d %H:%M:%S").to_string();
    let signal_json = item
        .signal
        .as_ref()
        .map(serde_json::to_string)
        .transpose()?;
    let entry_json = item
        .entry_trigger
        .as_ref()
        .map(serde_json::to_string)
        .transpose()?;
    let single_json = item
        .single_bar
        .as_ref()
        .map(serde_json::to_string)
        .transpose()?;
    let manual_json = item
        .manual_level
        .as_ref()
        .map(serde_json::to_string)
        .transpose()?;
    let inserted = tx
        .execute(sql(
            "INSERT INTO notification_history \
         (created_at,kind,title,content,signal_json,entry_trigger_json, \
          single_bar_json,manual_level_json,read_at) VALUES(?,?,?,?,?,?,?,?,NULL)",
            vec![
                now.clone().into(),
                item.kind.clone().into(),
                item.title.clone().into(),
                item.content.clone().into(),
                signal_json.into(),
                entry_json.into(),
                single_json.into(),
                manual_json.into(),
            ],
        ))
        .await?;
    let notice_id = inserted.last_insert_id() as i64;
    tx.execute(sql(
        "INSERT INTO signal_delivery_receipts(event_id,kind,notification_id) VALUES(?,?,?)",
        vec![
            task.event_id.into(),
            item.kind.clone().into(),
            notice_id.into(),
        ],
    ))
    .await?;
    tx.execute(sql(
        "UPDATE signal_delivery_outbox SET state='done',lease_until=0,updated_at=unixepoch() \
         WHERE event_id=? AND action=?",
        vec![task.event_id.into(), task.action.clone().into()],
    ))
    .await?;
    tx.execute(sql(
        "UPDATE sync_revisions SET revision=revision+1,updated_at=datetime('now','localtime') \
         WHERE scope='notifications'",
        vec![],
    ))
    .await?;
    tx.commit().await?;
    Ok(Some(n_protocol::NotificationHistoryItem {
        id: notice_id as u64,
        created_at: now,
        kind: item.kind.clone(),
        title: item.title.clone(),
        content: item.content.clone(),
        signal: item.signal.clone(),
        entry_trigger: item.entry_trigger.clone(),
        single_bar: item.single_bar.clone(),
        manual_level: item.manual_level.clone(),
        read_at: None,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use sea_orm::ConnectionTrait;
    #[tokio::test]
    async fn outbox_captures_recent_warning_and_trigger_atomically() {
        let db = crate::storage::connect(std::path::Path::new(":memory:"))
            .await
            .unwrap();
        db.execute_unprepared(
            "INSERT INTO pattern_events(symbol,direction,grade,level,s0_ts,s0_price,s1_ts,s1_price,\
             s2_ts,s2_price,a_move,b_move,a_bars,b_bars,retracement,warning_ts,detected_at,\
             warning_kind,entry_score,entry_score_dims,entry,stop,target,risk,rr,state,\
             hold_score_history,created_at,updated_at) VALUES\
             ('RB0','up','A级','fine','2026-11-04 09:00:00',1,'2026-11-04 09:15:00',2,\
              '2026-11-04 09:30:00',1.5,1,0.5,3,2,0.5,'2026-11-04 09:45:00',\
              '2026-11-04 09:45:00','wick',4,'{}',3,1,5,2,1,'pending','[]',\
              '2026-11-04 09:46:00','2026-11-04 09:46:00')"
        ).await.unwrap();
        // This fixture has a recent created_at relative to warning_ts and does
        // not rely on wall-clock time, so it remains deterministic.
        let count = db
            .query_one(sql(
                "SELECT COUNT(*) AS n FROM signal_delivery_outbox",
                vec![],
            ))
            .await
            .unwrap()
            .unwrap()
            .try_get::<i64>("", "n")
            .unwrap();
        assert_eq!(count, 3);
        db.execute_unprepared(
            "UPDATE pattern_events SET trigger_ts='2026-11-04 09:46:00',\
             updated_at='2026-11-04 09:46:00' WHERE symbol='RB0'",
        )
        .await
        .unwrap();
        let count = db
            .query_one(sql(
                "SELECT COUNT(*) AS n FROM signal_delivery_outbox",
                vec![],
            ))
            .await
            .unwrap()
            .unwrap()
            .try_get::<i64>("", "n")
            .unwrap();
        assert_eq!(count, 5);
    }

    #[tokio::test]
    async fn history_notice_and_receipt_commit_together_once() {
        let db = crate::storage::connect(std::path::Path::new(":memory:"))
            .await
            .unwrap();
        let task = Delivery {
            event_id: 123,
            action: NOTICE_WARNING.into(),
            attempts: 1,
        };
        let item = n_protocol::NewNotificationHistoryItem {
            kind: "warning".into(),
            title: Some("R4a test".into()),
            content: "once".into(),
            signal: None,
            entry_trigger: None,
            single_bar: None,
            manual_level: None,
        };
        db.execute_unprepared(
            "CREATE TRIGGER deny_r4a_receipt BEFORE INSERT ON signal_delivery_receipts \
             BEGIN SELECT RAISE(ABORT, 'injected receipt failure'); END",
        )
        .await
        .unwrap();
        assert!(persist_notice(&db, &task, &item).await.is_err());
        let count = db
            .query_one(sql(
                "SELECT COUNT(*) AS n FROM notification_history",
                vec![],
            ))
            .await
            .unwrap()
            .unwrap()
            .try_get::<i64>("", "n")
            .unwrap();
        assert_eq!(count, 0, "failed receipt must roll back the notice insert");
        db.execute_unprepared("DROP TRIGGER deny_r4a_receipt")
            .await
            .unwrap();
        let first = persist_notice(&db, &task, &item).await.unwrap();
        assert!(first.is_some());
        assert!(persist_notice(&db, &task, &item).await.unwrap().is_none());
        let count = db
            .query_one(sql(
                "SELECT COUNT(*) AS n FROM notification_history",
                vec![],
            ))
            .await
            .unwrap()
            .unwrap()
            .try_get::<i64>("", "n")
            .unwrap();
        assert_eq!(count, 1);
    }
}
