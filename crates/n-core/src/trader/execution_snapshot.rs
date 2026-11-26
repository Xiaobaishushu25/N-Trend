use super::*;
use super::execution_settlement::ReconciledFee;
use n_protocol::trader_stream::{ConditionRows, SharedTraderSnapshot};
use serde_json::{json, Value};

struct CachedField<T> {
    data: T,
    json: Arc<Value>,
}

enum FieldUpdate<T> {
    Reuse(Arc<Value>),
    Changed(T),
}

impl<T: Clone + PartialEq + serde::Serialize> FieldUpdate<T> {
    fn capture(cache: &Option<CachedField<T>>, data: &T) -> Self {
        if let Some(cached) = cache.as_ref().filter(|cached| cached.data == *data) {
            Self::Reuse(cached.json.clone())
        } else {
            Self::Changed(data.clone())
        }
    }

    fn render(
        self,
        cache: &mut Option<CachedField<T>>,
        project: impl FnOnce(&T) -> serde_json::Result<Value>,
    ) -> Result<Arc<Value>> {
        match self {
            Self::Reuse(json) => Ok(json),
            Self::Changed(data) => {
                let json = Arc::new(project(&data)?);
                *cache = Some(CachedField {
                    data,
                    json: json.clone(),
                });
                Ok(json)
            }
        }
    }

    fn render_json(self, cache: &mut Option<CachedField<T>>) -> Result<Arc<Value>> {
        self.render(cache, |data| serde_json::to_value(data))
    }
}

/// Owned by a single account publisher. Compare typed fields under the
/// execution lock and serialize changed fields after releasing it.
#[derive(Default)]
pub struct ExecutionSnapshotCache {
    conditions: SharedState<HashMap<String, ConditionOrder>>,
    condition_rows: Arc<ConditionRows>,
    positions: Option<CachedField<SharedState<HashMap<String, ManagedPosition>>>>,
    history: Option<CachedField<(SharedState<Vec<TradeRecord>>, Option<Arc<HashMap<String, ReconciledFee>>>)>>,
    statistics: Arc<Value>,
    strategy_states: Option<CachedField<SharedState<HashMap<String, Value>>>>,
    entry_decisions: Option<CachedField<SharedState<HashMap<String, Value>>>>,
    symbol_slots: Option<CachedField<HashMap<String, String>>>,
}

impl ExecutionState {
    fn close_progress(&self, now: i64) -> Value {
        let mut rows = Vec::new();
        for (id, task) in &self.closes {
            let Some(position) = self.positions.get(id).filter(|position| !position.is_closed)
            else {
                continue;
            };
            let remaining = (task.target - task.filled).min(position.volume);
            if remaining <= 0 {
                continue;
            }
            let mut reserved = 0i32;
            let mut pending = None;
            for reference in &self.unsettled_orders {
                let Some(order) = self
                    .orders
                    .get(reference)
                    .filter(|order| order.condition_id == *id && order.reason.is_some())
                else {
                    continue;
                };
                let quantity = order.reserved_quantity();
                if quantity == 0 {
                    continue;
                }
                reserved = reserved.saturating_add(quantity);
                let (priority, status) = match order.phase {
                    OrderPhase::Unknown if order.cancel_attempts >= 3 => (5, "cancel_unconfirmed"),
                    OrderPhase::CancelPending => (4, "waiting_cancel"),
                    OrderPhase::Unknown => (3, "waiting_ack"),
                    OrderPhase::Terminal | OrderPhase::Rejected => (2, "waiting_fills"),
                    OrderPhase::Working => (1, "working"),
                };
                let key = (priority, order.action_at, reference);
                if pending.as_ref().is_none_or(|(latest, _, _)| key > *latest) {
                    pending = Some((key, status, order));
                }
            }
            let unreserved = (remaining - reserved).max(0);
            let (status, reference, detail) = if let Some(error) =
                self.persistence_error.as_deref().or(self.error.as_deref())
            {
                ("blocked", None, Some(error))
            } else if let Some(((_, _, reference), status, order)) =
                pending.filter(|_| unreserved == 0)
            {
                (status, Some(reference.as_str()), Some(order.note.as_str()))
            } else if !self.broker_ready || !self.broker_connected {
                ("waiting_broker", None, None)
            } else if self.market_error.is_some()
                || !self
                    .quotes
                    .get(&position.instrument_id.to_uppercase())
                    .is_some_and(|quote| quote.valid_at(now))
            {
                ("waiting_quote", None, self.market_error.as_deref())
            } else {
                let rejections = self.rejected_submission_state();
                let rejection = rejections
                    .latest_exit_rejections
                    .get(id)
                    .and_then(|reference| self.orders.get_key_value(reference));
                match rejection {
                    Some((reference, order)) => {
                        ("rejected", Some(reference.as_str()), Some(order.note.as_str()))
                    }
                    None => ("waiting_submit", None, None),
                }
            };
            rows.push(json!({
                "position_id": id,
                "instrument_id": position.instrument_id,
                "direction": position.direction,
                "remaining": remaining,
                "reserved": reserved.min(remaining),
                "unreserved": unreserved,
                "status": status,
                "order_ref": reference,
                "detail": detail,
            }));
        }
        rows.sort_unstable_by(|left, right| {
            left["instrument_id"]
                .as_str()
                .cmp(&right["instrument_id"].as_str())
                .then_with(|| left["position_id"].as_str().cmp(&right["position_id"].as_str()))
        });
        Value::Array(rows)
    }
}

impl ExecutionEngine {
    pub async fn snapshot_fields(
        &self,
        cache: &mut ExecutionSnapshotCache,
        include_strategy_details: bool,
    ) -> Result<SharedTraderSnapshot> {
        let cfg = self.config.read().await;
        let fee_projection = self.settlement_projection.read().await.clone();
        let s = self.state.lock().await;
        let mut fields = SharedTraderSnapshot::default();
        fields.insert("config", serde_json::to_value(&*cfg)?);
        drop(cfg);
        let conditions = s.conditions.clone();
        let positions = FieldUpdate::capture(&cache.positions, &s.positions);
        let settlement_rows = fee_projection.as_ref()
            .filter(|projection| projection.belongs_to(s.account.as_deref()))
            .map(|projection| projection.rows.clone());
        let history = FieldUpdate::capture(&cache.history, &(s.history.clone(), settlement_rows));
        fields.insert("execution_error", json!(s.execution_error()));
        fields.insert(
            "strategy_comparison",
            Self::comparison_summary_for_state(&s),
        );

        let now = self.now_utc().timestamp();
        let calendar = self.calendar_health_for_state(&s, now);
        let details = if include_strategy_details {
            fields.insert("strategy", serde_json::to_value(&s.definition)?);
            fields.insert("paused", json!(s.paused));
            fields.insert("close_progress", s.close_progress(now));
            Some((
                FieldUpdate::capture(&cache.strategy_states, &s.strategy_states),
                FieldUpdate::capture(&cache.entry_decisions, &s.entry_decisions),
                FieldUpdate::capture(&cache.symbol_slots, &s.symbol_slots),
            ))
        } else {
            None
        };
        drop(s);

        if !Arc::ptr_eq(&cache.conditions.0, &conditions.0) {
            let updates = conditions
                .iter()
                .filter(|(id, row)| cache.conditions.get(*id) != Some(*row))
                .map(|(id, row)| Ok((id.clone(), Arc::from(serde_json::value::to_raw_value(row)?))))
                .collect::<Result<Vec<_>>>()?;
            if !updates.is_empty() || cache.conditions.len() != conditions.len() {
                let rows = Arc::make_mut(&mut cache.condition_rows);
                rows.retain(|id, _| conditions.contains_key(id));
                rows.extend(updates);
            }
            cache.conditions = conditions;
        }
        fields.insert_conditions(cache.condition_rows.clone());
        fields.insert_shared(
            "managed_positions",
            positions.render(&mut cache.positions, |items| {
                serde_json::to_value(items.values().collect::<Vec<_>>())
            })?,
        );
        let history = history.render(&mut cache.history, |(data, fees)| {
            let mut history = serde_json::to_value(data)?;
            let mut statistics = serde_json::to_value(Self::trade_statistics(data))?;
            if let Some(fees) = fees {
                let mut confirmed = 0;
                let (mut commission, mut net_pnl) = (0_i64, 0_i64);
                for (record, json) in data.iter().zip(history.as_array_mut().unwrap()) {
                    if let Some(fee) = fees.get(&record.id) {
                        json["fee_reconciliation"] = serde_json::to_value(fee)?;
                        if fee.status == "confirmed" {
                            confirmed += 1;
                            commission += fee.commission_cents.unwrap_or(0);
                            net_pnl += fee.net_pnl_cents.unwrap_or(0);
                        }
                    }
                }
                statistics["fee_reconciliation"] = json!({
                    "confirmed_count": confirmed,
                    "total_count": data.len(),
                    "commission_cents": (confirmed > 0).then_some(commission),
                    "net_pnl_cents": (confirmed > 0).then_some(net_pnl),
                });
            }
            cache.statistics = Arc::new(statistics);
            Ok(history)
        })?;
        fields.insert_shared("history", history);
        fields.insert_shared("statistics", cache.statistics.clone());
        let mut diagnostics = SharedTraderSnapshot::default();
        diagnostics.insert("calendar_health", json!(calendar));
        if let Some((states, entry_decisions, symbol_slots)) = details {
            diagnostics.insert_shared("states", states.render_json(&mut cache.strategy_states)?);
            diagnostics.insert_shared(
                "entry_decisions",
                entry_decisions.render_json(&mut cache.entry_decisions)?,
            );
            diagnostics.insert_shared(
                "symbol_slots",
                symbol_slots.render_json(&mut cache.symbol_slots)?,
            );
        }
        fields.insert_object("strategy_diagnostics", diagnostics);
        Ok(fields)
    }
}
