//! Warning-time inference. Old trigger-close predictions have no runtime fallback.
use crate::{
    analyze::model::Bar,
    storage::{entities::pattern_events, repo},
    v2::{dataset::DatasetRow, model::LogisticModel},
};
use anyhow::Result;
use sea_orm::{ConnectionTrait, DatabaseConnection, DbBackend, Statement};
#[derive(Debug, Clone, PartialEq)]
pub struct ChampionWinRate {
    pub model_id: String,
    pub p_win: f64,
}
pub fn dataset_row_from_event(event: &pattern_events::Model, bars: &[Bar]) -> Option<DatasetRow> {
    let w = bars
        .iter()
        .position(|b| b.dt.to_bar_ts().starts_with(&event.warning_ts))?;
    let prefix = &bars[..=w];
    let atr = crate::analyze::indicators::atr(prefix, 20);
    let s1 = prefix
        .iter()
        .position(|b| b.dt.to_bar_ts().starts_with(&event.s1_ts))?;
    let s2 = prefix
        .iter()
        .position(|b| b.dt.to_bar_ts().starts_with(&event.s2_ts))?;
    let a = atr.get(s1).and_then(|x| *x)?;
    let b = atr.get(s2).and_then(|x| *x)?;
    if a <= 0. || b <= 0. {
        return None;
    }
    Some(DatasetRow {
        event_id: event.id.to_string(),
        symbol: event.symbol.clone(),
        direction: event.direction.clone(),
        setup_quality: event.entry_score,
        a_move: event.a_move,
        b_move: event.b_move,
        a_move_atr: event.a_move / a,
        b_move_atr: event.b_move / b,
        a_speed: event.a_move / event.a_bars.max(1) as f64,
        retracement: event.retracement,
        warning_volume_ratio: crate::analyze::outcome::vol_ratio_at(prefix, w),
        trigger_bar_ts: Some(prefix[w].dt.to_bar_ts()),
        context_as_of_ts: Some(prefix[w].dt.to_bar_ts()),
        schema_version: crate::v3::FEATURES.into(),
        ..Default::default()
    })
}
/// Qualification requires actual prospective evidence, not an arbitrary status string.
pub fn eligible(metrics: &serde_json::Value) -> bool {
    let p = &metrics["prospective"];
    metrics["contract"] == crate::v3::CONTRACT
        && metrics["policy"] == crate::v3::POLICY
        && metrics["features"] == crate::v3::FEATURES
        && p["origin"] == "prospective"
        && p["trading_days"].as_u64().unwrap_or(0) >= 30
        && p["independent_groups"].as_u64().unwrap_or(0) >= 200
        && p["auc_ci_low"].as_f64().unwrap_or(0.) > 0.5
        && p["brier_skill"].as_f64().unwrap_or(-1.) > 0.
        && p["net_r_delta_1tick_ci_low"].as_f64().unwrap_or(-1.) > 0.
        && p["net_r_delta_2tick"].as_f64().unwrap_or(-1.) >= 0.
        && p["drawdown_not_worse"].as_bool().unwrap_or(false)
        && p["actual_contracts_and_costs"].as_bool().unwrap_or(false)
}
async fn models(
    db: &DatabaseConnection,
) -> Result<Vec<(String, String, LogisticModel, serde_json::Value)>> {
    let rows=db.query_all(Statement::from_string(DbBackend::Sqlite,"SELECT model_id,status,coefficients,metrics,feature_whitelist FROM v2_model_registry WHERE name='logistic-v3' AND schema_version='3' AND status IN ('champion','challenger')".to_string())).await?;
    let mut out = Vec::new();
    for row in rows {
        let m: serde_json::Value = serde_json::from_str(&row.try_get::<String>("", "metrics")?)?;
        let features: Vec<String> =
            serde_json::from_str(&row.try_get::<String>("", "feature_whitelist")?)?;
        if m["contract"] != crate::v3::CONTRACT
            || m["policy"] != crate::v3::POLICY
            || m["features"] != crate::v3::FEATURES
            || features != crate::v3::whitelist()
        {
            continue;
        }
        let model: LogisticModel =
            serde_json::from_str(&row.try_get::<String>("", "coefficients")?)?;
        if model.feature_names != features
            || model.coefficients.len() != features.len()
            || model.scaler_means.len() != features.len()
            || model.scaler_stds.len() != features.len()
        {
            continue;
        }
        out.push((
            row.try_get("", "model_id")?,
            row.try_get("", "status")?,
            model,
            m,
        ));
    }
    Ok(out)
}
pub async fn freeze_event(
    db: &DatabaseConnection,
    event: &pattern_events::Model,
    bars: &[Bar],
    origin: &str,
) -> Result<usize> {
    let Some(mut row) = dataset_row_from_event(event, bars) else {
        return Ok(0);
    };
    row.label_win = 0;
    row.exit_ts = None;
    row.r_multiple = None;
    let generation = repo::get_setting(db, "signal_generation")
        .await?
        .unwrap_or_else(|| "unprepared".into());
    let decision = row.trigger_bar_ts.clone().unwrap();
    let hash = crate::v2::model::feature_hash(&row, &crate::v3::whitelist());
    db.execute(Statement::from_sql_and_values(DbBackend::Sqlite,"INSERT INTO signal_snapshots(event_id,generation,origin,decision_at,feature_hash,features_json,created_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(event_id) DO NOTHING",vec![event.id.into(),generation.clone().into(),origin.into(),decision.clone().into(),hash.clone().into(),serde_json::to_string(&row)?.into(),chrono::Utc::now().to_rfc3339().into()])).await?;
    let frozen = db
        .query_one(Statement::from_sql_and_values(
            DbBackend::Sqlite,
            "SELECT feature_hash,features_json,origin FROM signal_snapshots WHERE event_id=?",
            vec![event.id.into()],
        ))
        .await?
        .unwrap();
    let frozen_origin: String = frozen.try_get("", "origin")?;
    let frozen_row: DatasetRow =
        serde_json::from_str(&frozen.try_get::<String>("", "features_json")?)?;
    let frozen_hash: String = frozen.try_get("", "feature_hash")?;
    let mut n = 0;
    for (id, _status, model, _metrics) in models(db).await? {
        let probability = model.predict_row_p(&frozen_row);
        if !probability.is_finite() {
            continue;
        }
        let result=db.execute(Statement::from_sql_and_values(DbBackend::Sqlite,"INSERT INTO prediction_history(event_id,model_id,generation,decision_at,feature_hash,p_win,predicted_at,origin,contract) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(event_id,model_id,feature_hash) DO NOTHING",vec![event.id.into(),id.into(),generation.clone().into(),decision.clone().into(),frozen_hash.clone().into(),probability.into(),chrono::Utc::now().to_rfc3339().into(),frozen_origin.clone().into(),crate::v3::CONTRACT.into()])).await?;
        n += result.rows_affected() as usize;
    }
    Ok(n)
}
/// Trigger-time calls must not create a warning-time forecast after seeing its outcome.
pub async fn predict_event(
    db: &DatabaseConnection,
    event: &pattern_events::Model,
    bars: &[Bar],
) -> Result<usize> {
    if event.state != "pending" || event.trigger_ts.is_some() {
        return Ok(0);
    }
    freeze_event(db, event, bars, "prospective").await
}
pub async fn needs_prediction(_db: &DatabaseConnection, _id: i64) -> Result<bool> {
    Ok(false)
}
pub async fn has_champion_model(db: &DatabaseConnection) -> Result<bool> {
    Ok(models(db)
        .await?
        .iter()
        .any(|(_, s, _, m)| s == "champion" && eligible(m)))
}
pub async fn champion_win_rate(
    db: &DatabaseConnection,
    event_id: i64,
) -> Result<Option<ChampionWinRate>> {
    for (id, status, _, metrics) in models(db).await? {
        if status != "champion" || !eligible(&metrics) {
            continue;
        }
        if let Some(row)=db.query_one(Statement::from_sql_and_values(DbBackend::Sqlite,"SELECT p_win FROM prediction_history WHERE event_id=? AND model_id=? AND origin='prospective' ORDER BY id DESC LIMIT 1",vec![event_id.into(),id.clone().into()])).await? {return Ok(Some(ChampionWinRate{model_id:id,p_win:row.try_get("","p_win")?}))}
    }
    Ok(None)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn old_or_incomplete_evidence_cannot_qualify() {
        assert!(!eligible(
            &serde_json::json!({"prospective":{"trading_days":9999,"independent_groups":9999}})
        ))
    }
    #[tokio::test]
    async fn old_champion_is_not_a_fallback() {
        let db = crate::storage::connect(std::path::Path::new(":memory:"))
            .await
            .unwrap();
        assert!(!has_champion_model(&db).await.unwrap());
        assert!(champion_win_rate(&db, 1).await.unwrap().is_none());
    }
}
