use anyhow::Result;
use std::path::PathBuf;

#[tokio::main]
async fn main() -> Result<()> {
    let args: Vec<String> = std::env::args().collect();
    let dry_run = args.iter().any(|a| a == "--dry-run");
    let db_path = args.iter().find(|a| a.starts_with("--db=")).map(|s| s.trim_start_matches("--db=").to_string()).unwrap_or_else(|| "ntrend.db".to_string());
    if dry_run {
        println!("[dry-run] Would migrate DB at {}", db_path);
        // Validate schema definitions without touching disk when using :memory:
        let db = n_core::storage::connect(&PathBuf::from(":memory:")).await?;
        // quick check v2 tables exist
        use sea_orm::{ConnectionTrait, Statement, DbBackend};
        let tables = ["v2_trade_events","v2_setup_features","v2_trigger_features","v2_model_predictions","v2_trade_outcomes","v2_model_registry"];
        for t in tables {
            let r = db.query_one(Statement::from_string(DbBackend::Sqlite, format!("SELECT name FROM sqlite_master WHERE type='table' AND name='{}'", t))).await?;
            if r.is_none() { anyhow::bail!("V2 table missing after migrate: {}", t); }
        }
        println!("[dry-run] OK — all V2 tables present, schema_migrated would be 4");
        return Ok(());
    }
    let path = PathBuf::from(&db_path);
    let _db = n_core::storage::connect(&path).await?;
    println!("Migration complete for {}", db_path);
    Ok(())
}