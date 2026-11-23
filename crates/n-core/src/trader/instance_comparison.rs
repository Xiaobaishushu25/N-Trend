use super::*;
use futures_util::TryStreamExt;
use serde::Serialize;
use tokio::sync::{watch, Semaphore};

const CURVE_BUCKETS: usize = 119;
const CURVE_LIMIT: usize = CURVE_BUCKETS * 2 + 2;
type ComparisonResult = std::result::Result<Arc<Value>, Arc<anyhow::Error>>;
type Interval = (i64, i64);

pub(super) struct Comparisons {
    pending: Mutex<HashMap<Interval, watch::Receiver<Option<ComparisonResult>>>>,
    reads: Semaphore,
    #[cfg(test)]
    builds: std::sync::atomic::AtomicUsize,
}

impl Default for Comparisons {
    fn default() -> Self {
        Self {
            pending: Mutex::default(),
            reads: Semaphore::new(2),
            #[cfg(test)]
            builds: std::sync::atomic::AtomicUsize::new(0),
        }
    }
}

impl InstanceManager {
    pub async fn comparison(&self, from: i64, to: i64) -> Result<Value> {
        ensure!(from <= to, "对比区间无效");
        let interval = (from, to);
        let mut receiver = {
            let mut pending = self.comparisons.pending.lock().await;
            if let Some(receiver) = pending.get(&interval) {
                receiver.clone()
            } else {
                let (sender, receiver) = watch::channel(None);
                // Only the producer owns the computation. Request cancellation
                // cannot invalidate a result still needed by another caller.
                let instances = self.catalog.lock().await.instances.clone();
                let runtimes = self.all().await;
                let comparisons = self.comparisons.clone();
                let db = self.db.clone();
                pending.insert(interval, receiver.clone());
                tokio::spawn(async move {
                    let result = async {
                        let _permit = comparisons.reads.acquire().await?;
                        #[cfg(test)]
                        comparisons
                            .builds
                            .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                        build_comparison(&db, runtimes, instances, from, to).await
                    }
                    .await
                    .map(Arc::new)
                    .map_err(Arc::new);
                    sender.send_replace(Some(result));
                    comparisons.pending.lock().await.remove(&interval);
                });
                receiver
            }
        };
        let result = receiver.wait_for(Option::is_some).await?;
        match result.as_ref().unwrap() {
            Ok(value) => Ok(value.as_ref().clone()),
            Err(error) => Err(anyhow::anyhow!("{error:#}")),
        }
    }

    #[cfg(test)]
    pub(crate) fn comparison_builds(&self) -> usize {
        self.comparisons
            .builds
            .load(std::sync::atomic::Ordering::Relaxed)
    }
}

#[derive(Clone, Copy, Serialize)]
struct EquityPoint {
    timestamp: i64,
    equity: f64,
}

struct EquityCurve {
    from: i64,
    to: i64,
    original: Vec<EquityPoint>,
    buckets: [Option<(EquityPoint, EquityPoint)>; CURVE_BUCKETS],
}

impl EquityCurve {
    fn new(from: i64, to: i64) -> Self {
        Self {
            from,
            to,
            original: Vec::new(),
            buckets: [None; CURVE_BUCKETS],
        }
    }

    fn push(&mut self, point: EquityPoint) {
        if self.original.len() <= CURVE_LIMIT {
            self.original.push(point);
        }
        let span = (i128::from(self.to) - i128::from(self.from) + 1).max(1);
        let elapsed = (i128::from(point.timestamp) - i128::from(self.from)).max(0);
        let index = ((elapsed * CURVE_BUCKETS as i128) / span) as usize;
        let (low, high) = self.buckets[index.min(CURVE_BUCKETS - 1)].get_or_insert((point, point));
        if point.equity < low.equity {
            *low = point;
        }
        if point.equity > high.equity {
            *high = point;
        }
    }

    fn finish(self, last: Option<EquityPoint>) -> Vec<EquityPoint> {
        if self.original.len() <= CURVE_LIMIT {
            return self.original;
        }
        let mut points = vec![self.original[0]];
        for (low, high) in self.buckets.into_iter().flatten() {
            points.extend([low, high]);
        }
        points.extend(last);
        points.sort_unstable_by_key(|point| point.timestamp);
        points.dedup_by_key(|point| point.timestamp);
        points
    }
}

async fn build_comparison(
    db: &DatabaseConnection,
    runtimes: Vec<Arc<InstanceRuntime>>,
    instances: BTreeMap<String, InstanceDefinition>,
    from: i64,
    to: i64,
) -> Result<Value> {
    let mut rows = Vec::new();
    let mut paired = BTreeMap::<String, BTreeMap<(String, String), f64>>::new();
    for runtime in runtimes {
        let id = &runtime.definition.id;
        let definition = instances.get(id).unwrap_or(&runtime.definition);
        let mut samples = sea_orm::sqlx::query_as::<_, (i64, f64, f64, Option<f64>, Option<String>)>(
            "SELECT timestamp,equity,margin,fees,trading_day FROM trader_equity WHERE instance_id=? AND timestamp>=? AND timestamp<=? ORDER BY timestamp",
        ).bind(id).bind(from).bind(to).fetch(db.get_sqlite_connection_pool());
        let mut curve = EquityCurve::new(from, to);
        let mut count = 0;
        let mut peak: f64 = 0.0;
        let mut drawdown: f64 = 0.0;
        let mut max_margin: f64 = 0.0;
        let mut first = None;
        let mut last: Option<EquityPoint> = None;
        let mut sample_gap = false;
        let mut fees_missing = false;
        let mut fees_by_day = BTreeMap::new();
        while let Some((timestamp, equity, margin, fees, day)) = samples.try_next().await? {
            let point = EquityPoint { timestamp, equity };
            count += 1;
            first.get_or_insert(point);
            sample_gap |= last.is_some_and(|p| timestamp.saturating_sub(p.timestamp) > 120);
            last = Some(point);
            peak = peak.max(equity);
            if peak > 0.0 {
                drawdown = drawdown.max((peak - equity) / peak);
            }
            max_margin = max_margin.max(margin);
            if let Some(fees) = fees {
                fees_by_day
                    .entry(day.unwrap_or_default())
                    .or_insert((fees, fees))
                    .1 = fees;
            } else {
                fees_missing = true;
            }
            curve.push(point);
        }
        drop(samples);

        let (trades, quote_gaps) = runtime.engine.comparison_trades(from, to).await;
        let wins = trades.values().filter(|trade| trade.0 > 0.0).count();
        let profits: f64 = trades
            .values()
            .map(|trade| trade.0)
            .filter(|pnl| *pnl > 0.0)
            .sum();
        let losses: f64 = trades
            .values()
            .map(|trade| trade.0)
            .filter(|pnl| *pnl < 0.0)
            .sum();
        let mut quality = Vec::new();
        if sample_gap {
            quality.push("equity_sample_gap");
        }
        if fees_missing {
            quality.push("fees_unconfirmed");
        }
        if count < 2 {
            quality.push("insufficient_samples");
        }
        if quote_gaps {
            quality.push("quote_gap");
        }
        paired.insert(
            id.clone(),
            trades
                .values()
                .filter(|trade| !quote_gaps && !trade.3.is_empty())
                .map(|trade| ((trade.2.clone(), trade.3.clone()), trade.0))
                .collect(),
        );
        let fees_confirmed = count > 0 && !fees_missing;
        rows.push(json!({
            "instance": definition, "account": runtime.account, "from": from, "to": to,
            "effective_from": first.map(|p| p.timestamp), "effective_to": last.map(|p| p.timestamp),
            "sample_count": count, "equity_curve": curve.finish(last),
            "return_pct": first.zip(last).filter(|(a, _)| a.equity > 0.0).map(|(a, b)| (b.equity / a.equity - 1.0) * 100.0),
            "net_profit": first.zip(last).map(|(a, b)| b.equity - a.equity),
            "max_drawdown_pct": (count > 0).then_some(drawdown * 100.0),
            "trades": trades.len(),
            "win_rate_pct": (!trades.is_empty()).then(|| wins as f64 / trades.len() as f64 * 100.0),
            "profit_factor": (losses < 0.0).then(|| profits / -losses),
            "max_margin": max_margin, "fees_confirmed": fees_confirmed,
            "observed_fees": fees_confirmed.then(|| fees_by_day.values().map(|(a, b)| (b - a).max(0.0)).sum::<f64>()),
            "quality_flags": quality, "cash_flow_adjusted": false,
        }));
    }
    let mut pairs = Vec::new();
    for (index, (baseline, left)) in paired.iter().enumerate() {
        for (candidate, right) in paired.iter().skip(index + 1) {
            let (count, difference) = left
                .iter()
                .filter_map(|(key, pnl)| right.get(key).map(|p| p - pnl))
                .fold((0, 0.0), |(n, sum), value| (n + 1, sum + value));
            pairs.push(json!({
                "baseline_instance_id": baseline, "candidate_instance_id": candidate,
                "paired_signals": count, "gross_profit_difference": difference,
                "basis": "completed_same_source_signals_gross",
            }));
        }
    }
    Ok(json!({
        "from": from, "to": to, "instances": rows, "signal_pairs": pairs,
        "note": "按共同区间展示可用样本；净值变化未调整外部出入金。模拟与实盘分别标识，影子实验不计入账户净值。费用为区间内可观察累计费用的差额，不补估缺失时段。",
    }))
}
