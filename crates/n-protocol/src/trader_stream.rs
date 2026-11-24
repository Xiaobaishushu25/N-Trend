//! Versioned snapshot replacements and condition records keyed by ID.
use serde::{Deserialize, Serialize};
use serde_json::value::RawValue;
use serde_json::{Map, Value};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TraderDelta {
    pub stream_id: String,
    pub base_revision: u64,
    pub revision: u64,
    pub changes: Map<String, Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub conditions_patch: Option<ConditionsPatch>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ConditionsPatch {
    pub upsert: Vec<Value>,
    pub remove: Vec<String>,
}

/// A missing value removes the key; an explicit JSON null is a replacement.
#[derive(Debug, Serialize)]
pub struct DiagnosticChange {
    pub path: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub value: Option<Value>,
}

const MAX_DIAGNOSTIC_CHANGES: usize = 128;

fn collect_diagnostic_changes(
    old: &Value,
    new: &Value,
    path: &mut Vec<String>,
    out: &mut Vec<DiagnosticChange>,
) {
    if out.len() > MAX_DIAGNOSTIC_CHANGES || old == new {
        return;
    }
    if let (Some(old), Some(new)) = (old.as_object(), new.as_object()) {
        for (key, value) in new {
            if out.len() > MAX_DIAGNOSTIC_CHANGES {
                return;
            }
            path.push(key.clone());
            if let Some(previous) = old.get(key) {
                collect_diagnostic_changes(previous, value, path, out);
            } else {
                out.push(DiagnosticChange {
                    path: path.clone(),
                    value: Some(value.clone()),
                });
            }
            path.pop();
        }
        for key in old.keys().filter(|key| !new.contains_key(*key)) {
            if out.len() > MAX_DIAGNOSTIC_CHANGES {
                return;
            }
            path.push(key.clone());
            out.push(DiagnosticChange {
                path: path.clone(),
                value: None,
            });
            path.pop();
        }
    } else {
        out.push(DiagnosticChange {
            path: path.clone(),
            value: Some(new.clone()),
        });
    }
}

use std::collections::BTreeMap;
use std::sync::Arc;

pub type ConditionRows = BTreeMap<String, Arc<RawValue>>;

#[derive(Clone)]
enum SnapshotField {
    Json(Arc<Value>),
    Object(Arc<SharedTraderSnapshot>),
    Conditions(Arc<ConditionRows>),
}

impl SnapshotField {
    fn same_value(&self, other: &Self) -> bool {
        match (self, other) {
            (Self::Json(left), Self::Json(right)) => Arc::ptr_eq(left, right) || left == right,
            (Self::Object(left), Self::Object(right)) => {
                Arc::ptr_eq(left, right) || left.fields_eq(right, &[])
            }
            (Self::Conditions(left), Self::Conditions(right)) => Arc::ptr_eq(left, right),
            _ => false,
        }
    }

    fn to_value(&self) -> Value {
        match self {
            Self::Json(value) => (**value).clone(),
            Self::Object(value) => value.to_value(),
            Self::Conditions(_) => serde_json::to_value(self).expect("condition rows contain valid JSON"),
        }
    }

    fn json(&self) -> Option<&Value> {
        match self {
            Self::Json(value) => Some(value),
            Self::Object(_) | Self::Conditions(_) => None,
        }
    }
}

impl Serialize for SnapshotField {
    fn serialize<S: serde::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        match self {
            Self::Json(value) => value.as_ref().serialize(serializer),
            Self::Object(value) => value.as_ref().serialize(serializer),
            Self::Conditions(rows) => serializer.collect_seq(rows.values().map(Arc::as_ref)),
        }
    }
}

/// Immutable field values can be shared by the publisher and its REST baseline.
#[derive(Clone, Default, Serialize)]
#[serde(transparent)]
pub struct SharedTraderSnapshot(BTreeMap<String, SnapshotField>);

impl SharedTraderSnapshot {
    pub fn from_value(value: Value) -> Self {
        let Value::Object(object) = value else {
            return Self::default();
        };
        let mut result = Self::default();
        for (key, value) in object {
            if key == "strategy_diagnostics" && value.is_object() {
                result.insert_object(key, Self::from_value(value));
            } else {
                result.insert(key, value);
            }
        }
        result
    }

    pub fn insert(&mut self, key: impl Into<String>, value: Value) {
        self.insert_shared(key, Arc::new(value));
    }

    pub fn insert_shared(&mut self, key: impl Into<String>, value: Arc<Value>) {
        self.0.insert(key.into(), SnapshotField::Json(value));
    }

    pub fn insert_object(&mut self, key: impl Into<String>, value: Self) {
        self.0
            .insert(key.into(), SnapshotField::Object(Arc::new(value)));
    }

    pub fn insert_conditions(&mut self, rows: Arc<ConditionRows>) {
        self.0
            .insert("conditions".into(), SnapshotField::Conditions(rows));
    }

    pub fn condition_rows(&self) -> Option<&ConditionRows> {
        match self.0.get("conditions")? {
            SnapshotField::Conditions(rows) => Some(rows),
            _ => None,
        }
    }

    pub fn expand_conditions_patch(&self, delta: &mut TraderDelta) {
        if delta.conditions_patch.take().is_some() {
            if let Some(rows) = self.0.get("conditions") {
                delta.changes.insert("conditions".into(), rows.to_value());
            }
        }
    }

    pub fn extend(&mut self, fields: Self) {
        self.0.extend(fields.0);
    }

    pub fn get(&self, key: &str) -> Option<&Value> {
        self.0.get(key).and_then(SnapshotField::json)
    }

    pub fn object(&self, key: &str) -> Option<&Self> {
        match self.0.get(key) {
            Some(SnapshotField::Object(value)) => Some(value),
            _ => None,
        }
    }

    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }

    pub fn field_names(&self) -> impl Iterator<Item = &str> {
        self.0.keys().map(String::as_str)
    }

    pub fn to_value(&self) -> Value {
        Value::Object(
            self.0
                .iter()
                .map(|(key, value)| (key.clone(), value.to_value()))
                .collect(),
        )
    }

    fn fields_eq(&self, other: &Self, ignored: &[&str]) -> bool {
        self.0
            .iter()
            .filter(|(key, _)| !ignored.contains(&key.as_str()))
            .all(|(key, value)| other.0.get(key).is_some_and(|old| value.same_value(old)))
            && self
                .0
                .keys()
                .filter(|key| !ignored.contains(&key.as_str()))
                .count()
                == other
                    .0
                    .keys()
                    .filter(|key| !ignored.contains(&key.as_str()))
                    .count()
    }
}

pub struct TraderPublisher {
    stream_id: String,
    revision: u64,
    latest: SharedTraderSnapshot,
}

fn semantic_field_eq(key: &str, left: &Value, right: &Value) -> bool {
    if key == "strategy_diagnostics" {
        // Calendar countdowns change every second. They must not retransmit the
        // entire strategy archive; status/coverage transitions still publish.
        // update() keeps the complete countdown fresh in the REST baseline.
        if let (Some(left), Some(right)) = (left.as_object(), right.as_object()) {
            if !object_fields_eq(left, right, &["calendar_health"]) {
                return false;
            }
            return calendar_health_eq(left.get("calendar_health"), right.get("calendar_health"));
        }
    }
    let ignored: &[&str] = match key {
        "health" => &[
            "uptime",
            "account_updated_at",
            "positions_updated_at",
            "orders_updated_at",
            "trades_updated_at",
        ],
        "account" => &["updated_at"],
        _ => &[],
    };
    if ignored.is_empty() {
        return left == right;
    }
    value_fields_eq(left, right, ignored)
}

fn calendar_health_eq(left: Option<&Value>, right: Option<&Value>) -> bool {
    match (left, right) {
        (Some(Value::Array(left)), Some(Value::Array(right))) => {
            left.len() == right.len()
                && left
                    .iter()
                    .zip(right)
                    .all(|(left, right)| value_fields_eq(left, right, &["remaining_seconds"]))
        }
        (left, right) => left == right,
    }
}

fn object_fields_eq(
    left: &Map<String, Value>,
    right: &Map<String, Value>,
    ignored: &[&str],
) -> bool {
    left.iter()
        .filter(|(key, _)| !ignored.contains(&key.as_str()))
        .all(|(key, value)| right.get(key) == Some(value))
        && left
            .keys()
            .filter(|key| !ignored.contains(&key.as_str()))
            .count()
            == right
                .keys()
                .filter(|key| !ignored.contains(&key.as_str()))
                .count()
}

fn value_fields_eq(left: &Value, right: &Value, ignored: &[&str]) -> bool {
    match (left.as_object(), right.as_object()) {
        (Some(left), Some(right)) => object_fields_eq(left, right, ignored),
        _ => left == right,
    }
}

impl TraderPublisher {
    pub fn new(stream_id: String) -> Self {
        Self {
            stream_id,
            revision: 0,
            latest: SharedTraderSnapshot::default(),
        }
    }

    pub fn heartbeat(&self) -> Value {
        serde_json::json!({"stream_id": self.stream_id, "revision": self.revision,
            "health": self.latest.get("health"), "account_updated_at": self.latest.get("account").and_then(|account| account.get("updated_at"))})
    }

    pub fn update_instance(
        &mut self,
        snapshot: Value,
    ) -> (Value, Option<TraderDelta>, Option<Vec<DiagnosticChange>>) {
        let (snapshot, delta, diagnostics) =
            self.update_shared(SharedTraderSnapshot::from_value(snapshot), true);
        (snapshot.to_value(), delta, diagnostics)
    }

    /// Refresh the REST baseline even when only query timestamps changed.
    pub fn update(&mut self, snapshot: Value) -> (Value, Option<TraderDelta>) {
        let (snapshot, delta, _) =
            self.update_shared(SharedTraderSnapshot::from_value(snapshot), false);
        (snapshot.to_value(), delta)
    }

    pub fn update_shared(
        &mut self,
        mut snapshot: SharedTraderSnapshot,
        compact_diagnostics: bool,
    ) -> (
        SharedTraderSnapshot,
        Option<TraderDelta>,
        Option<Vec<DiagnosticChange>>,
    ) {
        let object = &mut snapshot.0;
        for key in ["execution_orders", "revision", "_stream_id", "_revision"] {
            object.remove(key);
        }
        let previous = &self.latest.0;
        let mut changes = Map::new();
        let mut diagnostics = None;
        let mut conditions_patch = None;
        for (key, value) in object.iter() {
            let old = previous.get(key);
            if old.is_some_and(|old| snapshot_field_eq(key, old, value)) {
                continue;
            }
            if let (Some(SnapshotField::Conditions(old)), SnapshotField::Conditions(rows)) = (old, value) {
                let upsert: Vec<_> = rows.iter().filter_map(|(id, row)| {
                    if old.get(id).is_some_and(|previous| Arc::ptr_eq(previous, row) || previous.get() == row.get()) {
                        None
                    } else {
                        Some(serde_json::from_str(row.get()).expect("condition row contains valid JSON"))
                    }
                }).collect();
                let remove: Vec<_> = old.keys().filter(|id| !rows.contains_key(*id)).cloned().collect();
                if !upsert.is_empty() || !remove.is_empty() {
                    conditions_patch = Some(ConditionsPatch { upsert, remove });
                }
                continue;
            }
            let value = value.to_value();
            if compact_diagnostics && key == "strategy_diagnostics" && value.is_object() {
                if let Some(old) = old.map(SnapshotField::to_value).filter(Value::is_object) {
                    let mut changes = Vec::new();
                    collect_diagnostic_changes(&old, &value, &mut Vec::new(), &mut changes);
                    // A client may still have an earlier countdown because countdown-only
                    // changes are suppressed. Refresh it when a real change is sent.
                    if old.get("calendar_health") == value.get("calendar_health") {
                        if let Some(calendar) = value.get("calendar_health") {
                            changes.push(DiagnosticChange {
                                path: vec!["calendar_health".into()],
                                value: Some(calendar.clone()),
                            });
                        }
                    }
                    if !changes.is_empty() && changes.len() <= MAX_DIAGNOSTIC_CHANGES {
                        diagnostics = Some(changes);
                    }
                }
            }
            changes.insert(key.clone(), value);
        }
        for key in previous.keys().filter(|k| !k.starts_with('_')) {
            if !object.contains_key(key) {
                changes.insert(key.clone(), Value::Null);
            }
        }
        let delta = if changes.is_empty() && conditions_patch.is_none() {
            None
        } else {
            let base_revision = self.revision;
            self.revision += 1;
            Some(TraderDelta {
                stream_id: self.stream_id.clone(),
                base_revision,
                revision: self.revision,
                changes,
                conditions_patch,
            })
        };
        snapshot.insert("_stream_id", self.stream_id.clone().into());
        snapshot.insert("_revision", self.revision.into());
        self.latest = snapshot.clone();
        (snapshot, delta, diagnostics)
    }
}

fn snapshot_field_eq(key: &str, old: &SnapshotField, value: &SnapshotField) -> bool {
    match (old, value) {
        (SnapshotField::Json(left), SnapshotField::Json(right)) => {
            Arc::ptr_eq(left, right) || semantic_field_eq(key, left, right)
        }
        (SnapshotField::Object(left), SnapshotField::Object(right))
            if key == "strategy_diagnostics" =>
        {
            left.fields_eq(right, &["calendar_health"])
                && calendar_health_eq(left.get("calendar_health"), right.get("calendar_health"))
        }
        _ => old.same_value(value),
    }
}

#[derive(Default)]
pub struct TraderSnapshotCache {
    snapshot: Value,
}

impl TraderSnapshotCache {
    pub fn reset(&mut self) {
        self.snapshot = Value::Null;
    }

    /// Keep displayed query times fresh without retransmitting account/history data.
    pub fn refresh_metadata(&mut self, heartbeat: &Value) -> Option<Value> {
        if heartbeat["stream_id"].as_str().is_none()
            || heartbeat["stream_id"] != self.snapshot["_stream_id"]
            || heartbeat["revision"] != self.snapshot["_revision"]
        {
            return None;
        }
        if let Some(health) = heartbeat.get("health") {
            self.snapshot["health"] = health.clone();
        }
        if let Some(account) = self.snapshot["account"].as_object_mut() {
            if let Some(timestamp) = heartbeat.get("account_updated_at") {
                account.insert("updated_at".into(), timestamp.clone());
            }
        }
        Some(self.snapshot.clone())
    }

    pub fn replace(&mut self, snapshot: Value) -> Option<Value> {
        if self.snapshot["_stream_id"].as_str().is_some()
            && self.snapshot["_stream_id"] == snapshot["_stream_id"]
            && snapshot["_revision"].as_u64()? < self.snapshot["_revision"].as_u64()?
        {
            return None;
        }
        self.snapshot = snapshot;
        Some(self.snapshot.clone())
    }

    /// A gap must force a fresh baseline; never apply a patch to unknown state.
    pub fn apply(&mut self, delta: TraderDelta) -> Result<Option<Value>, &'static str> {
        if delta.base_revision.checked_add(1) != Some(delta.revision) {
            return Err("invalid trader revision");
        }
        if delta.changes.keys().any(|key| key.starts_with('_')) {
            return Err("invalid trader field");
        }
        // Registration and bootstrap are separate queue operations. A published
        // delta may already be queued before the guaranteed connection baseline.
        if self.snapshot.is_null() && delta.base_revision > 0 {
            return Ok(None);
        }
        if self.snapshot.is_null() && delta.base_revision == 0 {
            self.snapshot = serde_json::json!({"_stream_id": delta.stream_id, "_revision": 0});
        }
        if self.snapshot["_stream_id"].as_str() != Some(delta.stream_id.as_str()) {
            return Err("trader stream changed without baseline");
        }
        let current = self.snapshot["_revision"]
            .as_u64()
            .ok_or("missing trader baseline")?;
        if delta.revision <= current {
            return Ok(None);
        }
        if delta.base_revision != current {
            return Err("trader revision gap");
        }
        let object = self
            .snapshot
            .as_object_mut()
            .ok_or("invalid trader baseline")?;
        if let Some(patch) = delta.conditions_patch {
            if delta.changes.contains_key("conditions") {
                return Err("conflicting conditions changes");
            }
            let rows = object
                .get_mut("conditions")
                .and_then(Value::as_array_mut)
                .ok_or("missing conditions baseline")?;
            if rows
                .iter()
                .chain(&patch.upsert)
                .any(|row| row.get("id").and_then(Value::as_str).is_none())
            {
                return Err("invalid condition id");
            }
            let mut indexed: BTreeMap<_, _> = std::mem::take(rows)
                .into_iter()
                .map(|row| (row["id"].as_str().unwrap().to_owned(), row))
                .collect();
            for id in patch.remove {
                indexed.remove(&id);
            }
            indexed.extend(
                patch
                    .upsert
                    .into_iter()
                    .map(|row| (row["id"].as_str().unwrap().to_owned(), row)),
            );
            *rows = indexed.into_values().collect();
        }
        for (key, value) in delta.changes {
            object.insert(key, value);
        }
        object.insert("_revision".into(), delta.revision.into());
        Ok(Some(self.snapshot.clone()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn snapshot() -> Value {
        json!({"source":"server", "health":{"ready":true,"sync_pending":false,"positions_updated_at":1},
            "account":{"balance":1000,"updated_at":1}, "broker_positions":[{"position":2}],
            "conditions":[{"state":"Working"}], "execution_error":null,
            "execution_orders":vec![json!({"note":"old rejection"});4031], "revision":1})
    }

    #[test]
    fn borrowed_comparison_preserves_exact_legacy_semantics() {
        fn legacy(key: &str, value: &Value) -> Value {
            let mut value = value.clone();
            let ignored: &[&str] = match key {
                "health" => &[
                    "uptime",
                    "account_updated_at",
                    "positions_updated_at",
                    "orders_updated_at",
                    "trades_updated_at",
                ],
                "account" => &["updated_at"],
                _ => &[],
            };
            if let Some(object) = value.as_object_mut() {
                for key in ignored {
                    object.remove(*key);
                }
            }
            value
        }
        let values = [
            Value::Null,
            json!(true),
            json!(1),
            json!([]),
            json!({}),
            json!({"updated_at":1}),
            json!({"updated_at":2}),
            json!({"uptime":1,"ready":true}),
            json!({"ready":true}),
            json!({"ready":false}),
            json!({"positions_updated_at":2,"ready":true}),
            json!({"nested":{"updated_at":1}}),
            json!({"nested":{"updated_at":2}}),
            json!([{"updated_at":1}]),
            json!([{"updated_at":2}]),
        ];
        for key in ["health", "account", "conditions", "unknown"] {
            for left in &values {
                for right in &values {
                    assert_eq!(
                        semantic_field_eq(key, left, right),
                        legacy(key, left) == legacy(key, right),
                        "{key}: {left} / {right}"
                    );
                }
            }
        }
    }

    #[test]
    fn timestamps_do_not_publish_or_resend_history_but_rest_stays_fresh() {
        let mut publisher = TraderPublisher::new("boot".into());
        let (baseline, first) = publisher.update(snapshot());
        assert!(baseline.get("execution_orders").is_none());
        assert!(!first.unwrap().changes.contains_key("execution_orders"));
        let mut next = snapshot();
        next["health"]["positions_updated_at"] = json!(200);
        next["health"]["uptime"] = json!(20);
        next["account"]["updated_at"] = json!(200);
        next["revision"] = json!(20);
        let (fresh, delta) = publisher.update(next);
        assert!(delta.is_none());
        assert_eq!(fresh["account"]["updated_at"], 200);
        assert_eq!(fresh["_revision"], 1);
        let mut cache = TraderSnapshotCache::default();
        cache.replace(baseline);
        let refreshed = cache.refresh_metadata(&publisher.heartbeat()).unwrap();
        assert_eq!(refreshed["account"]["updated_at"], 200);
        assert_eq!(refreshed["account"]["balance"], 1000);
        assert_eq!(refreshed["broker_positions"][0]["position"], 2);
    }

    #[test]
    fn calendar_countdown_does_not_resend_large_strategy_diagnostics() {
        let mut publisher = TraderPublisher::new("boot".into());
        let mut current = snapshot();
        current["strategy_diagnostics"] = json!({
            "states":{"revision":{"decisions":vec![json!({"reason":"filtered", "evidence":"frozen strategy evidence"});20000]}},
            "entry_decisions":{}, "symbol_slots":{},
            "calendar_health":[{"revision":"revision","status":"ok","coverage_end":"2026-12-31 23:59:59", "remaining_seconds":300000}]
        });
        assert!(
            serde_json::to_vec(&current["strategy_diagnostics"])
                .unwrap()
                .len()
                > 1_000_000
        );
        current["runtime_diagnostics"] = json!({"processed_quotes":0});
        let (baseline, _) = publisher.update(current.clone());
        let mut cache = TraderSnapshotCache::default();
        cache.replace(baseline);
        let mut wire_bytes = 0;
        for second in 1..=20 {
            current["strategy_diagnostics"]["calendar_health"][0]["remaining_seconds"] =
                json!(300000 - second);
            let (baseline, delta) = publisher.update(current.clone());
            assert!(delta.is_none(), "a countdown-only tick is metadata");
            assert_eq!(
                baseline["strategy_diagnostics"],
                current["strategy_diagnostics"]
            );
            current["runtime_diagnostics"]["processed_quotes"] = json!(second);
            let (_, delta) = publisher.update(current.clone());
            let delta = delta.unwrap();
            assert_eq!(delta.changes.len(), 1);
            assert!(delta.changes.contains_key("runtime_diagnostics"));
            wire_bytes += serde_json::to_vec(&delta).unwrap().len();
            cache.apply(delta).unwrap();
        }
        assert!(
            wire_bytes < 4096,
            "small counters must not carry the strategy archive"
        );
        // Business changes must still replace diagnostics atomically in existing clients.
        for path in ["states", "entry_decisions", "symbol_slots"] {
            current["strategy_diagnostics"][path] = json!({"changed":true});
            let (_, delta) = publisher.update(current.clone());
            let merged = cache.apply(delta.unwrap()).unwrap().unwrap();
            assert_eq!(
                merged["strategy_diagnostics"],
                current["strategy_diagnostics"]
            );
        }
    }

    #[test]
    fn calendar_status_coverage_and_unknown_fields_are_never_ignored() {
        let base = json!({"states":{},"calendar_health":[{
            "revision":"a","status":"ok","coverage_end":"2026-12-31", "remaining_seconds":300000
        }]});
        let mut countdown = base.clone();
        countdown["calendar_health"][0]["remaining_seconds"] = json!(299999);
        assert!(semantic_field_eq("strategy_diagnostics", &base, &countdown));
        for (key, value) in [
            ("status", json!("expiring")),
            ("revision", json!("b")),
            ("coverage_end", json!("2027-01-01")),
            ("new_health_field", json!(true)),
        ] {
            let mut next = countdown.clone();
            next["calendar_health"][0][key] = value;
            assert!(
                !semantic_field_eq("strategy_diagnostics", &base, &next),
                "{key}"
            );
        }
        for calendars in [
            Value::Null,
            json!([]),
            json!([{}, {}]),
            json!([1]),
            json!({}),
        ] {
            let mut next = base.clone();
            next["calendar_health"] = calendars;
            assert!(!semantic_field_eq("strategy_diagnostics", &base, &next));
        }
        let mut missing = base.clone();
        missing.as_object_mut().unwrap().remove("calendar_health");
        assert!(!semantic_field_eq("strategy_diagnostics", &base, &missing));
        assert!(!semantic_field_eq(
            "strategy_diagnostics",
            &base,
            &Value::Null
        ));
    }

    #[test]
    fn changed_fields_merge_without_clearing_positions_and_explicit_empty_clears() {
        let mut publisher = TraderPublisher::new("boot".into());
        let (baseline, _) = publisher.update(snapshot());
        let mut cache = TraderSnapshotCache::default();
        cache.replace(baseline);
        let mut next = snapshot();
        next["account"]["balance"] = json!(2000);
        let (_, delta) = publisher.update(next.clone());
        let delta = delta.unwrap();
        assert_eq!(delta.changes.len(), 1);
        let merged = cache.apply(delta.clone()).unwrap().unwrap();
        assert_eq!(merged["account"]["balance"], 2000);
        assert_eq!(merged["broker_positions"][0]["position"], 2);
        assert!(cache.apply(delta).unwrap().is_none());
        next["broker_positions"] = json!([]);
        next["execution_error"] = json!("offline");
        cache
            .apply(publisher.update(next.clone()).1.unwrap())
            .unwrap();
        next["execution_error"] = Value::Null;
        let merged = cache
            .apply(publisher.update(next).1.unwrap())
            .unwrap()
            .unwrap();
        assert_eq!(merged["broker_positions"], json!([]));
        assert!(merged["execution_error"].is_null());
    }

    #[test]
    fn gaps_and_restarts_require_baseline_and_late_bootstrap_does_not_roll_back() {
        let mut publisher = TraderPublisher::new("boot".into());
        let (baseline, _) = publisher.update(snapshot());
        let mut cache = TraderSnapshotCache::default();
        cache.replace(baseline.clone());
        let mut next = snapshot();
        next["health"]["ready"] = json!(false);
        let old_heartbeat = publisher.heartbeat();
        let (latest, delta) = publisher.update(next.clone());
        cache.apply(delta.unwrap()).unwrap();
        assert!(cache.refresh_metadata(&old_heartbeat).is_none());
        assert!(cache.replace(baseline).is_none());
        next["account"]["balance"] = json!(2);
        publisher.update(next.clone());
        next["account"]["balance"] = json!(3);
        assert!(cache.apply(publisher.update(next).1.unwrap()).is_err());
        cache.replace(latest);
        let mut restarted = TraderPublisher::new("restart".into());
        let (baseline, delta) = restarted.update(snapshot());
        assert!(cache.apply(delta.unwrap()).is_err());
        cache.reset();
        assert!(cache.replace(baseline).is_some());
    }

    #[test]
    fn initial_delta_is_a_baseline_when_bridge_initializes_after_connection() {
        let mut publisher = TraderPublisher::new("boot".into());
        let mut cache = TraderSnapshotCache::default();
        let (_, delta) = publisher.update(snapshot());
        assert_eq!(
            cache.apply(delta.unwrap()).unwrap().unwrap()["account"]["balance"],
            1000
        );
    }

    #[test]
    fn already_queued_delta_waits_for_connection_baseline_without_reconnect_loop() {
        let mut publisher = TraderPublisher::new("boot".into());
        let mut current = snapshot();
        publisher.update(current.clone());
        current["account"]["balance"] = json!(2000);
        let (baseline, queued) = publisher.update(current.clone());
        let mut cache = TraderSnapshotCache::default();
        assert!(cache.apply(queued.unwrap()).unwrap().is_none());
        cache.replace(baseline);
        current["account"]["balance"] = json!(3000);
        assert_eq!(
            cache
                .apply(publisher.update(current).1.unwrap())
                .unwrap()
                .unwrap()["account"]["balance"],
            3000
        );
    }

    #[test]
    fn routine_counter_sync_sends_only_health_transitions_instead_of_five_archives() {
        let mut publisher = TraderPublisher::new("boot".into());
        let mut current = snapshot();
        publisher.update(current.clone());
        let mut updates = Vec::new();
        current["health"]["sync_pending"] = json!(true);
        updates.push(publisher.update(current.clone()).1);
        for field in [
            "positions_updated_at",
            "account_updated_at",
            "orders_updated_at",
        ] {
            current["health"][field] = json!(200);
            current["account"]["updated_at"] = json!(200);
            updates.push(publisher.update(current.clone()).1);
        }
        current["health"]["sync_pending"] = json!(false);
        current["health"]["trades_updated_at"] = json!(200);
        updates.push(publisher.update(current).1);
        let updates: Vec<_> = updates.into_iter().flatten().collect();
        assert_eq!(updates.len(), 2);
        assert!(updates
            .iter()
            .all(|delta| delta.changes.len() == 1 && delta.changes.contains_key("health")));
        assert!(serde_json::to_vec(&updates).unwrap().len() < 2048);
    }
}
