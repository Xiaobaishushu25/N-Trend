/// Logistic stub — Phase 6 placeholder, pure Rust, no linfa heavy dep yet
/// Stores coefficients as JSON in model_registry; predict is available now for smoke tests
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct LogisticModel {
    pub intercept: f64,
    pub coefficients: Vec<f64>,
    pub feature_names: Vec<String>,
}

impl LogisticModel {
    pub fn new(feature_names: Vec<String>) -> Self {
        let n = feature_names.len();
        Self { intercept: 0.0, coefficients: vec![0.0; n], feature_names }
    }
    pub fn logit(&self, features: &[f64]) -> f64 {
        let mut s = self.intercept;
        for (c, f) in self.coefficients.iter().zip(features.iter()) { s += c * f; }
        s
    }
    pub fn predict_p(&self, features: &[f64]) -> f64 {
        let l = self.logit(features);
        1.0 / (1.0 + (-l).exp())
    }
}

pub fn predict_p(model: &LogisticModel, features: &[f64]) -> f64 { model.predict_p(features) }

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn sigmoid_at_zero_is_half() {
        let m = LogisticModel::new(vec!["x".into()]);
        assert!((m.predict_p(&[0.0]) - 0.5).abs() < 1e-9);
    }
}
