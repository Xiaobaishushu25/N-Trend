//! ntrend core library: fetching, persistence, aggregation, analysis and scheduling.

pub mod analyze;
pub mod config;
pub mod derive;
pub mod fetch;
pub mod finality;
pub mod notify;
pub mod precision;
pub mod scheduler;
pub mod service;
pub mod storage;

pub use derive::Timeframe;
