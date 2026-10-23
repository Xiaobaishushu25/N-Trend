//! Execution transports. The paper variant has no HTTP/CTP capability.
use super::{client::CtpTraderClient, model::*};
use anyhow::{ensure, Result};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    path::PathBuf,
    sync::{Arc, Mutex},
};

pub(crate) fn atomic_write(path: &std::path::Path, value: &impl Serialize) -> Result<()> {
    use std::io::Write;
    let temp = path.with_extension("tmp");
    let mut file = std::fs::File::create(&temp)?;
    file.write_all(&serde_json::to_vec(value)?)?;
    file.sync_all()?;
    std::fs::rename(temp, path)?;
    Ok(())
}

#[derive(Clone)]
pub enum BrokerClient {
    Ctp(CtpTraderClient),
    Paper(Arc<PaperBroker>),
    Disabled(String),
}
impl From<CtpTraderClient> for BrokerClient {
    fn from(c: CtpTraderClient) -> Self {
        Self::Ctp(c)
    }
}
impl BrokerClient {
    pub fn multiplier(&self, instrument: &str) -> f64 {
        match self {
            Self::Disabled(_) => 0.0,
            Self::Ctp(_) => crate::precision::effective_multiplier(instrument, ""),
            Self::Paper(p) => p
                .config
                .contracts
                .get(&instrument.to_uppercase())
                .map(|c| c.multiplier)
                .unwrap_or(0.0),
        }
    }
    pub async fn insert_order(&self, r: &OrderInsertRequest) -> Result<OrderInsertResponse> {
        match self {
            Self::Disabled(reason) => Err(anyhow::anyhow!("账户执行已禁用: {reason}")),
            Self::Ctp(c) => c.insert_order(r).await,
            Self::Paper(p) => p.insert(r),
        }
    }
    pub async fn cancel_order(
        &self,
        r: &str,
        i: &str,
        e: Option<&str>,
    ) -> Result<OrderCancelResponse> {
        match self {
            Self::Disabled(reason) => Err(anyhow::anyhow!("账户执行已禁用: {reason}")),
            Self::Ctp(c) => c.cancel_order(r, i, e).await,
            Self::Paper(p) => p.cancel(r),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PaperContract {
    pub multiplier: f64,
    pub margin_rate: f64,
    pub open_fee_per_lot: f64,
    pub close_fee_per_lot: f64,
    pub close_today_fee_per_lot: f64,
}
impl PaperContract {
    fn valid(&self) -> bool {
        self.multiplier.is_finite()
            && self.multiplier > 0.0
            && self.margin_rate.is_finite()
            && self.margin_rate > 0.0
            && self.margin_rate <= 1.0
            && [
                self.open_fee_per_lot,
                self.close_fee_per_lot,
                self.close_today_fee_per_lot,
            ]
            .iter()
            .all(|x| x.is_finite() && *x >= 0.0)
    }
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PaperConfig {
    pub initial_balance: f64,
    pub contracts: BTreeMap<String, PaperContract>,
}
impl PaperConfig {
    pub fn validate(&self) -> Result<()> {
        ensure!(
            self.initial_balance.is_finite() && self.initial_balance > 0.0,
            "模拟初始资金必须大于0"
        );
        let keys = self
            .contracts
            .keys()
            .map(|s| s.to_uppercase())
            .collect::<std::collections::HashSet<_>>();
        ensure!(keys.len() == self.contracts.len(), "模拟合约存在大小写重复");
        ensure!(
            self.contracts
                .iter()
                .all(|(s, c)| is_actual_futures_contract(s) && c.valid()),
            "模拟合约成本或保证金参数无效"
        );
        Ok(())
    }
}
#[derive(Debug, Clone, Serialize, Deserialize)]
struct Lot {
    instrument: String,
    direction: String,
    price: f64,
    volume: i32,
    day: String,
    margin: f64,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
struct PaperState {
    balance: f64,
    realized: f64,
    fees: f64,
    lots: Vec<Lot>,
    orders: BTreeMap<String, TraderOrderRecord>,
    trades: Vec<TraderTradeRecord>,
    requests: BTreeMap<String, OrderInsertRequest>,
}
pub struct PaperBroker {
    id: String,
    config: PaperConfig,
    path: PathBuf,
    state: Mutex<PaperState>,
    quotes: Mutex<BTreeMap<String, ExecutionQuote>>,
    _lock: std::fs::File,
}
impl PaperBroker {
    pub fn open(id: String, mut config: PaperConfig, path: PathBuf) -> Result<Self> {
        config.validate()?;
        config.contracts = config
            .contracts
            .into_iter()
            .map(|(s, c)| (s.to_uppercase(), c))
            .collect();
        let lock = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(path.with_extension("lock"))?;
        lock.try_lock()
            .map_err(|e| anyhow::anyhow!("模拟账户已占用: {e}"))?;
        let state = if path.exists() {
            serde_json::from_slice(&std::fs::read(&path)?)?
        } else {
            PaperState {
                balance: config.initial_balance,
                realized: 0.0,
                fees: 0.0,
                lots: vec![],
                orders: BTreeMap::new(),
                trades: vec![],
                requests: BTreeMap::new(),
            }
        };
        Ok(Self {
            id,
            config,
            path,
            state: Mutex::new(state),
            quotes: Mutex::new(BTreeMap::new()),
            _lock: lock,
        })
    }
    pub fn on_quotes(&self, quotes: &[(String, ExecutionQuote)]) -> Result<()> {
        {
            let mut cached = self.quotes.lock().unwrap();
            for (_, q) in quotes {
                if q.valid() {
                    cached.insert(q.instrument_id.to_uppercase(), q.clone());
                }
            }
        }
        let mut state = self.state.lock().unwrap();
        let mut next = state.clone();
        let pending = next
            .orders
            .values()
            .filter(|o| o.status == "queueing")
            .map(|o| o.order_ref.clone())
            .collect::<Vec<_>>();
        for r in pending {
            let req = next.requests[&r].clone();
            self.fill(&mut next, &r, &req)?;
        }
        if serde_json::to_value(&next)? != serde_json::to_value(&*state)? {
            atomic_write(&self.path, &next)?;
            *state = next;
        }
        Ok(())
    }
    fn marked(&self, s: &PaperState) -> (f64, f64) {
        let quotes = self.quotes.lock().unwrap();
        let margin = s.lots.iter().map(|l| l.margin).sum();
        let floating = s
            .lots
            .iter()
            .map(|l| {
                let price = quotes
                    .get(&l.instrument.to_uppercase())
                    .map(|q| q.last)
                    .unwrap_or(l.price);
                let mult = self.config.contracts[&l.instrument.to_uppercase()].multiplier;
                (price - l.price)
                    * if l.direction == "buy" { 1.0 } else { -1.0 }
                    * mult
                    * l.volume as f64
            })
            .sum();
        (margin, floating)
    }
    pub fn insert(&self, r: &OrderInsertRequest) -> Result<OrderInsertResponse> {
        ensure!(
            r.volume > 0
                && r.price.is_finite()
                && r.price > 0.0
                && matches!(r.direction.as_str(), "buy" | "sell")
                && matches!(r.offset.as_str(), "open" | "auto" | "close" | "close_today"),
            "模拟报单参数无效"
        );
        let reference = r
            .order_ref
            .as_deref()
            .filter(|s| !s.is_empty())
            .ok_or_else(|| anyhow::anyhow!("模拟委托必须有稳定编号"))?;
        let mut state = self.state.lock().unwrap();
        if let Some(old) = state.requests.get(reference) {
            ensure!(
                serde_json::to_value(old)? == serde_json::to_value(r)?,
                "委托编号重复且内容不同"
            );
            let o = &state.orders[reference];
            return Ok(OrderInsertResponse {
                success: o.status != "rejected",
                order_ref: reference.into(),
                msg: o.status_msg.clone(),
            });
        }
        let mut next = state.clone();
        let now = chrono::Local::now().format("%H:%M:%S").to_string();
        next.requests.insert(reference.into(), r.clone());
        next.orders.insert(
            reference.into(),
            TraderOrderRecord {
                order_ref: reference.into(),
                instrument_id: r.instrument_id.clone(),
                exchange_id: r.exchange_id.clone().unwrap_or_default(),
                direction: r.direction.clone(),
                offset: r.offset.clone(),
                limit_price: r.price,
                volume_total_original: r.volume,
                volume_traded: 0,
                volume_total: r.volume,
                status: "queueing".into(),
                status_msg: "模拟委托等待有效盘口".into(),
                insert_time: now.clone(),
                update_time: now,
            },
        );
        if !self
            .config
            .contracts
            .contains_key(&r.instrument_id.to_uppercase())
        {
            Self::reject(&mut next, reference, "缺少合约费用、乘数或保证金参数");
        } else {
            self.fill(&mut next, reference, r)?;
        }
        atomic_write(&self.path, &next)?;
        let o = &next.orders[reference];
        let result = OrderInsertResponse {
            success: o.status != "rejected",
            order_ref: reference.into(),
            msg: o.status_msg.clone(),
        };
        *state = next;
        Ok(result)
    }
    fn reject(s: &mut PaperState, r: &str, msg: &str) {
        let o = s.orders.get_mut(r).unwrap();
        o.status = "rejected".into();
        o.status_msg = msg.into();
    }
    fn fill(&self, s: &mut PaperState, r: &str, req: &OrderInsertRequest) -> Result<()> {
        let quote = self
            .quotes
            .lock()
            .unwrap()
            .get(&req.instrument_id.to_uppercase())
            .cloned();
        let Some(q) = quote.filter(|q| q.valid()) else {
            return Ok(());
        };
        if req
            .exchange_id
            .as_ref()
            .is_some_and(|e| !e.eq_ignore_ascii_case(&q.exchange_id))
        {
            Self::reject(s, r, "合约交易所不匹配");
            return Ok(());
        }
        let price = if req.direction == "buy" { q.ask } else { q.bid };
        if !price.is_finite()
            || price < q.lower
            || price > q.upper
            || (if req.direction == "buy" {
                price > req.price
            } else {
                price < req.price
            })
        {
            return Ok(());
        }
        let c = &self.config.contracts[&req.instrument_id.to_uppercase()];
        let day = paper_trading_day();
        let qty = req.volume;
        let fee;
        if req.offset == "open" {
            let margin = price * c.multiplier * c.margin_rate * qty as f64;
            fee = c.open_fee_per_lot * qty as f64;
            let (used, floating) = self.marked(s);
            if s.balance + floating - used + 1e-6 < margin + fee {
                Self::reject(s, r, "[31]:资金不足");
                return Ok(());
            }
            s.lots.push(Lot {
                instrument: req.instrument_id.clone(),
                direction: req.direction.clone(),
                price,
                volume: qty,
                day: day.clone(),
                margin,
            });
        } else {
            let available: i32 = s
                .lots
                .iter()
                .filter(|l| {
                    l.instrument.eq_ignore_ascii_case(&req.instrument_id)
                        && l.direction != req.direction
                        && (req.offset != "close_today" || l.day == day)
                })
                .map(|l| l.volume)
                .sum();
            if available < qty {
                Self::reject(s, r, "平仓数量超过模拟持仓");
                return Ok(());
            }
            let mut left = qty;
            let mut costs = 0.0;
            let mut profit = 0.0;
            for l in s.lots.iter_mut().filter(|l| {
                l.instrument.eq_ignore_ascii_case(&req.instrument_id)
                    && l.direction != req.direction
                    && (req.offset != "close_today" || l.day == day)
            }) {
                let n = left.min(l.volume);
                if n <= 0 {
                    continue;
                }
                left -= n;
                profit += (price - l.price)
                    * if l.direction == "buy" { 1.0 } else { -1.0 }
                    * c.multiplier
                    * n as f64;
                costs += if l.day == day {
                    c.close_today_fee_per_lot
                } else {
                    c.close_fee_per_lot
                } * n as f64;
                l.margin *= (l.volume - n) as f64 / l.volume as f64;
                l.volume -= n;
            }
            s.lots.retain(|l| l.volume > 0);
            s.balance += profit;
            s.realized += profit;
            fee = costs;
        }
        s.balance -= fee;
        s.fees += fee;
        let o = s.orders.get_mut(r).unwrap();
        o.status = "all_traded".into();
        o.volume_traded = qty;
        o.volume_total = 0;
        o.status_msg = "模拟盘口成交".into();
        s.trades.push(TraderTradeRecord {
            trade_id: format!("paper-{r}"),
            order_ref: r.into(),
            instrument_id: req.instrument_id.clone(),
            exchange_id: q.exchange_id,
            direction: req.direction.clone(),
            offset: if req.offset == "open" {
                "open"
            } else {
                "close"
            }
            .into(),
            price,
            volume: qty,
            trade_time: chrono::Local::now().format("%H:%M:%S").to_string(),
            trade_date: day.clone(),
            trading_day: day,
        });
        Ok(())
    }
    pub fn cancel(&self, r: &str) -> Result<OrderCancelResponse> {
        let mut state = self.state.lock().unwrap();
        let mut next = state.clone();
        if let Some(o) = next.orders.get_mut(r).filter(|o| o.status == "queueing") {
            o.status = "canceled".into();
            o.status_msg = "模拟撤单".into();
        }
        atomic_write(&self.path, &next)?;
        *state = next;
        Ok(OrderCancelResponse {
            success: true,
            msg: "模拟撤单完成".into(),
        })
    }
    pub fn snapshot(&self) -> Value {
        let s = self.state.lock().unwrap();
        let (margin, floating) = self.marked(&s);
        let now = chrono::Utc::now().timestamp();
        let mut positions: BTreeMap<(String, String), Value> = BTreeMap::new();
        let day = paper_trading_day();
        let quotes = self.quotes.lock().unwrap();
        for l in &s.lots {
            let p=positions.entry((l.instrument.clone(),l.direction.clone())).or_insert_with(||json!({"instrument_id":l.instrument,"direction":l.direction,"position":0,"yd_position":0,"today_position":0,"open_cost":0.0,"position_profit":0.0,"margin":0.0}));
            p["position"] = json!(p["position"].as_i64().unwrap() + l.volume as i64);
            let field = if l.day == day {
                "today_position"
            } else {
                "yd_position"
            };
            p[field] = json!(p[field].as_i64().unwrap() + l.volume as i64);
            p["open_cost"] = json!(
                p["open_cost"].as_f64().unwrap()
                    + l.price
                        * self.config.contracts[&l.instrument.to_uppercase()].multiplier
                        * l.volume as f64
            );
            p["margin"] = json!(p["margin"].as_f64().unwrap() + l.margin);
            let price = quotes
                .get(&l.instrument.to_uppercase())
                .map(|q| q.last)
                .unwrap_or(l.price);
            let profit = (price - l.price)
                * (if l.direction == "buy" { 1.0 } else { -1.0 })
                * self.config.contracts[&l.instrument.to_uppercase()].multiplier
                * l.volume as f64;
            p["position_profit"] = json!(p["position_profit"].as_f64().unwrap() + profit);
        }
        json!({"source":"server","bridge_online":true,"health":{"service":"ntrend-paper","healthy":true,"ready":true,"connected":true,"authenticated":true,"uptime":0.0,"sync_pending":false,"logged_in":true,"settlement_confirmed":true,"broker_id":"paper","user_id":self.id,"account_id":self.id,"trading_day":paper_trading_day(),"orders_synced":true,"positions_synced":true,"trades_synced":true},
            "account":{"balance":s.balance+floating,"available":s.balance+floating-margin,"curr_margin":margin,"close_profit":s.realized,"position_profit":floating,"commission":s.fees,"updated_at":now},
            "broker_positions":positions.values().collect::<Vec<_>>(),"orders":s.orders.values().collect::<Vec<_>>(),"trades":s.trades,"fees_confirmed":true})
    }
}

fn paper_trading_day() -> String {
    use chrono::{Days, Timelike};
    let now = chrono::Local::now();
    let mut day = now.date_naive();
    if now.hour() >= 20 {
        day = day.checked_add_days(Days::new(1)).unwrap();
    }
    for _ in 0..30 {
        if crate::session::trading_calendar::is_trading_day(&day) {
            break;
        }
        day = day.checked_add_days(Days::new(1)).unwrap();
    }
    day.format("%Y%m%d").to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn short_partial_close_releases_proportional_margin_and_uses_yesterday_fee() {
        let dir = std::env::temp_dir().join(format!("ntrend-paper-short-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).unwrap();
        let c = PaperConfig {
            initial_balance: 2000.0,
            contracts: BTreeMap::from([(
                "PB2611".into(),
                PaperContract {
                    multiplier: 5.0,
                    margin_rate: 0.1,
                    open_fee_per_lot: 2.0,
                    close_fee_per_lot: 3.0,
                    close_today_fee_per_lot: 4.0,
                },
            )]),
        };
        let p = PaperBroker::open("short".into(), c, dir.join("paper.json")).unwrap();
        let q = ExecutionQuote {
            instrument_id: "pb2611".into(),
            exchange_id: "SHFE".into(),
            underlying_symbol: "PB0".into(),
            last: 1000.0,
            bid: 999.0,
            ask: 1001.0,
            tick: 1.0,
            lower: 500.0,
            upper: 2000.0,
            timestamp: chrono::Utc::now().timestamp(),
        };
        p.on_quotes(&[("PB2611".into(), q.clone())]).unwrap();
        let open = OrderInsertRequest {
            instrument_id: "pb2611".into(),
            direction: "sell".into(),
            offset: "open".into(),
            price: 990.0,
            volume: 2,
            exchange_id: Some("SHFE".into()),
            order_ref: Some("open".into()),
        };
        assert!(p.insert(&open).unwrap().success);
        p.state.lock().unwrap().lots[0].day = "20000101".into();
        let close = OrderInsertRequest {
            direction: "buy".into(),
            offset: "close_today".into(),
            price: 1100.0,
            volume: 1,
            order_ref: Some("bad-today".into()),
            ..open.clone()
        };
        assert!(!p.insert(&close).unwrap().success);
        let close = OrderInsertRequest {
            offset: "auto".into(),
            order_ref: Some("close-one".into()),
            ..close
        };
        p.insert(&close).unwrap();
        let s = p.snapshot();
        assert_eq!(s["account"]["commission"], 7.0);
        assert_eq!(s["account"]["curr_margin"], 499.5);
        assert_eq!(s["broker_positions"][0]["yd_position"], 1);
        assert_eq!(s["broker_positions"][0]["today_position"], 0);
        assert_eq!(s["trades"][0]["price"], 999.0);
        assert_eq!(s["trades"][1]["price"], 1001.0);
    }
}
