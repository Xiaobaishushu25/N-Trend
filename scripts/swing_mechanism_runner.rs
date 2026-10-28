//! Diagnostic only: frozen entries for first passage, and filter population ablation.
use anyhow::{ensure, Result};
use n_core::{
    fetch::kline::Kline,
    trader::ExecutionConfig,
    v2::replay::ReplayEvent,
    v3::ablation::{self, EntryGuard, Experiment, HolidayWindow, Policy, ResearchRules},
};
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    fs,
    io::{BufWriter, Write},
    path::Path,
};
fn read(p: &str) -> Result<Value> {
    Ok(serde_json::from_slice(&fs::read(p)?)?)
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
        g.roundtrip_fixed_override = x.as_f64();
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
fn grid(x: f64, t: f64, up: bool) -> f64 {
    if up {
        (x / t - 1e-9).ceil() * t
    } else {
        (x / t + 1e-9).floor() * t
    }
}
fn main() -> Result<()> {
    let a: Vec<_> = std::env::args().collect();
    ensure!(a.len() == 5, "protocol period cohorts newout");
    let p = read(&a[1])?;
    let paths = &p["periods"][&a[2]];
    let inp = read(paths["input"].as_str().unwrap())?;
    let data = read(paths["contracts"].as_str().unwrap())?;
    let fees = read(paths["fees"].as_str().unwrap())?;
    let maps = read(paths["segments"].as_str().unwrap())?;
    let hol = read(paths["holidays"].as_str().unwrap())?;
    let windows: Vec<HolidayWindow> = serde_json::from_value(hol["windows"].clone())?;
    let mut cfg: ExecutionConfig = serde_json::from_value(inp["execution_config"].clone())?;
    cfg.min_entry_risk_ticks = 10;
    let mut contracts: BTreeMap<String, Vec<Kline>> = BTreeMap::new();
    for (c, v) in data.as_object().unwrap() {
        contracts.insert(c.clone(), serde_json::from_value(v["minutes"].clone())?);
    }
    drop(data);
    let out = Path::new(&a[4]);
    ensure!(!out.exists(), "immutable batch exists");
    fs::create_dir_all(out)?;
    let cohorts = read(&a[3])?;
    let mut w = BufWriter::new(fs::File::create(out.join("first-passage.jsonl"))?);
    let mut n = 0;
    for row in cohorts.as_array().unwrap() {
        let baseline = &row["baseline"];
        let e = &baseline["result"]["evaluation"];
        let ev: ReplayEvent = serde_json::from_value(row["event"].clone())?;
        let sign = if ev.direction == "up" { 1. } else { -1. };
        let fill = e["fill"].as_f64().unwrap();
        let risk = e["risk"].as_f64().unwrap();
        for target in [0.5, 1., 2.] {
            let mut ev = ev.clone();
            ev.target = fill + sign * risk * target;
            let rules = ResearchRules {
                min_actual_risk_ticks: Some(10.),
                flat_before_weekend: true,
                ..Default::default()
            };
            let result = ablation::evaluate_research_with_holidays(
                &ev,
                &contracts[baseline["execution_contract"].as_str().unwrap()],
                &[],
                &cfg,
                Experiment {
                    policy: Policy::OriginalS1,
                    no_follow_bar: None,
                    slip_ticks: 1.,
                    fee_ticks: 0.,
                },
                baseline["multiplier"].as_f64().unwrap(),
                None,
                rules,
                &windows,
            );
            ensure!(
                result.evaluation.entry_ts.as_deref() == e["entry_ts"].as_str(),
                "entry time changed"
            );
            ensure!(result.evaluation.fill == Some(fill), "entry fill changed");
            writeln!(
                w,
                "{}",
                json!({"base":row["base"],"event_key":row["event_key"],"target_r":target,"result":result})
            )?;
            n += 1;
        }
    }
    w.flush()?;
    println!("{} frozen entry barrier checks: {n}", a[2]);
    // Reuse full archived candidate pool for non-midnight products only. Every
    // entry is repriced/re-gated on the same repaired executor; no future labels.
    let events: Vec<ReplayEvent> =
        serde_json::from_value(read(paths["signals"].as_str().unwrap())?)?;
    let mut pop = BufWriter::new(fs::File::create(out.join("population.jsonl"))?);
    let mut total = 0;
    for mut ev in events {
        if matches!(ev.symbol.as_str(), "PB0" | "SS0") {
            continue;
        }
        if ev.setup_features.setup_quality < 2.5 || ev.risk <= 0. {
            continue;
        }
        let up = ev.direction == "up";
        let t = ev.tick_size;
        ev.entry = grid(ev.entry, t, up);
        ev.stop = grid(ev.stop, t, !up);
        ev.target = grid(ev.target, t, !up);
        ev.risk = (ev.entry - ev.stop).abs();
        let seg = maps[&ev.symbol]
            .as_array()
            .unwrap()
            .iter()
            .find(|s| {
                s["first"].as_str().unwrap() <= ev.warning_ts.as_str()
                    && ev.warning_ts.as_str() <= s["last"].as_str().unwrap()
            })
            .unwrap();
        let code = seg["underlying"].as_str().unwrap();
        let mult = if ev.symbol == "JD0" {
            10.
        } else {
            n_core::precision::effective_multiplier(
                &ev.symbol,
                inp["symbols"][&ev.symbol]["variety"].as_str().unwrap(),
            )
        };
        let product = ev
            .symbol
            .trim_end_matches(|c: char| c.is_ascii_digit())
            .to_uppercase();
        let g = guard(&fees["products"][&product])?;
        for (base, cap) in [("P2", None), ("P3", Some(2.))] {
            let rules = ResearchRules {
                target_cap_r: cap,
                min_actual_risk_ticks: Some(10.),
                flat_before_weekend: true,
                ..Default::default()
            };
            let result = ablation::evaluate_research_with_holidays(
                &ev,
                &contracts[code],
                &[],
                &cfg,
                Experiment {
                    policy: Policy::OriginalS1,
                    no_follow_bar: Some(5),
                    slip_ticks: 1.,
                    fee_ticks: 0.,
                },
                mult,
                Some(g),
                rules,
                &windows,
            );
            writeln!(
                pop,
                "{}",
                json!({"symbol":ev.symbol,"direction":ev.direction,"event_key":key(&ev),"warning_ts":ev.warning_ts,"scenario":base,"multiplier":mult,"tick_size":t,"slip":1.,"planned_risk":ev.risk,"score":ev.setup_features.setup_quality,"execution_contract":code,"result":result})
            )?;
            total += 1;
        }
    }
    pop.flush()?;
    let ends: BTreeMap<_, _> = contracts
        .iter()
        .map(|(c, v)| (c.clone(), v.last().unwrap().datetime.clone()))
        .collect();
    fs::write(
        out.join("manifest.json"),
        serde_json::to_vec_pretty(
            &json!({"frozen_entry_checks":n,"population_evaluations":total,"contract_ends":ends,"production_changed":false}),
        )?,
    )?;
    println!("{} population checks: {total}", a[2]);
    Ok(())
}
