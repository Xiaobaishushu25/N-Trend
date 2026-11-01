//! Versioned, fixed-one-lot review contract. Independent of legacy review DTOs.
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

pub const PROFILE: &str = "review-fixed-one-r-5m-v1";
pub const COST_VERSION: &str = "roundtrip-2.5tick-including-2tick-slip-v1";

#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
#[serde(default, deny_unknown_fields)]
pub struct ReviewFilters {
    pub symbols: Vec<String>,
    pub directions: Vec<String>,
    pub levels: Vec<String>,
    pub grades: Vec<String>,
    pub states: Vec<String>,
    pub exit_reasons: Vec<String>,
    pub terminal_reasons: Vec<String>,
    pub score_metric: Option<String>,
    pub score_min: Option<f64>,
    pub score_max: Option<f64>,
    pub score_status: Option<String>,
    pub time_field: Option<String>,
    pub time_start: Option<String>,
    pub time_end: Option<String>,
    pub sessions: Vec<ReviewSession>,
    pub sample_scope: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ReviewSession {
    pub start: String,
    pub end: String,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct ReviewQuery {
    pub filters: ReviewFilters,
    pub sort_by: Option<String>,
    pub descending: Option<bool>,
    pub page_size: Option<usize>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct ReviewPageRequest {
    pub cursor: Option<usize>,
    pub limit: Option<usize>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReviewBucket {
    pub label: String,
    pub min: f64,
    pub max: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReviewTimeBucket {
    pub label: String,
    pub start: String,
    pub end: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReviewAggregateRequest {
    pub snapshot_id: String,
    pub dimensions: Vec<String>,
    #[serde(default)]
    pub score_buckets: Vec<ReviewBucket>,
    #[serde(default)]
    pub time_buckets: Vec<ReviewTimeBucket>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct ReviewSummary {
    pub event_id: i64,
    pub result_version: String,
    pub family_id: i64,
    pub family_size: usize,
    pub symbol: String,
    pub direction: String,
    pub level: String,
    pub grade: String,
    pub warning_ts: String,
    pub entry_ts: Option<String>,
    pub exit_ts: Option<String>,
    pub state: String,
    pub exit_reason: Option<String>,
    pub terminal_reason: Option<String>,
    pub scores: BTreeMap<String, Option<f64>>,
    pub score_status: String,
    pub risk_points: Option<f64>,
    pub risk_amount: Option<f64>,
    pub gross_amount: Option<f64>,
    pub net_amount: Option<f64>,
    pub gross_r: Option<f64>,
    pub net_r: Option<f64>,
    pub mfe_r: Option<f64>,
    pub mae_r: Option<f64>,
    pub mfe_amount: Option<f64>,
    pub mae_amount: Option<f64>,
    pub bars_held: usize,
    pub wait_bars: usize,
    pub coverage: Vec<String>,
    pub diagnostics: BTreeMap<String, Option<f64>>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ReviewMetric {
    pub value: Option<f64>,
    pub n: usize,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct ReviewStatistics {
    pub count: usize,
    pub counts: BTreeMap<String, usize>,
    pub metrics: BTreeMap<String, ReviewMetric>,
    pub gross_r_histogram: BTreeMap<String, usize>,
    pub mfe_r_histogram: BTreeMap<String, usize>,
    pub mae_r_histogram: BTreeMap<String, usize>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ReviewGroup {
    pub key: Vec<String>,
    pub statistics: ReviewStatistics,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ReviewEquityPoint {
    pub event_id: i64,
    pub exit_ts: String,
    pub gross_amount: Option<f64>,
    pub net_amount: Option<f64>,
    pub gross_r: Option<f64>,
    pub net_r: Option<f64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ReviewItemsPage {
    pub snapshot_id: String,
    pub revision: i64,
    pub total: usize,
    pub items: Vec<ReviewSummary>,
    pub next_cursor: Option<usize>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ReviewQueryResponse {
    #[serde(flatten)]
    pub page: ReviewItemsPage,
    pub expires_at: String,
    pub raw_count: usize,
    pub deduplicated_count: usize,
    pub overview: ReviewStatistics,
    pub groups: BTreeMap<String, Vec<ReviewGroup>>,
    pub equity: Vec<ReviewEquityPoint>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ReviewAggregateResponse {
    pub snapshot_id: String,
    pub groups: Vec<ReviewGroup>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ReviewLifecycleNode {
    pub sequence: usize,
    pub stage: String,
    pub occurred_at: String,
    pub known_at: String,
    pub recorded_at: String,
    pub origin: String,
    pub comment: String,
    pub evidence: serde_json::Value,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct ReviewExcursion {
    pub r: Option<f64>,
    pub amount: Option<f64>,
    pub price: Option<f64>,
    pub interval_start: Option<String>,
    pub interval_end: Option<String>,
    pub lower_r: Option<f64>,
    pub upper_r: Option<f64>,
    pub precision: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ReviewHoldingPoint {
    pub sequence: usize,
    pub interval_start: String,
    pub interval_end: String,
    pub price: f64,
    pub floating_r: Option<f64>,
    pub floating_amount: Option<f64>,
    pub mfe: ReviewExcursion,
    pub mae: ReviewExcursion,
    pub bars_held: usize,
    pub hold_score: Option<f64>,
    pub checks: serde_json::Value,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ReviewResultDetail {
    pub summary: ReviewSummary,
    pub profile: String,
    pub cost_version: String,
    pub source_minutes: usize,
    pub origin: String,
    pub input_hash: String,
    pub calendar_version: String,
    pub parameter_hash: String,
    pub computed_at: String,
    pub multiplier: Option<f64>,
    pub multiplier_source: String,
    pub entry_price: Option<f64>,
    pub exit_price: Option<f64>,
    pub cost: serde_json::Value,
    pub original_event: serde_json::Value,
    pub quality: Option<crate::quality::SignalQualityDto>,
    pub review_scores: Option<crate::quality::ReviewScoreDto>,
    pub lifecycle: Vec<ReviewLifecycleNode>,
    pub mfe: ReviewExcursion,
    pub mae: ReviewExcursion,
    pub holding_count: usize,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ReviewHoldingPage {
    pub event_id: i64,
    pub result_version: String,
    pub total: usize,
    pub items: Vec<ReviewHoldingPoint>,
    pub next_cursor: Option<usize>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct ReviewRebuildRequest {
    pub symbols: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ReviewJob {
    pub id: String,
    pub status: String,
    pub origin: String,
    pub total_symbols: usize,
    pub completed_symbols: usize,
    pub processed_signals: usize,
    pub coverage: BTreeMap<String, usize>,
    pub errors: Vec<String>,
    pub created_at: String,
    pub updated_at: String,
    pub revision: Option<i64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ReviewMetadata {
    pub profile: String,
    pub cost_version: String,
    pub timezone: String,
    pub revision: i64,
    pub symbols: Vec<String>,
    pub states: Vec<String>,
    pub exit_reasons: BTreeMap<String, String>,
    pub terminal_reasons: BTreeMap<String, String>,
    pub score_ranges: BTreeMap<String, [f64; 2]>,
    pub dimensions: Vec<String>,
    pub coverage: BTreeMap<String, usize>,
    pub jobs: Vec<ReviewJob>,
}
