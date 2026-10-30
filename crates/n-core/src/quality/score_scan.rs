//! Read-only full-history warning rescan; a single frozen research score candidate.
use super::{
    hash,
    research::{write_json, AuditRow},
    Rules, SignalQualityDto,
};
use crate::{
    analyze::{
        event::WarningCandidate,
        model::{Bar, Dir},
    },
    storage::{entities::pattern_events, repo},
    v2::replay::ReplayEvent,
    v3::ablation::{self, Experiment, Policy, ResearchRules},
};
use anyhow::{ensure, Context, Result};
use sea_orm::Database;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, HashSet},
    io::Write,
    path::{Path, PathBuf},
};

pub const PROFILE: &str = include_str!("quality-rules-v2-candidate-01.json");
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Profile {
    version: String,
    status: String,
    a_concentration_strength: f64,
    b_process_strength: f64,
    b_attempt_scale: f64,
    b_depth_scale: f64,
    morphology_weights: [f64; 3],
    quality_weights: [f64; 3],
    endpoint_penalty: f64,
    missing_levels: String,
    method: String,
}
impl Profile {
    fn validate(&self) -> Result<()> {
        ensure!(
            self.version == "quality-rules-v2-candidate-01" && self.endpoint_penalty == 0.,
            "unsupported candidate version or endpoint penalty"
        );
        for weights in [self.morphology_weights, self.quality_weights] {
            ensure!(
                weights.iter().all(|x| x.is_finite() && *x >= 0.)
                    && (weights.iter().sum::<f64>() - 1.).abs() < 1e-9,
                "invalid weights"
            );
        }
        ensure!(
            (0.0..=1.0).contains(&self.a_concentration_strength)
                && (0.0..=1.0).contains(&self.b_process_strength)
                && self.b_attempt_scale > 0.
                && self.b_depth_scale > 0.,
            "invalid candidate parameters"
        );
        Ok(())
    }
}
fn number(v: &Value) -> Option<f64> {
    v.as_f64().filter(|x| x.is_finite())
}
pub fn candidate_scores(q: &SignalQualityDto, d: &Value, p: &Profile) -> Option<Value> {
    if q.status != "ok" {
        return None;
    }
    let m = &d["modules"];
    let a = &m["a_process"]["metrics"];
    let b = &m["b_process"]["metrics"];
    let concentration = (number(&a["directional_body"]["largest_share"])?
        + number(&a["closing_records"]["largest_share"])?)
        / 2.;
    let anew = q.a_score? * (1. - p.a_concentration_strength * concentration);
    let attempts = (number(&b["reverse_body_bars"])? + number(&b["reverse_close_steps"])?) / 2.;
    let depth = if b["bars_without_warning"] == 0 {
        0.
    } else {
        (number(&b["max_range_retreat_from_s1_close_atr"])?
            + number(&b["max_close_retreat_from_s1_close_atr"])?)
            / 2.
            / number(&a["amplitude_atr"])?
    };
    let support =
        ((attempts / (attempts + p.b_attempt_scale)) * (depth / (depth + p.b_depth_scale))).sqrt();
    let bnew = q.b_score? * (1. - p.b_process_strength + p.b_process_strength * support);
    let trend = q.trend_score?;
    let space = q.space_score?;
    let morph = p
        .morphology_weights
        .iter()
        .zip([trend, anew, bnew])
        .map(|(w, x)| w * x)
        .sum::<f64>();
    let level =
        number(&m["levels"]["metrics"]["nearest_forward_obstacle_r"]).map(|r| 100. * r / (1. + r));
    let known = p.quality_weights[0]
        + p.quality_weights[1]
        + if level.is_some() {
            p.quality_weights[2]
        } else {
            0.
        };
    let total = (p.quality_weights[0] * morph
        + p.quality_weights[1] * space
        + p.quality_weights[2] * level.unwrap_or(0.))
        / known;
    Some(
        json!({"morphology_v1":q.morphology_score,"quality_v1":q.quality_score,"morphology_v2":morph,"quality_v2":total,
        "trend_ma":trend,"a_v2":anew,"b_v2":bnew,"space":space,"levels":level,
        "a_concentration":concentration,"b_attempts":attempts,"b_incremental_depth_ratio":depth,"b_support":support,
        "levels_partial":level.is_none(),"b_absent_before_warning":b["bars_without_warning"]==0,"candidate_version":p.version}),
    )
}
fn model(
    symbol: &str,
    bars: &[Bar],
    c: &WarningCandidate,
    id: i64,
) -> Result<pattern_events::Model> {
    let long = c.direction == Dir::Up;
    let s0 = &bars[c.s0_index];
    let s1 = &bars[c.s1_index];
    let s2 = &bars[c.s2_index];
    let w = bars[c.warning_index].dt.to_bar_ts();
    Ok(serde_json::from_value(
        json!({"id":id,"symbol":symbol,"direction":if long{"up"}else{"down"},"grade":c.grade,"level":c.level,
        "s0_ts":s0.dt.to_bar_ts(),"s1_ts":s1.dt.to_bar_ts(),"s2_ts":s2.dt.to_bar_ts(),
        "s0_price":if long{s0.low}else{s0.high},"s1_price":if long{s1.high}else{s1.low},"s2_price":if long{s2.low}else{s2.high},
        "a_move":c.a_move,"b_move":c.b_move,"a_bars":c.a_bars,"b_bars":c.b_bars,"retracement":c.retracement,
        "warning_ts":w,"detected_at":w,"warning_kind":c.warning_kind,"entry_score":c.entry_score,
        "entry_score_dims":json!({"dim_a":c.dim_a,"dim_b":c.dim_b,"dim_warning":c.dim_warning,"trend_state":c.trend_state,"trend_bonus":c.trend_bonus}).to_string(),
        "entry":c.entry,"stop":c.stop,"target":c.target,"risk":c.risk,"rr":c.rr,
        "state":"pending","hold_score_history":"[]","created_at":w,"updated_at":w}),
    )?)
}
fn replay(e: &pattern_events::Model, tick: f64) -> ReplayEvent {
    let mut ev = ReplayEvent {
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
    ev.setup_features.setup_quality = e.entry_score;
    ev
}
fn signature(c: &WarningCandidate) -> String {
    format!(
        "{:?}|{}|{}|{}|{}|{}",
        c.direction, c.level, c.s0_index, c.s1_index, c.s2_index, c.warning_index
    )
}

pub async fn run(args: &[String], out: &Path) -> Result<()> {
    let arg = |k: &str| {
        args.windows(2)
            .find(|x| x[0] == k)
            .map(|x| PathBuf::from(&x[1]))
            .with_context(|| format!("{k} required"))
    };
    ensure!(
        chrono::Local::now().offset().local_minus_utc() == 8 * 3600,
        "requires Asia/Shanghai host timezone"
    );
    let db_path = arg("--db")?;
    let db_bytes = std::fs::read(&db_path)?;
    let source: Value = serde_json::from_slice(&std::fs::read(arg("--source-manifest")?)?)?;
    ensure!(
        source["database_sha256"] == hash(&db_bytes),
        "frozen database hash mismatch"
    );
    let profile: Profile = serde_json::from_str(PROFILE)?;
    profile.validate()?;
    let rules = Rules::default();
    let code = hash(
        &[
            include_bytes!("score_scan.rs").as_slice(),
            include_bytes!("structure.rs").as_slice(),
            include_bytes!("../analyze/event.rs").as_slice(),
            include_bytes!("../v3/ablation.rs").as_slice(),
            include_bytes!("mod.rs").as_slice(),
        ]
        .concat(),
    );
    let protocol = json!({"candidate":profile,"candidate_hash":hash(PROFILE.as_bytes()),"baseline_rules":rules,"source_database_sha256":hash(&db_bytes),
        "code_sha256":code,"frozen_at":chrono::Utc::now().to_rfc3339(),"development_only":true,
        "population":"all symbols with raw5m; causal 15m N warnings, eligible fine/large candidates before final old-score winner. Original N geometry filters retained; earliest warning per exact S0/S1/S2/direction; identical cross-level geometry counted once (stable fine-first), distinct shapes retained. No old-score or risk gate.",
        "execution":"two monotone OHLC paths must agree, fixed_one_r and original_s1 policies, existing no-follow and horizon, 0/1/2 tick slippage; no score admission on fixed policies. Fee sensitivity reported separately, all statuses retained.",
        "calendar_overrides":{"SF0":"day-only","SM0":"day-only"},
        "calendar_override_source":"frozen archive day-only sessions, consistent with documented 2026-10-22 offline calendar correction; global/live default is untouched",
        "candidate_selection":"one predeclared hand-authored candidate; no grid search, no training against 20 tags, same historical data already partly inspected; not out-of-sample evidence"});
    write_json(&out.join("protocol.json"), &protocol)?;
    std::fs::write(out.join("candidate-rules.json"), PROFILE)?;
    let db = Database::connect(format!(
        "sqlite://{}?mode=ro",
        db_path.to_string_lossy().replace('\\', "/")
    ))
    .await?;
    let mut symbols = repo::list_symbols(&db, false).await?;
    symbols.sort_by(|a, b| a.code.cmp(&b.code));
    let mut writer = std::io::BufWriter::new(
        std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(out.join("signals.jsonl"))?,
    );
    let mut coverage = Vec::new();
    let mut id = 0_i64;
    for symbol in symbols {
        let raw: Vec<_> = repo::raw_klines(&db, &symbol.code)
            .await?
            .iter()
            .map(crate::service::model_to_fetch)
            .collect();
        if raw.is_empty() {
            coverage.push(json!({"symbol":symbol.code,"raw5m":0,"status":"no_raw_data"}));
            continue;
        }
        let rolls: Vec<_> = repo::symbol_rollovers(&db, &symbol.code)
            .await?
            .into_iter()
            .map(|r| crate::derive::rollover::RolloverRecord {
                symbol: r.symbol,
                ts: r.ts,
                from_contract: r.from_contract,
                to_contract: r.to_contract,
                confirmed: r.confirmed,
            })
            .collect();
        let (bars, _) = crate::v2::replay::ReplayEngine::default_engine()
            .aggregate_with_rollovers(&raw, &rolls);
        let tick =
            crate::precision::effective_tick(symbol.tick_size, &symbol.code, &symbol.variety);
        let multiplier = crate::precision::effective_multiplier(&symbol.code, &symbol.variety);
        println!(
            "{}: scanning {} raw5m / {} closed15m",
            symbol.code,
            raw.len(),
            bars.len()
        );
        let all = crate::analyze::event::research_warning_candidates(&symbol.code, &bars, tick);
        // Prefix replay checks candidate identity and all warning-time numeric fields,
        // not just whether the old-score winner remains the same.
        let mut prefix_audit = Vec::new();
        for pct in [50usize, 75] {
            let cut = bars.len() * pct / 100;
            if cut == 0 {
                continue;
            }
            let first = crate::analyze::event::research_warning_candidates(
                &symbol.code,
                &bars[..cut],
                tick,
            );
            let frozen: BTreeMap<_, _> = all
                .iter()
                .filter(|c| c.warning_index < cut)
                .map(|c| (signature(c), c))
                .collect();
            ensure!(
                frozen.len() == first.len(),
                "prefix population changed for {}",
                symbol.code
            );
            for c in &first {
                let orig = frozen
                    .get(&signature(c))
                    .context("prefix candidate disappeared")?;
                ensure!(
                    (orig.entry_score - c.entry_score).abs() < 1e-9
                        && orig.entry == c.entry
                        && orig.stop == c.stop,
                    "future changed warning snapshot"
                );
            }
            prefix_audit.push(json!({"cut15m":cut,"candidates":first.len(),"mismatches":0}));
        }
        let mut seen = HashSet::new();
        let mut statuses = BTreeMap::<String, usize>::new();
        let mut retained = 0;
        let mut priced = 0;
        for c in &all {
            let exact = format!(
                "{:?}|{}|{}|{}",
                c.direction, c.s0_index, c.s1_index, c.s2_index
            );
            if !seen.insert(exact) {
                continue;
            }
            id += 1;
            retained += 1;
            let e = model(&symbol.code, &bars, c, id)?;
            let quality = super::evaluate_with_research_calendar(
                &e,
                &bars,
                tick,
                raw.last().unwrap().datetime.as_str(),
                "historical_rescan",
                &rules,
                if matches!(symbol.code.as_str(), "SF0" | "SM0") {
                    Some(crate::session::spec::NightSessionType::None)
                } else {
                    None
                },
            );
            *statuses.entry(quality.status.clone()).or_default() += 1;
            let mut scores = Value::Null;
            let mut evidence = Value::Null;
            let mut evaluations = BTreeMap::new();
            if quality.status == "ok" {
                let payload = super::blind::case(&e, &bars, &quality, &rules);
                let w = payload["bars"].as_array().unwrap().len() - 1;
                let row = AuditRow {
                    event_id: id,
                    symbol: symbol.code.clone(),
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
                    Some(&row),
                    Some(tick / quality.features["atr_s1"].unwrap()),
                )?;
                scores = candidate_scores(&quality, &evidence, &profile).unwrap_or(Value::Null);
                let ev = replay(&e, tick);
                let mut config = crate::trader::ExecutionConfig::default();
                config.min_entry_risk_ticks = 0;
                for (name, policy) in [("fixed_1r", Policy::FixedOneR), ("s1", Policy::OriginalS1)]
                {
                    for slip in [0., 1., 2.] {
                        let ex = Experiment {
                            policy,
                            no_follow_bar: Some(crate::analyze::outcome::NO_FOLLOW_BAR),
                            slip_ticks: slip,
                            fee_ticks: 0.,
                        };
                        let result = ablation::evaluate_research(
                            &ev,
                            &raw,
                            &rolls,
                            &config,
                            ex,
                            multiplier,
                            None,
                            ResearchRules {
                                disable_admission_for_fixed_policy: true,
                                ..Default::default()
                            },
                        );
                        if name == "fixed_1r" && slip == 1. && result.evaluation.status == "closed"
                        {
                            priced += 1;
                        }
                        evaluations
                            .insert(format!("{name}_{slip}tick"), serde_json::to_value(result)?);
                    }
                }
            }
            let value = json!({"id":id,"candidate_key":super::event_key(&e),"symbol":symbol.code,"variety":symbol.variety,"multiplier":multiplier,"tick":tick,
                "reserved_until":bars[(c.warning_index+crate::analyze::outcome::PENDING_BARS+crate::analyze::outcome::TIME_HORIZON_BARS).min(bars.len()-1)].dt.to_bar_ts(),
                "event":e,"quality":quality,"scores":scores,"evidence":evidence,"evaluations":evaluations});
            serde_json::to_writer(&mut writer, &value)?;
            writer.write_all(b"\n")?;
        }
        writer.flush()?;
        println!(
            "{}: {} raw warning candidates -> {} unique structures, {} completed fixed1R/1tick",
            symbol.code,
            all.len(),
            retained,
            priced
        );
        coverage.push(json!({"symbol":symbol.code,"raw5m":raw.len(),"closed15m":bars.len(),"first":raw.first().unwrap().datetime,"last":raw.last().unwrap().datetime,"raw_candidates":all.len(),"unique_structures":retained,"quality_coverage":statuses,"prefix_audit":prefix_audit}));
    }
    writer.flush()?;
    drop(writer);
    db.close().await?;
    ensure!(
        hash(&std::fs::read(&db_path)?) == hash(&db_bytes),
        "read-only snapshot changed"
    );
    write_json(
        &out.join("manifest.json"),
        &json!({"protocol":protocol,"coverage":coverage,"unique_structures":id,"signals_sha256":hash(&std::fs::read(out.join("signals.jsonl"))?),"finished_at":chrono::Utc::now().to_rfc3339(),"source_unchanged":true}),
    )?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn q() -> SignalQualityDto {
        serde_json::from_value(json!({"event_id":1,"event_key":"","warning_ts":"","timeframe":"15m","feature_version":"v1","rules_version":"v1","parameter_hash":"","input_hash":"","origin":"test","status":"ok","trend_score":70.,"a_score":80.,"b_score":80.,"space_score":50.,"morphology_score":78.,"quality_score":72.4,"features":{},"contributions":[],"explanations":[],"flags":[]})).unwrap()
    }
    fn d(concentration: f64, attempts: f64) -> Value {
        json!({"modules":{"a_process":{"metrics":{"amplitude_atr":4.,"directional_body":{"largest_share":concentration},"closing_records":{"largest_share":concentration}}},"b_process":{"metrics":{"bars_without_warning":3,"reverse_body_bars":attempts,"reverse_close_steps":attempts,"max_range_retreat_from_s1_close_atr":1.,"max_close_retreat_from_s1_close_atr":0.8}},"levels":{"metrics":{"nearest_forward_obstacle_r":1.}}}})
    }
    #[test]
    fn frozen_candidate_separates_concentration_and_actual_pullback_without_endpoint_penalties() {
        let p: Profile = serde_json::from_str(PROFILE).unwrap();
        p.validate().unwrap();
        let q = q();
        let a = candidate_scores(&q, &d(0.2, 3.), &p).unwrap();
        let b = candidate_scores(&q, &d(1., 1.), &p).unwrap();
        assert!(a["quality_v2"].as_f64().unwrap() > b["quality_v2"].as_f64().unwrap());
        let mut unknown = d(0.2, 3.);
        unknown["modules"]["levels"]["metrics"]["nearest_forward_obstacle_r"] = Value::Null;
        assert_eq!(
            candidate_scores(&q, &unknown, &p).unwrap()["levels_partial"],
            true
        );
        unknown["modules"]["a_process"]["metrics"]["closing_records"]["largest_share"] =
            Value::Null;
        assert!(candidate_scores(&q, &unknown, &p).is_none());
        let mut future = d(0.2, 3.);
        future["future_win"] = json!(1);
        future["modules"]["warning"] = json!({"arbitrary_wick":1.});
        assert_eq!(a, candidate_scores(&q, &future, &p).unwrap());
    }
}
