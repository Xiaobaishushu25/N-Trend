// Included INSIDE service in the isolated fork. Calls the actual private service
// functions, with only closed-bar prefixes and no database/network interaction.
pub fn offline_forward_warning_keys(
    bars: &[crate::analyze::model::Bar],
    source: &[crate::v2::replay::ReplayEvent],
) -> Result<Vec<String>> {
    let mut events: Vec<pattern_events::Model> = vec![];
    let mut keep = vec![];
    let mut ordered = source.to_vec();
    ordered.sort_by(|a, b| {
        a.warning_ts
            .cmp(&b.warning_ts)
            .then(a.direction.cmp(&b.direction))
    });
    let mut cursor = 0;
    let index: HashMap<_, _> = bars
        .iter()
        .enumerate()
        .map(|(i, b)| (bar_ts(b), i))
        .collect();
    for w in 0..bars.len() {
        let ts = bar_ts(&bars[w]);
        let prefix = &bars[..=w];
        // A prior older than five bars cannot affect has_similar_warning.
        events.retain(|e| {
            index
                .get(&e.warning_ts)
                .is_some_and(|i| w - i <= outcome::DEDUP_WARNING_BARS)
        });
        while cursor < ordered.len() && ordered[cursor].warning_ts <= ts {
            let mut current = ordered[cursor].clone();
            cursor += 1;
            if current.warning_ts != ts {
                continue;
            }
            // Service event identity/active-state uses the raw structural plan;
            // execution-grid rounding belongs downstream, never in dedup distance.
            let sign = if current.direction == "up" { 1. } else { -1. };
            let atr = crate::analyze::indicators::atr(prefix, crate::analyze::model::ATR_PERIOD);
            let buffer = (0.1 * atr.last().and_then(|x| *x).unwrap_or(0.)).max(1.);
            current.entry = if sign > 0. {
                bars[w].high + current.tick_size
            } else {
                bars[w].low - current.tick_size
            };
            current.stop = current.s2_price - sign * buffer;
            current.risk = (current.entry - current.stop).abs();
            let e = &current;
            let dir = if e.direction == "up" {
                Dir::Up
            } else {
                Dir::Down
            };
            let c = event::WarningCandidate {
                direction: dir,
                grade: e.grade.clone(),
                level: "fine",
                s0_index: *index.get(&e.s0_ts).unwrap_or(&w),
                s1_index: *index.get(&e.s1_ts).unwrap_or(&w),
                s2_index: *index.get(&e.s2_ts).unwrap_or(&w),
                a_move: e.a_move,
                b_move: e.b_move,
                a_bars: e.a_bars as usize,
                b_bars: e.b_bars as usize,
                retracement: e.retracement,
                warning_index: w,
                warning_kind: "strong",
                entry_score: e.setup_features.setup_quality,
                dim_a: 0.,
                dim_b: 0.,
                dim_warning: 0.,
                entry: e.entry,
                stop: e.stop,
                target: e.target,
                risk: e.risk,
                rr: e.rr,
                trend_state: String::new(),
                trend_bonus: 0.,
            };
            if has_similar_warning(prefix, &c, &events)? {
                continue;
            }
            keep.push(format!(
                "{}|{}|{}|{}|{}|{}",
                e.symbol, e.direction, e.s0_ts, e.s1_ts, e.s2_ts, e.warning_ts
            ));
            events.push(pattern_events::Model {
                id: cursor as i64,
                symbol: e.symbol.clone(),
                direction: e.direction.clone(),
                grade: e.grade.clone(),
                level: e.level.clone(),
                s0_ts: e.s0_ts.clone(),
                s0_price: e.s0_price,
                s1_ts: e.s1_ts.clone(),
                s1_price: e.s1_price,
                s2_ts: e.s2_ts.clone(),
                s2_price: e.s2_price,
                a_move: e.a_move,
                b_move: e.b_move,
                a_bars: e.a_bars,
                b_bars: e.b_bars,
                retracement: e.retracement,
                warning_ts: e.warning_ts.clone(),
                detected_at: ts.clone(),
                warning_kind: e.warning_kind.clone(),
                entry_score: e.setup_features.setup_quality,
                entry_score_dims: "{}".into(),
                entry: e.entry,
                stop: e.stop,
                target: e.target,
                risk: e.risk,
                rr: e.rr,
                state: "pending".into(),
                last_advance_ts: None,
                trigger_ts: None,
                trigger_bar_ts: None,
                trigger_price: None,
                trigger_score: None,
                trigger_volume_ratio: None,
                overshoot_r: None,
                hold_score: None,
                hold_score_history: "[]".into(),
                outcome: None,
                exit_reason: None,
                exit_ts: None,
                exit_price: None,
                r_multiple: None,
                mfe_r: None,
                mae_r: None,
                created_at: ts.clone(),
                updated_at: ts.clone(),
            });
        }
        // Matches service ordering: insert new warnings, then advance existing
        // events with the current closed bar. Never uses final outcome labels.
        for e in &mut events {
            advance_event_model(e, prefix);
        }
    }
    Ok(keep)
}
