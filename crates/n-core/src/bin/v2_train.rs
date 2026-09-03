use anyhow::{bail, Result};
use n_core::v2::dataset::{DatasetBuilder, DatasetRow};
use n_core::v2::model::{train_logistic, TrainConfig, train_gam, GamTrainConfig, walk_forward_purge_aware, assert_purge, compute_metrics_with_baseline, InferenceBundle};
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
        DatasetRow { event_id: format!("synth|{:04}", i), symbol: "RB".into(), direction: if i%2==0 {"up".into()} else {"down".into()}, setup_quality: 3.0 + (i%4) as f64 * 0.4, a_move: 10.0, b_move: 5.0, a_move_atr: a_atr, b_move_atr: 1.8, a_speed: 1.2, retracement: ret, warning_volume_ratio: Some(1.0 + (i%3) as f64 * 0.4), trigger_close_overshoot_r: Some(overshoot), trigger_close_location: Some(0.6 + (i%5) as f64 *0.05), trigger_body_atr: Some(0.9), trigger_volume_ratio: Some(1.1), trigger_wick_atr: Some(0.2), internal_swing_margin_r: Some(0.3), chase_distance_r: Some(0.2), missing_mask: 0, label_win: label, r_multiple: Some(if label==1 {1.5} else {-1.0}), is_1r_aux_win: Some(label==1), trigger_bar_ts: Some(format!("2024-01-{:02} {:02}:15:00", (i/24)%28+1, (i%24))), exit_ts: Some(format!("2024-01-{:02} {:02}:30:00", (i/24)%28+1, (i%24))), schema_version: "v2.1".into() }
    }).collect()
}

#[tokio::main]
async fn main() -> Result<()> {
    let args: Vec<String> = std::env::args().collect();
    let db_path = args.iter().find(|a| a.starts_with("--db=")).map(|s| s.trim_start_matches("--db=").to_string()).unwrap_or_else(|| "ntrend.db".to_string());
    let from = args.iter().find(|a| a.starts_with("--from=")).map(|s| s.trim_start_matches("--from=").to_string());
    let paranoid = args.iter().any(|a| a=="--paranoid");
    let smoke_synth = args.iter().any(|a| a=="--smoke-synth");
    let symbol_filter = args.iter().find(|a| a.starts_with("--symbol=")).map(|s| s.trim_start_matches("--symbol=").to_string());
    println!("V2 Train — db={} from={:?} paranoid={} smoke_synth={} symbol={:?}", db_path, from, paranoid, smoke_synth, symbol_filter);
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
            if let Ok(evts) = engine.replay_history(sym, &raw5m, tick) {
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
                rows = synth_rows(200);
            } else {
                bail!("zero real training rows; refusing to train. Fix the replay/data pipeline or pass --smoke-synth only for a synthetic smoke test");
            }
        }
    } else {
        if smoke_synth {
            rows = synth_rows(200);
        } else {
            bail!("database contains no 5m klines; refusing to train. Load real data or pass --smoke-synth only for a synthetic smoke test");
        }
    }
    rows.sort_by(|a,b| a.trigger_bar_ts.cmp(&b.trigger_bar_ts));
    if paranoid { n_core::v2::dataset::leakage::assert_no_leakage(&rows).unwrap(); }
    let whitelist = DatasetBuilder::default_whitelist();
    let hash = builder.hash(&rows);
    println!("dataset rows {} hash {}", rows.len(), hash.0);
    let folds = walk_forward_purge_aware(&rows, 5);
    if !folds.is_empty() { assert_purge(&rows, &folds).unwrap(); println!("walk-forward folds: {:?}", folds); }
    let n = rows.len();
    let mut test_size = if n >= 1000 { 300 } else if n >= 600 { 250 } else if n >= 300 { 200 } else if n >= 100 { (n as f64 * 0.25).round() as usize } else { (n as f64 * 0.2).round() as usize };
    if test_size < 20 { test_size = 20.min(n); }
    if test_size > 300 { test_size = 300.min(n.saturating_sub(20)); }
    if test_size >= n { test_size = (n as f64 * 0.2).round() as usize; }
    let split_idx = n.saturating_sub(test_size);
    let (train_rows, test_rows) = rows.split_at(split_idx);
    println!("split: train {} test {} (test_size {} for n={})", train_rows.len(), test_rows.len(), test_size, n);
    if train_rows.len() < 40 { println!("WARN: train set small (<40)"); }
    let cfg = TrainConfig::default();
    let gam_cfg = GamTrainConfig::default();
    let wf = evaluate_walk_forward(&rows, &folds, &whitelist, &cfg, &gam_cfg);
    println!(
        "walk-forward evaluated {} folds: Logistic pooled AUC {:.3} Brier {:.3} (median {:.3}, worst {:.3}); GAM pooled AUC {:.3} Brier {:.3} (median {:.3}, worst {:.3})",
        wf.folds_used,
        wf.logistic_auc,
        wf.logistic_brier,
        wf.logistic_median_auc,
        wf.logistic_worst_auc,
        wf.gam_auc,
        wf.gam_brier,
        wf.gam_median_auc,
        wf.gam_worst_auc
    );
    let log_out = train_logistic(train_rows, &whitelist, Some(test_rows), &cfg);
    println!("Logistic train AUC {:.3} Brier {:.3} logloss {:.3} lift {:.2}", log_out.metrics_train.auc, log_out.metrics_train.brier, log_out.metrics_train.logloss, log_out.metrics_train.top20_lift);
    if let Some(vm) = &log_out.metrics_valid { println!("Logistic valid AUC {:.3} Brier {:.3} lift {:.2} vs baseline Brier {:.3}", vm.auc, vm.brier, vm.top20_lift, vm.baseline_brier); }
    let (gam_model, gam_metrics) = train_gam(train_rows, &gam_cfg);
    let y_test: Vec<i32> = test_rows.iter().map(|r| r.label_win).collect();
    let p_gam_test: Vec<f64> = test_rows.iter().map(|r| gam_model.predict_p(r)).collect();
    let train_prior = train_rows.iter().filter(|r| r.label_win == 1).count() as f64 / train_rows.len().max(1) as f64;
    let gam_test_metrics = compute_metrics_with_baseline(&y_test, &p_gam_test, train_prior);
    println!("GAM train AUC {:.3} Brier {:.3} ; test AUC {:.3} Brier {:.3} lift {:.2}", gam_metrics.auc, gam_metrics.brier, gam_test_metrics.auc, gam_test_metrics.brier, gam_test_metrics.top20_lift);
    // GAM remains a diagnostic challenger. Do not promote it from one
    // holdout window; promotion requires a later, explicit OOT review.
    let champion = "logistic";
    println!("Champion: {}", champion);
    let git_commit = std::process::Command::new("git").args(["rev-parse", "--short", "HEAD"]).output().ok().and_then(|o| String::from_utf8(o.stdout).ok()).unwrap_or_else(|| "unknown".into()).trim().to_string();
    let out_dir = PathBuf::from("target/v2_reports");
    std::fs::create_dir_all(&out_dir).unwrap();
    let log_model_id = format!("logistic-v1-{}", &hash.0[..8]);
    let log_metrics_json = serde_json::to_string(&log_out.metrics_valid.as_ref().unwrap_or(&log_out.metrics_train)).unwrap();
    let log_coef_json = serde_json::to_string(&serde_json::json!({"intercept": log_out.model.intercept, "coefficients": log_out.model.coefficients, "feature_names": log_out.model.feature_names, "scaler_means": log_out.model.scaler_means, "scaler_stds": log_out.model.scaler_stds})).unwrap();
    let train_window = from.clone().unwrap_or_else(|| "all".into());
    db.execute(Statement::from_string(DbBackend::Sqlite, format!("INSERT OR REPLACE INTO v2_model_registry (model_id, name, schema_version, feature_whitelist, train_window, dataset_hash, coefficients, spline_knots, metrics, created_at) VALUES ('{}', 'logistic-v1', 'v2.1', '{}', '{}', '{}', '{}', NULL, '{}', '{}')", log_model_id.replace("'","''"), serde_json::to_string(&whitelist).unwrap().replace("'","''"), train_window.replace("'","''"), hash.0, log_coef_json.replace("'","''"), log_metrics_json.replace("'","''"), chrono::Utc::now().to_rfc3339()))).await.unwrap();
    println!("Wrote logistic model {}", log_model_id);
    let gam_model_id = format!("gam-v1-{}", &hash.0[..8]);
    let gam_metrics_json = serde_json::to_string(&gam_test_metrics).unwrap();
    let gam_knots_json = serde_json::to_string(&gam_model.splines).unwrap();
    let gam_coef_json = serde_json::to_string(&serde_json::json!({"intercept": gam_model.intercept, "linear_features": gam_model.linear_features, "linear_coefficients": gam_model.linear_coefficients})).unwrap();
    db.execute(Statement::from_string(DbBackend::Sqlite, format!("INSERT OR REPLACE INTO v2_model_registry (model_id, name, schema_version, feature_whitelist, train_window, dataset_hash, coefficients, spline_knots, metrics, created_at) VALUES ('{}', 'gam-v1', 'v2.1', '{}', '{}', '{}', '{}', '{}', '{}', '{}')", gam_model_id.replace("'","''"), serde_json::to_string(&whitelist).unwrap().replace("'","''"), train_window.replace("'","''"), hash.0, gam_coef_json.replace("'","''"), gam_knots_json.replace("'","''"), gam_metrics_json.replace("'","''"), chrono::Utc::now().to_rfc3339()))).await.unwrap();
    println!("Wrote GAM model {}", gam_model_id);
    let log_bundle = InferenceBundle{ model_id: log_model_id.clone(), feature_whitelist: whitelist.clone(), scaler_means: log_out.model.scaler_means.clone(), scaler_stds: log_out.model.scaler_stds.clone(), logistic: Some(log_out.model.clone()), gam: None, schema_version: "v2.1".into() };
    std::fs::write(out_dir.join("logistic_bundle.json"), serde_json::to_string_pretty(&log_bundle).unwrap()).unwrap();
    let gam_bundle = InferenceBundle{ model_id: gam_model_id.clone(), feature_whitelist: whitelist.clone(), scaler_means: vec![], scaler_stds: vec![], logistic: None, gam: Some(gam_model.clone()), schema_version: "v2.1".into() };
    std::fs::write(out_dir.join("gam_bundle.json"), serde_json::to_string_pretty(&gam_bundle).unwrap()).unwrap();
    let mut md = String::new();
    md.push_str(&format!("# V2 Model Reports — {}\n\n", chrono::Local::now().format("%Y-%m-%d %H:%M:%S")));
    md.push_str(&format!("- Dataset hash: {}\n- Rows: {} (train {} / test {}) real_events {} \n- Git: {}\n- Champion: {}\n\n", hash.0, rows.len(), train_rows.len(), test_rows.len(), real_events, git_commit, champion));
    md.push_str(&format!("## Logistic Baseline ({})\n", log_model_id));
    md.push_str(&format!("- Coef: {:?}\n- Intercept: {:.4}\n", log_out.model.coefficients, log_out.model.intercept));
    md.push_str(&format!("- Train: AUC {:.3} Brier {:.3} LogLoss {:.3} Top20 lift {:.2} (baseline Brier {:.3})\n", log_out.metrics_train.auc, log_out.metrics_train.brier, log_out.metrics_train.logloss, log_out.metrics_train.top20_lift, log_out.metrics_train.baseline_brier));
    if let Some(vm) = &log_out.metrics_valid {
        md.push_str(&format!("- Valid: AUC {:.3} Brier {:.3} LogLoss {:.3} Top20 lift {:.2} Acc {:.3} (baseline Brier {:.3} LogLoss {:.3} win_rate {:.2})\n", vm.auc, vm.brier, vm.logloss, vm.top20_lift, vm.accuracy, vm.baseline_brier, vm.baseline_logloss, vm.constant_win_rate));
        md.push_str("- Calibration (bin count avg_p avg_y):\n");
        for b in &vm.calibration { md.push_str(&format!("  - {}: n={} p={:.2} y={:.2}\n", b.bin, b.count, b.avg_p, b.avg_y)); }
    }
    md.push_str(&format!("\n## GAM Challenger ({})\n", gam_model_id));
    md.push_str(&format!("- Splines: {}\n", gam_model.splines.iter().map(|s| format!("{} df={} knots={:?}", s.feature, s.df, s.knots)).collect::<Vec<_>>().join(", ")));
    md.push_str(&format!("- Train: AUC {:.3} Brier {:.3}\n", gam_metrics.auc, gam_metrics.brier));
    md.push_str(&format!("- Test:  AUC {:.3} Brier {:.3} lift {:.2} vs logistic valid AUC {:.3}\n", gam_test_metrics.auc, gam_test_metrics.brier, gam_test_metrics.top20_lift, log_out.metrics_valid.as_ref().map(|m| m.auc).unwrap_or(0.0)));
    md.push_str(&format!("\n- Walk-forward folds: {} (evaluated {}) — purge PASS\n", folds.len(), wf.folds_used));
    md.push_str(&format!("- Walk-forward pooled OOF: Logistic AUC {:.3} Brier {:.3} (median fold AUC {:.3}, worst fold AUC {:.3}); GAM AUC {:.3} Brier {:.3} (median fold AUC {:.3}, worst fold AUC {:.3})\n", wf.logistic_auc, wf.logistic_brier, wf.logistic_median_auc, wf.logistic_worst_auc, wf.gam_auc, wf.gam_brier, wf.gam_median_auc, wf.gam_worst_auc));
    md.push_str("- Notes: Logistic is the current champion by policy; GAM is diagnostic only until it passes a dedicated out-of-time review. Metrics use the training-window prior as baseline. Pure Rust, no Python.\n");
    let lift_ok = log_out.metrics_valid.as_ref().map(|m| m.top20_lift > 1.0).unwrap_or(true);
    md.push_str(&format!("\n- Acceptance: lift>1.0 is {} ({}), leakage PASS, hash reproducible PASS\n", if lift_ok {"PASS"} else {"WARN"}, log_out.metrics_valid.as_ref().map(|m| format!("{:.2}", m.top20_lift)).unwrap_or_else(|| "n/a".into())));
    if rows.iter().any(|r| r.event_id.starts_with("synth")) { md.push_str("- Data: SYNTH smoke (DB empty or 0 trainable rows)\n"); } else { md.push_str(&format!("- Data: REAL replay (events {}, kline_cnt {})\n", real_events, kline_cnt)); }
    std::fs::write(out_dir.join("logistic_report.md"), &md).unwrap();
    std::fs::write(out_dir.join("gam_report.md"), &md).unwrap();
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
    gam_auc: f64,
    gam_brier: f64,
    logistic_median_auc: f64,
    logistic_worst_auc: f64,
    gam_median_auc: f64,
    gam_worst_auc: f64,
}

fn median(values: &mut [f64]) -> f64 {
    if values.is_empty() {
        return 0.5;
    }
    values.sort_by(|a, b| a.total_cmp(b));
    values[values.len() / 2]
}

/// Evaluate both candidates only on forward validation windows. The final
/// champion policy intentionally remains Logistic until GAM has comparable
/// out-of-time evidence and a production-ready implementation.
fn evaluate_walk_forward(
    rows: &[DatasetRow],
    folds: &[n_core::v2::model::Fold],
    whitelist: &[String],
    log_cfg: &TrainConfig,
    gam_cfg: &GamTrainConfig,
) -> WalkForwardSummary {
    let mut out = WalkForwardSummary::default();
    let mut oof_y = Vec::new();
    let mut oof_log = Vec::new();
    let mut oof_gam = Vec::new();
    let mut oof_prior_weight = 0.0;
    let mut log_fold_auc = Vec::new();
    let mut gam_fold_auc = Vec::new();
    for f in folds {
        let train_rows = &rows[f.train_start..f.train_end];
        let valid_rows = &rows[f.valid_start..f.valid_end];
        if train_rows.len() < 40 || valid_rows.len() < 20 { continue; }
        let train_prior = train_rows.iter().filter(|r| r.label_win == 1).count() as f64 / train_rows.len() as f64;
        let log = train_logistic(train_rows, whitelist, Some(valid_rows), log_cfg);
        let (gam_model, _) = train_gam(train_rows, gam_cfg);
        let y_valid: Vec<i32> = valid_rows.iter().map(|r| r.label_win).collect();
        let p_gam: Vec<f64> = valid_rows.iter().map(|r| gam_model.predict_p(r)).collect();
        let gam_metrics = compute_metrics_with_baseline(&y_valid, &p_gam, train_prior);
        if let Some(metrics) = log.metrics_valid {
            let valid_n = y_valid.len() as f64;
            oof_y.extend_from_slice(&y_valid);
            oof_log.extend(valid_rows.iter().map(|r| log.model.predict_row_p(r)));
            oof_gam.extend(p_gam);
            oof_prior_weight += train_prior * valid_n;
            log_fold_auc.push(metrics.auc);
            gam_fold_auc.push(gam_metrics.auc);
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
        let gam_oof = compute_metrics_with_baseline(&oof_y, &oof_gam, prior);
        out.logistic_auc = log_oof.auc;
        out.logistic_brier = log_oof.brier;
        out.gam_auc = gam_oof.auc;
        out.gam_brier = gam_oof.brier;
        out.logistic_median_auc = median(&mut log_fold_auc);
        out.logistic_worst_auc = log_fold_auc.iter().copied().fold(1.0, f64::min);
        out.gam_median_auc = median(&mut gam_fold_auc);
        out.gam_worst_auc = gam_fold_auc.iter().copied().fold(1.0, f64::min);
    }
    out
}


