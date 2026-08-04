//! Repository layer over the SeaORM entities.

use anyhow::{anyhow, Context, Result};
use sea_orm::sea_query::OnConflict;
use sea_orm::{
    ActiveModelTrait, ColumnTrait, DatabaseConnection, EntityTrait, PaginatorTrait,
    QueryFilter, QueryOrder, QuerySelect, Set,
};

use crate::storage::entities::{klines, scans, settings, signals, symbols};

pub async fn upsert_klines(db: &DatabaseConnection, rows: Vec<klines::ActiveModel>) -> Result<()> {
    if rows.is_empty() {
        return Ok(());
    }
    klines::Entity::insert_many(rows)
        .on_conflict(
            OnConflict::columns([
                klines::Column::Symbol,
                klines::Column::Timeframe,
                klines::Column::Ts,
            ])
            .update_columns([
                klines::Column::Open,
                klines::Column::High,
                klines::Column::Low,
                klines::Column::Close,
                klines::Column::Volume,
                klines::Column::Hold,
                klines::Column::Source,
            ])
            .to_owned(),
        )
        .exec(db)
        .await
        .context("upsert K线失败")?;
    Ok(())
}

pub async fn delete_derived_klines(db: &DatabaseConnection, symbol: &str) -> Result<u64> {
    let res = klines::Entity::delete_many()
        .filter(klines::Column::Symbol.eq(symbol))
        .filter(klines::Column::Source.eq("derived"))
        .exec(db)
        .await
        .context("清理派生K线失败")?;
    Ok(res.rows_affected)
}

pub async fn delete_symbol_klines(db: &DatabaseConnection, symbol: &str) -> Result<()> {
    klines::Entity::delete_many()
        .filter(klines::Column::Symbol.eq(symbol))
        .exec(db)
        .await
        .context("删除品种K线失败")?;
    Ok(())
}
pub async fn latest_ts(
    db: &DatabaseConnection,
    symbol: &str,
    timeframe: &str,
) -> Result<Option<String>> {
    let row = klines::Entity::find()
        .filter(klines::Column::Symbol.eq(symbol))
        .filter(klines::Column::Timeframe.eq(timeframe))
        .order_by_desc(klines::Column::Ts)
        .one(db)
        .await
        .context("查询最新K线时间失败")?;
    Ok(row.map(|r| r.ts))
}

pub async fn klines(
    db: &DatabaseConnection,
    symbol: &str,
    timeframe: &str,
    limit: Option<usize>,
    end_ts: Option<&str>,
) -> Result<Vec<klines::Model>> {
    let mut q = klines::Entity::find()
        .filter(klines::Column::Symbol.eq(symbol))
        .filter(klines::Column::Timeframe.eq(timeframe));
    if let Some(end) = end_ts {
        q = q.filter(klines::Column::Ts.lte(end));
    }
    let mut rows = q
        .order_by_asc(klines::Column::Ts)
        .all(db)
        .await
        .context("查询K线失败")?;
    if let Some(limit) = limit {
        let take = limit.min(rows.len());
        rows = rows.split_off(rows.len() - take);
    }
    Ok(rows)
}

pub async fn raw_klines(db: &DatabaseConnection, symbol: &str) -> Result<Vec<klines::Model>> {
    let rows = klines::Entity::find()
        .filter(klines::Column::Symbol.eq(symbol))
        .filter(klines::Column::Timeframe.eq("5m"))
        .filter(klines::Column::Source.eq("raw"))
        .order_by_asc(klines::Column::Ts)
        .all(db)
        .await
        .context("查询原始K线失败")?;
    Ok(rows)
}

pub async fn list_symbols(
    db: &DatabaseConnection,
    only_enabled: bool,
) -> Result<Vec<symbols::Model>> {
    let mut q = symbols::Entity::find().order_by_asc(symbols::Column::Code);
    if only_enabled {
        q = q.filter(symbols::Column::Enabled.eq(true));
    }
    Ok(q.all(db).await.context("查询品种失败")?)
}

pub async fn symbol_exists(db: &DatabaseConnection, code: &str) -> Result<bool> {
    let count = symbols::Entity::find()
        .filter(symbols::Column::Code.eq(code))
        .count(db)
        .await
        .context("查询品种失败")?;
    Ok(count > 0)
}

pub async fn upsert_symbols(db: &DatabaseConnection, rows: Vec<symbols::ActiveModel>) -> Result<()> {
    if rows.is_empty() {
        return Ok(());
    }
    symbols::Entity::insert_many(rows)
        .on_conflict(
            OnConflict::columns([symbols::Column::Code])
                .update_columns([
                    symbols::Column::Name,
                    symbols::Column::Variety,
                    symbols::Column::Exchange,
                    symbols::Column::Node,
                ])
                .to_owned(),
        )
        .exec(db)
        .await
        .context("upsert品种失败")?;
    Ok(())
}

pub async fn set_symbol_flags(
    db: &DatabaseConnection,
    code: &str,
    watchlist: bool,
    enabled: bool,
) -> Result<()> {
    let row = symbols::Entity::find()
        .filter(symbols::Column::Code.eq(code))
        .one(db)
        .await
        .context("查询品种失败")?
        .ok_or_else(|| anyhow!("品种不存在: {code}"))?;
    let mut model: symbols::ActiveModel = row.into();
    model.watchlist = Set(watchlist);
    model.enabled = Set(enabled);
    model.updated_at = Set(crate::analyze::time::now_display());
    model.save(db).await.context("更新品种失败")?;
    Ok(())
}

pub async fn remove_symbol(db: &DatabaseConnection, code: &str) -> Result<()> {
    symbols::Entity::delete_many()
        .filter(symbols::Column::Code.eq(code))
        .exec(db)
        .await
        .context("删除品种失败")?;
    Ok(())
}

pub async fn insert_scan(
    db: &DatabaseConnection,
    started_at: String,
    finished_at: String,
    status: String,
    scanned: i64,
    active_count: i64,
    summary: String,
) -> Result<i64> {
    let model = scans::ActiveModel {
        id: sea_orm::NotSet,
        started_at: Set(started_at),
        finished_at: Set(finished_at),
        status: Set(status),
        scanned: Set(scanned),
        active_count: Set(active_count),
        summary: Set(summary),
    };
    let res = scans::Entity::insert(model).exec(db).await.context("写入扫描记录失败")?;
    Ok(res.last_insert_id)
}

pub async fn insert_signals(db: &DatabaseConnection, rows: Vec<signals::ActiveModel>) -> Result<()> {
    if rows.is_empty() {
        return Ok(());
    }
    signals::Entity::insert_many(rows)
        .exec(db)
        .await
        .context("写入信号失败")?;
    Ok(())
}

pub async fn recent_scans(db: &DatabaseConnection, limit: u64) -> Result<Vec<scans::Model>> {
    Ok(scans::Entity::find()
        .order_by_desc(scans::Column::Id)
        .limit(limit)
        .all(db)
        .await
        .context("查询扫描记录失败")?)
}

pub async fn signals_for_scan(
    db: &DatabaseConnection,
    scan_id: i64,
) -> Result<Vec<signals::Model>> {
    Ok(signals::Entity::find()
        .filter(signals::Column::ScanId.eq(scan_id))
        .order_by_desc(signals::Column::Id)
        .all(db)
        .await
        .context("查询信号失败")?)
}

/// 最近一次扫描产出的信号（与图表页口径一致，避免展示旧扫描的过期信号）。
pub async fn latest_signals(db: &DatabaseConnection, _limit: u64) -> Result<Vec<signals::Model>> {
    let Some(latest) = scans::Entity::find()
        .order_by_desc(scans::Column::Id)
        .one(db)
        .await
        .context("查询最新扫描失败")?
    else {
        return Ok(Vec::new());
    };
    signals_for_scan(db, latest.id).await
}

pub async fn all_settings(db: &DatabaseConnection) -> Result<std::collections::HashMap<String, String>> {
    let rows = settings::Entity::find()
        .all(db)
        .await
        .context("读取设置失败")?;
    Ok(rows.into_iter().map(|r| (r.key, r.value)).collect())
}

pub async fn set_settings(db: &DatabaseConnection, map: &std::collections::HashMap<String, String>) -> Result<()> {
    let rows: Vec<settings::ActiveModel> = map
        .iter()
        .map(|(key, value)| settings::ActiveModel {
            key: Set(key.clone()),
            value: Set(value.clone()),
        })
        .collect();
    if rows.is_empty() {
        return Ok(());
    }
    settings::Entity::insert_many(rows)
        .on_conflict(
            OnConflict::columns([settings::Column::Key])
                .update_columns([settings::Column::Value])
                .to_owned(),
        )
        .exec(db)
        .await
        .context("写入设置失败")?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use sea_orm::DatabaseConnection;

    use super::*;

    async fn test_db() -> DatabaseConnection {
        crate::storage::connect(std::path::Path::new(":memory:"))
            .await
            .expect("in-memory db")
    }

    #[tokio::test]
    async fn upsert_and_query_klines() {
        let db = test_db().await;
        let row = |ts: &str, close: f64| klines::ActiveModel {
            symbol: Set("RB0".to_string()),
            timeframe: Set("5m".to_string()),
            ts: Set(ts.to_string()),
            open: Set(1.0),
            high: Set(2.0),
            low: Set(0.5),
            close: Set(close),
            volume: Set(10.0),
            hold: Set(100.0),
            source: Set("raw".to_string()),
        };
        upsert_klines(&db, vec![row("2026-08-03 09:00:00", 1.5)]).await.unwrap();
        // 相同主键再次 upsert，close 被覆盖
        upsert_klines(&db, vec![row("2026-08-03 09:00:00", 1.8)]).await.unwrap();
        upsert_klines(&db, vec![row("2026-08-03 09:05:00", 1.9)]).await.unwrap();

        let rows = klines(&db, "RB0", "5m", None, None).await.unwrap();
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0].close, 1.8);
        assert_eq!(rows[1].ts, "2026-08-03 09:05:00");

        let limited = klines(&db, "RB0", "5m", Some(1), None).await.unwrap();
        assert_eq!(limited.len(), 1);
        assert_eq!(limited[0].ts, "2026-08-03 09:05:00");
    }

    #[tokio::test]
    async fn settings_roundtrip() {
        let db = test_db().await;
        let mut map = std::collections::HashMap::new();
        map.insert("a".to_string(), "1".to_string());
        map.insert("b".to_string(), "2".to_string());
        set_settings(&db, &map).await.unwrap();
        map.insert("a".to_string(), "9".to_string());
        set_settings(&db, &map).await.unwrap();
        let all = all_settings(&db).await.unwrap();
        assert_eq!(all.get("a").map(String::as_str), Some("9"));
        assert_eq!(all.get("b").map(String::as_str), Some("2"));
    }
}



