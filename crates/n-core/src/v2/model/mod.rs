pub mod gam;
pub mod inference;
pub mod logistic;
pub mod metrics;
pub mod scaler;
pub mod walk_forward;
pub use gam::{quantile_knots, train_gam, GamModel, GamTrainConfig, SplineTable};
pub use inference::{feature_hash, predict_gam, predict_logistic, InferenceBundle, Prediction};
pub use logistic::{predict_p, train as train_logistic, LogisticModel, TrainConfig, TrainOutput};
pub use metrics::{
    auc, brier_score, compute_metrics, compute_metrics_with_baseline, logloss, top20_lift,
    CalibrationBucket, Metrics,
};
pub use scaler::{get_feature, StandardScaler};
pub use walk_forward::{
    assert_purge, split_final_holdout, walk_forward, walk_forward_purge_aware, Fold,
};
