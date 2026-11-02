use super::{
    replay::{self, CalendarInput},
    ReviewEngine,
};
use crate::{
    analyze::model::{Bar, DT},
    fetch::kline::Kline,
    storage::entities::{klines, pattern_events, rollovers, symbols},
};
use anyhow::Result;
use chrono::{Duration, Utc};
use n_protocol::review::*;
use sea_orm::{
    ColumnTrait, ConnectionTrait, DatabaseConnection, DbBackend, EntityTrait, QueryFilter,
    QueryOrder, Statement, TransactionTrait,
};
use serde_json::json;
use std::collections::{BTreeMap, HashMap, HashSet};

pub(crate) fn sql(text: &str, values: Vec<sea_orm::Value>) -> Statement {
    Statement::from_sql_and_values(DbBackend::Sqlite, text, values)
}
pub(crate) fn now() -> String {
    Utc::now().to_rfc3339()
}

fn implementation_version() -> String {
    let mut hash = blake3::Hasher::new();
    for source in [
        include_str!("replay.rs"),
        include_str!("store.rs"),
        include_str!("../quality/mod.rs"),
        include_str!("../quality/review.rs"),
        include_str!("../quality/score_scan.rs"),
        include_str!("../quality/structure.rs"),
        include_str!("../quality/blind.rs"),
        include_str!("../quality/research.rs"),
        include_str!("../precision.rs"),
        include_str!("../session/calendar.rs"),
        include_str!("../session/spec.rs"),
        crate::quality::RULES_JSON,
        crate::quality::score_scan::PROFILE,
    ] {
        hash.update(source.as_bytes());
    }
    hash.finalize().to_hex().to_string()
}

pub async fn ensure(db: &DatabaseConnection) -> Result<()> {
    db.execute_unprepared(include_str!("schema.sql")).await?;
    Ok(())
}

pub(crate) async fn revision(db: &DatabaseConnection) -> Result<i64> {
    Ok(db
        .query_one(sql("SELECT revision FROM review_state WHERE id=1", vec![]))
        .await?
        .unwrap()
        .try_get("", "revision")?)
}

pub(crate) async fn job(db: &DatabaseConnection, id: &str) -> Result<Option<ReviewJob>> {
    db.query_one(sql(
        "SELECT data FROM review_jobs WHERE id=?",
        vec![id.into()],
    ))
    .await?
    .map(|r| Ok(serde_json::from_str(&r.try_get::<String>("", "data")?)?))
    .transpose()
}

impl ReviewEngine {
    /// Resume queued/running jobs after restart; seed historical backfill once.
    pub async fn initialize(&self) -> Result<()> {
        let _guard = self.worker.lock().await;
        let calendar = format!(
            "{}:{}",
            CalendarInput::current().version(),
            implementation_version()
        );
        for row in self.db.query_all(sql("SELECT id,data FROM review_jobs WHERE status IN ('queued','running') AND implementation_version<>?",vec![implementation_version().into()])).await? {
            let mut job:ReviewJob=serde_json::from_str(&row.try_get::<String>("","data")?)?;
            job.status="failed".into();job.updated_at=now();job.errors.push("implementation changed during rebuild; queued for complete retry".into());
            self.db.execute(sql("UPDATE review_jobs SET status='failed',data=? WHERE id=?",vec![serde_json::to_string(&job)?.into(),job.id.into()])).await?;
        }
        let old = self
            .db
            .query_one(sql(
                "SELECT calendar_version FROM review_state WHERE id=1",
                vec![],
            ))
            .await?
            .unwrap()
            .try_get::<String>("", "calendar_version")?;
        if old != calendar {
            let tx = self.db.begin().await?;
            tx.execute(sql("INSERT INTO review_dirty(symbol,revision) SELECT DISTINCT symbol,1 FROM pattern_events WHERE true ON CONFLICT(symbol) DO UPDATE SET revision=review_dirty.revision+1",vec![])).await?;
            tx.execute(sql(
                "UPDATE review_state SET calendar_version=? WHERE id=1",
                vec![calendar.into()],
            ))
            .await?;
            tx.commit().await?;
        }
        self.db.execute(sql("INSERT OR IGNORE INTO review_dirty(symbol,revision) SELECT DISTINCT p.symbol,1 FROM pattern_events p WHERE NOT EXISTS(SELECT 1 FROM review_current c WHERE c.event_id=p.id)",vec![])).await?;
        Ok(())
    }

    pub async fn rebuild(&self, request: ReviewRebuildRequest) -> Result<ReviewJob> {
        let mut selected: Vec<String> = request
            .symbols
            .iter()
            .map(|s| s.trim().to_ascii_uppercase())
            .collect();
        if selected.len() > 500 || selected.iter().any(|s| s.is_empty() || s.len() > 32) {
            return Err(super::error("INVALID_QUERY", "invalid rebuild symbols"));
        }
        if selected.is_empty() {
            selected = self
                .db
                .query_all(sql(
                    "SELECT DISTINCT symbol FROM pattern_events ORDER BY symbol",
                    vec![],
                ))
                .await?
                .into_iter()
                .map(|r| r.try_get("", "symbol"))
                .collect::<Result<_, _>>()?;
        }
        selected.sort();
        selected.dedup();
        self.create_job(selected, "historical_rebuild").await
    }

    async fn create_job(&self, selected: Vec<String>, origin: &str) -> Result<ReviewJob> {
        let job = ReviewJob {
            id: uuid::Uuid::new_v4().to_string(),
            status: "queued".into(),
            origin: origin.into(),
            total_symbols: selected.len(),
            completed_symbols: 0,
            processed_signals: 0,
            coverage: BTreeMap::new(),
            errors: vec![],
            created_at: now(),
            updated_at: now(),
            revision: None,
        };
        let tx = self.db.begin().await?;
        let waiting = tx
            .query_one(sql(
                "SELECT count(*) AS n FROM review_jobs WHERE status IN ('queued','running')",
                vec![],
            ))
            .await?
            .unwrap()
            .try_get::<i64>("", "n")?;
        if waiting >= 32 {
            return Err(super::error("QUEUE_FULL", "review queue full"));
        }
        tx.execute(sql(
            "INSERT INTO review_jobs(id,status,data,calendar_json,implementation_version) VALUES (?,'queued',?,?,?)",
            vec![
                job.id.clone().into(),
                serde_json::to_string(&job)?.into(),
                serde_json::to_string(&CalendarInput::current())?.into(),
                implementation_version().into(),
            ],
        ))
        .await?;
        for symbol in selected {
            tx.execute(sql("INSERT INTO review_job_symbols(job_id,symbol,dirty_revision) VALUES (?,?,COALESCE((SELECT revision FROM review_dirty WHERE symbol=?),0))",vec![job.id.clone().into(),symbol.clone().into(),symbol.into()])).await?;
        }
        tx.commit().await?;
        Ok(job)
    }

    pub async fn get_job(&self, id: &str) -> Result<ReviewJob> {
        job(&self.db, id)
            .await?
            .ok_or_else(|| super::error("NOT_FOUND", "review job not found"))
    }

    /// One symbol per step. Durable checkpoints and staging keep partial jobs invisible.
    pub async fn worker_step(&self) -> Result<Option<ReviewJob>> {
        let _guard = self.worker.lock().await;
        let selected=self.db.query_one(sql("SELECT id FROM review_jobs WHERE status IN ('queued','running') ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END,rowid LIMIT 1",vec![])).await?;
        let mut job = if let Some(row) = selected {
            self.get_job(&row.try_get::<String>("", "id")?).await?
        } else {
            let selected=self.db.query_all(sql("SELECT d.symbol FROM review_dirty d WHERE NOT EXISTS(SELECT 1 FROM review_job_symbols s JOIN review_jobs j ON j.id=s.job_id WHERE j.status='failed' AND s.symbol=d.symbol AND s.dirty_revision=d.revision) ORDER BY d.symbol",vec![])).await?.into_iter().map(|r|r.try_get("","symbol")).collect::<Result<Vec<String>,_>>()?;
            if selected.is_empty() {
                return Ok(None);
            }
            self.create_job(selected, "incremental").await?
        };
        job.status = "running".into();
        job.updated_at = now();
        self.db
            .execute(sql(
                "UPDATE review_jobs SET status='running',data=? WHERE id=?",
                vec![serde_json::to_string(&job)?.into(), job.id.clone().into()],
            ))
            .await?;
        let symbol=self.db.query_one(sql("SELECT symbol FROM review_job_symbols WHERE job_id=? AND done=0 ORDER BY symbol LIMIT 1",vec![job.id.clone().into()])).await?;
        if let Some(row) = symbol {
            let symbol: String = row.try_get("", "symbol")?;
            let calendar: String = self
                .db
                .query_one(sql(
                    "SELECT calendar_json FROM review_jobs WHERE id=?",
                    vec![job.id.clone().into()],
                ))
                .await?
                .unwrap()
                .try_get("", "calendar_json")?;
            let mut build_res = None;
            for attempt in 0..4 {
                match self
                    .build_symbol(&job, &symbol, serde_json::from_str(&calendar)?)
                    .await
                {
                    Ok(res) => {
                        build_res = Some(Ok(res));
                        break;
                    }
                    Err(e) => {
                        let err_str = format!("{e:#}");
                        let is_busy = err_str.contains("locked") || err_str.contains("busy") || err_str.contains("code 5");
                        if is_busy && attempt < 3 {
                            tracing::warn!(symbol=%symbol, attempt, "复盘品种计算遇到数据库锁冲突，退避重试: {err_str}");
                            tokio::time::sleep(std::time::Duration::from_millis(150 * (1 << attempt))).await;
                            continue;
                        }
                        build_res = Some(Err(e));
                        break;
                    }
                }
            }
            match build_res.unwrap() {
                Ok((count, coverage)) => {
                    job.processed_signals += count;
                    job.completed_symbols += 1;
                    job.updated_at = now();
                    for (key, n) in coverage {
                        *job.coverage.entry(key).or_default() += n;
                    }
                    let mut backoff = 50;
                    for attempt in 0..5 {
                        let tx_res = self.db.begin().await;
                        match tx_res {
                            Ok(tx) => {
                                let r1 = tx.execute(sql(
                                    "UPDATE review_job_symbols SET done=1 WHERE job_id=? AND symbol=?",
                                    vec![job.id.clone().into(), symbol.clone().into()],
                                )).await;
                                let r2 = tx.execute(sql(
                                    "UPDATE review_jobs SET data=? WHERE id=?",
                                    vec![serde_json::to_string(&job)?.into(), job.id.clone().into()],
                                )).await;
                                if r1.is_ok() && r2.is_ok() {
                                    if tx.commit().await.is_ok() {
                                        break;
                                    }
                                }
                            }
                            Err(_) => {}
                        }
                        if attempt < 4 {
                            tokio::time::sleep(std::time::Duration::from_millis(backoff)).await;
                            backoff *= 2;
                        }
                    }
                }
                Err(e) => {
                    job.status = "failed".into();
                    job.errors.push(format!("{symbol}: {e:#}"));
                    job.updated_at = now();
                    self.db
                        .execute(sql(
                            "UPDATE review_jobs SET status='failed',data=? WHERE id=?",
                            vec![serde_json::to_string(&job)?.into(), job.id.clone().into()],
                        ))
                        .await?;
                    // Keep durable dirty entries for an explicit retry; avoid an endless failed-job loop.
                    return Ok(Some(job));
                }
            }
        }
        if job.completed_symbols == job.total_symbols {
            let mut backoff = 50;
            for attempt in 0..5 {
                let tx_res = self.db.begin().await;
                let tx = match tx_res {
                    Ok(tx) => tx,
                    Err(e) => {
                        if attempt < 4 {
                            tokio::time::sleep(std::time::Duration::from_millis(backoff)).await;
                            backoff *= 2;
                            continue;
                        }
                        return Err(e.into());
                    }
                };
                let res = async {
                    tx.execute(sql("DELETE FROM review_current WHERE event_id IN (SELECT c.event_id FROM review_current c JOIN review_results r ON r.result_version=c.result_version WHERE r.symbol IN (SELECT symbol FROM review_job_symbols WHERE job_id=?))",vec![job.id.clone().into()])).await?;
                    tx.execute(sql("INSERT INTO review_current(event_id,result_version) SELECT event_id,result_version FROM review_staging WHERE job_id=?",vec![job.id.clone().into()])).await?;
                    tx.execute(sql("DELETE FROM review_dirty WHERE EXISTS(SELECT 1 FROM review_job_symbols j WHERE j.job_id=? AND j.symbol=review_dirty.symbol AND j.dirty_revision=review_dirty.revision)",vec![job.id.clone().into()])).await?;
                    tx.execute(sql(
                        "UPDATE review_state SET revision=revision+1 WHERE id=1",
                        vec![],
                    ))
                    .await?;
                    let rev = tx
                        .query_one(sql("SELECT revision FROM review_state WHERE id=1", vec![]))
                        .await?
                        .unwrap()
                        .try_get("", "revision")?;
                    job.status = "completed".into();
                    job.revision = Some(rev);
                    job.updated_at = now();
                    tx.execute(sql(
                        "UPDATE review_jobs SET status='completed',data=? WHERE id=?",
                        vec![serde_json::to_string(&job)?.into(), job.id.clone().into()],
                    ))
                    .await?;
                    tx.execute(sql(
                        "DELETE FROM review_staging WHERE job_id=?",
                        vec![job.id.clone().into()],
                    ))
                    .await?;
                    tx.commit().await?;
                    Ok::<i64, anyhow::Error>(rev)
                }.await;

                match res {
                    Ok(rev) => {
                        tracing::info!(job_id=%job.id,revision=rev,signals=job.processed_signals,"review generation published");
                        break;
                    }
                    Err(e) => {
                        let s = format!("{e:#}");
                        if (s.contains("locked") || s.contains("busy") || s.contains("code 5")) && attempt < 4 {
                            tokio::time::sleep(std::time::Duration::from_millis(backoff)).await;
                            backoff *= 2;
                            continue;
                        }
                        return Err(e);
                    }
                }
            }
        }
        Ok(Some(job))
    }

    async fn build_symbol(
        &self,
        job: &ReviewJob,
        symbol: &str,
        calendar: CalendarInput,
    ) -> Result<(usize, BTreeMap<String, usize>)> {
        // Read all inputs from the same SQLite snapshot, then release before CPU work.
        let tx = self.db.begin().await?;
        let events = pattern_events::Entity::find()
            .filter(pattern_events::Column::Symbol.eq(symbol))
            .order_by_asc(pattern_events::Column::WarningTs)
            .order_by_asc(pattern_events::Column::Id)
            .all(&tx)
            .await?;
        let raw_rows = klines::Entity::find()
            .filter(klines::Column::Symbol.eq(symbol))
            .filter(klines::Column::Timeframe.eq("5m"))
            .filter(klines::Column::Source.eq("raw"))
            .order_by_asc(klines::Column::Ts)
            .all(&tx)
            .await?;
        let bars_rows = klines::Entity::find()
            .filter(klines::Column::Symbol.eq(symbol))
            .filter(klines::Column::Timeframe.eq("15m"))
            .order_by_asc(klines::Column::Ts)
            .all(&tx)
            .await?;
        let roll = rollovers::Entity::find()
            .filter(rollovers::Column::Symbol.eq(symbol))
            .filter(rollovers::Column::Confirmed.eq(true))
            .all(&tx)
            .await?;
        let sym = symbols::Entity::find_by_id(symbol.to_string())
            .one(&tx)
            .await?;
        let origins=tx.query_all(sql("SELECT event_id,origin FROM review_event_origins WHERE event_id IN (SELECT id FROM pattern_events WHERE symbol=?)",vec![symbol.into()])).await?.into_iter().map(|r|Ok((r.try_get::<i64>("","event_id")?,r.try_get::<String>("","origin")?))).collect::<Result<HashMap<_,_>>>()?;
        tx.commit().await?;
        let cutoff = (Utc::now() + Duration::hours(8))
            .format("%Y-%m-%d %H:%M:%S")
            .to_string();
        let raw: Vec<Kline> = raw_rows
            .into_iter()
            .filter(|r| r.ts <= cutoff)
            .map(|r| Kline {
                datetime: r.ts,
                open: r.open,
                high: r.high,
                low: r.low,
                close: r.close,
                volume: r.volume,
                hold: r.hold,
            })
            .collect();
        let roll: HashSet<String> = roll.into_iter().map(|r| r.ts).collect();
        let bars: Vec<Bar> = bars_rows
            .into_iter()
            .filter_map(|r| {
                Some(Bar {
                    dt: DT::from_bar_ts(&r.ts)?,
                    rollover: roll.contains(&r.ts),
                    open: r.open,
                    high: r.high,
                    low: r.low,
                    close: r.close,
                    volume: r.volume,
                    hold: r.hold,
                })
            })
            .collect();
        let variety = sym.as_ref().map(|s| s.variety.as_str()).unwrap_or("");
        let prefix = crate::session::SessionCalendar::contract_prefix(symbol);
        let multiplier = crate::precision::DEFAULT_MULTIPLIERS
            .iter()
            .find(|(k, _)| k.to_ascii_uppercase() == prefix || !variety.is_empty() && *k == variety)
            .map(|(_, v)| *v);
        let known_tick = crate::precision::DEFAULT_TICKS
            .iter()
            .find(|(k, _)| k.to_ascii_uppercase() == prefix || !variety.is_empty() && *k == variety)
            .map(|(_, v)| *v);
        let tick = sym
            .as_ref()
            .filter(|s| s.tick_size.is_finite() && s.tick_size > 0.)
            .map(|s| s.tick_size)
            .or(known_tick);
        let tick_val = tick.unwrap_or(1.);
        let events = resolve_events_for_replay(symbol, events, &bars, tick_val);
        let origin = job.origin.clone();
        let batch = tokio::task::spawn_blocking(move || {
            build_batch(
                events, raw, bars, roll, calendar, tick, multiplier, &origin, origins,
            )
        })
        .await??;
        let mut coverage = BTreeMap::new();
        for chunk in batch.chunks(20) {
            let mut backoff = 50;
            for attempt in 0..5 {
                let tx_res = self.db.begin().await;
                match tx_res {
                    Ok(tx) => {
                        let mut chunk_err = None;
                        for (detail, holding) in chunk {
                            if let Err(e) = Self::save_result_tx(&tx, &job.id, detail, holding).await {
                                chunk_err = Some(e);
                                break;
                            }
                        }
                        if let Some(e) = chunk_err {
                            let _ = tx.rollback().await;
                            let s = format!("{e:#}");
                            if (s.contains("locked") || s.contains("busy") || s.contains("code 5")) && attempt < 4 {
                                tokio::time::sleep(std::time::Duration::from_millis(backoff)).await;
                                backoff *= 2;
                                continue;
                            }
                            return Err(e);
                        }
                        match tx.commit().await {
                            Ok(_) => {
                                for (detail, _) in chunk {
                                    for key in &detail.summary.coverage {
                                        *coverage.entry(key.clone()).or_default() += 1;
                                    }
                                    *coverage.entry(detail.summary.state.clone()).or_default() += 1;
                                }
                                break;
                            }
                            Err(e) => {
                                let s = format!("{e:#}");
                                if (s.contains("locked") || s.contains("busy") || s.contains("code 5")) && attempt < 4 {
                                    tokio::time::sleep(std::time::Duration::from_millis(backoff)).await;
                                    backoff *= 2;
                                    continue;
                                }
                                return Err(e.into());
                            }
                        }
                    }
                    Err(e) => {
                        let s = format!("{e:#}");
                        if (s.contains("locked") || s.contains("busy") || s.contains("code 5")) && attempt < 4 {
                            tokio::time::sleep(std::time::Duration::from_millis(backoff)).await;
                            backoff *= 2;
                            continue;
                        }
                        return Err(e.into());
                    }
                }
            }
        }
        Ok((batch.len(), coverage))
    }

    async fn save_result_tx(
        tx: &sea_orm::DatabaseTransaction,
        job_id: &str,
        detail: &ReviewResultDetail,
        holding: &[ReviewHoldingPoint],
    ) -> Result<()> {
        let s = &detail.summary;
        if tx
            .query_one(sql(
                "SELECT 1 AS present FROM review_results WHERE result_version=?",
                vec![s.result_version.clone().into()],
            ))
            .await?
            .is_some()
        {
            tx.execute(sql("INSERT INTO review_staging(job_id,event_id,result_version) VALUES (?,?,?) ON CONFLICT(job_id,event_id) DO UPDATE SET result_version=excluded.result_version",vec![job_id.into(),s.event_id.into(),s.result_version.clone().into()])).await?;
            return Ok(());
        }
        let mut head = detail.clone();
        head.lifecycle.clear();
        tx.execute(sql("INSERT OR IGNORE INTO review_results(result_version,event_id,symbol,family_id,warning_ts,entry_ts,exit_ts,direction,level,grade,state,exit_reason,terminal_reason,old,quality_v1,morphology_v1,quality_v2,morphology_v2,a_score,b_score,score_status,gross_amount,net_amount,gross_r,net_r,mfe_r,mae_r,bars_held,summary_json,detail_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            vec![s.result_version.clone().into(),s.event_id.into(),s.symbol.clone().into(),s.family_id.into(),s.warning_ts.clone().into(),s.entry_ts.clone().into(),s.exit_ts.clone().into(),s.direction.clone().into(),s.level.clone().into(),s.grade.clone().into(),s.state.clone().into(),s.exit_reason.clone().into(),s.terminal_reason.clone().into(),
                s.scores.get("old").copied().flatten().into(),s.scores.get("quality_v1").copied().flatten().into(),s.scores.get("morphology_v1").copied().flatten().into(),s.scores.get("quality_v2").copied().flatten().into(),s.scores.get("morphology_v2").copied().flatten().into(),s.scores.get("a_score").copied().flatten().into(),s.scores.get("b_score").copied().flatten().into(),s.score_status.clone().into(),s.gross_amount.into(),s.net_amount.into(),s.gross_r.into(),s.net_r.into(),s.mfe_r.into(),s.mae_r.into(),(s.bars_held as i64).into(),serde_json::to_string(s)?.into(),serde_json::to_string(&head)?.into()])).await?;
        for n in &detail.lifecycle {
            tx.execute(sql(
                "INSERT OR IGNORE INTO review_nodes(result_version,sequence,data) VALUES (?,?,?)",
                vec![
                    s.result_version.clone().into(),
                    (n.sequence as i64).into(),
                    serde_json::to_string(n)?.into(),
                ],
            ))
            .await?;
        }
        for chunk in holding.chunks(100) {
            let placeholders = vec!["(?,?,?)"; chunk.len()].join(",");
            let mut values = vec![];
            for p in chunk {
                values.extend([
                    s.result_version.clone().into(),
                    (p.sequence as i64).into(),
                    serde_json::to_string(p)?.into(),
                ]);
            }
            tx.execute(sql(&format!("INSERT OR IGNORE INTO review_holding(result_version,sequence,data) VALUES {placeholders}"),values)).await?;
        }
        tx.execute(sql("INSERT INTO review_staging(job_id,event_id,result_version) VALUES (?,?,?) ON CONFLICT(job_id,event_id) DO UPDATE SET result_version=excluded.result_version",vec![job_id.into(),s.event_id.into(),s.result_version.clone().into()])).await?;
        Ok(())
    }

    pub async fn save_result(
        &self,
        job_id: &str,
        detail: &ReviewResultDetail,
        holding: &[ReviewHoldingPoint],
    ) -> Result<()> {
        let tx = self.db.begin().await?;
        Self::save_result_tx(&tx, job_id, detail, holding).await?;
        tx.commit().await?;
        Ok(())
    }

    pub async fn detail(&self, event_id: i64, version: &str) -> Result<ReviewResultDetail> {
        let row = self
            .db
            .query_one(sql(
                "SELECT detail_json FROM review_results WHERE event_id=? AND result_version=?",
                vec![event_id.into(), version.into()],
            ))
            .await?
            .ok_or_else(|| super::error("NOT_FOUND", "review result not found"))?;
        let mut detail: ReviewResultDetail =
            serde_json::from_str(&row.try_get::<String>("", "detail_json")?)?;
        detail.lifecycle = self
            .db
            .query_all(sql(
                "SELECT data FROM review_nodes WHERE result_version=? ORDER BY sequence",
                vec![version.into()],
            ))
            .await?
            .into_iter()
            .map(|r| Ok(serde_json::from_str(&r.try_get::<String>("", "data")?)?))
            .collect::<Result<_>>()?;
        Ok(detail)
    }

    /// Resolve a legacy event-only link to a published immutable head, never to a profile name.
    pub async fn current_detail(&self, event_id: i64) -> Result<ReviewResultDetail> {
        let row = self.db
            .query_one(sql(
                "SELECT result_version FROM review_current WHERE event_id=?",
                vec![event_id.into()],
            ))
            .await?
            .ok_or_else(|| super::error("NOT_FOUND", "该信号尚无已发布复盘结果，请检查复盘任务状态"))?;
        self.detail(event_id, &row.try_get::<String>("", "result_version")?)
            .await
    }

    pub async fn holding(
        &self,
        event_id: i64,
        version: &str,
        cursor: usize,
        limit: usize,
    ) -> Result<ReviewHoldingPage> {
        anyhow::ensure!((1..=500).contains(&limit), "holding limit must be 1..500");
        let total=self.db.query_one(sql("SELECT json_extract(detail_json,'$.holding_count') AS n FROM review_results WHERE event_id=? AND result_version=?",vec![event_id.into(),version.into()])).await?.ok_or_else(||super::error("NOT_FOUND","review result not found"))?.try_get::<i64>("","n")? as usize;
        let items=self.db.query_all(sql("SELECT data FROM review_holding WHERE result_version=? AND sequence>=? ORDER BY sequence LIMIT ?",vec![version.into(),(cursor as i64).into(),(limit as i64).into()])).await?.into_iter().map(|r|Ok(serde_json::from_str(&r.try_get::<String>("","data")?)?)).collect::<Result<Vec<_>>>()?;
        let next = cursor.saturating_add(items.len());
        Ok(ReviewHoldingPage {
            event_id,
            result_version: version.into(),
            total,
            items,
            next_cursor: (next < total).then_some(next),
        })
    }
}

pub fn resolve_events_for_replay(
    symbol: &str,
    existing_events: Vec<pattern_events::Model>,
    bars: &[Bar],
    tick: f64,
) -> Vec<pattern_events::Model> {
    if bars.len() < 30 {
        return existing_events;
    }
    let candidates = crate::analyze::event::replay_warnings(symbol, bars, tick);
    if candidates.is_empty() {
        return existing_events;
    }
    let mut by_key: HashMap<(String, String), pattern_events::Model> = HashMap::new();
    for e in existing_events {
        by_key.insert((e.direction.clone(), e.warning_ts.clone()), e);
    }
    let mut resolved = Vec::with_capacity(candidates.len());
    for c in candidates {
        let is_long = c.direction == crate::analyze::model::Dir::Up;
        let dir_str = if is_long { "up" } else { "down" };
        let warning_ts = bars[c.warning_index].dt.to_bar_ts();
        let key = (dir_str.to_string(), warning_ts.clone());
        if let Some(mut existing) = by_key.remove(&key) {
            existing.entry = c.entry;
            existing.stop = c.stop;
            existing.target = c.target;
            existing.risk = c.risk;
            existing.rr = c.rr;
            resolved.push(existing);
        } else {
            let s0_ts = bars[c.s0_index].dt.to_bar_ts();
            let s1_ts = bars[c.s1_index].dt.to_bar_ts();
            let s2_ts = bars[c.s2_index].dt.to_bar_ts();
            let s0_price = if is_long { bars[c.s0_index].low } else { bars[c.s0_index].high };
            let s1_price = if is_long { bars[c.s1_index].high } else { bars[c.s1_index].low };
            let s2_price = if is_long { bars[c.s2_index].low } else { bars[c.s2_index].high };
            let hash_key = format!("{symbol}:{dir_str}:{warning_ts}:{s0_ts}:{s1_ts}:{s2_ts}");
            let hash = blake3::hash(hash_key.as_bytes());
            let id = (u64::from_le_bytes(hash.as_bytes()[0..8].try_into().unwrap()) & 0x7FFF_FFFF_FFFF_FFFF) as i64;
            let dims = json!({
                "dim_a": c.dim_a,
                "dim_b": c.dim_b,
                "dim_warning": c.dim_warning,
            });
            resolved.push(pattern_events::Model {
                id,
                symbol: symbol.to_string(),
                direction: dir_str.to_string(),
                grade: c.grade.clone(),
                level: c.level.to_string(),
                s0_ts,
                s0_price,
                s1_ts,
                s1_price,
                s2_ts,
                s2_price,
                a_move: c.a_move,
                b_move: c.b_move,
                a_bars: c.a_bars as i64,
                b_bars: c.b_bars as i64,
                retracement: c.retracement,
                warning_ts: warning_ts.clone(),
                detected_at: warning_ts,
                warning_kind: c.warning_kind.to_string(),
                entry_score: c.entry_score,
                entry_score_dims: dims.to_string(),
                entry: c.entry,
                stop: c.stop,
                target: c.target,
                risk: c.risk,
                rr: c.rr,
                state: "pending".to_string(),
                last_advance_ts: None,
                trigger_ts: None,
                trigger_bar_ts: None,
                trigger_price: None,
                trigger_score: None,
                trigger_volume_ratio: None,
                overshoot_r: None,
                hold_score: None,
                hold_score_history: "[]".to_string(),
                outcome: None,
                exit_reason: None,
                exit_ts: None,
                exit_price: None,
                r_multiple: None,
                mfe_r: None,
                mae_r: None,
                created_at: String::new(),
                updated_at: String::new(),
            });
        }
    }
    for (_, remaining) in by_key {
        resolved.push(remaining);
    }
    resolved.sort_by(|a, b| a.warning_ts.cmp(&b.warning_ts).then_with(|| a.id.cmp(&b.id)));
    resolved
}

pub fn build_batch(
    events: Vec<pattern_events::Model>,
    raw: Vec<Kline>,
    bars: Vec<Bar>,
    roll: HashSet<String>,
    calendar: CalendarInput,
    tick: Option<f64>,
    multiplier: Option<f64>,
    origin: &str,
    origins: HashMap<i64, String>,
) -> Result<Vec<(ReviewResultDetail, Vec<ReviewHoldingPoint>)>> {
    let bar_index: HashMap<_, _> = bars
        .iter()
        .enumerate()
        .map(|(i, b)| (b.dt.to_bar_ts(), i))
        .collect();
    let mut families: Vec<(usize, String)> = vec![];
    let mut family_ids = vec![];
    for (i, e) in events.iter().enumerate() {
        let mut family = e.id;
        for (anchor, last) in &mut families {
            let a = &events[*anchor];
            let gap = bar_index
                .get(&e.warning_ts)
                .zip(bar_index.get(last))
                .map(|(x, y)| x.abs_diff(*y))
                .or_else(|| {
                    let parse = |s: &str| {
                        chrono::NaiveDateTime::parse_from_str(s, "%Y-%m-%d %H:%M:%S")
                            .or_else(|_| chrono::NaiveDateTime::parse_from_str(s, "%Y-%m-%d %H:%M"))
                            .ok()
                    };
                    parse(&e.warning_ts)
                        .zip(parse(last))
                        .map(|(a, b)| ((a - b).num_minutes().max(0) / 15) as usize)
                });
            let version = |e: &pattern_events::Model| {
                serde_json::from_str::<serde_json::Value>(&e.entry_score_dims).unwrap_or_default()
                    ["event_logic_version"]
                    .to_string()
            };
            if a.direction == e.direction
                && version(a) == version(e)
                && (a.entry - e.entry).abs() <= 0.3 * a.risk.max(1e-9)
                && gap.is_some_and(|n| n <= 5)
            {
                family = a.id;
                *last = e.warning_ts.clone();
                break;
            }
        }
        if family == e.id {
            families.push((i, e.warning_ts.clone()));
        }
        family_ids.push(family);
    }
    let mut family_sizes = HashMap::<i64, usize>::new();
    for id in &family_ids {
        *family_sizes.entry(*id).or_default() += 1;
    }
    let rules = crate::quality::Rules::default();
    let params=blake3::hash(&serde_json::to_vec(&json!({"profile":PROFILE,"implementation":implementation_version(),"cost":COST_VERSION,"rules":rules,"candidate":crate::quality::score_scan::PROFILE,"tick":tick,"multiplier":multiplier}))?).to_hex().to_string();
    let calendar_version = calendar.version();
    let recorded = now();
    let mut batch = vec![];
    for (i, event) in events.into_iter().enumerate() {
        let origin = if origin == "historical_rebuild" {
            origin
        } else {
            origins
                .get(&event.id)
                .map(|s| s.as_str())
                .unwrap_or("historical_rebuild")
        };
        let window = crate::quality::review::warning_window(&event, &bars, &rules);
        let quality = crate::quality::evaluate(
            &event,
            window,
            tick.unwrap_or(0.),
            &event.warning_ts,
            "review_recompute",
            &rules,
        );
        let candidate = crate::quality::review::score(&event, window, &quality, tick.unwrap_or(0.));
        let scores = BTreeMap::from([
            ("old".into(), Some(event.entry_score).filter(|v| v.is_finite())),
            ("quality_v1".into(), quality.quality_score.filter(|v| v.is_finite())),
            ("morphology_v1".into(), quality.morphology_score.filter(|v| v.is_finite())),
            ("quality_v2".into(), candidate.quality_score.filter(|v| v.is_finite())),
            ("morphology_v2".into(), candidate.morphology_score.filter(|v| v.is_finite())),
            ("a_score".into(), candidate.a_score.filter(|v| v.is_finite())),
            ("b_score".into(), candidate.b_score.filter(|v| v.is_finite())),
        ]);
        let policy = replay::EligibilityPolicy::ObserveAll;
        let start = raw.partition_point(|b| b.datetime < event.warning_ts);
        let mut output = replay::run_with_policy(
            &event,
            &raw[start..],
            &roll,
            &calendar,
            tick,
            multiplier,
            origin,
            &recorded,
            &policy,
            &scores,
            Some(&candidate.status),
            &candidate.explanations,
        );
        output.summary.family_id = family_ids[i];
        output.summary.family_size = family_sizes[&family_ids[i]];
        if !bar_index.contains_key(&event.warning_ts) {
            output.summary.coverage.push("dedup_time_fallback".into());
        }
        if candidate.status != "ok" {
            output
                .summary
                .coverage
                .push(format!("score_{}", candidate.status));
        }
        output.summary.diagnostics = BTreeMap::from([
            ("retracement".into(), Some(event.retracement)),
            (
                "b_a_speed".into(),
                (event.a_move > 0. && event.a_bars > 0 && event.b_bars > 0).then(|| {
                    (event.b_move / event.b_bars as f64) / (event.a_move / event.a_bars as f64)
                }),
            ),
            ("vol_ratio".into(), None),
            ("overshoot".into(), None),
            (
                "dim_a".into(),
                serde_json::from_str::<serde_json::Value>(&event.entry_score_dims)
                    .unwrap_or_default()["dim_a"]
                    .as_f64(),
            ),
            (
                "dim_b".into(),
                serde_json::from_str::<serde_json::Value>(&event.entry_score_dims)
                    .unwrap_or_default()["dim_b"]
                    .as_f64(),
            ),
            (
                "dim_warning".into(),
                serde_json::from_str::<serde_json::Value>(&event.entry_score_dims)
                    .unwrap_or_default()["dim_warning"]
                    .as_f64(),
            ),
        ]);
        let mean_volume = |start: &str, end: &str| {
            let values: Vec<_> = bars
                .iter()
                .filter(|b| {
                    let t = b.dt.to_bar_ts();
                    t.as_str() >= start && t.as_str() <= end && b.volume > 0.
                })
                .map(|b| b.volume)
                .collect();
            (!values.is_empty()).then(|| values.iter().sum::<f64>() / values.len() as f64)
        };
        let b_vol = mean_volume(&event.s0_ts, &event.s1_ts)
            .zip(mean_volume(&event.s1_ts, &event.s2_ts))
            .filter(|(a, _)| *a > 0.)
            .map(|(a, b)| b / a);
        output
            .summary
            .diagnostics
            .insert("b_vol_ratio".into(), b_vol);
        output.summary.diagnostics.insert(
            "a_move_atr".into(),
            quality.features.get("a_move_atr").copied().flatten(),
        );
        output.summary.diagnostics.insert(
            "trigger_lag".into(),
            output
                .summary
                .entry_ts
                .as_ref()
                .map(|_| output.summary.wait_bars as f64),
        );
        if let Some(index) = output
            .summary
            .entry_ts
            .as_ref()
            .and_then(|t| raw.iter().position(|b| &b.datetime == t))
        {
            let b = &raw[index];
            let volume = if index >= 20
                && b.volume > 0.
                && raw[index - 20..index].iter().all(|b| b.volume > 0.)
            {
                Some(
                    b.volume / (raw[index - 20..index].iter().map(|b| b.volume).sum::<f64>() / 20.),
                )
            } else {
                None
            };
            let favorable = if event.direction == "down" {
                event.entry - b.low
            } else {
                b.high - event.entry
            };
            output
                .summary
                .diagnostics
                .insert("vol_ratio".into(), volume);
            output.summary.diagnostics.insert(
                "overshoot".into(),
                (event.risk > 0.).then(|| (favorable / event.risk).max(0.)),
            );
            output.summary.diagnostics.insert(
                "oi_increase".into(),
                (index > 0 && b.hold > 0. && raw[index.saturating_sub(1)].hold > 0.).then(|| {
                    if b.hold > raw[index - 1].hold {
                        1.
                    } else {
                        0.
                    }
                }),
            );
            let gap = output
                .nodes
                .iter()
                .find(|n| n.stage == "trigger")
                .and_then(|n| n.evidence["gap"].as_bool())
                .map(|b| if b { 1. } else { 0. });
            output.summary.diagnostics.insert("gap_entry".into(), gap);
        }
        let gap_exit = output
            .summary
            .exit_ts
            .as_ref()
            .and_then(|t| raw.iter().find(|b| &b.datetime == t))
            .map(|b| {
                if output.summary.exit_reason.as_deref() == Some("stop")
                    && (b.open - event.stop) * (if event.direction == "down" { -1. } else { 1. })
                        < 0.
                {
                    1.
                } else {
                    0.
                }
            });
        output
            .summary
            .diagnostics
            .insert("gap_exit".into(), gap_exit);
        if let Some(n) = output.nodes.iter_mut().find(|n| n.stage == "trigger") {
            n.evidence["diagnostics"] = json!(output.summary.diagnostics);
            n.evidence["diagnostic_basis"] = json!({"volume_timeframe":"5m","volume_lookback":20,"a_strength":"warning_frozen_a_move_atr","b_volume_timeframe":"15m"});
        }
        for node in output.nodes.iter_mut().take(2) {
            let score = if node.stage == "a_segment" {
                candidate.a_score
            } else {
                candidate.b_score
            };
            node.evidence["review_score"] = json!(score);
            node.evidence["score_version"] = json!(candidate.candidate_version);
            node.evidence["score_explanations"] = json!(candidate.explanations);
            let module = if node.stage == "a_segment" {
                "a_process"
            } else {
                "b_process"
            };
            if let Some(evidence) = candidate
                .evidence
                .as_ref()
                .and_then(|e| e["modules"].get(module))
            {
                node.evidence["process"] = evidence.clone();
                if let Some(reading) = evidence["reading"].as_str() {
                    node.comment.push_str(&format!("；{reading}"));
                }
            }
            node.comment.push_str(&format!(
                "；复盘分{}（0–100，预警时点历史重算）",
                score
                    .map(|s| format!("{s:.2}"))
                    .unwrap_or_else(|| "缺失".into())
            ));
        }
        let through = output
            .summary
            .exit_ts
            .as_deref()
            .or_else(|| {
                output
                    .nodes
                    .last()
                    .filter(|n| n.stage == "terminal")
                    .map(|n| n.occurred_at.as_str())
            })
            .unwrap_or_else(|| {
                raw.last()
                    .map(|b| b.datetime.as_str())
                    .unwrap_or(&event.warning_ts)
            });
        let end = raw
            .partition_point(|b| b.datetime.as_str() <= through)
            .max(start);
        let raw_hash = blake3::hash(&serde_json::to_vec(&raw[start..end])?)
            .to_hex()
            .to_string();
        let mut roll_sorted: Vec<_> = roll
            .iter()
            .filter(|t| t.as_str() > event.warning_ts.as_str() && t.as_str() <= through)
            .collect();
        roll_sorted.sort();
        let input_hash=blake3::hash(&serde_json::to_vec(&json!({"event":event,"raw":raw_hash,"rollovers":roll_sorted,"quality_input":quality.input_hash,"diagnostics":output.summary.diagnostics,"calendar":calendar_version,"params":params,"family_id":output.summary.family_id,"family_size":output.summary.family_size,"origin":origin}))?).to_hex().to_string();
        let version = format!("{}-{}", event.id, input_hash);
        output.summary.result_version = version;
        let cost = json!({"estimated":true,"currency":"CNY","settled":output.summary.state=="closed",
            "entry_slippage_ticks":1,"exit_slippage_ticks":1,"additional_roundtrip_fee_ticks":0.5,"total_friction_ticks":2.5,"tick_size":tick,"volume":1,
            "entry_slippage_amount":output.entry_price.and(tick.zip(multiplier).map(|(t,m)|t*m)),
            "exit_slippage_amount":output.exit_price.and(tick.zip(multiplier).map(|(t,m)|t*m)),
            "additional_fee_amount":(output.summary.state=="closed").then(||tick.zip(multiplier).map(|(t,m)|0.5*t*m)).flatten()});
        let detail = ReviewResultDetail {
            summary: output.summary,
            profile: PROFILE.into(),
            cost_version: COST_VERSION.into(),
            source_minutes: 5,
            origin: origin.into(),
            input_hash,
            calendar_version: calendar_version.clone(),
            parameter_hash: params.clone(),
            computed_at: recorded.clone(),
            multiplier,
            multiplier_source: if multiplier.is_some() {
                "bundled_product_table"
            } else {
                "unknown"
            }
            .into(),
            entry_price: output.entry_price,
            exit_price: output.exit_price,
            cost,
            original_event: serde_json::to_value(&event)?,
            quality: Some(quality),
            review_scores: Some(candidate),
            lifecycle: output.nodes,
            mfe: output.mfe,
            mae: output.mae,
            holding_count: output.holding.len(),
        };
        batch.push((detail, output.holding));
    }
    Ok(batch)
}
