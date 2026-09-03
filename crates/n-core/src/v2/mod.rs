/// V2 Probabilistic Decision Pipeline
/// Phase 1: version contracts + state machine + feature/replay/dataset/model modules

pub mod state_machine;
pub mod features;
pub mod replay;
pub mod dataset;
pub mod model;
pub mod prediction;

/// Feature schema version — bump when any feature definition changes
pub const FEATURE_SCHEMA_VERSION: &str = "v2.1";
/// Pattern logic version — bump when N detection thresholds change
pub const PATTERN_LOGIC_VERSION: &str = "v2-strict-1";
/// Execution version — bump when aggregation or bar derivation rules change
pub const EXECUTION_VERSION: &str = "v2-exec-1";
/// Label contract version
pub const LABEL_CONTRACT_VERSION: &str = "v2-label-1";

/// V2 event state machine (Section 3 of the spec)
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub enum EventState {
    SetupDetected,
    WaitingTrigger,
    TriggerTouched,
    TriggerConfirmed,
    ModelDecided,
    Open,
    Skipped,
    Closed,
    Expired,
}

impl EventState {
    pub fn as_str(&self) -> &str {
        match self {
            Self::SetupDetected => "setup_detected",
            Self::WaitingTrigger => "waiting_trigger",
            Self::TriggerTouched => "trigger_touched",
            Self::TriggerConfirmed => "trigger_confirmed",
            Self::ModelDecided => "model_decided",
            Self::Open => "open",
            Self::Skipped => "skipped",
            Self::Closed => "closed",
            Self::Expired => "expired",
        }
    }
    pub fn from_str(s: &str) -> Option<Self> {
        match s {
            "setup_detected" => Some(Self::SetupDetected),
            "waiting_trigger" => Some(Self::WaitingTrigger),
            "trigger_touched" => Some(Self::TriggerTouched),
            "trigger_confirmed" => Some(Self::TriggerConfirmed),
            "model_decided" => Some(Self::ModelDecided),
            "open" => Some(Self::Open),
            "skipped" => Some(Self::Skipped),
            "closed" => Some(Self::Closed),
            "expired" => Some(Self::Expired),
            _ => None,
        }
    }
    /// Allowed transitions (strict)
    pub fn can_transition_to(&self, next: &Self) -> bool {
        matches!(
            (self, next),
            (Self::SetupDetected, Self::WaitingTrigger)
                | (Self::WaitingTrigger, Self::TriggerTouched)
                | (Self::WaitingTrigger, Self::Expired)
                | (Self::TriggerTouched, Self::TriggerConfirmed)
                | (Self::TriggerTouched, Self::Expired)
                | (Self::TriggerConfirmed, Self::ModelDecided)
                | (Self::ModelDecided, Self::Open)
                | (Self::ModelDecided, Self::Skipped)
                | (Self::Open, Self::Closed)
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn state_transitions_are_strict() {
        assert!(EventState::SetupDetected.can_transition_to(&EventState::WaitingTrigger));
        assert!(!EventState::SetupDetected.can_transition_to(&EventState::Open));
        assert!(EventState::ModelDecided.can_transition_to(&EventState::Skipped));
    }
}
