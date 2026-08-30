//! Finality Observation and Shadow Validator module.
//!
//! 零风险高频观测链与影子判定器，用于实测分析新浪 5m 接口在不同时段（普通盘中与各类收盘边界）的
//! 真实修改行为、候选确认判定（Candidate Final）有效性与最小结算期（minimum settle）参数。

pub mod analysis;
pub mod model;
pub mod observer;
pub mod tracker;

pub use analysis::{
    evaluate_sentinels, format_finality_report, format_simulation_table, simulate_strategies,
    summarize_trials, FinalityReport, SentinelEvaluationResult, StrategyDef,
    StrategySimulationResult,
};
pub use model::{
    BarFingerprint, FinalityTrial, ObservationRecord, SessionType, DEFAULT_SENTINELS,
};
pub use observer::{FinalityConfig, FinalityObserver};
pub use tracker::{BarFinalityTracker, ProbeResult};
