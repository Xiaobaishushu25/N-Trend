//! Deterministic allocation regression: a long-lived decision archive must not
//! be copied by every market callback. No network, orders or account credentials.
use n_core::trader::{
    b5, candidates, model::ExecutionQuote, strategy::*, CtpTraderClient, ExecutionConfig,
    ExecutionEngine,
};
use serde_json::{json, Value};
use std::{
    alloc::{GlobalAlloc, Layout, System},
    cell::Cell,
    sync::Arc,
    time::Instant,
};

struct Allocator;
thread_local! {static COUNT:Cell<Option<usize>>=const {Cell::new(None)};}
unsafe impl GlobalAlloc for Allocator {
    unsafe fn alloc(&self, l: Layout) -> *mut u8 {
        let _ = COUNT.try_with(|n| {
            if let Some(v) = n.get() {
                n.set(Some(v + 1));
            }
        });
        System.alloc(l)
    }
    unsafe fn dealloc(&self, p: *mut u8, l: Layout) {
        System.dealloc(p, l)
    }
    unsafe fn realloc(&self, p: *mut u8, l: Layout, n: usize) -> *mut u8 {
        let _ = COUNT.try_with(|n| {
            if let Some(v) = n.get() {
                n.set(Some(v + 1));
            }
        });
        System.realloc(p, l, n)
    }
}
#[global_allocator]
static ALLOCATOR: Allocator = Allocator;
struct FullCallback(Box<dyn Strategy>);
impl Strategy for FullCallback {
    fn handles_event(&self, e: &StrategyEvent) -> bool {
        self.0.handles_event(e)
    }
    fn on_event(
        &self,
        e: &StrategyEvent,
        c: &StrategyContext<'_>,
        s: &mut Value,
    ) -> anyhow::Result<Vec<TradeIntent>> {
        self.0.on_event(e, c, s)
    }
}
fn event() -> StrategyEvent {
    StrategyEvent::Quote(ExecutionQuote {
        instrument_id: "pb2701".into(),
        exchange_id: "SHFE".into(),
        underlying_symbol: "PB0".into(),
        last: 1000.,
        bid: 999.,
        ask: 1001.,
        tick: 1.,
        lower: 500.,
        upper: 2000.,
        timestamp: chrono::Utc::now().timestamp(),
    })
}
fn measure(f: impl FnOnce()) -> (usize, std::time::Duration) {
    COUNT.with(|n| n.set(Some(0)));
    let now = Instant::now();
    f();
    let elapsed = now.elapsed();
    (COUNT.with(|n| n.replace(None).unwrap()), elapsed)
}

#[test]
fn native_quote_cost_does_not_copy_twenty_thousand_historical_decisions() {
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    let defs = [
        candidates::definition(candidates::P2, candidates::Parameters::reference_snapshot()),
        candidates::p3::definition(candidates::p3::P3Parameters::reference_snapshot()),
        b5::definition(),
    ];
    for mut def in defs {
        def.revision = "load-regression".into();
        let root = std::env::temp_dir().join(format!("ntrend-quote-load-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&root).unwrap();
        let archive=(0..20_000).map(|i|(format!("signal-{i}"),json!({"reason":"filtered_net_rr_plan","values":{"risk":20.,"score":3.5,"evidence":"frozen historical evidence retained across restarts"}}))).collect::<serde_json::Map<_,_>>();
        let mut state = if def.strategy_id == b5::ID {
            json!({"raw_cursor":{},"cursor":{},"dedup":{},"status":{},"minutes":{},"confirmed":{},"released":{},"lifecycle":null})
        } else {
            json!({"seen":[],"pending":{},"holding":{}})
        };
        state["decisions"] = Value::Object(archive);
        let cfg = ExecutionConfig::default();
        let serialized =
            json!({"definition":def,"active_config":cfg,"strategy_states":{&def.revision:state}});
        for dir in ["scoped", "full"] {
            std::fs::create_dir_all(root.join(dir)).unwrap();
            std::fs::write(
                root.join(dir).join("trader_execution.json"),
                serde_json::to_vec(&serialized).unwrap(),
            )
            .unwrap();
        }
        let optimized = ExecutionEngine::with_config_path(
            CtpTraderClient::for_port(1),
            cfg.clone(),
            Some(root.join("scoped/trader_config.json")),
        );
        let mut full = ExecutionEngine::with_config_path(
            CtpTraderClient::for_port(1),
            cfg,
            Some(root.join("full/trader_config.json")),
        );
        let mut registry = StrategyRegistry::default();
        let template = def.clone();
        registry.register_strategy(&def.strategy_id, &def.code_version, move |parameters| {
            let mut definition = template.clone();
            definition.parameters = parameters.clone();
            Ok(Box::new(FullCallback(
                StrategyRegistry::default().strategy(&definition)?,
            )))
        });
        full.registry = Arc::new(registry);
        let e = event();
        runtime.block_on(async {
            optimized.dispatch(e.clone()).await.unwrap();
            full.dispatch(e.clone()).await.unwrap();
        });
        let (new, new_time) = measure(|| {
            runtime.block_on(async {
                for _ in 0..8 {
                    optimized.dispatch(e.clone()).await.unwrap();
                }
            })
        });
        let (old, old_time) = measure(|| {
            runtime.block_on(async {
                for _ in 0..8 {
                    full.dispatch(e.clone()).await.unwrap();
                }
            })
        });
        println!("{} / 20000 archived decisions / 8 quotes: full={old} allocations {old_time:?}, scoped={new} allocations {new_time:?}",def.strategy_id);
        assert!(
            new * 20 < old,
            "market callback must not allocate proportional to the decision archive"
        );
        runtime.block_on(async {
            assert!(
                optimized.strategy_diagnostics().await["states"]
                    == full.strategy_diagnostics().await["states"],
                "full and scoped callbacks must agree"
            );
        });
        let StrategyEvent::Quote(q) = event() else {
            unreachable!()
        };
        let batch = vec![("PB0".into(), q); 512];
        let batch_started = Instant::now();
        runtime.block_on(optimized.on_quote_batch(batch)).unwrap();
        println!(
            "{} / 512 ordered ticks including durable full-state checkpoint: {:?}",
            def.strategy_id,
            batch_started.elapsed()
        );
        runtime.block_on(async {
            assert_eq!(
                optimized.strategy_diagnostics().await["states"][&def.revision]["decisions"]
                    .as_object()
                    .unwrap()
                    .len(),
                20_000
            );
        });
        drop(optimized);
        drop(full);
        let resolved = root.canonicalize().unwrap();
        assert!(resolved.starts_with(std::env::temp_dir().canonicalize().unwrap()));
        std::fs::remove_dir_all(resolved).unwrap();
    }
}
