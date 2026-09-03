pub mod logistic;
pub mod gam;
pub mod scaler;
pub mod metrics;
pub mod walk_forward;
pub mod inference;
pub use logistic::{LogisticModel, predict_p, TrainConfig, TrainOutput, train as train_logistic};
pub use gam::{GamModel, SplineTable, GamTrainConfig, train_gam, quantile_knots};
pub use scaler::{StandardScaler, get_feature};
pub use metrics::{Metrics, CalibrationBucket, compute_metrics, compute_metrics_with_baseline, brier_score, logloss, auc, top20_lift};
pub use walk_forward::{walk_forward, walk_forward_purge_aware, assert_purge, split_final_holdout, Fold};
pub use inference::{InferenceBundle, Prediction, feature_hash, predict_logistic, predict_gam};

