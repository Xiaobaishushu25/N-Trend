//! Shared entry lifetime, sizing and partial-exit lifecycle. No strategy signals.
use super::*;

impl ExecutionOrder {
    pub(super) fn settled(&self) -> bool {
        matches!(self.phase, OrderPhase::Terminal | OrderPhase::Rejected)
            && self.filled >= self.reported_filled
    }

    pub(super) fn reserved_quantity(&self) -> i32 {
        if self.settled() {
            0
        } else if matches!(self.phase, OrderPhase::Terminal | OrderPhase::Rejected) {
            (self.reported_filled - self.filled).max(0)
        } else {
            (self.request.volume - self.filled).max(0)
        }
    }
}

impl ExecutionState {
    pub(super) fn close_reservation(&self, id: &str) -> i32 {
        self.orders
            .values()
            .filter(|o| o.condition_id == id && o.reason.is_some())
            .fold(0i32, |qty, o| qty.saturating_add(o.reserved_quantity()))
    }
}

fn priority(reason: &str) -> u8 {
    match reason {
        "stop" | "trailing" | "breakeven" | "market_gap" | "risk_budget" => 3,
        "manual" | "pre_weekend" | "pre_holiday" | "late_entry_fill" | "contract_expiry"
        | "pre_calendar_end" => 2,
        _ => 1,
    }
}

impl ExecutionEngine {
    pub(super) fn latch_exit_quantity(
        s: &mut ExecutionState,
        id: &str,
        reason: &str,
        reference: f64,
        volume: Option<i32>,
    ) {
        s.aborted_entries.insert(id.into());
        let qty = s.positions.get(id).map(|p| p.volume).unwrap_or(0);
        let all = volume.is_none();
        let task = s.closes.entry(id.into()).or_insert_with(|| CloseTask {
            reason: reason.into(),
            target: volume.unwrap_or(qty).min(qty),
            filled: 0,
            reference,
            attempts: 0,
            all,
        });
        // A repeated partial-close request is the same task, not another lot.
        // A full exit may enlarge the task, but a profit request cannot demote it.
        if all {
            if !task.all || priority(reason) > priority(&task.reason) {
                task.reason = reason.into();
                task.reference = reference;
            }
            task.all = true;
            task.target = task.target.max(task.filled.saturating_add(qty));
        }
        if let Some(c) = s.conditions.get_mut(id) {
            c.state = ConditionState::Closing;
            c.note = format!("退出任务已锁定: {}", task.reason);
        }
    }

    pub(super) fn sizing_limit(
        &self,
        s: &ExecutionState,
        c: &ConditionOrder,
        price: f64,
        quote: Option<&ExecutionQuote>,
        phase: SizingPhase,
    ) -> Result<i32> {
        let Some(frozen) = &c.frozen else {
            return Ok(c.volume);
        };
        let (filled_volume, filled_notional) = s
            .orders
            .values()
            .filter(|o| o.condition_id == c.id && o.reason.is_none())
            .fold((0, 0.), |(n, cash), o| {
                // Confirmed entry cash is stored independently of remaining position quantity.
                (n + o.filled, cash + o.entry_fill_notional)
            });
        let context = SizingContext {
            phase,
            quote,
            contract_multiplier: self.client.multiplier(&c.instrument_id),
            proposed_price: price,
            approved_volume: c.volume,
            filled_volume,
            filled_notional,
        };
        let policy = self.registry.sizing(&frozen.definition.sizing_policy)?;
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            policy.volume_at_price(
                &frozen.plan,
                &frozen.execution,
                &frozen.definition.parameters,
                &context,
            )
        }))
        .map_err(|_| anyhow::anyhow!("报单风险复核模块异常"))??;
        anyhow::ensure!(result >= 0, "报单风险复核不能返回负手数");
        Ok(result.min(c.volume))
    }

    pub(super) fn recheck_filled_sizing(&self, s: &mut ExecutionState) {
        for id in s
            .positions
            .values()
            .filter(|p| !p.is_closed && p.volume > 0)
            .map(|p| p.id.clone())
            .collect::<Vec<_>>()
        {
            let c = &s.conditions[&id];
            let filled = s
                .orders
                .values()
                .filter(|o| o.condition_id == id && o.reason.is_none())
                .map(|o| o.filled)
                .sum::<i32>();
            let price = s.positions[&id].fill_price;
            let limit = self.sizing_limit(
                s,
                c,
                price,
                s.quotes.get(&c.instrument_id.to_uppercase()),
                SizingPhase::ConfirmedFill,
            );
            match limit {
                Ok(limit) if limit < filled => Self::latch_exit(s, &id, "risk_budget", price, true),
                Err(error) => {
                    // A policy failure must never roll back an observed real fill.
                    s.paused = true;
                    s.strategy_error = Some(error.to_string());
                    Self::latch_exit(s, &id, "risk_budget", price, true);
                }
                _ => {}
            }
        }
    }

    pub(super) fn complete_partial_exits(&self, s: &mut ExecutionState) {
        let complete = s
            .closes
            .iter()
            .filter(|(id, task)| {
                !task.all
                    && task.target > 0
                    && task.filled >= task.target
                    && s.orders
                        .values()
                        .filter(|o| &o.condition_id == *id && o.reason.is_some())
                        .all(ExecutionOrder::settled)
            })
            .map(|(id, _)| id.clone())
            .collect::<Vec<_>>();
        for id in complete {
            let task = s.closes.remove(&id).unwrap();
            let Some(p) = s
                .positions
                .get_mut(&id)
                .filter(|p| !p.is_closed && p.volume > 0)
            else {
                continue;
            };
            let frozen = s.conditions[&id].frozen.as_ref();
            let policy_id = frozen
                .map(|f| f.definition.exit_policy.as_str())
                .unwrap_or("n_exit_v1");
            let parameters = frozen
                .map(|f| &f.definition.parameters)
                .unwrap_or(&serde_json::Value::Null);
            let update = self.registry.exit(policy_id).ok().and_then(|policy| {
                std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    policy.on_partial_close(
                        p,
                        &task.reason,
                        task.filled,
                        parameters,
                        s.quotes.get(&p.instrument_id.to_uppercase()),
                    )
                }))
                .ok()
            });
            if let Some(update) = update.filter(|u| u.stop.is_finite() && u.stop >= 0.) {
                p.current_stop_price = if p.direction == "up" {
                    p.current_stop_price.max(update.stop)
                } else if update.stop > 0. && p.current_stop_price > 0. {
                    p.current_stop_price.min(update.stop)
                } else {
                    p.current_stop_price.max(update.stop)
                };
                p.lot1_closed |= update.first_target_completed;
                if p.lot1_closed {
                    p.trailing_grade = p.trailing_grade.max(1);
                }
            } else {
                s.paused = true;
                s.strategy_error = Some("部分平仓完成处理异常，保留已有保护价".into());
            }
        }
    }

    pub(super) async fn request_cancel(&self, reference: String, now: i64) -> Result<()> {
        let mut s = self.state.lock().await;
        let Some(o) = s.orders.get_mut(&reference).filter(|o| !o.settled()) else {
            return Ok(());
        };
        o.phase = OrderPhase::CancelPending;
        o.cancel_attempts += 1;
        o.action_at = now;
        let request = o.request.clone();
        s.last_action_at = now;
        self.save(&mut s)?;
        drop(s);
        if let Err(error) = self
            .client
            .cancel_order(
                &reference,
                &request.instrument_id,
                request.exchange_id.as_deref(),
            )
            .await
        {
            tracing::warn!("撤单结果待柜台确认: {error}");
        }
        Ok(())
    }
}
