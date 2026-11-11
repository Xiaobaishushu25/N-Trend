#!/usr/bin/env python3
"""
TQSDK + n-core N-pattern strategy backtest runner & analytics engine.

Architecture:
1. Signal recognition runs in local Rust `n-signal-worker` (`crates/n-core`) on
   15-minute completed bars so N-pattern structural recognition & scoring match
   the live system 100%.
2. Order execution, slippage/spread simulation, margin, and commission run in
   TQSDK `TqBacktest` + `TqSim` at 1-minute quote granularity (O/H/L/C slices).
3. Continuous contracts (`KQ.m@...`) support Rollover-Hold: on main-contract
   rollover, untriggered Pending conditions on the old contract are cancelled,
   while open positions on the old contract remain alive until normal exit.
4. Single-trade RiskGate enforces `min_risk_ticks` and optional
   `max_risk_yuan` / `max_risk_pct` (`downsize` 2->1 or `reject`).
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from typing import Any, Optional

# Allow importing sibling helpers whether invoked from repo root or python-bridge/
_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from tq_bridge import SymbolMapper, VARIETY_NAMES  # noqa: E402


# ===== Strategy constants matching crates/n-core =====
PENDING_BARS = 12
DEDUP_WARNING_BARS = 5
DEDUP_ENTRY_R = 0.3
NO_FOLLOW_BAR = 5
NO_FOLLOW_MFE_R = 0.5
TIME_HORIZON_BARS = 60
TRAIL_ACTIVATE_R = 1.6
TRAIL_DISTANCE_R = 0.8

DEFAULT_MIN_SCORE = 2.5
DEFAULT_LARGE_SCORE = 3.5
DEFAULT_MIN_RISK_TICKS = 10
DEFAULT_BAR_SECONDS = 900  # 15m

_SHFE_INE = {"SHFE", "INE"}
_BJ_OFFSET_SECS = 8 * 3600


# ===== Data models =====
@dataclass
class BacktestConfig:
    symbols: list[str]
    start: date | datetime
    end: date | datetime
    init_balance: float = 1_000_000.0
    warmup_bars: int = 200
    min_score: float = DEFAULT_MIN_SCORE
    large_score: float = DEFAULT_LARGE_SCORE
    min_risk_ticks: int = DEFAULT_MIN_RISK_TICKS
    max_risk_yuan: Optional[float] = None
    max_risk_pct: Optional[float] = None
    risk_action: str = "downsize"  # "downsize" | "reject"
    slippage_ticks: int = 0
    allow_rollover_overlap: bool = False
    bar_seconds: int = DEFAULT_BAR_SECONDS
    worker_path: Optional[str] = None
    web_gui: bool | str = False
    export_dir: Optional[str] = None
    quiet: bool = False


@dataclass
class QuoteTick:
    symbol: str
    datetime: str
    last_price: float
    ask_price1: float = float("nan")
    bid_price1: float = float("nan")
    upper_limit: float = float("nan")
    lower_limit: float = float("nan")
    price_tick: float = 1.0
    volume_multiple: float = 1.0
    underlying_symbol: str = ""


@dataclass
class RiskGateDecision:
    allowed: bool
    lots: int
    downsized: bool
    reason: str
    unit_risk_yuan: float
    total_risk_yuan: float
    effective_cap_yuan: Optional[float]


@dataclass
class OrderInstruction:
    intent: str  # "open" | "close"
    ref_id: str  # cond_id for open, pos_id for close
    logical_symbol: str
    actual_symbol: str
    direction: str  # "BUY" | "SELL"
    volume: int
    reason: str
    trigger_price: float
    is_lot1: bool = False
    closing_all: bool = False


@dataclass
class ConditionOrder:
    cond_id: str
    logical_symbol: str
    actual_symbol: str
    direction: str  # "up" | "down"
    grade: str
    entry_score: float
    entry: float
    stop: float
    target: float
    risk: float
    rr: float
    warning_index: int
    warning_ts: str
    created_ts: str
    lots: int
    tier: str  # "large" | "small"
    tick: float
    multiplier: float
    bars_waited: int = 0
    state: str = "pending"  # pending | submitting | triggered | expired | cancelled | rejected
    reject_reason: str = ""

    @property
    def is_active(self) -> bool:
        return self.state in ("pending", "submitting", "triggered")


@dataclass
class OpenPosition:
    pos_id: str
    cond_id: str
    logical_symbol: str
    actual_symbol: str
    direction: str  # "up" | "down"
    grade: str
    entry_score: float
    tier: str
    total_lots: int
    remaining_lots: int
    open_fill_price: float
    open_ts: str
    planned_entry: float
    planned_risk: float
    initial_stop: float
    current_stop: float
    target: float
    tick: float
    multiplier: float
    bars_held: int = 0
    max_mfe_r: float = 0.0
    lot1_exit_sent: bool = False
    lot1_closed: bool = False
    closing_all_sent: bool = False
    open_order_ids: list[str] = field(default_factory=list)
    open_commission: float = 0.0
    exit_legs: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class RiskGateEvent:
    ts: str
    logical_symbol: str
    actual_symbol: str
    cond_id: str
    direction: str
    score: float
    action: str  # "downsized" | "rejected"
    planned_lots: int
    final_lots: int
    unit_risk_yuan: float
    total_risk_yuan: float
    cap_yuan: Optional[float]
    reason: str


# ===== Pure math & formatting helpers =====
def round_to_tick(price: float, tick: float) -> float:
    if not math.isfinite(price) or tick <= 0:
        return price
    steps = round(price / tick)
    rounded = steps * tick
    # Avoid floating point tail artifacts like 3450.0000000000005
    decimals = 0
    tick_str = f"{tick:.10f}".rstrip("0").rstrip(".")
    if "." in tick_str:
        decimals = len(tick_str.split(".", 1)[1])
    return round(rounded, decimals)


def tq_bar_end_datetime(bar_start_ns: int, duration_secs: int = DEFAULT_BAR_SECONDS) -> str:
    """Convert TQSDK bar start (ns UTC) into Beijing bar END `YYYY-MM-DD HH:MM:SS`."""
    ts_sec = int(bar_start_ns // 1_000_000_000)
    dt = datetime.fromtimestamp(ts_sec + duration_secs + _BJ_OFFSET_SECS, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def format_tq_nano_bj(ts_ns: int) -> str:
    """Convert TQSDK timestamp in ns UTC (or s UTC if small) into Beijing `YYYY-MM-DD HH:MM:SS`."""
    ts_sec = int(ts_ns // 1_000_000_000) if abs(ts_ns) > 1_000_000_000_000 else int(ts_ns)
    dt = datetime.fromtimestamp(ts_sec + _BJ_OFFSET_SECS, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def continuous_stop_price(
    fill_price: float,
    risk: float,
    sign: float,
    max_mfe_r: float,
    current_stop: float,
) -> float:
    """Exact port of `crates/n-core/src/trader/trailing.rs::continuous_stop_price`."""
    lock_r = (max_mfe_r - TRAIL_DISTANCE_R) if (max_mfe_r + 1e-9 >= TRAIL_ACTIVATE_R) else 0.0
    candidate = fill_price + sign * lock_r * risk
    return max(current_stop, candidate) if sign > 0.0 else min(current_stop, candidate)


def compute_close_offsets(
    exchange_id: str,
    total_close_lots: int,
    avail_his: int,
    avail_today: int,
) -> list[tuple[str, int]]:
    """
    Split close volume into `(offset, lots)` legs respecting SHFE/INE CLOSE vs CLOSETODAY.
    For non-SHFE/INE exchanges, always uses a single `CLOSE`.
    """
    if total_close_lots <= 0:
        return []
    ex = exchange_id.strip().upper()
    if ex not in _SHFE_INE:
        return [("CLOSE", total_close_lots)]

    remaining = total_close_lots
    legs: list[tuple[str, int]] = []
    take_his = min(max(0, int(avail_his)), remaining)
    if take_his > 0:
        legs.append(("CLOSE", take_his))
        remaining -= take_his
    take_today = min(max(0, int(avail_today)), remaining)
    if take_today > 0:
        legs.append(("CLOSETODAY", take_today))
        remaining -= take_today
    if remaining > 0:
        # Fallback if frozen state obscured available today vs his counts
        legs.append(("CLOSE" if avail_his >= avail_today else "CLOSETODAY", remaining))
    return legs


def compute_slippage_limit_price(
    direction: str,
    quote: QuoteTick,
    slippage_ticks: int,
) -> Optional[float]:
    """
    Return order `limit_price`:
    - `None` (market / ANY order) when `slippage_ticks <= 0` so TqSim fills at opponent price.
    - Crossed limit price when `slippage_ticks > 0` (opponent price +- slippage_ticks * tick),
      clamped within upper/lower limits when valid.
    """
    if slippage_ticks <= 0:
        return None
    tick = quote.price_tick if quote.price_tick > 0 else 1.0
    if direction == "BUY":
        opp = quote.ask_price1 if math.isfinite(quote.ask_price1) and quote.ask_price1 > 0 else quote.last_price
        raw = opp + slippage_ticks * tick
        if math.isfinite(quote.upper_limit) and quote.upper_limit > 0:
            raw = min(raw, quote.upper_limit)
    else:
        opp = quote.bid_price1 if math.isfinite(quote.bid_price1) and quote.bid_price1 > 0 else quote.last_price
        raw = opp - slippage_ticks * tick
        if math.isfinite(quote.lower_limit) and quote.lower_limit > 0:
            raw = max(raw, quote.lower_limit)
    return round_to_tick(raw, tick)


def symbol_display_name(logical_symbol: str) -> str:
    _, variety, _ = SymbolMapper.parse_code(logical_symbol)
    cn = VARIETY_NAMES.get(variety.upper(), "")
    return f"{logical_symbol} ({cn})" if cn else logical_symbol


# ===== Single-trade risk control =====
class RiskGate:
    """Enforces min_risk_ticks and optional max_risk_yuan / max_risk_pct per signal."""

    @staticmethod
    def evaluate(
        entry_score: float,
        risk: float,
        tick: float,
        multiplier: float,
        cfg: BacktestConfig,
        current_equity: float,
    ) -> RiskGateDecision:
        if tick <= 0 or risk <= 0 or (risk / tick) + 1e-9 < cfg.min_risk_ticks:
            unit = max(0.0, risk * multiplier)
            return RiskGateDecision(
                allowed=False,
                lots=0,
                downsized=False,
                reason=f"risk_below_{cfg.min_risk_ticks}_ticks",
                unit_risk_yuan=unit,
                total_risk_yuan=unit,
                effective_cap_yuan=None,
            )

        base_lots = 2 if entry_score + 1e-9 >= cfg.large_score else 1
        unit_risk = risk * multiplier
        total_risk = unit_risk * base_lots

        caps: list[float] = []
        if cfg.max_risk_yuan is not None and cfg.max_risk_yuan > 0:
            caps.append(float(cfg.max_risk_yuan))
        if cfg.max_risk_pct is not None and cfg.max_risk_pct > 0 and current_equity > 0:
            caps.append(float(current_equity) * float(cfg.max_risk_pct))
        cap = min(caps) if caps else None

        if cap is not None and total_risk > cap + 1e-9:
            if base_lots == 2 and cfg.risk_action == "downsize" and unit_risk <= cap + 1e-9:
                return RiskGateDecision(
                    allowed=True,
                    lots=1,
                    downsized=True,
                    reason="downsized_2_to_1_max_risk",
                    unit_risk_yuan=unit_risk,
                    total_risk_yuan=unit_risk,
                    effective_cap_yuan=cap,
                )
            return RiskGateDecision(
                allowed=False,
                lots=0,
                downsized=False,
                reason="exceeds_max_risk_cap",
                unit_risk_yuan=unit_risk,
                total_risk_yuan=total_risk,
                effective_cap_yuan=cap,
            )

        return RiskGateDecision(
            allowed=True,
            lots=base_lots,
            downsized=False,
            reason="ok",
            unit_risk_yuan=unit_risk,
            total_risk_yuan=total_risk,
            effective_cap_yuan=cap,
        )


# ===== Local Rust n-signal-worker client =====
def resolve_worker_binary(explicit_path: Optional[str] = None) -> Path:
    candidates: list[Path] = []
    if explicit_path:
        candidates.append(Path(explicit_path))
    env_bin = os.environ.get("N_SIGNAL_WORKER_BIN")
    if env_bin:
        candidates.append(Path(env_bin))
    exe_name = "n-signal-worker.exe" if os.name == "nt" else "n-signal-worker"
    candidates.append(_REPO_ROOT / "target" / "release" / exe_name)
    candidates.append(_REPO_ROOT / "target" / "debug" / exe_name)

    for cand in candidates:
        if cand.is_file():
            return cand
    searched = "\n  - ".join(str(p) for p in candidates)
    raise FileNotFoundError(
        f"n-signal-worker binary not found. Searched:\n  - {searched}\n"
        f"Please build it first with:\n  cargo build --release -p n-core --bin n-signal-worker"
    )


class SignalWorkerClient:
    """Line-delimited JSON IPC client for Rust `n-signal-worker`."""

    def __init__(self, worker_path: Optional[str] = None):
        self.binary_path = resolve_worker_binary(worker_path)
        self._proc: Optional[subprocess.Popen[str]] = None

    def start(self) -> None:
        if self._proc is not None:
            return
        self._proc = subprocess.Popen(
            [str(self.binary_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self.ping()

    def close(self) -> None:
        if self._proc is None:
            return
        proc = self._proc
        self._proc = None
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream and not stream.closed:
                    stream.close()
            except Exception:
                pass

    def __enter__(self) -> "SignalWorkerClient":
        self.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._proc is None:
            self.start()
        assert self._proc is not None and self._proc.stdin is not None and self._proc.stdout is not None
        line = json.dumps(payload, ensure_ascii=False)
        self._proc.stdin.write(line + "\n")
        self._proc.stdin.flush()
        resp_line = self._proc.stdout.readline()
        if not resp_line:
            stderr_tail = ""
            if self._proc.stderr:
                stderr_tail = self._proc.stderr.read()
            raise RuntimeError(
                f"n-signal-worker exited unexpectedly (code={self._proc.poll()}): {stderr_tail}"
            )
        resp = json.loads(resp_line)
        if not resp.get("ok"):
            raise RuntimeError(f"n-signal-worker error: {resp.get('error', resp)}")
        return resp

    def ping(self) -> None:
        self._request({"cmd": "ping"})

    def init_symbol(self, symbol: str, tick: float, bars: list[dict[str, Any]]) -> dict[str, Any]:
        return self._request({
            "cmd": "init_symbol",
            "symbol": symbol,
            "tick": float(tick),
            "bars": bars,
        })

    def step_bar(self, symbol: str, bar: dict[str, Any]) -> list[dict[str, Any]]:
        resp = self._request({
            "cmd": "step_bar",
            "symbol": symbol,
            "bar": bar,
        })
        return resp.get("signals", [])


# ===== Analytics Collector =====
class AccountAnalytics:
    """
    Collects trade legs, grouped condition-order metrics, risk-gate interceptions,
    per-symbol breakdown, and equity curve points; reconciles exact commissions
    from TqSim `trade_log`.
    """

    def __init__(self, init_balance: float):
        self.init_balance = float(init_balance)
        self.peak_equity = float(init_balance)
        self.max_drawdown_yuan = 0.0
        self.max_drawdown_pct = 0.0
        self.equity_points: list[tuple[str, float, float]] = []  # (ts, equity, dd_pct)
        self.exit_legs: list[dict[str, Any]] = []
        self.risk_events: list[RiskGateEvent] = []
        self.symbol_counters: dict[str, dict[str, int]] = {}
        self.open_positions_at_end: list[dict[str, Any]] = []
        self.broker_rejections: list[dict[str, Any]] = []
        self.tqsdk_stat: dict[str, Any] = {}
        self.final_equity: float = float(init_balance)
        self.account_commission: float = 0.0

    def _sym_counter(self, logical_symbol: str) -> dict[str, int]:
        if logical_symbol not in self.symbol_counters:
            self.symbol_counters[logical_symbol] = {
                "signals_seen": 0,
                "cond_created": 0,
                "cond_expired": 0,
                "cond_cancelled_rollover": 0,
                "cond_rejected_entry": 0,
                "dedup_skipped": 0,
                "slot_blocked_skipped": 0,
                "risk_downsized": 0,
                "risk_rejected": 0,
            }
        return self.symbol_counters[logical_symbol]

    def inc_counter(self, logical_symbol: str, field_name: str, delta: int = 1) -> None:
        c = self._sym_counter(logical_symbol)
        c[field_name] = c.get(field_name, 0) + delta

    def record_risk_event(self, ev: RiskGateEvent) -> None:
        self.risk_events.append(ev)
        if ev.action == "downsized":
            self.inc_counter(ev.logical_symbol, "risk_downsized")
        elif ev.action == "rejected":
            self.inc_counter(ev.logical_symbol, "risk_rejected")

    def record_equity(self, ts: str, equity: float) -> None:
        if not math.isfinite(equity):
            return
        if equity > self.peak_equity:
            self.peak_equity = equity
        dd_yuan = max(0.0, self.peak_equity - equity)
        dd_pct = (dd_yuan / self.peak_equity) if self.peak_equity > 0 else 0.0
        if dd_yuan > self.max_drawdown_yuan:
            self.max_drawdown_yuan = dd_yuan
        if dd_pct > self.max_drawdown_pct:
            self.max_drawdown_pct = dd_pct
        if not self.equity_points or self.equity_points[-1][0] != ts or abs(self.equity_points[-1][1] - equity) > 1e-3:
            self.equity_points.append((ts, equity, dd_pct))

    def record_exit_leg(self, leg: dict[str, Any]) -> None:
        self.exit_legs.append(leg)

    def reconcile_commissions(
        self,
        positions_by_id: dict[str, OpenPosition],
        comm_by_order: dict[str, float],
    ) -> None:
        """
        Allocate exact TqSim order commissions onto open_commission and every ExitLeg,
        recomputing net_pnl cleanly.
        """
        for pos in positions_by_id.values():
            if pos.open_order_ids:
                exact_open = sum(comm_by_order.get(oid, 0.0) for oid in pos.open_order_ids)
                if exact_open > 0 or any(oid in comm_by_order for oid in pos.open_order_ids):
                    pos.open_commission = exact_open

        for leg in self.exit_legs:
            pos = positions_by_id.get(leg["pos_id"])
            open_comm_share = 0.0
            if pos and pos.total_lots > 0:
                open_comm_share = pos.open_commission * (leg["lots"] / pos.total_lots)
            close_oids = leg.get("close_order_ids", [])
            close_comm = leg.get("close_commission", 0.0)
            if close_oids and any(oid in comm_by_order for oid in close_oids):
                close_comm = sum(comm_by_order.get(oid, 0.0) for oid in close_oids)
                leg["close_commission"] = close_comm
            leg["open_commission_share"] = open_comm_share
            leg["commission"] = open_comm_share + close_comm
            leg["net_pnl"] = leg["gross_pnl"] - leg["commission"]

    def grouped_trades(self) -> list[dict[str, Any]]:
        """
        Aggregate exit legs by `cond_id` (matching `n-core` instances/execution stats),
        preserving individual leg reasons and lot-weighted R multiple.
        """
        by_cond: dict[str, list[dict[str, Any]]] = {}
        for leg in self.exit_legs:
            by_cond.setdefault(leg["cond_id"], []).append(leg)

        trades: list[dict[str, Any]] = []
        for cond_id, legs in by_cond.items():
            first = legs[0]
            last = legs[-1]
            total_lots = sum(int(l["lots"]) for l in legs)
            gross_pnl = sum(float(l["gross_pnl"]) for l in legs)
            commission = sum(float(l["commission"]) for l in legs)
            net_pnl = sum(float(l["net_pnl"]) for l in legs)
            weighted_r = (
                sum(float(l["r_multiple"]) * int(l["lots"]) for l in legs) / total_lots
                if total_lots > 0
                else 0.0
            )
            reasons = "+".join(l["exit_reason"] for l in legs)
            trades.append({
                "cond_id": cond_id,
                "logical_symbol": first["logical_symbol"],
                "actual_symbol": first["actual_symbol"],
                "direction": first["direction"],
                "grade": first["grade"],
                "entry_score": first["entry_score"],
                "tier": first["tier"],
                "lots": total_lots,
                "open_ts": first["open_ts"],
                "close_ts": last["close_ts"],
                "open_price": first["open_price"],
                "close_price_last": last["close_price"],
                "planned_risk": first["planned_risk"],
                "r_multiple": weighted_r,
                "gross_pnl": gross_pnl,
                "commission": commission,
                "net_pnl": net_pnl,
                "exit_reasons": reasons,
                "bars_held": last["bars_held"],
                "max_mfe_r": max(float(l["max_mfe_r"]) for l in legs),
                "leg_count": len(legs),
            })
        trades.sort(key=lambda t: (t["open_ts"], t["cond_id"]))
        return trades

    @staticmethod
    def _compute_group_metrics(trades: list[dict[str, Any]]) -> dict[str, Any]:
        n = len(trades)
        if n == 0:
            return {
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "flats": 0,
                "win_rate": 0.0,
                "gross_pnl": 0.0,
                "commission": 0.0,
                "net_pnl": 0.0,
                "profit_factor": 0.0,
                "avg_net_pnl": 0.0,
                "avg_r": 0.0,
                "avg_win_yuan": 0.0,
                "avg_loss_yuan": 0.0,
                "payoff_ratio": 0.0,
                "max_win_yuan": 0.0,
                "max_loss_yuan": 0.0,
            }
        wins = [t for t in trades if t["net_pnl"] > 1e-6]
        losses = [t for t in trades if t["net_pnl"] < -1e-6]
        flats = n - len(wins) - len(losses)
        gross_win = sum(t["net_pnl"] for t in wins)
        gross_loss = abs(sum(t["net_pnl"] for t in losses))
        pf = (gross_win / gross_loss) if gross_loss > 0 else (math.inf if gross_win > 0 else 0.0)
        avg_win = (gross_win / len(wins)) if wins else 0.0
        avg_loss = (gross_loss / len(losses)) if losses else 0.0
        payoff = (avg_win / avg_loss) if avg_loss > 0 else (math.inf if avg_win > 0 else 0.0)
        return {
            "trades": n,
            "wins": len(wins),
            "losses": len(losses),
            "flats": flats,
            "win_rate": len(wins) / n,
            "gross_pnl": sum(t["gross_pnl"] for t in trades),
            "commission": sum(t["commission"] for t in trades),
            "net_pnl": sum(t["net_pnl"] for t in trades),
            "profit_factor": pf,
            "avg_net_pnl": sum(t["net_pnl"] for t in trades) / n,
            "avg_r": sum(t["r_multiple"] for t in trades) / n,
            "avg_win_yuan": avg_win,
            "avg_loss_yuan": -avg_loss if avg_loss > 0 else 0.0,
            "payoff_ratio": payoff,
            "max_win_yuan": max((t["net_pnl"] for t in trades), default=0.0),
            "max_loss_yuan": min((t["net_pnl"] for t in trades), default=0.0),
        }

    def build_report(self, all_symbols: list[str]) -> dict[str, Any]:
        trades = self.grouped_trades()
        overall = self._compute_group_metrics(trades)

        by_symbol: dict[str, dict[str, Any]] = {}
        seen_syms = list(dict.fromkeys(all_symbols + list(self.symbol_counters.keys()) + [t["logical_symbol"] for t in trades]))
        for sym in seen_syms:
            sym_trades = [t for t in trades if t["logical_symbol"] == sym]
            sym_legs = [l for l in self.exit_legs if l["logical_symbol"] == sym]
            long_trades = [t for t in sym_trades if t["direction"] == "up"]
            short_trades = [t for t in sym_trades if t["direction"] == "down"]
            reason_counts: dict[str, int] = {}
            for leg in sym_legs:
                reason_counts[leg["exit_reason"]] = reason_counts.get(leg["exit_reason"], 0) + 1
            entry = {
                "symbol": sym,
                "display_name": symbol_display_name(sym),
                **self._compute_group_metrics(sym_trades),
                "leg_count": len(sym_legs),
                "long": self._compute_group_metrics(long_trades),
                "short": self._compute_group_metrics(short_trades),
                "counters": dict(self._sym_counter(sym)),
                "exit_reasons": reason_counts,
            }
            by_symbol[sym] = entry

        overall_reasons: dict[str, int] = {}
        for leg in self.exit_legs:
            overall_reasons[leg["exit_reason"]] = overall_reasons.get(leg["exit_reason"], 0) + 1

        open_unrealized = sum(float(p.get("unrealized_pnl", 0.0)) for p in self.open_positions_at_end)
        open_comm = sum(float(p.get("open_commission", 0.0)) for p in self.open_positions_at_end)
        total_ret_yuan = self.final_equity - self.init_balance
        total_ret_pct = (total_ret_yuan / self.init_balance) if self.init_balance > 0 else 0.0

        return {
            "account": {
                "init_balance": self.init_balance,
                "final_equity": self.final_equity,
                "total_return_yuan": total_ret_yuan,
                "total_return_pct": total_ret_pct,
                "max_drawdown_yuan": self.max_drawdown_yuan,
                "max_drawdown_pct": self.max_drawdown_pct,
                "realized_gross_pnl": overall["gross_pnl"],
                "realized_commission": overall["commission"],
                "realized_net_pnl": overall["net_pnl"],
                "account_commission_reported": self.account_commission,
                "open_positions_count": len(self.open_positions_at_end),
                "open_unrealized_pnl": open_unrealized,
                "open_commission": open_comm,
                "reconciled_strategy_net_yuan": overall["net_pnl"] + open_unrealized - open_comm,
                "tqsdk_stat": dict(self.tqsdk_stat),
            },
            "overall": overall,
            "exit_reasons": overall_reasons,
            "risk_events_count": len(self.risk_events),
            "broker_rejections_count": len(self.broker_rejections),
            "broker_rejections": list(self.broker_rejections),
            "by_symbol": by_symbol,
            "trades": trades,
            "legs": list(self.exit_legs),
            "open_positions_at_end": list(self.open_positions_at_end),
            "risk_events": [dataclasses.asdict(e) for e in self.risk_events],
        }


# ===== Core Decoupled Strategy + Execution Engine =====
class NPatternBacktestEngine:
    """
    Pure, TQSDK-agnostic strategy & execution state machine.

    Emits `OrderInstruction` objects when quote prices trigger a pending condition
    order or hit an active position's exit rule (`stop`, `target_1r`, `trailing`,
    `breakeven`, `target_s1`, `no_follow`, `time_exit`).
    """

    def __init__(self, cfg: BacktestConfig, worker: Optional[SignalWorkerClient] = None):
        self.cfg = cfg
        self.worker = worker
        self.analytics = AccountAnalytics(cfg.init_balance)
        self.conditions: dict[str, ConditionOrder] = {}
        self.recent_conditions: dict[str, list[ConditionOrder]] = {}
        self.positions: dict[str, OpenPosition] = {}
        self.active_positions: dict[str, OpenPosition] = {}
        self._cond_seq = 0
        self._pos_seq = 0

    def _next_cond_id(self, logical_symbol: str) -> str:
        self._cond_seq += 1
        return f"C{self._cond_seq}_{logical_symbol}"

    def _next_pos_id(self, logical_symbol: str) -> str:
        self._pos_seq += 1
        return f"P{self._pos_seq}_{logical_symbol}"

    def has_active_slot(self, logical_symbol: str) -> bool:
        if any(p.logical_symbol == logical_symbol for p in self.active_positions.values()):
            return True
        if any(
            c.logical_symbol == logical_symbol and c.state in ("submitting", "triggered")
            for c in self.conditions.values()
        ):
            return True
        return False

    def _is_duplicate_signal(self, logical_symbol: str, sig: dict[str, Any]) -> bool:
        w_idx = int(sig["warning_index"])
        entry = float(sig["entry"])
        for prev in self.recent_conditions.get(logical_symbol, []):
            if prev.direction != sig["direction"]:
                continue
            if abs(w_idx - prev.warning_index) <= DEDUP_WARNING_BARS:
                if prev.is_active or abs(entry - prev.entry) <= DEDUP_ENTRY_R * max(prev.risk, 1e-9):
                    return True
        return False

    def cancel_pending_for_rollover(self, logical_symbol: str, reason: str = "rollover") -> list[ConditionOrder]:
        """Cancel untriggered Pending conditions on rollover while keeping OpenPositions alive."""
        cancelled: list[ConditionOrder] = []
        for cond in self.conditions.values():
            if cond.logical_symbol == logical_symbol and cond.state == "pending":
                cond.state = "cancelled"
                cond.reject_reason = reason
                self.analytics.inc_counter(logical_symbol, "cond_cancelled_rollover")
                cancelled.append(cond)
        return cancelled

    def step_bar_timers(self, logical_symbol: str, bar_ts: str, current_price: float) -> list[OrderInstruction]:
        """
        Advance 15m bar counters for pending conditions (`PENDING_BARS`) and active
        positions (`NO_FOLLOW_BAR` / `TIME_HORIZON_BARS`).
        """
        instructions: list[OrderInstruction] = []

        for cond in self.conditions.values():
            if cond.logical_symbol == logical_symbol and cond.state == "pending":
                cond.bars_waited += 1
                if cond.bars_waited >= PENDING_BARS:
                    cond.state = "expired"
                    cond.reject_reason = "pending_bars_expired"
                    self.analytics.inc_counter(logical_symbol, "cond_expired")

        for pos in list(self.active_positions.values()):
            if pos.logical_symbol != logical_symbol:
                continue
            if pos.closing_all_sent or (pos.lot1_exit_sent and not pos.lot1_closed):
                continue
            pos.bars_held += 1
            reason: Optional[str] = None
            if pos.bars_held >= TIME_HORIZON_BARS:
                reason = "time_exit"
            elif pos.bars_held == NO_FOLLOW_BAR and pos.max_mfe_r < NO_FOLLOW_MFE_R:
                reason = "no_follow"
            if reason is not None:
                pos.closing_all_sent = True
                instructions.append(OrderInstruction(
                    intent="close",
                    ref_id=pos.pos_id,
                    logical_symbol=pos.logical_symbol,
                    actual_symbol=pos.actual_symbol,
                    direction="SELL" if pos.direction == "up" else "BUY",
                    volume=pos.remaining_lots,
                    reason=reason,
                    trigger_price=current_price,
                    is_lot1=False,
                    closing_all=True,
                ))
        return instructions

    def ingest_signals(
        self,
        logical_symbol: str,
        bar_ts: str,
        actual_symbol: str,
        tick: float,
        multiplier: float,
        current_equity: float,
        signals: list[dict[str, Any]],
    ) -> list[ConditionOrder]:
        """Process signals emitted on closed 15m bar through dedup, exclusivity & RiskGate."""
        created: list[ConditionOrder] = []
        for sig in signals:
            score = float(sig.get("entry_score", 0.0))
            if score + 1e-9 < self.cfg.min_score:
                continue
            self.analytics.inc_counter(logical_symbol, "signals_seen")

            if (not self.cfg.allow_rollover_overlap) and self.has_active_slot(logical_symbol):
                self.analytics.inc_counter(logical_symbol, "slot_blocked_skipped")
                continue

            if self._is_duplicate_signal(logical_symbol, sig):
                self.analytics.inc_counter(logical_symbol, "dedup_skipped")
                continue

            direction = str(sig["direction"])
            entry = float(sig["entry"])
            stop = float(sig["stop"])
            risk = float(sig["risk"])
            sign = 1.0 if direction == "up" else -1.0
            target = float(sig["target"])
            if (target - entry) * sign <= risk:
                target = entry + sign * risk

            decision = RiskGate.evaluate(
                entry_score=score,
                risk=risk,
                tick=tick,
                multiplier=multiplier,
                cfg=self.cfg,
                current_equity=current_equity,
            )
            cond_id = self._next_cond_id(logical_symbol)
            planned_lots = 2 if score + 1e-9 >= self.cfg.large_score else 1

            if decision.downsized or not decision.allowed:
                self.analytics.record_risk_event(RiskGateEvent(
                    ts=bar_ts,
                    logical_symbol=logical_symbol,
                    actual_symbol=actual_symbol,
                    cond_id=cond_id,
                    direction=direction,
                    score=score,
                    action="rejected" if not decision.allowed else "downsized",
                    planned_lots=planned_lots,
                    final_lots=decision.lots,
                    unit_risk_yuan=decision.unit_risk_yuan,
                    total_risk_yuan=decision.total_risk_yuan,
                    cap_yuan=decision.effective_cap_yuan,
                    reason=decision.reason,
                ))

            if not decision.allowed:
                continue

            cond = ConditionOrder(
                cond_id=cond_id,
                logical_symbol=logical_symbol,
                actual_symbol=actual_symbol,
                direction=direction,
                grade=str(sig.get("grade", "")),
                entry_score=score,
                entry=entry,
                stop=stop,
                target=target,
                risk=risk,
                rr=float(sig.get("rr", 0.0)),
                warning_index=int(sig["warning_index"]),
                warning_ts=str(sig.get("warning_ts", bar_ts)),
                created_ts=bar_ts,
                lots=decision.lots,
                tier="large" if decision.lots >= 2 else "small",
                tick=tick,
                multiplier=multiplier,
            )
            self.conditions[cond_id] = cond
            buf = self.recent_conditions.setdefault(logical_symbol, [])
            buf.append(cond)
            if len(buf) > 30:
                del buf[:-30]
            self.analytics.inc_counter(logical_symbol, "cond_created")
            created.append(cond)
        return created

    def on_bar_closed(
        self,
        logical_symbol: str,
        bar: dict[str, Any],
        actual_symbol: str,
        tick: float,
        multiplier: float,
        current_equity: float,
    ) -> tuple[list[OrderInstruction], list[ConditionOrder]]:
        """
        Handle a newly completed 15m bar:
        1. Step timers for pending conditions and open positions (`no_follow` / `time_exit`).
        2. If `bar['rollover']`, cancel untriggered pending conditions for `logical_symbol`.
        3. Call Rust `n-signal-worker` `step_bar` and ingest new signals.
        """
        assert self.worker is not None, "worker client required for on_bar_closed"
        bar_ts = str(bar["datetime"])
        instructions = self.step_bar_timers(logical_symbol, bar_ts, float(bar["close"]))
        if bar.get("rollover"):
            self.cancel_pending_for_rollover(logical_symbol, reason="rollover")
        signals = self.worker.step_bar(logical_symbol, bar)
        created = self.ingest_signals(
            logical_symbol=logical_symbol,
            bar_ts=bar_ts,
            actual_symbol=actual_symbol,
            tick=tick,
            multiplier=multiplier,
            current_equity=current_equity,
            signals=signals,
        )
        return instructions, created

    def on_quote(self, quote: QuoteTick) -> list[OrderInstruction]:
        """Evaluate active positions and pending condition orders for `quote.symbol` (actual contract)."""
        price = quote.last_price
        if not math.isfinite(price) or price <= 0:
            return []

        instructions: list[OrderInstruction] = []

        # 1. Active positions exit checks (`crates/n-core/src/trader/policy.rs`)
        for pos in list(self.active_positions.values()):
            if pos.actual_symbol != quote.symbol:
                continue
            if pos.closing_all_sent or (pos.lot1_exit_sent and not pos.lot1_closed):
                continue

            sign = 1.0 if pos.direction == "up" else -1.0
            risk = max(pos.planned_risk, 1e-9)
            r = (price - pos.open_fill_price) * sign / risk
            if r > pos.max_mfe_r:
                pos.max_mfe_r = r

            if (price - pos.current_stop) * sign <= 0.0:
                if not pos.lot1_closed:
                    reason = "stop"
                elif (pos.current_stop - pos.open_fill_price) * sign > 0.0:
                    reason = "trailing"
                else:
                    reason = "breakeven"
                pos.closing_all_sent = True
                instructions.append(OrderInstruction(
                    intent="close",
                    ref_id=pos.pos_id,
                    logical_symbol=pos.logical_symbol,
                    actual_symbol=pos.actual_symbol,
                    direction="SELL" if pos.direction == "up" else "BUY",
                    volume=pos.remaining_lots,
                    reason=reason,
                    trigger_price=price,
                    is_lot1=False,
                    closing_all=True,
                ))
            elif (not pos.lot1_closed) and r >= 1.0:
                if pos.tier == "small" or pos.remaining_lots <= 1:
                    pos.closing_all_sent = True
                    instructions.append(OrderInstruction(
                        intent="close",
                        ref_id=pos.pos_id,
                        logical_symbol=pos.logical_symbol,
                        actual_symbol=pos.actual_symbol,
                        direction="SELL" if pos.direction == "up" else "BUY",
                        volume=pos.remaining_lots,
                        reason="target_1r",
                        trigger_price=price,
                        is_lot1=False,
                        closing_all=True,
                    ))
                else:
                    pos.lot1_exit_sent = True
                    instructions.append(OrderInstruction(
                        intent="close",
                        ref_id=pos.pos_id,
                        logical_symbol=pos.logical_symbol,
                        actual_symbol=pos.actual_symbol,
                        direction="SELL" if pos.direction == "up" else "BUY",
                        volume=1,
                        reason="target_1r",
                        trigger_price=price,
                        is_lot1=True,
                        closing_all=False,
                    ))
            elif pos.lot1_closed:
                pos.current_stop = continuous_stop_price(
                    pos.open_fill_price,
                    risk,
                    sign,
                    pos.max_mfe_r,
                    pos.current_stop,
                )
                if (price - pos.current_stop) * sign <= 0.0:
                    reason = "trailing" if (pos.current_stop - pos.open_fill_price) * sign > 0.0 else "breakeven"
                    pos.closing_all_sent = True
                    instructions.append(OrderInstruction(
                        intent="close",
                        ref_id=pos.pos_id,
                        logical_symbol=pos.logical_symbol,
                        actual_symbol=pos.actual_symbol,
                        direction="SELL" if pos.direction == "up" else "BUY",
                        volume=pos.remaining_lots,
                        reason=reason,
                        trigger_price=price,
                        is_lot1=False,
                        closing_all=True,
                    ))
                elif (pos.target - pos.open_fill_price) * sign > risk + 1e-6 and (price - pos.target) * sign >= 0.0:
                    pos.closing_all_sent = True
                    instructions.append(OrderInstruction(
                        intent="close",
                        ref_id=pos.pos_id,
                        logical_symbol=pos.logical_symbol,
                        actual_symbol=pos.actual_symbol,
                        direction="SELL" if pos.direction == "up" else "BUY",
                        volume=pos.remaining_lots,
                        reason="target_s1",
                        trigger_price=price,
                        is_lot1=False,
                        closing_all=True,
                    ))

        # 2. Pending condition orders trigger check (`crates/n-core/src/trader/execution.rs`)
        for cond in list(self.conditions.values()):
            if cond.actual_symbol != quote.symbol or cond.state != "pending":
                continue
            hit = (price >= cond.entry) if cond.direction == "up" else (price <= cond.entry)
            if not hit:
                continue
            if (not self.cfg.allow_rollover_overlap) and self.has_active_slot(cond.logical_symbol):
                cond.state = "cancelled"
                cond.reject_reason = "logical_slot_occupied"
                self.analytics.inc_counter(cond.logical_symbol, "slot_blocked_skipped")
                continue

            cond.state = "submitting"
            instructions.append(OrderInstruction(
                intent="open",
                ref_id=cond.cond_id,
                logical_symbol=cond.logical_symbol,
                actual_symbol=cond.actual_symbol,
                direction="BUY" if cond.direction == "up" else "SELL",
                volume=cond.lots,
                reason="entry_trigger",
                trigger_price=price,
            ))

        return instructions

    def on_open_filled(
        self,
        cond_id: str,
        filled_volume: int,
        fill_price: float,
        fill_ts: str,
        order_ids: Optional[list[str]] = None,
        commission: float = 0.0,
    ) -> Optional[OpenPosition]:
        cond = self.conditions.get(cond_id)
        if cond is None:
            return None
        if filled_volume <= 0:
            cond.state = "rejected"
            cond.reject_reason = "open_order_unfilled"
            self.analytics.inc_counter(cond.logical_symbol, "cond_rejected_entry")
            return None

        cond.state = "triggered"
        tier = "large" if filled_volume >= 2 else "small"
        pos_id = self._next_pos_id(cond.logical_symbol)
        pos = OpenPosition(
            pos_id=pos_id,
            cond_id=cond.cond_id,
            logical_symbol=cond.logical_symbol,
            actual_symbol=cond.actual_symbol,
            direction=cond.direction,
            grade=cond.grade,
            entry_score=cond.entry_score,
            tier=tier,
            total_lots=filled_volume,
            remaining_lots=filled_volume,
            open_fill_price=float(fill_price),
            open_ts=fill_ts,
            planned_entry=cond.entry,
            planned_risk=cond.risk,
            initial_stop=cond.stop,
            current_stop=cond.stop,
            target=cond.target,
            tick=cond.tick,
            multiplier=cond.multiplier,
            open_order_ids=list(order_ids or []),
            open_commission=float(commission),
        )
        self.positions[pos_id] = pos
        self.active_positions[pos_id] = pos
        return pos

    def on_open_rejected(self, cond_id: str, reason: str, ts: str = "") -> None:
        cond = self.conditions.get(cond_id)
        if cond is not None:
            cond.state = "rejected"
            cond.reject_reason = reason
            self.analytics.inc_counter(cond.logical_symbol, "cond_rejected_entry")
            self.analytics.broker_rejections.append({
                "ts": ts,
                "cond_id": cond_id,
                "logical_symbol": cond.logical_symbol,
                "actual_symbol": cond.actual_symbol,
                "lots": cond.lots,
                "reason": reason,
            })

    def on_close_filled(
        self,
        pos_id: str,
        filled_volume: int,
        fill_price: float,
        fill_ts: str,
        reason: str,
        is_lot1: bool,
        order_ids: Optional[list[str]] = None,
        close_commission: float = 0.0,
    ) -> Optional[dict[str, Any]]:
        pos = self.active_positions.get(pos_id) or self.positions.get(pos_id)
        if pos is None:
            return None
        if filled_volume <= 0:
            # Allow re-triggering on subsequent quote if close order failed
            pos.lot1_exit_sent = False
            pos.closing_all_sent = False
            return None

        actual_closed = min(filled_volume, pos.remaining_lots)
        sign = 1.0 if pos.direction == "up" else -1.0
        price_diff = (float(fill_price) - pos.open_fill_price) * sign
        r_multiple = price_diff / max(pos.planned_risk, 1e-9)
        gross_pnl = price_diff * pos.multiplier * actual_closed
        open_comm_share = pos.open_commission * (actual_closed / max(pos.total_lots, 1))
        total_comm = open_comm_share + float(close_commission)

        leg = {
            "pos_id": pos.pos_id,
            "cond_id": pos.cond_id,
            "logical_symbol": pos.logical_symbol,
            "actual_symbol": pos.actual_symbol,
            "direction": pos.direction,
            "grade": pos.grade,
            "entry_score": pos.entry_score,
            "tier": pos.tier,
            "lots": actual_closed,
            "open_price": pos.open_fill_price,
            "close_price": float(fill_price),
            "open_ts": pos.open_ts,
            "close_ts": fill_ts,
            "planned_risk": pos.planned_risk,
            "r_multiple": r_multiple,
            "gross_pnl": gross_pnl,
            "open_commission_share": open_comm_share,
            "close_commission": float(close_commission),
            "commission": total_comm,
            "net_pnl": gross_pnl - total_comm,
            "exit_reason": reason,
            "bars_held": pos.bars_held,
            "max_mfe_r": pos.max_mfe_r,
            "close_order_ids": list(order_ids or []),
        }
        pos.exit_legs.append(leg)
        self.analytics.record_exit_leg(leg)

        pos.remaining_lots -= actual_closed
        if pos.remaining_lots <= 0:
            self.active_positions.pop(pos.pos_id, None)
            cond = self.conditions.get(pos.cond_id)
            if cond is not None:
                cond.state = "closed"
        elif is_lot1:
            # Protection lock: move stop to breakeven only after confirmed Lot 1 close fill
            pos.lot1_closed = True
            pos.current_stop = max(pos.current_stop, pos.open_fill_price) if sign > 0 else min(
                pos.current_stop, pos.open_fill_price
            )
            pos.current_stop = continuous_stop_price(
                pos.open_fill_price,
                max(pos.planned_risk, 1e-9),
                sign,
                pos.max_mfe_r,
                pos.current_stop,
            )
        return leg

    def on_close_rejected(self, pos_id: str) -> None:
        pos = self.active_positions.get(pos_id)
        if pos is not None:
            pos.lot1_exit_sent = False
            pos.closing_all_sent = False


# ===== TQSDK Integration helpers =====
@dataclass
class _PendingBrokerGroup:
    instruction: OrderInstruction
    orders: list[Any]


def _order_is_finished(order: Any) -> bool:
    return getattr(order, "status", "") == "FINISHED"


def _submit_instruction_orders(
    api: Any,
    instr: OrderInstruction,
    quote_obj: Any,
    slippage_ticks: int,
) -> list[Any]:
    q_tick = QuoteTick(
        symbol=instr.actual_symbol,
        datetime=str(getattr(quote_obj, "datetime", "")),
        last_price=float(getattr(quote_obj, "last_price", instr.trigger_price)),
        ask_price1=float(getattr(quote_obj, "ask_price1", float("nan"))),
        bid_price1=float(getattr(quote_obj, "bid_price1", float("nan"))),
        upper_limit=float(getattr(quote_obj, "upper_limit", float("nan"))),
        lower_limit=float(getattr(quote_obj, "lower_limit", float("nan"))),
        price_tick=float(getattr(quote_obj, "price_tick", 1.0) or 1.0),
        volume_multiple=float(getattr(quote_obj, "volume_multiple", 1.0) or 1.0),
    )
    limit_price = compute_slippage_limit_price(instr.direction, q_tick, slippage_ticks)

    if instr.intent == "open":
        order = api.insert_order(
            symbol=instr.actual_symbol,
            direction=instr.direction,
            offset="OPEN",
            volume=int(instr.volume),
            limit_price=limit_price,
        )
        return [order]

    exchange_id = instr.actual_symbol.split(".", 1)[0] if "." in instr.actual_symbol else ""
    pos_obj = api.get_position(instr.actual_symbol)
    if instr.direction == "SELL":
        # Closing long position
        avail_his = max(0, int(getattr(pos_obj, "volume_long_his", 0)) - int(getattr(pos_obj, "volume_long_frozen_his", 0)))
        avail_today = max(0, int(getattr(pos_obj, "volume_long_today", 0)) - int(getattr(pos_obj, "volume_long_frozen_today", 0)))
    else:
        # Closing short position
        avail_his = max(0, int(getattr(pos_obj, "volume_short_his", 0)) - int(getattr(pos_obj, "volume_short_frozen_his", 0)))
        avail_today = max(0, int(getattr(pos_obj, "volume_short_today", 0)) - int(getattr(pos_obj, "volume_short_frozen_today", 0)))

    legs = compute_close_offsets(exchange_id, instr.volume, avail_his, avail_today)
    orders = []
    for offset, vol in legs:
        orders.append(api.insert_order(
            symbol=instr.actual_symbol,
            direction=instr.direction,
            offset=offset,
            volume=int(vol),
            limit_price=limit_price,
        ))
    return orders


def _extract_group_fill(group: _PendingBrokerGroup, fallback_ts: str) -> tuple[int, float, str, list[str], str]:
    total_filled = 0
    weighted_price_sum = 0.0
    last_msg = ""
    order_ids: list[str] = []
    last_ts = fallback_ts
    for o in group.orders:
        oid = str(getattr(o, "order_id", ""))
        if oid:
            order_ids.append(oid)
        orign = int(getattr(o, "volume_orign", 0) or 0)
        left = int(getattr(o, "volume_left", 0) or 0)
        filled = max(0, orign - left)
        if filled > 0:
            tp = float(getattr(o, "trade_price", float("nan")))
            if not math.isfinite(tp) or tp <= 0:
                tp = group.instruction.trigger_price
            total_filled += filled
            weighted_price_sum += tp * filled
        msg = str(getattr(o, "last_msg", "") or "")
        if msg:
            last_msg = msg
        ins_ns = getattr(o, "insert_date_time", 0)
        if ins_ns:
            last_ts = format_tq_nano_bj(int(ins_ns))
    avg_price = (weighted_price_sum / total_filled) if total_filled > 0 else 0.0
    return total_filled, avg_price, last_ts, order_ids, last_msg


def _collect_comm_by_order(sim: Any) -> dict[str, float]:
    """Extract exact order_id -> commission map from TqSim `trade_log` and unsettled buffer."""
    comm_by_order: dict[str, float] = {}
    trade_log = getattr(sim, "trade_log", None) or {}
    for day_entry in trade_log.values():
        for tr in day_entry.get("trades", []):
            oid = str(tr.get("order_id", ""))
            if oid:
                comm_by_order[oid] = comm_by_order.get(oid, 0.0) + float(tr.get("commission", 0.0) or 0.0)
    # Include intraday trades if backtest stopped before EOD settle
    sim_trades = getattr(sim, "_trades", None)
    if sim_trades is None:
        inner = getattr(sim, "_sim_trade", None)
        sim_trades = getattr(inner, "_trades", None) if inner is not None else None
    if isinstance(sim_trades, list):
        for tr in sim_trades:
            oid = str(tr.get("order_id", ""))
            if oid and oid not in comm_by_order:
                comm_by_order[oid] = comm_by_order.get(oid, 0.0) + float(tr.get("commission", 0.0) or 0.0)
    return comm_by_order


def _kline_row_to_bar(row: Any, rollover: bool, duration_secs: int) -> dict[str, Any]:
    hold_val = row.get("close_oi", 0.0) if hasattr(row, "get") else getattr(row, "close_oi", 0.0)
    if hold_val is None or (isinstance(hold_val, float) and not math.isfinite(hold_val)):
        hold_val = 0.0
    vol_val = row.get("volume", 0.0) if hasattr(row, "get") else getattr(row, "volume", 0.0)
    if vol_val is None or (isinstance(vol_val, float) and not math.isfinite(vol_val)):
        vol_val = 0.0
    return {
        "datetime": tq_bar_end_datetime(int(row["datetime"]), duration_secs),
        "open": float(row["open"]),
        "high": float(row["high"]),
        "low": float(row["low"]),
        "close": float(row["close"]),
        "volume": float(vol_val),
        "hold": float(hold_val),
        "rollover": bool(rollover),
    }


def _resolve_tq_auth(account: Optional[str], password: Optional[str]) -> tuple[str, str]:
    acc = (
        account
        or os.environ.get("TQ_REPLAY_ACCOUNT")
        or os.environ.get("TQ_ACCOUNT")
        or ""
    ).strip()
    pwd = (
        password
        or os.environ.get("TQ_REPLAY_PASSWORD")
        or os.environ.get("TQ_PASSWORD")
        or ""
    ).strip()
    if not acc or not pwd:
        raise RuntimeError(
            "Missing Tianqin auth credentials. Pass `--account USER --password PASS` or set "
            "env vars `TQ_REPLAY_ACCOUNT`/`TQ_REPLAY_PASSWORD` (or `TQ_ACCOUNT`/`TQ_PASSWORD`)."
        )
    return acc, pwd


def run_tq_backtest(
    cfg: BacktestConfig,
    account: Optional[str] = None,
    password: Optional[str] = None,
) -> dict[str, Any]:
    """Execute end-to-end backtest via TQSDK (`TqBacktest` + `TqSim`) and Rust `n-signal-worker`."""
    from tqsdk import BacktestFinished, TqApi, TqAuth, TqBacktest, TqSim

    acc, pwd = _resolve_tq_auth(account, password)
    if not cfg.symbols:
        raise ValueError("At least one symbol is required.")

    symbol_specs: list[tuple[str, str]] = []
    for raw in cfg.symbols:
        norm = raw.strip().upper()
        if not norm:
            continue
        _, _, tq_sym = SymbolMapper.parse_code(norm)
        symbol_specs.append((norm, tq_sym))

    if not cfg.quiet:
        print(f"[backtest] Initializing n-signal-worker & TqBacktest ({cfg.start} -> {cfg.end})...")

    start_dt = cfg.start if isinstance(cfg.start, datetime) else datetime(cfg.start.year, cfg.start.month, cfg.start.day, 0, 0, 0)
    end_dt = cfg.end if isinstance(cfg.end, datetime) else datetime(cfg.end.year, cfg.end.month, cfg.end.day, 23, 59, 59)
    start_bj_str = start_dt.strftime("%Y-%m-%d %H:%M:%S")

    worker = SignalWorkerClient(cfg.worker_path)
    worker.start()
    engine = NPatternBacktestEngine(cfg, worker=worker)
    sim = TqSim(init_balance=cfg.init_balance)
    api = TqApi(
        sim,
        backtest=TqBacktest(start_dt=start_dt, end_dt=end_dt),
        auth=TqAuth(acc, pwd),
        web_gui=cfg.web_gui,
        disable_print=cfg.quiet,
    )

    pending_groups: list[_PendingBrokerGroup] = []
    latest_prices: dict[str, tuple[float, str]] = {}
    last_equity = float(cfg.init_balance)
    last_commission_reported = 0.0
    last_progress_day = ""

    try:
        kline_len = max(cfg.warmup_bars + 40, 260)
        klines_map: dict[str, Any] = {}
        cont_quotes: dict[str, Any] = {}
        tq_sym_map: dict[str, str] = {}
        initialized_syms: set[str] = set()
        last_closed_id: dict[str, int] = {}
        last_underlying: dict[str, str] = {}
        rollover_pending: dict[str, bool] = {}
        sym_meta: dict[str, tuple[float, float]] = {}

        actual_quotes: dict[str, Any] = {}
        actual_1m_klines: dict[str, Any] = {}

        def ensure_actual_subscribed(actual_sym: str) -> None:
            if not actual_sym or actual_sym in actual_quotes:
                return
            q_obj = api.get_quote(actual_sym)
            actual_quotes[actual_sym] = q_obj
            actual_1m_klines[actual_sym] = api.get_kline_serial(actual_sym, 60, data_length=5)

        for logical, tq_sym in symbol_specs:
            tq_sym_map[logical] = tq_sym
            cont_q = api.get_quote(tq_sym)
            cont_quotes[logical] = cont_q
            klines_map[logical] = api.get_kline_serial(tq_sym, cfg.bar_seconds, data_length=kline_len)
            init_actual = (getattr(cont_q, "underlying_symbol", "") or "").strip() or tq_sym
            last_underlying[logical] = init_actual
            ensure_actual_subscribed(init_actual)

        acct_obj = api.get_account()

        def current_sim_ts() -> str:
            for q in list(actual_quotes.values()) + list(cont_quotes.values()):
                dt_str = str(getattr(q, "datetime", "") or "")
                if dt_str:
                    return dt_str[:19]
            return start_bj_str

        def dispatch_instructions(instrs: list[OrderInstruction]) -> None:
            for instr in instrs:
                ensure_actual_subscribed(instr.actual_symbol)
                q_obj = actual_quotes[instr.actual_symbol]
                orders = _submit_instruction_orders(api, instr, q_obj, cfg.slippage_ticks)
                pending_groups.append(_PendingBrokerGroup(instruction=instr, orders=orders))

        while True:
            api.wait_update()

            sim_ts = current_sim_ts()
            sim_day = sim_ts[:10]
            if not cfg.quiet and sim_day and sim_day != last_progress_day and initialized_syms:
                last_progress_day = sim_day
                print(
                    f"[progress] {sim_day} | eq={last_equity:,.2f} | "
                    f"legs={len(engine.analytics.exit_legs)} | open={len(engine.active_positions)} | "
                    f"risk_ev={len(engine.analytics.risk_events)} | rej={len(engine.analytics.broker_rejections)}"
                )

            eq = float(getattr(acct_obj, "balance", last_equity))
            if math.isfinite(eq) and eq > 0:
                last_equity = eq
                engine.analytics.record_equity(sim_ts, eq)
            comm_now = float(getattr(acct_obj, "commission", 0.0) or 0.0)
            if math.isfinite(comm_now):
                last_commission_reported = comm_now

            # 1. Reconcile finished broker orders first (protects lot1 confirmation invariant)
            still_pending: list[_PendingBrokerGroup] = []
            for grp in pending_groups:
                if not all(_order_is_finished(o) for o in grp.orders):
                    still_pending.append(grp)
                    continue
                filled_vol, fill_price, fill_ts, order_ids, last_msg = _extract_group_fill(grp, sim_ts)
                instr = grp.instruction
                if instr.intent == "open":
                    if filled_vol > 0:
                        engine.on_open_filled(
                            cond_id=instr.ref_id,
                            filled_volume=filled_vol,
                            fill_price=fill_price,
                            fill_ts=fill_ts,
                            order_ids=order_ids,
                        )
                    else:
                        engine.on_open_rejected(instr.ref_id, last_msg or "unfilled", ts=fill_ts)
                else:
                    if filled_vol > 0:
                        engine.on_close_filled(
                            pos_id=instr.ref_id,
                            filled_volume=filled_vol,
                            fill_price=fill_price,
                            fill_ts=fill_ts,
                            reason=instr.reason,
                            is_lot1=instr.is_lot1,
                            order_ids=order_ids,
                        )
                    else:
                        engine.on_close_rejected(instr.ref_id)
            pending_groups = still_pending

            # 2. Check rollover transitions on continuous contracts
            for logical, cont_q in cont_quotes.items():
                tq_sym = tq_sym_map[logical]
                cur_actual = (getattr(cont_q, "underlying_symbol", "") or "").strip() or tq_sym
                ensure_actual_subscribed(cur_actual)
                prev_actual = last_underlying.get(logical, cur_actual)
                if cur_actual != prev_actual:
                    last_underlying[logical] = cur_actual
                    rollover_pending[logical] = True
                    cancelled = engine.cancel_pending_for_rollover(logical, reason="rollover")
                    if not cfg.quiet:
                        print(
                            f"[rollover] {logical} {prev_actual} -> {cur_actual} "
                            f"(cancelled_pending={len(cancelled)})"
                        )

            # 3. Check 1m quote updates on subscribed actual contracts
            for actual_sym, q_obj in list(actual_quotes.items()):
                lp = float(getattr(q_obj, "last_price", float("nan")))
                if math.isfinite(lp) and lp > 0:
                    latest_prices[actual_sym] = (lp, str(getattr(q_obj, "datetime", "") or current_sim_ts())[:19])
                if not api.is_changing(q_obj, ["datetime", "last_price"]):
                    continue
                if not math.isfinite(lp) or lp <= 0:
                    continue
                q_tick = QuoteTick(
                    symbol=actual_sym,
                    datetime=str(getattr(q_obj, "datetime", "") or current_sim_ts())[:19],
                    last_price=lp,
                    ask_price1=float(getattr(q_obj, "ask_price1", float("nan"))),
                    bid_price1=float(getattr(q_obj, "bid_price1", float("nan"))),
                    upper_limit=float(getattr(q_obj, "upper_limit", float("nan"))),
                    lower_limit=float(getattr(q_obj, "lower_limit", float("nan"))),
                    price_tick=float(getattr(q_obj, "price_tick", 1.0) or 1.0),
                    volume_multiple=float(getattr(q_obj, "volume_multiple", 1.0) or 1.0),
                    underlying_symbol=str(getattr(q_obj, "underlying_symbol", "") or ""),
                )
                dispatch_instructions(engine.on_quote(q_tick))

            # 4. Check completed 15m bars for signal recognition
            for logical, df in klines_map.items():
                if df is None or len(df) < 2:
                    continue
                valid = df[df["datetime"] > 0]
                if len(valid) < 2:
                    continue
                completed = valid.iloc[:-1]
                cur_last_id = int(completed.iloc[-1]["id"])

                tq_sym = tq_sym_map[logical]
                actual_sym = last_underlying.get(logical, tq_sym)
                q_meta = actual_quotes.get(actual_sym) or cont_quotes[logical]
                tick = float(getattr(q_meta, "price_tick", 0.0) or getattr(cont_quotes[logical], "price_tick", 0.0) or 1.0)

                def step_rows(rows_iter: Any, eff_tick: float, eff_mult: float) -> None:
                    for _, row in rows_iter:
                        ro = rollover_pending.pop(logical, False)
                        bar = _kline_row_to_bar(row, ro, cfg.bar_seconds)
                        instrs, _ = engine.on_bar_closed(
                            logical_symbol=logical,
                            bar=bar,
                            actual_symbol=actual_sym,
                            tick=eff_tick,
                            multiplier=eff_mult,
                            current_equity=last_equity,
                        )
                        dispatch_instructions(instrs)

                if logical not in initialized_syms:
                    warmup_bars: list[dict[str, Any]] = []
                    active_rows: list[tuple[Any, Any]] = []
                    for idx, row in completed.iterrows():
                        bar_end = tq_bar_end_datetime(int(row["datetime"]), cfg.bar_seconds)
                        if bar_end < start_bj_str:
                            warmup_bars.append(_kline_row_to_bar(row, False, cfg.bar_seconds))
                        else:
                            active_rows.append((idx, row))
                    resp = worker.init_symbol(logical, tick, warmup_bars)
                    sym_meta[logical] = (
                        float(resp.get("tick", tick)),
                        float(resp.get("multiplier", getattr(q_meta, "volume_multiple", 1.0) or 1.0)),
                    )
                    initialized_syms.add(logical)
                    last_closed_id[logical] = cur_last_id
                    if active_rows:
                        step_rows(active_rows, *sym_meta[logical])
                    continue

                prev_id = last_closed_id.get(logical, -1)
                if cur_last_id <= prev_id:
                    continue

                new_rows = completed[completed["id"] > prev_id]
                eff_tick, eff_mult = sym_meta.get(
                    logical,
                    (tick, float(getattr(q_meta, "volume_multiple", 1.0) or 1.0)),
                )
                step_rows(new_rows.iterrows(), eff_tick, eff_mult)
                last_closed_id[logical] = cur_last_id

    except BacktestFinished:
        if not cfg.quiet:
            print("[backtest] TqBacktest finished.")
    finally:
        try:
            api.close()
        except Exception:
            pass
        worker.close()

    # Reconcile commissions & summarize any still-open positions
    comm_by_order = _collect_comm_by_order(sim)
    engine.analytics.reconcile_commissions(engine.positions, comm_by_order)
    engine.analytics.final_equity = last_equity
    engine.analytics.account_commission = last_commission_reported
    engine.analytics.tqsdk_stat = dict(getattr(sim, "tqsdk_stat", None) or {})

    still_open: list[dict[str, Any]] = []
    for pos in engine.active_positions.values():
        last_px, last_ts = latest_prices.get(pos.actual_symbol, (pos.open_fill_price, pos.open_ts))
        sign = 1.0 if pos.direction == "up" else -1.0
        unrealized = (last_px - pos.open_fill_price) * sign * pos.multiplier * pos.remaining_lots
        still_open.append({
            "pos_id": pos.pos_id,
            "cond_id": pos.cond_id,
            "logical_symbol": pos.logical_symbol,
            "actual_symbol": pos.actual_symbol,
            "direction": pos.direction,
            "lots_remaining": pos.remaining_lots,
            "open_ts": pos.open_ts,
            "open_price": pos.open_fill_price,
            "last_ts": last_ts,
            "last_price": last_px,
            "unrealized_pnl": unrealized,
            "open_commission": pos.open_commission * (pos.remaining_lots / max(pos.total_lots, 1)),
        })
    engine.analytics.open_positions_at_end = still_open

    report = engine.analytics.build_report([s for s, _ in symbol_specs])
    report["meta"] = {
        "symbols": [s for s, _ in symbol_specs],
        "start": cfg.start.isoformat(),
        "end": cfg.end.isoformat(),
        "min_score": cfg.min_score,
        "large_score": cfg.large_score,
        "min_risk_ticks": cfg.min_risk_ticks,
        "max_risk_yuan": cfg.max_risk_yuan,
        "max_risk_pct": cfg.max_risk_pct,
        "risk_action": cfg.risk_action,
        "slippage_ticks": cfg.slippage_ticks,
        "allow_rollover_overlap": cfg.allow_rollover_overlap,
    }
    return report


# ===== Reporting & CSV/JSON export =====
def _fmt_pf(pf: float) -> str:
    if math.isinf(pf):
        return "INF"
    return f"{pf:.2f}"


def print_report(report: dict[str, Any]) -> None:
    acc = report["account"]
    ov = report["overall"]
    meta = report.get("meta", {})
    stat = acc.get("tqsdk_stat") or {}

    print("\n" + "=" * 78)
    print(f"N-Pattern Strategy TQSDK Backtest Report ({meta.get('start', '?')} ~ {meta.get('end', '?')})")
    print("=" * 78)
    print(
        f"Initial Balance : {acc['init_balance']:>12,.2f}    "
        f"Final Equity    : {acc['final_equity']:>12,.2f}"
    )
    print(
        f"Net Return      : {acc['total_return_yuan']:>+12,.2f} ({acc['total_return_pct'] * 100:>+6.2f}%)  "
        f"Max Drawdown    : {acc['max_drawdown_yuan']:>10,.2f} ({acc['max_drawdown_pct'] * 100:.2f}%)"
    )
    print(
        f"Realized Gross  : {acc['realized_gross_pnl']:>+12,.2f}    "
        f"Commission      : {acc['realized_commission']:>12,.2f}"
    )
    print(
        f"Realized Net    : {acc['realized_net_pnl']:>+12,.2f}    "
        f"Open Unrealized : {acc['open_unrealized_pnl']:>+12,.2f} (open={acc['open_positions_count']})"
    )
    if stat:
        print(
            f"TQSim Stats     : Sharpe={stat.get('sharpe_ratio', 0.0):.2f}  "
            f"Annual={stat.get('annual_yield', 0.0) * 100:.2f}%  "
            f"MaxDD={stat.get('max_drawdown', 0.0) * 100:.2f}%  "
            f"Sortino={stat.get('sortino_ratio', 0.0):.2f}"
        )

    print("-" * 78)
    print(
        f"Closed Trades   : {ov['trades']} (W:{ov['wins']} / L:{ov['losses']} / F:{ov['flats']})  "
        f"WinRate={ov['win_rate'] * 100:.1f}%  PF={_fmt_pf(ov['profit_factor'])}  "
        f"AvgR={ov['avg_r']:+.2f}R"
    )
    print(
        f"Avg Win / Loss  : {ov['avg_win_yuan']:+,.2f} / {ov['avg_loss_yuan']:+,.2f}    "
        f"Max Win / Loss  : {ov['max_win_yuan']:+,.2f} / {ov['max_loss_yuan']:+,.2f}"
    )
    if report["exit_reasons"]:
        reasons_str = ", ".join(f"{k}={v}" for k, v in sorted(report["exit_reasons"].items()))
        print(f"Exit Legs       : {reasons_str}")
    if report["risk_events_count"] > 0:
        print(f"Risk Gate Events: {report['risk_events_count']} (see risk_events.csv)")
    if report.get("broker_rejections_count", 0) > 0:
        samples = ", ".join(
            f"{r['logical_symbol']}({r['reason']})" for r in report.get("broker_rejections", [])[:4]
        )
        print(f"Broker Rejects  : {report['broker_rejections_count']} (e.g. {samples})")

    print("-" * 78)
    header = (
        f"{'Symbol':<18} {'Trades':>6} {'Win%':>6} {'PF':>6} {'AvgR':>7} "
        f"{'NetPnL':>12} {'Comm':>9} {'Long(N/Net)':>15} {'Short(N/Net)':>15}"
    )
    print(header)
    print("-" * len(header))
    for sym, entry in report["by_symbol"].items():
        lng = entry["long"]
        sht = entry["short"]
        lng_str = f"{lng['trades']}/{lng['net_pnl']:+,.0f}"
        sht_str = f"{sht['trades']}/{sht['net_pnl']:+,.0f}"
        print(
            f"{entry['display_name']:<18} "
            f"{entry['trades']:>6d} "
            f"{entry['win_rate'] * 100:>5.1f}% "
            f"{_fmt_pf(entry['profit_factor']):>6} "
            f"{entry['avg_r']:>+6.2f}R "
            f"{entry['net_pnl']:>+12,.2f} "
            f"{entry['commission']:>9,.2f} "
            f"{lng_str:>15} "
            f"{sht_str:>15}"
        )
    print("=" * 78 + "\n")


def export_report(report: dict[str, Any], export_dir: str | Path) -> Path:
    out_dir = Path(export_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    summary_payload = {
        "meta": report.get("meta", {}),
        "account": report["account"],
        "overall": report["overall"],
        "exit_reasons": report["exit_reasons"],
        "risk_events_count": report["risk_events_count"],
        "broker_rejections_count": report.get("broker_rejections_count", 0),
        "broker_rejections": report.get("broker_rejections", []),
        "by_symbol": report["by_symbol"],
        "open_positions_at_end": report["open_positions_at_end"],
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary_payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    _write_csv(out_dir / "trades.csv", report["trades"])
    _write_csv(out_dir / "legs.csv", report["legs"])
    _write_csv(out_dir / "risk_events.csv", report["risk_events"])

    by_symbol_rows = []
    for sym, entry in report["by_symbol"].items():
        by_symbol_rows.append({
            "symbol": sym,
            "display_name": entry["display_name"],
            "trades": entry["trades"],
            "wins": entry["wins"],
            "losses": entry["losses"],
            "win_rate": round(entry["win_rate"], 4),
            "profit_factor": entry["profit_factor"],
            "avg_r": round(entry["avg_r"], 4),
            "gross_pnl": round(entry["gross_pnl"], 2),
            "commission": round(entry["commission"], 2),
            "net_pnl": round(entry["net_pnl"], 2),
            "long_trades": entry["long"]["trades"],
            "long_net_pnl": round(entry["long"]["net_pnl"], 2),
            "short_trades": entry["short"]["trades"],
            "short_net_pnl": round(entry["short"]["net_pnl"], 2),
            "cond_created": entry["counters"].get("cond_created", 0),
            "risk_rejected": entry["counters"].get("risk_rejected", 0),
            "risk_downsized": entry["counters"].get("risk_downsized", 0),
            "cond_cancelled_rollover": entry["counters"].get("cond_cancelled_rollover", 0),
        })
    _write_csv(out_dir / "by_symbol.csv", by_symbol_rows)
    return out_dir


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def parse_cli_datetime(raw: str, is_end: bool = False) -> datetime:
    val = raw.strip().replace("T", " ")
    if len(val) == 10:
        d = date.fromisoformat(val)
        return datetime(d.year, d.month, d.day, 23, 59, 59) if is_end else datetime(d.year, d.month, d.day, 0, 0, 0)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(val, fmt)
        except ValueError:
            continue
    return datetime.fromisoformat(val)


def load_symbols_from_db(db_path: str | Path) -> list[str]:
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(f"SQLite symbol database not found: {path}")
    con = sqlite3.connect(str(path))
    try:
        rows = con.execute("SELECT code FROM symbols WHERE enabled = 1 ORDER BY rowid").fetchall()
        if not rows:
            rows = con.execute("SELECT code FROM symbols ORDER BY rowid").fetchall()
        return [str(r[0]).strip().upper() for r in rows if r and str(r[0]).strip()]
    finally:
        con.close()


# ===== CLI =====
def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Backtest n-core N-pattern strategy on TQSDK.")
    p.add_argument("--symbols", default="", help="Comma-separated symbols, e.g. RB0,EG0,AU0")
    p.add_argument("--symbols-db", default=None, help="Load enabled symbols from SQLite DB (e.g. data-dev/ntrend.db)")
    p.add_argument("--start", required=True, help="Backtest start time YYYY-MM-DD or 'YYYY-MM-DD HH:MM(:SS)'")
    p.add_argument("--end", required=True, help="Backtest end time YYYY-MM-DD or 'YYYY-MM-DD HH:MM(:SS)'")
    p.add_argument("--balance", type=float, default=1_000_000.0, help="Initial simulated balance (CNY)")
    p.add_argument("--warmup-bars", type=int, default=200, help="15m warmup bars before start date")
    p.add_argument("--min-score", type=float, default=DEFAULT_MIN_SCORE, help="Min N-pattern entry score")
    p.add_argument("--large-score", type=float, default=DEFAULT_LARGE_SCORE, help="Score threshold for 2-lot sizing")
    p.add_argument("--min-risk-ticks", type=int, default=DEFAULT_MIN_RISK_TICKS, help="Minimum stop distance in ticks")
    p.add_argument("--max-risk-yuan", type=float, default=None, help="Max risk per trade in CNY")
    p.add_argument("--max-risk-pct", type=float, default=None, help="Max risk per trade as fraction of equity (e.g. 0.01)")
    p.add_argument("--risk-action", choices=["downsize", "reject"], default="downsize", help="Action when 2-lot exceeds cap")
    p.add_argument("--slippage-ticks", type=int, default=0, help="Additional slippage ticks beyond ask1/bid1")
    p.add_argument("--allow-rollover-overlap", action="store_true", help="Allow new contract entry while old contract open")
    p.add_argument("--worker-bin", default=None, help="Optional explicit path to n-signal-worker binary")
    p.add_argument("--web-gui", default="", help="Pass true or host:port to open TQSDK WebUI during backtest")
    p.add_argument("--export-dir", default=None, help="Directory to export JSON & CSV reports")
    p.add_argument("--account", default=None, help="Tianqin account (or TQ_REPLAY_ACCOUNT / TQ_ACCOUNT)")
    p.add_argument("--password", default=None, help="Tianqin password (or TQ_REPLAY_PASSWORD / TQ_PASSWORD)")
    p.add_argument("--quiet", action="store_true", help="Suppress intermediate progress logs")
    return p


def _parse_web_gui(raw: str) -> bool | str:
    val = raw.strip()
    if not val or val.lower() in ("false", "0", "no"):
        return False
    if val.lower() in ("true", "1", "yes"):
        return True
    return val


def main(argv: Optional[list[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    parser = build_arg_parser()
    args = parser.parse_args(argv)
    symbols: list[str] = []
    if args.symbols_db:
        symbols.extend(load_symbols_from_db(args.symbols_db))
    if args.symbols:
        for s in args.symbols.split(","):
            norm = s.strip().upper()
            if norm and norm not in symbols:
                symbols.append(norm)
    if not symbols:
        parser.error("Provide --symbols and/or --symbols-db.")

    cfg = BacktestConfig(
        symbols=symbols,
        start=parse_cli_datetime(args.start, is_end=False),
        end=parse_cli_datetime(args.end, is_end=True),
        init_balance=args.balance,
        warmup_bars=args.warmup_bars,
        min_score=args.min_score,
        large_score=args.large_score,
        min_risk_ticks=args.min_risk_ticks,
        max_risk_yuan=args.max_risk_yuan,
        max_risk_pct=args.max_risk_pct,
        risk_action=args.risk_action,
        slippage_ticks=args.slippage_ticks,
        allow_rollover_overlap=args.allow_rollover_overlap,
        worker_path=args.worker_bin,
        web_gui=_parse_web_gui(args.web_gui),
        export_dir=args.export_dir,
        quiet=args.quiet,
    )

    report = run_tq_backtest(cfg, account=args.account, password=args.password)
    print_report(report)
    if cfg.export_dir:
        out = export_report(report, cfg.export_dir)
        print(f"[backtest] Reports exported to: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
