//! Fixed-one-lot historical observer. Never changes an event or production policy.
use crate::{analyze::outcome, fetch::kline::Kline, storage::entities::pattern_events};
use chrono::{Duration, NaiveDate, NaiveDateTime, Timelike};
use n_protocol::review::*;
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::collections::{BTreeMap, HashSet};

const FMT: &str = "%Y-%m-%d %H:%M:%S";

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct CalendarInput {
    pub days: BTreeMap<String, bool>,
}

impl CalendarInput {
    pub fn current() -> Self {
        let days = crate::session::trading_calendar::global_calendar()
            .read()
            .map(|c| {
                c.export_file()
                    .records
                    .into_iter()
                    .map(|d| (d.date, d.trading))
                    .collect()
            })
            .unwrap_or_default();
        Self { days }
    }
    pub fn version(&self) -> String {
        blake3::hash(&serde_json::to_vec(self).unwrap())
            .to_hex()
            .to_string()
    }
    fn trading(&self, date: NaiveDate) -> Result<bool, &'static str> {
        self.days
            .get(&date.to_string())
            .copied()
            .ok_or("missing_calendar")
    }
    /// Bar timestamps denote interval ends. Test the minute inside the interval.
    pub(super) fn expected(&self, symbol: &str, end: NaiveDateTime) -> Result<bool, &'static str> {
        use crate::session::spec::NightSessionType as N;
        let t = end - Duration::minutes(1);
        let m = t.hour() * 60 + t.minute();
        let date = t.date();
        if (540..615).contains(&m) || (630..690).contains(&m) || (810..900).contains(&m) {
            return self.trading(date);
        }
        let night = crate::session::SessionCalendar::get(symbol).night_type;
        if night == N::None {
            return Ok(false);
        }
        let close: u32 = match night {
            N::Close2300 => 1380,
            N::Close2330 => 1410,
            N::Close0100 => 1500,
            N::Close0230 => 1590,
            N::None => 0,
        };
        let night_date = if m >= 1260 && m < close.min(1440) {
            Some(date)
        } else if m < close.saturating_sub(1440) {
            Some(date - Duration::days(1))
        } else {
            None
        };
        if let Some(d) = night_date {
            use chrono::Datelike;
            if !self.trading(d)? {
                return Ok(false);
            }
            // Friday night continues across Saturday; a weekday holiday blocks its eve.
            for offset in 1..=15 {
                let next = d + Duration::days(offset);
                if self.trading(next)? {
                    return Ok(true);
                }
                if !matches!(next.weekday(), chrono::Weekday::Sat | chrono::Weekday::Sun) {
                    return Ok(false);
                }
            }
            return Err("missing_calendar");
        }
        Ok(false)
    }
}

pub struct ReplayOutput {
    pub summary: ReviewSummary,
    pub entry_price: Option<f64>,
    pub exit_price: Option<f64>,
    pub nodes: Vec<ReviewLifecycleNode>,
    pub holding: Vec<ReviewHoldingPoint>,
    pub mfe: ReviewExcursion,
    pub mae: ReviewExcursion,
}

fn interval(ts: &str) -> (String, String) {
    let start = NaiveDateTime::parse_from_str(ts, FMT)
        .map(|d| (d - Duration::minutes(5)).format(FMT).to_string())
        .unwrap_or_default();
    (start, ts.into())
}

fn excursion(
    lower: f64,
    upper: f64,
    sign: f64,
    fill: f64,
    risk: f64,
    multiplier: Option<f64>,
    at: &str,
) -> ReviewExcursion {
    let exact = (lower - upper).abs() <= 1e-9;
    let r = exact.then_some(lower);
    let (start, end) = interval(at);
    ReviewExcursion {
        r,
        amount: r.zip(multiplier).map(|(r, m)| r * risk * m),
        price: r.map(|r| fill + sign * risk * r),
        interval_start: Some(start),
        interval_end: Some(end),
        lower_r: Some(lower),
        upper_r: Some(upper),
        precision: if exact { "bar_interval" } else { "bounded" }.into(),
    }
}

fn node(
    nodes: &mut Vec<ReviewLifecycleNode>,
    stage: &str,
    occurred: &str,
    known: &str,
    recorded: &str,
    origin: &str,
    comment: String,
    evidence: serde_json::Value,
) {
    nodes.push(ReviewLifecycleNode {
        sequence: nodes.len(),
        stage: stage.into(),
        occurred_at: occurred.into(),
        known_at: known.into(),
        recorded_at: recorded.into(),
        origin: origin.into(),
        comment,
        evidence,
    });
}

pub fn reason_label(reason: &str) -> &'static str {
    match reason {
        "stop" => "初始止损",
        "target_1r" => "达到1R止盈，固定一手全部退出",
        "no_follow" => "持有第5根15分钟K线时最大有利变动不足0.5R",
        "time_exit" => "达到60根15分钟K线持有期限",
        "filtered" => "未满足原始入场分或最小风险门槛",
        "untriggered" => "等待12根15分钟K线后仍未触发",
        "invalidated" => "入场前结构止损失效",
        "unfilled" => "跳空或滑点超过追价上限",
        "ambiguous" => "5分钟K线内的价格先后顺序无法确定",
        "rollover" => "跨确认换月，停止连续合约模拟",
        "missing_data" => "缺少完整或有效的5分钟行情",
        "missing_calendar" => "交易日历未覆盖所需日期",
        "missing_parameters" => "缺少有效的合约参数",
        _ => "等待后续行情",
    }
}

/// No inferred intrabar path: entry/exit conflicts are excluded, extrema carry bounds.
pub fn run(
    event: &pattern_events::Model,
    raw: &[Kline],
    rollovers: &HashSet<String>,
    calendar: &CalendarInput,
    tick: Option<f64>,
    multiplier: Option<f64>,
    origin: &str,
    recorded_at: &str,
) -> ReplayOutput {
    let scores = BTreeMap::from([("old".into(), Some(event.entry_score))]);
    let mut out = ReplayOutput {
        summary: ReviewSummary {
            event_id: event.id,
            family_id: event.id,
            family_size: 1,
            symbol: event.symbol.clone(),
            direction: event.direction.clone(),
            level: event.level.clone(),
            grade: event.grade.clone(),
            warning_ts: event.warning_ts.clone(),
            scores,
            plan: ReviewPlan {
                entry: event.entry.is_finite().then_some(event.entry),
                stop: event.stop.is_finite().then_some(event.stop),
                reference_target: event.target.is_finite().then_some(event.target),
                risk: event.risk.is_finite().then_some(event.risk),
                rr: event.rr.is_finite().then_some(event.rr),
            },
            state: "pending".into(),
            score_status: "unscored".into(),
            ..Default::default()
        },
        entry_price: None,
        exit_price: None,
        nodes: vec![],
        holding: vec![],
        mfe: ReviewExcursion::default(),
        mae: ReviewExcursion::default(),
    };
    let dims: serde_json::Value = serde_json::from_str(&event.entry_score_dims).unwrap_or_default();
    for (stage, start, end, p0, p1, bars, score) in [
        (
            "a_segment",
            &event.s0_ts,
            &event.s1_ts,
            event.s0_price,
            event.s1_price,
            event.a_bars,
            dims["dim_a"].as_f64(),
        ),
        (
            "b_segment",
            &event.s1_ts,
            &event.s2_ts,
            event.s1_price,
            event.s2_price,
            event.b_bars,
            dims["dim_b"].as_f64(),
        ),
    ] {
        node(
            &mut out.nodes,
            stage,
            end,
            &event.warning_ts,
            recorded_at,
            origin,
            format!(
                "{}段：{}根K线，幅度{:.4}；原始分项{}（预警时记录）",
                if stage == "a_segment" { "A" } else { "B" },
                bars,
                (p1 - p0).abs(),
                score
                    .map(|x| format!("{x:.2}"))
                    .unwrap_or_else(|| "缺失".into())
            ),
            json!({"start":start,"end":end,"start_price":p0,"end_price":p1,"bars":bars,"original_score":score,"retracement":event.retracement}),
        );
    }
    node(
        &mut out.nodes,
        "warning",
        &event.warning_ts,
        &event.detected_at,
        recorded_at,
        origin,
        format!(
            "{}预警，原始入场分{:.2}",
            event.warning_kind, event.entry_score
        ),
        json!({"kind":event.warning_kind,"entry":event.entry,"stop":event.stop,"target":event.target,"planned_risk":event.risk,"original_dims":dims}),
    );
    node(
        &mut out.nodes,
        "waiting",
        &event.warning_ts,
        &event.detected_at,
        recorded_at,
        origin,
        "等待突破，最多12根15分钟交易K线".into(),
        json!({"pending_bars":outcome::PENDING_BARS}),
    );
    let mut terminal: Option<(&str, String)> = None;
    let sign = if event.direction == "down" { -1. } else { 1. };
    let valid = [event.entry, event.stop, event.risk, event.entry_score]
        .iter()
        .all(|x| x.is_finite())
        && event.risk > 0.
        && matches!(event.direction.as_str(), "up" | "down");
    if !valid || tick.is_none_or(|t| !t.is_finite() || t <= 0.) {
        terminal = Some(("missing_parameters", event.warning_ts.clone()));
    }
    let tick = tick.unwrap_or(1.);
    if terminal.is_none() && (event.entry_score < 2.5 || event.risk / tick + 1e-6 < 10.) {
        terminal = Some(("filtered", event.warning_ts.clone()));
    }
    let warning = NaiveDateTime::parse_from_str(&event.warning_ts, FMT).ok();
    if warning.is_none() {
        terminal = Some(("missing_data", event.warning_ts.clone()));
    }
    let mut previous = warning;
    let mut bucket = String::new();
    let mut held_bucket = String::new();
    let mut mfe_lo: f64 = 0.;
    let mut mfe_hi: f64 = 0.;
    let mut mae_lo: f64 = 0.;
    let mut mae_hi: f64 = 0.;
    let mut mfe_at = event.warning_ts.clone();
    let mut mae_at = event.warning_ts.clone();
    if terminal.is_none() && !raw.iter().any(|b| b.datetime == event.warning_ts) {
        terminal = Some(("missing_data", event.warning_ts.clone()));
    }
    for b in raw.iter().filter(|b| b.datetime > event.warning_ts) {
        if terminal.is_some() {
            break;
        }
        let Ok(dt) = NaiveDateTime::parse_from_str(&b.datetime, FMT) else {
            terminal = Some(("missing_data", b.datetime.clone()));
            break;
        };
        if dt.minute() % 5 != 0
            || dt.second() != 0
            || ![b.open, b.high, b.low, b.close]
                .iter()
                .all(|x| x.is_finite())
            || b.low > b.open.min(b.close)
            || b.high < b.open.max(b.close)
            || b.low > b.high
        {
            terminal = Some(("missing_data", b.datetime.clone()));
            break;
        }
        if let Some(prior) = previous {
            let mut expected = prior + Duration::minutes(5);
            while expected <= dt {
                match calendar.expected(&event.symbol, expected) {
                    Err(r) => {
                        terminal = Some((r, expected.format(FMT).to_string()));
                        break;
                    }
                    Ok(true) if expected < dt => {
                        terminal = Some(("missing_data", expected.format(FMT).to_string()));
                        break;
                    }
                    Ok(false) if expected == dt => {
                        terminal = Some(("missing_data", b.datetime.clone()));
                        break;
                    }
                    _ => {}
                }
                expected += Duration::minutes(5);
            }
        }
        if terminal.is_some() {
            break;
        }
        previous = Some(dt);
        if rollovers
            .iter()
            .any(|t| t > &event.warning_ts && t <= &b.datetime)
        {
            terminal = Some(("rollover", b.datetime.clone()));
            break;
        }
        let current_bucket =
            crate::derive::current_bucket_end_ts(dt, crate::derive::Timeframe::M15);
        if out.entry_price.is_none() && bucket != current_bucket {
            out.summary.wait_bars += 1;
            bucket = current_bucket.clone();
        }
        if out.entry_price.is_none() && out.summary.wait_bars > outcome::PENDING_BARS {
            terminal = Some(("untriggered", b.datetime.clone()));
            break;
        }
        let favorable = if sign > 0. { b.high } else { b.low };
        let adverse = if sign > 0. { b.low } else { b.high };
        let mut new_entry = false;
        if out.entry_price.is_none() {
            let hit = (favorable - event.entry) * sign >= 0.;
            let invalid = (adverse - event.stop) * sign <= 0.;
            if !hit {
                if invalid {
                    terminal = Some(("invalidated", b.datetime.clone()));
                    break;
                }
                if current_bucket == b.datetime && out.summary.wait_bars >= outcome::PENDING_BARS {
                    terminal = Some(("untriggered", b.datetime.clone()));
                    break;
                }
                continue;
            }
            if invalid && (b.open - event.entry) * sign < 0. {
                terminal = Some(("ambiguous", b.datetime.clone()));
                break;
            }
            let observed = if (b.open - event.entry) * sign >= 0. {
                b.open
            } else {
                event.entry
            };
            let fill = observed + sign * tick;
            if (fill - event.entry) * sign > (event.risk * 0.2).max(2. * tick) + 1e-6 {
                terminal = Some(("unfilled", b.datetime.clone()));
                break;
            }
            let risk = (fill - event.stop) * sign;
            if risk <= 0. {
                terminal = Some(("invalidated", b.datetime.clone()));
                break;
            }
            out.entry_price = Some(fill);
            out.summary.entry_ts = Some(b.datetime.clone());
            out.summary.risk_points = Some(risk);
            out.summary.risk_amount = multiplier.map(|m| risk * m);
            node(
                &mut out.nodes,
                "trigger",
                &b.datetime,
                &b.datetime,
                recorded_at,
                origin,
                "突破入场条件，模拟固定一手成交".into(),
                json!({"interval":interval(&b.datetime),"observed_price":observed,"fill":fill,"slippage_ticks":1,"volume":1,"risk":risk,"gap":observed!=event.entry}),
            );
            node(
                &mut out.nodes,
                "holding",
                &b.datetime,
                &b.datetime,
                recorded_at,
                origin,
                "开始持有，首次达到1R全部退出".into(),
                json!({"target_1r":fill+sign*risk,"stop":event.stop,"hold_score":null}),
            );
            new_entry = true;
        }
        if held_bucket != current_bucket {
            held_bucket = current_bucket.clone();
            out.summary.bars_held += 1;
        }
        let fill = out.entry_price.unwrap();
        let risk = out.summary.risk_points.unwrap();
        let stop_hit = (adverse - event.stop) * sign <= 0.;
        let target = fill + sign * risk;
        let target_hit = (favorable - target) * sign >= 0.;
        if stop_hit && target_hit {
            terminal = Some(("ambiguous", b.datetime.clone()));
            break;
        }
        let high_r = ((favorable - fill) * sign / risk).max(0.);
        let low_r = ((adverse - fill) * sign / risk).min(0.);
        let at_open = !new_entry || (b.open - event.entry) * sign >= 0.;
        let open_r = (b.open - fill) * sign / risk;
        let exit = if stop_hit {
            let observed = if (b.open - event.stop) * sign < 0. {
                b.open
            } else {
                event.stop
            };
            let adverse_r = ((observed - fill) * sign / risk).min(0.);
            mae_lo = mae_lo.min(adverse_r);
            mae_hi = mae_hi.min(adverse_r);
            mfe_lo = mfe_lo.max(if at_open { open_r.max(0.) } else { 0. });
            mfe_hi = mfe_hi.max(high_r.min(1.));
            Some(("stop", observed - sign * tick))
        } else if target_hit {
            mfe_lo = mfe_lo.max(1.);
            mfe_hi = mfe_hi.max(1.);
            mae_lo = mae_lo.min(low_r);
            mae_hi = mae_hi.min(if at_open { open_r.min(0.) } else { 0. });
            Some(("target_1r", target - sign * tick))
        } else {
            mfe_lo = mfe_lo.max(high_r);
            mfe_hi = mfe_hi.max(high_r);
            mae_lo = mae_lo.min(low_r);
            mae_hi = mae_hi.min(if at_open {
                low_r
            } else {
                ((b.close - fill) * sign / risk).min(0.)
            });
            None
        };
        if out.mfe.upper_r.is_none_or(|x| mfe_hi > x) {
            mfe_at = b.datetime.clone();
        }
        if out.mae.lower_r.is_none_or(|x| mae_lo < x) {
            mae_at = b.datetime.clone();
        }
        out.mfe = excursion(mfe_lo, mfe_hi, sign, fill, risk, multiplier, &mfe_at);
        out.mae = excursion(mae_lo, mae_hi, sign, fill, risk, multiplier, &mae_at);
        let completed = current_bucket == b.datetime;
        // A bounded MFE cannot establish a no-follow condition; require its upper bound.
        let no_follow = completed
            && out.summary.bars_held >= outcome::NO_FOLLOW_BAR
            && mfe_hi < outcome::NO_FOLLOW_MFE_R;
        let time_exit = completed && out.summary.bars_held >= outcome::TIME_HORIZON_BARS;
        let exit = exit.or_else(|| {
            if time_exit {
                Some(("time_exit", b.close - sign * tick))
            } else if no_follow {
                Some(("no_follow", b.close - sign * tick))
            } else {
                None
            }
        });
        let mark = exit.map(|(_, p)| p).unwrap_or(b.close);
        let floating = (mark - fill) * sign;
        let (start, end) = interval(&b.datetime);
        out.holding.push(ReviewHoldingPoint {sequence:out.holding.len(),interval_start:start,interval_end:end,
            price:mark,floating_r:Some(floating/risk),floating_amount:multiplier.map(|m| floating*m),
            mfe:out.mfe.clone(),mae:out.mae.clone(),bars_held:out.summary.bars_held,hold_score:None,
            checks:json!({"stop":event.stop,"target_1r":target,"stop_hit":stop_hit,"target_hit":target_hit,
                "completed_15m":completed,"no_follow_bar":outcome::NO_FOLLOW_BAR,"no_follow_mfe_r":outcome::NO_FOLLOW_MFE_R,
                "no_follow":no_follow,"time_horizon":outcome::TIME_HORIZON_BARS,"time_exit":time_exit})});
        if let Some((reason, price)) = exit {
            out.exit_price = Some(price);
            out.summary.exit_ts = Some(b.datetime.clone());
            out.summary.exit_reason = Some(reason.into());
            out.summary.state = "closed".into();
            let points = (price - fill) * sign;
            out.summary.gross_r = Some(points / risk);
            out.summary.net_r = Some((points - 0.5 * tick) / risk);
            out.summary.gross_amount = multiplier.map(|m| points * m);
            out.summary.net_amount = multiplier.map(|m| (points - 0.5 * tick) * m);
            node(
                &mut out.nodes,
                "exit",
                &b.datetime,
                &b.datetime,
                recorded_at,
                origin,
                reason_label(reason).into(),
                json!({"reason":reason,"fill":price,"gross_r":out.summary.gross_r,"net_r":out.summary.net_r,"checks":out.holding.last().unwrap().checks}),
            );
            break;
        }
    }
    if let Some((reason, at)) = terminal {
        out.summary.state = match reason {
            "untriggered" | "invalidated" => "expired",
            x => x,
        }
        .into();
        out.summary.terminal_reason = Some(reason.into());
        out.summary.coverage.push(reason.into());
        node(
            &mut out.nodes,
            "terminal",
            &at,
            &at,
            recorded_at,
            origin,
            reason_label(reason).into(),
            json!({"reason":reason,"wait_bars":out.summary.wait_bars,"bars_held":out.summary.bars_held,"exit":false,
                "bar":raw.iter().find(|b|b.datetime==at),"planned_entry":event.entry,"stop":event.stop,
                "min_entry_score":2.5,"actual_entry_score":event.entry_score,"min_risk_ticks":10,"planned_risk_ticks":event.risk/tick,
                "pending_bars":outcome::PENDING_BARS,"max_entry_overshoot_points":(event.risk*0.2).max(2.*tick)}),
        );
    } else if out.entry_price.is_some() && out.summary.state != "closed" {
        out.summary.state = "open".into();
    }
    if let Some(waiting) = out.nodes.iter_mut().find(|n| n.stage == "waiting") {
        waiting.evidence["wait_bars"] = json!(out.summary.wait_bars);
        waiting.evidence["entry_ts"] = json!(out.summary.entry_ts);
        waiting.evidence["state"] = json!(out.summary.state);
    }
    if multiplier.is_none() {
        out.summary.coverage.push("missing_multiplier".into());
    }
    out.summary.mfe_r = out.mfe.r;
    out.summary.mae_r = out.mae.r;
    out.summary.mfe_amount = out.mfe.amount;
    out.summary.mae_amount = out.mae.amount;
    out.summary.entry_price = out.entry_price;
    out.summary.exit_price = out.exit_price;
    out.summary.mfe = Some(out.mfe.clone());
    out.summary.mae = Some(out.mae.clone());
    if out.mfe.precision == "bounded" || out.mae.precision == "bounded" {
        out.summary.coverage.push("bounded_excursion".into());
    }
    // Prevent undefined amounts/R from being mistaken for settlement on uncertain paths.
    if out.summary.state != "closed" {
        out.summary.gross_r = None;
        out.summary.net_r = None;
    }
    out
}
