use super::*;
use serde_json::Value;

pub(super) fn fixtures() -> Vec<(pattern_events::Model, Vec<Bar>, f64)> {
    let root: Value = serde_json::from_str(include_str!("fixtures/four-signals.json")).unwrap();
    root["cases"]
        .as_array()
        .unwrap()
        .iter()
        .map(|c| {
            let event = serde_json::from_value(c["event"].clone()).unwrap();
            let bars = c["bars"]
                .as_array()
                .unwrap()
                .iter()
                .map(|r| Bar {
                    dt: crate::analyze::model::DT::from_bar_ts(r[0].as_str().unwrap()).unwrap(),
                    open: r[1].as_f64().unwrap(),
                    high: r[2].as_f64().unwrap(),
                    low: r[3].as_f64().unwrap(),
                    close: r[4].as_f64().unwrap(),
                    volume: r[5].as_f64().unwrap(),
                    hold: r[6].as_f64().unwrap(),
                    rollover: r[7].as_bool().unwrap(),
                })
                .collect();
            (event, bars, c["tick"].as_f64().unwrap())
        })
        .collect()
}
fn score(e: &pattern_events::Model, bs: &[Bar], tick: f64) -> SignalQualityDto {
    evaluate(
        e,
        bs,
        tick,
        &e.warning_ts,
        "historical_recompute",
        &Rules::default(),
    )
}
fn path_bars(values: &[f64]) -> Vec<Bar> {
    let template = fixtures().remove(0).1[0].clone();
    values
        .iter()
        .map(|v| Bar {
            open: v - 0.1,
            high: v + 0.5,
            low: v - 0.5,
            close: *v,
            ..template.clone()
        })
        .collect()
}
fn path_quality(p: &Path) -> f64 {
    weighted(
        &[
            p.efficiency,
            down(p.drawdown, [0.1, 0.4]),
            down(p.stall_fraction, [0.1, 0.5]),
            1. - p.overlap,
        ],
        &Rules::default().a_weights,
    )
}

#[test]
fn four_real_warning_snapshots_reproduce_legacy_and_explain_structure() {
    for (e, bars, tick) in fixtures() {
        let q = score(&e, &bars, tick);
        assert_eq!(q.status, "ok", "{} {:?}", e.id, q.explanations);
        let dims: Value = serde_json::from_str(&e.entry_score_dims).unwrap();
        for (key, old) in [
            ("legacy_a_score", "dim_a"),
            ("legacy_b_score", "dim_b"),
            ("legacy_warning_score", "dim_warning"),
        ] {
            assert!(
                (q.features[key].unwrap() - dims[old].as_f64().unwrap()).abs() < 1e-6,
                "{} {} new={:?} old={}",
                e.id,
                key,
                q.features[key],
                dims[old]
            );
        }
        assert!(q.explanations.iter().any(|s| s.contains("结构空间")));
        if e.id == 7068 {
            assert!(q.features["ma20_slope_a"].unwrap() > 0.);
            assert!(q.features["warning_close_ma_atr"].unwrap() < 0.);
        }
        if e.id == 7086 {
            assert!(q.features["a_terminal_stall_bars"].unwrap() >= 2.);
        }
        for s in [
            q.morphology_score,
            q.space_score,
            q.quality_score,
            q.trend_score,
            q.a_score,
            q.b_score,
        ] {
            assert!((0.0..=100.).contains(&s.unwrap()));
        }
    }
}

#[test]
fn a_process_separates_progress_from_round_trips_at_equal_span_and_length() {
    let clean = path(
        &path_bars(&[0., 1., 2., 3., 4., 5., 6.]),
        1.,
        0.1,
        6.,
        2.,
        0.4,
    );
    let dirty = path(
        &path_bars(&[0., 4., 1., 5., 2., 3., 6.]),
        1.,
        0.1,
        6.,
        2.,
        0.4,
    );
    assert!(path_quality(&clean) > path_quality(&dirty));
    assert!(clean.efficiency > dirty.efficiency && clean.drawdown < dirty.drawdown);
}
#[test]
fn overlap_with_steady_progress_is_better_than_overlap_stagnation() {
    let clean = path(
        &path_bars(&[0., 0.4, 0.8, 1.2, 1.6, 2.]),
        1.,
        0.1,
        2.,
        1.,
        0.4,
    );
    let flat = path(
        &path_bars(&[0., 0.1, 0., 0.1, 0., 0.1]),
        1.,
        0.1,
        2.,
        1.,
        0.4,
    );
    assert!(clean.overlap < flat.overlap && path_quality(&clean) > path_quality(&flat));
}
#[test]
fn ordering_of_same_steps_changes_drawdown_and_stall() {
    let a = path(
        &path_bars(&[0., 2., 1., 3., 2., 4., 3., 5.]),
        1.,
        0.1,
        5.,
        1.,
        0.4,
    );
    let b = path(
        &path_bars(&[0., 2., 4., 6., 8., 7., 6., 5.]),
        1.,
        0.1,
        5.,
        1.,
        0.4,
    );
    assert!((a.efficiency - b.efficiency).abs() < 1e-9);
    assert!(a.drawdown < b.drawdown && a.terminal_stall < b.terminal_stall);
    assert!(path_quality(&a) > path_quality(&b));
}
#[test]
fn future_and_outcome_mutations_never_change_warning_features_or_hash() {
    for (mut e, mut bars, tick) in fixtures() {
        let before = score(&e, &bars, tick);
        let mut future = bars.last().unwrap().clone();
        future.dt = future.dt.add_minutes(15);
        future.high = f64::NAN;
        bars.push(future);
        e.state = "closed".into();
        e.outcome = Some("win".into());
        e.r_multiple = Some(100.);
        e.trigger_volume_ratio = Some(500.);
        let after = score(&e, &bars, tick);
        assert_eq!(before, after);
    }
}
#[test]
fn core_endpoint_separation_does_not_cherry_pick() {
    let (e, mut bs, tick) = fixtures().remove(0);
    let before = score(&e, &bs, tick);
    let i = bs.iter().position(|b| b.dt.to_bar_ts() == e.s1_ts).unwrap();
    bs[i].close = bs[i].low;
    let after = score(&e, &bs, tick);
    for k in [
        "a_core_efficiency",
        "a_drawdown_ratio",
        "a_stall_fraction",
        "a_overlap_stagnation",
    ] {
        assert_eq!(before.features[k], after.features[k]);
    }
    assert_ne!(before.input_hash, after.input_hash);
}
#[test]
fn mirror_translation_and_scaling_preserve_scores() {
    for (e, bs, tick) in fixtures() {
        let original = score(&e, &bs, tick);
        for (scale, offset, mirror) in [(3., 2000., false), (1., 40000., true)] {
            let map = |x: f64| {
                if mirror {
                    offset - scale * x
                } else {
                    offset + scale * x
                }
            };
            let mut ev = e.clone();
            ev.s0_price = map(e.s0_price);
            ev.s1_price = map(e.s1_price);
            ev.s2_price = map(e.s2_price);
            ev.entry = map(e.entry);
            ev.stop = map(e.stop);
            ev.target = map(e.target);
            ev.a_move *= scale;
            ev.b_move *= scale;
            ev.risk *= scale;
            if mirror {
                ev.direction = if e.direction == "up" { "down" } else { "up" }.into();
            }
            let mapped: Vec<_> = bs
                .iter()
                .map(|b| Bar {
                    open: map(b.open),
                    close: map(b.close),
                    high: map(if mirror { b.low } else { b.high }),
                    low: map(if mirror { b.high } else { b.low }),
                    ..b.clone()
                })
                .collect();
            let new = score(&ev, &mapped, tick * scale);
            assert_eq!(new.status, "ok");
            for (a, b) in [
                (original.quality_score, new.quality_score),
                (original.a_score, new.a_score),
                (original.b_score, new.b_score),
            ] {
                assert!(
                    (a.unwrap() - b.unwrap()).abs() < 1e-7,
                    "{} {:?} {:?}",
                    e.id,
                    a,
                    b
                );
            }
        }
    }
}
#[test]
fn bad_inputs_are_explicit_and_never_fake_full_scores() {
    let (e, bs, tick) = fixtures().remove(0);
    assert_eq!(score(&e, &bs, 0.).status, "missing_tick");
    assert_eq!(
        evaluate(
            &e,
            &bs,
            tick,
            &e.s1_ts,
            "historical_recompute",
            &Rules::default()
        )
        .status,
        "partial_warning"
    );
    let i = bs.iter().position(|b| b.dt.to_bar_ts() == e.s0_ts).unwrap();
    let q = score(&e, &bs[i - 20..], tick);
    assert_eq!(q.status, "insufficient_history");
    assert_eq!(q.quality_score, None);
    let mut roll = bs.clone();
    roll[i].rollover = true;
    assert_eq!(score(&e, &roll, tick).status, "rollover");
    let mut missing = bs.clone();
    missing.remove(i + 2);
    assert_eq!(score(&e, &missing, tick).status, "missing_data");
    let mut zero = bs.clone();
    for b in &mut zero {
        b.open = 100.;
        b.high = 100.;
        b.low = 100.;
        b.close = 100.;
    }
    assert_eq!(score(&e, &zero, tick).status, "invalid_atr");
}
#[test]
fn maps_are_continuous_including_old_amplitude_boundary() {
    let r = Rules::default();
    for b in [
        r.slope_bounds,
        r.drawdown_bounds,
        r.stall_bounds,
        r.amplitude_bounds,
        r.retracement_bounds,
        r.relative_speed_bounds,
        r.departure_bounds,
        r.proximity_bounds,
        r.damage_bounds,
        r.cross_bounds,
        r.confirmation_bounds,
    ] {
        for x in [b[0], b[1], 1.5] {
            assert!((up(x - 1e-7, b) - up(x + 1e-7, b)).abs() < 1e-5);
        }
    }
    assert!(up(1.500001, r.amplitude_bounds) >= up(1.499999, r.amplitude_bounds));
}
#[test]
fn noisy_crossing_uses_hysteresis_and_penalizes_repeated_crosses() {
    assert_eq!(crosses([0.2, 0.01, -0.01, 0.3].into_iter(), 0.1), 0);
    assert_eq!(crosses([0.2, -0.3, 0.01, 0.3, -0.4].into_iter(), 0.1), 3);
    let r = Rules::default();
    assert!(down(1., r.cross_bounds) > down(3., r.cross_bounds));
}

#[tokio::test]
async fn sidecar_is_immutable_generation_scoped_and_live_frozen_wins() {
    use sea_orm::{ConnectionTrait, DbBackend, Statement};
    let db = crate::storage::connect(std::path::Path::new(":memory:"))
        .await
        .unwrap();
    let (e, bs, tick) = fixtures().remove(0);
    let q = score(&e, &bs, tick);
    store::save(&db, &q).await.unwrap();
    store::save(&db, &q).await.unwrap();
    let mut live = q.clone();
    live.origin = "live_frozen".into();
    live.quality_score = Some(71.);
    store::save(&db, &live).await.unwrap();
    let mut revised_live = live.clone();
    revised_live.input_hash = "revised-live-data".into();
    revised_live.quality_score = Some(99.);
    store::save(&db, &revised_live).await.unwrap();
    let mut later = q.clone();
    later.input_hash = "revised-data".into();
    later.quality_score = Some(99.);
    store::save(&db, &later).await.unwrap();
    let found = store::latest(&db, &[e.clone()]).await.unwrap();
    assert_eq!(found[&e.id].quality_score, Some(71.));
    let n = db
        .query_one(Statement::from_string(
            DbBackend::Sqlite,
            "SELECT count(*) n FROM signal_quality_evaluations".to_string(),
        ))
        .await
        .unwrap()
        .unwrap()
        .try_get::<i64>("", "n")
        .unwrap();
    assert_eq!(n, 4);
    crate::storage::repo::set_settings(
        &db,
        &[(
            "signal_generation".to_string(),
            "other-generation".to_string(),
        )]
        .into_iter()
        .collect(),
    )
    .await
    .unwrap();
    assert!(store::latest(&db, &[e]).await.unwrap().is_empty());
}

#[tokio::test]
async fn warning_persistence_and_prediction_snapshot_are_identical_with_quality_on_or_off() {
    use crate::analyze::{event::WarningCandidate, model::Dir};
    let (e, bars, _) = fixtures().remove(0);
    let find = |ts: &str| bars.iter().position(|b| b.dt.to_bar_ts() == ts).unwrap();
    let dims: Value = serde_json::from_str(&e.entry_score_dims).unwrap();
    let candidate = WarningCandidate {
        direction: Dir::Up,
        grade: e.grade.clone(),
        level: "large",
        s0_index: find(&e.s0_ts),
        s1_index: find(&e.s1_ts),
        s2_index: find(&e.s2_ts),
        a_move: e.a_move,
        b_move: e.b_move,
        a_bars: e.a_bars as usize,
        b_bars: e.b_bars as usize,
        retracement: e.retracement,
        warning_index: find(&e.warning_ts),
        warning_kind: "strong",
        entry_score: e.entry_score,
        dim_a: dims["dim_a"].as_f64().unwrap(),
        dim_b: dims["dim_b"].as_f64().unwrap(),
        dim_warning: dims["dim_warning"].as_f64().unwrap(),
        entry: e.entry,
        stop: e.stop,
        target: e.target,
        risk: e.risk,
        rr: e.rr,
        trend_state: "WEAK_UP".into(),
        trend_bonus: 0.,
    };
    let mut results = Vec::new();
    for enabled in [false, true] {
        let db = crate::storage::connect(std::path::Path::new(":memory:"))
            .await
            .unwrap();
        crate::storage::repo::set_settings(
            &db,
            &[("signal_quality_enabled".into(), enabled.to_string())]
                .into_iter()
                .collect(),
        )
        .await
        .unwrap();
        let row = crate::service::insert_warning_event(&db, &e.symbol, &bars, &candidate)
            .await
            .unwrap();
        let mut data = serde_json::to_value(&row).unwrap();
        data.as_object_mut().unwrap().remove("created_at");
        data.as_object_mut().unwrap().remove("updated_at");
        use sea_orm::{ConnectionTrait, DbBackend, Statement};
        let snapshot = db
            .query_one(Statement::from_sql_and_values(
                DbBackend::Sqlite,
                "SELECT feature_hash,features_json FROM signal_snapshots WHERE event_id=?",
                vec![row.id.into()],
            ))
            .await
            .unwrap()
            .unwrap();
        let feature_hash: String = snapshot.try_get("", "feature_hash").unwrap();
        let features: String = snapshot.try_get("", "features_json").unwrap();
        let saved = store::latest(&db, &[row]).await.unwrap();
        assert_eq!(saved.len(), usize::from(enabled));
        results.push((data, feature_hash, features));
    }
    assert_eq!(results[0], results[1]);
}

#[test]
fn decay_ignores_warning_candle_and_retest_distinguishes_one_touch_from_whipsaw() {
    let (e, mut bars, tick) = fixtures().remove(0);
    let before = score(&e, &bars, tick);
    let w = bars.len() - 1;
    bars[w].open = bars[w].low;
    bars[w].close = bars[w].high;
    let after = score(&e, &bars, tick);
    assert_eq!(before.features["b_decay"], after.features["b_decay"]);
    assert_ne!(
        before.features["warning_close_location"],
        after.features["warning_close_location"]
    );
    let r = Rules::default();
    let hold = down(0.1, r.damage_bounds);
    assert!(hold * down(1., r.cross_bounds) > hold * down(3., r.cross_bounds));
}

#[test]
fn blind_export_has_no_future_scores_or_outcomes_and_import_checks_identity() {
    use super::research::AuditRow;
    let folder = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../target/quality-tests")
        .join(uuid::Uuid::new_v4().to_string());
    std::fs::create_dir_all(&folder).unwrap();
    let mut rows = Vec::new();
    let mut cases = std::collections::HashMap::new();
    for (e, bars, tick) in fixtures() {
        let q = score(&e, &bars, tick);
        let case = blind::case(&e, &bars, &q, &Rules::default());
        let w = bars
            .iter()
            .position(|b| b.dt.to_bar_ts() == e.warning_ts)
            .unwrap();
        let s0 = bars
            .iter()
            .position(|b| b.dt.to_bar_ts() == e.s0_ts)
            .unwrap();
        assert_eq!(case["bars"].as_array().unwrap().len(), w - s0 + 41);
        cases.insert(e.id, case);
        rows.push(AuditRow {
            event_id: e.id,
            symbol: e.symbol,
            direction: e.direction,
            grade: e.grade,
            level: e.level,
            warning_ts: e.warning_ts,
            s0_ts: e.s0_ts,
            s1_ts: e.s1_ts,
            old_score: e.entry_score,
            old_dims: Value::Null,
            quality: q,
            legacy_recomputed: None,
            legacy_max_error: None,
            evaluations: Default::default(),
        });
    }
    blind::export(&folder, &rows, &cases).unwrap();
    let manifest_path = folder.join("blind-manifest.json");
    let manifest: Value = serde_json::from_slice(&std::fs::read(&manifest_path).unwrap()).unwrap();
    let raw = serde_json::to_string(&manifest).unwrap();
    for forbidden in [
        "quality_score",
        "old_score",
        "morphology_score",
        "outcome",
        "exit_price",
        "r_multiple",
        "warning_ts",
    ] {
        assert!(!raw.contains(forbidden));
    }
    let pair = &manifest["pairs"][0];
    assert!(pair.is_object());
    let label = serde_json::json!({"pair_id":pair["pair_id"],"left_case_id":pair["left"]["case_id"],"right_case_id":pair["right"]["case_id"],"left_input_hash":pair["left"]["input_hash"],"right_input_hash":pair["right"]["input_hash"],"morphology":"left","opportunity":"tie","reasons":["a"],"note":"小K持续推进"});
    let mut labels = serde_json::json!({"manifest_hash":manifest["manifest_hash"],"display_version":manifest["display_version"],"labels":[label]});
    let label_path = folder.join("labels.json");
    std::fs::write(&label_path, serde_json::to_vec(&labels).unwrap()).unwrap();
    let args = vec![
        "signal-quality".into(),
        "blind-import".into(),
        "--manifest".into(),
        manifest_path.to_string_lossy().to_string(),
        "--labels".into(),
        label_path.to_string_lossy().to_string(),
    ];
    let accepted = folder.join("accepted");
    std::fs::create_dir(&accepted).unwrap();
    blind::import(&args, &accepted).unwrap();
    let imported: Value =
        serde_json::from_slice(&std::fs::read(accepted.join("preferences.json")).unwrap()).unwrap();
    assert_eq!(
        imported["preferences"][0]["calibration_group"],
        "legacy_unrecorded"
    );
    let notes =
        super::assistant_review::load(&folder.join("assistant-notes.json"), &manifest).unwrap();
    // The companion prose depends only on warning-time raw features, never outcome or total scores.
    let mut changed = rows.clone();
    for r in &mut changed {
        r.old_score = 999.;
        r.quality.quality_score = Some(0.);
        r.quality.morphology_score = Some(100.);
        r.evaluations
            .insert("future_profit".into(), serde_json::json!({"net_r":999.}));
    }
    assert_eq!(
        notes,
        super::assistant_review::build(&manifest, &changed).unwrap()
    );
    labels["labels"][0]["assistant_reference"] = serde_json::json!({"viewed":true,"version":notes["version"],"notes_hash":notes["notes_hash"],"opened_at":"2026-10-29T09:20:00Z","before":{"morphology":"right","opportunity":"","reasons":["a"],"note":"先写下的独立判断"}});
    std::fs::write(&label_path, serde_json::to_vec(&labels).unwrap()).unwrap();
    let referenced = folder.join("referenced");
    std::fs::create_dir(&referenced).unwrap();
    blind::import(&args, &referenced).unwrap();
    let imported: Value =
        serde_json::from_slice(&std::fs::read(referenced.join("preferences.json")).unwrap())
            .unwrap();
    assert_eq!(
        imported["preferences"][0]["calibration_group"],
        "reference_viewed"
    );
    assert_eq!(
        imported["preferences"][0]["label"]["assistant_reference"]["before"]["note"],
        "先写下的独立判断"
    );
    labels["labels"][0]["assistant_reference"]["notes_hash"] = serde_json::json!("wrong-version");
    std::fs::write(&label_path, serde_json::to_vec(&labels).unwrap()).unwrap();
    assert!(blind::import(&args, &referenced)
        .unwrap_err()
        .to_string()
        .contains("reference version"));
    labels["labels"][0]["left_input_hash"] = serde_json::json!("stale");
    std::fs::write(&label_path, serde_json::to_vec(&labels).unwrap()).unwrap();
    assert!(blind::import(&args, &accepted).is_err());
}
