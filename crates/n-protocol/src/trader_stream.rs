//! Versioned, top-level replacements: absent fields are unchanged, null clears a field.
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TraderDelta {
    pub stream_id: String,
    pub base_revision: u64,
    pub revision: u64,
    pub changes: Map<String, Value>,
}

pub struct TraderPublisher {
    stream_id: String,
    revision: u64,
    latest: Value,
}

fn semantic_field(key: &str, value: &Value) -> Value {
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

impl TraderPublisher {
    pub fn new(stream_id: String) -> Self {
        Self {
            stream_id,
            revision: 0,
            latest: Value::Null,
        }
    }

    pub fn heartbeat(&self) -> Value {
        serde_json::json!({"stream_id": self.stream_id, "revision": self.revision,
            "health": self.latest["health"], "account_updated_at": self.latest["account"]["updated_at"]})
    }

    /// Refresh the REST baseline even when only query timestamps changed.
    pub fn update(&mut self, mut snapshot: Value) -> (Value, Option<TraderDelta>) {
        let object = snapshot
            .as_object_mut()
            .expect("trader snapshot must be an object");
        for key in ["execution_orders", "revision", "_stream_id", "_revision"] {
            object.remove(key);
        }
        let previous = self.latest.as_object();
        let mut changes = Map::new();
        for (key, value) in object.iter() {
            if previous
                .and_then(|p| p.get(key))
                .map(|old| semantic_field(key, old))
                != Some(semantic_field(key, value))
            {
                changes.insert(key.clone(), value.clone());
            }
        }
        if let Some(previous) = previous {
            for key in previous.keys().filter(|k| !k.starts_with('_')) {
                if !object.contains_key(key) {
                    changes.insert(key.clone(), Value::Null);
                }
            }
        }
        let delta = if changes.is_empty() {
            None
        } else {
            let base_revision = self.revision;
            self.revision += 1;
            Some(TraderDelta {
                stream_id: self.stream_id.clone(),
                base_revision,
                revision: self.revision,
                changes,
            })
        };
        object.insert("_stream_id".into(), self.stream_id.clone().into());
        object.insert("_revision".into(), self.revision.into());
        self.latest = snapshot.clone();
        (snapshot, delta)
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
