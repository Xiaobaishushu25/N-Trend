/// GAM placeholder — low-DF quantile knots + linear interpolation lookup
/// Complements Logistic baseline; per-feature df 3-4, linear interpolation
use serde::{Deserialize, Serialize};
use crate::v2::dataset::DatasetRow;
use crate::v2::model::scaler::get_feature;
use crate::v2::model::metrics::{compute_metrics_with_baseline, Metrics};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct SplineTable {
    pub feature: String,
    pub knots: Vec<f64>,
    pub values: Vec<f64>, // f(knot) learned
    pub df: usize,
}

impl SplineTable {
    pub fn new(feature: String, knots: Vec<f64>, df: usize) -> Self {
        let n = knots.len();
        Self { feature, knots, values: vec![0.0; n], df }
    }
    /// linear interpolation lookup; clamp outside knots to edge value
    pub fn eval(&self, x: f64) -> f64 {
        if self.knots.is_empty() { return 0.0; }
        if x <= self.knots[0] { return self.values[0]; }
        if x >= self.knots[self.knots.len()-1] { return self.values[self.knots.len()-1]; }
        // binary search interval
        let mut lo = 0usize;
        let mut hi = self.knots.len()-1;
        while hi - lo > 1 {
            let mid = (lo + hi)/2;
            if x < self.knots[mid] { hi = mid; } else { lo = mid; }
        }
        let x0 = self.knots[lo];
        let x1 = self.knots[hi];
        let y0 = self.values[lo];
        let y1 = self.values[hi];
        if (x1 - x0).abs() < 1e-12 { return y0; }
        y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct GamModel {
    pub intercept: f64,
    pub splines: Vec<SplineTable>,
    pub linear_coefficients: Vec<f64>,
    pub linear_features: Vec<String>,
}

impl GamModel {
    pub fn new() -> Self { Self { intercept: 0.0, splines: vec![], linear_coefficients: vec![], linear_features: vec![] } }
    pub fn predict_logit(&self, row: &DatasetRow) -> f64 {
        let mut s = self.intercept;
        for (c, name) in self.linear_coefficients.iter().zip(self.linear_features.iter()) {
            if let Some(v) = get_feature(row, name) { s += c * v; }
        }
        for spl in &self.splines {
            if let Some(v) = get_feature(row, &spl.feature) { s += spl.eval(v); }
        }
        s
    }
    pub fn predict_p(&self, row: &DatasetRow) -> f64 {
        let l = self.predict_logit(row);
        1.0 / (1.0 + (-l).exp())
    }
    /// per-feature contributions for explanation
    pub fn contributions(&self, row: &DatasetRow) -> Vec<GamContribution> {
        let mut out = Vec::new();
        for (c, name) in self.linear_coefficients.iter().zip(self.linear_features.iter()) {
            if let Some(v) = get_feature(row, name) { out.push(GamContribution{ feature: name.clone(), value: v, contribution: c*v, kind: "linear".into() }); }
        }
        for spl in &self.splines {
            if let Some(v) = get_feature(row, &spl.feature) { out.push(GamContribution{ feature: spl.feature.clone(), value: v, contribution: spl.eval(v), kind: "spline".into() }); }
        }
        out
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct GamContribution {
    pub feature: String,
    pub value: f64,
    pub contribution: f64,
    pub kind: String,
}

impl Default for GamModel { fn default() -> Self { Self::new() } }

pub trait GamPredict {
    fn predict_p(&self, row: &DatasetRow) -> f64;
}

pub fn quantile_knots(values: &[f64], df: usize) -> Vec<f64> {
    if values.is_empty() { return vec![]; }
    let mut vs = values.to_vec();
    vs.sort_by(|a,b| a.partial_cmp(b).unwrap());
    let n_knots = df.max(2);
    let mut knots = Vec::new();
    for i in 0..n_knots {
        let q = i as f64 / (n_knots - 1) as f64;
        let idx = ((vs.len() as f64 - 1.0) * q).round() as usize;
        knots.push(vs[idx.min(vs.len()-1)]);
    }
    // dedup and ensure monotonic increase
    knots.dedup_by(|a,b| (*a - *b).abs() < 1e-9);
    if knots.len() < 2 { knots = vec![vs[0], vs[vs.len()-1]]; }
    knots
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct GamTrainConfig {
    pub df: usize,
    pub l2: f64,
    pub linear_features: Vec<String>,
    pub spline_features: Vec<String>,
}

impl Default for GamTrainConfig {
    fn default() -> Self {
        Self {
            df: 4,
            l2: 1.0,
            linear_features: vec!["direction_num".into()],
            spline_features: vec!["a_move_atr".into(), "retracement".into(), "trigger_close_overshoot_r".into(), "trigger_close_location".into()],
        }
    }
}

/// Very light GAM training: fit spline values via binned mean logit residual
/// 1) fit intercept to prior, 2) for each spline feature, estimate value at each knot as
///    mean logit residual within quantile bin. This is not full backfitting but gives
///    a stable low-DF baseline that preserves ordering and stays pure-Rust.
pub fn train_gam(rows: &[DatasetRow], cfg: &GamTrainConfig) -> (GamModel, Metrics) {
    if rows.is_empty() {
        let m = GamModel::new();
        let metrics = compute_metrics_with_baseline(&[], &[], 0.5);
        return (m, metrics);
    }
    let win_rate = rows.iter().filter(|r| r.label_win==1).count() as f64 / rows.len() as f64;
    let prior = win_rate.clamp(0.05, 0.95);
    let intercept = (prior/(1.0-prior)).ln();
    let mut model = GamModel { intercept, splines: vec![], linear_coefficients: vec![0.0; cfg.linear_features.len()], linear_features: cfg.linear_features.clone() };

    // Build knots for each spline feature
    for feat in &cfg.spline_features {
        let vals: Vec<f64> = rows.iter().filter_map(|r| get_feature(r, feat)).collect();
        if vals.is_empty() { continue; }
        let knots = quantile_knots(&vals, cfg.df);
        let mut table = SplineTable::new(feat.clone(), knots.clone(), cfg.df);
        // estimate value at each knot as mean residual logit in neighbourhood
        // residual logit = logit(empirical win rate in bin) - intercept
        // bin assignment by nearest knot interval
        for (k_idx, _knot) in knots.iter().enumerate() {
            // collect rows where feature close to knot: we use quantile bin windows
            // simple: rows whose feature quantile bin == k_idx
            let mut bin_rows: Vec<&DatasetRow> = Vec::new();
            for r in rows {
                if let Some(v) = get_feature(r, feat) {
                    // assign to nearest knot index by distance
                    let mut best = 0usize;
                    let mut bestd = f64::INFINITY;
                    for (i, kk) in knots.iter().enumerate() { let d=(v-kk).abs(); if d<bestd { bestd=d; best=i; } }
                    if best==k_idx { bin_rows.push(r); }
                }
            }
            if bin_rows.is_empty() {
                table.values[k_idx] = 0.0;
            } else {
                let wr = bin_rows.iter().filter(|r| r.label_win==1).count() as f64 / bin_rows.len() as f64;
                let wrc = wr.clamp(0.05, 0.95);
                let logit_bin = (wrc/(1.0-wrc)).ln();
                let val = logit_bin - intercept;
                // shrink via L2: val * n/(n+l2)
                let n = bin_rows.len() as f64;
                table.values[k_idx] = val * n / (n + cfg.l2);
            }
        }
        // center spline to zero mean to keep intercept identifiable
        let mean_val = table.values.iter().sum::<f64>() / table.values.len() as f64;
        for v in &mut table.values { *v -= mean_val; }
        model.splines.push(table);
    }

    let y_true: Vec<i32> = rows.iter().map(|r| r.label_win).collect();
    let p_pred: Vec<f64> = rows.iter().map(|r| model.predict_p(r)).collect();
    let metrics = compute_metrics_with_baseline(&y_true, &p_pred, win_rate);
    (model, metrics)
}

/// Ablation groups in order §32: Base -> Setup -> Warning -> Trigger -> Volume -> OI
pub fn ablation_groups() -> Vec<(String, Vec<String>)> {
    vec![
        ("Base".into(), vec!["a_move_atr".into()]),
        ("Setup".into(), vec!["a_move_atr".into(), "b_move_atr".into(), "a_speed".into(), "retracement".into()]),
        ("Warning".into(), vec!["warning_volume_ratio".into()]),
        ("Trigger".into(), vec!["trigger_close_overshoot_r".into(), "trigger_close_location".into(), "trigger_body_atr".into(), "trigger_wick_atr".into(), "internal_swing_margin_r".into(), "chase_distance_r".into()]),
        ("Volume".into(), vec!["trigger_volume_ratio".into()]),
        ("OI".into(), vec!["oi_ratio".into()]),
    ]
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn spline_interp_linear() {
        let mut t = SplineTable::new("x".into(), vec![0.0, 1.0, 2.0], 3);
        t.values = vec![0.0, 1.0, 2.0];
        assert!((t.eval(0.5) - 0.5).abs() < 1e-9);
        assert!((t.eval(1.5) - 1.5).abs() < 1e-9);
        assert!((t.eval(-1.0) - 0.0).abs() < 1e-9);
        assert!((t.eval(10.0) - 2.0).abs() < 1e-9);
    }
    #[test]
    fn quantile_knots_basic() {
        let v: Vec<f64> = (0..100).map(|x| x as f64).collect();
        let k = quantile_knots(&v, 4);
        assert_eq!(k.len(), 4);
        assert!(k[0] < k[1] && k[1] < k[2]);
    }
    #[test]
    fn gam_train_smoke() {
        use crate::v2::dataset::DatasetRow;
        let rows: Vec<DatasetRow> = (0..20).map(|i| DatasetRow{ event_id: format!("e{}",i), symbol:"RB".into(), direction:"up".into(), setup_quality:3.0, a_move:10.0,b_move:5.0,a_move_atr: if i<10 {1.0} else {5.0}, b_move_atr:1.0,a_speed:1.0,retracement:0.5, warning_volume_ratio:Some(1.0), trigger_close_overshoot_r:Some(0.2), trigger_close_location:Some(0.5), trigger_body_atr:Some(1.0), trigger_volume_ratio:Some(1.0), trigger_wick_atr:Some(0.2), internal_swing_margin_r:Some(0.2), chase_distance_r:Some(0.1), missing_mask:0, label_win: if i<10 {0} else {1}, r_multiple:Some(1.0), is_1r_aux_win:Some(true), trigger_bar_ts:Some(format!("2024-01-{:02} 10:00:00", i+1)), exit_ts:Some("2024-01-20 11:00:00".into()), schema_version:"v2.1".into() }).collect();
        let (m, metrics) = train_gam(&rows, &GamTrainConfig::default());
        assert!(metrics.auc >= 0.5);
        assert!(!m.splines.is_empty());
    }
}

