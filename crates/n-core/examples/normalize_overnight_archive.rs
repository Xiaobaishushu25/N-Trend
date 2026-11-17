//! Generate append-only corrections; no broker connection and no input overwrites.
use std::{collections::BTreeMap, io::Write};
fn main() -> anyhow::Result<()> {
    let args = std::env::args().skip(1).collect::<Vec<_>>();
    anyhow::ensure!(args.len() == 5, "usage: normalize_overnight_archive INPUT_JSONL OUTPUT_JSONL CANONICAL_STATE CALENDAR_JSON CONTRACT_OPENS_JSON");
    anyhow::ensure!(
        !std::path::Path::new(&args[1]).exists(),
        "output already exists"
    );
    n_core::session::trading_calendar::load_global_calendar_from_file(std::path::Path::new(
        &args[3],
    ))?;
    let state: serde_json::Value = serde_json::from_slice(&std::fs::read(&args[2])?)?;
    let opens: Vec<serde_json::Value> = serde_json::from_slice(&std::fs::read(&args[4])?)?;
    let mut latest = BTreeMap::new();
    for line in std::fs::read_to_string(&args[0])?
        .lines()
        .filter(|line| !line.trim().is_empty())
    {
        let snapshot: n_core::trader::OvernightSnapshot = serde_json::from_str(line)?;
        latest.insert(snapshot.id.clone(), snapshot);
    }
    let mut output = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&args[1])?;
    for snapshot in latest.into_values() {
        let corrected = snapshot.correct_archive(&state, chrono::Utc::now().timestamp(), &opens);
        writeln!(output, "{}", serde_json::to_string(&corrected)?)?;
    }
    output.sync_all()?;
    println!(
        "Corrections written to {} (append to the original journal after backup)",
        args[1]
    );
    Ok(())
}
