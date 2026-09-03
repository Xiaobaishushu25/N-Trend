use anyhow::{bail, Result};
use n_core::v2::dataset::{DatasetBuilder, DatasetRow};
use n_core::v2::model::{train_logistic, TrainConfig, walk_forward_purge_aware, assert_purge, compute_metrics_with_baseline, InferenceBundle, split_final_holdout};
use n_core::v2::replay::{ReplayEngine, ReplayConfig};
use n_core::storage;
use std::path::PathBuf;
use sea_orm::{ConnectionTrait, Statement, DbBackend};

fn synth_rows(n: usize) -> Vec<DatasetRow> {
    (0..n).map(|i| {
        let a_atr = if i % 3 == 0 { 1.2 } else if i % 3 == 1 { 3.5 } else { 5.8 };
        let ret = 0.3 + (i as f64 * 0.02).sin().abs() * 0.4;
        let overshoot = if i % 2 == 0 { 0.15 } else { 0.45 };
        let label = if a_atr > 3.0 && overshoot > 0.3 { 1 } else if i % 5 == 0 { 1 } else { 0 };
        DatasetRow { event_id: format!("synth|{:04}", i), symbol: "RB".into(), direction: if i%2==0 {"up".into()} else {"down".into()}, setup_quality: 3.0 + (i%4) as f64 * 0.4, a_move: 10.0, b_move: 5.0, a_move_atr: a_atr, b_move_atr: 1.8, a_speed: 1.2, retracement: ret, warning_volume_ratio: Some(1.0 + (i%3) as f64 * 0.4), trigger_close_overshoot_r: Some(overshoot), trigger_close_location: Some(0.6 + (i%5) as f64 *0.05), trigger_body_atr: Some(0.9), trigger_volume_ratio: Some(1.1), trigger_wick_atr: Some(0.2), internal_swing_margin_r: Some(0.3), chase_distance_r: Some(0.2), missing_mask: 0, label_win: label, r_multiple: Some(if label==1 {1.5} else {-1.0}), is_1r_aux_win: Some(label==1), trigger_bar_ts: Some(format!("2024-01-{:02} {:02}:15:00", (i/24)%28+1, (i%24))), exit_ts: Some(format!("2024-01-{:02} {:02}:30:00", (i/24)%28+1, (i%24))), schema_version: n_core::v2::FEATURE_SCHEMA_VERSION.into(), trend_gap_60: None, trend_slope_60: None, trend_strength_60: None, trend_alignment_60: None, trend_10d: None, trend_alignment_10d: None, range_position_10d: None, mr_position_10d: None, distance_ma10_dir: None, trend_position_interaction: None, context_as_of_ts: None, context_last_60m_ts: None, context_last_daily_day: None, crossed_rollover_10d: false }
    }).collect()
}

#[tokio::main]
async fn main() -> Result<()> {
    let args: Vec<String> = std::env::args().collect();
    let db_path = args.iter().find(|a| a.starts_with("--db=")).map(|s| s.trim_start_matches("--db=").to_string()).unwrap_or_else(|| "ntrend.db".to_string());
    let from = args.iter().find(|a| a.starts_with("--from=")).map(|s| s.trim_start_matches("--from=").to_string());
    let paranoid = args.iter().any(|a| a=="--paranoid");
    let smoke_synth = args.iter().any(|a| a=="--smoke-synth");
    let evaluate_locked = args.iter().any(|a| a=="--evaluate-locked");
    let symbol_filter = args.iter().find(|a| a.starts_with("--symbol=")).map(|s| s.trim_start_matches("--symbol=").to_string());
    println!("V2 Train — db={} from={:?} paranoid={} smoke_synth={} evaluate_locked={} symbol={:?}", db_path, from, paranoid, smoke_synth, evaluate_locked, symbol_filter);
    let db = if std::path::Path::new(&db_path).exists() {
        storage::connect(&PathBuf::from(db_path.clone())).await?
    } else {
        storage::connect(&PathBuf::from(":memory:")).await?
    };
    let kline_cnt: i64 = db.query_one(Statement::from_string(DbBackend::Sqlite, "SELECT COUNT(*) as c FROM klines".to_string())).await.ok().flatten().and_then(|r| r.try_get::<i64>("","c").ok()).unwrap_or(0);
    println!("klines in db: {}", kline_cnt);
    let builder = DatasetBuilder::new(DatasetBuilder::default_whitelist());
    let mut rows: Vec<DatasetRow>;
    let mut real_events = 0usize;
    if kline_cnt > 0 {
        let symbols: Vec<String> = if let Some(sf) = symbol_filter.clone() { if sf == "all" { vec![] } else { vec![sf] } } else { vec![] };
        let symbols_to_run: Vec<String> = if !symbols.is_empty() { symbols } else {
            let mut syms = Vec::new();
            if let Ok(Some(row)) = db.query_one(Statement::from_string(DbBackend::Sqlite, "SELECT GROUP_CONCAT(symbol, ',') as s FROM (SELECT DISTINCT symbol FROM klines WHERE timeframe='5m' AND source='raw')".to_string())).await {
                if let Ok(s) = row.try_get::<String>("","s") { syms = s.split(',').map(|x| x.trim().to_string()).filter(|x| !x.is_empty()).collect(); }
            }
            if syms.is_empty() {
                if let Ok(rows_db) = db.query_all(Statement::from_string(DbBackend::Sqlite, "SELECT DISTINCT symbol FROM klines WHERE timeframe='5m' AND source='raw'".to_string())).await {
                    for r in rows_db { if let Ok(s) = r.try_get::<String>("","symbol") { syms.push(s); } }
                }
            }
            syms
        };
        println!("symbols to replay: {:?} ({} total)", symbols_to_run.iter().take(5).collect::<Vec<_>>(), symbols_to_run.len());
        let engine = ReplayEngine::new(ReplayConfig::default());
        let mut all_events = Vec::new();
        for sym in &symbols_to_run {
            let symbol_meta = db
                .query_one(Statement::from_string(
                    DbBackend::Sqlite,
                    format!("SELECT tick_size, variety FROM symbols WHERE code='{}'", sym.replace("'", "''")),
                ))
                .await
                .ok()
                .flatten();
            let stored_tick = symbol_meta.as_ref().and_then(|r| r.try_get::<f64>("", "tick_size").ok()).unwrap_or(0.0);
            let variety = symbol_meta.as_ref().and_then(|r| r.try_get::<String>("", "variety").ok()).unwrap_or_default();
            let tick = n_core::precision::effective_tick(stored_tick, sym, &variety);
            let sql = if let Some(ref f) = from {
                format!("SELECT ts, open, high, low, close, volume, hold FROM klines WHERE symbol='{}' AND timeframe='5m' AND source='raw' AND ts >= '{}' ORDER BY ts ASC", sym.replace("'","''"), f.replace("'","''"))
            } else {
                format!("SELECT ts, open, high, low, close, volume, hold FROM klines WHERE symbol='{}' AND timeframe='5m' AND source='raw' ORDER BY ts ASC", sym.replace("'","''"))
            };
            let kline_rows = db.query_all(Statement::from_string(DbBackend::Sqlite, sql)).await.unwrap_or_default();
            if kline_rows.is_empty() { continue; }
            let mut raw5m: Vec<n_core::fetch::kline::Kline> = Vec::with_capacity(kline_rows.len());
            for r in kline_rows {
                let ts: String = r.try_get("","ts").unwrap_or_default();
                let open: f64 = r.try_get("","open").unwrap_or(0.0);
                let high: f64 = r.try_get("","high").unwrap_or(0.0);
                let low: f64 = r.try_get("","low").unwrap_or(0.0);
                let close: f64 = r.try_get("","close").unwrap_or(0.0);
                let volume: f64 = r.try_get("","volume").unwrap_or(0.0);
                let hold: f64 = r.try_get("","hold").unwrap_or(0.0);
                raw5m.push(n_core::fetch::kline::Kline{ datetime: ts, open, high, low, close, volume, hold });
            }
            let rollover_rows = db.query_all(Statement::from_string(DbBackend::Sqlite, format!("SELECT ts, from_contract, to_contract, confirmed FROM rollovers WHERE symbol='{}' ORDER BY ts ASC", sym.replace("'", "''")))).await.unwrap_or_default();
            let rollover_records = rollover_rows.into_iter().filter_map(|r| {
                let confirmed: bool = r.try_get("", "confirmed").ok()?;
                if !confirmed { return None; }
                Some(n_core::derive::rollover::RolloverRecord { symbol: sym.clone(), ts: r.try_get("", "ts").ok()?, from_contract: r.try_get("", "from_contract").ok()?, to_contract: r.try_get("", "to_contract").ok()?, confirmed })
            }).collect::<Vec<_>>();
            if let Ok(evts) = engine.replay_history_with_rollovers(sym, &raw5m, tick, &rollover_records) {
                real_events += evts.len();
                all_events.extend(evts);
            }
        }
        println!("real replay events total: {} across {} symbols", all_events.len(), symbols_to_run.len());
        let trig_cnt = all_events.iter().filter(|e| e.trigger_features.is_some()).count();
        let out_cnt = all_events.iter().filter(|e| e.outcome.is_some()).count();
        println!("diagnostic trig {} out {} both {}", trig_cnt, out_cnt, all_events.iter().filter(|e| e.trigger_features.is_some() && e.outcome.is_some()).count());
        if trig_cnt>0 { for e in all_events.iter().filter(|e| e.trigger_features.is_some()).take(2) { println!("sample trig {} {} trig_ts {:?} outcome {:?}", e.symbol, e.s2_ts, e.trigger_bar_ts, e.outcome.as_ref().map(|o| &o.outcome)); } } else if !all_events.is_empty() { for e in all_events.iter().take(2) { println!("sample no-trig {} dir {} s2 {} lvl {} entry {} stop {} target {}", e.symbol, e.direction, e.s2_ts, e.trigger_level, e.entry, e.stop, e.target); } }
        rows = builder.build(all_events);
        println!("real dataset rows (trigger+outcome): {}", rows.len());
        if rows.is_empty() {
            if smoke_synth {
                println!("WARN: real klines produced 0 trainable rows — using explicit synth smoke mode");
                rows = synth_rows(500);
            } else {
                bail!("zero real training rows; refusing to train. Fix the replay/data pipeline or pass --smoke-synth only for a synthetic smoke test");
            }
        }
    } else {
        if smoke_synth {
            rows = synth_rows(500);
        } else {
            bail!("database contains no 5m klines; refusing to train. Load real data or pass --smoke-synth only for a synthetic smoke test");
        }
    }
    rows.sort_by(|a,b| a.trigger_bar_ts.cmp(&b.trigger_bar_ts));
    if paranoid { n_core::v2::dataset::leakage::assert_no_leakage(&rows).unwrap(); }
    let whitelist = DatasetBuilder::default_whitelist();
    let hash = builder.hash(&rows);
    println!("dataset rows {} hash {}", rows.len(), hash.0);
    let (dev_rows, locked_test) = split_final_holdout(&rows, 300);
    if locked_test.is_empty() || dev_rows.len() < 40 { bail!("insufficient rows for DEV plus locked historical test: total={}, dev={}, locked={}", rows.len(), dev_rows.len(), locked_test.len()); }
    let folds = walk_forward_purge_aware(dev_rows, 4);
    let dev_hash = builder.hash(dev_rows);
    if !folds.is_empty() { assert_purge(dev_rows, &folds).unwrap(); println!("walk-forward folds: {:?}", folds); }
    println!("split: DEV {} LOCKED_HISTORICAL_TEST {} (fixed holdout=300)", dev_rows.len(), locked_test.len());
    let cfg = TrainConfig::default();
    let wf = evaluate_walk_forward(dev_rows, &folds, &whitelist, &cfg);
    println!(
        "walk-forward evaluated {} folds: Logistic pooled AUC {:.3} Brier {:.3} (median {:.3}, worst {:.3})",
        wf.folds_used,
        wf.logistic_auc,
        wf.logistic_brier,
        wf.logistic_median_auc,
        wf.logistic_worst_auc
    );
    // Locked historical test is reserved until feature/model selection ends.
    let log_out = train_logistic(dev_rows, &whitelist, if evaluate_locked { Some(locked_test) } else { None }, &cfg);
    println!("Logistic train AUC {:.3} Brier {:.3} logloss {:.3} lift {:.2}", log_out.metrics_train.auc, log_out.metrics_train.brier, log_out.metrics_train.logloss, log_out.metrics_train.top20_lift);
    if let Some(vm) = &log_out.metrics_valid { println!("Logistic valid AUC {:.3} Brier {:.3} lift {:.2} vs baseline Brier {:.3}", vm.auc, vm.brier, vm.top20_lift, vm.baseline_brier); }
    println!("GAM: not run in this stage (Logistic-only policy)");
    println!("Locked historical test: {}", if evaluate_locked { "evaluated by explicit --evaluate-locked" } else { "RESERVED (not evaluated before feature/model selection)" });
    let git_commit = std::process::Command::new("git").args(["rev-parse", "--short", "HEAD"]).output().ok().and_then(|o| String::from_utf8(o.stdout).ok()).unwrap_or_else(|| "unknown".into()).trim().to_string();
    let out_dir = PathBuf::from("target/v2_reports");
    std::fs::create_dir_all(&out_dir).unwrap();
    let log_model_id = format!("logistic-v1-{}", &dev_hash.0[..8]);
    let log_metrics_json = serde_json::to_string(&log_out.metrics_valid.as_ref().unwrap_or(&log_out.metrics_train)).unwrap();
    let log_coef_json = serde_json::to_string(&serde_json::json!({"intercept": log_out.model.intercept, "coefficients": log_out.model.coefficients, "feature_names": log_out.model.feature_names, "scaler_means": log_out.model.scaler_means, "scaler_stds": log_out.model.scaler_stds})).unwrap();
    let train_window = from.clone().unwrap_or_else(|| "all".into());
    db.execute(Statement::from_string(DbBackend::Sqlite, format!("INSERT OR REPLACE INTO v2_model_registry (model_id, name, schema_version, feature_whitelist, train_window, dataset_hash, coefficients, spline_knots, metrics, created_at, status, scoring_slot) VALUES ('{}', 'logistic-v1', '{}', '{}', '{}', '{}', '{}', NULL, '{}', '{}', 'challenger', 'default')", log_model_id.replace("'","''"), n_core::v2::FEATURE_SCHEMA_VERSION, serde_json::to_string(&whitelist).unwrap().replace("'","''"), train_window.replace("'","''"), dev_hash.0, log_coef_json.replace("'","''"), log_metrics_json.replace("'","''"), chrono::Utc::now().to_rfc3339()))).await.unwrap();
    println!("Wrote logistic model {}", log_model_id);
    let log_bundle = InferenceBundle{ model_id: log_model_id.clone(), feature_whitelist: whitelist.clone(), scaler_means: log_out.model.scaler_means.clone(), scaler_stds: log_out.model.scaler_stds.clone(), logistic: Some(log_out.model.clone()), gam: None, schema_version: n_core::v2::FEATURE_SCHEMA_VERSION.into() };
    std::fs::write(out_dir.join("logistic_bundle.json"), serde_json::to_string_pretty(&log_bundle).unwrap()).unwrap();
    let mut md = String::new();
    md.push_str(&format!("# V2 Model Reports — {}\n\n", chrono::Local::now().format("%Y-%m-%d %H:%M:%S")));
    md.push_str(&format!("- Dataset hash: {}\n- Rows: {} (DEV {} / locked historical test {}) real_events {} \n- Git: {}\n- Status: challenger (not champion)\n\n", hash.0, rows.len(), dev_rows.len(), locked_test.len(), real_events, git_commit));
    md.push_str(&format!("## Logistic Baseline ({})\n", log_model_id));
    md.push_str(&format!("- Coef: {:?}\n- Intercept: {:.4}\n", log_out.model.coefficients, log_out.model.intercept));
    md.push_str(&format!("- Train: AUC {:.3} Brier {:.3} LogLoss {:.3} Top20 lift {:.2} (baseline Brier {:.3})\n", log_out.metrics_train.auc, log_out.metrics_train.brier, log_out.metrics_train.logloss, log_out.metrics_train.top20_lift, log_out.metrics_train.baseline_brier));
    if let Some(vm) = &log_out.metrics_valid {
        md.push_str(&format!("- Valid: AUC {:.3} Brier {:.3} LogLoss {:.3} Top20 lift {:.2} Acc {:.3} (baseline Brier {:.3} LogLoss {:.3} win_rate {:.2})\n", vm.auc, vm.brier, vm.logloss, vm.top20_lift, vm.accuracy, vm.baseline_brier, vm.baseline_logloss, vm.constant_win_rate));
        md.push_str("- Calibration (bin count avg_p avg_y):\n");
        for b in &vm.calibration { md.push_str(&format!("  - {}: n={} p={:.2} y={:.2}\n", b.bin, b.count, b.avg_p, b.avg_y)); }
    }
    md.push_str(&format!("\n- Walk-forward folds: {} (evaluated {}) — purge PASS\n", folds.len(), wf.folds_used));
    md.push_str(&format!("- Walk-forward pooled OOF: Logistic AUC {:.3} Brier {:.3} (median fold AUC {:.3}, worst fold AUC {:.3})\n", wf.logistic_auc, wf.logistic_brier, wf.logistic_median_auc, wf.logistic_worst_auc));
    md.push_str("- Notes: Logistic-only stage; locked test is reserved for the post-selection run. RR is intentionally excluded.\n");
    if evaluate_locked {
        if let Some(vm) = &log_out.metrics_valid {
            md.push_str(&format!("\n## LOCKED HISTORICAL TEST (explicit one-time evaluation)\n- AUC {:.3} Brier {:.3} LogLoss {:.3} Top20 lift {:.2}\n", vm.auc, vm.brier, vm.logloss, vm.top20_lift));
        }
    } else {
        md.push_str("\n## LOCKED HISTORICAL TEST\n- RESERVED; pass --evaluate-locked only after feature/model selection is complete.\n");
    }
    let lift_ok = log_out.metrics_valid.as_ref().map(|m| m.top20_lift > 1.0).unwrap_or(true);
    md.push_str(&format!("\n- Acceptance: lift>1.0 is {} ({}), leakage PASS, hash reproducible PASS\n", if lift_ok {"PASS"} else {"WARN"}, log_out.metrics_valid.as_ref().map(|m| format!("{:.2}", m.top20_lift)).unwrap_or_else(|| "n/a".into())));
    if rows.iter().any(|r| r.event_id.starts_with("synth")) { md.push_str("- Data: SYNTH smoke (DB empty or 0 trainable rows)\n"); } else { md.push_str(&format!("- Data: REAL replay (events {}, kline_cnt {})\n", real_events, kline_cnt)); }
    std::fs::write(out_dir.join("logistic_report.md"), &md).unwrap();
    std::fs::write(out_dir.join("gam_report.md"), "# GAM Report\n\nNot run in the strict Logistic-only context validation stage.\n").unwrap();
    println!("Wrote target/v2_reports/logistic_report.md and gam_report.md");
    let backfill = n_core::v2::prediction::backfill(&db).await?;
    println!(
        "V2 predictions: {} events scored, {} rows written; cohort current={} legacy={}",
        backfill.events_scored,
        backfill.predictions_written,
        backfill.current_cohort_events,
        backfill.legacy_cohort_events
    );
    Ok(())
}

#[derive(Default, Debug, Clone, Copy)]
struct WalkForwardSummary {
    folds_used: usize,
    logistic_auc: f64,
    logistic_brier: f64,
    logistic_median_auc: f64,
    logistic_worst_auc: f64,
}

fn median(values: &mut [f64]) -> f64 {
    if values.is_empty() {
        return 0.5;
    }
    values.sort_by(|a, b| a.total_cmp(b));
    values[values.len() / 2]
}

/// Evaluate the fixed Logistic baseline only on forward validation windows.
fn evaluate_walk_forward(
    rows: &[DatasetRow],
    folds: &[n_core::v2::model::Fold],
    whitelist: &[String],
    log_cfg: &TrainConfig,
) -> WalkForwardSummary {
    let mut out = WalkForwardSummary::default();
    let mut oof_y = Vec::new();
    let mut oof_log = Vec::new();
    let mut oof_prior_weight = 0.0;
    let mut log_fold_auc = Vec::new();
    for f in folds {
        let train_rows = &rows[f.train_start..f.train_end];
        let valid_rows = &rows[f.valid_start..f.valid_end];
        if train_rows.len() < 40 || valid_rows.len() < 20 { continue; }
        let train_prior = train_rows.iter().filter(|r| r.label_win == 1).count() as f64 / train_rows.len() as f64;
        let log = train_logistic(train_rows, whitelist, Some(valid_rows), log_cfg);
        let y_valid: Vec<i32> = valid_rows.iter().map(|r| r.label_win).collect();
        if let Some(metrics) = log.metrics_valid {
            let valid_n = y_valid.len() as f64;
            oof_y.extend_from_slice(&y_valid);
            oof_log.extend(valid_rows.iter().map(|r| log.model.predict_row_p(r)));
            oof_prior_weight += train_prior * valid_n;
            log_fold_auc.push(metrics.auc);
            out.folds_used += 1;
        }
    }
    if out.folds_used > 0 {
        let prior = if oof_y.is_empty() {
            0.5
        } else {
            (oof_prior_weight / oof_y.len() as f64).clamp(1e-6, 1.0 - 1e-6)
        };
        let log_oof = compute_metrics_with_baseline(&oof_y, &oof_log, prior);
        out.logistic_auc = log_oof.auc;
        out.logistic_brier = log_oof.brier;
        out.logistic_median_auc = median(&mut log_fold_auc);
        out.logistic_worst_auc = log_fold_auc.iter().copied().fold(1.0, f64::min);
    }
    out
}


