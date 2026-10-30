//! Read-only review presentation using exactly the frozen research candidate.
use super::{research::AuditRow, score_scan, Rules, SignalQualityDto};
use crate::{analyze::model::Bar, storage::entities::pattern_events};
use n_protocol::quality::ReviewScoreDto;

/// Both scorers consume finite warmup. Bound review work to this warning's
/// window rather than repeatedly copying a symbol's entire history per row.
pub fn warning_window<'a>(
    event: &pattern_events::Model,
    bars: &'a [Bar],
    rules: &Rules,
) -> &'a [Bar] {
    let start = bars
        .partition_point(|b| b.dt.to_bar_ts() < event.s0_ts)
        .saturating_sub(rules.warmup_max);
    let end = bars.partition_point(|b| b.dt.to_bar_ts() <= event.warning_ts);
    &bars[start.min(end)..end]
}

pub fn score(
    event: &pattern_events::Model,
    bars: &[Bar],
    quality: &SignalQualityDto,
    tick: f64,
) -> ReviewScoreDto {
    let mut result = ReviewScoreDto {
        candidate_version: "quality-rules-v2-candidate-01".into(),
        status: quality.status.clone(),
        morphology_score: None,
        quality_score: None,
        a_score: None,
        b_score: None,
        levels_score: None,
        levels_partial: true,
        explanations: quality.explanations.clone(),
        evidence: None,
    };
    if quality.status != "ok" {
        return result;
    }
    let calculate = || -> anyhow::Result<(serde_json::Value, serde_json::Value)> {
        let payload = super::blind::case(event, bars, quality, &Rules::default());
        let warning = payload["bars"]
            .as_array()
            .map_or(0, |b| b.len().saturating_sub(1));
        let audit = AuditRow {
            event_id: event.id,
            symbol: event.symbol.clone(),
            direction: event.direction.clone(),
            grade: event.grade.clone(),
            level: event.level.clone(),
            warning_ts: event.warning_ts.clone(),
            s0_ts: event.s0_ts.clone(),
            s1_ts: event.s1_ts.clone(),
            old_score: event.entry_score,
            old_dims: serde_json::from_str(&event.entry_score_dims).unwrap_or_default(),
            quality: quality.clone(),
            legacy_recomputed: None,
            legacy_max_error: None,
            evaluations: Default::default(),
        };
        let atr = quality
            .features
            .get("atr_s1")
            .copied()
            .flatten()
            .filter(|x| *x > 0.);
        let evidence =
            super::structure::diagnose(&payload, warning, Some(&audit), atr.map(|a| tick / a))?;
        let profile = serde_json::from_str(score_scan::PROFILE)?;
        let scores = score_scan::candidate_scores(quality, &evidence, &profile)
            .ok_or_else(|| anyhow::anyhow!("候选评分缺少过程指标"))?;
        Ok((scores, evidence))
    };
    match calculate() {
        Ok((scores, evidence)) => {
            result.morphology_score = scores["morphology_v2"].as_f64();
            result.quality_score = scores["quality_v2"].as_f64();
            result.a_score = scores["a_v2"].as_f64();
            result.b_score = scores["b_v2"].as_f64();
            result.levels_score = scores["levels"].as_f64();
            result.levels_partial = scores["levels_partial"].as_bool().unwrap_or(true);
            result.evidence = Some(evidence);
        }
        Err(error) => {
            result.status = "diagnostic_unavailable".into();
            result
                .explanations
                .push(format!("候选评分暂不可用：{error}"));
        }
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn candidate_presentation_is_causal_and_does_not_rewrite_event_scores() {
        for (event, bars, tick) in super::super::tests::fixtures() {
            let before = serde_json::to_value(&event).unwrap();
            let q = super::super::evaluate(
                &event,
                &bars,
                tick,
                &event.warning_ts,
                "review_recompute",
                &Rules::default(),
            );
            let candidate = score(&event, &bars, &q, tick);
            let window = warning_window(&event, &bars, &Rules::default());
            let bounded = super::super::evaluate(
                &event,
                window,
                tick,
                &event.warning_ts,
                "review_recompute",
                &Rules::default(),
            );
            assert_eq!(q.input_hash, bounded.input_hash);
            assert_eq!(
                candidate.quality_score,
                score(&event, window, &bounded, tick).quality_score
            );
            assert_eq!(candidate.status, "ok", "{:?}", candidate.explanations);
            assert!((0.0..=100.).contains(&candidate.quality_score.unwrap()));
            assert!(candidate.a_score.unwrap() <= q.a_score.unwrap());
            assert!(candidate.b_score.unwrap() <= q.b_score.unwrap());
            let mut future = bars.clone();
            let mut bar = bars.last().unwrap().clone();
            bar.dt = crate::analyze::model::DT::from_bar_ts("2099-01-01 09:15:00").unwrap();
            bar.close *= 10.;
            bar.high = bar.close;
            future.push(bar);
            let q2 = super::super::evaluate(
                &event,
                &future,
                tick,
                "2099-01-01 09:15:00",
                "review_recompute",
                &Rules::default(),
            );
            assert_eq!(q.input_hash, q2.input_hash);
            assert_eq!(candidate, score(&event, &future, &q2, tick));
            assert_eq!(before, serde_json::to_value(&event).unwrap());
            let unknown = super::super::evaluate(
                &event,
                &[],
                tick,
                &event.warning_ts,
                "review_recompute",
                &Rules::default(),
            );
            let candidate = score(&event, &[], &unknown, tick);
            assert!(candidate.quality_score.is_none());
            assert_ne!(candidate.status, "ok");
        }
    }
}
