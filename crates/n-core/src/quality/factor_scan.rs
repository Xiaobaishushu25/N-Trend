//! Concrete-contract, warning-time factor snapshots. No execution or production writes.
use super::{hash, research::write_json, research::AuditRow, Rules};
use crate::analyze::{
    event::WarningCandidate,
    model::{Bar, DT},
};
use anyhow::{ensure, Context, Result};
use serde::Deserialize;
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, BTreeSet},
    io::{BufRead, Write},
    path::Path,
};

#[derive(Deserialize)]
struct InputBar {
    ts: String,
    open: f64,
    high: f64,
    low: f64,
    close: f64,
    volume: f64,
    hold: f64,
}
#[derive(Deserialize)]
struct Contract {
    symbol: String,
    contract: String,
    bars: Vec<InputBar>,
    eligible_ts: BTreeSet<String>,
    tick: f64,
    multiplier: f64,
    window_start: String,
}
fn identity(c: &WarningCandidate, bars: &[Bar], contract: &str) -> String {
    format!(
        "{}|{:?}|{}|{}|{}|{}",
        contract,
        c.direction,
        bars[c.s0_index].dt,
        bars[c.s1_index].dt,
        bars[c.s2_index].dt,
        bars[c.warning_index].dt
    )
}
fn snapshots(input: &Contract, bars: &[Bar]) -> Result<BTreeMap<String, Value>> {
    let tick = input.tick;
    let multiplier = input.multiplier;
    ensure!(
        tick > 0. && multiplier > 0.,
        "invalid contract specification"
    );
    let mut found = BTreeMap::new();
    let mut seen = BTreeSet::new();
    let rules = Rules::default();
    for c in crate::analyze::event::research_warning_candidates(&input.symbol, bars, tick) {
        let ts = bars[c.warning_index].dt.to_bar_ts();
        let key = identity(&c, bars, &input.contract);
        // Same endpoints across fine/large levels retain stable detector order.
        let shape = format!(
            "{:?}|{}|{}|{}",
            c.direction, c.s0_index, c.s1_index, c.s2_index
        );
        if !seen.insert(shape) {
            continue;
        }
        if !input.eligible_ts.contains(&ts) {
            continue;
        }
        let e = super::score_scan::model(&input.symbol, bars, &c, 0)?;
        let quality = super::evaluate_with_research_calendar(
            &e,
            bars,
            tick,
            &ts,
            "factor-study",
            &rules,
            if matches!(input.symbol.as_str(), "SF0" | "SM0") {
                Some(crate::session::spec::NightSessionType::None)
            } else {
                None
            },
        );
        let mut evidence = Value::Null;
        if quality.status == "ok" {
            let payload = super::blind::case(&e, bars, &quality, &rules);
            let w = payload["bars"].as_array().context("blind bars")?.len() - 1;
            let audit = AuditRow {
                event_id: 0,
                symbol: e.symbol.clone(),
                direction: e.direction.clone(),
                grade: e.grade.clone(),
                level: e.level.clone(),
                warning_ts: e.warning_ts.clone(),
                s0_ts: e.s0_ts.clone(),
                s1_ts: e.s1_ts.clone(),
                old_score: e.entry_score,
                old_dims: serde_json::from_str(&e.entry_score_dims)?,
                quality: quality.clone(),
                legacy_recomputed: None,
                legacy_max_error: None,
                evaluations: BTreeMap::new(),
            };
            evidence = super::structure::diagnose(
                &payload,
                w,
                Some(&audit),
                Some(tick / quality.features["atr_s1"].context("ATR")?),
            )?;
        }
        let group = format!("{}|{}|{}|{}", input.contract, e.direction, e.s0_ts, e.s1_ts);
        let mut features = serde_json::to_value(&quality.features)?;
        if let Some(f) = features.as_object_mut() {
            f.insert("a_bars".into(), json!(e.a_bars));
            f.insert("b_bars".into(), json!(e.b_bars));
            f.insert(
                "b_a_time_ratio".into(),
                json!(e.b_bars as f64 / e.a_bars.max(1) as f64),
            );
            f.insert(
                "b_move_atr".into(),
                json!(quality
                    .features
                    .get("atr_s1")
                    .copied()
                    .flatten()
                    .map(|a| e.b_move / a)),
            );
        }
        found.insert(
            key.clone(),
            json!({"schema":"FactorSnapshotV1","candidate_id":hash(key.as_bytes()),
            "candidate_key":key,"group_id":group,"symbol":input.symbol,"contract":input.contract,
            "warning_ts":ts,"available_at":ts,"tick":tick,"multiplier":multiplier,
            "in_window":ts.as_str() >= input.window_start.as_str(),
            "event":e,"features":features,"quality":quality,"evidence":evidence}),
        );
    }
    Ok(found)
}

pub fn run(args: &[String], out: &Path) -> Result<()> {
    let file = args
        .windows(2)
        .find(|p| p[0] == "--input")
        .context("--input prepared scan-input.jsonl required")?;
    let source = std::path::PathBuf::from(&file[1]);
    let calendar = source
        .parent()
        .context("input directory")?
        .join("calendar.json");
    crate::session::trading_calendar::load_global_calendar_from_file(&calendar)?;
    let mut writer = std::io::BufWriter::new(
        std::fs::OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(out.join("snapshots.jsonl"))?,
    );
    let mut audits = Vec::new();
    for line in std::io::BufReader::new(std::fs::File::open(&source)?).lines() {
        let path = source.parent().unwrap_or(Path::new(".")).join(line?);
        let bytes = std::fs::read(&path)?;
        let input: Contract = serde_json::from_slice(&bytes)?;
        let bars: Vec<Bar> = input
            .bars
            .iter()
            .map(|b| -> Result<Bar> {
                ensure!(
                    [b.open, b.high, b.low, b.close, b.volume, b.hold]
                        .iter()
                        .all(|x| x.is_finite())
                        && b.low <= b.open.min(b.close)
                        && b.high >= b.open.max(b.close)
                        && b.low > 0.,
                    "invalid OHLC"
                );
                Ok(Bar {
                    dt: DT::from_bar_ts(&b.ts).context("bar timestamp")?,
                    open: b.open,
                    high: b.high,
                    low: b.low,
                    close: b.close,
                    volume: b.volume,
                    hold: b.hold,
                    rollover: false,
                })
            })
            .collect::<Result<_>>()?;
        ensure!(bars.windows(2).all(|b| b[0].dt < b[1].dt), "unordered bars");
        let all = snapshots(&input, &bars)?;
        for pct in [25, 50, 75] {
            let cut = bars.len() * pct / 100;
            if cut == 0 {
                continue;
            }
            let prefix = snapshots(&input, &bars[..cut])?;
            let boundary = bars[cut - 1].dt.to_bar_ts();
            let expected: BTreeMap<_, _> = all
                .iter()
                .filter(|(_, v)| v["warning_ts"].as_str().unwrap() <= boundary.as_str())
                .map(|(k, v)| (k.clone(), v.clone()))
                .collect();
            ensure!(
                prefix == expected,
                "future changed snapshot: {} at {}%",
                input.contract,
                pct
            );
        }
        for row in all.values() {
            serde_json::to_writer(&mut writer, row)?;
            writer.write_all(b"\n")?;
        }
        writer.flush()?;
        println!(
            "{}: {} bars, {} candidates, prefixes verified",
            input.contract,
            bars.len(),
            all.len()
        );
        audits.push(
            json!({"contract":input.contract,"bars":bars.len(),"candidates":all.len(),
            "input_sha256":hash(&bytes),"prefix_mismatches":0}),
        );
    }
    write_json(
        &out.join("manifest.json"),
        &json!({"schema":"factor-scan-v1","development_only":true,
        "contracts":audits,"input_sha256":hash(&std::fs::read(&source)?),
        "code_sha256":hash(&[include_bytes!("factor_scan.rs").as_slice(),include_bytes!("mod.rs").as_slice(),
            include_bytes!("../analyze/event.rs").as_slice(),include_bytes!("structure.rs").as_slice()].concat()),
        "baseline_rules":Rules::default()}),
    )
}
