use super::*;

pub(super) struct BudgetSettings {
    cents: i64,
    pub max_lots: i32,
}
impl BudgetSettings {
    pub fn new(yuan: f64, max_lots: i32) -> Result<Self> {
        ensure!(
            yuan.is_finite() && yuan > 0. && yuan * 100. < i64::MAX as f64,
            "P3风险预算金额无效"
        );
        ensure!((1..=2).contains(&max_lots), "P3最大手数必须为1或2");
        let cents = (yuan * 100.).floor() as i64;
        ensure!(cents > 0, "P3风险预算至少为0.01元");
        Ok(Self { cents, max_lots })
    }
    fn from_value(p: &Value) -> Result<Self> {
        Self::new(
            p["risk_budget_yuan"].as_f64().context("P3风险预算缺失")?,
            p["max_lots"]
                .as_i64()
                .and_then(|n| i32::try_from(n).ok())
                .context("P3最大手数缺失")?,
        )
    }
    pub fn yuan(&self) -> f64 {
        self.cents as f64 / 100.
    }
}
fn cents_up(yuan: f64) -> Result<i64> {
    ensure!(
        yuan.is_finite() && yuan >= 0. && yuan * 100. < i64::MAX as f64,
        "P3风险金额无法计算"
    );
    Ok((yuan * 100.).ceil() as i64)
}
pub(super) fn unit_loss(s: &Snapshot, fill: f64) -> Result<i64> {
    let stop = s.stop - s.sign() * s.tick;
    let points = (fill - stop) * s.sign();
    ensure!(
        fill.is_finite() && fill > 0. && points > 0.,
        "P3实际止损方向无效"
    );
    let loss = points * s.multiplier + s.fees.bound(fill, stop, s.multiplier);
    let cents = cents_up(loss)?;
    ensure!(cents > 0, "P3单手风险必须为正");
    Ok(cents)
}
pub(super) fn initial_volume(s: &Snapshot, budget: &BudgetSettings) -> Result<i32> {
    Ok(
        (budget.cents / unit_loss(s, s.entry + s.sign() * s.tick)?).min(budget.max_lots as i64)
            as i32,
    )
}

pub(super) struct RiskBudget;
fn snapshot(plan: &EntryPlan) -> Result<Snapshot> {
    let s: Snapshot = serde_json::from_value(plan.evidence["candidate"].clone())?;
    ensure!(
        plan.instrument_id.eq_ignore_ascii_case(&s.instrument)
            && plan.symbol == s.symbol
            && plan.direction == s.direction
            && plan.reference_price == s.entry
            && plan.stop_price == Some(s.stop)
            && plan.target_price == Some(s.target)
            && plan.risk_per_unit == s.risk,
        "P3计划与冻结风险快照不一致"
    );
    ensure!(
        s.fees.valid()
            && s.tick.is_finite()
            && s.tick > 0.
            && s.multiplier.is_finite()
            && s.multiplier > 0.
            && s.risk.is_finite()
            && s.risk > 0.,
        "P3风险快照无效"
    );
    Ok(s)
}
impl SizingPolicy for RiskBudget {
    fn volume(&self, plan: &EntryPlan, _: &ExecutionConfig, parameters: &Value) -> Result<i32> {
        let s = snapshot(plan)?;
        let budget = BudgetSettings::from_value(parameters)?;
        ensure!(
            plan.requested_volume >= 0 && plan.requested_volume <= 2,
            "P3计划手数无效"
        );
        Ok(initial_volume(&s, &budget)?.min(plan.requested_volume))
    }
    fn volume_at_price(
        &self,
        plan: &EntryPlan,
        _: &ExecutionConfig,
        parameters: &Value,
        c: &SizingContext<'_>,
    ) -> Result<i32> {
        let s = snapshot(plan)?;
        let b = BudgetSettings::from_value(parameters)?;
        ensure!(
            c.contract_multiplier.is_finite()
                && c.contract_multiplier > 0.
                && (c.contract_multiplier - s.multiplier).abs() <= 1e-8,
            "P3信号点值与执行账户不一致"
        );
        ensure!(
            c.filled_volume >= 0 && c.filled_notional.is_finite() && c.filled_notional >= 0.,
            "P3累计开仓数据无效"
        );
        if let Some(q) = c.quote {
            ensure!(
                q.instrument_id.eq_ignore_ascii_case(&s.instrument)
                    && (q.tick - s.tick).abs() <= 1e-8,
                "P3实际合约元数据不一致"
            );
        }
        let stop = s.stop - s.sign() * s.tick;
        let n = c.filled_volume;
        ensure!(
            (n == 0 && c.filled_notional == 0.) || (n > 0 && c.filled_notional > 0.),
            "P3累计数量与成交金额不一致"
        );
        let used = if n == 0 {
            0
        } else {
            let average = c.filled_notional / n as f64;
            let points = s.sign() * (c.filled_notional - stop * n as f64);
            ensure!(points > 0., "P3已成交止损方向无效");
            cents_up(points * s.multiplier + s.fees.bound(average, stop, s.multiplier) * n as f64)?
        };
        if used > b.cents || n > b.max_lots {
            return Ok(0);
        }
        let remaining = (b.cents - used) / unit_loss(&s, c.proposed_price)?;
        Ok((n as i64 + remaining)
            .min(b.max_lots as i64)
            .min(c.approved_volume as i64) as i32)
    }
}
