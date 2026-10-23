//! Broad but finite mechanism experiments. All development, no broker or account cap.
use anyhow::{ensure, Context, Result};
use n_core::{
    derive::rollover::RolloverRecord,
    fetch::kline::Kline,
    trader::ExecutionConfig,
    v2::replay::ReplayEvent,
    v3::{
        ablation::{
            self, EntryGuard, EntryMode, Experiment, FeeRule, HolidayWindow, Policy, ResearchRules,
            ResultRow,
        },
        replay::Evaluation,
    },
};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    io::{BufWriter, Write},
    path::Path,
};
#[derive(Deserialize)]
struct Roll {
    symbol: String,
    ts: String,
    from_contract: String,
    to_contract: String,
    confirmed: bool,
}
#[derive(Deserialize)]
struct RawSymbol {
    bars: Vec<Kline>,
    rollovers: Vec<Roll>,
    variety: String,
}
#[derive(Deserialize)]
struct Input {
    symbols: BTreeMap<String, RawSymbol>,
    execution_config: ExecutionConfig,
}
#[derive(Deserialize)]
struct HolidayFile {
    coverage_start: String,
    coverage_end: String,
    windows: Vec<HolidayWindow>,
}
#[derive(Clone, Copy, Serialize)]
#[serde(rename_all = "snake_case")]
enum Filter {
    None,
    Trend,
    Participation,
    Score3,
    Friction20,
}
#[derive(Clone, Copy, Default, Serialize)]
#[serde(rename_all = "snake_case")]
enum BreadthFilter {
    #[default]
    None,
    RiskAtr,
    CloseQuality,
    RrBand,
    RiskAtrAndClose,
}
#[derive(Clone, Serialize)]
struct Candidate {
    name: &'static str,
    rules: ResearchRules,
    min_risk_ticks: u32,
    net_guard: bool,
    no_follow: bool,
    filter: Filter,
    min_score: f64,
    holiday_flat: bool,
    breadth_filter: BreadthFilter,
}
fn candidate(name: &'static str) -> Candidate {
    Candidate {
        name,
        rules: ResearchRules {
            min_actual_risk_ticks: Some(10.),
            ..Default::default()
        },
        min_risk_ticks: 10,
        net_guard: true,
        no_follow: true,
        filter: Filter::None,
        min_score: 2.5,
        holiday_flat: false,
        breadth_filter: BreadthFilter::None,
    }
}
fn definitions() -> Vec<Candidate> {
    let mut c = Vec::new();
    let mut x = candidate("touch_s1_risk10");
    x.net_guard = false;
    c.push(x);
    c.push(candidate("net_s1"));
    let mut x = candidate("net_s1_no_follow_off");
    x.no_follow = false;
    c.push(x);
    for (name, cap) in [("cap15", 1.5), ("cap20", 2.)] {
        let mut x = candidate(name);
        x.rules.target_cap_r = Some(cap);
        c.push(x);
    }
    let mut x = candidate("close_confirm");
    x.rules.entry_mode = EntryMode::CloseConfirmNextOpen;
    c.push(x);
    let mut x = candidate("retest_quarter");
    x.rules.entry_mode = EntryMode::BreakoutRetestQuarter;
    c.push(x);
    let mut x = candidate("early_fail2");
    x.rules.fast_fail_bucket = Some(2);
    c.push(x);
    let mut x = candidate("fresh4");
    x.rules.pending_buckets = Some(4);
    c.push(x);
    for (name, filter) in [
        ("trend_ema20", Filter::Trend),
        ("participation20", Filter::Participation),
        ("score3", Filter::Score3),
        ("friction20", Filter::Friction20),
    ] {
        let mut x = candidate(name);
        x.filter = filter;
        c.push(x);
    }
    let mut x = candidate("touch_s1_risk0");
    x.net_guard = false;
    x.min_risk_ticks = 0;
    x.rules.min_actual_risk_ticks = Some(0.);
    c.push(x);
    let mut x = candidate("net_s1_risk0");
    x.min_risk_ticks = 0;
    x.rules.min_actual_risk_ticks = Some(0.);
    c.push(x);
    for (name, mode) in [
        ("cap15_confirm", EntryMode::CloseConfirmNextOpen),
        ("cap15_retest", EntryMode::BreakoutRetestQuarter),
    ] {
        let mut x = candidate(name);
        x.rules.target_cap_r = Some(1.5);
        x.rules.entry_mode = mode;
        c.push(x);
    }
    let mut x = candidate("cap15_trend");
    x.rules.target_cap_r = Some(1.5);
    x.filter = Filter::Trend;
    c.push(x);
    let mut x = candidate("cap15_early_fail2");
    x.rules.target_cap_r = Some(1.5);
    x.rules.fast_fail_bucket = Some(2);
    c.push(x);
    // Second-stage mechanism combinations, disclosed separately from stage one.
    let mut x = candidate("score3_no_follow_off");
    x.min_score = 3.;
    x.no_follow = false;
    c.push(x);
    let mut x = candidate("score3_confirm");
    x.min_score = 3.;
    x.rules.entry_mode = EntryMode::CloseConfirmNextOpen;
    c.push(x);
    let mut x = candidate("score3_participation");
    x.min_score = 3.;
    x.filter = Filter::Participation;
    c.push(x);
    let mut x = candidate("score3_friction20");
    x.min_score = 3.;
    x.filter = Filter::Friction20;
    c.push(x);
    for (name, cap) in [("score3_cap15", 1.5), ("score3_cap20", 2.)] {
        let mut x = candidate(name);
        x.min_score = 3.;
        x.rules.target_cap_r = Some(cap);
        c.push(x);
    }
    let mut x = candidate("score3_confirm_no_follow_off");
    x.min_score = 3.;
    x.no_follow = false;
    x.rules.entry_mode = EntryMode::CloseConfirmNextOpen;
    c.push(x);
    let mut x = candidate("score3_participation_no_follow_off");
    x.min_score = 3.;
    x.no_follow = false;
    x.filter = Filter::Participation;
    c.push(x);
    let mut x = candidate("score3_flat_long_break");
    x.min_score = 3.;
    x.rules.flat_before_long_break = true;
    c.push(x);
    let mut x = candidate("score3_confirm_flat");
    x.min_score = 3.;
    x.rules.flat_before_long_break = true;
    x.rules.entry_mode = EntryMode::CloseConfirmNextOpen;
    c.push(x);
    let mut x = candidate("score3_skip_prebreak_entries");
    x.min_score = 3.;
    x.rules.skip_prebreak_entries = true;
    c.push(x);
    for (name, progress) in [("score3_flat_losers", 0.), ("score3_flat_unproven", 0.5)] {
        let mut x = candidate(name);
        x.min_score = 3.;
        x.rules.exit_before_break_below_r = Some(progress);
        c.push(x);
    }
    let mut x = candidate("score3_friction_flat_unproven");
    x.min_score = 3.;
    x.filter = Filter::Friction20;
    x.rules.exit_before_break_below_r = Some(0.5);
    c.push(x);
    let mut x = candidate("score3_confirm_flat_unproven");
    x.min_score = 3.;
    x.rules.entry_mode = EntryMode::CloseConfirmNextOpen;
    x.rules.exit_before_break_below_r = Some(0.5);
    c.push(x);
    c
}
fn breadth_definitions() -> Vec<Candidate> {
    let all = definitions();
    let mut defs: Vec<_> = all
        .into_iter()
        .filter(|d| {
            matches!(
                d.name,
                "net_s1" | "score3_friction20" | "score3_confirm_no_follow_off"
            )
        })
        .collect();
    for d in &mut defs {
        d.holiday_flat = true;
    }
    let net = defs.iter().find(|d| d.name == "net_s1").unwrap().clone();
    let fric = defs
        .iter()
        .find(|d| d.name == "score3_friction20")
        .unwrap()
        .clone();
    let confirm = defs
        .iter()
        .find(|d| d.name == "score3_confirm_no_follow_off")
        .unwrap()
        .clone();
    for (base, name, filter) in [
        (&net, "net_atr", BreadthFilter::RiskAtr),
        (&net, "net_close", BreadthFilter::CloseQuality),
        (&net, "net_rr_band", BreadthFilter::RrBand),
        (&net, "net_atr_close", BreadthFilter::RiskAtrAndClose),
        (&fric, "fric_atr", BreadthFilter::RiskAtr),
        (&fric, "fric_close", BreadthFilter::CloseQuality),
        (&fric, "fric_rr_band", BreadthFilter::RrBand),
        (&confirm, "confirm_atr", BreadthFilter::RiskAtr),
    ] {
        let mut d = base.clone();
        d.name = name;
        d.breadth_filter = filter;
        defs.push(d);
    }
    let mut d = net.clone();
    d.name = "net_volume";
    d.filter = Filter::Participation;
    defs.push(d);
    for (base, name) in [
        (&net, "net_weekend"),
        (&fric, "fric_weekend"),
        (&confirm, "confirm_weekend"),
    ] {
        let mut d = base.clone();
        d.name = name;
        d.rules.flat_before_weekend = true;
        defs.push(d);
    }
    for (base, name) in [(&net, "net_hold24"), (&fric, "fric_hold24")] {
        let mut d = base.clone();
        d.name = name;
        d.rules.max_holding_buckets = Some(24);
        defs.push(d);
    }
    for (base, name) in [(&net, "net_cap20_weekend"), (&fric, "fric_cap20_weekend")] {
        let mut d = base.clone();
        d.name = name;
        d.rules.target_cap_r = Some(2.);
        d.rules.flat_before_weekend = true;
        defs.push(d);
    }
    let mut d = fric.clone();
    d.name = "fric_no_follow_off";
    d.no_follow = false;
    defs.push(d);
    let mut d = fric.clone();
    d.name = "fric_confirm";
    d.no_follow = false;
    d.rules.entry_mode = EntryMode::CloseConfirmNextOpen;
    defs.push(d);
    // Stage two combines only the mechanisms diagnosed in the first21 cases.
    // Thresholds stay fixed; later-stage selection is disclosed in the manifest.
    for (base, name, atr, close, weekend, no_follow_off) in [
        (&fric, "fric_atr_weekend", true, false, true, false),
        (&fric, "fric_atr_no_follow_off", true, false, false, true),
        (
            &fric,
            "fric_atr_weekend_no_follow_off",
            true,
            false,
            true,
            true,
        ),
        (&fric, "fric_atr_close", true, true, false, false),
        (&net, "net_atr_close_weekend", true, true, true, false),
        (&confirm, "confirm_atr_weekend", true, false, true, true),
        (
            &fric,
            "fric_no_follow_off_weekend",
            false,
            false,
            true,
            true,
        ),
    ] {
        let mut d = base.clone();
        d.name = name;
        d.breadth_filter = if close {
            BreadthFilter::RiskAtrAndClose
        } else if atr {
            BreadthFilter::RiskAtr
        } else {
            BreadthFilter::None
        };
        d.rules.flat_before_weekend = weekend;
        if no_follow_off {
            d.no_follow = false;
        }
        defs.push(d);
    }
    // Third stage tests whether a2R payout cap broadens the already improved
    // warning/ATR/weekend branches. No new numeric thresholds are searched.
    for (base, name, close, cap, no_follow_off) in [
        (&net, "net_atr_close_cap20_weekend", true, true, false),
        (&fric, "fric_atr_cap20_weekend", false, true, false),
        (
            &net,
            "net_atr_close_weekend_no_follow_off",
            true,
            false,
            true,
        ),
        (
            &net,
            "net_atr_close_cap20_weekend_no_follow_off",
            true,
            true,
            true,
        ),
    ] {
        let mut d = base.clone();
        d.name = name;
        d.rules.flat_before_weekend = true;
        d.breadth_filter = if close {
            BreadthFilter::RiskAtrAndClose
        } else {
            BreadthFilter::RiskAtr
        };
        if cap {
            d.rules.target_cap_r = Some(2.);
        }
        if no_follow_off {
            d.no_follow = false;
        }
        defs.push(d);
    }
    defs
}
#[derive(Clone)]
struct ContextBar {
    ts: String,
    close: f64,
    volume: f64,
    ema: f64,
    open: f64,
    high: f64,
    low: f64,
    atr14: f64,
}
fn context_bars(raw: &[Kline]) -> Vec<ContextBar> {
    let mut groups = BTreeMap::<String, (usize, f64, f64, String, f64, f64, f64)>::new();
    for b in raw {
        let Ok(t) = chrono::NaiveDateTime::parse_from_str(&b.datetime, "%Y-%m-%d %H:%M:%S") else {
            continue;
        };
        // Preserve end-labelled midnight:00:00 belongs with23:50/23:55,
        // not with the next00:15 bucket. The shared legacy helper forces the
        // first daily bucket to00:15; correct only these offline features here.
        let midnight = t.date().and_hms_opt(0, 0, 0).unwrap();
        let elapsed = (t - midnight).num_minutes();
        let bucket = (midnight + chrono::Duration::minutes((elapsed + 14) / 15 * 15))
            .format("%Y-%m-%d %H:%M:%S")
            .to_string();
        let a = groups.entry(bucket).or_default();
        if a.0 == 0 {
            a.4 = b.open;
            a.5 = b.high;
            a.6 = b.low;
        } else {
            a.5 = a.5.max(b.high);
            a.6 = a.6.min(b.low);
        }
        a.0 += 1;
        a.1 = b.close;
        a.2 += b.volume;
        a.3 = b.datetime.clone();
    }
    let mut bars: Vec<_> = groups
        .into_iter()
        .filter(|(ts, (n, _, _, last, _, _, _))| *n == 3 && ts == last)
        .map(|(ts, (_, close, volume, _, open, high, low))| ContextBar {
            ts,
            close,
            volume,
            ema: f64::NAN,
            open,
            high,
            low,
            atr14: f64::NAN,
        })
        .collect();
    let tr: Vec<_> = bars
        .iter()
        .enumerate()
        .map(|(i, b)| {
            if i == 0 {
                b.high - b.low
            } else {
                (b.high - b.low)
                    .max((b.high - bars[i - 1].close).abs())
                    .max((b.low - bars[i - 1].close).abs())
            }
        })
        .collect();
    for i in 13..bars.len() {
        bars[i].atr14 = tr[i - 13..=i].iter().sum::<f64>() / 14.;
    }
    if bars.len() >= 20 {
        let mut ema = bars[..20].iter().map(|b| b.close).sum::<f64>() / 20.;
        bars[19].ema = ema;
        for b in &mut bars[20..] {
            ema += (b.close - ema) * 2. / 21.;
            b.ema = ema;
        }
    }
    bars
}
fn breadth_pass(filter: BreadthFilter, ev: &ReplayEvent, bars: &[ContextBar]) -> bool {
    if matches!(filter, BreadthFilter::None) {
        return true;
    }
    if matches!(filter, BreadthFilter::RrBand) {
        return ev.rr.is_finite() && ev.rr <= 3.;
    }
    let n = bars.partition_point(|b| b.ts <= ev.warning_ts);
    if n < 14 || bars[n - 1].ts != ev.warning_ts {
        return false;
    }
    let b = &bars[n - 1];
    if !b.atr14.is_finite() || b.atr14 <= 0. {
        return false;
    }
    let risk = ev.risk / b.atr14;
    let atr = (0.75..=2.5).contains(&risk);
    let sign = if ev.direction == "up" { 1. } else { -1. };
    let loc = if b.high > b.low {
        if sign > 0. {
            (b.close - b.low) / (b.high - b.low)
        } else {
            (b.high - b.close) / (b.high - b.low)
        }
    } else {
        0.
    };
    let close = loc >= 0.65 && (b.close - b.open) * sign / b.atr14 >= 0.25;
    match filter {
        BreadthFilter::RiskAtr => atr,
        BreadthFilter::CloseQuality => close,
        BreadthFilter::RiskAtrAndClose => atr && close,
        _ => unreachable!(),
    }
}
fn context_pass(
    filter: Filter,
    ev: &ReplayEvent,
    bars: &[ContextBar],
    fee: EntryGuard,
    mult: f64,
) -> bool {
    let sign = if ev.direction == "up" { 1. } else { -1. };
    match filter {
        Filter::None => true,
        Filter::Score3 => ev.setup_features.setup_quality >= 3.,
        Filter::Friction20 => {
            let fill = ev.entry + sign * ev.tick_size;
            let stop = ev.stop - sign * ev.tick_size;
            (2. * ev.tick_size * mult + fee.commission_bound(fill, stop, mult))
                <= ev.risk * mult * 0.2 + 1e-7
        }
        Filter::Trend | Filter::Participation => {
            let n = bars.partition_point(|b| b.ts <= ev.warning_ts);
            if n < 24 || bars[n - 1].ts != ev.warning_ts {
                return false;
            }
            let current = &bars[n - 1];
            match filter {
                Filter::Trend => {
                    (current.close - current.ema) * sign > 0.
                        && (current.ema - bars[n - 4].ema) * sign > 0.
                }
                _ => {
                    let mut volumes: Vec<_> =
                        bars[n - 21..n - 1].iter().map(|b| b.volume).collect();
                    volumes.sort_by(f64::total_cmp);
                    let median = (volumes[9] + volumes[10]) * 0.5;
                    median > 0. && current.volume >= median
                }
            }
        }
    }
}
fn fee(spec: &Value) -> Result<EntryGuard> {
    let mut g = EntryGuard {
        min_net_reward_loss: 1.,
        assumed_slip_ticks: 1.,
        open: FeeRule::default(),
        close_today: FeeRule::default(),
        close_yesterday: FeeRule::default(),
        roundtrip_fixed_override: None,
    };
    if let Some(value) = spec.get("roundtrip_fixed_yuan_per_lot") {
        g.roundtrip_fixed_override = Some(value.as_f64().context("fee override")?);
    } else {
        g.open = serde_json::from_value(spec["open"].clone())?;
        g.close_today = serde_json::from_value(spec["close_today"].clone())?;
        g.close_yesterday = serde_json::from_value(spec["close_yesterday"].clone())?;
    }
    ensure!(
        [
            g.open.fixed_yuan_per_lot,
            g.open.notional_rate,
            g.close_today.fixed_yuan_per_lot,
            g.close_today.notional_rate,
            g.close_yesterday.fixed_yuan_per_lot,
            g.close_yesterday.notional_rate,
            g.roundtrip_fixed_override.unwrap_or(0.)
        ]
        .iter()
        .all(|x| x.is_finite() && *x >= 0.),
        "invalid fee schedule"
    );
    Ok(g)
}
fn writer(p: &Path) -> Result<BufWriter<std::fs::File>> {
    Ok(BufWriter::new(
        std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(p)?,
    ))
}
fn main() -> Result<()> {
    let args = std::env::args().collect::<Vec<_>>();
    ensure!(
        args.len() == 5 || args.len() == 6 || (args.len()==7 && args[6]=="--breadth"),
        "usage: signal-search <input> <signals> <fees> <new-output-dir> [published-holidays.json [--breadth]]"
    );
    ensure!(
        chrono::Local::now().offset().local_minus_utc() == 8 * 3600,
        "requires Asia/Shanghai local timezone"
    );
    let bytes = std::fs::read(&args[1])?;
    let sbytes = std::fs::read(&args[2])?;
    let fbytes = std::fs::read(&args[3])?;
    let input: Input = serde_json::from_slice(&bytes)?;
    let signals: Vec<ReplayEvent> = serde_json::from_slice(&sbytes)?;
    let fees: Value = serde_json::from_slice(&fbytes)?;
    let holiday_bytes = if args.len() >= 6 {
        Some(std::fs::read(&args[5])?)
    } else {
        None
    };
    let holiday_file: Option<HolidayFile> = holiday_bytes
        .as_ref()
        .map(|b| serde_json::from_slice(b))
        .transpose()?;
    if let Some(h) = &holiday_file {
        ensure!(!h.windows.is_empty(), "explicit holiday schedule required");
        for raw in input.symbols.values() {
            for bar in &raw.bars {
                ensure!(
                    bar.datetime.as_str() >= h.coverage_start.as_str()
                        && bar.datetime.as_str() <= h.coverage_end.as_str(),
                    "holiday schedule does not cover source data"
                );
            }
        }
        for w in &h.windows {
            for t in [&w.block_from, &w.flat_at, &w.last_close, &w.reopen] {
                chrono::NaiveDateTime::parse_from_str(t, "%Y-%m-%d %H:%M:%S")?;
            }
            ensure!(
                w.block_from < w.flat_at && w.flat_at < w.last_close && w.last_close < w.reopen,
                "invalid holiday chronology"
            );
        }
        ensure!(
            h.windows.windows(2).all(|w| w[0].reopen < w[1].block_from),
            "overlapping holidays"
        );
    }
    let out = Path::new(&args[4]);
    ensure!(!out.exists(), "frozen output already exists");
    std::fs::create_dir(out)?;
    let mut defs = definitions();
    if holiday_file.is_some() {
        defs.retain(|d| {
            matches!(
                d.name,
                "net_s1" | "score3_friction20" | "score3_confirm_no_follow_off"
            )
        });
        let mut adjusted = defs.clone();
        for d in &mut adjusted {
            d.holiday_flat = true;
            d.name = match d.name {
                "net_s1" => "net_s1_holiday_flat",
                "score3_friction20" => "score3_friction20_holiday_flat",
                "score3_confirm_no_follow_off" => "score3_confirm_no_follow_off_holiday_flat",
                _ => unreachable!(),
            };
        }
        defs.extend(adjusted);
    }
    if args.len() == 7 {
        defs = breadth_definitions();
    }
    let mut manifest = json!({"source":"historical_development_exploration","definitions":defs,"case_count":defs.len()*3,
        "account_cap":null,"quantity":1,"slips":[0,1,2],"gate_forecast_slip":1,
        "exploration_stages":[19,10,5],"later_stages_chosen_after_earlier_results":true,
        "flat_rule":"existing declared session calendar, within10min of close with no trading30min later; 5m close plus slippage",
        "cohort":"all warning score>=2.5 with positive risk; risk0 and risk10 cases share this universe",
        "input_sha256":format!("{:x}",Sha256::digest(&bytes)),"signals_sha256":format!("{:x}",Sha256::digest(&sbytes)),"fees_sha256":format!("{:x}",Sha256::digest(&fbytes)),
        "engine_source_sha256":format!("{:x}",Sha256::digest(include_str!("../v3/ablation.rs").as_bytes())),
        "runner_source_sha256":format!("{:x}",Sha256::digest(include_str!("signal_search.rs").as_bytes())),
        "warning_features":"completed raw 15m triples only; EMA20 with 3-bar slope; warning volume versus prior20 median",
        "retest":"first breakout must occur in an earlier 5m interval; then market on 0.25R retouch, no same-bar arm/fill",
        "confirm":"first completed5m close above breakout then next5m interval open; no use of confirming close as a fill",
        "intrabar":"two monotone paths agree only, not all possible real paths","qualified_for_trading":false,"production_changed":false});
    if let Some(bytes) = &holiday_bytes {
        manifest["holiday_schedule"] = serde_json::from_slice(bytes)?;
        manifest["holiday_schedule_sha256"] = json!(format!("{:x}", Sha256::digest(bytes)));
        manifest["exploration_stages"] = json!([3, 3]);
        manifest["later_stages_chosen_after_earlier_results"] = json!(false);
        manifest["holiday_policy"] = json!("fixed14:30 block entries,14:45 flatten before published multi-day holidays only; no weekend/lunch flatten; missing cutoff quote remains unknown");
    }
    if args.len() == 7 {
        manifest["exploration_stages"] = json!([21, 7, 4]);
        manifest["later_stages_chosen_after_earlier_results"] = json!(true);
        manifest["study"]=json!("profit breadth; fixed finite causal mechanisms; all holiday-flat; controls use original names with holiday policy enabled");
        manifest["breadth_filters"] = json!({"risk_atr":"planned risk / completed15m simple14 TR mean in[.75,2.5]", "close_quality":"completed warning15m directional close location>=.65 and directional body>=.25ATR14", "rr_band":"planned gross S1 reward/risk<=3 with existing net-space minimum", "weekend":"flat within10minutes of last Friday-night session close, Friday15 for no-night symbols; no future prices", "hold24":"max24 completed15m holding buckets"});
        manifest["offline_weekend_day_only_correction"] = json!({"symbols":["SF0","SM0"],"reason":"existing shared default falls through to23:00 but frozen archive has no night bars; published product contracts specify day hours, night requires separate notice; shared production calendar unchanged"});
        manifest["diagnostic_targets"] = json!({"primary_and_stress_net_cash_and_r_positive":true,"remove_best3_exit_dates_net_positive_both_slips":true,"top3_positive_symbol_net_share_max":0.6,"at_least150_primary_closed":true,"all_are_development_diagnostics_not_trading_qualification":true});
    }
    writeln!(
        writer(&out.join("manifest.json"))?,
        "{}",
        serde_json::to_string_pretty(&manifest)?
    )?;
    let contexts: BTreeMap<_, _> = input
        .symbols
        .iter()
        .map(|(s, r)| (s.clone(), context_bars(&r.bars)))
        .collect();
    let mut rows = writer(&out.join("evaluations.jsonl"))?;
    let mut count = 0;
    for ev in signals {
        if ev.setup_features.setup_quality < 2.5 || ev.risk <= 0. {
            continue;
        }
        count += 1;
        let raw = input.symbols.get(&ev.symbol).context("raw symbol")?;
        let rolls: Vec<_> = raw
            .rollovers
            .iter()
            .map(|r| RolloverRecord {
                symbol: r.symbol.clone(),
                ts: r.ts.clone(),
                from_contract: r.from_contract.clone(),
                to_contract: r.to_contract.clone(),
                confirmed: r.confirmed,
            })
            .collect();
        let mult = n_core::precision::effective_multiplier(&ev.symbol, &raw.variety);
        let product = ev
            .symbol
            .trim_end_matches(|c: char| c.is_ascii_digit())
            .to_uppercase();
        let guard = fee(fees["products"]
            .get(&product)
            .context("product fee missing")?)?;
        let key = format!(
            "{}|{}|{}|{}|{}|{}",
            ev.symbol, ev.direction, ev.s0_ts, ev.s1_ts, ev.s2_ts, ev.warning_ts
        );
        for def in &defs {
            let pass = ev.setup_features.setup_quality >= def.min_score
                && context_pass(def.filter, &ev, &contexts[&ev.symbol], guard, mult)
                && breadth_pass(def.breadth_filter, &ev, &contexts[&ev.symbol]);
            let mut cfg = input.execution_config.clone();
            cfg.min_entry_risk_ticks = def.min_risk_ticks;
            for slip in [0., 1., 2.] {
                let result = if !pass {
                    ResultRow {
                        evaluation: Evaluation {
                            status: "filtered_warning_context".into(),
                            ..Default::default()
                        },
                        disagreement: None,
                        fills: vec![],
                    }
                } else {
                    ablation::evaluate_research_with_holidays(
                        &ev,
                        &raw.bars,
                        &rolls,
                        &cfg,
                        Experiment {
                            policy: Policy::OriginalS1,
                            no_follow_bar: if def.no_follow { Some(5) } else { None },
                            slip_ticks: slip,
                            fee_ticks: 0.,
                        },
                        mult,
                        if def.net_guard { Some(guard) } else { None },
                        def.rules,
                        if def.holiday_flat {
                            &holiday_file
                                .as_ref()
                                .expect("validated holiday file")
                                .windows
                        } else {
                            &[]
                        },
                    )
                };
                writeln!(
                    rows,
                    "{}",
                    json!({"event_key":key,"symbol":ev.symbol,"direction":ev.direction,"warning_ts":ev.warning_ts,
                    "score":ev.setup_features.setup_quality,"rr":ev.rr,"tick_size":ev.tick_size,"multiplier":mult,
                    "scenario":format!("{}/slip{}",def.name,slip as i32),"result":result,
                    "planned_risk":ev.risk,"planned_risk_ticks":ev.risk/ev.tick_size,
                    "warning_context":contexts[&ev.symbol].iter().rev().find(|b|b.ts<=ev.warning_ts).map(|b|json!({"as_of":b.ts,"atr14":b.atr14,"open":b.open,"high":b.high,"low":b.low,"close":b.close,"volume":b.volume}))})
                )?;
            }
        }
    }
    rows.flush()?;
    println!(
        "cohort={count}, candidates={}, cases={}",
        defs.len(),
        defs.len() * 3
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn completed_context_midnight_bar_belongs_to_the_previous_interval() {
        let raw: Vec<_> = [
            "2026-10-21 23:50:00",
            "2026-10-21 23:55:00",
            "2026-10-22 00:00:00",
        ]
        .iter()
        .enumerate()
        .map(|(i, t)| Kline {
            datetime: t.to_string(),
            open: 10. + i as f64,
            high: 12. + i as f64,
            low: 9.,
            close: 11. + i as f64,
            volume: 1.,
            hold: 1.,
        })
        .collect();
        let bars = context_bars(&raw);
        assert_eq!(bars.len(), 1);
        assert_eq!(bars[0].ts, "2026-10-22 00:00:00");
        assert_eq!(bars[0].open, 10.);
        assert_eq!(bars[0].close, 13.);
        assert_eq!(bars[0].volume, 3.);
    }
    #[test]
    fn context_ignores_future_and_incomplete_warning_bars() {
        let e = ReplayEvent {
            warning_ts: "2026-10-22 09:15:00".into(),
            direction: "up".into(),
            ..Default::default()
        };
        let g = EntryGuard {
            min_net_reward_loss: 1.,
            assumed_slip_ticks: 1.,
            open: FeeRule::default(),
            close_today: FeeRule::default(),
            close_yesterday: FeeRule::default(),
            roundtrip_fixed_override: None,
        };
        let bars = vec![ContextBar {
            ts: "2026-10-22 09:30:00".into(),
            close: 1e9,
            volume: 1e9,
            ema: 1.,
            open: 1.,
            high: 2.,
            low: 0.,
            atr14: 1.,
        }];
        assert!(!context_pass(Filter::Trend, &e, &bars, g, 10.));
        assert!(!context_pass(Filter::Participation, &e, &bars, g, 10.));
    }
    #[test]
    fn breadth_features_use_only_complete_warning_bars_and_direction() {
        let mut e = ReplayEvent {
            warning_ts: "2026-10-22 10:00:00".into(),
            direction: "up".into(),
            risk: 1.,
            rr: 2.,
            ..Default::default()
        };
        let mut bars: Vec<_> = (0..14)
            .map(|i| ContextBar {
                ts: format!("2026-10-21 {:02}:00:00", i),
                open: 10.,
                high: 12.,
                low: 10.,
                close: 11.8,
                volume: 1.,
                ema: 11.,
                atr14: 1.,
            })
            .collect();
        bars[13].ts = e.warning_ts.clone();
        assert!(breadth_pass(BreadthFilter::RiskAtrAndClose, &e, &bars));
        e.direction = "down".into();
        assert!(!breadth_pass(BreadthFilter::CloseQuality, &e, &bars));
        e.direction = "up".into();
        bars[13].ts = "2026-10-22 10:15:00".into();
        assert!(!breadth_pass(BreadthFilter::RiskAtr, &e, &bars));
    }
}
