//! Offline-only: never overwrite input or contact a running broker.
fn main() -> anyhow::Result<()> {
    let args = std::env::args().skip(1).collect::<Vec<_>>();
    anyhow::ensure!(
        args.len() >= 2 && args.len() <= 4,
        "usage: normalize_execution_archive INPUT OUTPUT [CALENDAR_JSON] [CONTRACT_OPENS_JSON]"
    );
    let input = std::fs::canonicalize(&args[0])?;
    let output = std::path::Path::new(&args[1]);
    anyhow::ensure!(
        !output.exists(),
        "output already exists; refusing to overwrite"
    );
    if let Some(calendar) = args.get(2) {
        n_core::session::trading_calendar::load_global_calendar_from_file(std::path::Path::new(
            calendar,
        ))?;
    }
    let opens: Vec<serde_json::Value> = args
        .get(3)
        .map(|p| -> anyhow::Result<_> { Ok(serde_json::from_slice(&std::fs::read(p)?)?) })
        .transpose()?
        .unwrap_or_default();
    let original: serde_json::Value = serde_json::from_slice(&std::fs::read(input)?)?;
    let repaired = n_core::trader::execution::normalize_execution_archive(
        original,
        chrono::Utc::now().timestamp(),
        &opens,
    )?;
    use std::io::Write;
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(output)?;
    file.write_all(&serde_json::to_vec_pretty(&repaired)?)?;
    file.sync_all()?;
    println!("Repaired copy written to {}", output.display());
    Ok(())
}
