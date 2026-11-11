use n_protocol::trader_stream::TraderPublisher;
use serde_json::{json, Map, Value};
use std::alloc::{GlobalAlloc, Layout, System};
use std::cell::Cell;
use std::time::Instant;

struct CountingAllocator;
thread_local! {
    static ALLOCATIONS: Cell<Option<usize>> = const { Cell::new(None) };
}

unsafe impl GlobalAlloc for CountingAllocator {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        let _ = ALLOCATIONS.try_with(|count| {
            if let Some(n) = count.get() {
                count.set(Some(n + 1));
            }
        });
        System.alloc(layout)
    }
    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        System.dealloc(ptr, layout)
    }
    unsafe fn realloc(&self, ptr: *mut u8, old: Layout, new: usize) -> *mut u8 {
        let _ = ALLOCATIONS.try_with(|count| {
            if let Some(n) = count.get() {
                count.set(Some(n + 1));
            }
        });
        System.realloc(ptr, old, new)
    }
}

#[global_allocator]
static ALLOCATOR: CountingAllocator = CountingAllocator;

fn measure<T>(f: impl FnOnce() -> T) -> (T, usize, std::time::Duration) {
    ALLOCATIONS.with(|n| n.set(Some(0)));
    let start = Instant::now();
    let result = f();
    let elapsed = start.elapsed();
    let allocations = ALLOCATIONS.with(|n| n.replace(None).unwrap());
    (result, allocations, elapsed)
}

// Frozen pre-fix update for an unchanged business snapshot. Constructing the
// caller's input is outside both measurements, as it is in production.
fn legacy_unchanged_update(latest: &mut Value, mut snapshot: Value) -> Value {
    fn semantic(key: &str, value: &Value) -> Value {
        let mut out = value.clone();
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
        if let Some(object) = out.as_object_mut() {
            for key in ignored {
                object.remove(*key);
            }
        }
        out
    }
    let object = snapshot.as_object_mut().unwrap();
    for key in ["execution_orders", "revision", "_stream_id", "_revision"] {
        object.remove(key);
    }
    let mut changes = Map::new();
    for (key, value) in object.iter() {
        if latest.get(key).map(|old| semantic(key, old)) != Some(semantic(key, value)) {
            changes.insert(key.clone(), value.clone());
        }
    }
    assert!(changes.is_empty());
    object.insert("_stream_id".into(), "boot".into());
    object.insert("_revision".into(), 1.into());
    *latest = snapshot.clone();
    snapshot
}

#[test]
fn unchanged_large_snapshot_avoids_comparison_deep_copies() {
    let evidence = json!({"reason":"frozen strategy evidence", "levels":vec![json!({
        "label":"confirmed zone", "prices":[100.0,101.0,102.0], "context":"historical evidence"
    });24]});
    let snapshot = json!({"health":{"ready":true,"uptime":1},
        "account":{"balance":1000,"updated_at":1},
        "conditions":vec![json!({"id":"condition","frozen":evidence});300]});
    let mut publisher = TraderPublisher::new("boot".into());
    let (mut legacy, _) = publisher.update(snapshot.clone());
    let mut updated = snapshot;
    updated["health"]["uptime"] = json!(2);
    updated["account"]["updated_at"] = json!(2);
    let legacy_input = updated.clone();
    let ((baseline, delta), allocations, elapsed) = measure(|| publisher.update(updated));
    let (expected, old_allocations, old_elapsed) =
        measure(|| legacy_unchanged_update(&mut legacy, legacy_input));
    assert!(delta.is_none());
    assert_eq!(baseline, expected);
    println!(
        "snapshot={} bytes; allocations old={} new={}; elapsed old={:?} new={:?}",
        serde_json::to_vec(&baseline).unwrap().len(),
        old_allocations,
        allocations,
        old_elapsed,
        elapsed
    );
    assert!(allocations * 10 < old_allocations * 6,
        "comparison must stop copying both complete snapshots: old={old_allocations}, new={allocations}");
}
