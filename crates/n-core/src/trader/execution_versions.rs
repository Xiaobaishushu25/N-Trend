use super::*;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub(super) struct SavedRevision {
    pub definition: StrategyDefinition,
    pub execution: ExecutionConfig,
    pub effective_at: Option<i64>,
    pub previous: Option<RevisionIdentity>,
    pub note: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub(super) struct RevisionIdentity {
    pub strategy_id: String,
    pub revision: String,
}

impl From<&StrategyDefinition> for RevisionIdentity {
    fn from(definition: &StrategyDefinition) -> Self {
        Self {
            strategy_id: definition.strategy_id.clone(),
            revision: definition.revision.clone(),
        }
    }
}

#[derive(Clone, PartialEq)]
pub(super) struct RevisionSource {
    active: StrategyDefinition,
    execution: ExecutionConfig,
    changes: SharedState<Vec<SavedRevision>>,
    conditions: SharedState<HashMap<String, ConditionOrder>>,
    history: SharedState<Vec<TradeRecord>>,
}

#[derive(Serialize)]
struct RevisionRow {
    #[serde(flatten)]
    identity: RevisionIdentity,
    definition: Option<StrategyDefinition>,
    execution: Option<ExecutionConfig>,
    effective_at: Option<i64>,
    first_condition_at: Option<i64>,
    first_fill_at: Option<i64>,
    previous: Option<RevisionIdentity>,
    note: Option<String>,
    #[serde(skip)]
    saved_order: usize,
}

impl RevisionRow {
    fn new(strategy_id: &str, revision: &str) -> Self {
        Self {
            identity: RevisionIdentity {
                strategy_id: strategy_id.into(),
                revision: revision.into(),
            },
            definition: None,
            execution: None,
            effective_at: None,
            first_condition_at: None,
            first_fill_at: None,
            previous: None,
            note: None,
            saved_order: 0,
        }
    }
}

fn condition_identity(condition: &ConditionOrder) -> Option<(&str, &str)> {
    let origin = &condition.origin;
    if !origin.strategy_id.is_empty() && !origin.strategy_version.is_empty() {
        Some((&origin.strategy_id, &origin.strategy_version))
    } else {
        let definition = &condition.frozen.as_ref()?.definition;
        Some((&definition.strategy_id, &definition.revision))
    }
}

impl RevisionSource {
    pub fn capture(state: &ExecutionState, execution: &ExecutionConfig) -> Self {
        Self {
            active: state.definition.clone(),
            execution: execution.clone(),
            changes: state.strategy_revisions.clone(),
            conditions: state.conditions.clone(),
            history: state.history.clone(),
        }
    }

    pub fn render(&self) -> serde_json::Result<serde_json::Value> {
        let mut rows = BTreeMap::new();
        for (index, change) in self.changes.iter().enumerate() {
            let definition = &change.definition;
            let mut row = RevisionRow::new(&definition.strategy_id, &definition.revision);
            row.definition = Some(definition.clone());
            row.execution = Some(change.execution.clone());
            row.effective_at = change.effective_at;
            row.previous = change.previous.clone();
            row.note = change.note.clone();
            row.saved_order = index + 1;
            rows.insert(
                (
                    definition.strategy_id.as_str(),
                    definition.revision.as_str(),
                ),
                row,
            );
        }
        rows.entry((&self.active.strategy_id, &self.active.revision))
            .or_insert_with(|| {
                let mut row = RevisionRow::new(&self.active.strategy_id, &self.active.revision);
                row.definition = Some(self.active.clone());
                row.execution = Some(self.execution.clone());
                row
            });
        for condition in self.conditions.values() {
            let Some((strategy, revision)) = condition_identity(condition) else {
                continue;
            };
            let row = rows
                .entry((strategy, revision))
                .or_insert_with(|| RevisionRow::new(strategy, revision));
            if let Some(frozen) = condition.frozen.as_ref().filter(|frozen| {
                row.definition.is_none()
                    && frozen.definition.strategy_id == strategy
                    && frozen.definition.revision == revision
            }) {
                row.definition = Some(frozen.definition.clone());
                row.execution = Some(frozen.execution.clone());
            }
            if condition.created_at > 0 {
                row.first_condition_at = Some(
                    row.first_condition_at
                        .map_or(condition.created_at, |at| at.min(condition.created_at)),
                );
            }
        }
        for trade in &self.history {
            let (strategy, revision) = if !trade.origin.strategy_id.is_empty()
                && !trade.origin.strategy_version.is_empty()
            {
                (
                    trade.origin.strategy_id.as_str(),
                    trade.origin.strategy_version.as_str(),
                )
            } else if let Some(identity) = self
                .conditions
                .get(&trade.condition_order_id)
                .and_then(condition_identity)
            {
                identity
            } else {
                continue;
            };
            let row = rows
                .entry((strategy, revision))
                .or_insert_with(|| RevisionRow::new(strategy, revision));
            if let Ok(at) = super::super::candidates::parse_time(&trade.open_ts) {
                row.first_fill_at = Some(row.first_fill_at.map_or(at, |first| first.min(at)));
            }
        }
        let mut rows: Vec<_> = rows.into_values().collect();
        rows.sort_by_key(|row| {
            (
                row.effective_at
                    .or(row.first_condition_at)
                    .or(row.first_fill_at),
                row.saved_order,
            )
        });
        serde_json::to_value(rows)
    }
}
