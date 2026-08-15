//! SQLite connection and schema migration.

use std::path::{Path, PathBuf};

use anyhow::{anyhow, Context, Result};
use sea_orm::{
    ConnectionTrait, Database as SeaDatabase, DatabaseConnection, DbBackend, Schema, Statement,
};
use tracing::info;

pub mod entities;
pub mod repo;

pub async fn connect(path: &Path) -> Result<DatabaseConnection> {
    let url = if path.to_string_lossy() == ":memory:" {
        "sqlite::memory:".to_string()
    } else {
        let path_str = path.to_string_lossy().replace('\\', "/");
        format!("sqlite://{path_str}?mode=rwc")
    };
    let db = SeaDatabase::connect(&url)
        .await
        .map_err(|e| anyhow!("连接 SQLite 失败 ({}): {e}", url))?;
    migrate_with_path(&db, Some(path)).await?;
    Ok(db)
}

pub async fn migrate(db: &DatabaseConnection) -> Result<()> {
    migrate_with_path(db, None).await
}

pub async fn migrate_with_path(db: &DatabaseConnection, path: Option<&Path>) -> Result<()> {
    let schema = Schema::new(DbBackend::Sqlite);
    let tables = [
        schema
            .create_table_from_entity(entities::symbols::Entity)
            .if_not_exists()
            .to_owned(),
        schema
            .create_table_from_entity(entities::klines::Entity)
            .if_not_exists()
            .to_owned(),
        schema
            .create_table_from_entity(entities::settings::Entity)
            .if_not_exists()
            .to_owned(),
        schema
            .create_table_from_entity(entities::groups::Entity)
            .if_not_exists()
            .to_owned(),
        schema
            .create_table_from_entity(entities::symbol_groups::Entity)
            .if_not_exists()
            .to_owned(),
        schema
            .create_table_from_entity(entities::rollovers::Entity)
            .if_not_exists()
            .to_owned(),
    ];
    let backend = db.get_database_backend();
    for table in tables {
        let stmt = backend.build(&table);
        db.execute(stmt).await.context("创建数据表失败")?;
    }
    ensure_column(db, "symbols", "sort_index", "BIGINT NOT NULL DEFAULT 0").await?;
    ensure_column(db, "symbols", "tick_size", "REAL NOT NULL DEFAULT 0.0").await?;

    migrate_legacy_signal_tables(db, path).await?;
    migrate_pattern_event_unique(db).await?;
    info!("数据库表结构已就绪");
    Ok(())
}

/// 老库清理：将 scans/signals/signal_outcomes 备份后删除，并创建新的 pattern_events 表。
/// 幂等：已写入 schema_migrated=2 标记后不再重复备份/删除。
async fn migrate_legacy_signal_tables(db: &DatabaseConnection, path: Option<&Path>) -> Result<()> {
    let migrated = db
        .query_one(Statement::from_string(
            db.get_database_backend(),
            "SELECT 1 AS x FROM settings WHERE key = 'schema_migrated' AND value IN ('2', '3')",
        ))
        .await
        .ok()
        .flatten()
        .is_some();
    if migrated {
        create_pattern_events(db).await?;
        return Ok(());
    }

    let legacy = db
        .query_one(Statement::from_string(
            db.get_database_backend(),
            "SELECT name FROM sqlite_master WHERE type='table' AND name='signals'",
        ))
        .await
        .ok()
        .flatten()
        .is_some();
    if !legacy {
        create_pattern_events(db).await?;
        mark_migrated(db).await?;
        return Ok(());
    }

    backup_database(db, path).await?;
    for table in ["signal_outcomes", "signals", "scans"] {
        db.execute_unprepared(&format!("DROP TABLE IF EXISTS {table}"))
            .await
            .with_context(|| format!("删除旧表 {table} 失败"))?;
    }
    create_pattern_events(db).await?;
    mark_migrated(db).await?;
    info!("旧信号表已备份并清理，pattern_events 已创建");
    Ok(())
}

async fn create_pattern_events(db: &DatabaseConnection) -> Result<()> {
    db.execute_unprepared(
        r#"
        CREATE TABLE IF NOT EXISTS pattern_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            grade TEXT NOT NULL,
            level TEXT NOT NULL DEFAULT '',
            s0_ts TEXT NOT NULL,
            s0_price REAL NOT NULL,
            s1_ts TEXT NOT NULL,
            s1_price REAL NOT NULL,
            s2_ts TEXT NOT NULL,
            s2_price REAL NOT NULL,
            a_move REAL NOT NULL DEFAULT 0,
            b_move REAL NOT NULL DEFAULT 0,
            a_bars INTEGER NOT NULL DEFAULT 0,
            b_bars INTEGER NOT NULL DEFAULT 0,
            retracement REAL NOT NULL DEFAULT 0,
            warning_ts TEXT NOT NULL,
            detected_at TEXT NOT NULL,
            warning_kind TEXT NOT NULL,
            entry_score REAL NOT NULL,
            entry_score_dims TEXT NOT NULL DEFAULT '{}',
            entry REAL NOT NULL,
            stop REAL NOT NULL,
            target REAL NOT NULL,
            risk REAL NOT NULL,
            rr REAL NOT NULL,
            state TEXT NOT NULL,
            last_advance_ts TEXT,
            trigger_ts TEXT,
            trigger_bar_ts TEXT,
            trigger_price REAL,
            trigger_score REAL,
            trigger_volume_ratio REAL,
            overshoot_r REAL,
            hold_score REAL,
            hold_score_history TEXT NOT NULL DEFAULT '[]',
            outcome TEXT,
            exit_reason TEXT,
            exit_ts TEXT,
            exit_price REAL,
            r_multiple REAL,
            mfe_r REAL,
            mae_r REAL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        "#,
    )
    .await
    .context("创建 pattern_events 表失败")?;
    for (name, cols) in [
        (
            "idx_pattern_events_symbol_state",
            "(symbol, direction, state)",
        ),
        ("idx_pattern_events_warning_ts", "(warning_ts)"),
        ("idx_pattern_events_entry_score", "(entry_score)"),
    ] {
        db.execute_unprepared(&format!(
            "CREATE INDEX IF NOT EXISTS {name} ON pattern_events {cols}"
        ))
        .await
        .with_context(|| format!("创建索引 {name} 失败"))?;
    }
    ensure_column(db, "pattern_events", "level", "TEXT NOT NULL DEFAULT ''").await?;
    ensure_column(db, "pattern_events", "last_advance_ts", "TEXT").await?;
    for (column, ddl) in [
        ("a_move", "REAL NOT NULL DEFAULT 0"),
        ("b_move", "REAL NOT NULL DEFAULT 0"),
        ("a_bars", "INTEGER NOT NULL DEFAULT 0"),
        ("b_bars", "INTEGER NOT NULL DEFAULT 0"),
        ("retracement", "REAL NOT NULL DEFAULT 0"),
    ] {
        ensure_column(db, "pattern_events", column, ddl).await?;
    }
    Ok(())
}

/// 清理 pattern_events 里同一根预警K线的重复记录，并加唯一索引兜底。
/// 幂等：已写入 schema_migrated=3 标记后不再重复清理。
async fn migrate_pattern_event_unique(db: &DatabaseConnection) -> Result<()> {
    let migrated = db
        .query_one(Statement::from_string(
            db.get_database_backend(),
            "SELECT 1 AS x FROM settings WHERE key = 'schema_migrated' AND value = '3'",
        ))
        .await
        .ok()
        .flatten()
        .is_some();
    if migrated {
        return Ok(());
    }

    db.execute_unprepared(
        "DELETE FROM pattern_events WHERE id NOT IN \
         (SELECT MIN(id) FROM pattern_events GROUP BY symbol, direction, warning_ts)",
    )
    .await
    .context("清理重复信号事件失败")?;
    db.execute_unprepared(
        "CREATE UNIQUE INDEX IF NOT EXISTS uniq_pattern_events_symbol_direction_warning_ts \
         ON pattern_events (symbol, direction, warning_ts)",
    )
    .await
    .context("创建信号事件唯一索引失败")?;
    db.execute_unprepared(
        "INSERT INTO settings(key, value) VALUES('schema_migrated', '3') \
         ON CONFLICT(key) DO UPDATE SET value = excluded.value",
    )
    .await
    .context("写入 schema_migrated=3 标记失败")?;
    info!("pattern_events 重复记录已清理并加唯一索引");
    Ok(())
}

async fn mark_migrated(db: &DatabaseConnection) -> Result<()> {
    db.execute_unprepared(
        "INSERT INTO settings(key, value) VALUES('schema_migrated', '2') \
         ON CONFLICT(key) DO UPDATE SET value = excluded.value",
    )
    .await
    .context("写入 schema_migrated 标记失败")?;
    Ok(())
}

/// 迁移前备份数据库文件。内存库跳过；优先用 VACUUM INTO 做一致性快照，
/// 失败时退回普通文件复制。
async fn backup_database(db: &DatabaseConnection, path: Option<&Path>) -> Result<()> {
    let Some(path) = path else {
        return Ok(());
    };
    if path.to_string_lossy() == ":memory:" {
        return Ok(());
    }
    let source = PathBuf::from(path);
    let backup = source.with_file_name(format!(
        "{}.legacy-backup.db",
        source
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or("ntrend")
    ));
    let backup_quoted = backup.to_string_lossy().replace('\\', "/");
    match db
        .execute_unprepared(&format!("VACUUM INTO '{backup_quoted}'"))
        .await
    {
        Ok(_) => info!("旧库已备份到 {}", backup.display()),
        Err(e) => {
            tracing::warn!("VACUUM INTO 备份失败({e})，尝试文件复制");
            if source.exists() {
                std::fs::copy(&source, &backup).with_context(|| {
                    format!(
                        "复制旧库备份失败: {} -> {}",
                        source.display(),
                        backup.display()
                    )
                })?;
            }
        }
    }
    Ok(())
}

/// 检查列是否存在，不存在则 ALTER 添加；symbols.sort_index 新加时按代码序回填，
/// 保证老数据升级后的默认顺序与之前的代码序一致。
async fn ensure_column(
    db: &DatabaseConnection,
    table: &str,
    column: &str,
    ddl: &str,
) -> Result<()> {
    let backend = db.get_database_backend();
    let found = db
        .query_one(Statement::from_string(
            backend,
            format!("SELECT 1 AS x FROM pragma_table_info('{table}') WHERE name = '{column}'"),
        ))
        .await
        .ok()
        .flatten()
        .is_some();
    if found {
        return Ok(());
    }
    db.execute_unprepared(&format!("ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
        .await
        .with_context(|| format!("为 {table} 添加列 {column} 失败"))?;
    if table == "symbols" && column == "sort_index" {
        db.execute_unprepared(
            "UPDATE symbols SET sort_index = (SELECT COUNT(*) FROM symbols AS s2 WHERE s2.code < symbols.code)",
        )
        .await
        .context("回填 symbols.sort_index 失败")?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn migrate_is_idempotent_and_has_pattern_events() {
        let db = SeaDatabase::connect("sqlite::memory:").await.unwrap();
        migrate(&db).await.unwrap();
        migrate(&db).await.unwrap();

        let backend = DbBackend::Sqlite;
        for column in [
            "warning_ts",
            "last_advance_ts",
            "entry_score",
            "entry_score_dims",
            "trigger_ts",
            "hold_score",
            "hold_score_history",
        ] {
            let found = db
                .query_one(Statement::from_string(
                    backend,
                    format!(
                        "SELECT 1 AS x FROM pragma_table_info('pattern_events') WHERE name = '{column}'"
                    ),
                ))
                .await
                .unwrap();
            assert!(found.is_some(), "pattern_events.{column} 应已迁移");
        }
    }

    #[tokio::test]
    async fn pattern_event_duplicates_are_deduped_and_unique_indexed() {
        let db = SeaDatabase::connect("sqlite::memory:").await.unwrap();
        create_pattern_events(&db).await.unwrap();

        db.execute_unprepared(
            "INSERT INTO pattern_events (
                symbol, direction, grade, level, s0_ts, s0_price, s1_ts, s1_price,
                s2_ts, s2_price, a_move, b_move, a_bars, b_bars, retracement,
                warning_ts, detected_at, warning_kind, entry_score, entry_score_dims,
                entry, stop, target, risk, rr, state, last_advance_ts,
                hold_score_history, created_at, updated_at
             ) VALUES (
                'BU0', 'up', 'A级', 'fine', '2026-08-14 09:15:00', 4128,
                '2026-08-14 09:30:00', 4150, '2026-08-14 09:45:00', 4137,
                22, 13, 1, 1, 0.59, '2026-08-14 11:30:00', '2026-08-14 11:30:00',
                'wick', 3.6, '{}', 4216, 4152, 4298, 64, 1.28, 'pending',
                '2026-08-14 11:30:00', '[]', '2026-08-15 16:09:10', '2026-08-15 16:09:10'
             ), (
                'BU0', 'up', 'A级', 'fine', '2026-08-14 09:15:00', 4128,
                '2026-08-14 09:30:00', 4150, '2026-08-14 09:45:00', 4137,
                22, 13, 1, 1, 0.59, '2026-08-14 11:30:00', '2026-08-14 11:30:00',
                'wick', 3.6, '{}', 4216, 4152, 4298, 64, 1.28, 'pending',
                '2026-08-14 11:30:00', '[]', '2026-08-15 16:09:10', '2026-08-15 16:09:10'
             )",
        )
        .await
        .unwrap();

        migrate(&db).await.unwrap();

        let ids = db
            .query_all(Statement::from_string(
                DbBackend::Sqlite,
                "SELECT id FROM pattern_events ORDER BY id",
            ))
            .await
            .unwrap();
        assert_eq!(ids.len(), 1, "同一预警K线应只保留一条记录");
        let kept_id = ids[0].try_get::<i64>("", "id").unwrap();
        assert_eq!(kept_id, 1, "应保留最早的一条记录");

        let unique = db
            .query_one(Statement::from_string(
                DbBackend::Sqlite,
                "SELECT name FROM sqlite_master WHERE type='index' AND name='uniq_pattern_events_symbol_direction_warning_ts'",
            ))
            .await
            .unwrap();
        assert!(unique.is_some(), "唯一索引应已创建");

        migrate(&db).await.unwrap();
        let ids_again = db
            .query_all(Statement::from_string(
                DbBackend::Sqlite,
                "SELECT id FROM pattern_events ORDER BY id",
            ))
            .await
            .unwrap();
        assert_eq!(ids_again.len(), 1, "重复清理应幂等");
    }

    #[tokio::test]
    async fn legacy_signal_tables_are_backed_up_and_dropped() {
        let dir = std::env::temp_dir().join(format!("ntrend-migrate-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let db_path = dir.join("test.db");
        let url = db_path.to_string_lossy().replace('\\', "/");
        let db = SeaDatabase::connect(&format!("sqlite://{url}?mode=rwc"))
            .await
            .unwrap();
        db.execute_unprepared(
            "CREATE TABLE scans (id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, status TEXT, scanned INTEGER, active_count INTEGER, summary TEXT)",
        )
        .await
        .unwrap();
        db.execute_unprepared(
            "CREATE TABLE signals (id INTEGER PRIMARY KEY, scan_id INTEGER, symbol TEXT, level TEXT, direction TEXT, grade TEXT, state TEXT, category TEXT, entry REAL, stop REAL, target REAL, rr REAL, score REAL, note TEXT, detail TEXT, created_at TEXT)",
        )
        .await
        .unwrap();
        db.execute_unprepared(
            "CREATE TABLE signal_outcomes (signal_id INTEGER PRIMARY KEY, sim_version INTEGER, outcome TEXT, exit_reason TEXT, updated_at TEXT)",
        )
        .await
        .unwrap();

        migrate_with_path(&db, Some(&db_path)).await.unwrap();

        for table in ["scans", "signals", "signal_outcomes"] {
            let found = db
                .query_one(Statement::from_string(
                    DbBackend::Sqlite,
                    format!("SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'"),
                ))
                .await
                .unwrap();
            assert!(found.is_none(), "{table} 应已被删除");
        }
        let events = db
            .query_one(Statement::from_string(
                DbBackend::Sqlite,
                "SELECT name FROM sqlite_master WHERE type='table' AND name='pattern_events'",
            ))
            .await
            .unwrap();
        assert!(events.is_some(), "pattern_events 应已创建");
        assert!(db_path.with_file_name("test.db.legacy-backup.db").exists());

        migrate_with_path(&db, Some(&db_path)).await.unwrap();
        migrate_with_path(&db, Some(&db_path)).await.unwrap();
    }
}
