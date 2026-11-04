//! Durable invalidation for R5a quote-side memory snapshots.
//!
//! These triggers run in the same SQLite transaction as the modified row.
//! Local repository mutations additionally bump atomic epochs, so the quote
//! worker does not have to query revisions every time a price arrives.
use anyhow::{Context, Result};
use sea_orm::{ConnectionTrait, DatabaseConnection};

pub async fn ensure(db: &DatabaseConnection) -> Result<()> {
    db.execute_unprepared(r#"
        CREATE TRIGGER IF NOT EXISTS r5a_pending_insert
        AFTER INSERT ON pattern_events
        WHEN NEW.state = 'pending' AND NEW.trigger_ts IS NULL
        BEGIN
            UPDATE sync_revisions SET revision = revision + 1,
                updated_at = datetime('now','localtime') WHERE scope = 'signals';
        END;

        CREATE TRIGGER IF NOT EXISTS r5a_pending_update
        AFTER UPDATE ON pattern_events
        WHEN (OLD.state = 'pending' AND OLD.trigger_ts IS NULL)
          OR (NEW.state = 'pending' AND NEW.trigger_ts IS NULL)
        BEGIN
            UPDATE sync_revisions SET revision = revision + 1,
                updated_at = datetime('now','localtime') WHERE scope = 'signals';
        END;

        CREATE TRIGGER IF NOT EXISTS r5a_pending_delete
        AFTER DELETE ON pattern_events
        WHEN OLD.state = 'pending' AND OLD.trigger_ts IS NULL
        BEGIN
            UPDATE sync_revisions SET revision = revision + 1,
                updated_at = datetime('now','localtime') WHERE scope = 'signals';
        END;

        CREATE TRIGGER IF NOT EXISTS r5a_symbols_insert
        AFTER INSERT ON symbols
        BEGIN
            UPDATE sync_revisions SET revision = revision + 1,
                updated_at = datetime('now','localtime') WHERE scope = 'symbols';
        END;
        CREATE TRIGGER IF NOT EXISTS r5a_symbols_update
        AFTER UPDATE ON symbols
        BEGIN
            UPDATE sync_revisions SET revision = revision + 1,
                updated_at = datetime('now','localtime') WHERE scope = 'symbols';
        END;
        CREATE TRIGGER IF NOT EXISTS r5a_symbols_delete
        AFTER DELETE ON symbols
        BEGIN
            UPDATE sync_revisions SET revision = revision + 1,
                updated_at = datetime('now','localtime') WHERE scope = 'symbols';
        END;
    "#).await.context("创建 R5a 同事务信号及品种修订触发器失败")?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::storage::repo;
    use sea_orm::ConnectionTrait;

    #[tokio::test]
    async fn r5a_pending_revision_tracks_insert_update_delete() {
        let db = crate::storage::connect(std::path::Path::new(":memory:")).await.unwrap();
        let revision = repo::get_revision(&db, "signals").await.unwrap();
        db.execute_unprepared(r#"INSERT INTO pattern_events(
            symbol,direction,grade,level,s0_ts,s0_price,s1_ts,s1_price,
            s2_ts,s2_price,warning_ts,detected_at,warning_kind,entry_score,
            entry,stop,target,risk,rr,state,created_at,updated_at
        ) VALUES (
            'MA0','up','A','fine','2026-08-11 09:15:00',1,
            '2026-08-11 09:30:00',2,'2026-08-11 09:45:00',1,
            '2026-08-11 10:00:00','2026-08-11 10:00:00','wick',4,
            2,1,4,1,2,'pending','2026-08-11 10:00:00','2026-08-11 10:00:00'
        )"#).await.unwrap();
        assert_eq!(repo::get_revision(&db, "signals").await.unwrap(), revision + 1);
        db.execute_unprepared("UPDATE pattern_events SET state='triggered' WHERE symbol='MA0'")
            .await.unwrap();
        assert_eq!(repo::get_revision(&db, "signals").await.unwrap(), revision + 2);
        db.execute_unprepared("DELETE FROM pattern_events WHERE symbol='MA0'")
            .await.unwrap();
        // The delete did not change the pending set; no extra invalidation needed.
        assert_eq!(repo::get_revision(&db, "signals").await.unwrap(), revision + 2);
    }
}
