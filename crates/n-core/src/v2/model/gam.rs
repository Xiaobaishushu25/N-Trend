/// GAM placeholder — trait + SplineTable structure for Phase 7
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct SplineTable {
    pub feature: String,
    pub knots: Vec<f64>,
    pub coefficients: Vec<f64>,
    pub df: usize,
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
    /// Placeholder predict — sums linear terms only until splines are populated
    pub fn predict_p(&self, linear_features: &[f64]) -> f64 {
        let mut s = self.intercept;
        for (c, f) in self.linear_coefficients.iter().zip(linear_features.iter()) { s += c * f; }
        // TODO: evaluate splines via lookup/interpolation when populated
        1.0 / (1.0 + (-s).exp())
    }
}

impl Default for GamModel { fn default() -> Self { Self::new() } }

pub trait GamPredict {
    fn predict_p(&self, features: &[f64]) -> f64;
}
