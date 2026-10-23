//! Offline legacy replay for execution audits; no database or broker connection.
use anyhow::{ensure, Context, Result};
use n_core::{
    derive::rollover::RolloverRecord,
    fetch::kline::Kline,
    trader::ExecutionConfig,
    v2::replay::ReplayEvent,
    v3::ablation::{self, Experiment, Policy},
};
use serde::Deserialize;
use serde_json::json;
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    io::{BufWriter, Write},
    path::Path,
};

#[derive(Deserialize)]
struct Roll {
    symbol: String,
    ts: String,
    from_contract: String,
    to_contract: String,
    confirmed: bool,
}
#[derive(Deserialize)]
struct RawSymbol {
    variety: String,
    bars: Vec<Kline>,
    rollovers: Vec<Roll>,
}
#[derive(Deserialize)]
struct Input {
    symbols: BTreeMap<String, RawSymbol>,
    execution_config: ExecutionConfig,
}

fn main() -> Result<()> {
    let args = std::env::args().collect::<Vec<_>>();
    ensure!(
        args.len() == 4,
        "usage: signal_execution_audit <input.json> <signals.json> <new-output>"
    );
    ensure!(
        chrono::Local::now().offset().local_minus_utc() == 28800,
        "requires Shanghai timezone"
    );
    let input_bytes = std::fs::read(&args[1])?;
    let signal_bytes = std::fs::read(&args[2])?;
    let input: Input = serde_json::from_slice(&input_bytes)?;
    let signals: Vec<ReplayEvent> = serde_json::from_slice(&signal_bytes)?;
    let out = Path::new(&args[3]);
    ensure!(!out.exists(), "output already exists");
    std::fs::create_dir_all(out)?;
    let mut rows = BufWriter::new(
        std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(out.join("evaluations.jsonl"))?,
    );
    for ev in &signals {
        let raw = input.symbols.get(&ev.symbol).context("missing symbol")?;
        let rolls = raw
            .rollovers
            .iter()
            .map(|r| RolloverRecord {
                symbol: r.symbol.clone(),
                ts: r.ts.clone(),
                from_contract: r.from_contract.clone(),
                to_contract: r.to_contract.clone(),
                confirmed: r.confirmed,
            })
            .collect::<Vec<_>>();
        let mult = n_core::precision::effective_multiplier(&ev.symbol, &raw.variety);
        let key = format!(
            "{}|{}|{}|{}|{}|{}",
            ev.symbol, ev.direction, ev.s0_ts, ev.s1_ts, ev.s2_ts, ev.warning_ts
        );
        for slip in [1., 2.] {
            let result = ablation::evaluate(
                ev,
                &raw.bars,
                &rolls,
                &input.execution_config,
                Experiment {
                    policy: Policy::ActualTier,
                    no_follow_bar: Some(5),
                    slip_ticks: slip,
                    fee_ticks: 0.,
                },
                mult,
            );
            writeln!(
                rows,
                "{}",
                json!({"event_key":key,"symbol":ev.symbol,"direction":ev.direction,
                "warning_ts":ev.warning_ts,"score":ev.setup_features.setup_quality,"rr":ev.rr,
                "tick_size":ev.tick_size,"multiplier":mult,"scenario":format!("LEGACY/slip{}",slip as i32),"result":result})
            )?;
        }
    }
    rows.flush()?;
    std::fs::write(
        out.join("manifest.json"),
        serde_json::to_vec_pretty(&json!({
        "input_sha256":format!("{:x}",Sha256::digest(&input_bytes)),
        "signals_sha256":format!("{:x}",Sha256::digest(&signal_bytes)),
        "engine_sha256":format!("{:x}",Sha256::digest(include_str!("../v3/ablation.rs").as_bytes())),
        "runner_sha256":format!("{:x}",Sha256::digest(include_str!("signal_execution_audit.rs").as_bytes())),
        "signals":signals.len(),"slips":[1,2],"execution_config":input.execution_config,
        "policy":"actual_tier, nf5, structural stop, legacy partial and trailing exits",
        "fee_ticks":0,"commission":"reprice exact fill legs with frozen fee profile later",
        "source":"counterfactual OHLC replay, not corrected actual fills","production_changed":false}))?,
    )?;
    println!("signals={} cases=2", signals.len());
    Ok(())
}
