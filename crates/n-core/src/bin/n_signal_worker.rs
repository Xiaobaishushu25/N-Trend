//! Lightweight stdio JSON-Lines signal worker for local backtesting.
//! Exposes incremental forward-only N-pattern detection (`selected_warnings`)
//! with 100% algorithmic parity with `n-core` production analysis.

use std::collections::HashMap;
use std::io::{self, BufRead, Write};

use anyhow::{anyhow, Result};
use serde::{Deserialize, Serialize};

use n_core::analyze::event::{selected_warnings, WarningCandidate};
use n_core::analyze::model::{Bar, Dir, ATR_PERIOD, DT};
use n_core::precision::{effective_multiplier, effective_tick};

#[derive(Debug, Clone, Deserialize)]
struct BarInput {
    datetime: String,
    open: f64,
    high: f64,
    low: f64,
    close: f64,
    #[serde(default)]
    volume: f64,
    #[serde(default)]
    hold: f64,
    #[serde(default)]
    rollover: bool,
}

impl BarInput {
    fn to_bar(&self) -> Result<Bar> {
        let dt = DT::from_bar_ts(&self.datetime)
            .ok_or_else(|| anyhow!("invalid bar datetime: {}", self.datetime))?;
        Ok(Bar {
            dt,
            open: self.open,
            high: self.high,
            low: self.low,
            close: self.close,
            volume: self.volume,
            hold: self.hold,
            rollover: self.rollover,
        })
    }
}

#[derive(Debug, Deserialize)]
#[serde(tag = "cmd", rename_all = "snake_case")]
enum WorkerRequest {
    Ping,
    InitSymbol {
        symbol: String,
        #[serde(default)]
        tick: f64,
        #[serde(default)]
        bars: Vec<BarInput>,
    },
    StepBar {
        symbol: String,
        bar: BarInput,
    },
}

#[derive(Debug, Clone, Serialize, PartialEq)]
struct SignalDto {
    symbol: String,
    direction: String,
    grade: String,
    level: String,
    warning_kind: String,
    entry_score: f64,
    dim_a: f64,
    dim_b: f64,
    dim_warning: f64,
    entry: f64,
    stop: f64,
    target: f64,
    risk: f64,
    rr: f64,
    trend_state: String,
    warning_index: usize,
    warning_ts: String,
    s0_ts: String,
    s1_ts: String,
    s2_ts: String,
    s0_price: f64,
    s1_price: f64,
    s2_price: f64,
}

#[derive(Debug, Serialize)]
struct WorkerResponse {
    ok: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    symbol: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    tick: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    multiplier: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    bar_count: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    signals: Option<Vec<SignalDto>>,
}

struct SymbolState {
    symbol: String,
    tick: f64,
    multiplier: f64,
    bars: Vec<Bar>,
}

struct WorkerState {
    symbols: HashMap<String, SymbolState>,
}

impl WorkerState {
    fn new() -> Self {
        Self {
            symbols: HashMap::new(),
        }
    }

    fn handle(&mut self, req: WorkerRequest) -> WorkerResponse {
        match self.handle_inner(req) {
            Ok(resp) => resp,
            Err(err) => WorkerResponse {
                ok: false,
                error: Some(err.to_string()),
                symbol: None,
                tick: None,
                multiplier: None,
                bar_count: None,
                signals: None,
            },
        }
    }

    fn handle_inner(&mut self, req: WorkerRequest) -> Result<WorkerResponse> {
        match req {
            WorkerRequest::Ping => Ok(WorkerResponse {
                ok: true,
                error: None,
                symbol: None,
                tick: None,
                multiplier: None,
                bar_count: None,
                signals: None,
            }),
            WorkerRequest::InitSymbol { symbol, tick, bars } => {
                let eff_tick = effective_tick(tick, &symbol, "");
                let eff_mult = effective_multiplier(&symbol, "");
                let mut parsed = Vec::with_capacity(bars.len());
                for b in &bars {
                    parsed.push(b.to_bar()?);
                }
                let bar_count = parsed.len();
                self.symbols.insert(
                    symbol.clone(),
                    SymbolState {
                        symbol: symbol.clone(),
                        tick: eff_tick,
                        multiplier: eff_mult,
                        bars: parsed,
                    },
                );
                Ok(WorkerResponse {
                    ok: true,
                    error: None,
                    symbol: Some(symbol),
                    tick: Some(eff_tick),
                    multiplier: Some(eff_mult),
                    bar_count: Some(bar_count),
                    signals: None,
                })
            }
            WorkerRequest::StepBar { symbol, bar } => {
                let state = self
                    .symbols
                    .get_mut(&symbol)
                    .ok_or_else(|| anyhow!("symbol not initialized: {symbol}"))?;
                let next_bar = bar.to_bar()?;
                if let Some(last) = state.bars.last_mut() {
                    if next_bar.dt < last.dt {
                        return Err(anyhow!(
                            "out-of-order bar for {symbol}: {} < {}",
                            next_bar.dt.to_bar_ts(),
                            last.dt.to_bar_ts()
                        ));
                    }
                    if next_bar.dt == last.dt {
                        *last = next_bar;
                        return Ok(WorkerResponse {
                            ok: true,
                            error: None,
                            symbol: Some(symbol),
                            tick: Some(state.tick),
                            multiplier: Some(state.multiplier),
                            bar_count: Some(state.bars.len()),
                            signals: Some(vec![]),
                        });
                    }
                }
                state.bars.push(next_bar);
                let bar_count = state.bars.len();
                let signals = if bar_count < ATR_PERIOD + 2 {
                    vec![]
                } else {
                    let last_idx = bar_count - 1;
                    selected_warnings(&state.symbol, &state.bars, state.tick, false, last_idx)
                        .into_iter()
                        .map(|c| candidate_to_dto(&state.symbol, &state.bars, &c))
                        .collect()
                };
                Ok(WorkerResponse {
                    ok: true,
                    error: None,
                    symbol: Some(symbol),
                    tick: Some(state.tick),
                    multiplier: Some(state.multiplier),
                    bar_count: Some(bar_count),
                    signals: Some(signals),
                })
            }
        }
    }
}

fn candidate_to_dto(symbol: &str, bars: &[Bar], c: &WarningCandidate) -> SignalDto {
    let is_long = c.direction == Dir::Up;
    let s0_bar = &bars[c.s0_index];
    let s1_bar = &bars[c.s1_index];
    let s2_bar = &bars[c.s2_index];
    let w_bar = &bars[c.warning_index];

    SignalDto {
        symbol: symbol.to_string(),
        direction: if is_long { "up" } else { "down" }.to_string(),
        grade: c.grade.clone(),
        level: c.level.to_string(),
        warning_kind: c.warning_kind.to_string(),
        entry_score: c.entry_score,
        dim_a: c.dim_a,
        dim_b: c.dim_b,
        dim_warning: c.dim_warning,
        entry: c.entry,
        stop: c.stop,
        target: c.target,
        risk: c.risk,
        rr: c.rr,
        trend_state: c.trend_state.clone(),
        warning_index: c.warning_index,
        warning_ts: w_bar.dt.to_bar_ts(),
        s0_ts: s0_bar.dt.to_bar_ts(),
        s1_ts: s1_bar.dt.to_bar_ts(),
        s2_ts: s2_bar.dt.to_bar_ts(),
        s0_price: if is_long { s0_bar.low } else { s0_bar.high },
        s1_price: if is_long { s1_bar.high } else { s1_bar.low },
        s2_price: if is_long { s2_bar.low } else { s2_bar.high },
    }
}

fn main() -> Result<()> {
    let stdin = io::stdin();
    let stdout = io::stdout();
    let mut out = io::BufWriter::new(stdout.lock());
    let mut worker = WorkerState::new();

    for line in stdin.lock().lines() {
        let line = line?;
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        let resp = match serde_json::from_str::<WorkerRequest>(trimmed) {
            Ok(req) => worker.handle(req),
            Err(err) => WorkerResponse {
                ok: false,
                error: Some(format!("invalid request json: {err}")),
                symbol: None,
                tick: None,
                multiplier: None,
                bar_count: None,
                signals: None,
            },
        };
        serde_json::to_writer(&mut out, &resp)?;
        out.write_all(b"\n")?;
        out.flush()?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use n_core::analyze::event::replay_warnings;

    fn make_bar_input(idx: usize, o: f64, h: f64, l: f64, c: f64) -> BarInput {
        let base = DT {
            year: 2026,
            month: 9,
            day: 1,
            hour: 9,
            minute: 15,
        }
        .add_minutes((idx * 15) as i32);
        BarInput {
            datetime: base.to_bar_ts(),
            open: o,
            high: h,
            low: l,
            close: c,
            volume: 100.0,
            hold: 1000.0,
            rollover: false,
        }
    }

    #[test]
    fn incremental_step_bar_matches_batch_replay_warnings() {
        let mut raw = Vec::new();
        // Flat warmup
        for i in 0..25 {
            let base = 5000.0 + (i % 2) as f64;
            raw.push(make_bar_input(i, base, base + 4.0, base - 4.0, base + 1.0));
        }
        // Strong up leg (A)
        raw.push(make_bar_input(25, 5002.0, 5025.0, 5001.0, 5024.0));
        raw.push(make_bar_input(26, 5024.0, 5055.0, 5023.0, 5053.0));
        raw.push(make_bar_input(27, 5053.0, 5082.0, 5050.0, 5080.0));
        // Pullback leg (B) ~45% retracement
        raw.push(make_bar_input(28, 5080.0, 5081.0, 5058.0, 5060.0));
        raw.push(make_bar_input(29, 5060.0, 5062.0, 5042.0, 5045.0));
        // Strong reversal candle (warning)
        raw.push(make_bar_input(30, 5045.0, 5074.0, 5044.0, 5072.0));

        let bars: Vec<Bar> = raw.iter().map(|b| b.to_bar().unwrap()).collect();
        let expected: Vec<SignalDto> = replay_warnings("EG0", &bars, 1.0)
            .into_iter()
            .map(|c| candidate_to_dto("EG0", &bars, &c))
            .collect();

        let mut worker = WorkerState::new();
        let init_resp = worker.handle(WorkerRequest::InitSymbol {
            symbol: "EG0".into(),
            tick: 1.0,
            bars: raw[..22].to_vec(),
        });
        assert!(init_resp.ok);
        assert_eq!(init_resp.multiplier, Some(10.0));

        let mut actual = Vec::new();
        for b in &raw[22..] {
            let step_resp = worker.handle(WorkerRequest::StepBar {
                symbol: "EG0".into(),
                bar: b.clone(),
            });
            assert!(step_resp.ok);
            actual.extend(step_resp.signals.unwrap_or_default());
        }

        assert!(!expected.is_empty(), "fixture should produce at least 1 N-pattern signal");
        assert_eq!(actual, expected);
    }
}
