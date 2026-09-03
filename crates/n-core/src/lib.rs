//! ntrend core library: fetching, persistence, aggregation, analysis and scheduling.

pub mod analyze;
pub mod config;
pub mod derive;
pub mod fetch;
pub mod finality;
pub mod integrity;
pub mod notify;
pub mod precision;
pub mod process;
pub mod scheduler;
pub mod session;
pub mod service;
pub mod storage;

pub mod v2;
pub use sea_orm;

pub use derive::Timeframe;

