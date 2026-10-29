//! Offline audit/backfill. Input databases are read-only and never migrated.
use super::*;
use crate::{fetch::kline::Kline, storage::repo, v2::replay::ReplayEvent};
use anyhow::{ensure, Context, Result};
use sea_orm::{ConnectionTrait, Database, DbBackend, Statement};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, HashMap},
    io::{Read, Write},
    path::{Path, PathBuf},
};

fn arg(args: &[String], key: &str) -> Option<String> {
    args.windows(2).find(|p| p[0] == key).map(|p| p[1].clone())
}
pub fn write_json(path: &Path, value: &impl Serialize) -> Result<()> {
    let mut f = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)?;
    f.write_all(&serde_json::to_vec_pretty(value)?)?;
    Ok(())
}
pub fn bars_from_rows(
    rows: &[crate::storage::entities::klines::Model],
    rolls: &[crate::storage::entities::rollovers::Model],
) -> Vec<Bar> {
    let mut bars: Vec<_> = rows
        .iter()
        .filter_map(|b| {
            Some(Bar {
                dt: crate::analyze::model::DT::from_bar_ts(&b.ts)?,
                open: b.open,
                high: b.high,
                low: b.low,
                close: b.close,
                volume: b.volume,
                hold: b.hold,
                rollover: false,
            })
        })
        .collect();
    for r in rolls.iter().filter(|r| r.confirmed) {
        if let Some(b) = bars.iter_mut().find(|b| b.dt.to_bar_ts() >= r.ts) {
            b.rollover = true;
        }
    }
    bars
}
#[derive(Clone, Serialize, Deserialize)]
pub struct AuditRow {
    pub event_id: i64,
    pub symbol: String,
    pub direction: String,
    pub grade: String,
    pub level: String,
    pub warning_ts: String,
    pub s0_ts: String,
    pub s1_ts: String,
    pub old_score: f64,
    pub old_dims: Value,
    pub quality: SignalQualityDto,
    pub legacy_recomputed: Option<[f64; 4]>,
    pub legacy_max_error: Option<f64>,
    pub evaluations: BTreeMap<String, Value>,
}

pub async fn run(args: Vec<String>) -> Result<()> {
    let command = args.get(1).map(String::as_str).unwrap_or("help");
    if command == "help" || args.iter().any(|a| a == "--help") {
        println!("signal-quality <audit|report|backfill|blind-export> (--backup snapshot.sqlite.gz | --db snapshot.sqlite) --out NEW_DIRECTORY [--rules rules.json] [--roundtrip-cost-ticks 2.5]\nsignal-quality blind-import --manifest blind-manifest.json --labels labels.json --out NEW_DIRECTORY\nsignal-quality annotate --manifest blind-manifest.json --audit audit.json --out NEW_DIRECTORY [--review-notes reviewed-notes.json]\nbackfill always creates and writes its own isolated output copy; original input remains read-only.");
        return Ok(());
    }
    ensure!(
        [
            "audit",
            "report",
            "backfill",
            "blind-export",
            "blind-import",
            "annotate"
        ]
        .contains(&command),
        "unknown command"
    );
    let out = PathBuf::from(arg(&args, "--out").context("--out NEW_DIRECTORY required")?);
    ensure!(
        !out.exists(),
        "output must be a new immutable run directory"
    );
    // All artifacts must stay in this project's research workspace.
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()?;
    let absolute = if out.is_absolute() {
        out.clone()
    } else {
        std::env::current_dir()?.join(&out)
    };
    ensure!(
        !absolute
            .components()
            .any(|c| c == std::path::Component::ParentDir),
        "output path cannot contain '..'"
    );
    let mut existing = absolute.as_path();
    while !existing.exists() {
        existing = existing.parent().context("invalid output parent")?;
    }
    ensure!(
        existing.canonicalize()?.starts_with(&root),
        "output must remain inside project workspace"
    );
    std::fs::create_dir_all(&out)?;
    if command == "blind-import" {
        return super::blind::import(&args, &out);
    }
    if command == "annotate" {
        return super::assistant_review::annotate(&args, &out);
    }
    let rules = if let Some(path) = arg(&args, "--rules") {
        serde_json::from_slice::<Rules>(&std::fs::read(path)?)?
    } else {
        Rules::default()
    };
    rules.validate()?;
    ensure!(
        rules.hash() == Rules::default().hash() || rules.version != Rules::default().version,
        "changed parameters require a new rules version"
    );
    let backup = arg(&args, "--backup");
    let db_input = arg(&args, "--db");
    ensure!(
        backup.is_some() ^ db_input.is_some(),
        "provide exactly one of --backup and --db"
    );
    let source = PathBuf::from(backup.as_ref().or(db_input.as_ref()).unwrap());
    let source_bytes = std::fs::read(&source)?;
    let source_hash = hash(&source_bytes);
    if backup.is_some() {
        let check = PathBuf::from(format!("{}.sha256", source.display()));
        ensure!(check.exists(), "backup checksum file missing");
        let text = std::fs::read_to_string(check)?;
        ensure!(
            text.split_whitespace().next() == Some(source_hash.as_str()),
            "backup checksum mismatch"
        );
    }
    let db_bytes = if backup.is_some() {
        let mut bytes = Vec::new();
        flate2::read::GzDecoder::new(source_bytes.as_slice()).read_to_end(&mut bytes)?;
        bytes
    } else {
        source_bytes
    };
    // Even backfill only writes this newly created copy, never the source database.
    let copied = out.join("source.sqlite");
    std::fs::write(&copied, &db_bytes)?;
    let db = Database::connect(format!(
        "sqlite://{}?mode=ro",
        copied.to_string_lossy().replace('\\', "/")
    ))
    .await?;
    db.execute_unprepared("PRAGMA query_only=ON").await?;
    let integrity = db
        .query_one(Statement::from_string(
            DbBackend::Sqlite,
            "PRAGMA quick_check".to_string(),
        ))
        .await?
        .context("integrity response")?;
    ensure!(
        integrity.try_get::<String>("", "quick_check")? == "ok",
        "database integrity check failed"
    );
    let source_events = repo::all_pattern_events(&db).await?;
    let symbols = repo::list_symbols(&db, false).await?;
    let cost: f64 = arg(&args, "--roundtrip-cost-ticks")
        .unwrap_or_else(|| "2.5".into())
        .parse()?;
    ensure!(cost.is_finite() && cost >= 0., "invalid cost assumption");
    let mut audits = Vec::new();
    let mut blind_cases = HashMap::new();
    let all_codes: std::collections::BTreeSet<_> =
        source_events.iter().map(|e| e.symbol.clone()).collect();
    for code in all_codes {
        let rows = repo::klines(&db, &code, "15m", None, None).await?;
        let rolls = repo::symbol_rollovers(&db, &code).await?;
        let bars = bars_from_rows(&rows, &rolls);
        let raw: Vec<Kline> = repo::raw_klines(&db, &code)
            .await?
            .iter()
            .map(crate::service::model_to_fetch)
            .collect();
        let roll_records: Vec<_> = rolls
            .iter()
            .map(|r| crate::derive::rollover::RolloverRecord {
                symbol: r.symbol.clone(),
                ts: r.ts.clone(),
                from_contract: r.from_contract.clone(),
                to_contract: r.to_contract.clone(),
                confirmed: r.confirmed,
            })
            .collect();
        let tick = symbols
            .iter()
            .find(|s| s.code == code)
            .map(|s| crate::precision::effective_tick(s.tick_size, &s.code, &s.variety))
            .unwrap_or(0.);
        for e in source_events.iter().filter(|e| e.symbol == code) {
            let quality = evaluate(
                e,
                &bars,
                tick,
                raw.last().map(|b| b.datetime.as_str()).unwrap_or(""),
                "historical_recompute",
                &rules,
            );
            let old_dims: Value = serde_json::from_str(&e.entry_score_dims).unwrap_or(Value::Null);
            let f = &quality.features;
            let legacy_recomputed = (|| {
                let a = f.get("legacy_a_score")?.as_ref()?;
                let b = f.get("legacy_b_score")?.as_ref()?;
                let w = f.get("legacy_warning_score")?.as_ref()?;
                Some([
                    *a,
                    *b,
                    *w,
                    crate::analyze::scoring::entry_score(*a, *b, *w, &e.warning_kind),
                ])
            })()
            .or_else(|| {
                let prefix: Vec<_> = bars
                    .iter()
                    .filter(|b| b.dt.to_bar_ts() <= e.warning_ts)
                    .cloned()
                    .collect();
                let pattern = super::legacy_pattern(e, &prefix)?;
                let atr = indicators::atr(&prefix, 20);
                if atr[pattern.s0.index..]
                    .iter()
                    .any(|a| a.is_none_or(|x| x <= 0.))
                {
                    return None;
                }
                let w = prefix
                    .iter()
                    .position(|b| b.dt.to_bar_ts() == e.warning_ts)?;
                let a = crate::analyze::scoring::score_a(&prefix, &atr, &pattern);
                let b = crate::analyze::scoring::score_b(&pattern);
                let warning = crate::analyze::scoring::dim_warning(
                    &prefix,
                    &atr,
                    w,
                    &e.warning_kind,
                    pattern.dir,
                );
                Some([
                    a,
                    b,
                    warning,
                    crate::analyze::scoring::entry_score(a, b, warning, &e.warning_kind),
                ])
            });
            let legacy_max_error = legacy_recomputed.map(|r| {
                [
                    old_dims["dim_a"].as_f64(),
                    old_dims["dim_b"].as_f64(),
                    old_dims["dim_warning"].as_f64(),
                    Some(e.entry_score),
                ]
                .iter()
                .zip(r)
                .filter_map(|(old, new)| old.map(|x| (x - new).abs()))
                .fold(0., f64::max)
            });
            let mut evaluations = BTreeMap::new();
            if quality.status == "ok" {
                let ev = ReplayEvent {
                    symbol: e.symbol.clone(),
                    direction: e.direction.clone(),
                    grade: e.grade.clone(),
                    level: e.level.clone(),
                    s0_ts: e.s0_ts.clone(),
                    s1_ts: e.s1_ts.clone(),
                    s2_ts: e.s2_ts.clone(),
                    s0_price: e.s0_price,
                    s1_price: e.s1_price,
                    s2_price: e.s2_price,
                    a_move: e.a_move,
                    b_move: e.b_move,
                    a_bars: e.a_bars,
                    b_bars: e.b_bars,
                    retracement: e.retracement,
                    warning_ts: e.warning_ts.clone(),
                    warning_kind: e.warning_kind.clone(),
                    entry: e.entry,
                    stop: e.stop,
                    target: e.target,
                    risk: e.risk,
                    rr: e.rr,
                    tick_size: tick,
                    ..Default::default()
                };
                for slip in [1., 2.] {
                    let mut eval =
                        crate::v3::replay::evaluate(&ev, &raw, &roll_records, true, slip);
                    if eval.status == "closed" {
                        eval.net_r = Some(
                            (eval.gross_points - cost * tick * eval.volume as f64)
                                / (eval.risk * eval.volume as f64),
                        );
                    }
                    evaluations
                        .insert(format!("fixed_one_{slip}tick"), serde_json::to_value(eval)?);
                }
                blind_cases.insert(e.id, super::blind::case(e, &bars, &quality, &rules));
            }
            audits.push(AuditRow {
                event_id: e.id,
                symbol: e.symbol.clone(),
                direction: e.direction.clone(),
                grade: e.grade.clone(),
                level: e.level.clone(),
                warning_ts: e.warning_ts.clone(),
                s0_ts: e.s0_ts.clone(),
                s1_ts: e.s1_ts.clone(),
                old_score: e.entry_score,
                old_dims,
                quality,
                legacy_recomputed,
                legacy_max_error,
                evaluations,
            });
        }
        println!("{code}: evaluated");
    }
    audits.sort_by(|a, b| {
        a.warning_ts
            .cmp(&b.warning_ts)
            .then(a.event_id.cmp(&b.event_id))
    });
    let git = std::process::Command::new("git")
        .args(["rev-parse", "HEAD"])
        .output()
        .ok()
        .map(|r| String::from_utf8_lossy(&r.stdout).trim().to_string());
    write_json(
        &out.join("manifest.json"),
        &json!({"source":source,"source_sha256":source_hash,"database_sha256":hash(&db_bytes),"branch":"codex/signal-quality-v1","commit":git,"created_at":chrono::Utc::now().to_rfc3339(),"rules":rules,"parameter_hash":rules.hash(),"feature_version":FEATURE_VERSION,"quality_source_sha256":quality_source_hash(),"working_tree_dirty":true,"events":source_events.len(),"origin":"historical_development","data_revision_note":"current snapshot reconstruction; not proof of original warning-time data version","cost":{"roundtrip_cost_ticks":cost,"slippage_ticks":[1,2],"actual_contracts_and_fees":false,"execution_config":crate::trader::ExecutionConfig::default(),"policy":crate::v3::POLICY}}),
    )?;
    write_json(&out.join("audit.json"), &audits)?;
    write_json(
        &out.join("four-cases.json"),
        &audits
            .iter()
            .filter(|a| [7068, 7034, 7084, 7086].contains(&a.event_id))
            .collect::<Vec<_>>(),
    )?;
    write_json(&out.join("parameters.json"), &rules)?;
    super::report::write(&out, &audits, &rules)?;
    super::blind::export(&out, &audits, &blind_cases)?;
    db.close().await?;
    if command == "backfill" {
        // No legacy migration runs: only the new independent table is created.
        let target = Database::connect(format!(
            "sqlite://{}?mode=rw",
            copied.to_string_lossy().replace('\\', "/")
        ))
        .await?;
        store::ensure(&target).await?;
        for a in &audits {
            store::save(&target, &a.quality).await?;
        }
        target.close().await?;
    }
    println!("Report: {}", out.join("report.html").display());
    Ok(())
}

fn quality_source_hash() -> String {
    let mut bytes = Vec::new();
    for (name, content) in [
        ("mod.rs", include_bytes!("mod.rs").as_slice()),
        ("research.rs", include_bytes!("research.rs").as_slice()),
        ("store.rs", include_bytes!("store.rs").as_slice()),
        ("report.rs", include_bytes!("report.rs").as_slice()),
        ("blind.rs", include_bytes!("blind.rs").as_slice()),
        (
            "rules.json",
            include_bytes!("quality-rules-v1.json").as_slice(),
        ),
        (
            "blind-review.html",
            include_bytes!("blind-review.html").as_slice(),
        ),
    ] {
        bytes.extend_from_slice(name.as_bytes());
        bytes.extend_from_slice(content);
    }
    super::hash(&bytes)
}
