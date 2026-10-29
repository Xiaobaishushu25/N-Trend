//! Additive display-only contract; never an execution or probability input.
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct QualityContribution {
    pub name: String,
    pub score: f64,
    pub weight: f64,
    pub contribution: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct SignalQualityDto {
    pub event_id: i64,
    pub event_key: String,
    pub warning_ts: String,
    pub timeframe: String,
    pub feature_version: String,
    pub rules_version: String,
    pub parameter_hash: String,
    pub input_hash: String,
    pub origin: String,
    pub status: String,
    pub morphology_score: Option<f64>,
    pub space_score: Option<f64>,
    pub quality_score: Option<f64>,
    pub trend_score: Option<f64>,
    pub a_score: Option<f64>,
    pub b_score: Option<f64>,
    pub features: BTreeMap<String, Option<f64>>,
    pub contributions: Vec<QualityContribution>,
    pub explanations: Vec<String>,
    pub flags: Vec<String>,
}
