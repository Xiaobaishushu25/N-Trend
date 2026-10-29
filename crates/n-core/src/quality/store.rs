//! Immutable sidecar evaluations. No updates to events, predictions or trading settings.
use super::{event_key, Rules};
use crate::storage::{entities::pattern_events, repo};
use anyhow::Result;
use n_protocol::quality::SignalQualityDto;
use sea_orm::{ConnectionTrait, DatabaseConnection, DbBackend, Statement};
use std::collections::HashMap;

pub async fn ensure(db: &DatabaseConnection) -> Result<()> {
    db.execute_unprepared("CREATE TABLE IF NOT EXISTS signal_quality_evaluations(id INTEGER PRIMARY KEY AUTOINCREMENT,event_id INTEGER NOT NULL,event_key TEXT NOT NULL,generation TEXT NOT NULL,warning_ts TEXT NOT NULL,feature_version TEXT NOT NULL,rules_version TEXT NOT NULL,parameter_hash TEXT NOT NULL,input_hash TEXT NOT NULL,origin TEXT NOT NULL,evaluation_json TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(event_key,generation,feature_version,parameter_hash,input_hash,origin))").await?;
    db.execute_unprepared("CREATE INDEX IF NOT EXISTS idx_quality_event_policy ON signal_quality_evaluations(event_id,rules_version,parameter_hash)").await?;
    Ok(())
}
pub async fn enabled(db: &DatabaseConnection) -> bool {
    !matches!(
        repo::get_setting(db, "signal_quality_enabled")
            .await
            .ok()
            .flatten()
            .as_deref(),
        Some("false" | "0")
    )
}
pub async fn save(db: &DatabaseConnection, q: &SignalQualityDto) -> Result<()> {
    let generation = repo::get_setting(db, "signal_generation")
        .await?
        .unwrap_or_else(|| "unprepared".into());
    db.execute(Statement::from_sql_and_values(DbBackend::Sqlite,"INSERT OR IGNORE INTO signal_quality_evaluations(event_id,event_key,generation,warning_ts,feature_version,rules_version,parameter_hash,input_hash,origin,evaluation_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",vec![q.event_id.into(),q.event_key.clone().into(),generation.into(),q.warning_ts.clone().into(),q.feature_version.clone().into(),q.rules_version.clone().into(),q.parameter_hash.clone().into(),q.input_hash.clone().into(),q.origin.clone().into(),serde_json::to_string(q)?.into(),chrono::Utc::now().to_rfc3339().into()])).await?;
    Ok(())
}
pub async fn latest(
    db: &DatabaseConnection,
    events: &[pattern_events::Model],
) -> Result<HashMap<i64, SignalQualityDto>> {
    let mut result = HashMap::new();
    if events.is_empty() || !enabled(db).await {
        return Ok(result);
    }
    let rules = Rules::default();
    let generation = repo::get_setting(db, "signal_generation")
        .await?
        .unwrap_or_else(|| "unprepared".into());
    let keys: HashMap<_, _> = events.iter().map(|e| (e.id, event_key(e))).collect();
    // Bound variable count, with live frozen entries taking precedence over historical recomputes.
    for chunk in events.chunks(400) {
        let sql=format!("SELECT event_id,event_key,evaluation_json FROM signal_quality_evaluations WHERE rules_version=? AND parameter_hash=? AND generation=? AND event_id IN ({}) ORDER BY CASE origin WHEN 'live_frozen' THEN 0 ELSE 1 END,CASE WHEN origin='live_frozen' THEN id END ASC,id DESC",vec!["?";chunk.len()].join(","));
        let mut values: Vec<sea_orm::Value> = vec![
            rules.version.clone().into(),
            rules.hash().into(),
            generation.clone().into(),
        ];
        values.extend(chunk.iter().map(|e| e.id.into()));
        for row in db
            .query_all(Statement::from_sql_and_values(
                DbBackend::Sqlite,
                sql,
                values,
            ))
            .await?
        {
            let id: i64 = row.try_get("", "event_id")?;
            let key: String = row.try_get("", "event_key")?;
            if keys.get(&id) == Some(&key) && !result.contains_key(&id) {
                if let Ok(q) = serde_json::from_str(&row.try_get::<String>("", "evaluation_json")?)
                {
                    result.insert(id, q);
                }
            }
        }
    }
    Ok(result)
}

/// Optional presentation data must not break the original signal API.
pub async fn display(
    db: &DatabaseConnection,
    events: &[pattern_events::Model],
) -> HashMap<i64, SignalQualityDto> {
    match latest(db, events).await {
        Ok(rows) => rows,
        Err(error) => {
            tracing::warn!("读取独立质量评价失败: {error:#}");
            HashMap::new()
        }
    }
}
