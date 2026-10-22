use sea_orm::{ConnectionTrait, DatabaseConnection};
pub async fn ensure(db: &DatabaseConnection) -> anyhow::Result<()> {
    for sql in [
 "CREATE TABLE IF NOT EXISTS signal_snapshots(event_id INTEGER PRIMARY KEY,generation TEXT NOT NULL,origin TEXT NOT NULL,decision_at TEXT NOT NULL,feature_hash TEXT NOT NULL,features_json TEXT NOT NULL,created_at TEXT NOT NULL)",
 "CREATE TABLE IF NOT EXISTS prediction_history(id INTEGER PRIMARY KEY AUTOINCREMENT,event_id INTEGER NOT NULL,model_id TEXT NOT NULL,generation TEXT NOT NULL,decision_at TEXT NOT NULL,feature_hash TEXT NOT NULL,p_win REAL NOT NULL,predicted_at TEXT NOT NULL,origin TEXT NOT NULL,contract TEXT NOT NULL,UNIQUE(event_id,model_id,feature_hash))",
 "CREATE TABLE IF NOT EXISTS analysis_runs(run_id TEXT PRIMARY KEY,contract TEXT NOT NULL,policy TEXT NOT NULL,cost_version TEXT NOT NULL,created_at TEXT NOT NULL,status TEXT NOT NULL,report_json TEXT NOT NULL)",
 "CREATE TABLE IF NOT EXISTS signal_evaluations(run_id TEXT NOT NULL,event_id INTEGER NOT NULL,policy TEXT NOT NULL,scenario TEXT NOT NULL,evaluation_json TEXT NOT NULL,PRIMARY KEY(run_id,event_id,policy,scenario))"
 ] {db.execute_unprepared(sql).await?;}
    Ok(())
}
