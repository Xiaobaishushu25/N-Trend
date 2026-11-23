use super::*;
use crate::trader::trade_time::{resolve, split_timestamp, TradeTimeEvidence};

/// Offline-only migration: overlay owned fields and preserve unknown archive data.
pub fn normalize_archive(
    mut original: serde_json::Value,
    now: i64,
    opens: &[serde_json::Value],
) -> Result<serde_json::Value> {
    let mut state: ExecutionState = serde_json::from_value(original.clone())?;
    state.normalize_legacy_trade_times();
    for snapshot in state.overnight_snapshots.values_mut() {
        snapshot.observe_next_session(None, now, now, None);
        for open in opens {
            if let (Some(instrument), Some(time), Some(price)) = (
                open["instrument_id"].as_str(),
                open["open_ts"].as_str(),
                open["price"].as_f64(),
            ) {
                snapshot.backfill_contract_open(instrument, time, price);
                if let (Some(close_ts), Some(close)) = (
                    open["previous_close_ts"].as_str(),
                    open["previous_close"].as_f64(),
                ) {
                    snapshot.backfill_contract_close(instrument, close_ts, close);
                }
            }
        }
    }
    let normalized = serde_json::to_value(state)?;
    for key in [
        "positions",
        "history",
        "overnight_snapshots",
        "strategy_states",
        "trade_times",
        "time_normalization_version",
    ] {
        original[key] = normalized[key].clone();
    }
    Ok(original)
}

impl ExecutionState {
    fn set_position_natural_time(&mut self, id: &str, timestamp: &str) {
        let Some(position) = self.positions.get_mut(id) else {
            return;
        };
        let date_changed = super::super::candidates::parse_time(&position.open_ts).ok()
            != super::super::candidates::parse_time(timestamp).ok();
        position.open_ts = timestamp.into();
        // Missing historical quotes cannot be recreated by a timestamp repair.
        // Do not manufacture a zero-MFE no-follow failure during recovery.
        if date_changed {
            self.p3_stall.invalidate(id, "entry_time_corrected");
        }
        if date_changed && !position.is_closed {
            self.p3_runner.invalidate_entry(id, "entry_time_corrected");
            for state in self.strategy_states.values_mut().filter(|v| v.is_object()) {
                if state.get("holding").is_some() {
                    state["holding"][id] =
                        serde_json::json!({"buckets":{},"no_follow_checked":true});
                    state["time_recovery"][id] = serde_json::json!({"reason":"entry_time_corrected_missing_quote_history","open_ts":timestamp});
                }
            }
        }
        for record in self
            .history
            .iter_mut()
            .filter(|h| h.condition_order_id == id)
        {
            record.open_ts = timestamp.into();
        }
        if let Some(snapshot) = self.overnight_snapshots.get_mut(id) {
            snapshot.open_ts = timestamp.into();
        }
    }

    pub(super) fn repair_trade_time(
        &mut self,
        key: &str,
        trade: &TraderTradeRecord,
        time: &TradeTimeEvidence,
    ) {
        let Some(timestamp) = time.timestamp.as_deref() else {
            return;
        };
        if trade.offset == "open" {
            if let Some(order) = self.orders.get(&trade.order_ref).filter(|o| {
                o.reason.is_none()
                    && o.request
                        .instrument_id
                        .eq_ignore_ascii_case(&trade.instrument_id)
            }) {
                let id = order.condition_id.clone();
                // Preserve the first execution timestamp when later partial fills arrive.
                if let Some(position) = self.positions.get(&id) {
                    let old = super::super::candidates::parse_time(&position.open_ts).ok();
                    let new = super::super::candidates::parse_time(timestamp).ok();
                    if old.is_none() || new < old {
                        self.set_position_natural_time(&id, timestamp);
                    }
                }
            }
        } else {
            self.p3_runner.repair_intervention_time(key, timestamp);
            for history in self.history.iter_mut().filter(|h| h.id == key) {
                history.exit_ts = timestamp.into();
                if let Some(snapshot) = self
                    .overnight_snapshots
                    .get_mut(&history.condition_order_id)
                {
                    snapshot.actual_exit_ts = Some(timestamp.into());
                }
            }
        }
    }

    pub(super) fn normalize_legacy_trade_times(&mut self) {
        if self.time_normalization_version >= 1 {
            return;
        }
        for position in self.positions.values().cloned().collect::<Vec<_>>() {
            let entry = self
                .orders
                .values()
                .filter(|o| {
                    o.condition_id == position.id && o.reason.is_none() && o.submitted_at > 0
                })
                .min_by_key(|o| o.submitted_at);
            let (raw_date, raw_time) = split_timestamp(&position.open_ts);
            let day = entry.map(|o| o.trading_day.as_str()).unwrap_or("");
            let resolved = resolve(
                &position.instrument_id,
                raw_date,
                raw_time,
                day,
                entry.map(|o| o.submitted_at),
            );
            if let Some(timestamp) = &resolved.timestamp {
                self.set_position_natural_time(&position.id, timestamp);
            }
            self.trade_times
                .insert(format!("legacy-position:{}", position.id), resolved);
        }
        for index in 0..self.history.len() {
            let record = self.history[index].clone();
            if !self.positions.contains_key(&record.condition_order_id) {
                let entry = self
                    .orders
                    .values()
                    .filter(|o| {
                        o.condition_id == record.condition_order_id
                            && o.reason.is_none()
                            && o.submitted_at > 0
                    })
                    .min_by_key(|o| o.submitted_at);
                let (day, time) = split_timestamp(&record.open_ts);
                let resolved = resolve(
                    &record.instrument_id,
                    day,
                    time,
                    entry.map(|o| o.trading_day.as_str()).unwrap_or(""),
                    entry.map(|o| o.submitted_at),
                );
                if let Some(timestamp) = &resolved.timestamp {
                    self.history[index].open_ts = timestamp.clone();
                }
                self.trade_times
                    .insert(format!("legacy-entry:{}", record.id), resolved);
            }
            let (raw_date, raw_time) = split_timestamp(&record.exit_ts);
            let day = record
                .id
                .split(':')
                .next()
                .filter(|s| s.len() == 8 && s.bytes().all(|b| b.is_ascii_digit()))
                .unwrap_or("");
            // Calendar + this trade's trading day, never the position's opening day.
            let resolved = resolve(&record.instrument_id, raw_date, raw_time, day, None);
            if let Some(timestamp) = &resolved.timestamp {
                self.history[index].exit_ts = timestamp.clone();
                if let Some(snapshot) = self.overnight_snapshots.get_mut(&record.condition_order_id)
                {
                    snapshot.actual_exit_ts = Some(timestamp.clone());
                }
            }
            self.trade_times
                .insert(format!("legacy-exit:{}", record.id), resolved);
        }
        self.time_normalization_version = 1;
    }
}
