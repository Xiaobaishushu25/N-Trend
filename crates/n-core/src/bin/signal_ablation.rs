//! Run frozen, read-only exported research inputs. Never opens a database or broker.
use anyhow::{ensure, Context, Result};
use n_core::{
    derive::rollover::RolloverRecord,
    fetch::kline::Kline,
    trader::ExecutionConfig,
    v2::replay::ReplayEvent,
    v3::{
        ablation::{self, Experiment, Policy},
        replay,
    },
};
use serde::Deserialize;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    io::{BufWriter, Write},
    path::{Path, PathBuf},
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
    bars: Vec<Kline>,
    rollovers: Vec<Roll>,
    variety: String,
}
#[derive(Deserialize)]
struct Input {
    symbols: BTreeMap<String, RawSymbol>,
    execution_config: ExecutionConfig,
    provenance: Value,
}
fn read(path: &Path) -> Result<Vec<u8>> {
    Ok(std::fs::read(path)?)
}
fn writer(path: &Path) -> Result<BufWriter<std::fs::File>> {
    Ok(BufWriter::new(
        std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(path)?,
    ))
}
fn delta(a: &Value, b: &Value) -> f64 {
    match (a, b) {
        (Value::Number(x), Value::Number(y)) => (x.as_f64().unwrap() - y.as_f64().unwrap()).abs(),
        (Value::Object(x), Value::Object(y)) => {
            if x.len() != y.len() {
                return f64::INFINITY;
            }
            x.iter()
                .map(|(k, v)| y.get(k).map(|w| delta(v, w)).unwrap_or(f64::INFINITY))
                .fold(0., f64::max)
        }
        _ => {
            if a == b {
                0.
            } else {
                f64::INFINITY
            }
        }
    }
}
fn main() -> Result<()> {
    let args = std::env::args().collect::<Vec<_>>();
    ensure!(
        args.len() == 4,
        "usage: signal-ablation <input.json> <signals.json> <new-output-dir>"
    );
    ensure!(
        chrono::Local::now().offset().local_minus_utc() == 8 * 3600,
        "session adapter requires Asia/Shanghai local timezone"
    );
    let input_bytes = read(Path::new(&args[1]))?;
    let signal_bytes = read(Path::new(&args[2]))?;
    let input: Input = serde_json::from_slice(&input_bytes)?;
    let events: Vec<ReplayEvent> = serde_json::from_slice(&signal_bytes)?;
    let out = PathBuf::from(&args[3]);
    ensure!(
        !out.exists(),
        "output must be new; frozen batches cannot be overwritten"
    );
    std::fs::create_dir(&out)?;
    let mut golden = writer(&out.join("baseline.jsonl"))?;
    let mut rows = writer(&out.join("evaluations.jsonl"))?;
    let mut max_delta = 0f64;
    let mut eligible = 0usize;
    let mut golden_count = 0usize;
    let mut experiments = Vec::<(String, Experiment)>::new();
    for policy in [
        Policy::OriginalS1,
        Policy::FixedOneR,
        Policy::LegacyStaircase,
        Policy::ActualTier,
    ] {
        let policy_name = serde_json::to_value(policy)?.as_str().unwrap().to_string();
        for (timing, no_follow_bar) in [("nf5", Some(5)), ("nf_off", None)] {
            for (cost, slip_ticks, fee_ticks) in [
                ("zero", 0., 0.),
                ("fee_only", 0., 2.5),
                ("slip1_fee", 1., 2.5),
                ("slip2_fee", 2., 2.5),
            ] {
                experiments.push((
                    format!("{policy_name}/{timing}/{cost}"),
                    Experiment {
                        policy,
                        no_follow_bar,
                        slip_ticks,
                        fee_ticks,
                    },
                ));
            }
        }
    }
    // Predeclared timing diagnostic; never optimize an arbitrary timeout grid.
    for policy in [Policy::LegacyStaircase, Policy::ActualTier] {
        let name = serde_json::to_value(policy)?.as_str().unwrap().to_string();
        experiments.push((
            format!("{name}/nf6/slip1_fee"),
            Experiment {
                policy,
                no_follow_bar: Some(6),
                slip_ticks: 1.,
                fee_ticks: 2.5,
            },
        ));
    }
    for ev in &events {
        let symbol = input
            .symbols
            .get(&ev.symbol)
            .context("missing symbol input")?;
        let rolls = symbol
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
        let multiplier = n_core::precision::effective_multiplier(&ev.symbol, &symbol.variety);
        let key = format!(
            "{}|{}|{}|{}|{}|{}",
            ev.symbol, ev.direction, ev.s0_ts, ev.s1_ts, ev.s2_ts, ev.warning_ts
        );
        let mut baseline =
            replay::evaluate_config(ev, &symbol.bars, &rolls, false, 1., &input.execution_config);
        if baseline.status == "closed" {
            let net = baseline.gross_points * multiplier
                - 2.5 * ev.tick_size * multiplier * baseline.volume as f64;
            baseline.net_amount = Some(net);
            baseline.net_r =
                Some(net / (baseline.risk * multiplier * baseline.volume as f64).max(1e-9));
        }
        let difference = delta(
            &serde_json::to_value(&baseline)?,
            &serde_json::to_value(&ev.evaluation)?,
        );
        ensure!(
            difference < 1e-7,
            "golden baseline mismatch for {key}: {difference}; stop before comparing policies"
        );
        max_delta = max_delta.max(difference);
        golden_count += 1;
        writeln!(
            golden,
            "{}",
            json!({"event_key":key,"symbol":ev.symbol,"evaluation":baseline})
        )?;
        if baseline.status == "filtered" {
            continue;
        }
        eligible += 1;
        for (scenario, ex) in &experiments {
            let result = ablation::evaluate(
                ev,
                &symbol.bars,
                &rolls,
                &input.execution_config,
                *ex,
                multiplier,
            );
            writeln!(
                rows,
                "{}",
                json!({"event_key":key,"symbol":ev.symbol,"direction":ev.direction,"warning_ts":ev.warning_ts,"score":ev.setup_features.setup_quality,"rr":ev.rr,"tick_size":ev.tick_size,"multiplier":multiplier,"scenario":scenario,"result":result})
            )?;
        }
        if eligible % 100 == 0 {
            println!(
                "candidates={eligible} golden={golden_count}/{}",
                events.len()
            );
        }
    }
    golden.flush()?;
    rows.flush()?;
    let manifest = json!({"contract":"frozen-exit-ablation-v1","source":"historical_development","input_blake3":blake3::hash(&input_bytes).to_hex().to_string(),"signals_blake3":blake3::hash(&signal_bytes).to_hex().to_string(),"provenance":input.provenance,"golden_baseline_checked":golden_count,"golden_max_numeric_delta":max_delta,"candidates":eligible,"filtered":events.len()-eligible,"experiments":experiments,"execution_config":input.execution_config,"intrabar_method":"Compare O-H-L-C and O-L-H-C; disagreeing outcomes/states are ambiguous. Agreement does not exhaust all oscillating paths.","costs":"2.5 tick fees are a research proxy, not actual exchange/broker commissions.","portfolio":"Independent signals; report generator separately replays one-active-opportunity-per-symbol reservation.","qualified_for_trading":false});
    let mut manifest = manifest;
    manifest["input_sha256"] = json!(format!("{:x}", Sha256::digest(&input_bytes)));
    let mut manifest_file = writer(&out.join("manifest.json"))?;
    writeln!(
        manifest_file,
        "{}",
        serde_json::to_string_pretty(&manifest)?
    )?;
    manifest_file.flush()?;
    println!(
        "completed: golden={golden_count}, candidates={eligible}, scenarios={}, out={}",
        experiments.len(),
        out.display()
    );
    Ok(())
}
