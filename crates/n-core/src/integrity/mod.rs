//! Raw 5m Data Integrity and Gap Repair module (Issue 05).
//!
//! 提供针对 5m 事实源的完整性检测、理论时间序列映射、异常诊断与自愈修复功能。

pub mod checker;
pub mod repair;
pub mod schedule;

pub use checker::{GapRange, RawDataIntegrityChecker, SymbolIntegrityReport};
pub use repair::{IntegrityRepairer, RepairResult};
pub use schedule::{
    classify_night_session, contract_prefix, is_valid_5m_slot, next_expected_5m_slot,
    NightSessionType,
};
