//! SQLite connection and schema migration.

use std::path::Path;

use anyhow::{anyhow, Context, Result};
use sea_orm::{ConnectionTrait, Database as SeaDatabase, DatabaseConnection, DbBackend, Schema};
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
    ];
    let backend = db.get_database_backend();
    for table in tables {
        let stmt = backend.build(&table);
        db.execute(stmt).await.context("创建数据表失败")?;
    }
    info!("数据库表结构已就绪");
    Ok(())
}


