use super::*;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeSet, VecDeque};

#[derive(Clone, Debug, Deserialize)]
pub struct SettlementReply {
    status: String,
    #[serde(flatten)]
    statement: Option<Statement>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub(super) struct Statement {
    trading_day: String,
    currency: String,
    broker_id: String,
    investor_id: String,
    environment: String,
    total_fee_cents: Option<i64>,
    fills: Vec<StatementFill>,
    closes: Vec<StatementClose>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
struct StatementFill {
    exchange: String,
    instrument: String,
    side: String,
    price: String,
    quantity: i32,
    trade_id: String,
    offset: String,
    fee_cents: i64,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
struct StatementClose {
    exchange: String,
    instrument: String,
    side: String,
    price: String,
    quantity: i32,
    open_day: String,
    open_price: String,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub(super) struct FillLink {
    position_id: String,
    price: f64,
    volume: i32,
    broker_volume: i32,
    offset: String,
    time: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    opening_allocations: Option<Vec<(String, i32)>>,
}

impl FillLink {
    fn new(position_id: &str, trade: &TraderTradeRecord, volume: i32) -> Self {
        Self {
            position_id: position_id.into(),
            price: trade.price,
            volume,
            broker_volume: trade.volume,
            offset: trade.offset.clone(),
            time: trade.trade_time.clone(),
            opening_allocations: None,
        }
    }
}

impl ExecutionState {
    pub(super) fn record_fill_link(
        &mut self,
        key: &str,
        position_id: &str,
        trade: &TraderTradeRecord,
        volume: i32,
    ) {
        let mut link = FillLink::new(position_id, trade, volume);
        if trade.offset != "open" {
            link.opening_allocations = self.allocate_recorded_openings(position_id, trade, volume);
        }
        self.fill_links.insert(key.into(), link);
    }

    fn allocate_recorded_openings(
        &self,
        position_id: &str,
        trade: &TraderTradeRecord,
        volume: i32,
    ) -> Option<Vec<(String, i32)>> {
        let position_volume = self.positions.get(position_id)?.volume;
        let mut openings = Vec::new();
        let mut used = HashMap::<&str, i32>::new();
        for (id, link) in self
            .fill_links
            .iter()
            .filter(|(_, link)| link.position_id == position_id)
        {
            if link.offset == "open" {
                let key = TradeKey::parse(id)?;
                openings.push((
                    key.day,
                    trade_session_phase(&link.time),
                    &link.time,
                    id,
                    link.volume,
                ));
            } else {
                for (opening, qty) in link.opening_allocations.as_ref()? {
                    *used.entry(opening.as_str()).or_default() += qty;
                }
            }
        }
        for (_, _, _, id, amount) in &mut openings {
            *amount -= used.get(id.as_str()).copied().unwrap_or(0);
        }
        if openings.iter().map(|entry| entry.4).sum::<i32>() != position_volume {
            return None;
        }
        openings.sort_unstable();
        let close_day = normalize_date_key(trade_report_day(trade));
        let mut remaining = volume;
        let mut allocations = Vec::new();
        for (day, phase, time, id, available) in openings {
            if available <= 0
                || day > close_day
                || (day == close_day
                    && (phase, time) > (trade_session_phase(&trade.trade_time), &trade.trade_time))
                || (trade.offset == "close_today" && day != close_day)
                || (trade.offset == "close_yesterday" && day == close_day)
            {
                continue;
            }
            let quantity = remaining.min(available);
            allocations.push((id.clone(), quantity));
            remaining -= quantity;
            if remaining == 0 {
                return Some(allocations);
            }
        }
        None
    }
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub(super) struct ReconciledFee {
    pub trading_day: String,
    pub status: &'static str,
    pub source: &'static str,
    pub open_fee_cents: Option<i64>,
    pub close_fee_cents: Option<i64>,
    pub commission_cents: Option<i64>,
    pub net_pnl_cents: Option<i64>,
}

#[derive(Clone)]
pub(super) struct ProjectionCache {
    input: Input,
    pub rows: Arc<HashMap<String, ReconciledFee>>,
}

impl ProjectionCache {
    pub(super) fn belongs_to(&self, account: Option<&str>) -> bool {
        self.input.account.as_deref() == account
    }
}

#[derive(Clone)]
struct Input {
    account: Option<String>,
    history: SharedState<Vec<TradeRecord>>,
    seen: SharedState<HashSet<String>>,
    links: SharedState<HashMap<String, FillLink>>,
    days: SharedState<HashMap<String, Statement>>,
}

impl Input {
    fn capture(state: &ExecutionState) -> Self {
        Self {
            account: state.account.clone(),
            history: state.history.clone(),
            seen: state.seen_trades.clone(),
            links: state.fill_links.clone(),
            days: state.settlement_days.clone(),
        }
    }

    fn same(&self, other: &Self) -> bool {
        self.account == other.account
            && Arc::ptr_eq(&self.history.0, &other.history.0)
            && Arc::ptr_eq(&self.seen.0, &other.seen.0)
            && Arc::ptr_eq(&self.links.0, &other.links.0)
            && Arc::ptr_eq(&self.days.0, &other.days.0)
    }
}

impl ExecutionEngine {
    /// Dates needed by retained fills, closes and the opening legs of complete statements.
    pub async fn settlement_days_to_query(&self) -> Vec<String> {
        let s = self.state.lock().await;
        let mut dates = BTreeSet::new();
        for id in s
            .seen_trades
            .iter()
            .chain(s.history.iter().map(|record| &record.id))
        {
            if let Some(key) = TradeKey::parse(id) {
                dates.insert(key.day);
            }
        }
        let closing_days = s
            .history
            .iter()
            .filter_map(|row| TradeKey::parse(&row.id).map(|key| key.day))
            .collect::<HashSet<_>>();
        for (day, statement) in s.settlement_days.iter() {
            if closing_days.contains(day) && statement_valid(statement) {
                dates.extend(statement.closes.iter().map(|row| row.open_day.clone()));
            }
        }
        dates
            .into_iter()
            .rev()
            .filter(|day| !s.settlement_days.get(day).is_some_and(statement_valid))
            .collect()
    }

    pub async fn apply_settlement_reply(&self, day: &str, reply: SettlementReply) -> Result<bool> {
        if reply.status != "verified" && reply.status != "missing_detail" {
            return Ok(false);
        }
        let Some(statement) = reply.statement else {
            return Ok(false);
        };
        anyhow::ensure!(
            statement.trading_day == day && statement.currency == "CNY",
            "结算账单日期或币种不符"
        );
        let mut s = self.state.lock().await;
        anyhow::ensure!(
            account_matches(s.account.as_deref(), &statement),
            "结算账单账户不符"
        );
        if s.settlement_days.get(day).is_some_and(|previous| {
            previous == &statement || (statement_valid(previous) && !statement_valid(&statement))
        }) {
            return Ok(statement_valid(&statement));
        }
        let valid = statement_valid(&statement);
        let previous = s.settlement_days.clone();
        s.settlement_days.insert(day.into(), statement);
        // A settlement-write failure must not mark the execution engine unready.
        let result = if self.state_path.is_some() {
            self.store
                .lock()
                .unwrap()
                .as_mut()
                .ok_or_else(|| anyhow::anyhow!("执行状态数据库未打开"))
                .and_then(|store| store.save(&s))
        } else {
            Ok(())
        };
        if let Err(error) = result {
            s.settlement_days = previous;
            return Err(error);
        }
        Ok(valid)
    }

    pub async fn refresh_settlement_projection(&self) -> Result<()> {
        let input = Input::capture(&*self.state.lock().await);
        if self
            .settlement_projection
            .read()
            .await
            .as_ref()
            .is_some_and(|cache| cache.input.same(&input))
        {
            return Ok(());
        }
        let work = input.clone();
        let rows = tokio::task::spawn_blocking(move || reconcile(&work)).await?;
        if input.same(&Input::capture(&*self.state.lock().await)) {
            *self.settlement_projection.write().await = Some(ProjectionCache {
                input,
                rows: Arc::new(rows),
            });
        }
        Ok(())
    }
}

fn account_matches(account: Option<&str>, statement: &Statement) -> bool {
    account
        .and_then(|value| value.split_once(':'))
        .is_some_and(|(broker, investor)| {
            broker == statement.broker_id
                && investor == statement.investor_id
                && (if broker == "9999" { "simnow" } else { "real" }) == statement.environment
        })
}

fn statement_valid(statement: &Statement) -> bool {
    !statement.fills.is_empty()
        && statement.fills.iter().all(|fill| fill.quantity > 0)
        && statement.total_fee_cents.is_some_and(|total| {
            statement
                .fills
                .iter()
                .map(|fill| i128::from(fill.fee_cents))
                .sum::<i128>()
                == i128::from(total)
        })
}

#[derive(Clone)]
struct TradeKey {
    day: String,
    exchange: String,
    instrument: String,
    side: String,
    id: String,
}

impl TradeKey {
    fn parse(value: &str) -> Option<Self> {
        let mut parts = value.split(':');
        let day = normalize_date_key(parts.next()?);
        let exchange = parts.next()?.trim().to_ascii_uppercase();
        let instrument = parts.next()?.trim().to_ascii_lowercase();
        let side = parts.next()?.trim().to_ascii_lowercase();
        let id = parts.next()?.trim().to_owned();
        if day.len() != 8
            || !day.bytes().all(|b| b.is_ascii_digit())
            || parts.next().is_some()
            || id.is_empty()
            || !matches!(side.as_str(), "buy" | "sell")
        {
            return None;
        }
        Some(Self {
            day,
            exchange,
            instrument,
            side,
            id,
        })
    }

    fn same_market(&self, row: &StatementFill) -> bool {
        self.exchange.eq_ignore_ascii_case(&row.exchange)
            && self.instrument.eq_ignore_ascii_case(&row.instrument)
            && self.side == row.side
    }

    fn identities(&self, statement: &Statement) -> Vec<usize> {
        let exact = statement
            .fills
            .iter()
            .enumerate()
            .filter_map(|(index, row)| {
                (self.same_market(row)
                    && normalize_number(&row.trade_id) == normalize_number(&self.id))
                .then_some(index)
            })
            .collect::<Vec<_>>();
        if !exact.is_empty() || self.exchange != "CZCE" || !is_number(&self.id) {
            return exact;
        }
        statement
            .fills
            .iter()
            .enumerate()
            .filter_map(|(index, row)| {
                (self.same_market(row)
                    && is_number(&row.trade_id)
                    && row.trade_id.trim().ends_with(normalize_number(&self.id)))
                .then_some(index)
            })
            .collect()
    }
}

fn is_number(value: &str) -> bool {
    !value.trim().is_empty() && value.trim().bytes().all(|b| b.is_ascii_digit())
}

fn normalize_number(value: &str) -> &str {
    if is_number(value) {
        let digits = value.trim().trim_start_matches('0');
        if digits.is_empty() {
            "0"
        } else {
            digits
        }
    } else {
        value.trim()
    }
}

fn price_matches(value: &str, actual: f64) -> bool {
    actual.is_finite()
        && value
            .parse::<f64>()
            .is_ok_and(|value| (value - actual).abs() < 0.000_000_1)
}

fn prices_equal(a: &str, b: &str) -> bool {
    b.parse().is_ok_and(|price| price_matches(a, price))
}

#[derive(Clone, Copy, PartialEq, Eq, Hash)]
struct Resource {
    index: usize,
    detail: Option<usize>,
    limit: i32,
}

struct Demand {
    id: String,
    quantity: i32,
    options: Vec<Resource>,
}

struct Opening<'a> {
    day: &'a str,
    fill: &'a StatementFill,
    owner: Option<(&'a str, &'a FillLink)>,
}

fn openings_for<'a>(input: &'a Input, days: &[(String, &'a Statement)]) -> Vec<Opening<'a>> {
    let mut owners = HashMap::new();
    let by_day = days
        .iter()
        .map(|(day, statement)| (day.as_str(), *statement))
        .collect::<HashMap<_, _>>();
    for id in input.seen.iter() {
        let Some(key) = TradeKey::parse(id) else {
            continue;
        };
        let Some(statement) = by_day.get(key.day.as_str()) else {
            continue;
        };
        if let [index] = key.identities(statement).as_slice() {
            let fill = &statement.fills[*index];
            if fill.offset == "open" {
                let link = input.links.get(id);
                let owner = link.filter(|owned| {
                    owned.offset == "open"
                        && owned.broker_volume == fill.quantity
                        && owned.volume == fill.quantity
                        && price_matches(&fill.price, owned.price)
                });
                if link.is_some() && owner.is_none() {
                    continue;
                }
                let key = (statement.trading_day.as_str(), *index);
                if owners
                    .insert(key, (true, owner.map(|link| (id.as_str(), link))))
                    .is_some()
                {
                    owners.insert(key, (false, None));
                }
            }
        }
    }
    let mut result = Vec::new();
    for (_, statement) in days {
        if !statement_valid(statement) {
            continue;
        }
        for (index, fill) in statement.fills.iter().enumerate() {
            if let Some((true, owner)) = owners.get(&(statement.trading_day.as_str(), index)) {
                result.push(Opening {
                    day: &statement.trading_day,
                    fill,
                    owner: *owner,
                });
            }
        }
    }
    result
}

fn reconcile(input: &Input) -> HashMap<String, ReconciledFee> {
    let mut days = input
        .days
        .iter()
        .filter(|(_, statement)| account_matches(input.account.as_deref(), statement))
        .map(|(day, statement)| (day.clone(), statement))
        .collect::<Vec<_>>();
    days.sort_unstable_by(|a, b| a.0.cmp(&b.0));
    let openings = openings_for(input, &days);
    let by_day = days
        .iter()
        .map(|(day, statement)| (day.as_str(), *statement))
        .collect::<HashMap<_, _>>();
    let mut detail_caps = Vec::new();
    let detail_start = days
        .iter()
        .map(|(day, statement)| {
            let start = detail_caps.len();
            detail_caps.extend(statement.closes.iter().map(|row| row.quantity));
            (day.as_str(), start)
        })
        .collect::<HashMap<_, _>>();
    let mut history = input.history.iter().collect::<Vec<_>>();
    history.sort_by(|a, b| {
        let a_day = a.id.split(':').next().unwrap_or("");
        let b_day = b.id.split(':').next().unwrap_or("");
        (a_day, a.exit_ts.as_str(), a.id.as_str()).cmp(&(b_day, b.exit_ts.as_str(), b.id.as_str()))
    });
    let mut rows = HashMap::new();
    let mut gross = HashMap::new();
    let mut demands = Vec::new();
    let mut history_ids = HashMap::new();
    let mut close_identities = HashMap::new();
    for trade in &history {
        *history_ids.entry(trade.id.as_str()).or_insert(0) += 1;
        if let Some(key) = TradeKey::parse(&trade.id) {
            if let Some(statement) = by_day.get(key.day.as_str()) {
                for index in key.identities(statement) {
                    *close_identities
                        .entry((statement.trading_day.as_str(), index))
                        .or_insert(0) += 1;
                }
            }
        }
    }
    for trade in history {
        let key = TradeKey::parse(&trade.id);
        let day = key.as_ref().map(|key| key.day.as_str()).unwrap_or("");
        let statement = by_day.get(day).copied();
        let mut row = ReconciledFee {
            trading_day: day.to_owned(),
            status: if key.is_none() {
                "unmatched"
            } else if statement.is_none() {
                "pending"
            } else {
                "unmatched"
            },
            source: if input
                .account
                .as_deref()
                .is_some_and(|id| id.starts_with("9999:"))
            {
                "simnow_settlement"
            } else {
                "broker_settlement"
            },
            open_fee_cents: None,
            close_fee_cents: None,
            commission_cents: None,
            net_pnl_cents: None,
        };
        if statement.is_some_and(|statement| !statement_valid(statement)) {
            row.status = "missing_detail";
        }
        if let (Some(key), Some(statement)) = (
            key.as_ref(),
            statement.filter(|statement| statement_valid(statement)),
        ) {
            let indices = key.identities(statement);
            if let [index] = indices.as_slice() {
                let fill = &statement.fills[*index];
                if fill.offset != "open"
                    && trade.volume > 0
                    && trade.volume <= fill.quantity
                    && price_matches(&fill.price, trade.exit_price)
                    && input
                        .links
                        .get(&trade.id)
                        .map_or(fill.quantity == trade.volume, |link| {
                            link.volume == trade.volume
                                && link.broker_volume == fill.quantity
                                && link.position_id == trade.condition_order_id
                                && link.offset != "open"
                        })
                    && history_ids.get(trade.id.as_str()) == Some(&1)
                    && close_identities.get(&(statement.trading_day.as_str(), *index)) == Some(&1)
                {
                    row.close_fee_cents =
                        Some(fee_share(fill.fee_cents, fill.quantity, 0, trade.volume));
                    let options = open_options(
                        trade,
                        key,
                        fill,
                        statement,
                        detail_start[day],
                        &openings,
                        input,
                    );
                    if !options.is_empty() {
                        demands.push(Demand {
                            id: trade.id.clone(),
                            quantity: trade.volume,
                            options,
                        });
                    }
                }
            }
        }
        if trade.realized_pnl_amount.is_finite() {
            gross.insert(
                trade.id.clone(),
                (trade.realized_pnl_amount * 100.0).round() as i64,
            );
        }
        rows.insert(trade.id.clone(), row);
    }
    for (id, open_fee) in solve_components(&demands, &openings, &detail_caps) {
        let row = rows.get_mut(id).unwrap();
        if let (Some(total), Some(gross)) = (
            row.close_fee_cents
                .and_then(|fee| fee.checked_add(open_fee)),
            gross.get(id),
        ) {
            row.open_fee_cents = Some(open_fee);
            row.commission_cents = Some(total);
            row.net_pnl_cents = gross.checked_sub(total);
            if row.net_pnl_cents.is_some() {
                row.status = "confirmed";
            }
        }
    }
    rows
}

fn open_options(
    trade: &TradeRecord,
    key: &TradeKey,
    close: &StatementFill,
    statement: &Statement,
    detail_start: usize,
    openings: &[Opening<'_>],
    input: &Input,
) -> Vec<Resource> {
    let eligible = openings
        .iter()
        .enumerate()
        .filter(|(_, opening)| {
            opening.fill.exchange == close.exchange
                && opening
                    .fill
                    .instrument
                    .eq_ignore_ascii_case(&close.instrument)
                && opening.fill.side != close.side
                && opening.day <= key.day.as_str()
                && (close.offset != "close_today" || opening.day == key.day)
                && (close.offset != "close_yesterday" || opening.day < key.day.as_str())
                && opening
                    .owner
                    .is_none_or(|(_, owner)| owner.position_id == trade.condition_order_id)
                && (opening.day != key.day
                    || input.links.get(&trade.id).is_none_or(|close_link| {
                        opening.owner.is_none_or(|(_, open_link)| {
                            (trade_session_phase(&open_link.time), &open_link.time)
                                <= (trade_session_phase(&close_link.time), &close_link.time)
                        })
                    }))
        })
        .collect::<Vec<_>>();
    if let Some(allocations) = input
        .links
        .get(&trade.id)
        .and_then(|link| link.opening_allocations.as_ref())
    {
        return allocations
            .iter()
            .map(|(id, quantity)| {
                let (index, _) = eligible
                    .iter()
                    .find(|(_, opening)| opening.owner.is_some_and(|(key, _)| key == id))?;
                Some(Resource {
                    index: *index,
                    detail: None,
                    limit: *quantity,
                })
            })
            .collect::<Option<Vec<_>>>()
            .unwrap_or_default();
    }
    let mut options = Vec::new();
    for (i, detail) in statement.closes.iter().enumerate() {
        if detail.quantity <= 0
            || detail.exchange != close.exchange
            || !detail.instrument.eq_ignore_ascii_case(&close.instrument)
            || detail.side != close.side
            || !prices_equal(&detail.price, &close.price)
        {
            continue;
        }
        if !input.days.get(&detail.open_day).is_some_and(|day| {
            account_matches(input.account.as_deref(), day) && statement_valid(day)
        }) {
            // A plausible opening from a date we have not fetched would make the amount incomplete.
            return Vec::new();
        }
        for (index, opening) in &eligible {
            if opening.day == detail.open_day
                && (prices_equal(&detail.open_price, &opening.fill.price)
                    || price_matches(&opening.fill.price, trade.fill_price)
                    || opening.owner.is_some())
            {
                options.push(Resource {
                    index: *index,
                    detail: Some(detail_start + i),
                    limit: trade.volume,
                });
            }
        }
    }
    options
}

fn fee_share(total: i64, volume: i32, used: i32, quantity: i32) -> i64 {
    let cents_at = |lots: i32| {
        let value = i128::from(total) * i128::from(lots);
        (value + value.signum() * i128::from(volume / 2)) / i128::from(volume)
    };
    (cents_at(used + quantity) - cents_at(used)) as i64
}

/// Independent groups keep common one-to-one matches cheap even for large histories.
fn solve_components<'a>(
    demands: &'a [Demand],
    openings: &[Opening<'_>],
    detail_caps: &[i32],
) -> Vec<(&'a str, i64)> {
    let mut resource_users = HashMap::<(bool, usize), Vec<usize>>::new();
    for (i, demand) in demands.iter().enumerate() {
        for option in &demand.options {
            resource_users
                .entry((false, option.index))
                .or_default()
                .push(i);
            if let Some(detail) = option.detail {
                resource_users.entry((true, detail)).or_default().push(i);
            }
        }
    }
    let mut visited = HashSet::new();
    let mut result = Vec::new();
    for root in 0..demands.len() {
        if !visited.insert(root) {
            continue;
        }
        let mut component = Vec::new();
        let mut queue = VecDeque::from([root]);
        while let Some(index) = queue.pop_front() {
            component.push(index);
            for option in &demands[index].options {
                let keys = [
                    (false, option.index),
                    (true, option.detail.unwrap_or(usize::MAX)),
                ];
                for key in keys {
                    for user in resource_users.remove(&key).into_iter().flatten() {
                        if visited.insert(user) {
                            queue.push_back(user);
                        }
                    }
                }
            }
        }
        // Limit ambiguous legacy searches. An incomplete proof never becomes a confirmed fee.
        if component.len() > 128
            || component
                .iter()
                .any(|&index| demands[index].options.len() > 128)
            || component
                .iter()
                .map(|&index| demands[index].options.len())
                .sum::<usize>()
                > 768
        {
            continue;
        }
        component.sort_unstable();
        let mut search = AllocationSearch {
            component: &component,
            demands,
            openings,
            detail_caps,
            used: HashMap::new(),
            assigned: Vec::new(),
            first: None,
            budget: 60_000,
        };
        search.search(0, 0, demands[component[0]].quantity, 0);
        if search.budget != 0 {
            if let Some(fees) = search.first {
                for (&index, fee) in component.iter().zip(fees) {
                    if let Some(fee) = fee {
                        result.push((demands[index].id.as_str(), fee));
                    }
                }
            }
        }
    }
    result
}

struct AllocationSearch<'a> {
    component: &'a [usize],
    demands: &'a [Demand],
    openings: &'a [Opening<'a>],
    detail_caps: &'a [i32],
    used: HashMap<(bool, usize), i32>,
    assigned: Vec<i64>,
    first: Option<Vec<Option<i64>>>,
    budget: usize,
}

impl AllocationSearch<'_> {
    fn search(&mut self, step: usize, candidate: usize, remaining: i32, total: i64) {
        if self.budget == 0 {
            return;
        }
        self.budget -= 1;
        if remaining == 0 {
            self.assigned.push(total);
            if step + 1 == self.component.len() {
                if let Some(fees) = &mut self.first {
                    for (fee, actual) in fees.iter_mut().zip(&self.assigned) {
                        if *fee != Some(*actual) {
                            *fee = None;
                        }
                    }
                } else {
                    self.first = Some(self.assigned.iter().copied().map(Some).collect());
                }
            } else {
                self.search(
                    step + 1,
                    0,
                    self.demands[self.component[step + 1]].quantity,
                    0,
                );
            }
            self.assigned.pop();
            return;
        }
        let demand = &self.demands[self.component[step]];
        if candidate >= demand.options.len() || candidate >= 128 {
            return;
        }
        let option = demand.options[candidate];
        let open_key = (false, option.index);
        let open = self.openings[option.index].fill;
        let already = *self.used.get(&open_key).unwrap_or(&0);
        let mut upper = remaining.min(open.quantity - already).min(option.limit);
        let detail = option
            .detail
            .map(|index| ((true, index), self.detail_caps[index]));
        if let Some((key, limit)) = detail {
            upper = upper.min(limit - *self.used.get(&key).unwrap_or(&0));
        }
        let lower = if candidate + 1 == demand.options.len() {
            remaining
        } else {
            0
        };
        for quantity in (lower..=upper).rev() {
            if self.budget == 0 {
                break;
            }
            *self.used.entry(open_key).or_default() += quantity;
            if let Some((key, _)) = detail {
                *self.used.entry(key).or_default() += quantity;
            }
            self.search(
                step,
                candidate + 1,
                remaining - quantity,
                total + fee_share(open.fee_cents, open.quantity, already, quantity),
            );
            *self.used.get_mut(&open_key).unwrap() -= quantity;
            if let Some((key, _)) = detail {
                *self.used.get_mut(&key).unwrap() -= quantity;
            }
        }
    }
}

#[cfg(test)]
#[path = "execution_settlement_tests.rs"]
mod tests;
