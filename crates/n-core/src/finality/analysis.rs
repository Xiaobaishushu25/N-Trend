//! Multi-strategy simulation and statistical analysis for Finality observations.

use std::collections::BTreeMap;
use serde::{Deserialize, Serialize};

use super::model::{FinalityTrial, ObservationRecord};

/// 多策略离线仿真配置。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StrategyDef {
    pub name: String,
    pub min_settle_secs: u32,
    pub consecutive_required: u32,
}

impl StrategyDef {
    pub fn new(name: impl Into<String>, min_settle_secs: u32, consecutive_required: u32) -> Self {
        Self {
            name: name.into(),
            min_settle_secs,
            consecutive_required,
        }
    }

    /// 评估预置策略集合。
    pub fn default_strategies() -> Vec<Self> {
        vec![
            Self::new("2次一致 (min 0s)", 0, 2),
            Self::new("3次一致 (min 0s [当前影子试验])", 0, 3),
            Self::new("4次一致 (min 0s)", 0, 4),
            Self::new("min 5s + 2次一致", 5, 2),
            Self::new("min 5s + 3次一致", 5, 3),
            Self::new("min 10s + 2次一致", 10, 2),
            Self::new("min 10s + 3次一致", 10, 3),
            Self::new("min 15s + 3次一致", 15, 3),
            Self::new("min 30s + 1次探测 (当前生产基准)", 30, 1),
            Self::new("min 30s + 3次一致", 30, 3),
        ]
    }
}

/// 单个策略在存量观测数据集上的仿真结果。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StrategySimulationResult {
    pub strategy_name: String,
    pub min_settle_secs: u32,
    pub consecutive_required: u32,
    pub total_bars: usize,
    pub confirmed_bars: usize,
    pub unconfirmed_bars: usize,
    pub false_final_count: usize,
    pub false_final_rate: f64,
    pub avg_delay_secs: f64,
    pub p50_delay_secs: f64,
    pub p90_delay_secs: f64,
    pub p95_delay_secs: f64,
    pub p99_delay_secs: f64,
    pub max_delay_secs: f64,
}

/// 某一组 Bar 的延迟分布统计。
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct DelayStats {
    pub count: usize,
    pub avg: f64,
    pub p50: f64,
    pub p90: f64,
    pub p95: f64,
    pub p99: f64,
    pub max: f64,
}

impl DelayStats {
    pub fn calculate(mut values_s: Vec<f64>) -> Self {
        if values_s.is_empty() {
            return Self::default();
        }
        values_s.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
        let count = values_s.len();
        let sum: f64 = values_s.iter().sum();
        let avg = sum / count as f64;
        let percentile = |p: f64| -> f64 {
            let idx = ((count as f64 * p).ceil() as usize).saturating_sub(1);
            values_s[idx.min(count - 1)]
        };
        Self {
            count,
            avg,
            p50: percentile(0.50),
            p90: percentile(0.90),
            p95: percentile(0.95),
            p99: percentile(0.99),
            max: *values_s.last().unwrap_or(&0.0),
        }
    }
}

/// 按时段类型或品种分组的 Finality 试验统计摘要。
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct GroupFinalitySummary {
    pub group_key: String,
    pub total_bars: usize,
    pub bars_with_revision: usize,
    pub revision_bar_rate: f64,
    pub candidate_final_count: usize,
    pub false_final_count: usize,
    pub false_final_rate: f64,
    pub candidate_delay: DelayStats,
    pub last_revision_delay: DelayStats,
}

/// 全量 Finality 观测报告。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FinalityReport {
    pub total_bars: usize,
    pub overall_summary: GroupFinalitySummary,
    pub by_session: Vec<GroupFinalitySummary>,
    pub by_symbol: Vec<GroupFinalitySummary>,
}

/// 哨兵批次定盘安全性分析结果。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SentinelEvaluationResult {
    pub sentinel_symbols: Vec<String>,
    pub total_batches: usize,
    pub valid_batches: usize,
    pub batches_with_non_sentinel_late_revision: usize,
    pub batch_false_final_rate: f64,
    pub avg_sentinel_batch_delay_secs: f64,
    pub p95_sentinel_batch_delay_secs: f64,
}

/// 运行多策略仿真引擎。
/// 输入为所有探测观测记录（内部按 (symbol, bar_ts) 分组回放）。
pub fn simulate_strategies(
    observations: &[ObservationRecord],
    strategies: &[StrategyDef],
) -> Vec<StrategySimulationResult> {
    // 按 (symbol, bar_ts) 分组，并确保按 probe_index 升序
    let mut bar_groups: BTreeMap<(String, String), Vec<&ObservationRecord>> = BTreeMap::new();
    for obs in observations {
        bar_groups
            .entry((obs.symbol.clone(), obs.bar_ts.clone()))
            .or_default()
            .push(obs);
    }
    for list in bar_groups.values_mut() {
        list.sort_by_key(|o| o.probe_index);
    }

    let mut results = Vec::new();
    for strat in strategies {
        let mut confirmed_bars = 0;
        let mut unconfirmed_bars = 0;
        let mut false_finals = 0;
        let mut confirm_delays_s = Vec::new();

        for (_key, probes) in &bar_groups {
            let mut last_fp: Option<&str> = None;
            let mut same_count = 0;
            let mut candidate_final_idx: Option<usize> = None;

            for (idx, probe) in probes.iter().enumerate() {
                let probe_elapsed_s = (probe.elapsed_ms as f64 / 1000.0).max(0.0);
                if let Some(prev) = last_fp {
                    if prev == probe.fingerprint {
                        same_count += 1;
                    } else {
                        same_count = 1;
                    }
                } else {
                    same_count = 1;
                }
                last_fp = Some(&probe.fingerprint);

                if candidate_final_idx.is_none()
                    && probe_elapsed_s >= strat.min_settle_secs as f64
                    && same_count >= strat.consecutive_required as usize
                {
                    candidate_final_idx = Some(idx);
                }
            }

            if let Some(cf_idx) = candidate_final_idx {
                confirmed_bars += 1;
                let cf_probe = probes[cf_idx];
                let delay_s = (cf_probe.elapsed_ms as f64 / 1000.0).max(0.0);
                confirm_delays_s.push(delay_s);

                // 检查候选确认后是否有后续探针修改了指纹
                let confirmed_fp = &cf_probe.fingerprint;
                let mut late_revision = false;
                for probe in probes.iter().skip(cf_idx + 1) {
                    if &probe.fingerprint != confirmed_fp {
                        late_revision = true;
                        break;
                    }
                }
                if late_revision {
                    false_finals += 1;
                }
            } else {
                unconfirmed_bars += 1;
            }
        }

        let total_bars = bar_groups.len();
        let false_final_rate = if confirmed_bars > 0 {
            false_finals as f64 / confirmed_bars as f64
        } else {
            0.0
        };
        let delay_stats = DelayStats::calculate(confirm_delays_s);

        results.push(StrategySimulationResult {
            strategy_name: strat.name.clone(),
            min_settle_secs: strat.min_settle_secs,
            consecutive_required: strat.consecutive_required,
            total_bars,
            confirmed_bars,
            unconfirmed_bars,
            false_final_count: false_finals,
            false_final_rate,
            avg_delay_secs: delay_stats.avg,
            p50_delay_secs: delay_stats.p50,
            p90_delay_secs: delay_stats.p90,
            p95_delay_secs: delay_stats.p95,
            p99_delay_secs: delay_stats.p99,
            max_delay_secs: delay_stats.max,
        });
    }

    results
}

/// 汇总 Finality 统计报告。
pub fn summarize_trials(trials: &[FinalityTrial]) -> FinalityReport {
    let overall = compute_group_summary("全市场", trials);

    // 按 session_type 分组
    let mut session_map: BTreeMap<String, Vec<&FinalityTrial>> = BTreeMap::new();
    for t in trials {
        session_map.entry(t.session_type.clone()).or_default().push(t);
    }
    let mut by_session = Vec::new();
    for (session, list) in session_map {
        let refs: Vec<FinalityTrial> = list.into_iter().cloned().collect();
        by_session.push(compute_group_summary(&session, &refs));
    }

    // 按 symbol 分组
    let mut symbol_map: BTreeMap<String, Vec<&FinalityTrial>> = BTreeMap::new();
    for t in trials {
        symbol_map.entry(t.symbol.clone()).or_default().push(t);
    }
    let mut by_symbol = Vec::new();
    for (symbol, list) in symbol_map {
        let refs: Vec<FinalityTrial> = list.into_iter().cloned().collect();
        by_symbol.push(compute_group_summary(&symbol, &refs));
    }

    FinalityReport {
        total_bars: trials.len(),
        overall_summary: overall,
        by_session,
        by_symbol,
    }
}

fn compute_group_summary(group_key: &str, trials: &[FinalityTrial]) -> GroupFinalitySummary {
    let total_bars = trials.len();
    if total_bars == 0 {
        return GroupFinalitySummary {
            group_key: group_key.to_string(),
            ..Default::default()
        };
    }

    let mut bars_with_revision = 0;
    let mut candidate_final_count = 0;
    let mut false_final_count = 0;
    let mut cf_delays = Vec::new();
    let mut rev_delays = Vec::new();

    for t in trials {
        if t.revision_count > 0 {
            bars_with_revision += 1;
        }
        if t.candidate_final_at.is_some() {
            candidate_final_count += 1;
            if let Some(ms) = t.candidate_delay_ms {
                cf_delays.push((ms as f64 / 1000.0).max(0.0));
            }
        }
        if t.false_final {
            false_final_count += 1;
        }
        if let Some(ms) = t.last_revision_delay_ms {
            rev_delays.push((ms as f64 / 1000.0).max(0.0));
        }
    }

    let revision_bar_rate = bars_with_revision as f64 / total_bars as f64;
    let false_final_rate = if candidate_final_count > 0 {
        false_final_count as f64 / candidate_final_count as f64
    } else {
        0.0
    };

    GroupFinalitySummary {
        group_key: group_key.to_string(),
        total_bars,
        bars_with_revision,
        revision_bar_rate,
        candidate_final_count,
        false_final_count,
        false_final_rate,
        candidate_delay: DelayStats::calculate(cf_delays),
        last_revision_delay: DelayStats::calculate(rev_delays),
    }
}

/// 评估指定哨兵批次的定盘安全性。
/// 计算逻辑：对每个 bar_ts，取指定哨兵都达到 Candidate Final 的最慢时间（sentinel_batch_final_ms）；
/// 检查其他被观测品种在此时刻后是否发生了任何修订，输出批次误判率。
pub fn evaluate_sentinels(
    observations: &[ObservationRecord],
    trials: &[FinalityTrial],
    sentinel_symbols: &[String],
) -> SentinelEvaluationResult {
    // 建立 trials 索引: (bar_ts, symbol) -> FinalityTrial
    let mut trial_map: BTreeMap<(String, String), &FinalityTrial> = BTreeMap::new();
    for t in trials {
        trial_map.insert((t.bar_ts.clone(), t.symbol.clone()), t);
    }

    // 建立 observations 索引: (bar_ts, symbol) -> Vec<ObservationRecord>
    let mut obs_map: BTreeMap<(String, String), Vec<&ObservationRecord>> = BTreeMap::new();
    for obs in observations {
        obs_map
            .entry((obs.bar_ts.clone(), obs.symbol.clone()))
            .or_default()
            .push(obs);
    }

    // 获取所有唯一的 bar_ts
    let all_bar_ts: std::collections::BTreeSet<String> =
        trials.iter().map(|t| t.bar_ts.clone()).collect();
    let total_batches = all_bar_ts.len();

    let mut valid_batches = 0;
    let mut batches_with_late_rev = 0;
    let mut batch_delays_s = Vec::new();

    for bar_ts in &all_bar_ts {
        // 检查所有指定的哨兵是否均在当次 bar_ts 达到 Candidate Final
        let mut max_sentinel_delay_ms: Option<i64> = Some(0);
        for s in sentinel_symbols {
            if let Some(t) = trial_map.get(&(bar_ts.clone(), s.clone())) {
                if let Some(delay) = t.candidate_delay_ms {
                    if let Some(cur_max) = max_sentinel_delay_ms {
                        max_sentinel_delay_ms = Some(cur_max.max(delay));
                    }
                } else {
                    max_sentinel_delay_ms = None;
                    break;
                }
            } else {
                max_sentinel_delay_ms = None;
                break;
            }
        }

        let sentinel_final_ms = match max_sentinel_delay_ms {
            Some(d) => d,
            None => continue, // 该批次某些哨兵未达成确认，跳过
        };

        valid_batches += 1;
        batch_delays_s.push(sentinel_final_ms as f64 / 1000.0);

        // 检查其他非哨兵品种在此批次定盘时间后是否发生修改
        let mut has_non_sentinel_revision = false;
        for t in trials.iter().filter(|t| &t.bar_ts == bar_ts) {
            if sentinel_symbols.contains(&t.symbol) {
                continue;
            }
            // 如果该品种最后修改时间发生在哨兵批次确认时间之后 -> 晚修订！
            if let Some(last_rev_ms) = t.last_revision_delay_ms {
                if last_rev_ms > sentinel_final_ms {
                    has_non_sentinel_revision = true;
                    break;
                }
            }
        }

        if has_non_sentinel_revision {
            batches_with_late_rev += 1;
        }
    }

    let batch_false_final_rate = if valid_batches > 0 {
        batches_with_late_rev as f64 / valid_batches as f64
    } else {
        0.0
    };
    let delay_stats = DelayStats::calculate(batch_delays_s);

    SentinelEvaluationResult {
        sentinel_symbols: sentinel_symbols.to_vec(),
        total_batches,
        valid_batches,
        batches_with_non_sentinel_late_revision: batches_with_late_rev,
        batch_false_final_rate,
        avg_sentinel_batch_delay_secs: delay_stats.avg,
        p95_sentinel_batch_delay_secs: delay_stats.p95,
    }
}

/// 格式化多策略仿真对比表格（Markdown / ASCII 格式）。
pub fn format_simulation_table(results: &[StrategySimulationResult]) -> String {
    let mut out = String::new();
    out.push_str("| 策略方案 | 样本量 | 确认率 | 平均确认 | P50确认 | P95确认 | P99确认 | False Final数 | 误判率 |\n");
    out.push_str("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |\n");
    for r in results {
        let confirm_pct = if r.total_bars > 0 {
            (r.confirmed_bars as f64 / r.total_bars as f64) * 100.0
        } else {
            0.0
        };
        out.push_str(&format!(
            "| {} | {} | {:.1}% | {:.1}s | {:.1}s | {:.1}s | {:.1}s | {} | {:.2}% |\n",
            r.strategy_name,
            r.total_bars,
            confirm_pct,
            r.avg_delay_secs,
            r.p50_delay_secs,
            r.p95_delay_secs,
            r.p99_delay_secs,
            r.false_final_count,
            r.false_final_rate * 100.0
        ));
    }
    out
}

/// 格式化 Finality 观测统计报告。
pub fn format_finality_report(report: &FinalityReport) -> String {
    let mut out = String::new();
    out.push_str("========================================================================================\n");
    out.push_str("                       N-Trend 5m Finality 实测观测报告                                   \n");
    out.push_str("========================================================================================\n\n");

    out.push_str(&format!("总观测 5m Bar 数量: {}\n", report.total_bars));
    out.push_str(&format!(
        "存在修改的 Bar 数量: {} ({:.1}%)\n",
        report.overall_summary.bars_with_revision,
        report.overall_summary.revision_bar_rate * 100.0
    ));
    out.push_str(&format!(
        "达成 Candidate Final 数量: {} | False Final(定盘后再被改)数量: {} (误判率: {:.2}%)\n",
        report.overall_summary.candidate_final_count,
        report.overall_summary.false_final_count,
        report.overall_summary.false_final_rate * 100.0
    ));
    out.push_str(&format!(
        "Candidate Final 确认延迟: 平均 {:.1}s | P50 {:.1}s | P95 {:.1}s | P99 {:.1}s | MAX {:.1}s\n",
        report.overall_summary.candidate_delay.avg,
        report.overall_summary.candidate_delay.p50,
        report.overall_summary.candidate_delay.p95,
        report.overall_summary.candidate_delay.p99,
        report.overall_summary.candidate_delay.max,
    ));
    out.push_str(&format!(
        "最后一次修改发生延迟: 平均 {:.1}s | P50 {:.1}s | P95 {:.1}s | P99 {:.1}s | MAX {:.1}s\n\n",
        report.overall_summary.last_revision_delay.avg,
        report.overall_summary.last_revision_delay.p50,
        report.overall_summary.last_revision_delay.p95,
        report.overall_summary.last_revision_delay.p99,
        report.overall_summary.last_revision_delay.max,
    ));

    out.push_str("--- [按时段类型 (Session Type) 分组统计] ---\n");
    out.push_str("| 时段类型 | 样本数 | 修订Bar占比 | 平均确认 | P95确认 | MAX修改延迟 | False Final数 | 误判率 |\n");
    out.push_str("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |\n");
    for s in &report.by_session {
        out.push_str(&format!(
            "| {:<10} | {:>6} | {:>10.1}% | {:>7.1}s | {:>6.1}s | {:>10.1}s | {:>13} | {:>6.2}% |\n",
            s.group_key,
            s.total_bars,
            s.revision_bar_rate * 100.0,
            s.candidate_delay.avg,
            s.candidate_delay.p95,
            s.last_revision_delay.max,
            s.false_final_count,
            s.false_final_rate * 100.0,
        ));
    }
    out.push('\n');

    out.push_str("--- [按品种 (Symbol) 分组统计] ---\n");
    out.push_str("| 品种代码 | 样本数 | 修订Bar占比 | 平均确认 | P95确认 | MAX修改延迟 | False Final数 | 误判率 |\n");
    out.push_str("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |\n");
    for s in &report.by_symbol {
        out.push_str(&format!(
            "| {:<8} | {:>6} | {:>10.1}% | {:>7.1}s | {:>6.1}s | {:>10.1}s | {:>13} | {:>6.2}% |\n",
            s.group_key,
            s.total_bars,
            s.revision_bar_rate * 100.0,
            s.candidate_delay.avg,
            s.candidate_delay.p95,
            s.last_revision_delay.max,
            s.false_final_count,
            s.false_final_rate * 100.0,
        ));
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn dummy_obs(symbol: &str, bar_ts: &str, elapsed_ms: i64, fp: &str) -> ObservationRecord {
        ObservationRecord {
            id: None,
            symbol: symbol.to_string(),
            bar_ts: bar_ts.to_string(),
            observed_at: "now".to_string(),
            elapsed_ms,
            probe_index: (elapsed_ms / 5000) as i32,
            open: 1.0,
            high: 2.0,
            low: 1.0,
            close: 2.0,
            volume: 100.0,
            hold: 100.0,
            fingerprint: fp.to_string(),
            session_type: "normal".to_string(),
            is_revision: false,
            raw_response: None,
        }
    }

    #[test]
    fn test_simulation_accuracy() {
        let mut obs = Vec::new();
        // Bar 1: T+0 A, T+5 A, T+10 A, T+15 A (完全稳定)
        obs.push(dummy_obs("RB0", "2026-08-28 10:45:00", 0, "A"));
        obs.push(dummy_obs("RB0", "2026-08-28 10:45:00", 5000, "A"));
        obs.push(dummy_obs("RB0", "2026-08-28 10:45:00", 10000, "A"));
        obs.push(dummy_obs("RB0", "2026-08-28 10:45:00", 15000, "A"));

        // Bar 2: T+0 A, T+5 A, T+10 A, T+15 B (15秒突发晚修改)
        obs.push(dummy_obs("CJ0", "2026-08-28 11:30:00", 0, "A"));
        obs.push(dummy_obs("CJ0", "2026-08-28 11:30:00", 5000, "A"));
        obs.push(dummy_obs("CJ0", "2026-08-28 11:30:00", 10000, "A"));
        obs.push(dummy_obs("CJ0", "2026-08-28 11:30:00", 15000, "B"));

        let strats = vec![
            StrategyDef::new("3-consec-0s", 0, 3),
            StrategyDef::new("min-15s-3consec", 15, 3),
        ];

        let results = simulate_strategies(&obs, &strats);
        assert_eq!(results.len(), 2);

        // 3-consec-0s 在 Bar 2 会在 T+10 确认为 A，然后在 T+15 出现 B，所以 false_final_count = 1
        assert_eq!(results[0].confirmed_bars, 2);
        assert_eq!(results[0].false_final_count, 1);
        assert_eq!(results[0].false_final_rate, 0.5);

        // min-15s 策略在 Bar 2 不会在 T+10 确认，到 T+15 看到 B 重置计数，最终未误判
        assert_eq!(results[1].confirmed_bars, 1); // 只有 Bar 1 确认
        assert_eq!(results[1].false_final_count, 0);
    }
}
