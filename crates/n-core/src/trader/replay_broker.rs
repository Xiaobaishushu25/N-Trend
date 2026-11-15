//! Queue transport only: matching, account state and fill confirmation belong to TqSim.
use super::*;
#[derive(Default)]
pub struct ReplayBroker {
    requests: Mutex<Vec<Value>>,
    multipliers: Mutex<BTreeMap<String, f64>>,
}
impl ReplayBroker {
    pub fn set_multiplier(&self, instrument: &str, multiplier: f64) -> Result<()> {
        ensure!(
            is_actual_futures_contract(instrument) && multiplier.is_finite() && multiplier > 0.,
            "Invalid replay contract metadata"
        );
        self.multipliers
            .lock()
            .unwrap()
            .insert(instrument.to_uppercase(), multiplier);
        Ok(())
    }
    pub fn multiplier(&self, instrument: &str) -> f64 {
        self.multipliers
            .lock()
            .unwrap()
            .get(&instrument.to_uppercase())
            .copied()
            .unwrap_or(0.)
    }
    pub fn drain(&self) -> Vec<Value> {
        std::mem::take(&mut *self.requests.lock().unwrap())
    }
    pub fn insert(&self, request: &OrderInsertRequest) -> Result<OrderInsertResponse> {
        ensure!(
            is_actual_futures_contract(&request.instrument_id)
                && request.volume > 0
                && request.price.is_finite()
                && request.price > 0.,
            "Invalid replay order"
        );
        let reference = request
            .order_ref
            .as_ref()
            .filter(|r| !r.is_empty())
            .ok_or_else(|| anyhow::anyhow!("Replay order requires a reference"))?;
        ensure!(
            self.multiplier(&request.instrument_id) > 0.,
            "Missing replay multiplier"
        );
        self.requests
            .lock()
            .unwrap()
            .push(json!({"action":"insert","request":request}));
        Ok(OrderInsertResponse {
            success: true,
            order_ref: reference.clone(),
            msg: "queued for TqSim".into(),
        })
    }
    pub fn cancel(
        &self,
        reference: &str,
        instrument: &str,
        exchange: Option<&str>,
    ) -> Result<OrderCancelResponse> {
        self.requests.lock().unwrap().push(json!({"action":"cancel","order_ref":reference,"instrument_id":instrument,"exchange_id":exchange}));
        Ok(OrderCancelResponse {
            success: true,
            msg: "cancel requested; awaiting TqSim terminal state".into(),
        })
    }
}
