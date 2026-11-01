use super::*;
use crate::{fetch::kline::Kline, storage::entities::pattern_events};
use replay::{run, CalendarInput};
use std::collections::{BTreeMap, HashSet};

fn event() -> pattern_events::Model {
    let fixture: serde_json::Value =
        serde_json::from_str(include_str!("../quality/fixtures/four-signals.json")).unwrap();
    let mut e: pattern_events::Model =
        serde_json::from_value(fixture["cases"][0]["event"].clone()).unwrap();
    e.id = 1;
    e.symbol = "RB0".into();
    e.direction = "up".into();
    e.warning_ts = "2026-01-05 09:45:00".into();
    e.detected_at = e.warning_ts.clone();
    e.entry = 100.;
    e.stop = 90.;
    e.risk = 10.;
    e.target = 130.;
    e.entry_score = 4.;
    e
}
fn bar(time: &str, o: f64, h: f64, l: f64, c: f64) -> Kline {
    Kline {
        datetime: format!("2026-01-05 {time}:00"),
        open: o,
        high: h,
        low: l,
        close: c,
        volume: 100.,
        hold: 1000.,
    }
}
fn calendar() -> CalendarInput {
    CalendarInput {
        days: BTreeMap::from([("2026-01-05".into(), true), ("2026-01-06".into(), true)]),
    }
}
fn replay(e: &pattern_events::Model, bars: &[Kline]) -> replay::ReplayOutput {
    run(
        e,
        bars,
        &HashSet::new(),
        &calendar(),
        Some(1.),
        Some(10.),
        "historical_rebuild",
        "2026-01-06T00:00:00Z",
    )
}

#[test]
fn one_lot_really_closes_at_one_r_and_does_not_double_charge_slippage() {
    let e = event();
    let before = serde_json::to_value(&e).unwrap();
    let r = replay(
        &e,
        &[
            bar("09:45", 95., 99., 94., 98.),
            bar("09:50", 100., 105., 99., 104.),
            bar("09:55", 105., 115., 104., 114.),
        ],
    );
    assert_eq!(r.summary.state, "closed");
    assert_eq!(r.summary.exit_reason.as_deref(), Some("target_1r"));
    assert_eq!(r.entry_price, Some(101.));
    assert_eq!(r.exit_price, Some(111.));
    assert_eq!(r.summary.gross_amount, Some(100.));
    assert_eq!(r.summary.net_amount, Some(95.));
    assert_eq!(r.summary.risk_amount, Some(110.));
    assert_eq!(r.mfe.r, Some(1.));
    assert_eq!(r.holding.len(), 2);
    assert_eq!(serde_json::to_value(&e).unwrap(), before);
}
#[test]
fn double_touch_and_pre_entry_stop_are_ambiguous_without_inventing_pnl() {
    for (open, high, low) in [(100., 115., 89.), (95., 105., 89.)] {
        let r = replay(
            &event(),
            &[
                bar("09:45", 95., 99., 94., 98.),
                bar("09:50", open, high, low, 100.),
            ],
        );
        assert_eq!(r.summary.state, "ambiguous");
        assert_eq!(r.summary.net_amount, None);
        assert_eq!(r.summary.net_r, None);
    }
}
#[test]
fn missing_five_minute_candle_and_calendar_are_explicit() {
    let bars = [
        bar("09:45", 95., 99., 94., 98.),
        bar("09:55", 100., 105., 99., 104.),
    ];
    assert_eq!(
        replay(&event(), &bars).summary.terminal_reason.as_deref(),
        Some("missing_data")
    );
    let r = run(
        &event(),
        &bars,
        &HashSet::new(),
        &CalendarInput::default(),
        Some(1.),
        Some(10.),
        "historical_rebuild",
        "",
    );
    assert_eq!(
        r.summary.terminal_reason.as_deref(),
        Some("missing_calendar")
    );
}
#[test]
fn unknown_multiplier_never_becomes_a_default_currency_amount() {
    let bars = [
        bar("09:45", 95., 99., 94., 98.),
        bar("09:50", 100., 105., 99., 104.),
        bar("09:55", 105., 115., 104., 114.),
    ];
    let r = run(
        &event(),
        &bars,
        &HashSet::new(),
        &calendar(),
        Some(1.),
        None,
        "historical_rebuild",
        "",
    );
    assert_eq!(r.summary.net_amount, None);
    assert!(r.summary.net_r.is_some());
    assert!(r.summary.coverage.contains(&"missing_multiplier".into()));
}
#[test]
fn score_time_and_multi_session_validation_reject_invalid_queries() {
    let mut q = ReviewQuery::default();
    q.filters.sessions = vec![ReviewSession {
        start: "21:00".into(),
        end: "02:30".into(),
    }];
    super::query::validate(&q).unwrap();
    q.filters.score_min = Some(101.);
    assert!(super::query::validate(&q).is_err());
    q.filters.score_min = None;
    q.sort_by = Some("DROP TABLE review_current".into());
    assert!(super::query::validate(&q).is_err());
}

#[test]
fn mirrored_short_has_identical_one_lot_r_and_money() {
    let e = event();
    let mut short = e.clone();
    short.direction = "down".into();
    short.stop = 110.;
    short.target = 70.;
    let long = [
        bar("09:45", 95., 99., 94., 98.),
        bar("09:50", 100., 105., 99., 104.),
        bar("09:55", 105., 115., 104., 114.),
    ];
    let short_bars: Vec<_> = long
        .iter()
        .map(|b| Kline {
            datetime: b.datetime.clone(),
            open: 200. - b.open,
            high: 200. - b.low,
            low: 200. - b.high,
            close: 200. - b.close,
            volume: b.volume,
            hold: b.hold,
        })
        .collect();
    let l = replay(&e, &long);
    let s = replay(&short, &short_bars);
    assert_eq!(l.summary.gross_r, s.summary.gross_r);
    assert_eq!(l.summary.net_amount, s.summary.net_amount);
    assert_eq!(s.exit_price, Some(89.));
}

#[test]
fn exit_bar_extrema_are_clipped_and_entry_bar_adverse_excursion_is_bounded() {
    let r = replay(
        &event(),
        &[
            bar("09:45", 95., 99., 94., 98.),
            bar("09:50", 95., 105., 94., 104.),
            bar("09:55", 105., 150., 103., 140.),
        ],
    );
    assert_eq!(r.summary.exit_reason.as_deref(), Some("target_1r"));
    assert_eq!(r.mfe.r, Some(1.));
    assert_eq!(r.mae.precision, "bounded");
    assert_eq!(r.mae.r, None);
    assert!(r.mae.lower_r.unwrap() < r.mae.upper_r.unwrap());
}

#[test]
fn admission_and_rollover_are_not_loss_exits() {
    let mut e = event();
    e.entry_score = 2.4;
    assert_eq!(replay(&e, &[]).summary.state, "filtered");
    e.entry_score = 4.;
    e.risk = 9.;
    assert_eq!(replay(&e, &[]).summary.state, "filtered");
    e.risk = 10.;
    let bars = [
        bar("09:45", 95., 99., 94., 98.),
        bar("09:50", 100., 105., 99., 104.),
    ];
    let r = run(
        &e,
        &bars,
        &HashSet::from([bars[1].datetime.clone()]),
        &calendar(),
        Some(1.),
        Some(10.),
        "historical_rebuild",
        "",
    );
    assert_eq!(r.summary.terminal_reason.as_deref(), Some("rollover"));
    assert!(r.summary.exit_reason.is_none());
    assert!(r.summary.net_r.is_none());
}

#[test]
fn no_follow_counts_fifteen_minute_buckets_not_five_minute_candles() {
    use chrono::{Duration, NaiveDateTime};
    let start = NaiveDateTime::parse_from_str("2026-01-05 09:45:00", "%Y-%m-%d %H:%M:%S").unwrap();
    let mut bars = vec![bar("09:45", 95., 99., 94., 98.)];
    for i in 1..=18 {
        let dt = start + Duration::minutes(i * 5);
        if dt.format("%H:%M").to_string() > "10:15".to_string()
            && dt.format("%H:%M").to_string() <= "10:30".to_string()
        {
            continue;
        }
        bars.push(Kline {
            datetime: dt.format("%Y-%m-%d %H:%M:%S").to_string(),
            open: 100.,
            high: 103.,
            low: 99.,
            close: 102.,
            volume: 100.,
            hold: 1000.,
        });
    }
    let r = replay(&event(), &bars);
    assert_eq!(r.summary.exit_reason.as_deref(), Some("no_follow"));
    assert_eq!(r.summary.bars_held, 5);
    assert_eq!(r.summary.exit_ts.as_deref(), Some("2026-01-05 11:15:00"));
    assert!(r.holding.len() > 5, "must not exit after five 5m candles");
}

#[test]
fn waiting_expiry_and_time_exit_use_completed_trading_buckets_across_breaks() {
    use chrono::{Duration, NaiveDateTime};
    let mut cal = calendar();
    for day in 5..=9 {
        cal.days.insert(format!("2026-01-{day:02}"), true);
    }
    let start = NaiveDateTime::parse_from_str("2026-01-05 09:45:00", "%Y-%m-%d %H:%M:%S").unwrap();
    for waiting in [true, false] {
        let mut bars = vec![bar("09:45", 95., 99., 94., 98.)];
        for i in 1..=3 * 24 * 12 {
            let dt = start + Duration::minutes(i * 5);
            if cal.expected("RB0", dt) != Ok(true) {
                continue;
            }
            bars.push(Kline {
                datetime: dt.format("%Y-%m-%d %H:%M:%S").to_string(),
                open: if waiting { 95. } else { 100. },
                high: if waiting { 99. } else { 107. },
                low: 94.,
                close: if waiting { 98. } else { 103. },
                volume: 100.,
                hold: 1000.,
            });
        }
        let result = run(
            &event(),
            &bars,
            &HashSet::new(),
            &cal,
            Some(1.),
            Some(10.),
            "historical_rebuild",
            "",
        );
        if waiting {
            assert_eq!(
                result.summary.terminal_reason.as_deref(),
                Some("untriggered")
            );
            assert_eq!(result.summary.wait_bars, 12);
            assert!(result.holding.is_empty());
        } else {
            assert_eq!(result.summary.exit_reason.as_deref(), Some("time_exit"));
            assert_eq!(result.summary.bars_held, 60);
            assert!(result.holding.len() > 150);
        }
    }
}

#[test]
fn stop_gap_prices_at_open_and_friday_night_calendar_handles_weekends() {
    let result = replay(
        &event(),
        &[
            bar("09:45", 95., 99., 94., 98.),
            bar("09:50", 100., 105., 99., 104.),
            bar("09:55", 85., 88., 80., 84.),
        ],
    );
    assert_eq!(result.summary.exit_reason.as_deref(), Some("stop"));
    assert_eq!(result.exit_price, Some(84.));
    assert_eq!(result.summary.gross_amount, Some(-170.));
    assert_eq!(result.summary.net_amount, Some(-175.));
    let cal = CalendarInput {
        days: BTreeMap::from([
            ("2026-01-09".into(), true),
            ("2026-01-10".into(), false),
            ("2026-01-11".into(), false),
            ("2026-01-12".into(), true),
        ]),
    };
    let at =
        chrono::NaiveDateTime::parse_from_str("2026-01-10 01:00:00", "%Y-%m-%d %H:%M:%S").unwrap();
    assert_eq!(cal.expected("AU0", at), Ok(true));
    assert_eq!(cal.expected("RB0", at), Ok(false));
}

#[test]
fn gross_net_flat_denominators_and_zero_loss_profit_factor_are_explicit() {
    let rows = vec![
        ReviewSummary {
            state: "closed".into(),
            gross_r: Some(0.1),
            net_r: Some(-0.1),
            ..Default::default()
        },
        ReviewSummary {
            state: "closed".into(),
            gross_r: Some(0.),
            net_r: Some(0.),
            ..Default::default()
        },
        ReviewSummary {
            state: "ambiguous".into(),
            ..Default::default()
        },
    ];
    let stats = super::query::statistics(&rows);
    assert_eq!(stats.metrics["win_rate_gross_r"].value, Some(0.5));
    assert_eq!(stats.metrics["win_rate_net_r"].value, Some(0.));
    assert_eq!(stats.metrics["win_rate_net_r"].n, 2);
    assert_eq!(stats.metrics["profit_factor_gross_r"].value, None);
    assert_eq!(stats.metrics["avg_net_amount"].n, 0);
    assert_eq!(stats.metrics["avg_net_amount"].value, None);
}

async fn db() -> sea_orm::DatabaseConnection {
    crate::storage::connect(std::path::Path::new(":memory:"))
        .await
        .unwrap()
}

async fn seed(db: &sea_orm::DatabaseConnection, symbol: &str, id: i64) {
    use sea_orm::{EntityTrait, IntoActiveModel, Set};
    let mut e = event();
    e.id = id;
    e.symbol = symbol.into();
    pattern_events::Entity::insert(e.into_active_model())
        .exec(db)
        .await
        .unwrap();
    for b in [
        bar("09:45", 95., 99., 94., 98.),
        bar("09:50", 100., 105., 99., 104.),
        bar("09:55", 105., 115., 104., 114.),
    ] {
        crate::storage::entities::klines::Entity::insert(
            crate::storage::entities::klines::ActiveModel {
                symbol: Set(symbol.into()),
                timeframe: Set("5m".into()),
                ts: Set(b.datetime),
                open: Set(b.open),
                high: Set(b.high),
                low: Set(b.low),
                close: Set(b.close),
                volume: Set(b.volume),
                hold: Set(b.hold),
                source: Set("raw".into()),
            },
        )
        .exec(db)
        .await
        .unwrap();
    }
}

async fn install_job_calendar(db: &sea_orm::DatabaseConnection, id: &str) {
    use sea_orm::ConnectionTrait;
    db.execute(store::sql(
        "UPDATE review_jobs SET calendar_json=? WHERE id=?",
        vec![
            serde_json::to_string(&calendar()).unwrap().into(),
            id.into(),
        ],
    ))
    .await
    .unwrap();
}

#[tokio::test]
async fn transactional_outbox_atomic_publication_resume_and_immutable_details() {
    use sea_orm::ConnectionTrait;
    let db = db().await;
    seed(&db, "RB0", 1).await;
    seed(&db, "SA0", 2).await;
    let engine = ReviewEngine::new(db.clone());
    let j = engine
        .rebuild(ReviewRebuildRequest::default())
        .await
        .unwrap();
    install_job_calendar(&db, &j.id).await;
    let first = engine.worker_step().await.unwrap().unwrap();
    assert_eq!(first.status, "running");
    assert_eq!(store::revision(&db).await.unwrap(), 0);
    assert_eq!(
        engine
            .query("device", ReviewQuery::default())
            .await
            .unwrap()
            .page
            .total,
        0
    );
    let finished = engine.worker_step().await.unwrap().unwrap();
    assert_eq!(finished.status, "completed");
    let initial = engine
        .query("device", ReviewQuery::default())
        .await
        .unwrap();
    assert_eq!(initial.page.total, 2);
    let old = initial
        .page
        .items
        .iter()
        .find(|r| r.symbol == "RB0")
        .unwrap()
        .clone();
    let detail = engine
        .detail(old.event_id, &old.result_version)
        .await
        .unwrap();
    assert_eq!(detail.summary.net_amount, Some(95.));
    assert_eq!(detail.origin, "historical_rebuild");
    assert_eq!(detail.holding_count, 2);
    assert!(detail.lifecycle.iter().any(|n| n.stage == "exit"));
    let holding = engine.holding(1, &old.result_version, 0, 1).await.unwrap();
    assert_eq!(holding.next_cursor, Some(1));
    let original_before = db
        .query_one(store::sql(
            "SELECT entry_score_dims FROM pattern_events WHERE id=1",
            vec![],
        ))
        .await
        .unwrap()
        .unwrap()
        .try_get::<String>("", "entry_score_dims")
        .unwrap();
    db.execute(store::sql(
        "UPDATE klines SET high=110,close=109 WHERE symbol='RB0' AND ts='2026-01-05 09:55:00'",
        vec![],
    ))
    .await
    .unwrap();
    let j = engine
        .rebuild(ReviewRebuildRequest::default())
        .await
        .unwrap();
    install_job_calendar(&db, &j.id).await;
    engine.worker_step().await.unwrap();
    assert_eq!(
        engine
            .query("device", ReviewQuery::default())
            .await
            .unwrap()
            .page
            .revision,
        1
    );
    // Simulated process restart: running job resumes at its durable next symbol.
    let resumed = ReviewEngine::new(db.clone());
    let j = resumed.worker_step().await.unwrap().unwrap();
    assert_eq!(j.status, "completed");
    assert_eq!(j.completed_symbols, 2);
    let new = resumed
        .query("device", ReviewQuery::default())
        .await
        .unwrap();
    assert_eq!(new.page.revision, 2);
    assert_eq!(
        new.page
            .items
            .iter()
            .find(|r| r.symbol == "RB0")
            .unwrap()
            .state,
        "open"
    );
    assert_eq!(
        engine
            .items(
                "device",
                &initial.page.snapshot_id,
                ReviewPageRequest::default()
            )
            .await
            .unwrap()
            .items
            .iter()
            .find(|r| r.symbol == "RB0")
            .unwrap()
            .state,
        "closed"
    );
    assert_eq!(
        resumed
            .detail(1, &old.result_version)
            .await
            .unwrap()
            .summary
            .net_amount,
        Some(95.)
    );
    assert!(engine
        .items(
            "another_device",
            &initial.page.snapshot_id,
            ReviewPageRequest::default()
        )
        .await
        .is_err());
    let before = db
        .query_one(store::sql(
            "SELECT count(*) AS n FROM review_results",
            vec![],
        ))
        .await
        .unwrap()
        .unwrap()
        .try_get::<i64>("", "n")
        .unwrap();
    let j = resumed
        .rebuild(ReviewRebuildRequest::default())
        .await
        .unwrap();
    install_job_calendar(&db, &j.id).await;
    resumed.worker_step().await.unwrap();
    resumed.worker_step().await.unwrap();
    let after = db
        .query_one(store::sql(
            "SELECT count(*) AS n FROM review_results",
            vec![],
        ))
        .await
        .unwrap()
        .unwrap()
        .try_get::<i64>("", "n")
        .unwrap();
    assert_eq!(
        before, after,
        "identical replay must reuse immutable artifacts"
    );
    assert_eq!(
        original_before,
        db.query_one(store::sql(
            "SELECT entry_score_dims FROM pattern_events WHERE id=1",
            vec![]
        ))
        .await
        .unwrap()
        .unwrap()
        .try_get::<String>("", "entry_score_dims")
        .unwrap()
    );
}

#[tokio::test]
async fn rollback_does_not_enqueue_or_leave_a_partial_signal() {
    use sea_orm::{ConnectionTrait, TransactionTrait};
    let db = db().await;
    seed(&db, "RB0", 1).await;
    db.execute(store::sql("DELETE FROM review_dirty", vec![]))
        .await
        .unwrap();
    let tx = db.begin().await.unwrap();
    tx.execute(store::sql(
        "UPDATE klines SET close=103 WHERE symbol='RB0' AND ts='2026-01-05 09:50:00'",
        vec![],
    ))
    .await
    .unwrap();
    assert_eq!(
        tx.query_one(store::sql("SELECT count(*) AS n FROM review_dirty", vec![]))
            .await
            .unwrap()
            .unwrap()
            .try_get::<i64>("", "n")
            .unwrap(),
        1
    );
    tx.rollback().await.unwrap();
    assert_eq!(
        db.query_one(store::sql("SELECT count(*) AS n FROM review_dirty", vec![]))
            .await
            .unwrap()
            .unwrap()
            .try_get::<i64>("", "n")
            .unwrap(),
        0
    );
}

async fn seed_summaries(db: &sea_orm::DatabaseConnection, n: usize) -> Vec<ReviewSummary> {
    use chrono::{Duration, NaiveDateTime};
    use sea_orm::{ConnectionTrait, TransactionTrait};
    let start = NaiveDateTime::parse_from_str("2026-01-05 09:00:00", "%Y-%m-%d %H:%M:%S").unwrap();
    let mut summaries = vec![];
    for i in 0..n {
        let dt = start + Duration::minutes(i as i64 * 5);
        let state = if i % 11 == 0 { "ambiguous" } else { "closed" };
        let gross = if i % 2 == 0 { 100. } else { -120. };
        let net = gross - 5.;
        let score = (i % 17 != 0).then_some(69. + (i % 32) as f64);
        let mut r = ReviewSummary {
            event_id: i as i64 + 1,
            result_version: format!("fixture-{i}"),
            family_id: ((i / 4) * 4 + 1) as i64,
            family_size: 4,
            symbol: format!("P{:02}", (i / 4) % 23),
            direction: if i % 2 == 0 { "up" } else { "down" }.into(),
            level: "fine".into(),
            grade: "A级".into(),
            warning_ts: dt.format("%Y-%m-%d %H:%M:%S").to_string(),
            entry_ts: Some(
                (dt + Duration::minutes(5))
                    .format("%Y-%m-%d %H:%M:%S")
                    .to_string(),
            ),
            exit_ts: (state == "closed").then(|| {
                (dt + Duration::minutes(10))
                    .format("%Y-%m-%d %H:%M:%S")
                    .to_string()
            }),
            state: state.into(),
            exit_reason: (state == "closed")
                .then(|| if net > 0. { "target_1r" } else { "stop" }.into()),
            terminal_reason: (state != "closed").then(|| "ambiguous".into()),
            scores: BTreeMap::from([
                ("old".into(), Some(4.)),
                ("quality_v2".into(), score),
                ("a_score".into(), score),
                ("b_score".into(), score),
            ]),
            score_status: if score.is_some() {
                "ok"
            } else {
                "missing_data"
            }
            .into(),
            risk_points: Some(11.),
            risk_amount: Some(110.),
            gross_amount: (state == "closed").then_some(gross),
            net_amount: (state == "closed").then_some(net),
            gross_r: (state == "closed").then_some(gross / 110.),
            net_r: (state == "closed").then_some(net / 110.),
            mfe_r: Some(1.),
            mae_r: Some(-0.5),
            mfe_amount: Some(110.),
            mae_amount: Some(-55.),
            bars_held: 2,
            wait_bars: 1,
            coverage: vec![],
            diagnostics: BTreeMap::new(),
        };
        for key in [
            "retracement",
            "b_a_speed",
            "vol_ratio",
            "overshoot",
            "dim_a",
            "dim_b",
            "dim_warning",
            "gap_entry",
            "gap_exit",
            "trigger_lag",
        ] {
            r.diagnostics.insert(key.into(), Some(0.5));
        }
        summaries.push(r);
    }
    let tx = db.begin().await.unwrap();
    for chunk in summaries.chunks(50) {
        let mut values = vec![];
        for s in chunk {
            values.extend([
                s.result_version.clone().into(),
                s.event_id.into(),
                s.symbol.clone().into(),
                s.family_id.into(),
                s.warning_ts.clone().into(),
                s.entry_ts.clone().into(),
                s.exit_ts.clone().into(),
                s.direction.clone().into(),
                s.level.clone().into(),
                s.grade.clone().into(),
                s.state.clone().into(),
                s.exit_reason.clone().into(),
                s.terminal_reason.clone().into(),
                s.score_status.clone().into(),
                (s.bars_held as i64).into(),
                s.scores["quality_v2"].into(),
                serde_json::to_string(s).unwrap().into(),
                "{\"holding_count\":1}".into(),
            ]);
        }
        tx.execute(store::sql(&format!("INSERT INTO review_results(result_version,event_id,symbol,family_id,warning_ts,entry_ts,exit_ts,direction,level,grade,state,exit_reason,terminal_reason,score_status,bars_held,quality_v2,summary_json,detail_json) VALUES {}",vec!["(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)";chunk.len()].join(",")),values)).await.unwrap();
    }
    tx.execute(store::sql("INSERT INTO review_current(event_id,result_version) SELECT event_id,result_version FROM review_results",vec![])).await.unwrap();
    // Corresponding holding records prove list queries do not hydrate heavy tables.
    for s in &summaries {
        let point = ReviewHoldingPoint {
            sequence: 0,
            interval_start: s.warning_ts.clone(),
            interval_end: s.entry_ts.clone().unwrap(),
            price: 100.,
            floating_r: Some(0.),
            floating_amount: Some(0.),
            mfe: ReviewExcursion::default(),
            mae: ReviewExcursion::default(),
            bars_held: 1,
            hold_score: None,
            checks: serde_json::json!({"stop":90,"target_1r":112,"completed_15m":false}),
        };
        tx.execute(store::sql(
            "INSERT INTO review_holding(result_version,sequence,data) VALUES (?,0,?)",
            vec![
                s.result_version.clone().into(),
                serde_json::to_string(&point).unwrap().into(),
            ],
        ))
        .await
        .unwrap();
    }
    tx.commit().await.unwrap();
    summaries
}

#[tokio::test]
async fn filters_sort_counts_groups_snapshot_expiry_and_paging_share_one_population() {
    let db = db().await;
    let all = seed_summaries(&db, 100).await;
    let engine = ReviewEngine::new(db);
    let mut q = ReviewQuery::default();
    q.filters.sample_scope = Some("all".into());
    q.filters.score_min = Some(75.);
    q.filters.score_max = Some(90.);
    q.filters.symbols = vec!["P01".into(), "P02".into()];
    q.sort_by = Some("score".into());
    q.page_size = Some(3);
    let expected: Vec<_> = all
        .iter()
        .filter(|r| {
            q.filters.symbols.contains(&r.symbol)
                && r.scores["quality_v2"].is_some_and(|v| (75.0..=90.0).contains(&v))
        })
        .collect();
    let response = engine.query("device", q).await.unwrap();
    assert_eq!(response.page.total, expected.len());
    assert_eq!(response.overview.count, expected.len());
    for groups in response.groups.values() {
        assert_eq!(
            groups.iter().map(|g| g.statistics.count).sum::<usize>(),
            expected.len()
        );
    }
    let mut ids = vec![];
    let mut cursor = Some(0);
    while let Some(c) = cursor {
        let p = engine
            .items(
                "device",
                &response.page.snapshot_id,
                ReviewPageRequest {
                    cursor: Some(c),
                    limit: Some(3),
                },
            )
            .await
            .unwrap();
        ids.extend(p.items.into_iter().map(|r| r.event_id));
        cursor = p.next_cursor;
    }
    assert_eq!(ids.len(), expected.len());
    assert_eq!(ids.iter().collect::<HashSet<_>>().len(), ids.len());
    let group = engine
        .aggregate(
            "device",
            ReviewAggregateRequest {
                snapshot_id: response.page.snapshot_id.clone(),
                dimensions: vec!["symbol".into(), "hour".into()],
                score_buckets: vec![],
                time_buckets: vec![],
            },
        )
        .await
        .unwrap();
    assert_eq!(
        group
            .groups
            .iter()
            .map(|g| g.statistics.count)
            .sum::<usize>(),
        expected.len()
    );
    let mut q = ReviewQuery::default();
    q.filters.score_min = Some(90.);
    let d = engine.query("device", q).await.unwrap();
    assert!(d.page.items.iter().all(|r| r.family_id == r.event_id));
    assert_eq!(
        d.page.total,
        all.iter()
            .filter(
                |r| r.event_id == r.family_id && r.scores["quality_v2"].is_some_and(|v| v >= 90.)
            )
            .count(),
        "do not replace a low-score family anchor with a high-score sibling"
    );
    let mut q = ReviewQuery::default();
    q.filters.sample_scope = Some("all".into());
    q.sort_by = Some("score".into());
    q.descending = Some(false);
    q.page_size = Some(100);
    let sorted = engine.query("device", q).await.unwrap();
    let last = sorted.page.items.last().unwrap();
    assert!(last.scores["quality_v2"].is_none());
    let mut q = ReviewQuery::default();
    q.filters.sample_scope = Some("all".into());
    q.filters.time_field = Some("entry".into());
    q.filters.sessions = vec![ReviewSession {
        start: "09:00".into(),
        end: "11:00".into(),
    }];
    let timed = engine.query("device", q).await.unwrap();
    assert!(timed.page.items.iter().all(|r| {
        let t = &r.entry_ts.as_ref().unwrap()[11..16];
        t >= "09:00" && t < "11:00"
    }));
    Arc::get_mut(
        engine
            .snapshots
            .lock()
            .await
            .get_mut(&response.page.snapshot_id)
            .unwrap(),
    )
    .unwrap()
    .created = std::time::Instant::now() - std::time::Duration::from_secs(601);
    assert_eq!(
        engine
            .items(
                "device",
                &response.page.snapshot_id,
                ReviewPageRequest::default()
            )
            .await
            .unwrap_err()
            .downcast_ref::<ReviewError>()
            .unwrap()
            .code,
        "SNAPSHOT_EXPIRED"
    );
}

#[tokio::test]
#[ignore = "explicit 10k performance acceptance run"]
async fn benchmark_10000_precomputed_signals() {
    use sea_orm::ConnectionTrait;
    use std::{io::Write, time::Instant};
    let db = db().await;
    seed_summaries(&db, 10_000).await;
    db.execute(store::sql("WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM seq WHERE n<17) INSERT INTO review_holding(result_version,sequence,data) SELECT h.result_version,seq.n,json_set(h.data,'$.sequence',seq.n,'$.bars_held',seq.n/3+1,'$.interval_start',datetime(json_extract(h.data,'$.interval_start'),'+'||(seq.n*5)||' minutes'),'$.interval_end',datetime(json_extract(h.data,'$.interval_end'),'+'||(seq.n*5)||' minutes')) FROM review_holding h CROSS JOIN seq WHERE h.sequence=0",vec![])).await.unwrap();
    let records = db
        .query_one(store::sql(
            "SELECT count(*) AS n FROM review_holding",
            vec![],
        ))
        .await
        .unwrap()
        .unwrap()
        .try_get::<i64>("", "n")
        .unwrap();
    assert_eq!(records, 180_000);
    let engine = ReviewEngine::new(db.clone());
    let mut times = vec![];
    let mut payload = vec![];
    for i in 0..10 {
        let mut q = ReviewQuery::default();
        q.filters.sample_scope = Some("all".into());
        q.filters.time_end = Some(format!("2099-01-01 00:00:{i:02}"));
        let start = Instant::now();
        let result = engine.query("benchmark", q).await.unwrap();
        times.push(start.elapsed().as_secs_f64() * 1000.);
        assert_eq!(result.page.total, 10_000);
        assert_eq!(result.page.items.len(), 100);
        payload = serde_json::to_vec(&result).unwrap();
    }
    times.sort_by(f64::total_cmp);
    let mut gzip = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::default());
    gzip.write_all(&payload).unwrap();
    let zipped = gzip.finish().unwrap();
    let q = ReviewQuery::default();
    let (sql, values) = super::query::selection(&q);
    let plan = db
        .query_all(store::sql(&format!("EXPLAIN QUERY PLAN {sql}"), values))
        .await
        .unwrap()
        .into_iter()
        .map(|r| r.try_get::<String>("", "detail").unwrap())
        .collect::<Vec<_>>();
    eprintln!("REVIEW_BENCH {{\"signals\":10000,\"holding_records\":180000,\"samples\":10,\"p50_ms\":{:.2},\"p95_ms\":{:.2},\"json_bytes\":{},\"gzip_bytes\":{},\"plan\":{:?}}}",times[4],times[9],payload.len(),zipped.len(),plan);
    assert!(
        zipped.len() <= 200 * 1024,
        "first response exceeds 200KiB gzip"
    );
}

#[tokio::test]
async fn midnight_multiple_sessions_exit_filters_and_custom_buckets_roundtrip() {
    let db = db().await;
    let all = seed_summaries(&db, 300).await;
    let engine = ReviewEngine::new(db);
    let mut q = ReviewQuery::default();
    q.filters.sample_scope = Some("all".into());
    q.filters.time_field = Some("entry".into());
    q.page_size = Some(500);
    q.filters.sessions = vec![
        ReviewSession {
            start: "21:00".into(),
            end: "02:30".into(),
        },
        ReviewSession {
            start: "09:00".into(),
            end: "11:00".into(),
        },
    ];
    q.filters.exit_reasons = vec!["stop".into()];
    let expected = all
        .iter()
        .filter(|r| {
            r.exit_reason.as_deref() == Some("stop")
                && r.entry_ts.as_ref().is_some_and(|s| {
                    let h = &s[11..16];
                    h >= "21:00" || h < "02:30" || h >= "09:00" && h < "11:00"
                })
        })
        .count();
    let result = engine.query("device", q).await.unwrap();
    assert_eq!(result.page.total, expected);
    let mut q = ReviewQuery::default();
    q.filters.sample_scope = Some("all".into());
    q.filters.score_min = Some(90.);
    let result = engine.query("device", q).await.unwrap();
    let request = ReviewAggregateRequest {
        snapshot_id: result.page.snapshot_id.clone(),
        dimensions: vec!["score_band".into()],
        score_buckets: vec![
            ReviewBucket {
                label: "90-95".into(),
                min: 90.,
                max: 95.,
            },
            ReviewBucket {
                label: "95-100".into(),
                min: 95.,
                max: 100.,
            },
        ],
        time_buckets: vec![],
    };
    let groups = engine.aggregate("device", request).await.unwrap();
    let expected = all
        .iter()
        .filter(|r| r.scores["quality_v2"].is_some_and(|s| (95.0..=100.0).contains(&s)))
        .count();
    assert_eq!(
        groups
            .groups
            .iter()
            .find(|g| g.key == vec!["95-100"])
            .unwrap()
            .statistics
            .count,
        expected
    );
    let bad = ReviewAggregateRequest {
        snapshot_id: result.page.snapshot_id,
        dimensions: vec!["hour".into()],
        score_buckets: vec![],
        time_buckets: vec![
            ReviewTimeBucket {
                label: "a".into(),
                start: "21:00".into(),
                end: "02:30".into(),
            },
            ReviewTimeBucket {
                label: "b".into(),
                start: "01:00".into(),
                end: "03:00".into(),
            },
        ],
    };
    assert_eq!(
        engine
            .aggregate("device", bad)
            .await
            .unwrap_err()
            .downcast_ref::<ReviewError>()
            .unwrap()
            .code,
        "INVALID_QUERY"
    );
}
