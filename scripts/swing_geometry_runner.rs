//! Broad, frozen mechanism comparisons. All reads from archived local files.
use anyhow::{ensure, Result};
use n_core::{
    derive::rollover::RolloverRecord,
    fetch::kline::Kline,
    trader::{candidates, ExecutionConfig},
    v2::replay::{ReplayEngine, ReplayEvent},
    v3::{
        ablation::{self, EntryGuard, Experiment, HolidayWindow, Policy, ResearchRules},
        geometry_research as geom,
    },
};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    io::{BufWriter, Write},
    path::Path,
};
fn read(p: impl AsRef<Path>) -> Result<Value> {
    Ok(serde_json::from_slice(&fs::read(p)?)?)
}
fn key(e: &ReplayEvent) -> String {
    format!(
        "{}|{}|{}|{}|{}|{}",
        e.symbol, e.direction, e.s0_ts, e.s1_ts, e.s2_ts, e.warning_ts
    )
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
fn friction(e: &ReplayEvent, fee: &Value, mult: f64) -> f64 {
    let sign = if e.direction == "up" { 1. } else { -1. };
    let entry = e.entry + sign * e.tick_size;
    let stop = e.stop - sign * e.tick_size;
    let commission = if let Some(f) = fee.get("roundtrip_fixed_yuan_per_lot") {
        f.as_f64().unwrap()
    } else {
        let charge = |kind: &str, price: f64| {
            fee[kind]["fixed_yuan_per_lot"].as_f64().unwrap()
                + fee[kind]["notional_rate"].as_f64().unwrap() * price * mult
        };
        charge("open", entry) + charge("close_today", stop).max(charge("close_yesterday", stop))
    };
    (2. * e.tick_size * mult + commission) / (e.risk * mult)
}
fn main() -> Result<()> {
    let args: Vec<_> = std::env::args().collect();
    ensure!(args.len() == 5, "protocol period pool/replay new-output");
    let p = read(&args[1])?;
    let period = &args[2];
    let paths = &p["periods"][period];
    let inp = read(paths["input"].as_str().unwrap())?;
    let fees = read(paths["fees"].as_str().unwrap())?;
    ensure!(!Path::new(&args[4]).exists(), "immutable output exists");
    if args[3] == "pool" {
        let old: Vec<ReplayEvent> =
            serde_json::from_value(read(paths["signals"].as_str().unwrap())?)?;
        let mask = read(
            Path::new(p["source"].as_str().unwrap())
                .join(period)
                .join("service-dedup-raw-plan.json"),
        )?;
        let allowed: BTreeSet<String> = mask["symbols"]
            .as_object()
            .unwrap()
            .values()
            .flat_map(|v| {
                v["keys"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|k| k.as_str().unwrap().to_string())
            })
            .collect();
        let engine = ReplayEngine::default_engine();
        let mut pool = vec![];
        for (symbol, v) in inp["symbols"].as_object().unwrap() {
            let raw: Vec<Kline> = serde_json::from_value(v["bars"].clone())?;
            let rolls: Vec<RolloverRecord> = v["rollovers"]
                .as_array()
                .unwrap()
                .iter()
                .map(|r| RolloverRecord {
                    symbol: symbol.clone(),
                    ts: r["ts"].as_str().unwrap().into(),
                    from_contract: r["from_contract"].as_str().unwrap().into(),
                    to_contract: r["to_contract"].as_str().unwrap().into(),
                    confirmed: r["confirmed"].as_bool().unwrap(),
                })
                .collect();
            let tick = v["tick_size"].as_f64().unwrap();
            let events = if raw.iter().any(|b| b.datetime.ends_with("00:00:00")) {
                println!("{period} regenerate {symbol}");
                engine.replay_history_with_rollovers(symbol, &raw, tick, &rolls)?
            } else {
                old.iter()
                    .filter(|e| e.symbol == *symbol)
                    .cloned()
                    .collect()
            };
            let completed = candidates::completed_bars(
                &raw,
                candidates::parse_time(&raw.last().unwrap().datetime)?,
            )?;
            let mut atrs = BTreeMap::new();
            let mut tr = vec![];
            let mut last: Option<f64> = None;
            for b in &completed {
                tr.push(last.map_or(b.high - b.low, |c| {
                    (b.high - b.low)
                        .max((b.high - c).abs())
                        .max((b.low - c).abs())
                }));
                last = Some(b.close);
                if tr.len() >= 14 {
                    atrs.insert(
                        b.datetime.clone(),
                        tr[tr.len() - 14..].iter().sum::<f64>() / 14.,
                    );
                }
            }
            let mult = if symbol == "JD0" {
                10.
            } else {
                n_core::precision::effective_multiplier(symbol, v["variety"].as_str().unwrap())
            };
            let product = symbol
                .trim_end_matches(|c: char| c.is_ascii_digit())
                .to_uppercase();
            let fee = &fees["products"][&product];
            for mut ev in events {
                if (period == "early"
                    && !("2026-01-01 00:00:00"..="2026-06-30 15:00:00")
                        .contains(&ev.warning_ts.as_str()))
                    || (period == "recent"
                        && !("2026-07-01 00:00:00"..="2026-10-23 11:05:00")
                            .contains(&ev.warning_ts.as_str()))
                {
                    continue;
                }
                if ev.setup_features.setup_quality < 2.5 {
                    continue;
                }
                let k = key(&ev);
                if !allowed.contains(&k) {
                    continue;
                }
                geom::normalize(&mut ev);
                let Some(atr) = atrs.get(&ev.warning_ts) else {
                    continue;
                };
                let end = completed.partition_point(|b| b.datetime < ev.warning_ts);
                let Some(warning) = completed.get(end).filter(|b| b.datetime == ev.warning_ts)
                else {
                    continue;
                };
                let prior = &completed[end.saturating_sub(12)..end];
                let f = friction(&ev, fee, mult);
                let risk_atr = ev.risk / atr;
                pool.push(json!({"event_key":k,"event":ev,"atr14":atr,"original_friction":f,"original_risk_atr":risk_atr,"warning_candle":warning,"prior":prior}));
            }
            println!("{period} {symbol} pool {}", pool.len());
        }
        fs::write(&args[4], serde_json::to_vec_pretty(&pool)?)?;
        return Ok(());
    }
    ensure!(args[3] == "replay", "unknown stage");
    let out = Path::new(&args[4]);
    fs::create_dir_all(out)?;
    let pool = read(
        Path::new(&args[1])
            .parent()
            .unwrap()
            .join(format!("{period}-pool.json")),
    )?;
    let maps = read(paths["segments"].as_str().unwrap())?;
    let data = read(paths["contracts"].as_str().unwrap())?;
    let hol = read(paths["holidays"].as_str().unwrap())?;
    let windows: Vec<HolidayWindow> = serde_json::from_value(hol["windows"].clone())?;
    let mut contracts: BTreeMap<String, Vec<Kline>> = BTreeMap::new();
    for (c, v) in data.as_object().unwrap() {
        contracts.insert(c.clone(), serde_json::from_value(v["minutes"].clone())?);
    }
    drop(data);
    let mut cfg: ExecutionConfig = serde_json::from_value(inp["execution_config"].clone())?;
    cfg.min_entry_risk_ticks = 10;
    let mut writer = BufWriter::new(fs::File::create(out.join("evaluations.jsonl"))?);
    let mut n = 0;
    for snap in pool.as_array().unwrap() {
        let original: ReplayEvent = serde_json::from_value(snap["event"].clone())?;
        let warning: Kline = serde_json::from_value(snap["warning_candle"].clone())?;
        let prior: Vec<Kline> = serde_json::from_value(snap["prior"].clone())?;
        let seg = maps[&original.symbol]
            .as_array()
            .unwrap()
            .iter()
            .find(|s| {
                s["first"].as_str().unwrap() <= original.warning_ts.as_str()
                    && original.warning_ts.as_str() <= s["last"].as_str().unwrap()
            })
            .unwrap();
        let code = seg["underlying"].as_str().unwrap();
        let all = &contracts[code];
        let start = all.partition_point(|b| b.datetime <= original.warning_ts);
        let raw = &all[start..];
        let mult = if original.symbol == "JD0" {
            10.
        } else {
            n_core::precision::effective_multiplier(
                &original.symbol,
                inp["symbols"][&original.symbol]["variety"]
                    .as_str()
                    .unwrap(),
            )
        };
        let product = original
            .symbol
            .trim_end_matches(|c: char| c.is_ascii_digit())
            .to_uppercase();
        let fee = &fees["products"][&product];
        let g = guard(fee)?;
        for template in p["templates"].as_array().unwrap() {
            let mut ev = original.clone();
            let wide = template["wide"].as_bool().unwrap();
            let eligible = wide
                || (original.setup_features.setup_quality >= 3.
                    && snap["original_friction"].as_f64().unwrap() <= 0.2 + 1e-9
                    && (0.75..=2.5).contains(&snap["original_risk_atr"].as_f64().unwrap()));
            if template["body"].as_bool().unwrap() {
                geom::body_stop(&mut ev, &warning);
            }
            let local_source = if template["local"].as_bool().unwrap() {
                geom::local_target(&mut ev, &prior)
            } else {
                None
            };
            let new_friction = friction(&ev, fee, mult);
            let geometry_ok = ev.risk / ev.tick_size + 1e-6 >= 10. && new_friction <= 0.2 + 1e-9;
            for mode in p["modes"].as_array().unwrap() {
                let mode = mode.as_str().unwrap();
                let mut rules: ResearchRules = serde_json::from_value(template["rules"].clone())?;
                rules.next_minute_open = mode == "next_minute_open";
                for slip in [1., 2.] {
                    let mut result = if eligible && geometry_ok {
                        ablation::evaluate_research_with_holidays(
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
                        )
                    } else {
                        let mut e = n_core::v3::replay::Evaluation::default();
                        e.status = if !eligible {
                            "filtered_setup"
                        } else {
                            "filtered_geometry"
                        }
                        .into();
                        ablation::ResultRow {
                            evaluation: e,
                            disagreement: None,
                            fills: vec![],
                        }
                    };
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
                        json!({"symbol":ev.symbol,"direction":ev.direction,"event_key":snap["event_key"],"warning_ts":ev.warning_ts,"scenario":format!("{name}/{mode}/slip{slip}"),
                        "multiplier":mult,"tick_size":ev.tick_size,"slip":slip,"planned_risk":ev.risk,"original_risk":original.risk,"score":ev.setup_features.setup_quality,
                        "execution_contract":code,"new_friction":new_friction,"original_target":original.target,"target":ev.target,"stop":ev.stop,"entry":ev.entry,
                        "local_level_time":local_source,"zero_volume_execution_unknown":zero,"result":result})
                    )?;
                    n += 1;
                }
            }
        }
        if n % 12800 == 0 {
            println!("{period} {n} evaluations");
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
    println!("{period} complete {n}");
    Ok(())
}
