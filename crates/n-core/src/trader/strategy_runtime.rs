use super::*;

impl ExecutionEngine {
    pub async fn needs_market_history(&self) -> bool {
        let s = self.state.lock().await;
        self.registry
            .strategy(&s.definition)
            .is_ok_and(|p| p.uses_history())
    }
    pub async fn on_market_history(
        &self,
        symbol: &str,
        raw: &[crate::fetch::kline::Kline],
        rollovers: Vec<crate::derive::rollover::RolloverRecord>,
    ) -> Result<()> {
        let now = chrono::Utc::now().timestamp();
        let latest = raw
            .iter()
            .rev()
            .find(|b| super::super::candidates::parse_time(&b.datetime).is_ok_and(|t| t <= now));
        let Some(latest) = latest else {
            return Ok(());
        };
        {
            let s = self.state.lock().await;
            if !self
                .registry
                .strategy(&s.definition)
                .is_ok_and(|p| p.uses_history())
            {
                return Ok(());
            }
            if s.strategy_states
                .get(&s.definition.revision)
                .and_then(|v| v["raw_cursor"][symbol].as_str())
                .is_some_and(|t| t >= latest.datetime.as_str())
            {
                return Ok(());
            }
        }
        self.dispatch(StrategyEvent::History5m {
            symbol: symbol.into(),
            bars: raw.to_vec(),
            rollovers,
            timestamp: now,
        })
        .await
    }
    pub(super) fn portfolio_decision(
        &self,
        s: &ExecutionState,
        c: &ConditionOrder,
        now: i64,
    ) -> EntryDecision {
        let Some(f) = &c.frozen else {
            return EntryDecision::Allow;
        };
        let Ok(strategy) = self.registry.strategy(&f.definition) else {
            return EntryDecision::Reject("strategy_unavailable".into());
        };
        strategy.check_portfolio(
            &f.plan,
            &EntryPortfolio {
                conditions: &s.conditions,
                positions: &s.positions,
                orders: &s.orders,
                history: &s.history,
                state: s
                    .strategy_states
                    .get(&f.definition.revision)
                    .unwrap_or(&serde_json::Value::Null),
            },
            now,
        )
    }
    fn completed_bar_condition(&self, s: &ExecutionState, c: &ConditionOrder) -> bool {
        c.frozen.as_ref().is_some_and(|f| {
            self.registry
                .strategy(&f.definition)
                .is_ok_and(|p| p.entry_constraints().complete_15m)
        }) && (matches!(
            c.state,
            ConditionState::Pending
                | ConditionState::Triggered
                | ConditionState::WaitingConfirm
                | ConditionState::Working
                | ConditionState::Unknown
                | ConditionState::PartFilled
                | ConditionState::Closing
        ) || s.positions.get(&c.id).is_some_and(|p| !p.is_closed)
            || s.orders.values().any(|o| {
                o.condition_id == c.id
                    && (!matches!(o.phase, OrderPhase::Terminal | OrderPhase::Rejected)
                        || o.filled < o.reported_filled)
            }))
    }
    pub async fn needs_completed_15m(&self) -> bool {
        let s = self.state.lock().await;
        self.registry
            .strategy(&s.definition)
            .is_ok_and(|p| p.entry_constraints().complete_15m)
            || s.conditions
                .values()
                .any(|c| self.completed_bar_condition(&s, c))
    }
    pub async fn on_completed_15m(
        &self,
        symbol: &str,
        raw: &[crate::fetch::kline::Kline],
    ) -> Result<()> {
        let start = {
            let s = self.state.lock().await;
            let mut revisions = HashMap::<String, String>::new();
            for c in s
                .conditions
                .values()
                .filter(|c| c.symbol == symbol && self.completed_bar_condition(&s, c))
            {
                let start = s
                    .positions
                    .get(&c.id)
                    .filter(|p| !p.is_closed)
                    .and_then(|p| super::super::candidates::parse_time(&p.open_ts).ok())
                    .map(super::super::candidates::label)
                    .unwrap_or_else(|| c.warning_bar_ts.clone());
                revisions
                    .entry(c.origin.strategy_version.clone())
                    .and_modify(|v| *v = v.clone().min(start.clone()))
                    .or_insert(start);
            }
            let starts = revisions
                .into_iter()
                .map(|(rev, start)| {
                    let key = format!("{rev}:{symbol}:candidate15m");
                    s.strategy_bars
                        .get(&key)
                        .map_or(start.clone(), |cursor| start.max(cursor.clone()))
                })
                .collect::<Vec<_>>();
            starts.into_iter().min()
        };
        let bars = super::super::candidates::completed_bars(raw, chrono::Utc::now().timestamp())?;
        let start = start.or_else(|| bars.last().map(|b| b.datetime.clone()));
        for b in bars
            .iter()
            .filter(|b| start.as_ref().is_some_and(|t| b.datetime >= *t))
        {
            self.dispatch(StrategyEvent::BarClosed {
                symbol: symbol.into(),
                timeframe: "candidate15m".into(),
                timestamp: b.datetime.clone(),
                high: b.high,
                low: b.low,
                close: b.close,
            })
            .await?;
        }
        Ok(())
    }
    pub async fn strategy_diagnostics(&self) -> serde_json::Value {
        let s = self.state.lock().await;
        serde_json::json!({"states":s.strategy_states,"entry_decisions":s.entry_decisions,"symbol_slots":s.symbol_slots})
    }
    pub(super) fn reject_strategy_entry(s: &mut ExecutionState, id: &str, reason: &str) {
        s.aborted_entries.insert(id.into());
        if let Some(c) = s.conditions.get_mut(id) {
            c.state = if reason == "invalidated" {
                ConditionState::Invalidated
            } else {
                ConditionState::Rejected
            };
            c.note = reason.into();
        }
        s.entry_decisions.insert(id.into(),serde_json::json!({"stage":"entry","reason":reason,"at":chrono::Utc::now().timestamp()}));
    }
    pub(super) fn symbol_slot_blocked(s: &ExecutionState, c: &ConditionOrder) -> bool {
        s.symbol_slots.get(&c.symbol).is_some_and(|id| id != &c.id)
            || s.symbol_release_after.get(&c.symbol).is_some_and(|seq| {
                s.quote_sequences
                    .get(&c.instrument_id.to_uppercase())
                    .copied()
                    .unwrap_or(0)
                    <= *seq
            })
            || s.positions
                .values()
                .any(|p| p.symbol == c.symbol && p.id != c.id && !p.is_closed && p.volume > 0)
            || s.orders.values().any(|o| {
                o.condition_id != c.id
                    && s.conditions
                        .get(&o.condition_id)
                        .is_some_and(|x| x.symbol == c.symbol)
                    && (!matches!(o.phase, OrderPhase::Terminal | OrderPhase::Rejected)
                        || o.filled < o.reported_filled)
            })
    }
    pub(super) fn release_symbol_slots(s: &mut ExecutionState, observed: &HashSet<String>) {
        let release = s
            .symbol_slots
            .iter()
            .filter_map(|(symbol, id)| {
                let pos_live = s
                    .positions
                    .get(id)
                    .is_some_and(|p| !p.is_closed && p.volume > 0);
                let unresolved = s.orders.values().any(|o| {
                    o.condition_id == *id
                        && (!matches!(o.phase, OrderPhase::Terminal | OrderPhase::Rejected)
                            || o.filled < o.reported_filled)
                });
                let terminal = s.aborted_entries.contains(id)
                    || s.conditions.get(id).is_some_and(|c| {
                        matches!(
                            c.state,
                            ConditionState::Closed
                                | ConditionState::Canceled
                                | ConditionState::Expired
                                | ConditionState::Invalidated
                                | ConditionState::Rejected
                        )
                    });
                if pos_live || unresolved || !terminal {
                    return None;
                }
                let instrument = s.conditions.get(id)?.instrument_id.to_uppercase();
                Some((
                    symbol.clone(),
                    s.quote_sequences.get(&instrument).copied().unwrap_or(0)
                        + u64::from(!observed.contains(&instrument)),
                ))
            })
            .collect::<Vec<_>>();
        for (symbol, seq) in release {
            s.symbol_slots.remove(&symbol);
            s.symbol_release_after.insert(symbol, seq);
        }
    }
    pub fn validate_saved_state(bytes: &[u8]) -> Result<()> {
        let _: ExecutionState = serde_json::from_slice(bytes)?;
        Ok(())
    }
    pub fn disable_loading(&mut self, message: String) {
        self.state.get_mut().persistence_error = Some(message);
    }
    pub fn broker_client(&self) -> BrokerClient {
        self.client.clone()
    }
    pub fn validate_config(c: &ExecutionConfig) -> Result<()> {
        anyhow::ensure!(
            c.max_risk_percent.is_finite()
                && c.max_risk_percent > 0.0
                && c.max_risk_percent <= 100.0
                && c.daily_loss_limit_r.is_finite()
                && c.daily_loss_limit_r >= 0.0
                && c.default_large_volume > 0
                && c.default_small_volume > 0
                && c.order_wait_seconds <= 3600
                && c.slippage_ticks <= 100
                && c.max_slippage_ticks <= 10000
                && c.max_exit_slippage_ticks <= 10000,
            "执行参数无效"
        );
        Ok(())
    }
    pub async fn bind(&self, account_id: &str, instance_id: &str) -> Result<()> {
        *self.identity.write().await = (account_id.into(), instance_id.into());
        let cfg = self.get_config().await;
        let mut s = self.state.lock().await;
        let definition = s.definition.clone();
        // Migration enriches old records without registering or reissuing any signal.
        for c in s.conditions.values_mut() {
            if c.origin.instance_id.is_empty() {
                c.origin = TradeOrigin {
                    account_id: account_id.into(),
                    instance_id: instance_id.into(),
                    strategy_id: N_STRATEGY.into(),
                    strategy_version: definition.revision.clone(),
                    signal_id: c
                        .event_id
                        .map(|n| n.to_string())
                        .unwrap_or_else(|| c.id.clone()),
                };
            }
            if c.frozen.is_none() {
                c.frozen = Some(FrozenPlan {
                    definition: definition.clone(),
                    execution: cfg.clone(),
                    plan: EntryPlan {
                        signal_id: c.origin.signal_id.clone(),
                        symbol: c.symbol.clone(),
                        instrument_id: c.instrument_id.clone(),
                        direction: if c.direction == "up" {
                            Direction::Long
                        } else {
                            Direction::Short
                        },
                        reference_price: c.entry_price,
                        trigger: EntryTrigger::Breakout {
                            price: c.entry_price,
                        },
                        stop_price: Some(c.stop_price),
                        target_price: Some(c.target_price),
                        risk_per_unit: c.risk,
                        requested_volume: c.volume,
                        created_at: c.created_at,
                        expires_at: None,
                        reason: c.note.clone(),
                        evidence: serde_json::json!({"entry_score":c.score,"id":c.event_id,"warning_ts":c.warning_bar_ts}),
                    },
                });
            }
        }
        let ready = s
            .conditions
            .values()
            .filter(|c| {
                c.frozen
                    .as_ref()
                    .is_some_and(|f| f.definition.strategy_id != N_STRATEGY)
            })
            .map(|c| c.symbol.clone())
            .collect::<Vec<_>>();
        s.entry_symbols_ready.extend(ready);
        let origins = s
            .conditions
            .iter()
            .map(|(id, c)| (id.clone(), c.origin.clone()))
            .collect::<HashMap<_, _>>();
        for p in s.positions.values_mut() {
            if let Some(o) = origins.get(&p.condition_order_id) {
                p.origin = o.clone();
            }
        }
        for p in &mut s.history {
            if let Some(o) = origins.get(&p.condition_order_id) {
                p.origin = o.clone();
            }
        }
        for p in s.orders.values_mut() {
            if let Some(o) = origins.get(&p.condition_id) {
                p.origin = o.clone();
            }
        }
        self.save(&mut s)
    }
    pub async fn market_subscriptions(&self) -> Vec<MarketSubscription> {
        let s = self.state.lock().await;
        let mut definitions =
            HashMap::from([(s.definition.revision.clone(), s.definition.clone())]);
        for c in s.conditions.values().filter(|c| {
            !matches!(
                c.state,
                ConditionState::Closed
                    | ConditionState::Expired
                    | ConditionState::Canceled
                    | ConditionState::Invalidated
            ) || s.positions.get(&c.id).is_some_and(|p| !p.is_closed)
        }) {
            if let Some(f) = &c.frozen {
                definitions.insert(f.definition.revision.clone(), f.definition.clone());
            }
        }
        definitions
            .values()
            .filter_map(|d| self.registry.strategy(d).ok())
            .flat_map(|strategy| strategy.subscriptions())
            .collect()
    }
    pub async fn definition(&self) -> StrategyDefinition {
        self.state.lock().await.definition.clone()
    }
    pub async fn set_definition(&self, d: StrategyDefinition) -> Result<()> {
        self.configure_strategy(d, self.get_config().await).await
    }
    pub async fn configure_strategy(
        &self,
        mut d: StrategyDefinition,
        cfg: ExecutionConfig,
    ) -> Result<()> {
        self.registry.validate(&d)?;
        Self::validate_config(&cfg)?;
        if self
            .registry
            .strategy(&d)?
            .entry_constraints()
            .terminal_rejections
        {
            anyhow::ensure!(
                cfg.min_entry_risk_ticks == 10,
                "P2/P3最小入场风险必须为10 tick"
            );
        }
        let mut current = self.config.write().await;
        let mut s = self.state.lock().await;
        d.revision = uuid::Uuid::new_v4().to_string();
        let mut next = s.clone();
        next.definition = d;
        next.active_config = Some(cfg.clone());
        next.strategy_error = None;
        // The state file is authoritative. A failed mirror write cannot activate a new version.
        if let Some(path) = &self.config_path {
            atomic_json(path, &cfg)?;
        }
        self.save(&mut next)?;
        *s = next;
        *current = cfg;
        Ok(())
    }
    pub async fn quote_gaps_between(&self, from: i64, to: i64) -> usize {
        self.state
            .lock()
            .await
            .quote_gaps
            .iter()
            .filter(|t| **t >= from && **t <= to)
            .count()
    }
    pub async fn paused(&self) -> bool {
        self.state.lock().await.paused
    }
    pub async fn pause(&self, paused: bool) -> Result<()> {
        let mut s = self.state.lock().await;
        s.paused = paused;
        // Pause also prevents a working entry order from adding exposure.
        if paused {
            let ids = s
                .orders
                .values()
                .filter(|o| {
                    o.reason.is_none()
                        && !matches!(o.phase, OrderPhase::Terminal | OrderPhase::Rejected)
                })
                .map(|o| o.condition_id.clone())
                .collect::<Vec<_>>();
            s.aborted_entries.extend(ids);
        }
        self.save(&mut s)
    }
    pub async fn flatten_positions(&self) -> Result<()> {
        let mut s = self.state.lock().await;
        let positions = s
            .positions
            .values()
            .filter(|p| !p.is_closed && p.volume > 0)
            .cloned()
            .collect::<Vec<_>>();
        let ids = s.conditions.keys().cloned().collect::<Vec<_>>();
        s.aborted_entries.extend(ids);
        for c in s.conditions.values_mut().filter(|c| {
            matches!(
                c.state,
                ConditionState::Pending
                    | ConditionState::WaitingConfirm
                    | ConditionState::Triggered
            )
        }) {
            c.state = ConditionState::Canceled;
            c.note = "紧急平仓已取消后续入场".into();
        }
        for p in positions {
            Self::latch_exit(&mut s, &p.id, "manual", p.fill_price, true);
        }
        self.save(&mut s)
    }
    pub async fn has_unfinished(&self) -> bool {
        let s = self.state.lock().await;
        s.positions.values().any(|p| !p.is_closed && p.volume > 0)
            || s.orders.values().any(|o| {
                !matches!(o.phase, OrderPhase::Terminal | OrderPhase::Rejected)
                    || o.filled < o.reported_filled
            })
            || s.conditions.values().any(|c| {
                matches!(
                    c.state,
                    ConditionState::Pending
                        | ConditionState::WaitingConfirm
                        | ConditionState::Triggered
                        | ConditionState::Unknown
                        | ConditionState::Working
                        | ConditionState::PartFilled
                        | ConditionState::Closing
                )
            })
    }
    pub async fn register_plan(
        &self,
        plan: EntryPlan,
        definition: StrategyDefinition,
        cfg: ExecutionConfig,
    ) -> Result<String> {
        let identity = self.identity.read().await.clone();
        let mut guard = self.state.lock().await;
        let mut next = guard.clone();
        let id = self.insert_plan(&mut next, plan, definition, cfg, identity)?;
        self.commit_state(&mut guard, next)?;
        Ok(id)
    }
    fn commit_state(&self, current: &mut ExecutionState, mut next: ExecutionState) -> Result<()> {
        if let Err(e) = self.save(&mut next) {
            current.persistence_error = next.persistence_error;
            return Err(e);
        }
        *current = next;
        Ok(())
    }
    fn insert_plan(
        &self,
        s: &mut ExecutionState,
        plan: EntryPlan,
        definition: StrategyDefinition,
        cfg: ExecutionConfig,
        identity: (String, String),
    ) -> Result<String> {
        self.registry.validate(&definition)?;
        Self::validate_config(&cfg)?;
        if self
            .registry
            .strategy(&definition)?
            .entry_constraints()
            .terminal_rejections
        {
            anyhow::ensure!(
                cfg.min_entry_risk_ticks == 10,
                "P2/P3最小入场风险固定为10 tick"
            );
        }
        anyhow::ensure!(
            !plan.signal_id.is_empty()
                && !plan.symbol.is_empty()
                && is_actual_futures_contract(&plan.instrument_id),
            "信号编号、品种或实际合约无效"
        );
        anyhow::ensure!(
            plan.reference_price.is_finite()
                && plan.reference_price > 0.0
                && plan.risk_per_unit.is_finite()
                && plan.risk_per_unit > 0.0,
            "价格或风险单位无效"
        );
        let sign = if plan.direction == Direction::Long {
            1.0
        } else {
            -1.0
        };
        anyhow::ensure!(
            plan.stop_price.is_none_or(|p| p.is_finite()
                && p > 0.0
                && (plan.reference_price - p) * sign > 0.0),
            "止损方向无效"
        );
        anyhow::ensure!(
            plan.target_price.is_none_or(|p| p.is_finite() && p > 0.0),
            "目标价格无效"
        );
        anyhow::ensure!(
            matches!(plan.trigger, EntryTrigger::Immediate)
                || matches!(plan.trigger,EntryTrigger::Breakout{price} if price.is_finite()&&price>0.0&&(price-plan.reference_price).abs()<1e-6),
            "突破价格必须等于入场参考价"
        );
        anyhow::ensure!(
            plan.created_at <= chrono::Utc::now().timestamp() + 5
                && plan
                    .expires_at
                    .is_none_or(|t| t >= chrono::Utc::now().timestamp()),
            "信号时间无效或已过期"
        );
        let volume = self.registry.sizing(&definition.sizing_policy)?.volume(
            &plan,
            &cfg,
            &definition.parameters,
        )?;
        anyhow::ensure!(volume > 0, "仓位模块必须返回正整数手数");
        let (account_id, instance_id) = identity;
        if let Some(c) = s.conditions.values().find(|c| {
            c.origin.strategy_id == definition.strategy_id && c.origin.signal_id == plan.signal_id
        }) {
            return Ok(c.id.clone());
        }
        anyhow::ensure!(!s.paused, "实例已暂停新入场");
        let id = uuid::Uuid::new_v4().to_string();
        let score = plan.evidence["entry_score"].as_f64().unwrap_or(0.0);
        let is_n = definition.strategy_id == N_STRATEGY;
        let c = ConditionOrder {
            origin: TradeOrigin {
                account_id,
                instance_id,
                strategy_id: definition.strategy_id.clone(),
                strategy_version: definition.revision.clone(),
                signal_id: plan.signal_id.clone(),
            },
            frozen: Some(FrozenPlan {
                definition,
                execution: cfg.clone(),
                plan: plan.clone(),
            }),
            id: id.clone(),
            event_id: plan.evidence["id"].as_i64(),
            symbol: plan.symbol.clone(),
            instrument_id: plan.instrument_id.clone(),
            direction: if sign > 0.0 { "up" } else { "down" }.into(),
            tier: if is_n && score >= 3.5 {
                PositionTier::Large
            } else {
                PositionTier::Small
            },
            entry_price: plan.reference_price,
            stop_price: plan.stop_price.unwrap_or(0.0),
            target_price: plan.target_price.unwrap_or(0.0),
            risk: plan.risk_per_unit,
            score,
            volume,
            warning_bar_ts: plan.evidence["warning_ts"].as_str().unwrap_or("").into(),
            trigger_ts: None,
            bars_waited: 0,
            max_wait_bars: if is_n { PENDING_BARS } else { usize::MAX },
            state: ConditionState::Pending,
            created_at: plan.created_at,
            note: plan.reason.clone(),
        };
        if is_n {
            s.comparison.register(&c, &cfg, c.created_at);
            let evidence = plan
                .evidence
                .get("source_evidence")
                .filter(|v| !v.is_null())
                .unwrap_or(&plan.evidence);
            s.comparison.attach_evidence(&id, evidence.clone());
        }
        if !is_n {
            s.entry_symbols_ready.insert(plan.symbol.clone());
        }
        s.conditions.insert(id.clone(), c);
        Ok(id)
    }
    pub async fn dispatch(&self, event: StrategyEvent) -> Result<()> {
        let cfg = self.get_config().await;
        let identity = self.identity.read().await.clone();
        let instance_id = identity.1.clone();
        let mut guard = self.state.lock().await;
        let mut s = guard.clone();
        if let StrategyEvent::Quote(q) = &event {
            s.quotes.insert(q.instrument_id.to_uppercase(), q.clone());
        }
        let active = s.definition.clone();
        let mut versions =
            HashMap::from([(active.revision.clone(), (active.clone(), cfg.clone()))]);
        for c in s.conditions.values().filter(|c| {
            matches!(
                c.state,
                ConditionState::Pending
                    | ConditionState::WaitingConfirm
                    | ConditionState::Triggered
                    | ConditionState::Unknown
                    | ConditionState::Working
                    | ConditionState::PartFilled
                    | ConditionState::Closing
            ) || s.positions.get(&c.id).is_some_and(|p| !p.is_closed)
                || s.orders.values().any(|o| {
                    o.condition_id == c.id
                        && !matches!(o.phase, OrderPhase::Terminal | OrderPhase::Rejected)
                })
        }) {
            if let Some(f) = &c.frozen {
                versions.insert(
                    f.definition.revision.clone(),
                    (f.definition.clone(), f.execution.clone()),
                );
            }
        }
        let mut actions = vec![];
        let mut changed = false;
        for (d, version_config) in versions.values() {
            let strategy = match self.registry.strategy(d) {
                Ok(strategy) => strategy,
                Err(e) => {
                    s.paused = true;
                    s.strategy_error = Some(e.to_string());
                    changed = true;
                    tracing::error!("实例 {instance_id} 缺少策略实现: {e}");
                    continue;
                }
            };
            if !strategy.handles_event(&event) {
                continue;
            }
            let bar_key = if let StrategyEvent::BarClosed {
                symbol,
                timeframe,
                timestamp,
                ..
            } = &event
            {
                let key = format!("{}:{}:{}", d.revision, symbol, timeframe);
                if s.strategy_bars.get(&key).is_some_and(|t| t >= timestamp) {
                    continue;
                }
                Some((key, timestamp.clone()))
            } else {
                None
            };
            let positions = s
                .positions
                .values()
                .filter(|p| p.origin.strategy_version == d.revision)
                .cloned()
                .collect::<Vec<_>>();
            let orders = s
                .orders
                .values()
                .filter(|o| o.origin.strategy_version == d.revision)
                .cloned()
                .collect::<Vec<_>>();
            let conditions = s
                .conditions
                .values()
                .filter(|o| o.origin.strategy_version == d.revision)
                .cloned()
                .collect::<Vec<_>>();
            let mut state = s
                .strategy_states
                .get(&d.revision)
                .cloned()
                .unwrap_or(serde_json::Value::Null);
            let context = StrategyContext {
                instance_id: &instance_id,
                positions: &positions,
                orders: &orders,
                conditions: &conditions,
                account: s.account_snapshot.as_ref(),
                quotes: &s.quotes,
                config: version_config,
                allow_entries: d.revision == active.revision && !s.paused,
            };
            let old_state = state.clone();
            let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                strategy.on_event(&event, &context, &mut state)
            }))
            .unwrap_or_else(|_| Err(anyhow::anyhow!("策略模块发生panic")));
            match result {
                Ok(intents) => {
                    changed |= state != old_state || !intents.is_empty() || bar_key.is_some();
                    s.strategy_states.insert(d.revision.clone(), state);
                    if let Some((key, ts)) = bar_key {
                        s.strategy_bars.insert(key, ts);
                    }
                    for intent in intents {
                        actions.push((d.clone(), version_config.clone(), intent));
                    }
                }
                Err(e) => {
                    s.paused = true;
                    s.strategy_error = Some(e.to_string());
                    changed = true;
                    tracing::error!("策略实例 {instance_id} 已暂停新入场: {e}");
                }
            }
        }
        if !changed {
            return Ok(());
        }
        for (d, version_config, intent) in actions {
            let result = match intent {
                TradeIntent::Open(plan) => {
                    if d.revision == active.revision && !s.paused {
                        self.insert_plan(&mut s, plan, d, version_config, identity.clone())
                            .map(|_| ())
                    } else {
                        Ok(())
                    }
                }
                other => Self::apply_control_intent(&mut s, &d, other),
            };
            if let Err(e) = result {
                guard.paused = true;
                guard.strategy_error = Some(e.to_string());
                self.save(&mut guard)?;
                return Err(e);
            }
        }
        // Strategy state and its resulting intents commit together: no crash window can lose a signal.
        self.commit_state(&mut guard, s)
    }
    fn apply_control_intent(
        s: &mut ExecutionState,
        d: &StrategyDefinition,
        intent: TradeIntent,
    ) -> Result<()> {
        let id = match &intent {
            TradeIntent::Close { position_id, .. } | TradeIntent::Protect { position_id, .. } => {
                position_id
            }
            TradeIntent::CancelEntry { condition_id, .. } => condition_id,
            _ => unreachable!(),
        };
        let c = s
            .conditions
            .get(id)
            .ok_or_else(|| anyhow::anyhow!("不存在本实例单据"))?;
        anyhow::ensure!(
            c.origin.strategy_version == d.revision,
            "禁止跨版本操作持仓"
        );
        match intent {
            TradeIntent::Close {
                position_id,
                direction,
                volume,
                reason,
            } => {
                let p = s
                    .positions
                    .get(&position_id)
                    .ok_or_else(|| anyhow::anyhow!("本实例无此持仓"))?;
                anyhow::ensure!(!p.is_closed && p.volume > 0, "持仓已经平仓");
                anyhow::ensure!(
                    (p.direction == "up") == (direction == Direction::Long),
                    "平仓方向与持仓不符"
                );
                anyhow::ensure!(
                    volume.is_none_or(|v| v > 0 && v <= p.volume),
                    "平仓数量无效"
                );
                let reference = p.fill_price;
                let target = volume.unwrap_or(p.volume);
                Self::latch_exit(s, &position_id, &reason, reference, volume.is_none());
                if let Some(t) = s.closes.get_mut(&position_id) {
                    t.target = t.filled + target;
                }
            }
            TradeIntent::Protect {
                position_id,
                stop_price,
            } => {
                let p = s
                    .positions
                    .get_mut(&position_id)
                    .ok_or_else(|| anyhow::anyhow!("本实例无此持仓"))?;
                anyhow::ensure!(stop_price.is_finite() && stop_price > 0.0, "保护价无效");
                anyhow::ensure!(
                    p.current_stop_price <= 0.0
                        || if p.direction == "up" {
                            stop_price >= p.current_stop_price
                        } else {
                            stop_price <= p.current_stop_price
                        },
                    "禁止放宽存量保护价"
                );
                p.current_stop_price = stop_price;
            }
            TradeIntent::CancelEntry {
                condition_id,
                reason,
            } => {
                s.aborted_entries.insert(condition_id.clone());
                if let Some(c) = s.conditions.get_mut(&condition_id) {
                    c.note = reason;
                    c.state = ConditionState::Canceled;
                }
            }
            _ => unreachable!(),
        }
        Ok(())
    }
}
