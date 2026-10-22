//! Warning-time, net-profit contract. Legacy trigger-close models are ineligible.
pub mod replay;
pub mod research;
pub mod store;
pub const CONTRACT: &str = "warning-breakout-net-v3";
pub const POLICY: &str = "live-partial-continuous-08r-v3";
pub const FEATURES: &str = "warning-only-v3";
pub const WHITELIST: &[&str] = &[
    "a_move_atr",
    "b_move_atr",
    "a_speed",
    "retracement",
    "warning_volume_ratio",
    "setup_quality",
    "direction_num",
];
pub fn whitelist() -> Vec<String> {
    WHITELIST.iter().map(|x| x.to_string()).collect()
}

#[cfg(test)]
mod closed_bar_tests {
    use sea_orm::Set;
    #[tokio::test]
    async fn cached_partial_bucket_is_never_an_analysis_input() {
        let db = crate::storage::connect(std::path::Path::new(":memory:"))
            .await
            .unwrap();
        let mut rows = Vec::new();
        for minute in [5, 10, 15, 20, 25] {
            rows.push(crate::storage::entities::klines::ActiveModel {
                symbol: Set("RB0".into()),
                timeframe: Set("5m".into()),
                ts: Set(format!("2026-01-05 11:{minute:02}:00")),
                open: Set(100.),
                high: Set(101.),
                low: Set(99.),
                close: Set(100.),
                volume: Set(1.),
                hold: Set(1.),
                source: Set("raw".into()),
            })
        }
        crate::storage::repo::upsert_klines(&db, rows)
            .await
            .unwrap();
        let mut cfg = crate::config::Config::default();
        cfg.data_source.primary_source = "offline".into();
        let service =
            crate::service::Services::new(db, cfg, std::env::temp_dir().join("v3-no-config.json"))
                .await
                .unwrap();
        service.pipeline.rebuild_derived("RB0").await.unwrap();
        let bars = service.bars_for("RB0", "15m").await.unwrap();
        assert!(!bars.is_empty());
        assert!(bars
            .iter()
            .all(|b| b.dt.to_bar_ts().as_str() <= "2026-01-05 11:25:00"));
        let response = service
            .get_chart_klines("RB0", "15m", None, "test")
            .await
            .unwrap();
        assert_eq!(response.status, "offline_snapshot");
        assert!(response
            .rows
            .iter()
            .all(|b| b.ts.as_str() <= "2026-01-05 11:25:00"));
        assert!(response.partial_bar.is_none());
    }
}
