//! Futures Trading Session and Calendar module (Issue 06).
//!
//! 替代各层散落的硬编码交易时钟与固定时段逻辑，
//! 提供统一的品种交易日历、时段判定与收盘时刻规范。

pub mod calendar;
pub mod spec;

pub use calendar::{classify_night_session, SessionCalendar};
pub use spec::{NightSessionType, TradingSessionSpec};
