use super::*;
use n_protocol::trader_stream::SharedTraderSnapshot;
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
    conditions: Option<CachedField<HashMap<String, ConditionOrder>>>,
    positions: Option<CachedField<HashMap<String, ManagedPosition>>>,
    history: Option<CachedField<Vec<TradeRecord>>>,
    statistics: Arc<Value>,
    strategy_states: Option<CachedField<HashMap<String, Value>>>,
    entry_decisions: Option<CachedField<HashMap<String, Value>>>,
    symbol_slots: Option<CachedField<HashMap<String, String>>>,
}

impl ExecutionEngine {
    pub async fn snapshot_fields(
        &self,
        cache: &mut ExecutionSnapshotCache,
        include_strategy_details: bool,
    ) -> Result<SharedTraderSnapshot> {
        let cfg = self.config.read().await;
        let s = self.state.lock().await;
        let mut fields = SharedTraderSnapshot::default();
        fields.insert("config", serde_json::to_value(&*cfg)?);
        drop(cfg);
        let conditions = FieldUpdate::capture(&cache.conditions, &s.conditions);
        let positions = FieldUpdate::capture(&cache.positions, &s.positions);
        let history = FieldUpdate::capture(&cache.history, &s.history);
        fields.insert("execution_error", json!(s.execution_error()));
        fields.insert(
            "strategy_comparison",
            Self::comparison_summary_for_state(&s),
        );

        let calendar = self.calendar_health_for_state(&s, self.now_utc().timestamp());
        let details = if include_strategy_details {
            fields.insert("strategy", serde_json::to_value(&s.definition)?);
            fields.insert("paused", json!(s.paused));
            Some((
                FieldUpdate::capture(&cache.strategy_states, &s.strategy_states),
                FieldUpdate::capture(&cache.entry_decisions, &s.entry_decisions),
                FieldUpdate::capture(&cache.symbol_slots, &s.symbol_slots),
            ))
        } else {
            None
        };
        drop(s);

        fields.insert_shared(
            "conditions",
            conditions.render(&mut cache.conditions, |items| {
                serde_json::to_value(items.values().collect::<Vec<_>>())
            })?,
        );
        fields.insert_shared(
            "managed_positions",
            positions.render(&mut cache.positions, |items| {
                serde_json::to_value(items.values().collect::<Vec<_>>())
            })?,
        );
        let history = history.render(&mut cache.history, |data| {
            let history = serde_json::to_value(data)?;
            cache.statistics = Arc::new(serde_json::to_value(Self::trade_statistics(data))?);
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
