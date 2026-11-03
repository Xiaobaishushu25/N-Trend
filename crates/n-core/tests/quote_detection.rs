use n_core::{
    analyze::{
        manual_level::{evaluate, ManualLevelDefinition},
        model::{Bar, DT},
    },
    config::Config,
    fetch::kline::Kline,
    service::{MarketSnapshot, Services},
    storage::{self, entities::manual_levels, repo},
};
use sea_orm::{ActiveModelTrait, ConnectionTrait, DbBackend, Set, Statement};

async fn level(db: &sea_orm::DatabaseConnection) -> manual_levels::Model {
    manual_levels::ActiveModel {
        symbol: Set("RB0".into()),
        timeframe: Set("15m".into()),
        name: Set("test".into()),
        start_ts: Set("2026-01-05 09:00:00".into()),
        end_ts: Set(None),
        zone_low: Set(99.0),
        zone_high: Set(101.0),
        role: Set("unknown".into()),
        role_override: Set("auto".into()),
        role_confidence: Set(0.0),
        status: Set("active".into()),
        monitor_enabled: Set(true),
        current_phase: Set("pending".into()),
        last_event_ts: Set(None),
        created_at: Set("2026-01-05 09:00:00".into()),
        updated_at: Set("2026-01-05 09:00:00".into()),
        ..Default::default()
    }
    .insert(db)
    .await
    .unwrap()
}

#[tokio::test]
async fn manual_level_edits_monitoring_and_archive_are_persisted() {
    let db = storage::connect(std::path::Path::new(":memory:"))
        .await
        .unwrap();
    let mut row = level(&db).await;
    row.current_phase = "testing".into();
    row.role = "support".into();
    row.zone_low = 98.0;
    row.role_confidence = 0.9;
    row.role_override = "support".into();
    repo::update_manual_level(&db, row.clone()).await.unwrap();
    let saved = repo::manual_level_by_id(&db, row.id)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(saved, row);
    let paused = repo::set_manual_level_monitoring(&db, row.id, false)
        .await
        .unwrap();
    assert!(!paused.monitor_enabled);
    assert_eq!(paused.status, "paused");
    repo::archive_manual_level(&db, row.id).await.unwrap();
    assert_eq!(
        repo::manual_level_by_id(&db, row.id)
            .await
            .unwrap()
            .unwrap()
            .status,
        "archived"
    );
}

#[tokio::test]
async fn stale_analysis_cannot_overwrite_a_same_second_user_edit() {
    let db = storage::connect(std::path::Path::new(":memory:"))
        .await
        .unwrap();
    let observed = level(&db).await;
    let mut edited = observed.clone();
    edited.zone_low = 97.0;
    repo::update_manual_level(&db, edited).await.unwrap();
    let mut next = observed.clone();
    next.current_phase = "testing".into();
    assert!(
        !repo::record_manual_level_evaluation(&db, observed.clone(), next, None)
            .await
            .unwrap()
    );
    let saved = repo::manual_level_by_id(&db, observed.id)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(saved.zone_low, 97.0);
    assert_eq!(saved.current_phase, "pending");
}

#[tokio::test]
async fn repeated_quote_does_not_write_manual_level_state_or_duplicate_events() {
    let db = storage::connect(std::path::Path::new(":memory:"))
        .await
        .unwrap();
    let mut config = Config::default();
    config.data_source.primary_source = "offline".into();
    let services = Services::new(
        db.clone(),
        config,
        std::env::temp_dir().join("quote-detection-test.json"),
    )
    .await
    .unwrap();
    services.seed_symbols("RB0").await.unwrap();
    let begin =
        chrono::NaiveDateTime::parse_from_str("2026-01-05 09:00:00", "%Y-%m-%d %H:%M:%S").unwrap();
    let raw: Vec<Kline> = (1..=180)
        .map(|i| Kline {
            datetime: (begin + chrono::Duration::minutes(i * 5))
                .format("%Y-%m-%d %H:%M:%S")
                .to_string(),
            open: 100.0,
            high: 102.0,
            low: 98.0,
            close: 100.0,
            volume: 100.0,
            hold: 0.0,
        })
        .collect();
    services
        .pipeline
        .process_raw_batch("RB0", &raw)
        .await
        .unwrap();
    let row = level(&db).await;
    let snapshots = [MarketSnapshot {
        code: "RB0".into(),
        latest: Some(100.0),
        change_pct: None,
    }];
    assert_eq!(
        services
            .manual_level_alerts(&snapshots)
            .await
            .unwrap()
            .len(),
        1
    );
    assert_eq!(
        repo::manual_level_by_id(&db, row.id)
            .await
            .unwrap()
            .unwrap()
            .current_phase,
        "testing"
    );
    async fn changes(db: &sea_orm::DatabaseConnection) -> i64 {
        db.query_one(Statement::from_string(
            DbBackend::Sqlite,
            "SELECT total_changes() AS n".to_string(),
        ))
        .await
        .unwrap()
        .unwrap()
        .try_get("", "n")
        .unwrap()
    }
    let before = changes(&db).await;
    assert!(services
        .manual_level_alerts(&snapshots)
        .await
        .unwrap()
        .is_empty());
    assert_eq!(
        before,
        changes(&db).await,
        "unchanged quotes must perform zero writes"
    );
    assert_eq!(
        repo::manual_level_events(&db, row.id).await.unwrap().len(),
        1
    );
    let mut saved = repo::manual_level_by_id(&db, row.id)
        .await
        .unwrap()
        .unwrap();
    let event_time = saved.last_event_ts.clone();
    saved.current_phase = "pending".into();
    repo::update_manual_level(&db, saved).await.unwrap();
    assert!(services
        .manual_level_alerts(&snapshots)
        .await
        .unwrap()
        .is_empty());
    let saved = repo::manual_level_by_id(&db, row.id)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(saved.current_phase, "testing");
    assert_eq!(
        saved.last_event_ts, event_time,
        "duplicate events must not refresh the event timestamp"
    );
    assert_eq!(
        repo::manual_level_events(&db, row.id).await.unwrap().len(),
        1
    );
}

#[test]
fn manual_level_tail_window_preserves_full_history_result() {
    let start =
        chrono::NaiveDateTime::parse_from_str("2026-01-05 09:00:00", "%Y-%m-%d %H:%M:%S").unwrap();
    let bars: Vec<Bar> = (0..200)
        .map(|i| {
            let ts = (start + chrono::Duration::minutes(i * 15))
                .format("%Y-%m-%d %H:%M:%S")
                .to_string();
            let p = 98.0 + (i % 9) as f64;
            Bar {
                dt: DT::from_bar_ts(&ts).unwrap(),
                open: p,
                high: p + 2.0,
                low: p - 2.0,
                close: p + 1.0,
                volume: 100.0 + i as f64,
                hold: 0.0,
                rollover: false,
            }
        })
        .collect();
    for index in [0, 100, 175, 195, 199] {
        let definition = ManualLevelDefinition {
            zone_low: 99.0,
            zone_high: 101.0,
            start_ts: bars[index].dt.to_bar_ts(),
        };
        for phase in ["pending", "testing", "breakout_confirmed"] {
            for role in ["auto", "support", "resistance"] {
                let full = evaluate(&definition, &bars, Some(100.0), phase, "unknown", role);
                let tail = evaluate(
                    &definition,
                    &bars[bars.len() - 30..],
                    Some(100.0),
                    phase,
                    "unknown",
                    role,
                );
                assert_eq!(
                    serde_json::to_value(full).unwrap(),
                    serde_json::to_value(tail).unwrap()
                );
            }
        }
    }
}
