pub mod logistic;
pub mod gam;
pub use logistic::{LogisticModel, predict_p};
pub use gam::{GamModel, SplineTable};
