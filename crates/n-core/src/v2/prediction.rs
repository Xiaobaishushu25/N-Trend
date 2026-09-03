//! V2 model inference and prediction persistence.
//!
//! Training produces model registry rows and bundles, while the application
//! consumes per-event rows from `v2_model_predictions`.  This module bridges
//! those two pieces for both historical backfill and live trigger events.

use anyhow::{Context, Result};
use sea_orm::{ConnectionTrait, DatabaseConnection, DbBackend, EntityTrait, Statement};
use serde::Serialize;
use std::collections::{HashMap, HashSet};

use crate::analyze::indicators;
use crate::analyze::model::{Bar, DT, ATR_PERIOD};
use crate::analyze::outcome;
use crate::storage::entities::{klines, pattern_events, v2_model_registry};
use crate::storage::repo;
use crate::v2::dataset::DatasetRow;
use crate::v2::features::{normalize_direction, extract_trigger_features, SetupFeatures};
use crate::v2::model::{
    predict_gam, predict_logistic, GamModel, InferenceBundle, LogisticModel, Prediction,
    SplineTable,
};

#[derive(Debug, Clone, Default, Serialize)]
pub struct BackfillResult {
    pub models: usize,
    pub events_seen: usize,
    pub events_scored: usize,
    pub predictions_written: usize,
}

fn json_f64_array(value: &serde_json::Value, key: &str) -> Option<Vec<f64>> {
    value
        .get(key)?
        .as_array()?
        .iter()
        .map(|v| v.as_f64())
        .collect()
}

fn json_string_array(value: &serde_json::Value, key: &str) -> Option<Vec<String>> {
    value
        .get(key)?
        .as_array()?
        .iter()
        .map(|v| v.as_str().map(str::to_string))
        .collect()
}

fn bundle_from_registry(row: &v2_model_registry::Model) -> Option<InferenceBundle> {
    let feature_whitelist: Vec<String> = serde_json::from_str(&row.feature_whitelist).ok()?;
    let coefficients: serde_json::Value = serde_json::from_str(&row.coefficients).ok()?;

    if row.name == "gam-v1" || row.spline_knots.is_some() {
        let linear_features = json_string_array(&coefficients, "linear_features").unwrap_or_default();
        let linear_coefficients = json_f64_array(&coefficients, "linear_coefficients").unwrap_or_default();
        let splines: Vec<SplineTable> = row
            .spline_knots
            .as_deref()
            .and_then(|s| serde_json::from_str(s).ok())
            .unwrap_or_default();
        return Some(InferenceBundle {
            model_id: row.model_id.clone(),
            feature_whitelist,
            scaler_means: Vec::new(),
            scaler_stds: Vec::new(),
            logistic: None,
            gam: Some(GamModel {
                intercept: coefficients.get("intercept")?.as_f64()?,
                splines,
                linear_coefficients,
                linear_features,
            }),
            schema_version: row.schema_version.clone(),
        });
    }

    let feature_names = json_string_array(&coefficients, "feature_names")
        .unwrap_or_else(|| feature_whitelist.clone());
    let coefficients_vec = json_f64_array(&coefficients, "coefficients")?;
    let scaler_means = json_f64_array(&coefficients, "scaler_means")
        .unwrap_or_else(|| vec![0.0; feature_names.len()]);
    let scaler_stds = json_f64_array(&coefficients, "scaler_stds")
        .unwrap_or_else(|| vec![1.0; feature_names.len()]);
    if coefficients_vec.len() != feature_names.len()
        || scaler_means.len() != feature_names.len()
        || scaler_stds.len() != feature_names.len()
    {
        return None;
    }
    Some(InferenceBundle {
        model_id: row.model_id.clone(),
        feature_whitelist,
        scaler_means: scaler_means.clone(),
        scaler_stds: scaler_stds.clone(),
        logistic: Some(LogisticModel {
            intercept: coefficients.get("intercept")?.as_f64()?,
            coefficients: coefficients_vec,
            feature_names,
            scaler_means,
            scaler_stds,
        }),
        gam: None,
        schema_version: row.schema_version.clone(),
    })
}

async fn load_bundles(db: &DatabaseConnection) -> Result<Vec<InferenceBundle>> {
    let rows = v2_model_registry::Entity::find().all(db).await?;
    Ok(rows.iter().filter_map(bundle_from_registry).collect())
}

fn to_bars(rows: &[klines::Model]) -> Vec<Bar> {
    rows.iter()
        .filter_map(|row| {
            let dt = DT::from_bar_ts(&row.ts)?;
            Some(Bar {
                dt,
                open: row.open,
                high: row.high,
                low: row.low,
                close: row.close,
                volume: row.volume,
                hold: row.hold,
                rollover: false,
            })
        })
        .collect()
}

fn find_bar_index(bars: &[Bar], ts: &str) -> Option<usize> {
    bars.iter().position(|bar| {
        let actual = bar.dt.to_bar_ts();
        actual == ts || actual.starts_with(ts) || ts.starts_with(&actual)
    })
}

/// Convert a persisted forward event into the exact feature shape consumed by
/// the trained models.  Trigger features are calculated only from the closed
/// trigger bar and bars before it.
pub fn dataset_row_from_event(event: &pattern_events::Model, bars: &[Bar]) -> Option<DatasetRow> {
    let trigger_ts = event.trigger_bar_ts.as_deref()?;
    let trigger_index = find_bar_index(bars, trigger_ts)?;
    let trigger_bar = bars.get(trigger_index)?;
    let atr = indicators::atr(bars, ATR_PERIOD)
        .get(trigger_index)
        .and_then(|value| *value);
    let trigger_volume = event
        .trigger_volume_ratio
        .or_else(|| outcome::vol_ratio_at(bars, trigger_index));
    let mut trigger = extract_trigger_features(
        trigger_bar,
        // The legacy pattern event stores the trigger price as `entry`.
        event.entry,
        event.risk,
        atr,
        trigger_volume,
        None,
        None,
    );

    let s1_index = find_bar_index(bars, &event.s1_ts);
    let a_atr = s1_index
        .and_then(|index| indicators::atr(bars, ATR_PERIOD).get(index).and_then(|value| *value))
        .unwrap_or(0.0);
    let warning_volume = find_bar_index(bars, &event.warning_ts)
        .and_then(|index| outcome::vol_ratio_at(bars, index));
    let mut setup = SetupFeatures {
        a_move: event.a_move,
        b_move: event.b_move,
        a_bars: event.a_bars,
        b_bars: event.b_bars,
        retracement: event.retracement,
        a_speed: if event.a_bars > 0 {
            event.a_move / event.a_bars as f64
        } else {
            0.0
        },
        a_move_atr: if a_atr > 1e-9 {
            event.a_move / a_atr
        } else {
            0.0
        },
        // Keep this aligned with the current replay trainer, which leaves the
        // B-leg ATR feature at zero for replay-built rows.
        b_move_atr: 0.0,
        grade: event.grade.clone(),
        level: event.level.clone(),
        direction: event.direction.clone(),
        a_strong_count: 0,
        setup_quality: event.entry_score,
        trend60_state: String::new(),
        warning_close_location: None,
        warning_body_atr: None,
        warning_wick_ratio: None,
        warning_volume_ratio: warning_volume,
        normalized: false,
        missing_mask: 0,
    };
    normalize_direction(&mut setup, Some(&mut trigger));

    Some(DatasetRow {
        event_id: event.id.to_string(),
        symbol: event.symbol.clone(),
        direction: setup.direction,
        setup_quality: setup.setup_quality,
        a_move: setup.a_move,
        b_move: setup.b_move,
        a_move_atr: setup.a_move_atr,
        b_move_atr: setup.b_move_atr,
        a_speed: setup.a_speed,
        retracement: setup.retracement,
        warning_volume_ratio: setup.warning_volume_ratio,
        trigger_close_overshoot_r: trigger.close_overshoot_r,
        trigger_close_location: trigger.close_location,
        trigger_body_atr: trigger.body_atr,
        trigger_volume_ratio: trigger.volume_ratio,
        trigger_wick_atr: trigger.wick_atr,
        internal_swing_margin_r: trigger.internal_swing_margin_r,
        chase_distance_r: trigger.chase_distance_r,
        missing_mask: setup.missing_mask | trigger.missing_mask,
        label_win: i32::from(event.outcome.as_deref() == Some("win")),
        r_multiple: event.r_multiple,
        is_1r_aux_win: None,
        trigger_bar_ts: Some(trigger.trigger_bar_ts),
        exit_ts: event.exit_ts.clone(),
        schema_version: crate::v2::FEATURE_SCHEMA_VERSION.to_string(),
    })
}

fn predict(bundle: &InferenceBundle, row: &DatasetRow) -> Option<Prediction> {
    if bundle.logistic.is_some() {
        predict_logistic(bundle, row)
    } else {
        predict_gam(bundle, row)
    }
}

fn sql_quote(value: &str) -> String {
    value.replace('\'', "''")
}

async fn upsert_prediction(
    db: &DatabaseConnection,
    event_id: i64,
    prediction: &Prediction,
) -> Result<()> {
    let sql = format!(
        "INSERT INTO v2_model_predictions (event_id, model_id, p_win, logit, feature_hash, predicted_at) VALUES ({}, '{}', {}, {}, '{}', '{}') ON CONFLICT(event_id, model_id) DO UPDATE SET p_win=excluded.p_win, logit=excluded.logit, feature_hash=excluded.feature_hash, predicted_at=excluded.predicted_at",
        event_id,
        sql_quote(&prediction.model_id),
        prediction.p_win,
        prediction.logit,
        sql_quote(&prediction.feature_hash),
        sql_quote(&prediction.predicted_at),
    );
    db.execute(Statement::from_string(DbBackend::Sqlite, sql))
        .await
        .context("写入 V2 预测失败")?;
    Ok(())
}

/// Score one newly-triggered event with every registered model.
pub async fn predict_event(
    db: &DatabaseConnection,
    event: &pattern_events::Model,
    bars: &[Bar],
) -> Result<usize> {
    let Some(row) = dataset_row_from_event(event, bars) else {
        return Ok(0);
    };
    let bundles = load_bundles(db).await?;
    let mut written = 0;
    for bundle in &bundles {
        if let Some(prediction) = predict(bundle, &row) {
            upsert_prediction(db, event.id, &prediction).await?;
            written += 1;
        }
    }
    Ok(written)
}

/// Backfill missing predictions for all persisted events that have triggered.
/// It is idempotent and only scores event/model pairs that are not present yet.
pub async fn backfill(db: &DatabaseConnection) -> Result<BackfillResult> {
    let bundles = load_bundles(db).await?;
    let events = repo::all_pattern_events(db).await?;
    let existing_rows = db
        .query_all(Statement::from_string(
            DbBackend::Sqlite,
            "SELECT event_id, model_id, p_win FROM v2_model_predictions".to_string(),
        ))
        .await?;
    let mut existing = HashSet::new();
    for row in existing_rows {
        if let (Ok(event_id), Ok(model_id), Ok(p_win)) = (
            row.try_get::<i64>("", "event_id"),
            row.try_get::<String>("", "model_id"),
            row.try_get::<Option<f64>>("", "p_win"),
        ) {
            if p_win.is_some() {
                existing.insert((event_id, model_id));
            }
        }
    }

    let mut by_symbol: HashMap<String, Vec<pattern_events::Model>> = HashMap::new();
    for event in events {
        if event.trigger_bar_ts.is_some() {
            by_symbol.entry(event.symbol.clone()).or_default().push(event);
        }
    }

    let mut result = BackfillResult {
        models: bundles.len(),
        ..Default::default()
    };
    for (symbol, events) in by_symbol {
        let rows = repo::klines(db, &symbol, "15m", None, None).await?;
        let bars = to_bars(&rows);
        for event in events {
            result.events_seen += 1;
            let Some(row) = dataset_row_from_event(&event, &bars) else {
                continue;
            };
            let mut scored = false;
            for bundle in &bundles {
                if existing.contains(&(event.id, bundle.model_id.clone())) {
                    continue;
                }
                if let Some(prediction) = predict(bundle, &row) {
                    upsert_prediction(db, event.id, &prediction).await?;
                    existing.insert((event.id, bundle.model_id.clone()));
                    result.predictions_written += 1;
                    scored = true;
                }
            }
            if scored {
                result.events_scored += 1;
            }
        }
    }
    Ok(result)
}
