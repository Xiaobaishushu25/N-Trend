use std::{cell::RefCell, future::Future, time::Duration};

/// Nested wall-clock durations from one execution task. Parent phases
/// include the lock, strategy, persistence and broker time spent within them.
#[derive(Clone, Copy, Debug, Default)]
pub struct ExecutionTimings {
    pub drive_lock_wait: Duration,
    pub state_lock_wait: Duration,
    pub observe_quotes: Duration,
    pub strategy_handler: Duration,
    pub flush_quotes: Duration,
    pub drive_after_quotes: Duration,
    pub broker_requests: Duration,
    pub save_count: u64,
    pub save_total: Duration,
    pub sqlite_write: Duration,
    pub sqlite_commit: Duration,
}

tokio::task_local! {
    static TIMINGS: RefCell<ExecutionTimings>;
}

impl ExecutionTimings {
    /// Collect nested execution timings for a single consumer cycle.
    pub async fn measure<F: Future>(operation: F) -> (F::Output, Self) {
        TIMINGS
            .scope(RefCell::new(Self::default()), async {
                let result = operation.await;
                (result, TIMINGS.with(|timings| *timings.borrow()))
            })
            .await
    }
}

pub(super) fn record(update: impl FnOnce(&mut ExecutionTimings)) {
    let _ = TIMINGS.try_with(|timings| update(&mut timings.borrow_mut()));
}
