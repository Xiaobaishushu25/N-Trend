use crate::v2::dataset::DatasetRow;
use std::collections::HashMap;

#[derive(Debug, serde::Serialize)]
pub struct MissingReport {
    pub total: usize,
    pub missing_counts: HashMap<String, usize>,
    pub dropped_due_to_atr: usize,
    pub missing_rate: HashMap<String, f64>,
}

#[derive(Debug, serde::Serialize)]
pub struct DistributionReport {
    pub feature: String,
    pub p5: f64, pub p25: f64, pub p50: f64, pub p75: f64, pub p95: f64,
    pub mean: f64, pub std: f64,
}

pub fn missing_report(rows: &[DatasetRow]) -> MissingReport {
    let total = rows.len();
    let mut counts: HashMap<String, usize> = HashMap::new();
    let mut dropped = 0;
    for r in rows {
        if r.warning_volume_ratio.is_none() { *counts.entry("warning_volume_ratio".into()).or_insert(0) += 1; }
        if r.trigger_volume_ratio.is_none() { *counts.entry("trigger_volume_ratio".into()).or_insert(0) += 1; }
        if r.trigger_close_overshoot_r.is_none() { *counts.entry("trigger_close_overshoot_r".into()).or_insert(0) += 1; }
        if (r.missing_mask & 4) != 0 { dropped += 1; }
    }
    let mut rate = HashMap::new();
    for (k,v) in &counts { rate.insert(k.clone(), *v as f64 / total.max(1) as f64); }
    MissingReport{ total, missing_counts: counts, dropped_due_to_atr: dropped, missing_rate: rate }
}

pub fn distribution_reports(rows: &[DatasetRow]) -> Vec<DistributionReport> {
    let mut out = Vec::new();
    let feats: Vec<(&str, Vec<f64>)> = vec![
        ("a_move_atr", rows.iter().map(|r| r.a_move_atr).collect()),
        ("retracement", rows.iter().map(|r| r.retracement).collect()),
        ("trigger_close_overshoot_r", rows.iter().filter_map(|r| r.trigger_close_overshoot_r).collect()),
        ("trigger_close_location", rows.iter().filter_map(|r| r.trigger_close_location).collect()),
    ];
    for (name, mut vals) in feats {
        if vals.is_empty() { continue; }
        vals.sort_by(|a,b| a.partial_cmp(b).unwrap());
        let p = |q: f64| {
            let idx = ((vals.len() as f64 - 1.0) * q) as usize;
            vals[idx]
        };
        let mean = vals.iter().sum::<f64>()/vals.len() as f64;
        let var = vals.iter().map(|v| (v-mean).powi(2)).sum::<f64>()/vals.len() as f64;
        out.push(DistributionReport{ feature: name.into(), p5: p(0.05), p25: p(0.25), p50: p(0.5), p75: p(0.75), p95: p(0.95), mean, std: var.sqrt() });
    }
    out
}
