/// Logistic baseline — pure Rust hand-rolled gradient descent with L2, no external linfa dep.
/// Keeps zero Python / minimal crate footprint; metrics compatible with linfa behaviour.

use serde::{Deserialize, Serialize};
use crate::v2::dataset::DatasetRow;
use crate::v2::model::scaler::{StandardScaler, get_feature};
use crate::v2::model::metrics::{compute_metrics_with_baseline, Metrics};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct LogisticModel {
    pub intercept: f64,
    pub coefficients: Vec<f64>,
    pub feature_names: Vec<String>,
    pub scaler_means: Vec<f64>,
    pub scaler_stds: Vec<f64>,
}

impl LogisticModel {
    pub fn new(feature_names: Vec<String>) -> Self {
        let n = feature_names.len();
        Self { intercept: 0.0, coefficients: vec![0.0; n], feature_names: feature_names.clone(), scaler_means: vec![0.0; n], scaler_stds: vec![1.0; n] }
    }
    pub fn logit(&self, features: &[f64]) -> f64 {
        // features are already scaled if caller uses scaler; model stores raw coef on scaled space
        let mut s = self.intercept;
        for (c, f) in self.coefficients.iter().zip(features.iter()) { s += c * f; }
        s
    }
    pub fn predict_p(&self, features: &[f64]) -> f64 {
        let l = self.logit(features);
        1.0 / (1.0 + (-l).exp())
    }
    /// predict from raw row using internal scaler
    pub fn predict_row_p(&self, row: &DatasetRow) -> f64 {
        let mut scaled = Vec::with_capacity(self.feature_names.len());
        for (j, name) in self.feature_names.iter().enumerate() {
            let sv = match get_feature(row, name) {
                Some(v) => (v - self.scaler_means[j]) / self.scaler_stds[j].max(1e-9),
                // Match StandardScaler::transform_row: missing values are
                // already represented by the scaled-space zero.
                None => 0.0,
            };
            scaled.push(sv);
        }
        self.predict_p(&scaled)
    }
    pub fn feature_contributions(&self, row: &DatasetRow) -> Vec<FeatureContribution> {
        let mut out = Vec::new();
        for (j, name) in self.feature_names.iter().enumerate() {
            let v = get_feature(row, name);
            let sv = v.map(|value| (value - self.scaler_means[j]) / self.scaler_stds[j].max(1e-9)).unwrap_or(0.0);
            let contrib = self.coefficients[j] * sv;
            out.push(FeatureContribution { feature: name.clone(), value: v.unwrap_or(0.0), scaled_value: sv, coefficient: self.coefficients[j], contribution: contrib });
        }
        out
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct FeatureContribution {
    pub feature: String,
    pub value: f64,
    pub scaled_value: f64,
    pub coefficient: f64,
    pub contribution: f64,
}

pub fn predict_p(model: &LogisticModel, features: &[f64]) -> f64 { model.predict_p(features) }

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct TrainConfig {
    pub l2: f64,
    pub lr: f64,
    pub epochs: usize,
    pub verbose: bool,
}

impl Default for TrainConfig {
    fn default() -> Self { Self { l2: 1.0, lr: 0.05, epochs: 400, verbose: false } }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct TrainOutput {
    pub model: LogisticModel,
    pub scaler: StandardScaler,
    pub metrics_train: Metrics,
    pub metrics_valid: Option<Metrics>,
    pub dataset_hash: String,
}

pub fn train(rows: &[DatasetRow], feature_names: &[String], valid_rows: Option<&[DatasetRow]>, cfg: &TrainConfig) -> TrainOutput {
    let scaler = StandardScaler::fit(rows, feature_names);
    let x_train: Vec<Vec<f64>> = rows.iter().map(|r| scaler.transform_row(r)).collect();
    let y_train: Vec<i32> = rows.iter().map(|r| r.label_win).collect();
    let mut model = LogisticModel::new(feature_names.to_vec());
    model.scaler_means = scaler.means.clone();
    model.scaler_stds = scaler.stds.clone();

    if rows.is_empty() {
        let metrics_train = compute_metrics_with_baseline(&y_train, &vec![0.5; y_train.len()], 0.5);
        return TrainOutput { model, scaler, metrics_train, metrics_valid: None, dataset_hash: String::new() };
    }

    // init intercept to logit(prior)
    let prior = (y_train.iter().filter(|y| **y==1).count() as f64 / y_train.len() as f64).clamp(0.05, 0.95);
    model.intercept = (prior / (1.0 - prior)).ln();

    let n = rows.len() as f64;
    let d = feature_names.len();
    for epoch in 0..cfg.epochs {
        // compute grads over full batch
        let mut grad_intercept = 0.0;
        let mut grad_coef = vec![0.0; d];
        let mut _loss = 0.0;
        for (x, y) in x_train.iter().zip(y_train.iter()) {
            let logit = model.logit(x);
            let p = 1.0 / (1.0 + (-logit).exp());
            let pc = p.clamp(1e-15, 1.0 - 1e-15);
            let yf = *y as f64;
            _loss += if *y==1 { -pc.ln() } else { -(1.0-pc).ln() };
            let err = p - yf;
            grad_intercept += err;
            for j in 0..d { grad_coef[j] += err * x[j]; }
        }
        _loss /= n;
        // L2 penalty
        for j in 0..d { _loss += 0.5 * cfg.l2 * model.coefficients[j].powi(2) / n; }
        for j in 0..d { grad_coef[j] = grad_coef[j] / n + cfg.l2 * model.coefficients[j] / n; }
        grad_intercept /= n;
        // step
        model.intercept -= cfg.lr * grad_intercept;
        for j in 0..d { model.coefficients[j] -= cfg.lr * grad_coef[j]; }
        if cfg.verbose && epoch % 100 == 0 {
            // tracing::debug!("epoch {} loss {:.4}", epoch, loss);
        }
        // early stop if grad small
        if grad_coef.iter().map(|g| g.abs()).fold(0.0, f64::max) < 1e-7 && grad_intercept.abs() < 1e-7 { break; }
    }

    let p_train: Vec<f64> = x_train.iter().map(|x| model.predict_p(x)).collect();
    let metrics_train = compute_metrics_with_baseline(&y_train, &p_train, prior);
    let metrics_valid = if let Some(vr) = valid_rows {
        let x_valid: Vec<Vec<f64>> = vr.iter().map(|r| scaler.transform_row(r)).collect();
        let y_valid: Vec<i32> = vr.iter().map(|r| r.label_win).collect();
        let p_valid: Vec<f64> = x_valid.iter().map(|x| model.predict_p(x)).collect();
        Some(compute_metrics_with_baseline(&y_valid, &p_valid, prior))
    } else { None };

    TrainOutput { model: model.clone(), scaler, metrics_train, metrics_valid, dataset_hash: String::new() }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::v2::dataset::DatasetRow;
    fn mk_row(a_atr: f64, label: i32, ts: &str) -> DatasetRow {
        DatasetRow { event_id: ts.into(), symbol: "RB".into(), direction: "up".into(), setup_quality: 3.0, a_move: 10.0, b_move: 5.0, a_move_atr: a_atr, b_move_atr: 1.0, a_speed: 1.0, retracement: 0.5, warning_volume_ratio: Some(1.0), trigger_close_overshoot_r: Some(0.2), trigger_close_location: Some(0.5), trigger_body_atr: Some(1.0), trigger_volume_ratio: Some(1.0), trigger_wick_atr: Some(0.2), internal_swing_margin_r: Some(0.2), chase_distance_r: Some(0.1), missing_mask: 0, label_win: label, r_multiple: Some(1.0), is_1r_aux_win: Some(true), trigger_bar_ts: Some(ts.into()), exit_ts: Some("2025-01-02 10:00:00".into()), schema_version: crate::v2::FEATURE_SCHEMA_VERSION.into(), trend_gap_60:None, trend_slope_60:None, trend_strength_60:None, trend_alignment_60:None, trend_10d:None, trend_alignment_10d:None, range_position_10d:None, mr_position_10d:None, distance_ma10_dir:None, trend_position_interaction:None, context_as_of_ts:None, context_last_60m_ts:None, context_last_daily_day:None, crossed_rollover_10d:false }
    }
    #[test]
    fn sigmoid_at_zero_is_half() {
        let m = LogisticModel::new(vec!["x".into()]);
        assert!((m.predict_p(&[0.0]) - 0.5).abs() < 1e-9);
    }
    #[test]
    fn train_separates() {
        let rows: Vec<DatasetRow> = (0..20).map(|i| mk_row(if i<10 {1.0} else {5.0}, if i<10 {0} else {1}, &format!("2024-01-{:02} 10:00:00", i+1))).collect();
        let out = train(&rows, &["a_move_atr".into()], None, &TrainConfig::default());
        // model should assign positive coef to a_move_atr
        assert!(out.model.coefficients[0] > 0.0);
        assert!(out.metrics_train.auc > 0.8);
    }
    #[test]
    fn contributions_sum_to_logit() {
        let mut m = LogisticModel::new(vec!["a_move_atr".into(), "b_move_atr".into()]);
        m.intercept = 0.2;
        m.coefficients = vec![0.5, -0.3];
        m.scaler_means = vec![0.0, 0.0];
        m.scaler_stds = vec![1.0, 1.0];
        let row = mk_row(2.0, 1, "2024-01-01 10:00:00");
        // override a_move_atr=1.0 b_move_atr=2.0
        let mut r = row.clone();
        r.a_move_atr = 1.0; r.b_move_atr = 2.0;
        let contribs = m.feature_contributions(&r);
        let sum: f64 = contribs.iter().map(|c| c.contribution).sum::<f64>() + m.intercept;
        assert!((sum - m.logit(&[1.0, 2.0])).abs() < 1e-9);
    }

    #[test]
    fn missing_inference_matches_training_transform() {
        let rows = vec![mk_row(1.0, 0, "2024-01-01 10:00:00"), mk_row(3.0, 1, "2024-01-02 10:00:00")];
        let feature_names = vec!["a_move_atr".into(), "trigger_volume_ratio".into()];
        let scaler = StandardScaler::fit(&rows, &feature_names);
        let mut model = LogisticModel::new(feature_names.clone());
        model.intercept = 0.17;
        model.coefficients = vec![0.4, -0.8];
        model.scaler_means = scaler.means.clone();
        model.scaler_stds = scaler.stds.clone();
        let mut missing = rows[0].clone();
        missing.trigger_volume_ratio = None;
        let expected = model.predict_p(&scaler.transform_row(&missing));
        assert!((model.predict_row_p(&missing) - expected).abs() < 1e-12);
    }
}



