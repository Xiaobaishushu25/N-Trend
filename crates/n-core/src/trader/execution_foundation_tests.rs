//! Counter-driven acceptance cases for shared execution, independent of P3 signals.
use super::*;
use std::sync::atomic::AtomicUsize;

#[tokio::test]
async fn zero_sizing_through_dispatch_commits_strategy_state_without_pausing() {
    struct EmitZero;
    impl Strategy for EmitZero {
        fn on_event(
            &self,
            _: &StrategyEvent,
            _: &StrategyContext<'_>,
            state: &mut Value,
        ) -> Result<Vec<TradeIntent>> {
            let count = state.as_u64().unwrap_or(0) + 1;
            *state = json!(count);
            let mut p = plan("dispatch-zero", Direction::Long, 990.);
            p.requested_volume = 0;
            Ok(vec![TradeIntent::Open(p)])
        }
    }
    let counter = Counter::new();
    let mut r = registry();
    r.register_strategy("foundation-test", "1", |_| Ok(Box::new(EmitZero)));
    let e = setup(&counter, "fixed_v1", "protect_only_v1", r).await;
    e.dispatch(StrategyEvent::Timer {
        timestamp: chrono::Utc::now().timestamp(),
    })
    .await
    .unwrap();
    e.dispatch(StrategyEvent::Timer {
        timestamp: chrono::Utc::now().timestamp(),
    })
    .await
    .unwrap();
    let s = e.state.lock().await;
    assert!(!s.paused && s.strategy_error.is_none());
    assert_eq!(s.strategy_states[&s.definition.revision], 2);
    assert_eq!(s.entry_decisions.len(), 1);
    assert!(s.conditions.is_empty());
}

#[tokio::test]
async fn late_open_fill_after_first_position_closed_is_still_flattened_after_restart() {
    let counter = Counter::new();
    let e = setup(&counter, "fixed_v1", "protect_only_v1", registry()).await;
    let id = e
        .register_plan(
            plan("late-after-close", Direction::Long, 990.),
            e.definition().await,
            e.get_config().await,
        )
        .await
        .unwrap()
        .unwrap();
    quote(&e, 1000.).await;
    let open = counter.inserts()[0].clone();
    let first = trade(&open, "first-entry", 1, 1000.);
    ready(
        &e,
        vec![order(&open, "part_traded", 1)],
        vec![first.clone()],
    )
    .await;
    quote(&e, 989.).await;
    let close = counter.inserts()[1].clone();
    let first_exit = trade(&close, "first-exit", 1, 988.);
    ready(
        &e,
        vec![
            order(&open, "part_traded", 1),
            order(&close, "all_traded", 1),
        ],
        vec![first.clone(), first_exit.clone()],
    )
    .await;
    assert!(e.state.lock().await.positions[&id].is_closed);
    drop(e);
    let mut e = counter.engine(ExecutionMode::FullAuto, 2);
    e.registry = Arc::new(registry());
    let late = trade(&open, "late-entry", 1, 1002.);
    let orders = vec![order(&open, "canceled", 2), order(&close, "all_traded", 1)];
    let fills = vec![first, first_exit, late];
    ready(&e, orders.clone(), fills.clone()).await;
    ready(&e, orders, fills).await;
    assert_eq!(e.state.lock().await.positions[&id].volume, 1);
    quote(&e, 1001.).await;
    assert_eq!(counter.inserts().len(), 3);
    assert_eq!(counter.inserts()[2]["volume"], 1);
    assert_eq!(
        counter
            .inserts()
            .iter()
            .filter(|o| o["offset"] == "open")
            .count(),
        1
    );
}

#[tokio::test]
async fn sizing_failure_after_real_fill_keeps_the_fill_and_protects_it() {
    struct FailOnFill;
    impl SizingPolicy for FailOnFill {
        fn volume(&self, _: &EntryPlan, _: &ExecutionConfig, _: &Value) -> Result<i32> {
            Ok(2)
        }
        fn volume_at_price(
            &self,
            _: &EntryPlan,
            _: &ExecutionConfig,
            _: &Value,
            c: &SizingContext<'_>,
        ) -> Result<i32> {
            anyhow::ensure!(c.filled_volume == 0, "test risk provider unavailable");
            Ok(2)
        }
    }
    let counter = Counter::new();
    let mut r = registry();
    r.register_sizing("fail-test", Arc::new(FailOnFill));
    let e = setup(&counter, "fail-test", "protect_only_v1", r).await;
    let id = e
        .register_plan(
            plan("provider-failed", Direction::Long, 990.),
            e.definition().await,
            e.get_config().await,
        )
        .await
        .unwrap()
        .unwrap();
    quote(&e, 1000.).await;
    let open = counter.inserts()[0].clone();
    ready(
        &e,
        vec![order(&open, "all_traded", 2)],
        vec![trade(&open, "real-fill", 2, 1000.)],
    )
    .await;
    {
        let s = e.state.lock().await;
        assert_eq!(s.positions[&id].volume, 2);
        assert!(
            s.paused
                && s.strategy_error
                    .as_deref()
                    .unwrap()
                    .contains("test risk provider unavailable")
        );
        assert_eq!(s.closes[&id].reason, "risk_budget");
    }
    quote(&e, 999.).await;
    assert_eq!(counter.inserts()[1]["volume"], 2);
    let persisted: ExecutionState = serde_json::from_slice(
        &std::fs::read(counter.directory.join("trader_execution.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(persisted.positions[&id].volume, 2);
    assert_eq!(persisted.seen_trades.len(), 1);
}

#[tokio::test]
async fn repeated_partial_requests_do_not_increase_the_task_quantity() {
    let counter = Counter::new();
    let (e, id, _, transitions, _) = split(&counter).await;
    quote(&e, 1012.).await;
    quote(&e, 1015.).await;
    assert_eq!(e.state.lock().await.closes[&id].target, 1);
    assert_eq!(counter.inserts().len(), 2);
    assert_eq!(transitions.load(Ordering::Relaxed), 0);
    quote(&e, 989.).await;
    assert_eq!(e.state.lock().await.closes[&id].reason, "stop");
    quote(&e, 1015.).await;
    assert_eq!(e.state.lock().await.closes[&id].reason, "stop");
    assert_eq!(e.state.lock().await.closes[&id].target, 2);
}

#[tokio::test]
async fn entry_cost_changes_rebase_observed_mfe_and_stop_moves_do_not_change_r() {
    struct Protect;
    impl Strategy for Protect {
        fn on_event(
            &self,
            event: &StrategyEvent,
            c: &StrategyContext<'_>,
            _: &mut Value,
        ) -> Result<Vec<TradeIntent>> {
            Ok(if matches!(event, StrategyEvent::Timer { .. }) {
                c.positions
                    .iter()
                    .filter(|p| !p.is_closed)
                    .map(|p| TradeIntent::Protect {
                        position_id: p.id.clone(),
                        stop_price: p.fill_price,
                    })
                    .collect()
            } else {
                vec![]
            })
        }
    }
    let counter = Counter::new();
    let mut r = registry();
    r.register_strategy("foundation-test", "1", |_| Ok(Box::new(Protect)));
    let e = setup(&counter, "fixed_v1", "protect_only_v1", r).await;
    let id = e
        .register_plan(
            plan("rebase", Direction::Long, 990.),
            e.definition().await,
            e.get_config().await,
        )
        .await
        .unwrap()
        .unwrap();
    quote(&e, 1000.).await;
    let open = counter.inserts()[0].clone();
    let first = trade(&open, "first", 1, 1000.);
    ready(
        &e,
        vec![order(&open, "part_traded", 1)],
        vec![first.clone()],
    )
    .await;
    quote(&e, 1020.).await;
    assert_eq!(e.state.lock().await.positions[&id].max_mfe_r, 2.);
    ready(
        &e,
        vec![order(&open, "all_traded", 2)],
        vec![first, trade(&open, "second", 1, 1002.)],
    )
    .await;
    {
        let s = e.state.lock().await;
        let p = &s.positions[&id];
        assert_eq!(p.risk, 11.);
        assert!((p.max_mfe_r - 19. / 11.).abs() < 1e-9);
    }
    e.dispatch(StrategyEvent::Timer {
        timestamp: chrono::Utc::now().timestamp(),
    })
    .await
    .unwrap();
    let s = e.state.lock().await;
    assert_eq!(s.positions[&id].current_stop_price, 1001.);
    assert_eq!(s.positions[&id].risk, 11.);
}

struct Passive;
impl Strategy for Passive {
    fn on_event(
        &self,
        _: &StrategyEvent,
        _: &StrategyContext<'_>,
        _: &mut Value,
    ) -> Result<Vec<TradeIntent>> {
        Ok(vec![])
    }
}

fn plan(key: &str, direction: Direction, stop: f64) -> EntryPlan {
    EntryPlan {
        signal_id: key.into(),
        symbol: "PB0".into(),
        instrument_id: "pb2611".into(),
        direction,
        reference_price: 1000.,
        trigger: EntryTrigger::Breakout { price: 1000. },
        stop_price: Some(stop),
        target_price: Some(if direction == Direction::Long {
            1200.
        } else {
            800.
        }),
        risk_per_unit: (1000. - stop).abs(),
        requested_volume: 2,
        created_at: chrono::Utc::now().timestamp(),
        expires_at: None,
        reason: "foundation test".into(),
        evidence: Value::Null,
    }
}

fn definition(sizing: &str, exit: &str) -> StrategyDefinition {
    StrategyDefinition {
        strategy_id: "foundation-test".into(),
        code_version: "1".into(),
        revision: "1".into(),
        sizing_policy: sizing.into(),
        exit_policy: exit.into(),
        parameters: Value::Null,
    }
}

async fn setup(
    counter: &Counter,
    sizing: &str,
    exit: &str,
    registry: StrategyRegistry,
) -> ExecutionEngine {
    let mut e = counter.engine(ExecutionMode::FullAuto, 2);
    e.registry = Arc::new(registry);
    e.set_definition(definition(sizing, exit)).await.unwrap();
    ready(&e, vec![], vec![]).await;
    e
}

fn registry() -> StrategyRegistry {
    let mut r = StrategyRegistry::default();
    r.register_strategy("foundation-test", "1", |_| Ok(Box::new(Passive)));
    r
}

#[tokio::test]
async fn zero_sizing_is_a_persisted_normal_rejection_without_condition_or_pause() {
    let counter = Counter::new();
    let e = setup(&counter, "fixed_v1", "protect_only_v1", registry()).await;
    let mut p = plan("zero", Direction::Long, 990.);
    p.requested_volume = 0;
    for _ in 0..2 {
        assert!(e
            .register_plan(p.clone(), e.definition().await, e.get_config().await)
            .await
            .unwrap()
            .is_none());
    }
    quote(&e, 1000.).await;
    let s = e.state.lock().await;
    assert!(!s.paused && s.strategy_error.is_none());
    assert!(s.conditions.is_empty() && s.orders.is_empty());
    assert_eq!(s.entry_decisions.len(), 1);
    assert_eq!(
        s.entry_decisions.values().next().unwrap()["reason"],
        "zero_volume"
    );
    drop(s);
    drop(e);
    let mut restored = counter.engine(ExecutionMode::FullAuto, 2);
    restored.registry = Arc::new(registry());
    assert!(restored
        .register_plan(p, restored.definition().await, restored.get_config().await)
        .await
        .unwrap()
        .is_none());
    assert_eq!(restored.state.lock().await.entry_decisions.len(), 1);
    assert!(counter.inserts().is_empty());
}

struct Budget;
impl SizingPolicy for Budget {
    fn volume(&self, _: &EntryPlan, _: &ExecutionConfig, _: &Value) -> Result<i32> {
        Ok(2)
    }
    fn volume_at_price(
        &self,
        p: &EntryPlan,
        _: &ExecutionConfig,
        _: &Value,
        c: &SizingContext<'_>,
    ) -> Result<i32> {
        let d = if p.direction == Direction::Long {
            1.
        } else {
            -1.
        };
        let exit = p.stop_price.unwrap() - d; // one adverse exit tick
        let used = d * (c.filled_notional - exit * c.filled_volume as f64) * 10.
            + 6. * c.filled_volume as f64;
        if used > 600. {
            return Ok(0);
        }
        let loss = d * (c.proposed_price - exit) * 10. + 6.;
        assert!(loss > 0. && loss.is_finite());
        Ok((c.filled_volume + ((600. - used) / loss).floor() as i32).min(2))
    }
}

#[tokio::test]
async fn proposed_limit_reduces_two_lots_to_one_in_both_directions() {
    for (direction, stop, expected) in [
        (Direction::Long, 973., 1003.),
        (Direction::Short, 1027., 997.),
    ] {
        let counter = Counter::new();
        let mut r = registry();
        r.register_sizing("budget-test", Arc::new(Budget));
        let e = setup(&counter, "budget-test", "protect_only_v1", r).await;
        let id = e
            .register_plan(
                plan("budget", direction, stop),
                e.definition().await,
                e.get_config().await,
            )
            .await
            .unwrap()
            .unwrap();
        quote(&e, 1000.).await;
        assert_eq!(counter.inserts().len(), 1);
        assert_eq!(counter.inserts()[0]["volume"], 1);
        assert_eq!(counter.inserts()[0]["price"], expected);
        assert_eq!(e.state.lock().await.conditions[&id].volume, 1);
    }
}

#[tokio::test]
async fn actual_entry_cash_survives_replay_and_excess_risk_latches_full_exit() {
    let counter = Counter::new();
    let mut r = registry();
    r.register_sizing("budget-test", Arc::new(Budget));
    let mut e = setup(&counter, "budget-test", "protect_only_v1", r.clone()).await;
    let mut cfg = e.get_config().await;
    cfg.slippage_ticks = 0;
    e.configure_strategy(definition("budget-test", "protect_only_v1"), cfg.clone())
        .await
        .unwrap();
    let id = e
        .register_plan(
            plan("actual", Direction::Long, 973.),
            e.definition().await,
            cfg,
        )
        .await
        .unwrap()
        .unwrap();
    quote(&e, 1000.).await;
    let open = counter.inserts()[0].clone();
    assert_eq!(open["volume"], 2);
    let fills = vec![
        trade(&open, "entry-a", 1, 1004.),
        trade(&open, "entry-b", 1, 1005.),
    ];
    ready(&e, vec![order(&open, "all_traded", 2)], fills.clone()).await;
    ready(&e, vec![order(&open, "all_traded", 2)], fills.clone()).await;
    assert_eq!(
        e.state
            .lock()
            .await
            .orders
            .values()
            .find(|o| o.reason.is_none())
            .unwrap()
            .entry_fill_notional,
        2009.
    );
    assert_eq!(e.state.lock().await.closes[&id].reason, "risk_budget");
    drop(e);
    e = counter.engine(ExecutionMode::FullAuto, 2);
    e.registry = Arc::new(r);
    ready(&e, vec![order(&open, "all_traded", 2)], fills).await;
    quote(&e, 1004.).await;
    assert_eq!(counter.inserts().len(), 2);
    assert_eq!(counter.inserts()[1]["offset"], "auto");
    assert_eq!(counter.inserts()[1]["volume"], 2);
}

struct SplitExit {
    transitions: Arc<AtomicUsize>,
}
impl ExitPolicy for SplitExit {
    fn on_quote(&self, p: &ManagedPosition, q: &ExecutionQuote, _: &Value) -> ExitUpdate {
        ExitUpdate {
            stop: p.current_stop_price,
            close: (!p.lot1_closed && q.last >= 1010.).then(|| ("first_s1".into(), Some(1))),
        }
    }
    fn on_partial_close(
        &self,
        p: &ManagedPosition,
        reason: &str,
        filled: i32,
        _: &Value,
        _: Option<&ExecutionQuote>,
    ) -> PartialExitUpdate {
        assert_eq!(reason, "first_s1");
        assert_eq!(filled, 1);
        self.transitions.fetch_add(1, Ordering::Relaxed);
        PartialExitUpdate {
            stop: p.fill_price,
            first_target_completed: true,
        }
    }
}

async fn split(
    counter: &Counter,
) -> (
    ExecutionEngine,
    String,
    Value,
    Arc<AtomicUsize>,
    StrategyRegistry,
) {
    let transitions = Arc::new(AtomicUsize::new(0));
    let mut r = registry();
    r.register_exit(
        "split-test",
        Arc::new(SplitExit {
            transitions: transitions.clone(),
        }),
    );
    let e = setup(counter, "fixed_v1", "split-test", r.clone()).await;
    let id = e
        .register_plan(
            plan("split", Direction::Long, 990.),
            e.definition().await,
            e.get_config().await,
        )
        .await
        .unwrap()
        .unwrap();
    quote(&e, 1000.).await;
    let open = counter.inserts()[0].clone();
    ready(
        &e,
        vec![order(&open, "all_traded", 2)],
        vec![trade(&open, "opened", 2, 1000.)],
    )
    .await;
    age(&e).await;
    quote(&e, 1010.).await;
    (e, id, open, transitions, r)
}

#[tokio::test]
async fn any_exit_policy_can_complete_a_partial_task_without_waiting_for_another_tick() {
    let counter = Counter::new();
    let (mut e, id, open, transitions, r) = split(&counter).await;
    let close = counter.inserts()[1].clone();
    let fills = vec![
        trade(&open, "opened", 2, 1000.),
        trade(&close, "first", 1, 1010.),
    ];
    let orders = vec![
        order(&open, "all_traded", 2),
        order(&close, "all_traded", 1),
    ];
    ready(&e, orders.clone(), fills.clone()).await;
    ready(&e, orders.clone(), fills.clone()).await;
    assert_eq!(transitions.load(Ordering::Relaxed), 1);
    {
        let s = e.state.lock().await;
        assert!(!s.closes.contains_key(&id));
        assert_eq!(s.positions[&id].volume, 1);
        assert!(s.positions[&id].lot1_closed);
        assert_eq!(s.positions[&id].current_stop_price, 1000.);
        assert_eq!(s.positions[&id].risk, 10.);
    }
    drop(e);
    e = counter.engine(ExecutionMode::FullAuto, 2);
    e.registry = Arc::new(r);
    ready(&e, orders, fills).await;
    assert_eq!(transitions.load(Ordering::Relaxed), 1);
    e.pause(true).await.unwrap();
    quote(&e, 999.).await;
    assert_eq!(counter.inserts().len(), 3);
    assert_eq!(counter.inserts()[2]["volume"], 1);
    assert_eq!(e.state.lock().await.closes[&id].reason, "breakeven");
}

#[tokio::test]
async fn stop_reserves_working_profit_order_and_immediately_closes_the_unreserved_lot() {
    let counter = Counter::new();
    let (e, id, open, transitions, _) = split(&counter).await;
    let profit = counter.inserts()[1].clone();
    let opened = trade(&open, "opened", 2, 1000.);
    ready(
        &e,
        vec![order(&open, "all_traded", 2), order(&profit, "queueing", 0)],
        vec![opened.clone()],
    )
    .await;
    quote(&e, 989.).await;
    assert_eq!(counter.inserts().len(), 3);
    let protective = counter.inserts()[2].clone();
    assert_eq!(protective["volume"], 1); // the other lot is reserved by the S1 order
    assert_eq!(e.state.lock().await.closes[&id].reason, "stop");
    e.drive().await.unwrap();
    assert!(counter
        .requests
        .lock()
        .unwrap()
        .iter()
        .any(|(route, r)| route == "/order/cancel" && r["order_ref"] == profit["order_ref"]));
    let orders = vec![
        order(&open, "all_traded", 2),
        order(&profit, "all_traded", 1),
        order(&protective, "all_traded", 1),
    ];
    let fills = vec![
        opened,
        trade(&profit, "late-profit", 1, 1010.),
        trade(&protective, "stop-fill", 1, 988.),
    ];
    ready(&e, orders.clone(), fills.clone()).await;
    ready(&e, orders, fills).await;
    quote(&e, 1015.).await;
    assert!(e.state.lock().await.positions[&id].is_closed);
    assert_eq!(e.get_trade_history().await.len(), 2);
    assert_eq!(counter.inserts().len(), 3);
    assert_eq!(transitions.load(Ordering::Relaxed), 0); // full stop preempted the partial transition
}

#[tokio::test]
async fn timeout_without_price_change_cancels_remainder_and_survives_restart() {
    let counter = Counter::new();
    let e = setup(&counter, "fixed_v1", "protect_only_v1", registry()).await;
    let id = e
        .register_plan(
            plan("timeout", Direction::Long, 990.),
            e.definition().await,
            e.get_config().await,
        )
        .await
        .unwrap()
        .unwrap();
    quote(&e, 1000.).await;
    let open = counter.inserts()[0].clone();
    let first = trade(&open, "first-entry", 1, 1000.);
    ready(
        &e,
        vec![order(&open, "part_traded", 1)],
        vec![first.clone()],
    )
    .await;
    {
        let mut s = e.state.lock().await;
        let o = s.orders.values_mut().next().unwrap();
        o.submitted_at -= 4;
        o.action_at = chrono::Utc::now().timestamp(); // retry/activity must not extend the deadline
    }
    e.drive().await.unwrap();
    assert_eq!(
        counter.requests.lock().unwrap().last().unwrap().0,
        "/order/cancel"
    );
    assert!(e.state.lock().await.aborted_entries.contains(&id));
    assert_eq!(e.state.lock().await.positions[&id].volume, 1);
    drop(e);
    let mut e = counter.engine(ExecutionMode::FullAuto, 2);
    e.registry = Arc::new(registry());
    ready(&e, vec![order(&open, "canceled", 1)], vec![first]).await;
    assert_eq!(e.state.lock().await.conditions[&id].volume, 1);
    age(&e).await;
    quote(&e, 1001.).await;
    assert_eq!(counter.inserts().len(), 1);
    quote(&e, 989.).await;
    assert_eq!(counter.inserts().len(), 2);
    assert_eq!(counter.inserts()[1]["volume"], 1);
}

#[tokio::test]
async fn partial_entry_stop_does_not_wait_for_the_open_order_cancel_ack() {
    let counter = Counter::new();
    let e = setup(&counter, "fixed_v1", "protect_only_v1", registry()).await;
    let id = e
        .register_plan(
            plan("partial-stop", Direction::Long, 990.),
            e.definition().await,
            e.get_config().await,
        )
        .await
        .unwrap()
        .unwrap();
    quote(&e, 1000.).await;
    let open = counter.inserts()[0].clone();
    ready(
        &e,
        vec![order(&open, "part_traded", 1)],
        vec![trade(&open, "first", 1, 1000.)],
    )
    .await;
    quote(&e, 989.).await;
    assert_eq!(counter.inserts().len(), 2);
    assert_eq!(counter.inserts()[1]["volume"], 1);
    assert_eq!(e.state.lock().await.closes[&id].reason, "stop");
    e.drive().await.unwrap();
    assert!(counter
        .requests
        .lock()
        .unwrap()
        .iter()
        .any(|(route, _)| route == "/order/cancel"));
}
