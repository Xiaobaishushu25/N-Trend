use super::*;
impl ExecutionEngine {
    /// Set before use; never changes the clock of other/live executors.
    pub fn with_clock(mut self, clock: Arc<dyn ExecutionClock>) -> Self {
        self.clock = clock;
        self
    }
    pub(super) fn now_utc(&self) -> chrono::DateTime<chrono::Utc> {
        self.clock.now_utc()
    }
    pub(super) fn now_local(&self) -> chrono::DateTime<chrono::FixedOffset> {
        self.clock.now_local()
    }
    /// Read-only audit export, deliberately limited to the non-network replay transport.
    pub async fn replay_evidence(&self) -> Result<serde_json::Value> {
        anyhow::ensure!(
            matches!(self.client, BrokerClient::Replay(_)),
            "Not a replay executor"
        );
        let s = self.state.lock().await;
        let mut value = serde_json::to_value(&*s)?;
        value["replay_runtime"] = serde_json::json!({"broker_ready":s.broker_ready,"broker_connected":s.broker_connected,
            "error":s.error,"market_error":s.market_error,"entry_symbols_ready":s.entry_symbols_ready,
            "last_action_at":s.last_action_at,"quotes":s.quotes});
        Ok(value)
    }
    pub async fn replay_condition_status(&self) -> Vec<serde_json::Value> {
        let s = self.state.lock().await;
        let mut conditions = s.conditions.values().collect::<Vec<_>>();
        conditions.sort_by(|a, b| a.id.cmp(&b.id));
        conditions
            .into_iter()
            .map(|c| {
                serde_json::json!({"id":c.id,"signal_id":c.origin.signal_id,
            "state":c.state,"note":c.note,"trigger_ts":c.trigger_ts})
            })
            .collect()
    }
}
