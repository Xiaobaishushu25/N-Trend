//! Isolated research runner. Compiled only against the patched research fork.
use anyhow::{ensure, Context, Result};
use n_core::{
    derive::rollover::RolloverRecord,
    fetch::kline::Kline,
    trader::{candidates, ExecutionConfig},
    v2::replay::{ReplayEngine, ReplayEvent},
    v3::ablation::{self, EntryGuard, Experiment, HolidayWindow, Policy, ResearchRules},
};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    io::{BufWriter, Write},
    path::Path,
};
fn read(p: &str) -> Result<Value> {
    Ok(serde_json::from_slice(&fs::read(p)?)?)
}
fn key(e: &ReplayEvent) -> String {
    format!(
        "{}|{}|{}|{}|{}|{}",
        e.symbol, e.direction, e.s0_ts, e.s1_ts, e.s2_ts, e.warning_ts
    )
}
fn round(x: f64, t: f64, up: bool) -> f64 {
    if up {
        (x / t - 1e-9).ceil() * t
    } else {
        (x / t + 1e-9).floor() * t
    }
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
    ensure!(a.len() == 4, "protocol period new-output");
    let p = read(&a[1])?;
    let period = &a[2];
    let paths = &p["periods"][period];
    let inp = read(paths["input"].as_str().context("input")?)?;
    let old: Vec<ReplayEvent> = serde_json::from_value(read(paths["signals"].as_str().unwrap())?)?;
    let data = read(paths["contracts"].as_str().unwrap())?;
    let maps = read(paths["segments"].as_str().unwrap())?;
    let fees = read(paths["fees"].as_str().unwrap())?;
    let hol = read(paths["holidays"].as_str().unwrap())?;
    let windows: Vec<HolidayWindow> = serde_json::from_value(hol["windows"].clone())?;
    let out = Path::new(&a[3]);
    ensure!(!out.exists(), "immutable batch exists");
    fs::create_dir_all(out)?;
    let mut contracts: BTreeMap<String, Vec<Kline>> = BTreeMap::new();
    for (c, v) in data.as_object().unwrap() {
        contracts.insert(c.clone(), serde_json::from_value(v["minutes"].clone())?);
    }
    drop(data);
    let mut ew = BufWriter::new(fs::File::create(out.join("events.jsonl"))?);
    let mut rw = BufWriter::new(fs::File::create(out.join("evaluations.jsonl"))?);
    let mut audit = BTreeMap::new();
    let engine = ReplayEngine::default_engine();
    let mut count = 0;
    for (symbol, v) in inp["symbols"].as_object().unwrap() {
        let bars: Vec<Kline> = serde_json::from_value(v["bars"].clone())?;
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
        // Only products with a 00:00 source timestamp need signal regeneration.
        // Others are mathematically unchanged by the sole aggregation repair.
        let midnight = bars.iter().any(|b| b.datetime.ends_with("00:00:00"));
        let regenerated = if midnight {
            println!("{period}: regenerate {symbol}");
            engine.replay_history_with_rollovers(symbol, &bars, tick, &rolls)?
        } else {
            old.iter()
                .filter(|e| e.symbol == *symbol)
                .cloned()
                .collect()
        };
        let completed = candidates::completed_bars(
            &bars,
            candidates::parse_time(&bars.last().context("bars")?.datetime)?,
        )?;
        // Same continuous SMA14 and complete triples as the candidate service;
        // compare against its public warning_context for every accepted event.
        let mut contexts = BTreeMap::new();
        let mut trs = vec![];
        let mut last: Option<f64> = None;
        for b in &completed {
            let tr = last.map_or(b.high - b.low, |c| {
                (b.high - b.low)
                    .max((b.high - c).abs())
                    .max((b.low - c).abs())
            });
            trs.push(tr);
            last = Some(b.close);
            if trs.len() >= 14 {
                contexts.insert(
                    b.datetime.clone(),
                    trs[trs.len() - 14..].iter().sum::<f64>() / 14.,
                );
            }
        }
        let mut seen = BTreeSet::new();
        let mut na = 0;
        let mut filtered = BTreeMap::<&str, usize>::new();
        let mut parity = 0;
        for mut ev in regenerated {
            if (period == "early"
                && !("2026-01-01 00:00:00"..="2026-06-30 15:00:00")
                    .contains(&ev.warning_ts.as_str()))
                || (period == "recent"
                    && !("2026-07-01 00:00:00"..="2026-10-23 11:05:00")
                        .contains(&ev.warning_ts.as_str()))
            {
                continue;
            }
            let up = ev.direction == "up";
            ev.entry = round(ev.entry, tick, up);
            ev.stop = round(ev.stop, tick, !up);
            ev.target = round(ev.target, tick, !up);
            ev.risk = (ev.entry - ev.stop).abs();
            ev.rr = (ev.target - ev.entry).abs() / ev.risk;
            // Purge every future outcome/trigger snapshot before research use.
            ev.evaluation = None;
            ev.outcome = None;
            ev.trigger_features = None;
            ev.trigger_bar_ts = None;
            ev.entry_ts = None;
            ev.entry_price = None;
            let k = key(&ev);
            if !seen.insert(k.clone()) {
                continue;
            }
            na += 1;
            if ev.risk <= 0. || ev.setup_features.setup_quality < 3. || ev.risk / tick + 1e-6 < 10.
            {
                *filtered.entry("score_or_risk").or_default() += 1;
                continue;
            }
            let Some(atr) = contexts.get(&ev.warning_ts) else {
                *filtered.entry("incomplete_atr").or_default() += 1;
                continue;
            };
            if !(0.75..=2.5).contains(&(ev.risk / atr)) {
                *filtered.entry("atr_range").or_default() += 1;
                continue;
            }
            let parity_context = candidates::warning_context(&bars, &ev.warning_ts)?.unwrap();
            ensure!(
                parity_context.complete && (parity_context.atr14.unwrap() - atr).abs() < 1e-9,
                "live ATR parity"
            );
            parity += 1;
            let sign = if up { 1. } else { -1. };
            let mult = if symbol == "JD0" {
                10.
            } else {
                n_core::precision::effective_multiplier(symbol, v["variety"].as_str().unwrap())
            };
            let product = symbol
                .trim_end_matches(|c: char| c.is_ascii_digit())
                .to_uppercase();
            let g = guard(&fees["products"][&product])?;
            let spec = &fees["products"][&product];
            let forecast = ev.entry + sign * tick;
            let stop = ev.stop - sign * tick;
            let fee = if let Some(x) = spec.get("roundtrip_fixed_yuan_per_lot") {
                x.as_f64().unwrap()
            } else {
                let charge = |kind: &str, price: f64| {
                    spec[kind]["fixed_yuan_per_lot"].as_f64().unwrap()
                        + spec[kind]["notional_rate"].as_f64().unwrap() * price * mult
                };
                charge("open", forecast)
                    + charge("close_today", stop).max(charge("close_yesterday", stop))
            };
            if (2. * tick * mult + fee) / (ev.risk * mult) > 0.2 + 1e-9 {
                *filtered.entry("friction").or_default() += 1;
                continue;
            }
            let seg = maps[symbol]
                .as_array()
                .unwrap()
                .iter()
                .find(|r| {
                    r["first"].as_str().unwrap() <= ev.warning_ts.as_str()
                        && ev.warning_ts.as_str() <= r["last"].as_str().unwrap()
                })
                .context("unmapped warning")?;
            let code = seg["underlying"].as_str().unwrap();
            let raw = &contracts[code];
            let atr_scale = ev.risk / atr;
            let friction = (2. * tick * mult + fee) / (ev.risk * mult);
            let a_bars: Vec<_> = completed
                .iter()
                .filter(|b| b.datetime >= ev.s0_ts && b.datetime <= ev.s1_ts)
                .collect();
            let denom = a_bars.iter().map(|b| (b.close - b.open).abs()).sum::<f64>();
            let eff = if denom > 0. {
                sign * a_bars.iter().map(|b| b.close - b.open).sum::<f64>() / denom
            } else {
                0.
            };
            writeln!(
                ew,
                "{}",
                json!({"event_key":k,"event":ev,"atr14":atr,"risk_atr":atr_scale,"friction_r":friction,"a_eff":eff,"group":if friction>0.15{"high_friction"}else if eff<0.4{"low_efficiency"}else{"efficient"}})
            )?;
            for t in p["templates"].as_array().unwrap() {
                let name = t["name"].as_str().unwrap();
                let policy: Policy = serde_json::from_value(t["policy"].clone())?;
                let mut cfg: ExecutionConfig =
                    serde_json::from_value(inp["execution_config"].clone())?;
                cfg.min_entry_risk_ticks = 10;
                for mode in p["modes"].as_array().unwrap() {
                    let mode = mode.as_str().unwrap();
                    let mut rules: ResearchRules = serde_json::from_value(t["rules"].clone())?;
                    rules.next_minute_open = mode == "next_minute_open";
                    for slip in [1., 2.] {
                        let result = ablation::evaluate_research_with_holidays(
                            &ev,
                            raw,
                            &[],
                            &cfg,
                            Experiment {
                                policy,
                                no_follow_bar: Some(5),
                                slip_ticks: slip,
                                fee_ticks: 0.,
                            },
                            mult,
                            Some(g),
                            rules,
                            &windows,
                        );
                        writeln!(
                            rw,
                            "{}",
                            json!({"symbol":symbol,"direction":ev.direction,"event_key":k,"warning_ts":ev.warning_ts,"scenario":format!("{name}/{mode}/slip{slip}"),"multiplier":mult,"tick_size":tick,"planned_risk":ev.risk,"score":ev.setup_features.setup_quality,"execution_contract":code,"slip":slip,"result":result})
                        )?;
                        count += 1;
                    }
                }
            }
        }
        audit.insert(symbol.clone(),json!({"regenerated":midnight,"warning_count":na,"filtered":filtered,"atr_public_service_parity":parity}));
        println!("{period}: {symbol} done; {count} evaluations");
    }
    ew.flush()?;
    rw.flush()?;
    let ends: BTreeMap<_, _> = contracts
        .iter()
        .map(|(c, v)| (c.clone(), v.last().unwrap().datetime.clone()))
        .collect();
    fs::write(
        out.join("manifest.json"),
        serde_json::to_vec_pretty(
            &json!({"contract_ends":ends,"audit":audit,"evaluations":count,"production_changed":false,"quote_parity":false}),
        )?,
    )?;
    Ok(())
}
