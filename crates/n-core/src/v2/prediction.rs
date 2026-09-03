//! V2 model inference and prediction persistence.
//!
//! Training produces model registry rows and bundles, while the application
//! consumes per-event rows from `v2_model_predictions`.  This module bridges
//! those two pieces for both historical backfill and live trigger events.

use anyhow::{Context, Result};
use sea_orm::{ColumnTrait, ConnectionTrait, DatabaseConnection, DbBackend, EntityTrait, QueryFilter, Statement};
use serde::Serialize;
use std::collections::{HashMap, HashSet};

use crate::analyze::indicators;
use crate::analyze::model::{Bar, DT, ATR_PERIOD};
use crate::analyze::outcome;
use crate::derive::{aggregate, Timeframe};
use crate::storage::entities::{klines, pattern_events, rollovers, v2_model_registry};
use crate::storage::repo;
use crate::v2::dataset::DatasetRow;
use crate::v2::features::{extract_market_context, normalize_direction, extract_trigger_features, MarketContextSnapshot, SetupFeatures};
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
    pub current_cohort_events: usize,
    pub legacy_cohort_events: usize,
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
    let rows = v2_model_registry::Entity::find()
        .filter(v2_model_registry::Column::Status.eq("champion"))
        .all(db).await?;
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

fn to_rollover_records(rows: &[rollovers::Model]) -> Vec<crate::derive::rollover::RolloverRecord> {
    rows.iter().filter(|r| r.confirmed).map(|r| crate::derive::rollover::RolloverRecord {
        symbol: r.symbol.clone(), ts: r.ts.clone(), from_contract: r.from_contract.clone(),
        to_contract: r.to_contract.clone(), confirmed: r.confirmed,
    }).collect()
}

fn find_bar_index(bars: &[Bar], ts: &str) -> Option<usize> {
    bars.iter().position(|bar| {
        let actual = bar.dt.to_bar_ts();
        actual == ts || actual.starts_with(ts) || ts.starts_with(&actual)
    })
}

fn event_dim_string(event: &pattern_events::Model, key: &str) -> Option<String> {
    serde_json::from_str::<serde_json::Value>(&event.entry_score_dims)
        .ok()?
        .get(key)?
        .as_str()
        .map(str::to_string)
}

fn is_current_event_cohort(event: &pattern_events::Model) -> bool {
    event_dim_string(event, "event_logic_version").as_deref() == Some(crate::v2::version::EVENT_LOGIC_VERSION)
        && event_dim_string(event, "pattern_version").as_deref()
            == Some(crate::v2::PATTERN_LOGIC_VERSION)
        && event_dim_string(event, "execution_version").as_deref()
            == Some(crate::v2::EXECUTION_VERSION)
        && event_dim_string(event, "feature_schema_version").as_deref()
            == Some(crate::v2::FEATURE_SCHEMA_VERSION)
}

/// Convert a persisted forward event into the exact feature shape consumed by
/// the trained models.  Trigger features are calculated only from the closed
/// trigger bar and bars before it.
pub fn dataset_row_from_event(event: &pattern_events::Model, bars: &[Bar]) -> Option<DatasetRow> {
    dataset_row_from_event_with_context(event, bars, None)
}

pub fn dataset_row_from_event_with_context(
    event: &pattern_events::Model,
    bars: &[Bar],
    market_context: Option<MarketContextSnapshot>,
) -> Option<DatasetRow> {
    let trigger_ts = event.trigger_bar_ts.as_deref()?;
    let trigger_index = find_bar_index(bars, trigger_ts)?;
    let trigger_bar = bars.get(trigger_index)?;
    let atr_series = indicators::atr(bars, ATR_PERIOD);
    let atr = atr_series.get(trigger_index).and_then(|value| *value);
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
    let s2_index = find_bar_index(bars, &event.s2_ts);
    let warning_index = find_bar_index(bars, &event.warning_ts).or(s2_index);
    let a_atr = s1_index
        .and_then(|index| atr_series.get(index).and_then(|value| *value))
        .unwrap_or(0.0);
    let b_atr = s2_index
        .and_then(|index| atr_series.get(index).and_then(|value| *value))
        .unwrap_or(0.0);
    let warning_volume = warning_index.and_then(|index| outcome::vol_ratio_at(bars, index));
    let (warning_close_location, warning_body_atr, warning_wick_ratio) =
        warning_index
            .and_then(|index| bars.get(index).map(|bar| {
                let range = (bar.high - bar.low).max(1e-9);
                let body = (bar.close - bar.open).abs();
                let upper = bar.high - bar.open.max(bar.close);
                let lower = bar.open.min(bar.close) - bar.low;
                (
                    Some((bar.close - bar.low) / range),
                    Some(body / b_atr.max(1e-9)),
                    Some(if body > 1e-9 { upper.max(lower) / body } else { 0.0 }),
                )
            }))
            .unwrap_or((None, None, None));
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
        b_move_atr: if b_atr > 1e-9 { event.b_move / b_atr } else { 0.0 },
        grade: event.grade.clone(),
        level: event.level.clone(),
        direction: event.direction.clone(),
        a_strong_count: 0,
        setup_quality: event.entry_score,
        trend60_state: event_dim_string(event, "trend_state").unwrap_or_default(),
        warning_close_location,
        warning_body_atr,
        warning_wick_ratio,
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
        trend_gap_60: market_context.as_ref().and_then(|c| c.trend_gap_60),
        trend_slope_60: market_context.as_ref().and_then(|c| c.trend_slope_60),
        trend_strength_60: market_context.as_ref().and_then(|c| c.trend_strength_60),
        trend_alignment_60: market_context.as_ref().and_then(|c| c.trend_alignment_60),
        trend_10d: market_context.as_ref().and_then(|c| c.trend_10d),
        trend_alignment_10d: market_context.as_ref().and_then(|c| c.trend_alignment_10d),
        range_position_10d: market_context.as_ref().and_then(|c| c.range_position_10d),
        mr_position_10d: market_context.as_ref().and_then(|c| c.mr_position_10d),
        distance_ma10_dir: market_context.as_ref().and_then(|c| c.distance_ma10_dir),
        trend_position_interaction: market_context.as_ref().and_then(|c| c.trend_position_interaction),
        context_as_of_ts: market_context.as_ref().map(|c| c.as_of_ts.clone()),
        context_last_60m_ts: market_context.as_ref().and_then(|c| c.latest_60m_close_ts.clone()),
        context_last_daily_day: market_context.as_ref().and_then(|c| c.latest_daily_trading_day.clone()),
        crossed_rollover_10d: market_context.as_ref().map(|c| c.crossed_rollover_10d).unwrap_or(false),
    })
}

fn predict(bundle: &InferenceBundle, row: &DatasetRow) -> Option<Prediction> {
    // A context model must never silently turn unavailable context into
    // scaled zero/mean values.  The caller can retain the V0 champion as the
    // fallback when this returns None.
    let context_features = [
        "trend_gap_60", "trend_slope_60", "trend_strength_60", "trend_alignment_60",
        "trend_10d", "trend_alignment_10d", "range_position_10d", "mr_position_10d",
        "distance_ma10_dir", "trend_position_interaction",
    ];
    if bundle.feature_whitelist.iter().any(|name| context_features.contains(&name.as_str())) {
        let complete = bundle.feature_whitelist.iter().all(|name| {
            !context_features.contains(&name.as_str()) || crate::v2::model::get_feature(row, name).is_some()
        });
        if !complete { return None; }
    }
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
        "INSERT INTO v2_model_predictions (event_id, model_id, p_win, logit, feature_hash, predicted_at, prediction_mode) VALUES ({}, '{}', {}, {}, '{}', '{}', '{}') ON CONFLICT(event_id, model_id) DO UPDATE SET p_win=excluded.p_win, logit=excluded.logit, feature_hash=excluded.feature_hash, predicted_at=excluded.predicted_at, prediction_mode=excluded.prediction_mode",
        event_id,
        sql_quote(&prediction.model_id),
        prediction.p_win,
        prediction.logit,
        sql_quote(&prediction.feature_hash),
        sql_quote(&prediction.predicted_at),
        sql_quote(&prediction.prediction_mode),
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
    let raw_rows = repo::raw_klines(db, &event.symbol).await?;
    let rollover_rows = repo::symbol_rollovers(db, &event.symbol).await?;
    let rollover_records = to_rollover_records(&rollover_rows);
    let raw = raw_rows.iter().map(crate::service::model_to_fetch).collect::<Vec<_>>();
    let (_, bars60) = {
        let m15 = aggregate(&raw, Timeframe::M15).iter().filter_map(|k| DT::from_bar_ts(&k.datetime).map(|dt| Bar { dt, open:k.open, high:k.high, low:k.low, close:k.close, volume:k.volume, hold:k.hold, rollover:false })).collect::<Vec<_>>();
        let m60 = aggregate(&raw, Timeframe::M60).iter().filter_map(|k| DT::from_bar_ts(&k.datetime).map(|dt| Bar { dt, open:k.open, high:k.high, low:k.low, close:k.close, volume:k.volume, hold:k.hold, rollover:false })).collect::<Vec<_>>();
        (m15, m60)
    };
    let daily = aggregate(&raw, Timeframe::Day).iter().filter_map(|k| DT::from_bar_ts(&k.datetime).map(|dt| Bar { dt, open:k.open, high:k.high, low:k.low, close:k.close, volume:k.volume, hold:k.hold, rollover:false })).collect::<Vec<_>>();
    let context = event.trigger_bar_ts.as_deref().and_then(|ts| extract_market_context(&event.symbol, ts, &event.direction, bars, &bars60, &daily, &rollover_records));
    let Some(row) = dataset_row_from_event_with_context(event, bars, context) else {
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
    let mut current_cohort_events = 0usize;
    let mut legacy_cohort_events = 0usize;
    for event in events {
        if event.trigger_bar_ts.is_some() {
            if is_current_event_cohort(&event) {
                // Keep these counts visible to callers so application
                // retrospective stats can report current and legacy cohorts
                // separately instead of silently mixing them.
                current_cohort_events += 1;
            } else {
                legacy_cohort_events += 1;
            }
            by_symbol.entry(event.symbol.clone()).or_default().push(event);
        }
    }

    let mut result = BackfillResult {
        models: bundles.len(),
        current_cohort_events,
        legacy_cohort_events,
        ..Default::default()
    };
    for (symbol, events) in by_symbol {
        let rows = repo::klines(db, &symbol, "15m", None, None).await?;
        let bars = to_bars(&rows);
        let rollover_rows = repo::symbol_rollovers(db, &symbol).await?;
        let rollover_records = to_rollover_records(&rollover_rows);
        let raw_rows = repo::raw_klines(db, &symbol).await?;
        let raw = raw_rows.iter().map(crate::service::model_to_fetch).collect::<Vec<_>>();
        let bars60 = aggregate(&raw, Timeframe::M60).iter().filter_map(|k| DT::from_bar_ts(&k.datetime).map(|dt| Bar { dt, open:k.open, high:k.high, low:k.low, close:k.close, volume:k.volume, hold:k.hold, rollover:false })).collect::<Vec<_>>();
        let daily = aggregate(&raw, Timeframe::Day).iter().filter_map(|k| DT::from_bar_ts(&k.datetime).map(|dt| Bar { dt, open:k.open, high:k.high, low:k.low, close:k.close, volume:k.volume, hold:k.hold, rollover:false })).collect::<Vec<_>>();
        for event in events {
            result.events_seen += 1;
            let context = event.trigger_bar_ts.as_deref().and_then(|ts| extract_market_context(&symbol, ts, &event.direction, &bars, &bars60, &daily, &rollover_records));
            let Some(row) = dataset_row_from_event_with_context(&event, &bars, context) else {
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
