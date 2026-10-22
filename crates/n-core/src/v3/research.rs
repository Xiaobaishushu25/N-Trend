//! Immutable runs; all currently available history is DEVELOPMENT, not prospective evidence.
use crate::{
    storage::repo,
    v2::{
        dataset::{DatasetBuilder, DatasetRow},
        model::{assert_purge, train_logistic, walk_forward_purge_aware, TrainConfig},
        replay::{ReplayEngine, ReplayEvent},
    },
};
use anyhow::{ensure, Result};
use sea_orm::{ConnectionTrait, DbBackend, Statement};
use serde_json::json;
use std::{
    collections::{BTreeMap, HashMap},
    io::Write,
    path::PathBuf,
};
fn arg(args: &[String], name: &str) -> Option<String> {
    args.iter()
        .position(|a| a == name)
        .and_then(|i| args.get(i + 1).cloned())
        .or_else(|| {
            args.iter()
                .find_map(|a| a.strip_prefix(&format!("{name}=")).map(str::to_owned))
        })
}
fn candidate_key(e: &ReplayEvent) -> String {
    format!(
        "{}|{}|{}|{}|{}|{}",
        e.symbol, e.direction, e.s0_ts, e.s1_ts, e.s2_ts, e.warning_ts
    )
}
fn write_json(path: &std::path::Path, value: &impl serde::Serialize) -> Result<()> {
    let mut f = std::fs::OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(path)?;
    f.write_all(&serde_json::to_vec_pretty(value)?)?;
    Ok(())
}
fn independent(rows: &[DatasetRow]) -> Vec<DatasetRow> {
    let mut end = HashMap::<String, String>::new();
    rows.iter()
        .filter(|r| {
            let start = r.trigger_bar_ts.as_deref().unwrap_or("");
            if end.get(&r.symbol).is_some_and(|e| start <= e.as_str()) {
                return false;
            }
            end.insert(r.symbol.clone(), r.exit_ts.clone().unwrap_or_default());
            true
        })
        .cloned()
        .collect()
}
fn stats(rows: &[&DatasetRow]) -> serde_json::Value {
    let mut returns = rows.iter().filter_map(|r| r.r_multiple).collect::<Vec<_>>();
    let n = returns.len();
    let sum = returns.iter().sum::<f64>();
    let mut equity = 0f64;
    let mut peak = 0f64;
    let mut dd = 0f64;
    for r in &returns {
        equity += r;
        peak = peak.max(equity);
        dd = dd.max(peak - equity)
    }
    let wins = rows.iter().filter(|r| r.label_win == 1).count();
    let rate = if n == 0 { 0. } else { wins as f64 / n as f64 };
    let z = 1.96;
    let denominator = 1. + z * z / (n.max(1) as f64);
    let center = (rate + z * z / (2. * n.max(1) as f64)) / denominator;
    let half = z * ((rate * (1. - rate) + z * z / (4. * n.max(1) as f64)) / n.max(1) as f64).sqrt()
        / denominator;
    let positive = returns
        .iter()
        .copied()
        .filter(|r| *r > 0.)
        .collect::<Vec<_>>();
    let negative = returns
        .iter()
        .copied()
        .filter(|r| *r < 0.)
        .collect::<Vec<_>>();
    returns.sort_by(f64::total_cmp);
    json!({"n":n,"win_rate":rate,"win_rate_wilson95":if n>0{Some([center-half,center+half])}else{None},"net_r":sum,"mean_net_r":sum/n.max(1)as f64,"median_net_r":returns.get(n/2),"closed_trade_drawdown_r":dd,"net_r_without_top5":sum-returns.iter().rev().filter(|r|**r>0.).take(5).sum::<f64>(),"payoff_ratio":if positive.is_empty()||negative.is_empty(){None}else{Some((positive.iter().sum::<f64>()/positive.len()as f64)/(-negative.iter().sum::<f64>()/negative.len()as f64))}})
}
fn classification(rows: &[DatasetRow], p: &[f64]) -> serde_json::Value {
    let mut tp = 0f64;
    let mut fp = 0f64;
    let mut fn_ = 0f64;
    let mut correct = 0usize;
    for (r, p) in rows.iter().zip(p) {
        if (*p >= 0.5) == (r.label_win == 1) {
            correct += 1
        }
        if *p >= 0.5 {
            if r.label_win == 1 {
                tp += 1.
            } else {
                fp += 1.
            }
        } else if r.label_win == 1 {
            fn_ += 1.
        }
    }
    let precision = tp / (tp + fp).max(1.);
    let recall = tp / (tp + fn_).max(1.);
    json!({"accuracy":correct as f64/rows.len().max(1)as f64,"precision":precision,"recall":recall,"f1":2.*precision*recall/(precision+recall).max(1e-9)})
}
fn comparisons(rows: &[DatasetRow], prob: &[f64], events: &[ReplayEvent]) -> serde_json::Value {
    let by_key = events
        .iter()
        .map(|ev| {
            (
                format!(
                    "{}|{}|{}|{}|{}",
                    ev.symbol, ev.direction, ev.s0_ts, ev.s2_ts, ev.warning_ts
                ),
                ev,
            )
        })
        .collect::<HashMap<_, _>>();
    let mut groups = [Vec::new(), Vec::new(), Vec::new(), Vec::new(), Vec::new()];
    for (i, r) in rows.iter().enumerate() {
        groups[0].push(r);
        if r.setup_quality >= 3.5 {
            groups[1].push(r)
        }
        if prob[i] >= 0.6 {
            groups[2].push(r)
        }
        if let Some(ev) = by_key.get(&r.event_id) {
            if ev.rr >= 1. && ev.risk / ev.tick_size >= 10. {
                groups[3].push(r);
                if prob[i] >= 0.6 {
                    groups[4].push(r)
                }
            }
        }
    }
    json!({"unfiltered":stats(&groups[0]),"score":stats(&groups[1]),"probability":stats(&groups[2]),"simple_geometry_cost":stats(&groups[3]),"simple_plus_probability":stats(&groups[4])})
}
fn validate(
    rows: &[DatasetRow],
    events: &[ReplayEvent],
    enabled: bool,
) -> Result<(serde_json::Value, Vec<DatasetRow>, Vec<f64>)> {
    let mut oof = Vec::new();
    let mut probabilities = Vec::new();
    let mut reports = Vec::new();
    if enabled && rows.len() >= 80 {
        let folds = walk_forward_purge_aware(rows, 4);
        assert_purge(rows, &folds)?;
        for fold in folds {
            let train = &rows[fold.train_start..fold.train_end];
            let valid = &rows[fold.valid_start..fold.valid_end];
            if train.len() < 40 || valid.len() < 20 {
                continue;
            }
            let fit = train_logistic(
                train,
                &super::whitelist(),
                Some(valid),
                &TrainConfig::default(),
            );
            reports.push(
                json!({"train":train.len(),"validation":valid.len(),"metrics":fit.metrics_valid}),
            );
            for r in valid {
                probabilities.push(fit.model.predict_row_p(r));
                oof.push(r.clone())
            }
        }
    }
    Ok((
        json!({"folds":reports,"comparisons":comparisons(&oof,&probabilities,events),"groups":groups(&oof,&probabilities)}),
        oof,
        probabilities,
    ))
}

fn groups(rows: &[DatasetRow], p: &[f64]) -> serde_json::Value {
    let mut output = serde_json::Map::new();
    for (name, kind) in [
        ("symbol", 0),
        ("direction", 1),
        ("month", 2),
        ("score_band", 3),
        ("probability_band", 4),
    ] {
        let mut bins = BTreeMap::<String, Vec<&DatasetRow>>::new();
        for (i, r) in rows.iter().enumerate() {
            let key = match kind {
                0 => r.symbol.clone(),
                1 => r.direction.clone(),
                2 => r
                    .trigger_bar_ts
                    .as_deref()
                    .unwrap_or("")
                    .chars()
                    .take(7)
                    .collect(),
                3 => if r.setup_quality >= 3.5 {
                    "3.5+"
                } else if r.setup_quality >= 2.5 {
                    "2.5-3.5"
                } else {
                    "<2.5"
                }
                .into(),
                _ => format!(
                    "{:.1}-{:.1}",
                    (p[i] * 5.).floor() / 5.,
                    ((p[i] * 5.).floor() + 1.) / 5.
                ),
            };
            bins.entry(key).or_default().push(r)
        }
        output.insert(
            name.into(),
            json!(bins
                .into_iter()
                .map(|(k, v)| (k, stats(&v)))
                .collect::<BTreeMap<_, _>>()),
        );
    }
    json!(output)
}
pub async fn run(args: Vec<String>, train: bool) -> Result<()> {
    let db_path = PathBuf::from(arg(&args, "--db").unwrap_or_else(|| "data-dev/ntrend.db".into()));
    ensure!(db_path.exists(), "database missing");
    let run_id = uuid::Uuid::new_v4().to_string();
    let out =
        PathBuf::from(arg(&args, "--out").unwrap_or_else(|| format!("research-runs/{run_id}")));
    ensure!(
        !out.exists(),
        "output must be a new immutable run directory"
    );
    let cost_ticks: f64 = arg(&args, "--roundtrip-cost-ticks")
        .unwrap_or_else(|| "2.5".into())
        .parse()?;
    ensure!(
        cost_ticks.is_finite() && cost_ticks > 0.,
        "explicit positive cost assumption required"
    );
    let cost_version = format!("research-proxy-roundtrip-{cost_ticks}-ticks");
    let db = crate::storage::connect(&db_path).await?;
    ensure!(
        repo::get_setting(&db, "signal_v3_prepared")
            .await?
            .as_deref()
            == Some("1"),
        "prepare an isolated v3 database first"
    );
    let mut config = crate::config::Config::default();
    config.data_source.primary_source = "offline".into();
    config.data_source.auto_spawn_bridge = false;
    config.email.enabled = false;
    let service = crate::service::Services::new(
        db.clone(),
        config,
        db_path.with_file_name("research-config.json"),
    )
    .await?;
    let manifest_path = db_path.with_file_name("manifest.json");
    ensure!(
        manifest_path.exists(),
        "research requires an isolated prepared manifest"
    );
    let manifest: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(&manifest_path)?)?;
    ensure!(
        manifest["origin"] != "offline-production-upgrade",
        "do not run research against a production data directory"
    );
    let execution_path = arg(&args, "--execution-config")
        .map(PathBuf::from)
        .or_else(|| {
            manifest["execution_config_archive"]
                .as_str()
                .map(PathBuf::from)
        })
        .or_else(|| {
            manifest["archive"]
                .as_str()
                .map(|p| PathBuf::from(p).with_file_name("execution-snapshot.json"))
        });
    let frozen_config = if let Some(path) = execution_path.filter(|p| p.exists()) {
        let j: serde_json::Value = serde_json::from_str(&std::fs::read_to_string(path)?)?;
        serde_json::from_value::<crate::trader::ExecutionConfig>(
            j.get("data")
                .and_then(|d| d.get("config"))
                .or_else(|| j.get("config"))
                .unwrap_or(&j)
                .clone(),
        )?
    } else {
        ensure!(
            args.iter().any(|a| a == "--use-default-execution-config"),
            "provide --execution-config; defaults require explicit --use-default-execution-config"
        );
        crate::trader::ExecutionConfig::default()
    };
    let symbols = repo::list_symbols(&db, false).await?;
    let engine = ReplayEngine::default_engine();
    let mut events = Vec::new();
    let mut audit = Vec::new();
    let mut counters = BTreeMap::<String, usize>::new();
    let mut lanes = HashMap::<String, Vec<ReplayEvent>>::new();
    for symbol in symbols {
        let raw = repo::raw_klines(&db, &symbol.code)
            .await?
            .iter()
            .map(crate::service::model_to_fetch)
            .collect::<Vec<_>>();
        if raw.len() < 90 {
            continue;
        }
        service.pipeline.rebuild_derived(&symbol.code).await?;
        let tick =
            crate::precision::effective_tick(symbol.tick_size, &symbol.code, &symbol.variety);
        let roll = repo::symbol_rollovers(&db, &symbol.code)
            .await?
            .into_iter()
            .filter(|r| r.confirmed)
            .map(|r| crate::derive::rollover::RolloverRecord {
                symbol: r.symbol,
                ts: r.ts,
                from_contract: r.from_contract,
                to_contract: r.to_contract,
                confirmed: r.confirmed,
            })
            .collect::<Vec<_>>();
        let mut current = engine.replay_history_with_rollovers(&symbol.code, &raw, tick, &roll)?;
        let full: HashMap<_, _> = current
            .iter()
            .map(|e| {
                (
                    candidate_key(e),
                    serde_json::to_value(&e.setup_features).unwrap(),
                )
            })
            .collect();
        // Sampled prefix audit is explicitly reported; unit fixtures cover the causal contract.
        for percentage in [25usize, 50, 75] {
            let cut = raw.len() * percentage / 100;
            let prefix =
                engine.replay_history_with_rollovers(&symbol.code, &raw[..cut], tick, &roll)?;
            let mismatch = prefix
                .iter()
                .filter(|e| {
                    full.get(&candidate_key(e))
                        != Some(&serde_json::to_value(&e.setup_features).unwrap())
                })
                .count();
            audit.push(json!({"symbol":symbol.code,"cutoff":raw[cut-1].datetime,"events":prefix.len(),"mismatches":mismatch}));
        }
        let (bars, _) = engine.aggregate_with_rollovers(&raw, &roll);
        let candidates = crate::analyze::event::replay_warnings(&symbol.code, &bars, tick);
        for ev in &mut current {
            let entry =
                repo::pattern_event_by_warning(&db, &ev.symbol, &ev.direction, &ev.warning_ts)
                    .await?;
            let row = if let Some(row) = entry {
                row
            } else {
                let candidate = candidates
                    .iter()
                    .find(|c| {
                        bars[c.warning_index].dt.to_bar_ts() == ev.warning_ts
                            && ((c.direction == crate::analyze::model::Dir::Up)
                                == (ev.direction == "up"))
                    })
                    .ok_or_else(|| anyhow::anyhow!("candidate disappeared"))?;
                crate::service::insert_warning_event(&db, &ev.symbol, &bars, candidate).await?
            };
            crate::v2::prediction::freeze_event(&db, &row, &bars, "historical_rebuild").await?;
            for fixed in [false, true] {
                for slip in [1., 2.] {
                    let mut eval = super::replay::evaluate_config(
                        ev,
                        &raw,
                        &roll,
                        fixed,
                        slip,
                        &frozen_config,
                    );
                    if eval.status == "closed" {
                        let mult =
                            crate::precision::effective_multiplier(&ev.symbol, &symbol.variety);
                        let net = eval.gross_points * mult
                            - cost_ticks * tick * mult * eval.volume as f64;
                        eval.net_amount = Some(net);
                        eval.net_r = Some(net / (eval.risk * mult * eval.volume as f64).max(1e-9));
                    }
                    let scenario = format!(
                        "{}-{slip}tick",
                        if fixed { "fixed_small" } else { "actual_tier" }
                    );
                    db.execute(Statement::from_sql_and_values(DbBackend::Sqlite,"INSERT INTO signal_evaluations(run_id,event_id,policy,scenario,evaluation_json) VALUES (?,?,?,?,?)",vec![run_id.clone().into(),row.id.into(),super::POLICY.into(),scenario.clone().into(),serde_json::to_string(&eval)?.into()])).await?;
                    let mut lane_event = ev.clone();
                    lane_event.evaluation = Some(eval.clone());
                    lanes.entry(scenario).or_default().push(lane_event);
                    if !fixed && slip == 1. {
                        *counters.entry(eval.status.clone()).or_default() += 1;
                        let mut updated = row.clone();
                        updated.state = match eval.status.as_str() {
                            "closed" => "closed",
                            "open" | "unfinished" => "triggered",
                            "pending" => "pending",
                            "untriggered" => "expired",
                            s => s,
                        }
                        .into();
                        updated.outcome = eval
                            .net_amount
                            .map(|n| if n > 0. { "win" } else { "loss" }.into());
                        updated.exit_reason = eval.exit_reason.clone();
                        updated.exit_ts = eval.exit_ts.clone();
                        updated.exit_price = eval.final_price;
                        updated.r_multiple = eval.net_r;
                        let mut dimensions: serde_json::Value =
                            serde_json::from_str(&updated.entry_score_dims)?;
                        dimensions["evaluation"] = serde_json::to_value(&eval)?;
                        dimensions["cost_version"] = json!(cost_version);
                        dimensions["evaluation_resolution"] = json!("5m_ohlc_conservative");
                        updated.entry_score_dims = dimensions.to_string();
                        updated.mfe_r = Some(eval.mfe_r);
                        updated.mae_r = Some(eval.mae_r);
                        updated.trigger_ts = eval.entry_ts.clone();
                        updated.trigger_bar_ts = eval.entry_ts.clone();
                        updated.trigger_price = eval.fill;
                        repo::update_pattern_event(&db, updated).await?;
                        ev.evaluation = Some(eval);
                    }
                }
            }
        }
        println!("{}: {} signals", symbol.code, current.len());
        events.extend(current);
    }
    let builder = DatasetBuilder::new(super::whitelist());
    let rows = builder.build(events.clone());
    let (rows, missing) = DatasetBuilder::filter_missing(rows);
    let rows = independent(&rows);
    let data_hash = builder.hash(&rows).0;
    let audit_pass = audit.iter().all(|r| r["mismatches"] == 0);
    let (actual_validation, oof_rows, oof_prob) = validate(&rows, &events, train && audit_pass)?;
    let fixed =
        independent(&builder.build(lanes.get("fixed_small-1tick").cloned().unwrap_or_default()));
    let (fixed_validation, _, _) = validate(&fixed, &events, train && audit_pass)?;
    let stress =
        independent(&builder.build(lanes.get("actual_tier-2tick").cloned().unwrap_or_default()));
    let stress_by_key = stress
        .iter()
        .map(|r| (r.event_id.clone(), r))
        .collect::<HashMap<_, _>>();
    let mut paired = Vec::new();
    let mut paired_prob = Vec::new();
    for (i, r) in oof_rows.iter().enumerate() {
        if let Some(s) = stress_by_key.get(&r.event_id) {
            paired.push((*s).clone());
            paired_prob.push(oof_prob[i]);
        }
    }
    let stress_comparison = comparisons(&paired, &paired_prob, &events);
    let fold_reports = actual_validation["folds"]
        .as_array()
        .cloned()
        .unwrap_or_default();
    let comparisons = actual_validation["comparisons"].clone();
    let mut report = json!({"run_id":run_id,"contract":super::CONTRACT,"policy":super::POLICY,"features":super::FEATURES,"cost_version":cost_version,"source":"historical_development","actual_contracts_and_costs":false,"cost_note":"2.5 tick default is an explicit research proxy, not actual historical exchange commissions; continuous-series execution remains provisional","signals":events.len(),"quality":counters,"missing_features":missing,"independent_training_rows":rows.len(),"dataset_hash":data_hash,"sampled_prefix_audit":audit,"causal_audit_pass":audit_pass,"walk_forward":fold_reports,"frozen_execution_config":frozen_config,"fixed_small_validation":fixed_validation,"paired_2tick_validation":{"n":paired.len(),"unmatched":oof_rows.len()-paired.len(),"comparisons":stress_comparison},"classification":classification(&oof_rows,&oof_prob),"actual_tier_comparisons":comparisons,"oof_groups":groups(&oof_rows,&oof_prob),"display_eligible":false,"prospective":{"origin":"not_started","trading_days":0,"independent_groups":0},"risk_audit":{"parameters_changed":false,"daily_loss_guard":"currently disabled in execution source","max_risk_percent":"configuration field requires enforcement audit","amount_limit_500":"not enabled by this change"}});
    std::fs::create_dir_all(out.parent().unwrap_or(std::path::Path::new(".")))?;
    std::fs::create_dir(&out)?;
    write_json(&out.join("signals.json"), &events)?;
    write_json(&out.join("dataset.json"), &rows)?;
    write_json(
        &out.join("oof.json"),
        &json!({"rows":oof_rows,"p_win":oof_prob}),
    )?;
    if train && rows.len() >= 80 && audit_pass && !fold_reports.is_empty() {
        let fitted = train_logistic(&rows, &super::whitelist(), None, &TrainConfig::default());
        let model_hash=blake3::hash(&serde_json::to_vec(&json!({"data":data_hash,"cost":cost_version,"contract":super::CONTRACT,"policy":super::POLICY,"features":super::FEATURES,"config":TrainConfig::default(),"execution_config":frozen_config,"source_sha256":manifest["source_sha256"],"model":fitted.model}))?).to_hex().to_string();
        let model_id = format!("logistic-v3-{}", &model_hash[..16]);
        write_json(&out.join("model.json"), &fitted.model)?;
        report["model_id"] = json!(model_id);
        db.execute(Statement::from_sql_and_values(DbBackend::Sqlite,"UPDATE v2_model_registry SET status='archived' WHERE name='logistic-v3' AND status='challenger' AND model_id<>?",vec![model_id.clone().into()])).await?;
        db.execute(Statement::from_sql_and_values(DbBackend::Sqlite,"INSERT INTO v2_model_registry(model_id,name,schema_version,feature_whitelist,train_window,dataset_hash,coefficients,spline_knots,metrics,created_at,status,scoring_slot) VALUES (?,'logistic-v3','3',?,'historical-development',?,?,NULL,?,?,'challenger','default') ON CONFLICT(model_id) DO NOTHING",vec![model_id.into(),serde_json::to_string(&super::whitelist())?.into(),data_hash.into(),serde_json::to_string(&fitted.model)?.into(),serde_json::to_string(&report)?.into(),chrono::Utc::now().to_rfc3339().into()])).await?;
    }
    write_json(&out.join("report.json"), &report)?;
    let text=format!("# Signal system v3 research\n\nHistorical development only. Not prospective validation.\n\n- Signals: {}\n- Training rows (non-overlap): {}\n- Prefix audit: {}\n- Cost: {} (proxy, not actual fees)\n- Qualified for display/trading: NO\n\n```json\n{}\n```\n",events.len(),rows.len(),audit_pass,cost_version,serde_json::to_string_pretty(&report)?);
    std::fs::write(out.join("report.md"), text)?;
    db.execute(Statement::from_sql_and_values(DbBackend::Sqlite,"INSERT INTO analysis_runs(run_id,contract,policy,cost_version,created_at,status,report_json) VALUES (?,?,?,?,?,'completed',?)",vec![run_id.into(),super::CONTRACT.into(),super::POLICY.into(),cost_version.into(),chrono::Utc::now().to_rfc3339().into(),serde_json::to_string(&report)?.into()])).await?;
    println!(
        "report={} signals={} independent_rows={} audit_pass={} display_eligible=false",
        out.display(),
        events.len(),
        rows.len(),
        audit_pass
    );
    Ok(())
}
