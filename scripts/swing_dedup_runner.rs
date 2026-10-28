use anyhow::{ensure, Result};
use n_core::{
    derive::rollover::RolloverRecord,
    fetch::kline::Kline,
    v2::replay::{ReplayEngine, ReplayEvent},
};
use serde_json::{json, Value};
use std::{collections::BTreeMap, fs};
fn read(p: &str) -> Result<Value> {
    Ok(serde_json::from_slice(&fs::read(p)?)?)
}
fn main() -> Result<()> {
    let a: Vec<_> = std::env::args().collect();
    ensure!(a.len() == 4, "protocol period new mask");
    ensure!(!std::path::Path::new(&a[3]).exists(), "immutable mask");
    let p = read(&a[1])?;
    let paths = &p["periods"][&a[2]];
    let inp = read(paths["input"].as_str().unwrap())?;
    let signal_path = if a[2] == "early" {
        std::path::Path::new(paths["signals"].as_str().unwrap()).with_file_name("signals-raw.json")
    } else {
        paths["signals"].as_str().unwrap().into()
    };
    let old: Vec<ReplayEvent> = serde_json::from_value(read(signal_path.to_str().unwrap())?)?;
    let engine = ReplayEngine::default_engine();
    let mut masks = BTreeMap::new();
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
        let events = if raw.iter().any(|b| b.datetime.ends_with("00:00:00")) {
            engine.replay_history_with_rollovers(
                symbol,
                &raw,
                v["tick_size"].as_f64().unwrap(),
                &rolls,
            )?
        } else {
            old.iter()
                .filter(|e| e.symbol == *symbol)
                .cloned()
                .collect()
        };
        let (bars, _) = engine.aggregate_with_rollovers(&raw, &rolls);
        let kept = n_core::service::offline_forward_warning_keys(&bars, &events)?;
        println!(
            "{}: {} raw {} service-forward kept",
            symbol,
            events.len(),
            kept.len()
        );
        masks.insert(symbol.clone(), json!({"raw":events.len(),"keys":kept}));
    }
    fs::write(
        &a[3],
        serde_json::to_vec_pretty(
            &json!({"mode":"service_forward_closed_bars_no_quote_updates","symbols":masks,"future_outcomes_consulted":false}),
        )?,
    )?;
    Ok(())
}
