use super::{
    store::{revision, sql},
    ReviewEngine, Snapshot,
};
use anyhow::Result;
use chrono::{Datelike, NaiveDateTime, NaiveTime, Utc};
use n_protocol::review::*;
use sea_orm::ConnectionTrait;
use std::{collections::BTreeMap, sync::Arc, time::Instant};

pub(crate) const SCORES: &[&str] = &[
    "old",
    "quality_v1",
    "morphology_v1",
    "quality_v2",
    "morphology_v2",
    "a_score",
    "b_score",
];
pub(crate) const DIMENSIONS: &[&str] = &[
    "score_band",
    "a_score_band",
    "b_score_band",
    "symbol",
    "direction",
    "grade",
    "level",
    "hour",
    "session",
    "weekday",
    "date",
    "holding_band",
    "exit_reason",
    "state",
    "symbol_hour",
    "vol_confirm",
    "vol_band",
    "b_vol",
    "retracement",
    "b_a_speed",
    "a_strength",
    "trigger_lag",
    "overshoot",
    "gap_combo",
    "dim_a",
    "dim_b",
    "dim_warning",
    "trend60",
    "oi",
    "tp_tier",
];
const SORTS: &[&str] = &[
    "score",
    "time",
    "gross_amount",
    "net_amount",
    "gross_r",
    "net_r",
    "mfe_r",
    "mae_r",
    "bars_held",
];

fn datetime(t: &str) -> Result<()> {
    let parsed = NaiveDateTime::parse_from_str(t, "%Y-%m-%d %H:%M:%S")?;
    anyhow::ensure!(
        parsed.format("%Y-%m-%d %H:%M:%S").to_string() == t,
        "invalid timestamp"
    );
    Ok(())
}
fn hour(t: &str) -> Result<()> {
    let parsed = NaiveTime::parse_from_str(t, "%H:%M")?;
    anyhow::ensure!(
        parsed.format("%H:%M").to_string() == t,
        "invalid session time"
    );
    Ok(())
}
fn session_matches(time: &str, start: &str, end: &str) -> bool {
    if start > end {
        time >= start || time < end
    } else {
        time >= start && time < end
    }
}

pub(crate) fn validate(q: &ReviewQuery) -> Result<()> {
    let f = &q.filters;
    anyhow::ensure!(
        SCORES.contains(&f.score_metric.as_deref().unwrap_or("quality_v2")),
        "unknown score metric"
    );
    anyhow::ensure!(
        SORTS.contains(&q.sort_by.as_deref().unwrap_or("time")),
        "unknown sort field"
    );
    anyhow::ensure!(
        (1..=500).contains(&q.page_size.unwrap_or(100)),
        "page_size must be 1..500"
    );
    anyhow::ensure!(
        ["warning", "entry", "exit"].contains(&f.time_field.as_deref().unwrap_or("warning")),
        "unknown time field"
    );
    anyhow::ensure!(
        ["deduplicated", "all"].contains(&f.sample_scope.as_deref().unwrap_or("deduplicated")),
        "unknown sample scope"
    );
    anyhow::ensure!(
        ["all", "scored", "unscored"].contains(&f.score_status.as_deref().unwrap_or("all")),
        "unknown score status"
    );
    let max = if f.score_metric.as_deref() == Some("old") {
        5.
    } else {
        100.
    };
    anyhow::ensure!(
        [f.score_min, f.score_max]
            .into_iter()
            .flatten()
            .all(|x| x.is_finite() && x >= 0. && x <= max),
        "score outside metric range"
    );
    anyhow::ensure!(
        !f.score_min.zip(f.score_max).is_some_and(|(a, b)| a > b),
        "score minimum exceeds maximum"
    );
    for t in [&f.time_start, &f.time_end].into_iter().flatten() {
        datetime(t)?;
    }
    anyhow::ensure!(
        !f.time_start
            .as_ref()
            .zip(f.time_end.as_ref())
            .is_some_and(|(a, b)| a >= b),
        "empty or reversed time interval"
    );
    anyhow::ensure!(f.sessions.len() <= 24, "too many sessions");
    for s in &f.sessions {
        hour(&s.start)?;
        hour(&s.end)?;
        anyhow::ensure!(s.start != s.end, "empty session interval");
    }
    for list in [
        &f.symbols,
        &f.directions,
        &f.levels,
        &f.grades,
        &f.states,
        &f.outcomes,
        &f.exit_reasons,
        &f.terminal_reasons,
    ] {
        anyhow::ensure!(
            list.len() <= 500 && list.iter().all(|s| !s.is_empty() && s.len() <= 64),
            "invalid filter values"
        );
    }
    anyhow::ensure!(
        f.directions
            .iter()
            .all(|d| matches!(d.as_str(), "up" | "down")),
        "unknown direction"
    );
    anyhow::ensure!(
        f.outcomes.iter().all(|s| matches!(s.as_str(), "win" | "loss" | "flat")),
        "unknown outcome"
    );
    Ok(())
}

/// All SQL identifiers come from validated allowlists; values are bound.
pub(crate) fn selection(q: &ReviewQuery) -> (String, Vec<sea_orm::Value>) {
    let f = &q.filters;
    let metric = f.score_metric.as_deref().unwrap_or("quality_v2");
    let time = match f.time_field.as_deref() {
        Some("entry") => "entry_ts",
        Some("exit") => "exit_ts",
        _ => "warning_ts",
    };
    let mut conditions = vec!["1=1".into()];
    let mut values = vec![];
    if !f.outcomes.is_empty() {
        let predicates: Vec<_> = f.outcomes.iter().map(|s| match s.as_str() {
            "win" => "r.net_r>0",
            "loss" => "r.net_r<0",
            _ => "r.net_r=0",
        }).collect();
        conditions.push(format!("(r.state='closed' AND ({}))", predicates.join(" OR ")));
    }
    for (column, list) in [
        ("symbol", &f.symbols),
        ("direction", &f.directions),
        ("level", &f.levels),
        ("grade", &f.grades),
        ("state", &f.states),
        ("exit_reason", &f.exit_reasons),
        ("terminal_reason", &f.terminal_reasons),
    ] {
        if !list.is_empty() {
            conditions.push(format!(
                "r.{column} IN ({})",
                vec!["?"; list.len()].join(",")
            ));
            for v in list {
                values.push(if column == "symbol" {
                    v.to_ascii_uppercase().into()
                } else {
                    v.clone().into()
                });
            }
        }
    }
    for (value, op) in [(f.score_min, ">="), (f.score_max, "<=")] {
        if let Some(v) = value {
            conditions.push(format!("r.{metric}{op}?"));
            values.push(v.into());
        }
    }
    match f.score_status.as_deref() {
        Some("scored") => conditions.push(format!("r.{metric} IS NOT NULL")),
        Some("unscored") => conditions.push(format!("r.{metric} IS NULL")),
        _ => {}
    }
    for (v, op) in [(&f.time_start, ">="), (&f.time_end, "<")] {
        if let Some(v) = v {
            conditions.push(format!("r.{time}{op}?"));
            values.push(v.clone().into());
        }
    }
    if !f.sessions.is_empty() {
        let mut sessions = vec![];
        for s in &f.sessions {
            sessions.push(format!(
                "(substr(r.{time},12,5)>=? {} substr(r.{time},12,5)<?)",
                if s.start > s.end { "OR" } else { "AND" }
            ));
            values.extend([s.start.clone().into(), s.end.clone().into()]);
        }
        conditions.push(format!("({})", sessions.join(" OR ")));
    }
    let column = match q.sort_by.as_deref().unwrap_or("time") {
        "score" => metric,
        "time" => time,
        x => x,
    };
    let order = if q.descending.unwrap_or(true) {
        "DESC"
    } else {
        "ASC"
    };
    let null_order = if column == "warning_ts" {
        String::new()
    } else {
        format!("r.{column} IS NULL,")
    };
    (format!("SELECT r.summary_json FROM review_current r WHERE {} ORDER BY {null_order}r.{column} {order},r.event_id DESC",conditions.join(" AND ")),values)
}

impl ReviewEngine {
    pub async fn query(&self, owner: &str, q: ReviewQuery) -> Result<ReviewQueryResponse> {
        validate(&q).map_err(|e| super::error("INVALID_QUERY", &e.to_string()))?;
        let _permit = self.query_slots.acquire().await?;
        let started = Instant::now();
        // Pin revision and summaries in one database read snapshot.
        use sea_orm::TransactionTrait;
        let tx = self.db.begin().await?;
        let rev = tx
            .query_one(sql("SELECT revision FROM review_state WHERE id=1", vec![]))
            .await?
            .unwrap()
            .try_get::<i64>("", "revision")?;
        let key = serde_json::to_string(&q)?;
        let cached = {
            let mut snapshots = self.snapshots.lock().await;
            snapshots.retain(|_, s| s.created.elapsed().as_secs() < 600);
            snapshots
                .iter()
                .find(|(_, s)| s.owner == owner && s.key == key && s.revision == rev)
                .map(|(id, s)| response(id, s, q.page_size.unwrap_or(100)))
        };
        if let Some(cached) = cached {
            tx.commit().await?;
            return Ok(cached);
        }
        let (select, values) = selection(&q);
        let payloads: Vec<String> = tx
            .query_all(sql(&select, values))
            .await?
            .into_iter()
            .map(|r| r.try_get("", "summary_json"))
            .collect::<Result<_, _>>()?;
        tx.commit().await?;
        let limit = q.page_size.unwrap_or(100);
        let owner = owner.to_string();
        let snapshot = tokio::task::spawn_blocking(move || -> Result<Snapshot> {
            let mut rows: Vec<ReviewSummary> = payloads
                .iter()
                .map(|s| serde_json::from_str(s))
                .collect::<Result<_, _>>()?;
            let raw_count = rows.len();
            let deduplicated_count = rows.iter().filter(|s| s.family_id == s.event_id).count();
            if q.filters.sample_scope.as_deref() != Some("all") {
                rows.retain(|s| s.family_id == s.event_id);
            }
            let metric = q
                .filters
                .score_metric
                .as_deref()
                .unwrap_or("quality_v2")
                .to_string();
            let rows = Arc::new(rows);
            let overview = statistics(&rows);
            let mut groups = BTreeMap::new();
            for d in ["score_band", "symbol", "hour", "exit_reason"] {
                groups.insert(d.into(), group(&rows, &[d.to_string()], &metric, &[], &[]));
            }
            let equity = equity(&rows, 500);
            let bytes = serde_json::to_vec(&*rows)?.len()
                + serde_json::to_vec(&groups)?.len()
                + serde_json::to_vec(&equity)?.len();
            anyhow::ensure!(
                bytes <= 64 * 1024 * 1024,
                "review snapshot exceeds 64MiB; narrow the query"
            );
            Ok(Snapshot {
                owner,
                created: Instant::now(),
                expires_at: (Utc::now() + chrono::Duration::minutes(10)).to_rfc3339(),
                revision: rev,
                key,
                rows,
                raw_count,
                deduplicated_count,
                metric,
                overview,
                groups,
                equity,
                bytes,
            })
        })
        .await??;
        let bytes = snapshot.bytes;
        let id = uuid::Uuid::new_v4().to_string();
        let result = response(&id, &snapshot, limit);
        let mut snapshots = self.snapshots.lock().await;
        while snapshots.len() >= 64
            || snapshots.values().map(|s| s.bytes).sum::<usize>() + bytes > 64 * 1024 * 1024
        {
            if let Some(oldest) = snapshots
                .iter()
                .min_by_key(|(_, s)| s.created)
                .map(|(id, _)| id.clone())
            {
                snapshots.remove(&oldest);
            } else {
                break;
            }
        }
        snapshots.insert(id, Arc::new(snapshot));
        tracing::info!(
            elapsed_ms = started.elapsed().as_millis(),
            matched = result.page.total,
            revision = rev,
            "review query completed"
        );
        Ok(result)
    }
    pub async fn items(
        &self,
        owner: &str,
        id: &str,
        p: ReviewPageRequest,
    ) -> Result<ReviewItemsPage> {
        let s = self.snapshot(owner, id).await?;
        let limit = p.limit.unwrap_or(100);
        anyhow::ensure!((1..=500).contains(&limit), "limit must be 1..500");
        Ok(page(id, &s, p.cursor.unwrap_or(0), limit))
    }
    pub async fn aggregate(
        &self,
        owner: &str,
        r: ReviewAggregateRequest,
    ) -> Result<ReviewAggregateResponse> {
        validate_aggregate(&r).map_err(|e| super::error("INVALID_QUERY", &e.to_string()))?;
        let _permit = self.query_slots.acquire().await?;
        let s = self.snapshot(owner, &r.snapshot_id).await?;
        let id = r.snapshot_id.clone();
        let groups = tokio::task::spawn_blocking(move || {
            group(
                &s.rows,
                &r.dimensions,
                &s.metric,
                &r.score_buckets,
                &r.time_buckets,
            )
        })
        .await?;
        Ok(ReviewAggregateResponse {
            snapshot_id: id,
            groups,
        })
    }
    pub async fn metadata(&self) -> Result<ReviewMetadata> {
        let rows=self.db.query_all(sql("SELECT r.summary_json FROM review_current c JOIN review_results r ON r.result_version=c.result_version",vec![])).await?;
        let summaries: Vec<ReviewSummary> = rows
            .into_iter()
            .map(|r| {
                Ok(serde_json::from_str(
                    &r.try_get::<String>("", "summary_json")?,
                )?)
            })
            .collect::<Result<_>>()?;
        let mut coverage = BTreeMap::new();
        coverage.insert("published_signals".into(), summaries.len());
        for s in &summaries {
            for key in &s.coverage {
                *coverage.entry(key.clone()).or_default() += 1;
            }
        }
        let raw = self
            .db
            .query_one(sql("SELECT count(*) AS n FROM pattern_events", vec![]))
            .await?
            .unwrap()
            .try_get::<i64>("", "n")?;
        coverage.insert("source_signals".into(), raw as usize);
        let jobs = self
            .db
            .query_all(sql(
                "SELECT data FROM review_jobs ORDER BY rowid DESC LIMIT 10",
                vec![],
            ))
            .await?
            .into_iter()
            .map(|r| Ok(serde_json::from_str(&r.try_get::<String>("", "data")?)?))
            .collect::<Result<_>>()?;
        let symbols=self.db.query_all(sql("SELECT DISTINCT symbol FROM pattern_events UNION SELECT DISTINCT symbol FROM review_results ORDER BY symbol",vec![])).await?.into_iter().map(|r|r.try_get("","symbol")).collect::<Result<_,_>>()?;
        let labels = |keys: &[&str]| {
            keys.iter()
                .map(|s| (s.to_string(), super::replay::reason_label(s).into()))
                .collect()
        };
        Ok(ReviewMetadata {
            profile: PROFILE.into(),
            cost_version: COST_VERSION.into(),
            timezone: "Asia/Shanghai".into(),
            revision: revision(&self.db).await?,
            symbols,
            states: [
                "pending",
                "open",
                "closed",
                "expired",
                "filtered",
                "unfilled",
                "ambiguous",
                "rollover",
                "missing_data",
                "missing_calendar",
                "missing_parameters",
            ]
            .map(String::from)
            .to_vec(),
            exit_reasons: labels(&["stop", "target_1r", "no_follow", "time_exit"]),
            terminal_reasons: labels(&[
                "filtered",
                "untriggered",
                "invalidated",
                "unfilled",
                "ambiguous",
                "rollover",
                "missing_data",
                "missing_calendar",
                "missing_parameters",
            ]),
            score_ranges: SCORES
                .iter()
                .map(|s| (s.to_string(), [0., if *s == "old" { 5. } else { 100. }]))
                .collect(),
            dimensions: DIMENSIONS.iter().map(|s| s.to_string()).collect(),
            coverage,
            jobs,
        })
    }
}

fn page(id: &str, s: &Snapshot, cursor: usize, limit: usize) -> ReviewItemsPage {
    let items: Vec<_> = s.rows.iter().skip(cursor).take(limit).cloned().collect();
    let next = cursor.saturating_add(items.len());
    ReviewItemsPage {
        snapshot_id: id.into(),
        revision: s.revision,
        total: s.rows.len(),
        items,
        next_cursor: (next < s.rows.len()).then_some(next),
    }
}
fn response(id: &str, s: &Snapshot, limit: usize) -> ReviewQueryResponse {
    let scored = s.rows.iter().filter(|r| r.scores.get(&s.metric).copied().flatten().is_some()).count();
    ReviewQueryResponse {
        page: page(id, s, 0, limit),
        expires_at: s.expires_at.clone(),
        raw_count: s.raw_count,
        deduplicated_count: s.deduplicated_count,
        scored,
        unscored: s.rows.len() - scored,
        overview: s.overview.clone(),
        groups: s.groups.clone(),
        equity: s.equity.clone(),
    }
}

fn metric(out: &mut ReviewStatistics, key: &str, values: &[f64], kind: &str) {
    let n = values.len();
    let sum = values.iter().sum::<f64>();
    let value = if n == 0 {
        None
    } else {
        Some(if kind == "sum" { sum } else { sum / n as f64 })
    };
    out.metrics.insert(key.into(), ReviewMetric { value, n });
}
pub(crate) fn statistics(rows: &[ReviewSummary]) -> ReviewStatistics {
    statistics_refs(&rows.iter().collect::<Vec<_>>())
}
fn statistics_refs(rows: &[&ReviewSummary]) -> ReviewStatistics {
    let mut out = ReviewStatistics {
        count: rows.len(),
        ..Default::default()
    };
    for r in rows {
        *out.counts.entry(r.state.clone()).or_default() += 1;
        for c in &r.coverage {
            *out.counts.entry(format!("coverage:{c}")).or_default() += 1;
        }
    }
    out.counts.insert(
        "triggered".into(),
        rows.iter().filter(|r| r.entry_ts.is_some()).count(),
    );
    out.counts.insert(
        "settled".into(),
        rows.iter().filter(|r| r.state == "closed").count(),
    );
    let mut sorted: Vec<_> = rows
        .iter()
        .filter(|r| r.state == "closed" && r.exit_ts.is_some())
        .collect();
    sorted.sort_by(|a, b| a.exit_ts.cmp(&b.exit_ts).then(a.event_id.cmp(&b.event_id)));
    for (name, get) in [
        (
            "gross_amount",
            (|r: &ReviewSummary| r.gross_amount) as fn(&ReviewSummary) -> Option<f64>,
        ),
        ("net_amount", |r| r.net_amount),
        ("gross_r", |r| r.gross_r),
        ("net_r", |r| r.net_r),
    ] {
        let values: Vec<f64> = rows
            .iter()
            .filter(|r| r.state == "closed")
            .filter_map(|r| get(r))
            .collect();
        let wins: Vec<_> = values.iter().copied().filter(|v| *v > 1e-9).collect();
        let losses: Vec<_> = values.iter().copied().filter(|v| *v < -1e-9).collect();
        let n = values.len();
        let win_sum = wins.iter().sum::<f64>();
        let loss_sum = losses.iter().sum::<f64>();
        out.counts.insert(format!("{name}:wins"), wins.len());
        out.counts.insert(format!("{name}:losses"), losses.len());
        out.counts
            .insert(format!("{name}:flat"), n - wins.len() - losses.len());
        metric(&mut out, &format!("total_{name}"), &values, "sum");
        metric(&mut out, &format!("avg_{name}"), &values, "avg");
        metric(&mut out, &format!("avg_win_{name}"), &wins, "avg");
        metric(&mut out, &format!("avg_loss_{name}"), &losses, "avg");
        out.metrics.insert(
            format!("win_rate_{name}"),
            ReviewMetric {
                value: (n > 0).then(|| wins.len() as f64 / n as f64),
                n,
            },
        );
        out.metrics.insert(
            format!("profit_factor_{name}"),
            ReviewMetric {
                value: (loss_sum < 0.).then(|| win_sum / -loss_sum),
                n,
            },
        );
        out.metrics.insert(
            format!("payoff_{name}"),
            ReviewMetric {
                value: (!wins.is_empty() && !losses.is_empty())
                    .then(|| (win_sum / wins.len() as f64) / (-loss_sum / losses.len() as f64)),
                n,
            },
        );
        let (mut sum, mut peak, mut drawdown) = (0_f64, 0_f64, 0_f64);
        for r in &sorted {
            if let Some(v) = get(r) {
                sum += v;
                peak = peak.max(sum);
                drawdown = drawdown.max(peak - sum);
            }
        }
        out.metrics.insert(
            format!("max_drawdown_{name}"),
            ReviewMetric {
                value: (n > 0).then_some(drawdown),
                n,
            },
        );
    }
    let held: Vec<_> = rows
        .iter()
        .filter(|r| r.entry_ts.is_some())
        .map(|r| r.bars_held as f64)
        .collect();
    metric(&mut out, "avg_bars_held", &held, "avg");
    let wait: Vec<_> = rows
        .iter()
        .filter(|r| r.entry_ts.is_some())
        .map(|r| r.wait_bars as f64)
        .collect();
    metric(&mut out, "avg_wait_bars", &wait, "avg");
    for (key, get) in [
        (
            "mfe_r",
            (|r: &ReviewSummary| r.mfe_r) as fn(&ReviewSummary) -> Option<f64>,
        ),
        ("mae_r", |r| r.mae_r),
        ("mfe_amount", |r| r.mfe_amount),
        ("mae_amount", |r| r.mae_amount),
    ] {
        let values: Vec<_> = rows.iter().filter_map(|r| get(r)).collect();
        metric(&mut out, &format!("avg_{key}"), &values, "avg");
    }
    let mfe: Vec<_> = rows.iter().filter_map(|r| r.mfe_r).collect();
    let mae: Vec<_> = rows.iter().filter_map(|r| r.mae_r).collect();
    for (key, values, threshold, above) in [
        ("mfe_ge1_rate", &mfe, 1., true),
        ("mae_le_neg1_rate", &mae, -1., false),
    ] {
        let n = values.len();
        out.metrics.insert(
            key.into(),
            ReviewMetric {
                value: (n > 0).then(|| {
                    values
                        .iter()
                        .filter(|v| {
                            if above {
                                **v >= threshold
                            } else {
                                **v <= threshold
                            }
                        })
                        .count() as f64
                        / n as f64
                }),
                n,
            },
        );
    }
    let giveback: Vec<_> = rows
        .iter()
        .filter(|r| r.state == "closed")
        .filter_map(|r| r.mfe_r.zip(r.gross_r).map(|(m, g)| (m - g).max(0.)))
        .collect();
    metric(&mut out, "avg_giveback_r", &giveback, "avg");
    for r in rows {
        if let Some(v) = r.gross_r {
            *out.gross_r_histogram
                .entry(number_band(v, &[-1., 0., 1., 2.]))
                .or_default() += 1;
        }
        if let Some(v) = r.mfe_r {
            *out.mfe_r_histogram
                .entry(number_band(v, &[0.5, 1., 2.]))
                .or_default() += 1;
        }
        if let Some(v) = r.mae_r {
            *out.mae_r_histogram
                .entry(number_band(v, &[-1., -0.5, 0.]))
                .or_default() += 1;
        }
    }
    out
}
fn number_band(v: f64, edges: &[f64]) -> String {
    let idx = edges.partition_point(|e| v >= *e);
    match idx {
        0 => format!("<{}", edges[0]),
        n if n == edges.len() => format!(">={}", edges[n - 1]),
        n => format!("[{}, {})", edges[n - 1], edges[n]),
    }
}
fn score_band(v: Option<f64>, old: bool, buckets: &[ReviewBucket]) -> String {
    let Some(v) = v else {
        return "missing".into();
    };
    if !buckets.is_empty() {
        return buckets
            .iter()
            .enumerate()
            .find(|(i, b)| v >= b.min && (v < b.max || *i == buckets.len() - 1 && v <= b.max))
            .map(|(_, b)| b.label.clone())
            .unwrap_or_else(|| "outside_buckets".into());
    }
    number_band(
        v,
        if old {
            &[2.5, 3.5]
        } else {
            &[60., 70., 80., 90.]
        },
    )
}
fn dimension(
    r: &ReviewSummary,
    d: &str,
    metric: &str,
    buckets: &[ReviewBucket],
    times: &[ReviewTimeBucket],
) -> String {
    let entry = r.entry_ts.as_deref();
    let score = |k: &str| r.scores.get(k).copied().flatten();
    let diagnostic = |k: &str| r.diagnostics.get(k).copied().flatten();
    let band = |k: &str, e: &[f64]| {
        diagnostic(k)
            .map(|v| number_band(v, e))
            .unwrap_or_else(|| "missing".into())
    };
    match d {
        "score_band" => score_band(score(metric), metric == "old", buckets),
        "a_score_band" => score_band(score("a_score"), false, buckets),
        "b_score_band" => score_band(score("b_score"), false, buckets),
        "symbol" => r.symbol.clone(),
        "direction" => r.direction.clone(),
        "grade" => r.grade.clone(),
        "level" => r.level.clone(),
        "state" => r.state.clone(),
        "exit_reason" => r
            .exit_reason
            .clone()
            .or_else(|| r.terminal_reason.clone().map(|s| format!("unsettled:{s}")))
            .unwrap_or_else(|| "none".into()),
        "hour" => {
            if times.is_empty() {
                entry
                    .and_then(|t| t.get(11..13))
                    .map(|h| format!("{h}:00"))
                    .unwrap_or_else(|| "missing".into())
            } else {
                entry
                    .and_then(|t| t.get(11..16))
                    .and_then(|t| times.iter().find(|b| session_matches(t, &b.start, &b.end)))
                    .map(|b| b.label.clone())
                    .unwrap_or_else(|| "outside_buckets".into())
            }
        }
        "symbol_hour" => format!(
            "{} {}",
            r.symbol,
            dimension(r, "hour", metric, buckets, times)
        ),
        "date" => entry.and_then(|t| t.get(..10)).unwrap_or("missing").into(),
        "weekday" => entry
            .and_then(|t| NaiveDateTime::parse_from_str(t, "%Y-%m-%d %H:%M:%S").ok())
            .map(|d| d.weekday().to_string())
            .unwrap_or_else(|| "missing".into()),
        "session" => entry
            .and_then(|t| t.get(11..16))
            .map(|h| {
                if !("04:00".."20:00").contains(&h) {
                    "night"
                } else {
                    "day"
                }
            })
            .unwrap_or("missing")
            .into(),
        "holding_band" => {
            if r.entry_ts.is_some() {
                number_band(r.bars_held as f64, &[5., 10., 20., 40., 60.])
            } else {
                "missing".into()
            }
        }
        "retracement" => band("retracement", &[0.2, 0.33, 0.5, 0.66]),
        "b_a_speed" => band("b_a_speed", &[0.5, 0.8, 1.2]),
        "vol_band" => band("vol_ratio", &[0.8, 1., 1.3, 2.]),
        "vol_confirm" => band("vol_ratio", &[2.]),
        "b_vol" => band("b_vol_ratio", &[0.8, 1., 1.3]),
        "a_strength" => band("a_move_atr", &[3., 6., 10.]),
        "trigger_lag" => band("trigger_lag", &[2., 4., 7., 13.]),
        "overshoot" => band("overshoot", &[0.1, 0.3, 0.6]),
        "dim_a" | "dim_b" | "dim_warning" | "trend60" => band(d, &[2., 3.5]),
        "gap_combo" => format!(
            "entry:{};exit:{}",
            diagnostic("gap_entry")
                .map(|v| if v > 0. { "gap" } else { "none" })
                .unwrap_or("missing"),
            diagnostic("gap_exit")
                .map(|v| if v > 0. { "gap" } else { "none" })
                .unwrap_or("missing")
        ),
        "oi" => band("oi_increase", &[0.5]),
        "tp_tier" => if r.exit_reason.as_deref() == Some("target_1r") {
            "tp1"
        } else {
            "none"
        }
        .into(),
        _ => "missing".into(),
    }
}
fn group(
    rows: &[ReviewSummary],
    dimensions: &[String],
    metric: &str,
    buckets: &[ReviewBucket],
    times: &[ReviewTimeBucket],
) -> Vec<ReviewGroup> {
    let mut groups = BTreeMap::<Vec<String>, Vec<&ReviewSummary>>::new();
    for r in rows {
        let key = dimensions
            .iter()
            .map(|d| dimension(r, d, metric, buckets, times))
            .collect();
        groups.entry(key).or_default().push(r);
    }
    groups
        .into_iter()
        .map(|(key, rows)| ReviewGroup {
            key,
            statistics: statistics_refs(&rows),
        })
        .collect()
}
fn equity(rows: &[ReviewSummary], max: usize) -> Vec<ReviewEquityPoint> {
    let mut sorted: Vec<_> = rows
        .iter()
        .filter(|r| r.state == "closed" && r.exit_ts.is_some())
        .collect();
    sorted.sort_by(|a, b| a.exit_ts.cmp(&b.exit_ts).then(a.event_id.cmp(&b.event_id)));
    let n = sorted.len();
    let stride = n.div_ceil(max).max(1);
    let (mut ga, mut na, mut gr, mut nr) = (None, None, None, None);
    let add = |a: &mut Option<f64>, b: Option<f64>| {
        if let Some(b) = b {
            *a = Some(a.unwrap_or(0.) + b);
        }
    };
    let mut out = vec![];
    for (i, r) in sorted.into_iter().enumerate() {
        add(&mut ga, r.gross_amount);
        add(&mut na, r.net_amount);
        add(&mut gr, r.gross_r);
        add(&mut nr, r.net_r);
        if (i + 1) % stride == 0 || i + 1 == n {
            out.push(ReviewEquityPoint {
                event_id: r.event_id,
                exit_ts: r.exit_ts.clone().unwrap(),
                gross_amount: ga,
                net_amount: na,
                gross_r: gr,
                net_r: nr,
            });
        }
    }
    out
}

fn validate_aggregate(r: &ReviewAggregateRequest) -> Result<()> {
    anyhow::ensure!(
        (1..=2).contains(&r.dimensions.len())
            && r.dimensions
                .iter()
                .all(|d| DIMENSIONS.contains(&d.as_str())),
        "one or two known dimensions required"
    );
    anyhow::ensure!(
        r.score_buckets.len() <= 100 && r.time_buckets.len() <= 24,
        "too many buckets"
    );
    for b in &r.score_buckets {
        anyhow::ensure!(
            !b.label.is_empty()
                && b.label.len() <= 100
                && b.min.is_finite()
                && b.max.is_finite()
                && b.min < b.max,
            "invalid score bucket"
        );
    }
    for pair in r.score_buckets.windows(2) {
        anyhow::ensure!(
            pair[0].max <= pair[1].min,
            "score buckets must be ordered and nonoverlapping"
        );
    }
    for b in &r.time_buckets {
        hour(&b.start)?;
        hour(&b.end)?;
        anyhow::ensure!(
            !b.label.is_empty() && b.label.len() <= 100 && b.start != b.end,
            "invalid time bucket"
        );
    }
    for minute in 0..1440 {
        let t = format!("{:02}:{:02}", minute / 60, minute % 60);
        anyhow::ensure!(
            r.time_buckets
                .iter()
                .filter(|b| session_matches(&t, &b.start, &b.end))
                .count()
                <= 1,
            "overlapping time buckets"
        );
    }
    Ok(())
}
