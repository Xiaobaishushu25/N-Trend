//! Offline one-lot structural/cost guards and exit candidates. No account cap.
use anyhow::{ensure, Context, Result};
use n_core::{
    derive::rollover::RolloverRecord,
    fetch::kline::Kline,
    trader::ExecutionConfig,
    v2::replay::ReplayEvent,
    v3::{
        ablation::{self, EntryGuard, Experiment, FeeRule, Policy, ResultRow},
        replay::Evaluation,
    },
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
}
fn writer(path: &Path) -> Result<BufWriter<std::fs::File>> {
    Ok(BufWriter::new(
        std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(path)?,
    ))
}
fn fee_rule(value: &Value) -> Result<FeeRule> {
    let rule: FeeRule = serde_json::from_value(value.clone())?;
    ensure!(
        rule.fixed_yuan_per_lot.is_finite()
            && rule.fixed_yuan_per_lot >= 0.
            && rule.notional_rate.is_finite()
            && rule.notional_rate >= 0.,
        "invalid fee rule"
    );
    Ok(rule)
}
fn guard(spec: &Value) -> Result<EntryGuard> {
    let mut g = EntryGuard {
        min_net_reward_loss: 1.,
        assumed_slip_ticks: 1.,
        open: FeeRule::default(),
        close_today: FeeRule::default(),
        close_yesterday: FeeRule::default(),
        roundtrip_fixed_override: None,
    };
    if let Some(fee) = spec.get("roundtrip_fixed_yuan_per_lot") {
        let fee = fee.as_f64().context("invalid roundtrip override")?;
        ensure!(fee.is_finite() && fee >= 0., "invalid roundtrip fee");
        g.roundtrip_fixed_override = Some(fee);
    } else {
        g.open = fee_rule(spec.get("open").context("missing open fee")?)?;
        g.close_today = fee_rule(spec.get("close_today").context("missing today fee")?)?;
        g.close_yesterday = fee_rule(
            spec.get("close_yesterday")
                .context("missing yesterday fee")?,
        )?;
    }
    Ok(g)
}
fn main() -> Result<()> {
    let args = std::env::args().collect::<Vec<_>>();
    ensure!(args.len()==5,"usage: signal-optimize <frozen-input.json> <signals.json> <fee-profile.json> <new-output-dir>");
    ensure!(
        chrono::Local::now().offset().local_minus_utc() == 8 * 3600,
        "requires Asia/Shanghai local timezone"
    );
    let input_bytes = std::fs::read(&args[1])?;
    let signal_bytes = std::fs::read(&args[2])?;
    let fee_bytes = std::fs::read(&args[3])?;
    let input: Input = serde_json::from_slice(&input_bytes)?;
    let signals: Vec<ReplayEvent> = serde_json::from_slice(&signal_bytes)?;
    let fees: Value = serde_json::from_slice(&fee_bytes)?;
    let out = Path::new(&args[4]);
    ensure!(!out.exists(), "refuse to overwrite frozen research");
    std::fs::create_dir(out)?;
    let definitions = json!([
        {"name":"s1_baseline","planned_rr":false,"net_guard":false,"protect":false,"no_follow":true},
        {"name":"s1_planned_rr1","planned_rr":true,"net_guard":false,"protect":false,"no_follow":true},
        {"name":"s1_net_rr1","planned_rr":false,"net_guard":true,"protect":false,"no_follow":true},
        {"name":"s1_net_rr1_no_follow_off","planned_rr":false,"net_guard":true,"protect":false,"no_follow":false},
        {"name":"s1_net_rr1_protect_at1r","planned_rr":false,"net_guard":true,"protect":true,"no_follow":true}
    ]);
    let manifest = json!({"source":"historical_development","account_cap":null,"volume":1,
        "definitions":definitions,"slippage_ticks_per_side":[0,1,2],"primary_slippage":1,"stress_slippage":2,
        "entry_guard_assumed_slip_ticks":1,"warning_guard_frozen_under_stress":true,
        "engine_source_sha256":format!("{:x}",Sha256::digest(include_str!("../v3/ablation.rs").as_bytes())),
        "runner_source_sha256":format!("{:x}",Sha256::digest(include_str!("signal_optimize.rs").as_bytes())),
        "gate_fee":"open plus worse of close_today/close_yesterday at proposed target and stop fills; SR user override",
        "net_guard":"at warning and rechecked at proposed fill; net target reward >= full stop loss including fees and slip",
        "input_sha256":format!("{:x}",Sha256::digest(&input_bytes)),"signals_sha256":format!("{:x}",Sha256::digest(&signal_bytes)),
        "fees_sha256":format!("{:x}",Sha256::digest(&fee_bytes)),"qualified_for_trading":false,
        "intrabar":"O-H-L-C/O-L-H-C agreement only; not exhaustive bounds", "production_changed":false});
    writeln!(
        writer(&out.join("manifest.json"))?,
        "{}",
        serde_json::to_string_pretty(&manifest)?
    )?;
    let mut rows = writer(&out.join("evaluations.jsonl"))?;
    let mut candidates = 0;
    for ev in signals {
        if ev.setup_features.setup_quality < 2.5
            || ev.risk / ev.tick_size + 1e-6 < input.execution_config.min_entry_risk_ticks as f64
        {
            continue;
        }
        candidates += 1;
        let raw = input
            .symbols
            .get(&ev.symbol)
            .context("missing raw symbol")?;
        let rolls: Vec<_> = raw
            .rollovers
            .iter()
            .map(|r| RolloverRecord {
                symbol: r.symbol.clone(),
                ts: r.ts.clone(),
                from_contract: r.from_contract.clone(),
                to_contract: r.to_contract.clone(),
                confirmed: r.confirmed,
            })
            .collect();
        let multiplier = n_core::precision::effective_multiplier(&ev.symbol, &raw.variety);
        let product = ev
            .symbol
            .trim_end_matches(|c: char| c.is_ascii_digit())
            .to_uppercase();
        let fee = guard(
            fees["products"]
                .get(&product)
                .context("missing product fee")?,
        )?;
        let sign = if ev.direction == "up" { 1. } else { -1. };
        let planned_rr = (ev.target - ev.entry) * sign / ev.risk;
        let key = format!(
            "{}|{}|{}|{}|{}|{}",
            ev.symbol, ev.direction, ev.s0_ts, ev.s1_ts, ev.s2_ts, ev.warning_ts
        );
        for def in definitions.as_array().unwrap() {
            for slip in [0., 1., 2.] {
                let ex = Experiment {
                    policy: if def["protect"] == true {
                        Policy::S1ProtectOneR
                    } else {
                        Policy::OriginalS1
                    },
                    no_follow_bar: if def["no_follow"] == true {
                        Some(5)
                    } else {
                        None
                    },
                    slip_ticks: slip,
                    fee_ticks: 0.,
                };
                let result = if def["planned_rr"] == true && planned_rr + 1e-9 < 1. {
                    ResultRow {
                        evaluation: Evaluation {
                            status: "filtered_planned_rr".into(),
                            ..Default::default()
                        },
                        disagreement: None,
                        fills: vec![],
                    }
                } else {
                    ablation::evaluate_guarded(
                        &ev,
                        &raw.bars,
                        &rolls,
                        &input.execution_config,
                        ex,
                        multiplier,
                        if def["net_guard"] == true {
                            Some(fee)
                        } else {
                            None
                        },
                    )
                };
                writeln!(
                    rows,
                    "{}",
                    json!({"event_key":key,"symbol":ev.symbol,"direction":ev.direction,
                    "warning_ts":ev.warning_ts,"score":ev.setup_features.setup_quality,"rr":planned_rr,
                    "tick_size":ev.tick_size,"multiplier":multiplier,"scenario":format!("{}/slip{}",def["name"].as_str().unwrap(),slip as i32),
                    "result":result})
                )?;
            }
        }
    }
    rows.flush()?;
    println!(
        "candidates={candidates}, cases=15; frozen output={}",
        out.display()
    );
    Ok(())
}
