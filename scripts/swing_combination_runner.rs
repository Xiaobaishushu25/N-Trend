//! Frozen A/B/C combination validation. No production services or persistence.
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
fn main() -> Result<()> {
    let a: Vec<_> = std::env::args().collect();
    ensure!(a.len() == 5, "protocol period pool new-out");
    let p = read(&a[1])?;
    let paths = &p["periods"][&a[2]];
    let inp = read(paths["input"].as_str().unwrap())?;
    let data = read(paths["contracts"].as_str().unwrap())?;
    let maps = read(paths["segments"].as_str().unwrap())?;
    let fees = read(paths["fees"].as_str().unwrap())?;
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
    ensure!(!out.exists(), "immutable output");
    fs::create_dir_all(out)?;
    let mut writer = BufWriter::new(fs::File::create(out.join("evaluations.jsonl"))?);
    let pool = read(&a[3])?;
    let mut n = 0;
    for snap in pool.as_array().unwrap() {
        let ev: ReplayEvent = serde_json::from_value(snap["event"].clone())?;
        let key = snap["event_key"].as_str().unwrap();
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
        let all = &contracts[code];
        let start = all.partition_point(|b| b.datetime <= ev.warning_ts);
        let raw = &all[start..];
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
        for template in p["templates"].as_array().unwrap() {
            for mode in p["modes"].as_array().unwrap() {
                let mode = mode.as_str().unwrap();
                let mut rules: ResearchRules = serde_json::from_value(template["rules"].clone())?;
                rules.next_minute_open = mode == "next_minute_open";
                for slip in [1., 2.] {
                    let mut result = ablation::evaluate_research_with_holidays(
                        &ev,
                        raw,
                        &[],
                        &cfg,
                        Experiment {
                            policy: Policy::OriginalS1,
                            no_follow_bar: Some(5),
                            slip_ticks: slip,
                            fee_ticks: 0.,
                        },
                        mult,
                        Some(g),
                        rules,
                        &windows,
                    );
                    let zero = result.fills.iter().any(|f| {
                        all.binary_search_by(|b| b.datetime.cmp(&f.ts))
                            .ok()
                            .is_none_or(|i| all[i].volume <= 0.)
                    });
                    if zero {
                        result.evaluation.status = "missing_data".into();
                        result.evaluation.net_amount = None;
                        result.evaluation.net_r = None;
                    }
                    let name = template["name"].as_str().unwrap();
                    writeln!(
                        writer,
                        "{}",
                        json!({"symbol":ev.symbol,"direction":ev.direction,"event_key":key,"warning_ts":ev.warning_ts,
                        "scenario":format!("{name}/{mode}/slip{slip}"),"multiplier":mult,"tick_size":ev.tick_size,"slip":slip,"planned_risk":ev.risk,
                        "score":ev.setup_features.setup_quality,"execution_contract":code,"result":result,"zero_volume_execution_unknown":zero})
                    )?;
                    n += 1;
                }
            }
        }
        if n % 3200 == 0 {
            println!("{} {n} evaluations", a[2]);
        }
    }
    writer.flush()?;
    let ends: BTreeMap<_, _> = contracts
        .iter()
        .map(|(c, v)| (c.clone(), v.last().unwrap().datetime.clone()))
        .collect();
    fs::write(
        out.join("manifest.json"),
        serde_json::to_vec_pretty(
            &json!({"evaluations":n,"contract_ends":ends,"pool_size":pool.as_array().unwrap().len(),"production_changed":false}),
        )?,
    )?;
    println!("{} complete: {n}", a[2]);
    Ok(())
}
