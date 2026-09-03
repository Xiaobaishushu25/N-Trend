use serde::{Deserialize, Serialize};
use crate::v2::dataset::DatasetRow;
use crate::v2::model::logistic::LogisticModel;
use crate::v2::model::gam::GamModel;
use crate::v2::model::scaler::get_feature;

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct InferenceBundle {
    pub model_id: String,
    pub feature_whitelist: Vec<String>,
    pub scaler_means: Vec<f64>,
    pub scaler_stds: Vec<f64>,
    pub logistic: Option<LogisticModel>,
    pub gam: Option<GamModel>,
    pub schema_version: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Prediction {
    pub event_id: String,
    pub model_id: String,
    pub p_win: f64,
    pub logit: f64,
    pub feature_hash: String,
    pub predicted_at: String,
    pub prediction_mode: String, // live / replay / shadow
    pub contributions: Vec<Contribution>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Contribution {
    pub feature: String,
    pub value: f64,
    pub coefficient: Option<f64>,
    pub contribution: f64,
}

pub fn feature_hash(row: &DatasetRow, whitelist: &[String]) -> String {
    let mut h = blake3::Hasher::new();
    h.update(whitelist.join(",").as_bytes());
    for name in whitelist {
        let v = get_feature(row, name).map(|x| x.to_string()).unwrap_or_else(|| "NULL".into());
        h.update(name.as_bytes());
        h.update(b"=");
        h.update(v.as_bytes());
        h.update(b";");
    }
    h.finalize().to_hex().to_string()
}

pub fn predict_logistic(bundle: &InferenceBundle, row: &DatasetRow) -> Option<Prediction> {
    let m = bundle.logistic.as_ref()?;
    let p = m.predict_row_p(row);
    let logit = m.logit(&m.feature_names.iter().enumerate().map(|(j, name)| {
        let v = get_feature(row, name).unwrap_or(0.0);
        (v - m.scaler_means[j]) / m.scaler_stds[j].max(1e-9)
    }).collect::<Vec<f64>>());
    let fh = feature_hash(row, &bundle.feature_whitelist);
    let contribs = m.feature_contributions(row).into_iter().map(|c| Contribution{ feature: c.feature, value: c.value, coefficient: Some(c.coefficient), contribution: c.contribution }).collect();
    Some(Prediction{ event_id: row.event_id.clone(), model_id: bundle.model_id.clone(), p_win: p, logit, feature_hash: fh, predicted_at: chrono::Utc::now().to_rfc3339(), prediction_mode: "live".into(), contributions: contribs })
}

pub fn predict_gam(bundle: &InferenceBundle, row: &DatasetRow) -> Option<Prediction> {
    let g = bundle.gam.as_ref()?;
    let p = g.predict_p(row);
    let logit = g.predict_logit(row);
    let fh = feature_hash(row, &bundle.feature_whitelist);
    let contribs = g.contributions(row).into_iter().map(|c| Contribution{ feature: c.feature, value: c.value, coefficient: None, contribution: c.contribution }).collect();
    Some(Prediction{ event_id: row.event_id.clone(), model_id: bundle.model_id.clone(), p_win: p, logit, feature_hash: fh, predicted_at: chrono::Utc::now().to_rfc3339(), prediction_mode: "live".into(), contributions: contribs })
}

pub fn bundle_to_json(bundle: &InferenceBundle) -> String {
    serde_json::to_string_pretty(bundle).unwrap_or_else(|_| "{}".into())
}
pub fn bundle_from_json(s: &str) -> Result<InferenceBundle, serde_json::Error> {
    serde_json::from_str(s)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::v2::dataset::DatasetRow;
    use crate::v2::model::logistic::LogisticModel;
    fn mk_row() -> DatasetRow {
        DatasetRow{ event_id:"e1".into(), symbol:"RB".into(), direction:"up".into(), setup_quality:3.0, a_move:10.0,b_move:5.0,a_move_atr:2.0,b_move_atr:1.0,a_speed:1.0,retracement:0.5, warning_volume_ratio:Some(1.0), trigger_close_overshoot_r:Some(0.2), trigger_close_location:Some(0.5), trigger_body_atr:Some(1.0), trigger_volume_ratio:Some(1.0), trigger_wick_atr:Some(0.2), internal_swing_margin_r:Some(0.2), chase_distance_r:Some(0.1), missing_mask:0,label_win:1, r_multiple:Some(1.0), is_1r_aux_win:Some(true), trigger_bar_ts:Some("2024-01-01 10:00:00".into()), exit_ts:Some("2024-01-02 10:00:00".into()), schema_version:"v2.1".into(), trend_gap_60:None, trend_slope_60:None, trend_strength_60:None, trend_alignment_60:None, trend_10d:None, trend_alignment_10d:None, range_position_10d:None, mr_position_10d:None, distance_ma10_dir:None, trend_position_interaction:None, context_as_of_ts:None, context_last_60m_ts:None, context_last_daily_day:None, crossed_rollover_10d:false }
    }
    #[test]
    fn hash_deterministic() {
        let r = mk_row();
        let h1 = feature_hash(&r, &["a_move_atr".into(), "retracement".into()]);
        let h2 = feature_hash(&r, &["a_move_atr".into(), "retracement".into()]);
        assert_eq!(h1, h2);
    }
    #[test]
    fn predict_logistic_smoke() {
        let r = mk_row();
        let mut m = LogisticModel::new(vec!["a_move_atr".into()]);
        m.scaler_means = vec![0.0]; m.scaler_stds = vec![1.0]; m.coefficients = vec![0.5]; m.intercept = 0.0;
        let bundle = InferenceBundle{ model_id:"logistic-v1".into(), feature_whitelist: vec!["a_move_atr".into()], scaler_means: vec![0.0], scaler_stds: vec![1.0], logistic: Some(m), gam: None, schema_version:"v2.1".into() };
        let p = predict_logistic(&bundle, &r).unwrap();
        assert!(p.p_win > 0.0 && p.p_win < 1.0);
    }
}
