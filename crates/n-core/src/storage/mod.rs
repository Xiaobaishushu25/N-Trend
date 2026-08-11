//! SQLite connection and schema migration.

use std::path::Path;

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
    migrate(&db).await?;
    Ok(db)
}

pub async fn migrate(db: &DatabaseConnection) -> Result<()> {
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
            .create_table_from_entity(entities::scans::Entity)
            .if_not_exists()
            .to_owned(),
        schema
            .create_table_from_entity(entities::signals::Entity)
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
            .create_table_from_entity(entities::signal_outcomes::Entity)
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
    // 老库升级：symbols 表补 sort_index 列（全部品种拖拽排序用）。
    // create_table 是 if_not_exists，不会给已存在的表加列，这里单独做幂等迁移。
    ensure_column(db, "symbols", "sort_index", "BIGINT NOT NULL DEFAULT 0").await?;
    // 品种精度列：0 表示未显式设置，扫描时用内置默认表兜底
    ensure_column(db, "symbols", "tick_size", "REAL NOT NULL DEFAULT 0.0").await?;
    // 复盘回放触发时间：快照 trigger_ts 缺失时图表用它补画触发标记
    ensure_column(db, "signal_outcomes", "entry_ts", "TEXT").await?;
    // 换月标记：信号在回放窗口内跨过连续合约换月时置 1，不计入盈亏统计
    ensure_column(db, "signal_outcomes", "rollover_crossed", "INTEGER").await?;
    // 缺口成交标记：入场价/止损价被跳空穿越时按 current.open 成交
    ensure_column(db, "signal_outcomes", "gap_crossed_entry", "INTEGER").await?;
    ensure_column(db, "signal_outcomes", "gap_crossed_exit", "INTEGER").await?;
    // ATR 分位：触发 bar 的 ATR20 在近 60 根 15m bar 中的相对位置
    ensure_column(db, "signal_outcomes", "atr_percentile", "REAL").await?;
    // 复盘诊断字段：止盈层级、b段缩量、a段强度、预警延迟、追价深度
    ensure_column(db, "signal_outcomes", "target_tier", "TEXT").await?;
    ensure_column(db, "signal_outcomes", "b_vol_ratio", "REAL").await?;
    ensure_column(db, "signal_outcomes", "a_move_atr", "REAL").await?;
    ensure_column(db, "signal_outcomes", "trigger_lag_bars", "INTEGER").await?;
    ensure_column(db, "signal_outcomes", "trigger_overshoot_r", "REAL").await?;
    info!("数据库表结构已就绪");
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
    async fn migrate_is_idempotent_and_has_diagnostic_columns() {
        let db = SeaDatabase::connect("sqlite::memory:").await.unwrap();
        migrate(&db).await.unwrap();
        migrate(&db).await.unwrap();

        let backend = DbBackend::Sqlite;
        for column in [
            "target_tier",
            "b_vol_ratio",
            "a_move_atr",
            "trigger_lag_bars",
            "trigger_overshoot_r",
        ] {
            let found = db
                .query_one(Statement::from_string(
                    backend,
                    format!(
                        "SELECT 1 AS x FROM pragma_table_info('signal_outcomes') WHERE name = '{column}'"
                    ),
                ))
                .await
                .unwrap();
            assert!(found.is_some(), "signal_outcomes.{column} 应已迁移");
        }
    }
}
