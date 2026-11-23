use super::*;
use crate::trader::candidates;

fn book(price: f64) -> ExecutionQuote {
    ExecutionQuote {
        instrument_id: "pb2701".into(),
        exchange_id: "SHFE".into(),
        underlying_symbol: "PB0".into(),
        last: price,
        bid: price - 1.,
        ask: price + 1.,
        tick: 1.,
        lower: 500.,
        upper: 2000.,
        timestamp: chrono::Utc::now().timestamp(),
    }
}

#[test]
fn native_market_state_scopes_match_full_callbacks_and_preserve_archives() {
    let r = StrategyRegistry::default();
    let defs = [
        candidates::definition(candidates::P2, candidates::Parameters::reference_snapshot()),
        candidates::p3::definition(candidates::p3::P3Parameters::reference_snapshot()),
        crate::trader::b5::definition(),
    ];
    for d in defs {
        let strategy = r.strategy(&d).unwrap();
        let cfg = ExecutionConfig::default();
        let quotes = HashMap::new();
        let context = StrategyContext {
            instance_id: "scope-test",
            positions: &[],
            conditions: &[],
            orders: &[],
            account: None,
            quotes: &quotes,
            config: &cfg,
            allow_entries: true,
        };
        let mut saved = if d.strategy_id == crate::trader::b5::ID {
            json!({"raw_cursor":{},"cursor":{},"dedup":{},"decisions":{"old":{"reason":"rejected"}},
                "status":{},"minutes":{},"confirmed":{},"released":{},"lifecycle":null})
        } else {
            json!({"seen":["original-warning"],"decisions":{"old":{"reason":"rejected"}},"pending":{},"holding":{}})
        };
        for event in [
            StrategyEvent::Quote(book(1000.)),
            StrategyEvent::Quote(book(1002.)),
            StrategyEvent::Timer {
                timestamp: chrono::Utc::now().timestamp(),
            },
            StrategyEvent::BarClosed {
                symbol: "PB0".into(),
                timeframe: "candidate15m".into(),
                timestamp: "2026-11-13 10:00:00".into(),
                high: 1002.,
                low: 1000.,
                close: 1001.,
            },
        ] {
            let scope = strategy.event_state_scope(&event);
            let mut full = saved.clone();
            let mut live = scope.select(&saved);
            let expected = strategy.on_event(&event, &context, &mut full).unwrap();
            let actual = strategy.on_event(&event, &context, &mut live).unwrap();
            assert_eq!(
                serde_json::to_value(actual).unwrap(),
                serde_json::to_value(expected).unwrap()
            );
            scope.apply(&mut saved, live);
            assert_eq!(
                saved, full,
                "native scope must preserve the complete state for {}",
                d.strategy_id
            );
            assert_eq!(saved["decisions"]["old"]["reason"], "rejected");
        }
        assert!(!strategy.handles_event(&StrategyEvent::ExecutionUpdate(
            json!({"large":"broker archive"})
        )));
    }
}

#[test]
fn candidate_maintenance_discards_only_finished_clocks_and_keeps_signal_dedup() {
    let strategy =
        candidates::CandidateStrategy::new(candidates::Parameters::reference_snapshot()).unwrap();
    let cfg = ExecutionConfig::default();
    let quotes = HashMap::new();
    let context = StrategyContext {
        instance_id: "maintenance",
        positions: &[],
        conditions: &[],
        orders: &[],
        account: None,
        quotes: &quotes,
        config: &cfg,
        allow_entries: true,
    };
    let mut state = json!({"seen":["old-warning"],"decisions":{"old-warning":{"reason":"accepted_warning"}},
        "pending":{"old-warning":{"buckets":{"2026-11-12 10:00:00":0.1}}},
        "holding":{"closed-position":{"buckets":{"2026-11-12 10:00:00":1.2}}}});
    strategy
        .on_event(
            &StrategyEvent::Timer {
                timestamp: chrono::Utc::now().timestamp(),
            },
            &context,
            &mut state,
        )
        .unwrap();
    assert!(state["pending"].as_object().unwrap().is_empty());
    assert!(state["holding"].as_object().unwrap().is_empty());
    assert_eq!(state["seen"], json!(["old-warning"]));
    assert_eq!(
        state["decisions"]["old-warning"]["reason"],
        "accepted_warning"
    );
}

struct ScopedClock;
impl Strategy for ScopedClock {
    fn event_state_scope(&self, _: &StrategyEvent) -> EventStateScope {
        EventStateScope::Fields(&["clock"])
    }
    fn on_event(
        &self,
        event: &StrategyEvent,
        c: &StrategyContext<'_>,
        state: &mut Value,
    ) -> Result<Vec<TradeIntent>> {
        if matches!(event, StrategyEvent::Signal { .. }) {
            return StrategyRegistry::default()
                .strategy(&StrategyDefinition::default())?
                .on_event(event, c, state);
        }
        state["clock"] = json!(state["clock"].as_u64().unwrap_or(0) + 1);
        // This archive is intentionally absent from the callback projection.
        assert!(state.get("decisions").is_none());
        if let StrategyEvent::Quote(q) = event {
            anyhow::ensure!(q.last != 999., "callback failure must roll back its clock");
            if q.last == 990. {
                return Ok(vec![TradeIntent::CancelEntry {
                    condition_id: c.conditions[0].id.clone(),
                    reason: "clock_test_cancel".into(),
                }]);
            }
        }
        Ok(vec![])
    }
}
fn clock_engine(counter: &Counter) -> ExecutionEngine {
    let mut e = counter.engine(ExecutionMode::MonitorOnly, 1);
    let mut registry = StrategyRegistry::default();
    registry.register_strategy(N_STRATEGY, N_VERSION, |_| Ok(Box::new(ScopedClock)));
    e.registry = Arc::new(registry);
    e
}

#[tokio::test]
async fn clock_observations_batch_but_control_intents_commit_immediately_and_restore() {
    let counter = Counter::new();
    let e = clock_engine(&counter);
    let id = register(&e, 1000., 980.).await;
    {
        let mut s = e.state.lock().await;
        let rev = s.definition.revision.clone();
        s.strategy_states
            .insert(rev, json!({"clock":0,"decisions":{"old":"immutable"}}));
        e.save(&mut s).unwrap();
    }
    let path = counter.directory.join("trader_execution.json");
    let before = export_execution_state(&path).unwrap();
    // No callback intent: retain the observation in memory until batch commit.
    e.dispatch_observation(StrategyEvent::Quote(book(1001.)), false)
        .await
        .unwrap();
    assert_eq!(
        export_execution_state(&path).unwrap(),
        before
    );
    let rev = e.definition().await.revision;
    assert_eq!(e.state.lock().await.strategy_states[&rev]["clock"], 1);
    // A cancellation is durable even before the surrounding batch completes.
    e.dispatch_observation(StrategyEvent::Quote(book(990.)), false)
        .await
        .unwrap();
    let disk: ExecutionState = serde_json::from_value(
        export_execution_state(&path).unwrap(),
    )
    .unwrap();
    assert_eq!(disk.conditions[&id].state, ConditionState::Canceled);
    assert_eq!(disk.strategy_states[&rev]["clock"], 2);
    assert_eq!(disk.strategy_states[&rev]["decisions"]["old"], "immutable");
    drop(e);
    let restored = clock_engine(&counter);
    assert_eq!(
        restored.state.lock().await.strategy_states[&rev]["clock"],
        2
    );
    assert_eq!(
        restored.list_conditions().await[0].state,
        ConditionState::Canceled
    );
}

#[tokio::test]
async fn failed_scoped_callback_preserves_clock_and_archive_and_pauses_entries() {
    let counter = Counter::new();
    let e = clock_engine(&counter);
    let rev = e.definition().await.revision;
    e.state
        .lock()
        .await
        .strategy_states
        .insert(rev.clone(), json!({"clock":7,"decisions":{"old":"keep"}}));
    e.dispatch_observation(StrategyEvent::Quote(book(999.)), false)
        .await
        .unwrap();
    let s = e.state.lock().await;
    assert!(s.paused);
    assert!(s.strategy_error.is_some());
    assert_eq!(
        s.strategy_states[&rev],
        json!({"clock":7,"decisions":{"old":"keep"}})
    );
}
