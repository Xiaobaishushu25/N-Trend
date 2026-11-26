//! Shared entry lifetime, sizing and partial-exit lifecycle. No strategy signals.
use super::*;

impl ExecutionOrder {
    fn clean_rejection(&self) -> bool {
        self.phase == OrderPhase::Rejected && self.filled == 0 && self.reported_filled == 0
    }

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

pub(super) enum RejectionWait {
    Seat {
        submitted_at: i64,
    },
    Price {
        submitted_at: i64,
        rejected_price: f64,
        rejected_trading_day: String,
        is_exit: bool,
    },
}

impl RejectionWait {
    pub(super) fn waiting(
        &self,
        q: &ExecutionQuote,
        price: f64,
        now: i64,
        orders_synced_at: f64,
        trading_day: &str,
    ) -> bool {
        let can_check_again = |submitted_at: i64| {
            now >= submitted_at.saturating_add(15)
                && orders_synced_at > submitted_at as f64
                && q.timestamp > submitted_at
        };
        match self {
            Self::Seat { submitted_at } => {
                // A healthy local bridge does not prove its trading seat is
                // connected. Recheck at most once per 15s after a fresh query.
                !can_check_again(*submitted_at)
            }
            Self::Price {
                submitted_at,
                rejected_price,
                rejected_trading_day,
                is_exit,
            } => {
                let new_band = !trading_day.is_empty()
                    && !rejected_trading_day.is_empty()
                    && trading_day != rejected_trading_day;
                let repriced = (price - rejected_price).abs() >= q.tick * 0.5;
                // A protective exit must still be able to probe a pinned
                // price after reconciliation, even if the book cannot reprice.
                q.timestamp <= *submitted_at
                    || !(repriced || new_band || *is_exit && can_check_again(*submitted_at))
            }
        }
    }
}

#[derive(Default)]
pub(super) struct RejectionWaits {
    pub(super) entry: Option<RejectionWait>,
    pub(super) exit: Option<RejectionWait>,
}

pub(super) struct Rejections {
    pub(super) entries: HashSet<String>,
    pub(super) waits: HashMap<String, RejectionWaits>,
    pub(super) latest_exit_rejections: HashMap<String, String>,
}

impl ExecutionState {
    pub(super) fn rejected_submission_state(&self) -> Arc<Rejections> {
        self.orders
            .rejections
            .get_or_init(|| Arc::new(self.inspect_order_rejections()))
            .clone()
    }

    fn inspect_order_rejections(&self) -> Rejections {
        let mut last = HashMap::new();
        let mut rejected_entries = HashSet::new();
        for (reference, order) in self.orders.iter() {
            let is_exit = order.reason.is_some();
            if !is_exit && order.clean_rejection() {
                rejected_entries.insert(order.condition_id.as_str());
            }
            let submitted_at = if order.submitted_at > 0 {
                order.submitted_at
            } else {
                order.action_at
            };
            let sequence = (
                submitted_at,
                reference.parse::<u64>().unwrap_or_default(),
                reference.as_str(),
            );
            let latest = last
                .entry((order.condition_id.as_str(), is_exit))
                .or_insert((sequence, order));
            if sequence > latest.0 {
                *latest = (sequence, order);
            }
        }
        let mut waits = HashMap::<_, RejectionWaits>::new();
        let mut latest_exit_rejections = HashMap::new();
        for ((id, is_exit), ((submitted_at, _, reference), order)) in last {
            if !order.clean_rejection() {
                continue;
            }
            if is_exit {
                latest_exit_rejections.insert(id.to_owned(), reference.to_owned());
            }
            let wait = if order.note.contains("该交易席位未连接到交易所") {
                RejectionWait::Seat { submitted_at }
            } else if order.note.contains("报单价格不在涨跌停板价范围内") {
                RejectionWait::Price {
                    submitted_at,
                    rejected_price: order.request.price,
                    rejected_trading_day: order.trading_day.clone(),
                    is_exit,
                }
            } else {
                continue;
            };
            let sides = waits.entry(id.to_owned()).or_default();
            if is_exit {
                sides.exit = Some(wait);
            } else {
                sides.entry = Some(wait);
            }
        }
        Rejections {
            entries: rejected_entries.into_iter().map(str::to_owned).collect(),
            waits,
            latest_exit_rejections,
        }
    }

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
    ) -> bool {
        let mut changed = s.abort_entry(id);
        let qty = s.positions.get(id).map(|p| p.volume).unwrap_or(0);
        let all = volume.is_none();
        let task = s.closes.entry(id.into()).or_insert_with(|| {
            changed = true;
            CloseTask {
                reason: reason.into(),
                target: volume.unwrap_or(qty).min(qty),
                filled: 0,
                reference,
                attempts: 0,
                all,
                note: None,
            }
        });
        // A repeated partial-close request is the same task, not another lot.
        // A full exit may enlarge the task, but a profit request cannot demote it.
        if all {
            if !task.all || priority(reason) > priority(&task.reason) {
                task.reason = reason.into();
                task.reference = reference;
                changed = true;
            }
            let target = task.target.max(task.filled.saturating_add(qty));
            changed |= !task.all || task.target != target;
            task.all = true;
            task.target = target;
        }
        if let Some(c) = s.conditions.get_mut(id) {
            if c.state != ConditionState::Closing
                || c.note.strip_prefix("退出任务已锁定: ") != Some(task.reason.as_str())
            {
                c.state = ConditionState::Closing;
                c.note = format!("退出任务已锁定: {}", task.reason);
                changed = true;
            }
        }
        changed
    }

    pub(super) fn latch_exit_with_note(
        s: &mut ExecutionState,
        id: &str,
        reason: &str,
        reference: f64,
        volume: Option<i32>,
        note: Option<String>,
    ) {
        Self::latch_exit_quantity(s, id, reason, reference, volume);
        if let Some(task) = s.closes.get_mut(id) {
            if note.is_some() {
                task.note = note;
            }
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

    pub(super) fn recheck_filled_sizing(&self, s: &mut ExecutionState) -> bool {
        let mut changed = false;
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
                Ok(limit) if limit < filled => {
                    s.p3_runner.invalidate_entry(&id, "risk_budget");
                    Self::latch_exit(s, &id, "risk_budget", price, true);
                    changed = true;
                }
                Err(error) => {
                    // A policy failure must never roll back an observed real fill.
                    s.p3_runner.invalidate_entry(&id, "risk_budget");
                    s.paused = true;
                    s.strategy_error = Some(error.to_string());
                    Self::latch_exit(s, &id, "risk_budget", price, true);
                    changed = true;
                }
                _ => {}
            }
        }
        changed
    }

    pub(super) fn complete_partial_exits(&self, s: &mut ExecutionState) -> bool {
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
        let changed = !complete.is_empty();
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
        changed
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
        let broker_started = Instant::now();
        let result = self
            .client
            .cancel_order(
                &reference,
                &request.instrument_id,
                request.exchange_id.as_deref(),
            )
            .await;
        execution_timing::record(|timings| timings.broker_requests += broker_started.elapsed());
        if let Err(error) = result {
            tracing::warn!("撤单结果待柜台确认: {error}");
        }
        Ok(())
    }
}
