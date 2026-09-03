//! Version contracts shared by replay, live inference, datasets and reports.
//!
//! Keep these values in one place.  A change to any of the contracts below
//! creates a new cohort and must never be hidden behind a scattered literal.

/// Event/state-machine contract.  Bumped for the second-stage causal cohort.
pub const EVENT_LOGIC_VERSION: &str = "6";
/// Market-context feature contract.  RR is intentionally not part of this
/// schema yet.
pub const FEATURE_SCHEMA_VERSION: &str = "2";
/// Execution/label-resolution contract.
pub const EXECUTION_LOGIC_VERSION: &str = "6";

/// Existing pattern and label contracts remain separately versioned.
pub const PATTERN_LOGIC_VERSION: &str = "v2-strict-1";
pub const LABEL_CONTRACT_VERSION: &str = "v2-label-1";
