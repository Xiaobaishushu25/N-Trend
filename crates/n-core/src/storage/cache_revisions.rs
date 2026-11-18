//! Durable invalidation for R5a quote-side memory snapshots.
//!
//! These triggers run in the same SQLite transaction as the modified row.
//! Local repository mutations additionally bump atomic epochs, so the quote
//! worker does not have to query revisions every time a price arrives.
use anyhow::{Context, Result};
use sea_orm::{ConnectionTrait, DatabaseConnection};

pub async fn ensure(db: &DatabaseConnection) -> Result<()> {
    db.execute_unprepared(
        r#"
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
    "#,
    )
    .await
    .context("创建 R5a 同事务信号及品种修订触发器失败")?;
    ensure_history_revisions(db).await?;
    Ok(())
}

async fn ensure_history_revisions(db: &DatabaseConnection) -> Result<()> {
    db.execute_unprepared(
        r#"CREATE TABLE IF NOT EXISTS market_history_revisions (
            symbol TEXT PRIMARY KEY NOT NULL, revision INTEGER NOT NULL
        ) WITHOUT ROWID;"#,
    )
    .await?;
    // The revision is only an invalidation token. Every committed change to
    // prices or rollovers must invalidate even if the latest timestamp is unchanged.
    for (table, changed) in [
        (
            "klines",
            "OLD.timeframe IS NOT NEW.timeframe OR OLD.ts IS NOT NEW.ts OR
             OLD.open IS NOT NEW.open OR OLD.high IS NOT NEW.high OR
             OLD.low IS NOT NEW.low OR OLD.close IS NOT NEW.close OR
             OLD.volume IS NOT NEW.volume OR OLD.hold IS NOT NEW.hold OR
             OLD.source IS NOT NEW.source",
        ),
        (
            "rollovers",
            "OLD.ts IS NOT NEW.ts OR OLD.from_contract IS NOT NEW.from_contract OR
             OLD.to_contract IS NOT NEW.to_contract OR OLD.confirmed IS NOT NEW.confirmed",
        ),
    ] {
        let bump = |row| {
            format!(
                "INSERT INTO market_history_revisions (symbol, revision) VALUES ({row}.symbol, 1)
                 ON CONFLICT(symbol) DO UPDATE SET revision = revision + 1;"
            )
        };
        db.execute_unprepared(&format!(
            "CREATE TRIGGER IF NOT EXISTS market_history_{table}_insert AFTER INSERT ON {table}
             BEGIN {new} END;
             CREATE TRIGGER IF NOT EXISTS market_history_{table}_delete AFTER DELETE ON {table}
             BEGIN {old} END;
             CREATE TRIGGER IF NOT EXISTS market_history_{table}_update AFTER UPDATE ON {table}
             WHEN OLD.symbol IS NOT NEW.symbol OR ({changed})
             BEGIN
                 {new}
                 INSERT INTO market_history_revisions (symbol, revision)
                     SELECT OLD.symbol, 1 WHERE OLD.symbol IS NOT NEW.symbol
                     ON CONFLICT(symbol) DO UPDATE SET revision = revision + 1;
             END;",
            new = bump("NEW"),
            old = bump("OLD"),
        ))
        .await
        .context("创建行情历史版本触发器失败")?;
    }
    Ok(())
}

pub async fn market_history_revision(db: &DatabaseConnection, symbol: &str) -> Result<i64> {
    use sea_orm::{DbBackend, Statement};
    let row = db
        .query_one(Statement::from_sql_and_values(
            DbBackend::Sqlite,
            "SELECT revision FROM market_history_revisions WHERE symbol = ?",
            [symbol.into()],
        ))
        .await?;
    row.map(|row| row.try_get("", "revision"))
        .transpose()
        .context("读取行情历史版本失败")
        .map(|revision| revision.unwrap_or(0))
}

#[cfg(test)]
mod tests {
    use crate::storage::repo;
    use sea_orm::ConnectionTrait;

    #[tokio::test]
    async fn r5a_pending_revision_tracks_insert_update_delete() {
        let db = crate::storage::connect(std::path::Path::new(":memory:"))
            .await
            .unwrap();
        let revision = repo::get_revision(&db, "signals").await.unwrap();
        db.execute_unprepared(
            r#"INSERT INTO pattern_events(
            symbol,direction,grade,level,s0_ts,s0_price,s1_ts,s1_price,
            s2_ts,s2_price,warning_ts,detected_at,warning_kind,entry_score,
            entry,stop,target,risk,rr,state,created_at,updated_at
        ) VALUES (
            'MA0','up','A','fine','2026-08-11 09:15:00',1,
            '2026-08-11 09:30:00',2,'2026-08-11 09:45:00',1,
            '2026-08-11 10:00:00','2026-08-11 10:00:00','wick',4,
            2,1,4,1,2,'pending','2026-08-11 10:00:00','2026-08-11 10:00:00'
        )"#,
        )
        .await
        .unwrap();
        assert_eq!(
            repo::get_revision(&db, "signals").await.unwrap(),
            revision + 1
        );
        db.execute_unprepared("UPDATE pattern_events SET state='triggered' WHERE symbol='MA0'")
            .await
            .unwrap();
        assert_eq!(
            repo::get_revision(&db, "signals").await.unwrap(),
            revision + 2
        );
        db.execute_unprepared("DELETE FROM pattern_events WHERE symbol='MA0'")
            .await
            .unwrap();
        // The delete did not change the pending set; no extra invalidation needed.
        assert_eq!(
            repo::get_revision(&db, "signals").await.unwrap(),
            revision + 2
        );
    }
}
