//! Prospective, paired signal comparison. This module has no broker client.
//! All four lanes use the same quotes and fill/cost model; none reserve capital.
use std::collections::{BTreeMap, BTreeSet};

use chrono::{Days, Local, TimeZone};
use serde::{Deserialize, Serialize};

use super::execution::{ConditionOrder, ExecutionConfig};
use super::model::ExecutionQuote;
use super::overnight::check_pre_close_checkpoint;
use crate::analyze::outcome::{NO_FOLLOW_BAR, NO_FOLLOW_MFE_R, TIME_HORIZON_BARS};

pub const COMPARISON_VERSION: &str = "paired-quote-v3-shared-policy";
pub const COST_TICKS: f64 = 2.5;
pub const FILTER_TICKS: u32 = 10;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExperimentDefinition {
    pub version: String,
    pub id: String,
    pub started_at: i64,
    pub config_hash: String,
    pub execution_config: ExecutionConfig,
    pub candidate_min_risk_ticks: u32,
    pub extra_roundtrip_cost_ticks: f64,
    pub close_minutes_before: u32,
    pub basis: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ShadowFill {
    pub at: i64,
    pub quote_at: i64,
    pub price: f64,
    pub volume: i32,
    pub reason: String,
    pub gross_amount: f64,
    pub estimated_extra_cost: f64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct VariantTrade {
    pub id: String,
    pub label: String,
    pub min_risk_ticks: u32,
    pub flat_before_close: bool,
    pub status: String,
    pub reason: String,
    pub entered_at: Option<i64>,
    pub entry_quote_at: Option<i64>,
    pub fill_price: Option<f64>,
    pub initial_risk: Option<f64>,
    pub remaining: i32,
    pub stop: f64,
    pub target: f64,
    pub first_leg_closed: bool,
    pub trailing_grade: usize,
    pub max_mfe_r: f64,
    pub max_mae_r: f64,
    pub bars_held: usize,
    pub flat_at: Option<i64>,
    pub pending_exit: Option<String>,
    pub fills: Vec<ShadowFill>,
}

impl VariantTrade {
    fn new(id: &str, label: &str, min_risk_ticks: u32, flat_before_close: bool) -> Self {
        Self {
            id: id.into(),
            label: label.into(),
            min_risk_ticks,
            flat_before_close,
            status: "pending".into(),
            reason: "等待有效行情".into(),
            entered_at: None,
            entry_quote_at: None,
            fill_price: None,
            initial_risk: None,
            remaining: 0,
            stop: 0.0,
            target: 0.0,
            first_leg_closed: false,
            trailing_grade: 0,
            max_mfe_r: 0.0,
            max_mae_r: 0.0,
            bars_held: 0,
            flat_at: None,
            pending_exit: None,
            fills: Vec::new(),
        }
    }
    fn exit(
        &mut self,
        signal: &ConditionOrder,
        q: &ExecutionQuote,
        now: i64,
        qty: i32,
        reason: &str,
    ) {
        let price = if signal.direction == "up" {
            q.bid
        } else {
            q.ask
        };
        let sign = if signal.direction == "up" { 1.0 } else { -1.0 };
        let multiplier = crate::precision::effective_multiplier(&signal.instrument_id, "");
        self.fills.push(ShadowFill {
            at: now,
            quote_at: q.timestamp,
            price,
            volume: qty,
            reason: reason.into(),
            gross_amount: (price - self.fill_price.unwrap()) * sign * multiplier * qty as f64,
            estimated_extra_cost: COST_TICKS * q.tick * multiplier * qty as f64,
        });
        self.remaining -= qty;
        if self.remaining == 0 {
            self.status = "closed".into();
            self.reason = reason.into();
            self.pending_exit = None;
        }
    }
    fn update(&mut self, signal: &ConditionOrder, q: &ExecutionQuote, now: i64, repeated: bool) {
        let sign = if signal.direction == "up" { 1.0 } else { -1.0 };
        if self.status == "pending" {
            if signal.risk / q.tick + 1e-6 < self.min_risk_ticks as f64 {
                self.status = "filtered".into();
                self.reason = "计划止损距离不足门槛".into();
                return;
            }
            if (q.last - signal.stop_price) * sign <= 0.0 {
                self.status = "invalidated".into();
                self.reason = "触发前已越过止损".into();
                return;
            }
            if (q.last - signal.entry_price) * sign < 0.0 {
                return;
            }
            if self.flat_before_close && in_flat_window(&signal.symbol, now) {
                self.reason = "收盘前不新开仓".into();
                return;
            }
            let price = if sign > 0.0 { q.ask } else { q.bid };
            if (price - signal.entry_price) * sign > (signal.risk * 0.2).max(2.0 * q.tick) + 1e-6 {
                self.reason = "盘口超出入场偏移限制".into();
                return;
            }
            let risk = (price - signal.stop_price) * sign;
            if risk <= 0.0 {
                return;
            }
            self.status = "open".into();
            self.reason = "影子盘口成交".into();
            self.entered_at = Some(now);
            self.entry_quote_at = Some(q.timestamp);
            self.fill_price = Some(price);
            self.initial_risk = Some(risk);
            self.remaining = signal.volume;
            self.stop = signal.stop_price;
            self.target = if (signal.target_price - signal.entry_price) * sign > signal.risk {
                signal.target_price
            } else {
                price + sign * risk
            };
            if self.flat_before_close {
                self.flat_at = next_flat_checkpoint(&signal.symbol, now);
            }
            return; // Entry and an exit cannot both be inferred from one observed quote.
        }
        if self.status != "open" {
            return;
        }
        let forced = self.flat_at.is_some_and(|at| now >= at);
        if forced && self.flat_at.is_some_and(|at| q.timestamp < at) {
            return; // A cached pre-checkpoint book is not a close-window fill.
        }
        if repeated && !forced && self.pending_exit.is_none() {
            return;
        }
        let r = (q.last - self.fill_price.unwrap()) * sign / self.initial_risk.unwrap();
        self.max_mfe_r = self.max_mfe_r.max(r);
        self.max_mae_r = self.max_mae_r.min(r);
        let reason = if (q.last - self.stop) * sign <= 0.0 {
            Some(if self.first_leg_closed {
                if self.trailing_grade > 1 {
                    "trailing"
                } else {
                    "breakeven"
                }
            } else {
                "stop"
            })
        } else if forced {
            Some("pre_close")
        } else {
            self.pending_exit.as_deref()
        };
        if let Some(reason) = reason.map(str::to_owned) {
            self.exit(signal, q, now, self.remaining, &reason);
        } else {
            let update = super::policy::on_price(
                signal.tier,
                self.fill_price.unwrap(),
                self.initial_risk.unwrap(),
                sign,
                q.last,
                self.target,
                self.stop,
                self.max_mfe_r,
                self.first_leg_closed,
            );
            self.stop = update.stop;
            if (self.stop - self.fill_price.unwrap()) * sign > 0.0 {
                self.trailing_grade = self.trailing_grade.max(2)
            }
            if let Some(intent) = update.intent {
                self.exit(
                    signal,
                    q,
                    now,
                    if intent.all {
                        self.remaining
                    } else {
                        self.remaining.min(1)
                    },
                    intent.reason,
                );
                if !intent.all {
                    self.first_leg_closed = true;
                    self.trailing_grade = 1;
                    self.stop = self.fill_price.unwrap();
                }
            }
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PairedSignal {
    pub condition: ConditionOrder,
    pub observed_at: i64,
    pub signal_evidence: Option<serde_json::Value>,
    pub first_quote_at: Option<i64>,
    pub tick_at_decision: Option<f64>,
    pub planned_risk_ticks: Option<f64>,
    pub bars_waited: usize,
    pub last_bar: Option<String>,
    pub last_quote: Option<(i64, f64, f64, f64)>,
    pub quote_gap_count: usize,
    pub variants: Vec<VariantTrade>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ComparisonRun {
    pub definition: ExperimentDefinition,
    pub signals: BTreeMap<String, PairedSignal>,
    #[serde(default)]
    archive: RunArchive,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
struct RunArchive {
    signals: usize,
    groups: Vec<VariantStats>,
    pairs: Vec<PairedEffect>,
    recent: Vec<serde_json::Value>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct ComparisonBook {
    pub active_run: Option<String>,
    pub runs: Vec<ComparisonRun>,
    #[serde(default)]
    pub archive_error: Option<String>,
}

impl ComparisonBook {
    pub fn register(&mut self, condition: &ConditionOrder, cfg: &ExecutionConfig, now: i64) {
        if self
            .runs
            .iter()
            .any(|run| run.signals.contains_key(&condition.id))
        {
            return;
        }
        let signature = format!(
            "{COMPARISON_VERSION}:{}",
            serde_json::to_string(cfg).unwrap()
        );
        let hash = blake3::hash(signature.as_bytes()).to_hex().to_string();
        if !self
            .runs
            .last()
            .is_some_and(|r| r.definition.config_hash == hash)
        {
            let id = format!("cmp-{}", uuid::Uuid::new_v4());
            self.active_run = Some(id.clone());
            self.runs.push(ComparisonRun {
                definition: ExperimentDefinition {
                    version: COMPARISON_VERSION.into(),
                    id,
                    started_at: now,
                    config_hash: hash,
                    execution_config: cfg.clone(),
                    candidate_min_risk_ticks: FILTER_TICKS,
                    extra_roundtrip_cost_ticks: COST_TICKS,
                    close_minutes_before: 2,
                    basis: "同信号盘口影子推演；首手1R兑现，次手1.6R起回撤0.8R连续追踪；不模拟五万元组合保证金和排队成交".into(),
                },
                signals: BTreeMap::new(),
                archive: RunArchive::default(),
            });
        }
        self.runs.last_mut().unwrap().signals.insert(
            condition.id.clone(),
            PairedSignal {
                condition: condition.clone(),
                observed_at: now,
                signal_evidence: None,
                first_quote_at: None,
                tick_at_decision: None,
                planned_risk_ticks: None,
                bars_waited: 0,
                last_bar: Some(condition.warning_bar_ts.clone()),
                last_quote: None,
                quote_gap_count: 0,
                variants: vec![
                    VariantTrade::new("base_hold", "原规则·常规退出", 0, false),
                    VariantTrade::new("filter_hold", "10 tick·常规退出", FILTER_TICKS, false),
                    VariantTrade::new("base_flat", "原规则·收盘前清仓", 0, true),
                    VariantTrade::new("filter_flat", "10 tick·收盘前清仓", FILTER_TICKS, true),
                ],
            },
        );
    }

    pub fn attach_evidence(&mut self, condition_id: &str, evidence: serde_json::Value) {
        if let Some(signal) = self
            .runs
            .iter_mut()
            .find_map(|r| r.signals.get_mut(condition_id))
        {
            if signal.signal_evidence.is_none() {
                if evidence.get("outcome").is_some_and(|v| !v.is_null())
                    || matches!(evidence["state"].as_str(), Some("closed" | "expired"))
                {
                    for lane in &mut signal.variants {
                        lane.status = "not_prospective".into();
                        lane.reason = "首次观察时结局已知，不计入前瞻比较".into();
                    }
                }
                signal.signal_evidence = Some(evidence);
            }
        }
    }

    pub fn advance(
        &mut self,
        quotes: &std::collections::HashMap<String, ExecutionQuote>,
        now: i64,
    ) -> bool {
        let mut changed = false;
        for run in &mut self.runs {
            for record in run.signals.values_mut() {
                if !record
                    .variants
                    .iter()
                    .any(|v| v.status == "pending" || v.status == "open")
                {
                    continue;
                }
                let before = record.variants.clone();
                // Missing a required close window is an invalid observation, never
                // a fabricated fill at the next session's price.
                for lane in &mut record.variants {
                    if lane.status == "open" && lane.flat_at.is_some_and(|at| now >= at + 120) {
                        lane.status = "missing_data".into();
                        lane.reason = "错过收盘前清仓窗口，排除该组收益统计".into();
                    }
                    if run.definition.version != COMPARISON_VERSION
                        && (lane.status == "open" || lane.status == "pending")
                    {
                        lane.status = "missing_data".into();
                        lane.reason = "推演版本变化，保留旧批次且停止续算".into();
                    }
                }
                changed |= before != record.variants;
                let Some(q) = quotes
                    .get(&record.condition.instrument_id.to_uppercase())
                    .filter(|q| {
                        q.instrument_id
                            .eq_ignore_ascii_case(&record.condition.instrument_id)
                            && q.valid_at(now)
                            && q.bid >= q.lower
                            && q.ask <= q.upper
                            && q.bid > 0.0
                            && q.ask >= q.bid
                    })
                else {
                    continue;
                };
                if q.timestamp < record.observed_at {
                    continue;
                }
                if record.first_quote_at.is_none() {
                    record.first_quote_at = Some(q.timestamp);
                    record.tick_at_decision = Some(q.tick);
                    record.planned_risk_ticks = Some(record.condition.risk / q.tick);
                    changed = true;
                }
                let frame = (q.timestamp, q.last, q.bid, q.ask);
                let repeated = record.last_quote == Some(frame);
                if record.last_quote.is_some_and(|old| q.timestamp < old.0) {
                    continue;
                }
                record.last_quote = Some(frame);
                for lane in &mut record.variants {
                    lane.update(&record.condition, q, now, repeated);
                }
                changed |= !repeated || before != record.variants;
            }
        }
        changed
    }

    pub fn on_bar(&mut self, symbol: &str, bar: &str) {
        for record in self
            .runs
            .iter_mut()
            .flat_map(|r| r.signals.values_mut())
            .filter(|s| s.condition.symbol == symbol)
        {
            if record.last_bar.as_deref().is_some_and(|old| old >= bar) {
                continue;
            }
            let first = record.last_bar.replace(bar.into()).is_none();
            if first {
                continue;
            } // Recovered/current bar establishes a baseline.
            if bar > record.condition.warning_bar_ts.as_str() {
                record.bars_waited += 1;
            }
            for lane in &mut record.variants {
                if lane.status == "pending" && record.bars_waited > record.condition.max_wait_bars {
                    lane.status = "expired".into();
                    lane.reason = "等待触发超过有效K线数".into();
                } else if lane.status == "open" {
                    lane.bars_held += 1;
                    if lane.bars_held >= TIME_HORIZON_BARS {
                        lane.pending_exit = Some("time_exit".into());
                    } else if lane.bars_held == NO_FOLLOW_BAR && lane.max_mfe_r < NO_FOLLOW_MFE_R {
                        lane.pending_exit = Some("no_follow".into());
                    }
                }
            }
        }
    }

    pub fn mark_quote_gap(&mut self) {
        for signal in self.runs.iter_mut().flat_map(|r| r.signals.values_mut()) {
            if signal
                .variants
                .iter()
                .any(|v| v.status == "open" || v.status == "pending")
            {
                signal.quote_gap_count += 1;
            }
        }
    }

    pub fn contracts(&self) -> BTreeSet<String> {
        self.runs
            .iter()
            .flat_map(|r| r.signals.values())
            .filter(|s| {
                s.variants
                    .iter()
                    .any(|v| v.status == "pending" || v.status == "open")
            })
            .map(|s| s.condition.instrument_id.clone())
            .collect()
    }

    /// Full observations are appended before compacting the hot execution state.
    /// Exports deduplicate archive_id if a crash repeats an already appended line.
    pub fn archive_completed(&mut self, path: &std::path::Path) -> std::io::Result<()> {
        use std::io::{Read, Seek, SeekFrom, Write};
        let completed: Vec<_> = self
            .runs
            .iter()
            .enumerate()
            .flat_map(|(index, run)| {
                run.signals
                    .values()
                    .filter(|s| {
                        s.variants
                            .iter()
                            .all(|v| v.status != "pending" && v.status != "open")
                    })
                    .map(move |s| (index, s.condition.id.clone()))
            })
            .collect();
        if completed.is_empty() {
            return Ok(());
        }
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let mut file = std::fs::OpenOptions::new()
            .create(true)
            .read(true)
            .write(true)
            .truncate(false)
            .open(path)?;
        // Recover a torn final JSONL write before appending the retried record.
        let length = file.metadata()?.len();
        if length > 0 {
            file.seek(SeekFrom::End(-1))?;
            let mut last = [0];
            file.read_exact(&mut last)?;
            if last[0] != b'\n' {
                let mut cursor = length;
                let mut truncate_at = 0;
                while cursor > 0 {
                    let begin = cursor.saturating_sub(8192);
                    let mut bytes = vec![0; (cursor - begin) as usize];
                    file.seek(SeekFrom::Start(begin))?;
                    file.read_exact(&mut bytes)?;
                    if let Some(index) = bytes.iter().rposition(|b| *b == b'\n') {
                        truncate_at = begin + index as u64 + 1;
                        break;
                    }
                    cursor = begin;
                }
                file.set_len(truncate_at)?;
            }
        }
        // The execution engine serializes writers. Explicit write access permits
        // tail repair on Windows, where append-only handles cannot truncate.
        file.seek(SeekFrom::End(0))?;
        for (index, id) in &completed {
            let run = &self.runs[*index];
            serde_json::to_writer(
                &mut file,
                &serde_json::json!({
                    "archive_id":format!("{}:{}",run.definition.id,id),"definition":run.definition,"signal":run.signals[id]
                }),
            )?;
            file.write_all(b"\n")?;
        }
        file.sync_all()?;
        #[cfg(unix)]
        if let Some(parent) = path.parent() {
            std::fs::File::open(parent)?.sync_all()?;
        }
        for (index, id) in completed {
            let run = &mut self.runs[index];
            let record = run.signals.remove(&id).unwrap();
            if run.archive.groups.is_empty() {
                run.archive.groups = (0..4).map(|_| VariantStats::default()).collect();
            }
            for (stats, lane) in run.archive.groups.iter_mut().zip(&record.variants) {
                stats.add(&record, lane);
            }
            if run.archive.pairs.is_empty() {
                run.archive.pairs = pair_definitions();
            }
            for pair in &mut run.archive.pairs {
                pair.add(&record);
            }
            run.archive.signals += 1;
            run.archive.recent.push(signal_brief(&record));
            run.archive
                .recent
                .sort_by_key(|v| std::cmp::Reverse(v["observed_at"].as_i64().unwrap_or(0)));
            run.archive.recent.truncate(12);
        }
        Ok(())
    }

    pub fn summary(&self) -> serde_json::Value {
        let runs: Vec<_> = self.runs.iter().rev().take(20).map(|run| {
            let groups: Vec<_> = ["base_hold", "filter_hold", "base_flat", "filter_flat"].iter().map(|id| {
                let mut stats=run.archive.groups.iter().find(|s|s.id==*id).cloned().unwrap_or_default();
                for record in run.signals.values() {
                    let lane=record.variants.iter().find(|v| v.id==*id).unwrap();
                    stats.add(record,lane);
                }
                stats
            }).collect();
            let mut ordered:Vec<_>=run.signals.values().collect();
            ordered.sort_by_key(|s|std::cmp::Reverse(s.observed_at));
            let mut recent=run.archive.recent.clone();
            recent.extend(ordered.into_iter().take(12).map(signal_brief));
            recent.sort_by_key(|v|std::cmp::Reverse(v["observed_at"].as_i64().unwrap_or(0)));
            recent.truncate(12);
            let mut pairs=if run.archive.pairs.is_empty(){pair_definitions()}else{run.archive.pairs.clone()};
            for record in run.signals.values(){for pair in &mut pairs{pair.add(record);}}
            serde_json::json!({"definition":run.definition,"signals":run.archive.signals+run.signals.len(),"groups":groups,"recent":recent,"pairs":pairs})
        }).collect();
        serde_json::json!({"active_run":self.active_run,"archived_runs":self.runs.len().saturating_sub(20),"runs":runs,"archive_error":self.archive_error,
            "basis":"四组均为同信号盘口影子推演；真实成交单独统计；成本为盘口之外额外2.5 tick假设；未模拟资金容量"})
    }
}

fn signal_brief(s: &PairedSignal) -> serde_json::Value {
    serde_json::json!({"condition_id":s.condition.id,"event_id":s.condition.event_id,"instrument_id":s.condition.instrument_id,
        "observed_at":s.observed_at,"risk_ticks":s.planned_risk_ticks,"quote_gap_count":s.quote_gap_count,
        "variants":s.variants.iter().map(|v|serde_json::json!({"id":v.id,"status":v.status,"reason":v.reason})).collect::<Vec<_>>()})
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
struct PairedEffect {
    base: String,
    candidate: String,
    matched_signals: usize,
    gross_delta: f64,
    estimated_net_delta: f64,
}
fn pair_definitions() -> Vec<PairedEffect> {
    [
        ("base_hold", "filter_hold"),
        ("base_hold", "base_flat"),
        ("filter_hold", "filter_flat"),
    ]
    .into_iter()
    .map(|(a, b)| PairedEffect {
        base: a.into(),
        candidate: b.into(),
        ..Default::default()
    })
    .collect()
}
impl PairedEffect {
    fn add(&mut self, signal: &PairedSignal) {
        let a = signal.variants.iter().find(|v| v.id == self.base).unwrap();
        let b = signal
            .variants
            .iter()
            .find(|v| v.id == self.candidate)
            .unwrap();
        // A filtered opportunity is zero exposure. Wait for both alternatives to
        // resolve, and do not count two filtered lanes as two successful trades.
        if ![a, b]
            .iter()
            .all(|v| v.status == "closed" || v.status == "filtered")
            || (a.status == "filtered" && b.status == "filtered")
        {
            return;
        }
        if signal.quote_gap_count > 0 {
            return;
        }
        self.matched_signals += 1;
        let gross = |v: &VariantTrade| v.fills.iter().map(|f| f.gross_amount).sum::<f64>();
        let net = |v: &VariantTrade| {
            v.fills
                .iter()
                .map(|f| f.gross_amount - f.estimated_extra_cost)
                .sum::<f64>()
        };
        self.gross_delta += gross(b) - gross(a);
        self.estimated_net_delta += net(b) - net(a);
    }
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
struct VariantStats {
    id: String,
    label: String,
    signals: usize,
    filtered: usize,
    pending: usize,
    opened: usize,
    closed: usize,
    invalid: usize,
    wins: usize,
    losses: usize,
    flat: usize,
    gross_amount: f64,
    estimated_net_amount: f64,
    sum_r: f64,
    net_wins: usize,
    win_rate_pct: Option<f64>,
    avg_r: Option<f64>,
    quote_gap_signals: usize,
}
impl VariantStats {
    fn add(&mut self, signal: &PairedSignal, lane: &VariantTrade) {
        self.id = lane.id.clone();
        self.label = lane.label.clone();
        self.signals += 1;
        if signal.quote_gap_count > 0 && (lane.status == "open" || lane.status == "closed") {
            self.invalid += 1;
            self.quote_gap_signals += 1;
            return;
        }
        match lane.status.as_str() {
            "pending" => self.pending += 1,
            "filtered" => self.filtered += 1,
            "open" => self.opened += 1,
            "closed" => {
                self.closed += 1;
                let gross: f64 = lane.fills.iter().map(|f| f.gross_amount).sum();
                let costs: f64 = lane.fills.iter().map(|f| f.estimated_extra_cost).sum();
                self.gross_amount += gross;
                self.estimated_net_amount += gross - costs;
                let risk = lane.initial_risk.unwrap()
                    * crate::precision::effective_multiplier(&signal.condition.instrument_id, "")
                    * signal.condition.volume as f64;
                self.sum_r += gross / risk;
                if gross > 1e-6 {
                    self.wins += 1;
                } else if gross < -1e-6 {
                    self.losses += 1;
                } else {
                    self.flat += 1;
                }
                if gross - costs > 0.0 {
                    self.net_wins += 1;
                }
            }
            _ => self.invalid += 1,
        }
        if self.closed > 0 {
            self.win_rate_pct = Some(self.wins as f64 / self.closed as f64 * 100.0);
            self.avg_r = Some(self.sum_r / self.closed as f64);
        }
    }
}

fn in_flat_window(symbol: &str, now: i64) -> bool {
    [0, 60].iter().any(|back| {
        Local
            .timestamp_opt(now - back, 0)
            .single()
            .is_some_and(|time| check_pre_close_checkpoint(symbol, &time, 2).is_some())
    })
}
fn next_flat_checkpoint(symbol: &str, now: i64) -> Option<i64> {
    let date = Local.timestamp_opt(now, 0).single()?.date_naive();
    for day in 0..=7 {
        let day = date.checked_add_days(Days::new(day))?;
        for (h, m) in [(0, 58), (2, 28), (14, 58), (22, 58), (23, 28)] {
            let at = Local
                .from_local_datetime(&day.and_hms_opt(h, m, 0)?)
                .single()?;
            if at.timestamp() > now && check_pre_close_checkpoint(symbol, &at, 2).is_some() {
                return Some(at.timestamp());
            }
        }
    }
    None
}

#[cfg(test)]
#[path = "comparison_tests.rs"]
mod tests;
