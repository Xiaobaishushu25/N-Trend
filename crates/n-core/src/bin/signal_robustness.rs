//! Offline, protocol-driven replay. Shares the frozen research-v5 execution engine.
//! No production database, broker, registration, or strategy mutation.
use anyhow::{ensure, Context, Result};
use n_core::{
    analyze::{event::replay_warnings, model::Dir},
    derive::rollover::RolloverRecord,
    fetch::kline::Kline,
    trader::ExecutionConfig,
    v2::replay::{ReplayEngine, ReplayEvent},
    v3::ablation::{self, EntryGuard, Experiment, HolidayWindow, Policy, ResearchRules},
};
use serde::Deserialize;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    io::{BufWriter, Write},
    path::Path,
};

#[derive(Deserialize)]
struct Raw {
    variety: String,
    bars: Vec<Kline>,
    #[serde(deserialize_with = "rolls")]
    rollovers: Vec<RolloverRecord>,
}
fn rolls<'de, D: serde::Deserializer<'de>>(
    d: D,
) -> std::result::Result<Vec<RolloverRecord>, D::Error> {
    #[derive(Deserialize)]
    struct R {
        symbol: String,
        ts: String,
        from_contract: String,
        to_contract: String,
        confirmed: bool,
    }
    Ok(Vec::<R>::deserialize(d)?
        .into_iter()
        .map(|r| RolloverRecord {
            symbol: r.symbol,
            ts: r.ts,
            from_contract: r.from_contract,
            to_contract: r.to_contract,
            confirmed: r.confirmed,
        })
        .collect())
}
#[derive(Deserialize)]
struct Input {
    symbols: BTreeMap<String, Raw>,
    execution_config: ExecutionConfig,
}
#[derive(Deserialize)]
struct Template {
    name: String,
    policy: Policy,
    no_follow_bar: Option<usize>,
    rules: ResearchRules,
    #[serde(default)]
    uniform_two_lot: bool,
}
fn guard(v: &Value) -> Result<EntryGuard> {
    let mut g = EntryGuard {
        min_net_reward_loss: 1.,
        assumed_slip_ticks: 1.,
        open: Default::default(),
        close_today: Default::default(),
        close_yesterday: Default::default(),
        roundtrip_fixed_override: None,
    };
    if let Some(x) = v.get("roundtrip_fixed_yuan_per_lot") {
        g.roundtrip_fixed_override = Some(x.as_f64().context("fee override")?);
    } else {
        g.open = serde_json::from_value(v["open"].clone())?;
        g.close_today = serde_json::from_value(v["close_today"].clone())?;
        g.close_yesterday = serde_json::from_value(v["close_yesterday"].clone())?;
    }
    Ok(g)
}
fn key(e: &ReplayEvent) -> String {
    format!(
        "{}|{}|{}|{}|{}|{}",
        e.symbol, e.direction, e.s0_ts, e.s1_ts, e.s2_ts, e.warning_ts
    )
}
fn main() -> Result<()> {
    let a: Vec<_> = std::env::args().collect();
    ensure!(
        a.len() == 7,
        "usage: signal_robustness <input> <signals> <fees> <holidays> <protocol> <new-output>"
    );
    ensure!(
        chrono::Local::now().offset().local_minus_utc() == 28800,
        "requires Shanghai timezone"
    );
    let blobs: Vec<_> = a[1..6]
        .iter()
        .map(std::fs::read)
        .collect::<std::io::Result<_>>()?;
    let input: Input = serde_json::from_slice(&blobs[0])?;
    let events: Vec<ReplayEvent> = serde_json::from_slice(&blobs[1])?;
    let fees: Value = serde_json::from_slice(&blobs[2])?;
    let holidays: Value = serde_json::from_slice(&blobs[3])?;
    let windows: Vec<HolidayWindow> = serde_json::from_value(holidays["windows"].clone())?;
    let protocol: Value = serde_json::from_slice(&blobs[4])?;
    let templates: Vec<Template> = serde_json::from_value(protocol["templates"].clone())?;
    for (i, field) in ["input", "signals", "fees", "holidays"].iter().enumerate() {
        ensure!(
            protocol["hashes"][field].as_str() == Some(&format!("{:x}", Sha256::digest(&blobs[i]))),
            "protocol input hash mismatch: {field}"
        );
    }
    for raw in input.symbols.values() {
        for b in &raw.bars {
            ensure!(
                b.datetime.as_str()
                    >= holidays["coverage_start"]
                        .as_str()
                        .context("calendar start")?
                    && b.datetime.as_str()
                        <= holidays["coverage_end"].as_str().context("calendar end")?,
                "calendar coverage"
            );
        }
    }
    let out = Path::new(&a[6]);
    ensure!(!out.exists(), "immutable output exists");
    std::fs::create_dir_all(out)?;
    let mut scores = BTreeMap::new();
    let engine = ReplayEngine::default_engine();
    for (symbol, raw) in &input.symbols {
        let tick = events
            .iter()
            .find(|e| e.symbol == *symbol)
            .context("symbol signals")?
            .tick_size;
        let (bars, _) = engine.aggregate_with_rollovers(&raw.bars, &raw.rollovers);
        for c in replay_warnings(symbol, &bars, tick) {
            let direction = if c.direction == Dir::Up { "up" } else { "down" };
            let k = format!(
                "{}|{}|{}|{}|{}|{}",
                symbol,
                direction,
                bars[c.s0_index].dt.to_bar_ts(),
                bars[c.s1_index].dt.to_bar_ts(),
                bars[c.s2_index].dt.to_bar_ts(),
                bars[c.warning_index].dt.to_bar_ts()
            );
            scores.insert(k,json!({"dim_a":c.dim_a,"dim_b":c.dim_b,"dim_warning":c.dim_warning,"total":c.entry_score}));
        }
    }
    let mut writer = BufWriter::new(
        std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(out.join("evaluations.jsonl"))?,
    );
    let mut count = 0;
    for ev in &events {
        if ev.risk <= 0. || ev.setup_features.setup_quality < 2.5 {
            continue;
        }
        let k = key(ev);
        let dims = scores.get(&k).context("warning components not found")?;
        ensure!(
            (dims["total"].as_f64().unwrap() - ev.setup_features.setup_quality).abs() < 1e-9,
            "score reconstruction mismatch"
        );
        let raw = &input.symbols[&ev.symbol];
        let mult = n_core::precision::effective_multiplier(&ev.symbol, &raw.variety);
        let prod = ev
            .symbol
            .trim_end_matches(|c: char| c.is_ascii_digit())
            .to_uppercase();
        let fee = guard(fees["products"].get(&prod).context("missing fee")?)?;
        let mut cfg = input.execution_config.clone();
        cfg.min_entry_risk_ticks = 10;
        for t in &templates {
            let mut execution_event = ev.clone();
            if t.uniform_two_lot {
                ensure!(
                    t.policy == Policy::ActualTier,
                    "uniform two-lot requires actual-tier policy"
                );
                // Explicit research control: uniform two-lot initial exposure,
                // first lot at 1R, remaining lot follows the EXISTING legacy policy.
                // Original score remains in output and all admission filters.
                execution_event.setup_features.setup_quality = 5.;
                cfg.default_large_volume = 2;
            }
            for slip in [1., 2.] {
                let r = ablation::evaluate_research_with_holidays(
                    &execution_event,
                    &raw.bars,
                    &raw.rollovers,
                    &cfg,
                    Experiment {
                        policy: t.policy,
                        no_follow_bar: t.no_follow_bar,
                        slip_ticks: slip,
                        fee_ticks: 0.,
                    },
                    mult,
                    Some(fee),
                    t.rules,
                    &windows,
                );
                writeln!(
                    writer,
                    "{}",
                    json!({"event_key":k,"symbol":ev.symbol,"direction":ev.direction,
                    "warning_ts":ev.warning_ts,"score":ev.setup_features.setup_quality,"rr":ev.rr,
                    "tick_size":ev.tick_size,"multiplier":mult,"planned_risk":ev.risk,
                    "planned_risk_ticks":ev.risk/ev.tick_size,"score_components":dims,
                    "scenario":format!("{}/slip{}",t.name,slip as u32),"result":r})
                )?;
            }
        }
        count += 1;
    }
    writer.flush()?;
    let manifest = json!({"cohort":count,"template_count":templates.len(),"protocol":protocol,
        "protocol_sha256":format!("{:x}",Sha256::digest(&blobs[4])),
        "runner_sha256":format!("{:x}",Sha256::digest(include_str!("signal_robustness.rs"))),
        "engine_sha256":format!("{:x}",Sha256::digest(include_str!("../v3/ablation.rs"))),
        "production_changed":false,"qualified_for_trading":false});
    std::fs::write(
        out.join("manifest.json"),
        serde_json::to_vec_pretty(&manifest)?,
    )?;
    println!(
        "cohort={count}, templates={}, exact warning components verified",
        templates.len()
    );
    Ok(())
}
