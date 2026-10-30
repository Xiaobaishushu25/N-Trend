use super::*;
use std::cmp::Ordering;

#[derive(Serialize)]
pub struct ReviewPage {
    pub items: Vec<OutcomeDetail>,
    pub total: usize,
    pub scored: usize,
    pub unscored: usize,
    pub offset: usize,
    pub limit: usize,
    pub symbols: Vec<String>,
}

fn value(row: &OutcomeDetail, metric: &str) -> Option<f64> {
    let q = row.quality.as_ref().filter(|q| q.status == "ok");
    let v2 = row.review_scores.as_ref().filter(|q| q.status == "ok");
    match metric {
        "old" => Some(row.entry_score),
        "morphology_v1" => q.and_then(|q| q.morphology_score),
        "quality_v1" => q.and_then(|q| q.quality_score),
        "morphology_v2" => v2.and_then(|q| q.morphology_score),
        _ => v2.and_then(|q| q.quality_score),
    }
    .filter(|x| x.is_finite())
}

fn timestamp<'a>(
    warning: &'a str,
    trigger: Option<&'a str>,
    bar: Option<&'a str>,
    f: &OutcomeFilter,
) -> Option<&'a str> {
    if f.time_field.as_deref() == Some("trigger") {
        trigger
            .filter(|t| !t.is_empty())
            .or(bar.filter(|t| !t.is_empty()))
    } else {
        Some(warning)
    }
}

fn time_matches(ts: Option<&str>, f: &OutcomeFilter) -> bool {
    let constrained = f.time_start.is_some()
        || f.time_end.is_some()
        || f.hour_start.is_some()
        || f.hour_end.is_some();
    let Some(ts) = ts else {
        return !constrained;
    };
    if f.time_start.as_deref().is_some_and(|t| ts < t)
        || f.time_end.as_deref().is_some_and(|t| ts > t)
    {
        return false;
    }
    let Some(hour) = ts.get(11..16) else {
        return !constrained;
    };
    match (f.hour_start.as_deref(), f.hour_end.as_deref()) {
        (Some(a), Some(b)) if a > b => hour >= a || hour <= b,
        (a, b) => a.map_or(true, |a| hour >= a) && b.map_or(true, |b| hour <= b),
    }
}

fn optional_compare(a: Option<f64>, b: Option<f64>, asc: bool) -> Ordering {
    match (a, b) {
        (Some(a), Some(b)) => {
            if asc {
                a.total_cmp(&b)
            } else {
                b.total_cmp(&a)
            }
        }
        (None, Some(_)) => Ordering::Greater,
        (Some(_), None) => Ordering::Less,
        _ => Ordering::Equal,
    }
}

fn compare(a: &OutcomeDetail, b: &OutcomeDetail, f: &OutcomeFilter) -> Ordering {
    let order = f.sort_by.as_deref().unwrap_or("time_desc");
    let metric = f.quality_metric.as_deref().unwrap_or("quality_v2");
    let primary = if order.starts_with("score_") {
        optional_compare(value(a, metric), value(b, metric), order == "score_asc")
    } else {
        let at = timestamp(
            &a.warning_ts,
            a.trigger_ts.as_deref(),
            a.trigger_bar_ts.as_deref(),
            f,
        );
        let bt = timestamp(
            &b.warning_ts,
            b.trigger_ts.as_deref(),
            b.trigger_bar_ts.as_deref(),
            f,
        );
        match (at, bt) {
            (Some(a), Some(b)) => {
                if order == "time_asc" {
                    a.cmp(b)
                } else {
                    b.cmp(a)
                }
            }
            (None, Some(_)) => Ordering::Greater,
            (Some(_), None) => Ordering::Less,
            _ => Ordering::Equal,
        }
    };
    primary
        .then_with(|| b.warning_ts.cmp(&a.warning_ts))
        .then_with(|| b.event_id.cmp(&a.event_id))
}

fn validate(f: &OutcomeFilter) -> Result<()> {
    for x in [f.score_min, f.score_max, f.quality_min, f.quality_max]
        .into_iter()
        .flatten()
    {
        anyhow::ensure!(x.is_finite(), "评分必须是有限数值");
    }
    anyhow::ensure!(
        !f.quality_min.zip(f.quality_max).is_some_and(|(a, b)| a > b),
        "评分下限不能大于上限"
    );
    for ts in [&f.time_start, &f.time_end].into_iter().flatten() {
        let parsed = NaiveDateTime::parse_from_str(ts, "%Y-%m-%d %H:%M:%S")?;
        anyhow::ensure!(
            parsed.format("%Y-%m-%d %H:%M:%S").to_string() == *ts,
            "时间格式须为YYYY-MM-DD HH:mm:ss"
        );
    }
    for hour in [&f.hour_start, &f.hour_end].into_iter().flatten() {
        let parsed = chrono::NaiveTime::parse_from_str(hour, "%H:%M")?;
        anyhow::ensure!(
            parsed.format("%H:%M").to_string() == *hour,
            "每日时段格式须为HH:mm"
        );
    }
    anyhow::ensure!(
        !f.time_start
            .as_ref()
            .zip(f.time_end.as_ref())
            .is_some_and(|(a, b)| a > b),
        "开始时间不能晚于结束时间"
    );
    if let Some(metric) = &f.quality_metric {
        anyhow::ensure!(
            [
                "old",
                "quality_v1",
                "morphology_v1",
                "quality_v2",
                "morphology_v2"
            ]
            .contains(&metric.as_str()),
            "未知评分版本"
        );
    }
    if let Some(order) = &f.sort_by {
        anyhow::ensure!(
            ["score_asc", "score_desc", "time_asc", "time_desc"].contains(&order.as_str()),
            "未知排序"
        );
    }
    if let Some(field) = &f.time_field {
        anyhow::ensure!(
            ["warning", "trigger"].contains(&field.as_str()),
            "未知时间口径"
        );
    }
    if let Some(status) = &f.quality_status {
        anyhow::ensure!(
            ["all", "scored", "unscored"].contains(&status.as_str()),
            "未知评分状态"
        );
    }
    Ok(())
}

fn select_page(
    mut rows: Vec<OutcomeDetail>,
    limit: usize,
    offset: usize,
    filter: &OutcomeFilter,
    symbols: Vec<String>,
) -> ReviewPage {
    let metric = filter.quality_metric.as_deref().unwrap_or("quality_v2");
    rows.retain(|r| {
        let score = value(r, metric);
        match filter.quality_status.as_deref() {
            Some("scored") if score.is_none() => return false,
            Some("unscored") if score.is_some() => return false,
            _ => {}
        }
        if filter.quality_min.is_some() || filter.quality_max.is_some() {
            score.is_some_and(|s| score_in_range(s, filter.quality_min, filter.quality_max))
        } else {
            true
        }
    });
    let total = rows.len();
    let scored = rows.iter().filter(|r| value(r, metric).is_some()).count();
    rows.sort_by(|a, b| compare(a, b, filter));
    let limit = limit.clamp(1, 2000);
    let items: Vec<_> = rows.into_iter().skip(offset).take(limit).collect();
    ReviewPage {
        items,
        total,
        scored,
        unscored: total - scored,
        offset,
        limit,
        symbols,
    }
}

impl Services {
    pub(super) async fn fill_review_scores(
        &self,
        event: &pattern_events::Model,
        bars: &[Bar],
        tick: f64,
        row: &mut OutcomeDetail,
    ) {
        let rules = crate::quality::Rules::default();
        let closed_through = bars.last().map(bar_ts);
        let bars = crate::quality::review::warning_window(event, bars, &rules);
        let quality = crate::quality::evaluate(
            event,
            bars,
            tick,
            closed_through.as_deref().unwrap_or(""),
            "review_recompute",
            &rules,
        );
        let key = format!(
            "{}:{}:{}",
            quality.event_key, quality.input_hash, quality.parameter_hash
        );
        let cached = if quality.status == "ok" {
            self.review_score_cache.read().await.get(&key).cloned()
        } else {
            None
        };
        row.review_scores = Some(match cached {
            Some(score) => score,
            None => {
                let score = crate::quality::review::score(event, bars, &quality, tick);
                if quality.status == "ok" {
                    let mut cache = self.review_score_cache.write().await;
                    if cache.len() >= 10000 {
                        cache.clear();
                    }
                    cache.insert(key, score.clone());
                }
                score
            }
        });
        row.quality = Some(quality);
    }

    /// Global filters and deterministic ordering precede pagination. No event writes.
    pub async fn review_page(
        &self,
        limit: usize,
        offset: usize,
        filter: &OutcomeFilter,
    ) -> Result<ReviewPage> {
        validate(filter)?;
        let events = repo::all_pattern_events(&self.db).await?;
        let symbols = repo::list_symbols(&self.db, false).await?;
        let ticks: HashMap<_, _> = symbols
            .iter()
            .map(|s| {
                (
                    s.code.clone(),
                    crate::precision::effective_tick(s.tick_size, &s.code, &s.variety),
                )
            })
            .collect();
        let mut symbol_options: Vec<_> = events
            .iter()
            .map(|e| e.symbol.clone())
            .collect::<HashSet<_>>()
            .into_iter()
            .collect();
        symbol_options.sort();
        let allowed: HashSet<_> = filter
            .symbols
            .as_deref()
            .unwrap_or("")
            .split(',')
            .map(|s| s.trim().to_ascii_uppercase())
            .filter(|s| !s.is_empty())
            .collect();
        let selected: Vec<_> = events
            .iter()
            .filter(|e| matches_outcome_filter(e, filter))
            .filter(|e| allowed.is_empty() || allowed.contains(&e.symbol.to_ascii_uppercase()))
            .filter(|e| {
                time_matches(
                    timestamp(
                        &e.warning_ts,
                        e.trigger_ts.as_deref(),
                        e.trigger_bar_ts.as_deref(),
                        filter,
                    ),
                    filter,
                )
            })
            .collect();
        let mut rows = Vec::new();
        let mut bars_by_symbol = HashMap::new();
        for event in selected {
            if !bars_by_symbol.contains_key(&event.symbol) {
                bars_by_symbol.insert(
                    event.symbol.clone(),
                    self.bars_for(&event.symbol, "15m").await?,
                );
            }
            let bars = &bars_by_symbol[&event.symbol];
            let tick = ticks.get(&event.symbol).copied().unwrap_or(0.);
            let mut row = outcome_detail_from(event, Some(tick));
            self.fill_review_scores(event, bars, tick, &mut row).await;
            rows.push(row);
        }
        let mut page = select_page(rows, limit, offset, filter, symbol_options);
        let items = &mut page.items;
        let ids: HashSet<_> = items.iter().map(|r| r.event_id).collect();
        let mut annotations: HashMap<i64, Vec<SignalAnnotationDto>> = HashMap::new();
        for a in repo::all_signal_annotations(&self.db)
            .await?
            .into_iter()
            .filter(|a| ids.contains(&a.event_id))
        {
            annotations
                .entry(a.event_id)
                .or_default()
                .push(SignalAnnotationDto {
                    id: a.id,
                    event_id: a.event_id,
                    content: a.content,
                    created_at: a.created_at,
                });
        }
        let decisions: HashMap<_, _> = repo::all_signal_decisions(&self.db)
            .await?
            .into_iter()
            .map(|d| (d.event_id, d.opened))
            .collect();
        let mut indicators = HashMap::new();
        for row in items {
            row.annotations = annotations.remove(&row.event_id).unwrap_or_default();
            row.opened = decisions.get(&row.event_id).copied();
            let bars = &bars_by_symbol[&row.symbol];
            let (atr, index) = indicators.entry(row.symbol.clone()).or_insert_with(|| {
                (
                    crate::analyze::indicators::atr(bars, ATR_PERIOD),
                    bars.iter()
                        .enumerate()
                        .map(|(i, b)| (bar_ts(b), i))
                        .collect::<HashMap<_, _>>(),
                )
            });
            fill_leg_detail(row, bars, atr, index);
        }
        Ok(page)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn row(id: i64, score: Option<f64>) -> OutcomeDetail {
        let fixture: serde_json::Value =
            serde_json::from_str(include_str!("../quality/fixtures/four-signals.json")).unwrap();
        let mut event: pattern_events::Model =
            serde_json::from_value(fixture["cases"][0]["event"].clone()).unwrap();
        event.id = id;
        let mut row = outcome_detail_from(&event, Some(1.));
        row.review_scores = Some(n_protocol::quality::ReviewScoreDto {
            candidate_version: "test".into(),
            status: if score.is_some() {
                "ok"
            } else {
                "missing_data"
            }
            .into(),
            quality_score: score,
            morphology_score: score,
            a_score: None,
            b_score: None,
            levels_score: None,
            levels_partial: true,
            explanations: vec![],
            evidence: None,
        });
        row
    }
    #[test]
    fn score_filter_sort_and_counts_cover_all_records_before_pagination() {
        let rows = vec![
            row(1, Some(20.)),
            row(2, Some(90.)),
            row(3, None),
            row(4, Some(80.)),
            row(5, Some(80.)),
        ];
        let f = OutcomeFilter {
            sort_by: Some("score_desc".into()),
            quality_min: Some(70.),
            ..Default::default()
        };
        let page = select_page(rows.clone(), 1, 0, &f, vec![]);
        assert_eq!(page.total, 3);
        assert_eq!(page.scored, 3);
        assert_eq!(page.items[0].event_id, 2);
        let page = select_page(rows.clone(), 1, 1, &f, vec![]);
        assert_eq!(page.items[0].event_id, 5); // Deterministic equal-score tie.
        let f = OutcomeFilter {
            sort_by: Some("score_asc".into()),
            ..Default::default()
        };
        let page = select_page(rows.clone(), 100, 0, &f, vec![]);
        assert_eq!(page.items.last().unwrap().event_id, 3);
        assert_eq!((page.scored, page.unscored), (4, 1));
        let f = OutcomeFilter {
            quality_status: Some("unscored".into()),
            ..Default::default()
        };
        assert_eq!(select_page(rows, 100, 0, &f, vec![]).items[0].event_id, 3);
    }
    #[test]
    fn time_range_and_midnight_session_do_not_include_untriggered_rows() {
        let f = OutcomeFilter {
            hour_start: Some("21:00".into()),
            hour_end: Some("02:30".into()),
            time_start: Some("2026-10-01 00:00:00".into()),
            ..Default::default()
        };
        assert!(time_matches(Some("2026-10-02 01:15:00"), &f));
        assert!(time_matches(Some("2026-10-02 21:00:00"), &f));
        assert!(!time_matches(Some("2026-10-02 14:00:00"), &f));
        assert!(!time_matches(Some("2026-09-30 21:00:00"), &f));
        assert!(!time_matches(None, &f));
        assert!(time_matches(None, &OutcomeFilter::default()));
    }
    #[test]
    fn unknown_score_is_last_in_both_directions_and_invalid_ranges_are_rejected() {
        assert_eq!(optional_compare(None, Some(0.), true), Ordering::Greater);
        assert_eq!(optional_compare(None, Some(100.), false), Ordering::Greater);
        assert_eq!(optional_compare(Some(10.), Some(20.), true), Ordering::Less);
        assert!(validate(&OutcomeFilter {
            quality_min: Some(80.),
            quality_max: Some(70.),
            ..Default::default()
        })
        .is_err());
        assert!(validate(&OutcomeFilter {
            time_start: Some("2026-99-01 00:00:00".into()),
            ..Default::default()
        })
        .is_err());
    }
}
