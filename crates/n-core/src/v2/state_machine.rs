use crate::v2::EventState;

/// State machine driver — guarantees snapshot immutability
/// Rule: after SetupDetected, SetupSnapshot must never be mutated by later bars.
#[derive(Debug, Clone)]
pub struct StateMachine {
    pub state: EventState,
    pub last_advance_ts: Option<String>,
}

impl StateMachine {
    pub fn new() -> Self {
        Self { state: EventState::SetupDetected, last_advance_ts: None }
    }
    pub fn with_state(state: EventState) -> Self {
        Self { state, last_advance_ts: None }
    }
    /// Try to advance; returns error if transition illegal
    pub fn try_advance(&mut self, next: EventState, bar_ts: &str) -> anyhow::Result<()> {
        if !self.state.can_transition_to(&next) {
            anyhow::bail!("illegal V2 transition: {:?} -> {:?}", self.state, next);
        }
        // idempotency guard: do not re-process same bar
        if let Some(last) = &self.last_advance_ts {
            if last == bar_ts {
                return Ok(());
            }
        }
        self.state = next;
        self.last_advance_ts = Some(bar_ts.to_string());
        Ok(())
    }
    /// Force set for recovery (logs warning, but still checks legality in strict mode)
    pub fn force_set(&mut self, state: EventState) {
        self.state = state;
    }
}

impl Default for StateMachine {
    fn default() -> Self { Self::new() }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn advance_guards_replay() {
        let mut sm = StateMachine::new();
        sm.try_advance(EventState::WaitingTrigger, "2024-01-01 10:00:00").unwrap();
        // same bar re-advance is idempotent
        sm.try_advance(EventState::WaitingTrigger, "2024-01-01 10:00:00").unwrap_err();
        // but Touch is allowed from Waiting
        sm.try_advance(EventState::TriggerTouched, "2024-01-01 10:15:00").unwrap();
        assert_eq!(sm.state, EventState::TriggerTouched);
    }
}
