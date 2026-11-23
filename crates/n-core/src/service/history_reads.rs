use super::*;
use crate::storage::cache_revisions::{
    market_series_boundary, market_series_revisions, SeriesRevision, SeriesRevisions,
};

#[derive(Clone, PartialEq, Eq)]
pub(super) struct BarHistoryStamp {
    series: SeriesRevision,
    raw: Option<SeriesRevision>,
    rollovers: i64,
    visible: Option<String>,
}

impl BarHistoryStamp {
    pub(super) fn new(revisions: SeriesRevisions, visible: Option<String>, uses_raw: bool) -> Self {
        Self {
            series: revisions.series,
            raw: uses_raw.then_some(revisions.raw),
            rollovers: revisions.rollovers,
            visible,
        }
    }

    pub(super) fn uses_raw(&self) -> bool {
        self.raw.is_some()
    }

    fn patch_start<'a>(&'a self, previous: &'a Self) -> Option<&'a str> {
        if self.uses_raw() || previous.uses_raw() || self.rollovers != previous.rollovers {
            return None;
        }
        let boundary = self.visible.as_deref()?.min(previous.visible.as_deref()?);
        match self.series.revision.checked_sub(previous.series.revision) {
            Some(0) => Some(boundary),
            Some(1) => Some(boundary.min(self.series.changed_at.as_str())),
            _ => None,
        }
    }
}

impl Services {
    pub async fn shared_bars_for(&self, symbol: &str, timeframe: &str) -> Result<Arc<Vec<Bar>>> {
        let tf = Timeframe::parse(timeframe).ok_or_else(|| anyhow!("不支持的级别 {timeframe}"))?;
        let timeframe = tf.as_str();
        let started = Instant::now();
        let mut slot = self
            .analysis_history
            .lock((symbol.to_owned(), timeframe.to_owned()))
            .await;
        if let Some((stamp, rows)) = slot.as_ref() {
            let current = market_series_boundary(&self.db, symbol, timeframe).await?;
            if *stamp == BarHistoryStamp::new(current.versions, current.visible, stamp.uses_raw()) {
                return Ok(rows.clone());
            }
        }
        let _read = self.history_reads.acquire().await?;
        let conn = self.db.begin().await?;
        let current = market_series_boundary(&conn, symbol, timeframe).await?;
        let cutoff = current.cutoff;
        let with_raw =
            BarHistoryStamp::new(current.versions.clone(), current.visible.clone(), true);
        let derived = BarHistoryStamp::new(current.versions, current.visible, false);
        if let Some((stamp, rows)) = slot.as_ref() {
            if *stamp == derived || *stamp == with_raw {
                conn.commit().await?;
                return Ok(rows.clone());
            }
        }

        let (mut bars, incremental) = if let Some(((_, rows), start)) = slot
            .as_ref()
            .and_then(|cached| derived.patch_start(&cached.0).map(|start| (cached, start)))
        {
            let start_dt = parse_dt(start);
            match start_dt {
                Some(start_dt) if rows.windows(2).all(|pair| pair[0].dt < pair[1].dt) => {
                    let prefix = rows.partition_point(|bar| bar.dt < start_dt);
                    let mut bars = rows[..prefix].to_vec();
                    bars.extend(
                        repo::series_window(&conn, symbol, timeframe, start, &cutoff)
                            .await?
                            .iter()
                            .filter_map(model_to_bar),
                    );
                    (bars, true)
                }
                _ => (
                    read_derived(&conn, symbol, timeframe, &cutoff).await?,
                    false,
                ),
            }
        } else {
            (
                read_derived(&conn, symbol, timeframe, &cutoff).await?,
                false,
            )
        };
        let stamp = if bars.len() < ATR_PERIOD + 2 {
            let raw = repo::raw_klines(&conn, symbol).await?;
            let raw: Vec<_> = raw.iter().map(model_to_fetch).collect();
            let fallback: Vec<_> = aggregate(&raw, tf)
                .iter()
                .filter(|row| row.datetime <= cutoff)
                .filter_map(fetch_to_bar)
                .collect();
            if fallback.len() > bars.len() {
                bars = fallback;
            }
            with_raw
        } else {
            derived
        };
        let rollovers = repo::symbol_rollovers(&conn, symbol).await?;
        conn.commit().await?;
        // A correction can move the rollover marker to a different bar.
        for bar in &mut bars {
            bar.rollover = false;
        }
        mark_rollover_bars(&mut bars, &rollovers, timeframe);
        let bars = Arc::new(bars);
        if started.elapsed() >= Duration::from_millis(500) {
            tracing::info!(target: "scan_perf", symbol, timeframe, incremental, rows = bars.len(),
                total_ms = started.elapsed().as_millis(), "后台分析历史读取较慢");
        }
        *slot = Some((stamp, bars.clone()));
        Ok(bars)
    }

    pub async fn shared_raw_for(&self, symbol: &str) -> Result<Arc<Vec<Kline>>> {
        let mut slot = self
            .raw_history
            .lock((symbol.to_owned(), "5m".into()))
            .await;
        if let Some((stamp, rows)) = slot.as_ref() {
            if *stamp == market_series_revisions(&self.db, symbol, "5m").await?.raw {
                return Ok(rows.clone());
            }
        }
        let _read = self.history_reads.acquire().await?;
        let conn = self.db.begin().await?;
        let stamp = market_series_revisions(&conn, symbol, "5m").await?.raw;
        if let Some((old, rows)) = slot.as_ref() {
            if *old == stamp {
                conn.commit().await?;
                return Ok(rows.clone());
            }
        }
        let rows = match slot.as_ref() {
            Some((old, rows)) if stamp.revision.checked_sub(old.revision) == Some(1) => {
                let prefix = rows.partition_point(|bar| bar.datetime < stamp.changed_at);
                let mut bars = rows[..prefix].to_vec();
                let last = repo::latest_raw_ts(&conn, symbol)
                    .await?
                    .unwrap_or_default();
                bars.extend(
                    repo::raw_klines_window(&conn, symbol, &stamp.changed_at, &last)
                        .await?
                        .iter()
                        .map(model_to_fetch),
                );
                bars
            }
            _ => repo::raw_klines(&conn, symbol)
                .await?
                .iter()
                .map(model_to_fetch)
                .collect(),
        };
        conn.commit().await?;
        let rows = Arc::new(rows);
        *slot = Some((stamp, rows.clone()));
        Ok(rows)
    }
}

async fn read_derived(
    db: &impl ConnectionTrait,
    symbol: &str,
    timeframe: &str,
    cutoff: &str,
) -> Result<Vec<Bar>> {
    Ok(repo::klines(db, symbol, timeframe, None, Some(cutoff))
        .await?
        .iter()
        .filter_map(model_to_bar)
        .collect())
}
