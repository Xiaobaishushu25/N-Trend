#!/usr/bin/env python3
"""
TQSDK + n-core N-pattern strategy backtest runner & analytics engine.

Architecture:
1. Signal recognition runs in local Rust `n-signal-worker` (`crates/n-core`) on
   15-minute completed bars using the live structural recognition & scoring code.
2. Order execution, slippage/spread simulation, margin, and commission run in
   TQSDK `TqBacktest` + `TqSim`, using actual-contract ticks by default.
   Minute mode is an explicitly approximate diagnostic mode.
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
import functools
import html as _html
import json
import math
import os
import re
import hashlib
from pathlib import Path
import sqlite3
import subprocess
import sys
import time as _time
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

# ===== P2 Strategy definitions & reference fees =====
_P2_FEES: dict[str, Any] = {}
_fees_cand_path = _REPO_ROOT / "crates" / "n-core" / "src" / "trader" / "candidates" / "reference-fees-20261022.json"
if _fees_cand_path.is_file():
    try:
        _P2_FEES = json.loads(_fees_cand_path.read_text(encoding="utf-8")).get("products", {})
    except Exception:
        pass


def p2_calc_fee(symbol: str, fill: float, close: float, multiplier: float) -> float:
    product = ''.join(c for c in symbol if c.isalpha()).upper()
    f = _P2_FEES.get(product, {})
    if f.get("roundtrip_fixed_yuan_per_lot") is not None:
        return float(f["roundtrip_fixed_yuan_per_lot"])
    def charge(rule: Optional[dict[str, Any]], price: float) -> float:
        if not rule:
            return 0.0
        return float(rule.get("fixed_yuan_per_lot", 0.0)) + float(rule.get("notional_rate", 0.0)) * price * multiplier
    return charge(f.get("open"), fill) + max(charge(f.get("close_today"), close), charge(f.get("close_yesterday"), close))


def p2_net_space(symbol: str, direction: str, entry: float, target: float, stop: float, tick: float, multiplier: float, fill: float) -> bool:
    sign = 1.0 if direction == "up" else -1.0
    t_adj = target - sign * tick
    s_adj = stop - sign * tick
    reward = (t_adj - fill) * sign * multiplier - p2_calc_fee(symbol, fill, t_adj, multiplier)
    loss = (fill - s_adj) * sign * multiplier + p2_calc_fee(symbol, fill, s_adj, multiplier)
    return loss > 0.0 and reward + 1e-7 >= loss


def p2_is_weekend_window(symbol: str, dt_str: str) -> bool:
    try:
        dt_val = datetime.fromisoformat(str(dt_str)[:19])
        weekday = dt_val.weekday()
        hour, minute = dt_val.hour, dt_val.minute
        product = ''.join(c for c in symbol if c.isalpha()).upper()
        if product in ("SF", "SM"):
            if weekday == 4 and (hour > 14 or (hour == 14 and minute >= 50)):
                return True
            if weekday in (5, 6):
                return True
            if weekday == 0 and hour < 9:
                return True
        elif product in ("CU", "AL", "ZN", "PB", "NI", "SN", "BC", "SS", "AO"):
            if weekday == 5 and (hour > 0 or (hour == 0 and minute >= 50)):
                return True
            if weekday == 6:
                return True
            if weekday == 0 and hour < 9:
                return True
        else:
            if weekday == 4 and (hour > 22 or (hour == 22 and minute >= 50)):
                return True
            if weekday in (5, 6):
                return True
            if weekday == 0 and hour < 9:
                return True
    except Exception:
        pass
    return False


def p2_is_pre_weekend_exit(symbol: str, dt_str: str) -> bool:
    try:
        dt_val = datetime.fromisoformat(str(dt_str)[:19])
        weekday = dt_val.weekday()
        hour, minute = dt_val.hour, dt_val.minute
        product = ''.join(c for c in symbol if c.isalpha()).upper()
        if product in ("SF", "SM"):
            if weekday == 4 and hour == 14 and minute >= 50:
                return True
        elif product in ("CU", "AL", "ZN", "PB", "NI", "SN", "BC", "SS", "AO"):
            if weekday == 5 and hour == 0 and minute >= 50:
                return True
        else:
            if weekday == 4 and hour == 22 and minute >= 50:
                return True
    except Exception:
        pass
    return False


def p2_compute_atr14(bars: list[dict[str, Any]]) -> Optional[float]:
    if len(bars) < 15:
        return None
    trs = []
    for i in range(len(bars) - 14, len(bars)):
        h = float(bars[i]["high"])
        l = float(bars[i]["low"])
        c_prev = float(bars[i - 1]["close"])
        tr = max(h - l, abs(h - c_prev), abs(l - c_prev))
        trs.append(tr)
    return sum(trs) / 14.0


# ===== Data models =====
@dataclass
class BacktestConfig:
    symbols: list[str]
    start: date | datetime
    end: date | datetime
    init_balance: float = 50_000.0
    warmup_bars: int = 200
    min_score: float = DEFAULT_MIN_SCORE
    large_score: float = DEFAULT_LARGE_SCORE
    min_risk_ticks: int = DEFAULT_MIN_RISK_TICKS
    max_risk_yuan: Optional[float] = None
    max_risk_pct: Optional[float] = None
    risk_action: str = "downsize"  # "downsize" | "reject"
    slippage_ticks: int = 2  # Live aggressive limit offset; not an extra fill cost.
    quote_mode: str = "tick"
    order_wait_seconds: int = 3
    action_interval_seconds: int = 2
    max_exit_slippage_ticks: int = 0
    large_volume: int = 2
    small_volume: int = 1
    strategy: str = "n_pattern"  # "n_pattern" | "p2"
    one_per_symbol: bool = False  # NPatternStrategy uses default (non-exclusive) EntryConstraints.
    max_positions: Optional[int] = None  # Maximum concurrent open positions allowed across portfolio
    market_gap_events: Optional[str] = None
    allow_rollover_overlap: bool = False
    bar_seconds: int = DEFAULT_BAR_SECONDS
    worker_path: Optional[str] = None
    web_gui: bool | str = False
    hold_web: bool = False
    export_dir: Optional[str] = None
    quiet: bool = False

    def __post_init__(self) -> None:
        if self.quote_mode not in ("tick", "minute"):
            raise ValueError("quote_mode must be tick or minute")
        if not math.isfinite(self.init_balance) or self.init_balance <= 0:
            raise ValueError("init_balance must be finite and positive")
        if not 0 <= self.warmup_bars <= 9960:
            raise ValueError("warmup_bars must be between 0 and 9960 (TQ serial length limit)")
        if self.bar_seconds != DEFAULT_BAR_SECONDS:
            raise ValueError("N-pattern live parity requires 900-second bars")
        if min(self.large_volume, self.small_volume) < 1 or min(self.slippage_ticks, self.min_risk_ticks,
                self.max_exit_slippage_ticks, self.order_wait_seconds, self.action_interval_seconds) < 0:
            raise ValueError("Invalid sizing or execution limits")
        if self.strategy not in ("n_pattern", "p2"):
            raise ValueError("strategy must be 'n_pattern' or 'p2'")
        if self.strategy == "p2":
            self.large_volume = 1
            self.small_volume = 1
            self.one_per_symbol = True
            if self.min_score < 3.0:
                self.min_score = 3.0
        if self.max_positions is not None and self.max_positions <= 0:
            raise ValueError("max_positions must be positive")


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
    entry_authorized: bool = False
    retry_after: float = 0.0
    funding_available: Optional[float] = None
    funding_margin: Optional[float] = None
    funding_required: Optional[float] = None
    funding_updated_at: float = 0.0
    funding_frozen_margin: Optional[float] = None
    funding_revision: int = 0
    last_order_message: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def is_active(self) -> bool:
        return self.state in ("pending", "waiting", "submitting", "triggered")


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
    risk: float = 0.0  # Actual fill-to-initial-stop risk, separate from planned_risk.
    pending_exit_reason: str = ""
    close_attempts: int = 0
    pending_lot1_exit: bool = False

    @property
    def management_risk(self) -> float:
        return max(self.risk or abs(self.open_fill_price - self.initial_stop), 1e-9)


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
    scaled = price / tick
    steps = math.floor(scaled + 0.5) if scaled >= 0 else math.ceil(scaled - 0.5)
    rounded = steps * tick
    # Avoid floating point tail artifacts like 3450.0000000000005
    decimals = 0
    tick_str = f"{tick:.10f}".rstrip("0").rstrip(".")
    if "." in tick_str:
        decimals = len(tick_str.split(".", 1)[1])
    return round(rounded, decimals)


@functools.lru_cache(maxsize=16384)
def quote_timestamp(value: str) -> float:
    if not value:
        return 0.0
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
    return dt.timestamp()


def positive_float(*values: Any) -> float:
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number) and number > 0:
            return number
    raise ValueError("No finite positive contract metadata available")


def completed_tq_bars(df: Any, now_ns: int, duration_seconds: int) -> Any:
    """SDK 3.10 emits a final Kline at start + duration - 1 microsecond.

    The final row is complete at that event, including before session breaks.
    Never read its final OHLC before that event (a tick at :59 is not enough).
    """
    valid = df[df['datetime'] > 0]
    return valid[valid['datetime'] + duration_seconds * 1_000_000_000 - 1000 <= now_ns]


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
    reference: Optional[float] = None,
    max_ticks: int = 0,
) -> float:
    """
    Return aggressive `limit_price` accepted by ALL Chinese futures exchanges
    (including SHFE/INE/CFFEX which reject market / ANY orders):
    - `slippage_ticks <= 0`: opponent price (`ask_price1` for BUY, `bid_price1` for SELL),
      falling back to the directional daily limit, then `last_price`, if missing.
    - `slippage_ticks > 0`: opponent price +- `slippage_ticks * tick`.
    Clamped within `[lower_limit, upper_limit]` when valid and rounded to tick.
    """
    tick = positive_float(quote.price_tick)
    extra = max(0, int(slippage_ticks)) * tick
    def usable(price: float) -> bool:
        return (math.isfinite(price) and price > 0
                and (not math.isfinite(quote.lower_limit) or quote.lower_limit <= 0 or price >= quote.lower_limit)
                and (not math.isfinite(quote.upper_limit) or quote.upper_limit <= 0 or price <= quote.upper_limit))
    if direction == "BUY":
        opp = quote.ask_price1 if usable(quote.ask_price1) else (
            quote.upper_limit if math.isfinite(quote.upper_limit) and quote.upper_limit > 0 else quote.last_price)
        raw = opp + extra
    else:
        opp = quote.bid_price1 if usable(quote.bid_price1) else (
            quote.lower_limit if math.isfinite(quote.lower_limit) and quote.lower_limit > 0 else quote.last_price)
        raw = opp - extra
    if reference is not None and max_ticks > 0:
        raw = min(raw, reference + max_ticks * tick) if direction == "BUY" else max(raw, reference - max_ticks * tick)
    raw = round_to_tick(raw, tick)
    if math.isfinite(quote.upper_limit) and quote.upper_limit > 0:
        raw = min(raw, quote.upper_limit)
    if math.isfinite(quote.lower_limit) and quote.lower_limit > 0:
        raw = max(raw, quote.lower_limit)
    return raw


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
        if not all(math.isfinite(v) and v > 0 for v in (tick, risk, multiplier)) or (risk / tick) + 1e-6 < cfg.min_risk_ticks:
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

        base_lots = cfg.large_volume if entry_score >= cfg.large_score else cfg.small_volume
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
    env_bin = os.environ.get("N_SIGNAL_WORKER_BIN")
    selected = explicit_path or env_bin
    if selected:
        path = Path(selected)
        if not path.is_file():
            raise FileNotFoundError(f"Explicit signal worker not found: {path}")
        return path
    exe_name = "n-signal-worker.exe" if os.name == "nt" else "n-signal-worker"
    candidates = [_REPO_ROOT / "target" / mode / exe_name for mode in ("debug", "release")]
    existing = [path for path in candidates if path.is_file()]
    if existing:
        return max(existing, key=lambda path: path.stat().st_mtime_ns)
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
        # Preserve exact tick-level drawdown above; retain only the last equity
        # point per minute to bound memory during multi-symbol tick replays.
        if self.equity_points and self.equity_points[-1][0][:16] == ts[:16]:
            self.equity_points[-1] = (ts, equity, dd_pct)
        else:
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
                "actual_risk": first.get("actual_risk", first["planned_risk"]),
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
            "equity_curve": list(self.equity_points),
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
        self._positions_by_actual: dict[str, dict[str, OpenPosition]] = {}
        self._conditions_by_actual: dict[str, list[ConditionOrder]] = {}
        self._cond_seq = 0
        self._pos_seq = 0
        self._history_bars: dict[str, list[dict[str, Any]]] = {}
        self.events: list[dict[str, Any]] = []
        self._last_timer_bar: dict[str, str] = {}
        self.funds: Optional[tuple[float, float, float]] = None
        self.frozen_margin: Optional[float] = None
        self.funds_revision = 0
        self.quote_floor = 0.0

    def on_market_gap(self, ts: str, working_entry_ids: list[str]) -> None:
        """Replay an explicitly recorded transport gap, never a session break."""
        self.quote_floor = quote_timestamp(ts)
        for cid in working_entry_ids:
            cond = self.conditions[cid]
            cond.state, cond.reject_reason = "cancelled", "market_gap"
        for pos in self.active_positions.values():
            pos.pending_exit_reason = pos.pending_exit_reason or "market_gap"
        self.event(ts, "market_gap", "account", working_entries=working_entry_ids)

    def event(self, ts: str, kind: str, ref_id: str, **details: Any) -> None:
        self.events.append(dict(ts=ts, kind=kind, ref_id=ref_id, **details))

    def update_funds(self, available: float, margin: float, at: float, frozen_margin: Optional[float] = None) -> None:
        if all(math.isfinite(v) for v in (available, margin, at)):
            frozen = (float(frozen_margin) if frozen_margin is not None and math.isfinite(frozen_margin)
                      and frozen_margin >= 0 else None)
            if self.funds != (available, margin, at) or self.frozen_margin != frozen:
                self.funds_revision += 1
            self.funds = (available, margin, at)
            self.frozen_margin = frozen

    def entry_ready(self, cond: ConditionOrder, quote: QuoteTick) -> bool:
        if not all(math.isfinite(v) and v > 0 for v in (quote.last_price, quote.price_tick)):
            return False
        sign = 1.0 if cond.direction == "up" else -1.0
        if cond.entry_authorized and (quote.last_price - cond.stop) * sign <= 0:
            cond.state, cond.reject_reason = "cancelled", "stop_before_entry"
            self.event(quote.datetime, "entry_cancelled", cond.cond_id, reason=cond.reject_reason)
            return False
        if not cond.entry_authorized:
            if (quote.last_price - cond.entry) * sign < 0:
                return False
            cond.entry_authorized = True
            self.event(quote.datetime, "entry_triggered", cond.cond_id, price=quote.last_price)
        now = quote_timestamp(quote.datetime)
        if now < cond.retry_after:
            return False
        if self.funds is not None and (self.funds[0] <= 0 or now - self.funds[2] > 60):
            return False
        if cond.funding_available is not None:
            if self.funds is None:
                return False
            available, margin, updated = self.funds
            old_committed, committed = cond.funding_margin, margin
            if cond.funding_frozen_margin is not None and self.frozen_margin is not None:
                old_committed += cond.funding_frozen_margin
                committed += self.frozen_margin
            recovered = (available >= cond.funding_required if cond.funding_required is not None
                         else committed + 0.01 < old_committed and available > cond.funding_available + 0.01)
            fresh = updated > cond.funding_updated_at or self.funds_revision > cond.funding_revision
            if not fresh or not recovered:
                return False
            cond.funding_available = cond.funding_margin = cond.funding_required = None
            cond.funding_frozen_margin = None
        opposite = quote.ask_price1 if sign > 0 else quote.bid_price1
        if not math.isfinite(opposite) or opposite <= 0:
            opposite = quote.last_price
        allowed = max(cond.risk * 0.2, 2.0 * quote.price_tick)
        return (opposite - cond.entry) * sign <= allowed + 1e-6

    def _next_cond_id(self, logical_symbol: str) -> str:
        self._cond_seq += 1
        return f"C{self._cond_seq}_{logical_symbol}"

    def _next_pos_id(self, logical_symbol: str) -> str:
        self._pos_seq += 1
        return f"P{self._pos_seq}_{logical_symbol}"

    def has_active_slot(self, logical_symbol: str, exclude_cond_id: Optional[str] = None) -> bool:
        if any(p.logical_symbol == logical_symbol for p in self.active_positions.values()):
            return True
        if any(
            c.logical_symbol == logical_symbol and c.cond_id != exclude_cond_id
            and (c.state in ("submitting", "triggered") or (c.state == "waiting" and c.entry_authorized))
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
            if cond.logical_symbol == logical_symbol and cond.state in ("pending", "waiting") and not cond.entry_authorized:
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

        if self._last_timer_bar.get(logical_symbol) == bar_ts:
            return instructions
        self._last_timer_bar[logical_symbol] = bar_ts

        for cond in self.conditions.values():
            if cond.logical_symbol == logical_symbol and cond.state in ("pending", "waiting", "submitting"):
                if bar_ts <= cond.warning_ts:
                    continue
                cond.bars_waited += 1
                if cond.bars_waited > PENDING_BARS:
                    cond.state = "expired"
                    cond.reject_reason = "pending_bars_expired"
                    self.analytics.inc_counter(logical_symbol, "cond_expired")

        for pos in list(self.active_positions.values()):
            if pos.logical_symbol != logical_symbol:
                continue
            pos.bars_held += 1
            reason: Optional[str] = None
            if pos.bars_held >= TIME_HORIZON_BARS:
                reason = "time_exit"
            elif pos.bars_held == NO_FOLLOW_BAR and pos.max_mfe_r < NO_FOLLOW_MFE_R:
                reason = "no_follow"
            if reason is not None:
                pos.pending_exit_reason = pos.pending_exit_reason or reason
                if pos.closing_all_sent or pos.lot1_exit_sent:
                    continue
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
            if not math.isfinite(score):
                raise ValueError(f"Invalid signal score for {logical_symbol}")
            if score < self.cfg.min_score:
                continue
            self.analytics.inc_counter(logical_symbol, "signals_seen")

            if self.cfg.one_per_symbol and (not self.cfg.allow_rollover_overlap) and self.has_active_slot(logical_symbol):
                self.analytics.inc_counter(logical_symbol, "slot_blocked_skipped")
                continue

            if self._is_duplicate_signal(logical_symbol, sig):
                self.analytics.inc_counter(logical_symbol, "dedup_skipped")
                continue

            direction = str(sig["direction"])
            entry = float(sig["entry"])
            stop = float(sig["stop"])
            risk = abs(entry - stop)
            sign = 1.0 if direction == "up" else -1.0
            if direction not in ("up", "down") or not all(math.isfinite(v) and v > 0 for v in (entry, stop, risk, tick, multiplier)) or (entry - stop) * sign <= 0:
                raise ValueError(f"Invalid signal/contract metadata for {logical_symbol}: {sig}")
            target = float(sig["target"])
            if not math.isfinite(target) or target <= 0:
                raise ValueError(f"Invalid signal target for {logical_symbol}")
            if (target - entry) * sign <= risk:
                target = entry + sign * risk

            if self.cfg.strategy == "p2":
                if score < 3.0:
                    self.analytics.inc_counter(logical_symbol, "p2_score_below_min")
                    continue
                if risk / tick + 1e-6 < 10.0:
                    self.analytics.inc_counter(logical_symbol, "p2_risk_ticks_below_min")
                    continue
                forecast_fill = entry + sign * tick
                forecast_stop = stop - sign * tick
                friction = 2.0 * tick * multiplier + p2_calc_fee(logical_symbol, forecast_fill, forecast_stop, multiplier)
                if friction > 0.20 * risk * multiplier + 1e-7:
                    self.analytics.inc_counter(logical_symbol, "p2_friction_above20")
                    continue
                atr14 = p2_compute_atr14(self._history_bars.get(logical_symbol, []))
                if atr14 is None or atr14 <= 0:
                    self.analytics.inc_counter(logical_symbol, "p2_atr_unavailable")
                    continue
                if not (0.75 <= (risk / atr14) <= 2.5):
                    self.analytics.inc_counter(logical_symbol, "p2_atr_out_of_range")
                    continue
                if not p2_net_space(logical_symbol, direction, entry, target, stop, tick, multiplier, forecast_fill):
                    self.analytics.inc_counter(logical_symbol, "p2_filtered_net_rr_plan")
                    continue
                if p2_is_weekend_window(logical_symbol, bar_ts):
                    self.analytics.inc_counter(logical_symbol, "p2_filtered_weekend_window")
                    continue

            decision = RiskGate.evaluate(
                entry_score=score,
                risk=risk,
                tick=tick,
                multiplier=multiplier,
                cfg=self.cfg,
                current_equity=current_equity,
            )
            cond_id = self._next_cond_id(logical_symbol)
            planned_lots = 1 if self.cfg.strategy == "p2" else (
                self.cfg.large_volume if score >= self.cfg.large_score else self.cfg.small_volume
            )

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
                evidence=dict(sig),
            )
            self.conditions[cond_id] = cond
            self._conditions_by_actual.setdefault(actual_symbol, []).append(cond)
            buf = self.recent_conditions.setdefault(logical_symbol, [])
            buf.append(cond)
            if len(buf) > 30:
                del buf[:-30]
            self.analytics.inc_counter(logical_symbol, "cond_created")
            self.event(bar_ts, "condition_created", cond_id, signal=dict(sig), actual_symbol=actual_symbol,
                       tick=tick, multiplier=multiplier, lots=cond.lots)
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
        self._history_bars.setdefault(logical_symbol, []).append(bar)
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
        if not math.isfinite(price) or price <= 0 or quote_timestamp(quote.datetime) < self.quote_floor:
            return []

        instructions: list[OrderInstruction] = []

        # Observe every stop crossing, even while an order is queued/working and
        # broker actions are throttled. A later rebound cannot resurrect it.
        live_conditions = [c for c in self._conditions_by_actual.get(quote.symbol, []) if c.is_active]
        self._conditions_by_actual[quote.symbol] = live_conditions
        for cond in live_conditions:
            if cond.actual_symbol == quote.symbol and cond.state == "submitting" and cond.entry_authorized:
                if (price - cond.stop) * (1 if cond.direction == "up" else -1) <= 0:
                    cond.state, cond.reject_reason = "cancelled", "stop_before_entry"
                    self.event(quote.datetime, "entry_cancelled", cond.cond_id, reason=cond.reject_reason)

        # 1. Active positions exit checks (`crates/n-core/src/trader/policy.rs`)
        for pos in list(self._positions_by_actual.get(quote.symbol, {}).values()):
            if pos.actual_symbol != quote.symbol:
                continue
            sign = 1.0 if pos.direction == "up" else -1.0
            risk = pos.management_risk
            r = (price - pos.open_fill_price) * sign / risk
            if r > pos.max_mfe_r:
                pos.max_mfe_r = r

            # ExecutionEngine observes the initial stop even while a first-lot
            # task is queued/working. Keep the upgrade latched through rebounds.
            if ((pos.lot1_exit_sent or pos.pending_lot1_exit or pos.closing_all_sent)
                    and (price - pos.initial_stop) * sign <= 0 and pos.pending_exit_reason != "stop"):
                pos.pending_exit_reason = "stop"
                pos.pending_lot1_exit = False
                self.event(quote.datetime, "exit_upgraded", pos.pos_id, reason="stop", volume=pos.remaining_lots)

            if pos.closing_all_sent or (pos.lot1_exit_sent and not pos.lot1_closed):
                continue

            if pos.pending_exit_reason:
                pos.closing_all_sent = True
                instructions.append(OrderInstruction("close", pos.pos_id, pos.logical_symbol, pos.actual_symbol,
                    "SELL" if sign > 0 else "BUY", pos.remaining_lots, pos.pending_exit_reason, price,
                    closing_all=True))
            elif self.cfg.strategy == "p2":
                if (price - pos.initial_stop) * sign <= 0.0:
                    pos.closing_all_sent = True
                    instructions.append(OrderInstruction(
                        intent="close",
                        ref_id=pos.pos_id,
                        logical_symbol=pos.logical_symbol,
                        actual_symbol=pos.actual_symbol,
                        direction="SELL" if sign > 0 else "BUY",
                        volume=pos.remaining_lots,
                        reason="stop",
                        trigger_price=price,
                        is_lot1=False,
                        closing_all=True,
                    ))
                elif (price - pos.target) * sign >= 0.0:
                    pos.closing_all_sent = True
                    instructions.append(OrderInstruction(
                        intent="close",
                        ref_id=pos.pos_id,
                        logical_symbol=pos.logical_symbol,
                        actual_symbol=pos.actual_symbol,
                        direction="SELL" if sign > 0 else "BUY",
                        volume=pos.remaining_lots,
                        reason="target_s1",
                        trigger_price=price,
                        is_lot1=False,
                        closing_all=True,
                    ))
                elif p2_is_pre_weekend_exit(pos.logical_symbol, quote.datetime):
                    pos.closing_all_sent = True
                    instructions.append(OrderInstruction(
                        intent="close",
                        ref_id=pos.pos_id,
                        logical_symbol=pos.logical_symbol,
                        actual_symbol=pos.actual_symbol,
                        direction="SELL" if sign > 0 else "BUY",
                        volume=pos.remaining_lots,
                        reason="pre_weekend",
                        trigger_price=price,
                        is_lot1=False,
                        closing_all=True,
                    ))
                continue
            elif (price - pos.current_stop) * sign <= 0.0:
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
            elif (not pos.lot1_closed) and (r >= 1.0 or pos.pending_lot1_exit):
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
                    pos.pending_lot1_exit = True
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
        for cond in live_conditions:
            if cond.actual_symbol != quote.symbol or cond.state not in ("pending", "waiting"):
                continue
            if not self.entry_ready(cond, quote):
                continue
            if self.cfg.one_per_symbol and (not self.cfg.allow_rollover_overlap) and self.has_active_slot(cond.logical_symbol, cond.cond_id):
                cond.state = "cancelled"
                cond.reject_reason = "logical_slot_occupied"
                self.analytics.inc_counter(cond.logical_symbol, "slot_blocked_skipped")
                continue

            if self.cfg.strategy == "p2":
                actual_risk = abs(price - cond.stop)
                if actual_risk / quote.price_tick + 1e-6 < 10.0:
                    cond.state = "cancelled"
                    cond.reject_reason = "filtered_actual_risk"
                    self.analytics.inc_counter(cond.logical_symbol, "p2_actual_risk_blocked")
                    self.event(quote.datetime, "entry_cancelled", cond.cond_id, reason="filtered_actual_risk")
                    continue
                if not p2_net_space(cond.logical_symbol, cond.direction, cond.entry, cond.target, cond.stop, quote.price_tick, cond.multiplier, price):
                    cond.state = "cancelled"
                    cond.reject_reason = "filtered_net_rr_fill"
                    self.analytics.inc_counter(cond.logical_symbol, "p2_net_rr_fill_blocked")
                    self.event(quote.datetime, "entry_cancelled", cond.cond_id, reason="filtered_net_rr_fill")
                    continue

            if self.cfg.max_positions is not None:
                active_and_submitting = len(self.active_positions) + sum(
                    1 for c in self.conditions.values() if c.state == "submitting" and c.cond_id != cond.cond_id
                )
                if active_and_submitting >= self.cfg.max_positions:
                    cond.state = "cancelled"
                    cond.reject_reason = "max_positions_reached"
                    self.analytics.inc_counter(cond.logical_symbol, "max_positions_blocked")
                    self.event(quote.datetime, "entry_cancelled", cond.cond_id, reason="max_positions_reached")
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
        if not math.isfinite(fill_price) or fill_price <= 0:
            raise ValueError("Invalid confirmed open fill price")

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
            risk=max(abs(float(fill_price) - cond.stop), 1e-9),
            initial_stop=cond.stop,
            current_stop=cond.stop,
            target=(float(fill_price) + (1.0 if cond.direction == "up" else -1.0) * abs(float(fill_price) - cond.stop)
                    if (cond.target - cond.entry) * (1.0 if cond.direction == "up" else -1.0) <= cond.risk + 1e-6
                    else cond.target),
            tick=cond.tick,
            multiplier=cond.multiplier,
            open_order_ids=list(order_ids or []),
            open_commission=float(commission),
            pending_exit_reason=("stop" if cond.reject_reason == "stop_before_entry" or
                                 (float(fill_price) - cond.stop) * (1 if cond.direction == "up" else -1) <= 0 else
                                 "market_gap" if cond.reject_reason == "market_gap" else ""),
        )
        self.positions[pos_id] = pos
        self.active_positions[pos_id] = pos
        self._positions_by_actual.setdefault(pos.actual_symbol, {})[pos_id] = pos
        self.event(fill_ts, "open_filled", cond_id, pos_id=pos_id, price=fill_price, volume=filled_volume,
                   planned_risk=cond.risk, risk=pos.risk, target=pos.target, order_ids=pos.open_order_ids)
        return pos

    def on_open_rejected(self, cond_id: str, reason: str, ts: str = "", *, cancelled: bool = False) -> None:
        cond = self.conditions.get(cond_id)
        if cond is not None:
            # A failed/cancelled N-pattern entry remains authorized until expiry/stop.
            if cond.state not in ("expired", "cancelled"):
                cond.state = "waiting"
            cond.retry_after = quote_timestamp(ts) + self.cfg.action_interval_seconds
            if ("资金不足" in reason or "[31]" in reason or reason.startswith("31:")
                    or "insufficient" in reason.lower()):
                available, margin, updated = self.funds or (0.0, 0.0, quote_timestamp(ts))
                cond.funding_available, cond.funding_margin, cond.funding_updated_at = available, margin, updated
                cond.funding_frozen_margin, cond.funding_revision = self.frozen_margin, self.funds_revision
                match = re.search(r"资金\[([0-9]+(?:\.[0-9]+)?)\]", reason)
                cond.funding_required = max(available, 0.0) + float(match[1]) + 0.01 if match else None
            cond.last_order_message = reason
            if cond.state not in ("expired", "cancelled"):
                cond.reject_reason = reason
            self.analytics.inc_counter(cond.logical_symbol, "entry_order_cancelled" if cancelled else "cond_rejected_entry")
            if not cancelled:
                self.analytics.broker_rejections.append({
                    "ts": ts,
                    "cond_id": cond_id,
                    "logical_symbol": cond.logical_symbol,
                    "actual_symbol": cond.actual_symbol,
                    "lots": cond.lots,
                    "reason": reason,
                })
            self.event(ts, "entry_wait", cond_id, reason=reason, retry_after=cond.retry_after,
                       funding_required=cond.funding_required)

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
        r_multiple = price_diff / pos.management_risk
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
            "actual_risk": pos.management_risk,
            "planned_r_multiple": price_diff / max(pos.planned_risk, 1e-9),
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
        self.event(fill_ts, "close_filled", pos_id, reason=reason, price=fill_price, volume=actual_closed,
                   risk=pos.management_risk, order_ids=list(order_ids or []))

        pos.remaining_lots -= actual_closed
        if pos.remaining_lots <= 0:
            self.active_positions.pop(pos.pos_id, None)
            self._positions_by_actual.get(pos.actual_symbol, {}).pop(pos.pos_id, None)
            cond = self.conditions.get(pos.cond_id)
            if cond is not None:
                cond.state = "closed"
        elif is_lot1:
            # Protection lock: move stop to breakeven only after confirmed Lot 1 close fill
            pos.lot1_closed = True
            pos.pending_lot1_exit = False
            pos.current_stop = max(pos.current_stop, pos.open_fill_price) if sign > 0 else min(
                pos.current_stop, pos.open_fill_price
            )
            pos.current_stop = continuous_stop_price(
                pos.open_fill_price,
                pos.management_risk,
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
    submitted_at: float = 0.0
    cancel_sent: bool = False
    funds_at_submit: Optional[tuple[float, float, float]] = None
    frozen_margin_at_submit: Optional[float] = None
    funds_revision_at_submit: int = 0


def _order_is_finished(order: Any) -> bool:
    return getattr(order, "status", "") == "FINISHED"


def _submit_instruction_orders(
    api: Any,
    instr: OrderInstruction,
    quote_obj: Any,
    slippage_ticks: int,
    reference: Optional[float] = None,
    max_ticks: int = 0,
) -> list[Any]:
    raw_lp = float(getattr(quote_obj, "last_price", instr.trigger_price))
    safe_lp = raw_lp if math.isfinite(raw_lp) and raw_lp > 0 else instr.trigger_price
    q_tick = QuoteTick(
        symbol=instr.actual_symbol,
        datetime=str(getattr(quote_obj, "datetime", "")),
        last_price=safe_lp,
        ask_price1=float(getattr(quote_obj, "ask_price1", float("nan"))),
        bid_price1=float(getattr(quote_obj, "bid_price1", float("nan"))),
        upper_limit=float(getattr(quote_obj, "upper_limit", float("nan"))),
        lower_limit=float(getattr(quote_obj, "lower_limit", float("nan"))),
        price_tick=float(getattr(quote_obj, "price_tick", 1.0) or 1.0),
        volume_multiple=float(getattr(quote_obj, "volume_multiple", 1.0) or 1.0),
    )
    limit_price = compute_slippage_limit_price(instr.direction, q_tick, slippage_ticks, reference, max_ticks)

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
        records = getattr(o, "trade_records", {}) or {}
        if records:
            total_time = max(int(getattr(tr, "trade_date_time", 0)) for tr in records.values())
            if total_time:
                # Preserve microseconds for event ordering and action throttling.
                last_ts = datetime.fromtimestamp(total_time / 1e9, tz=timezone(timedelta(hours=8))).isoformat(sep=" ")
    avg_price = (weighted_price_sum / total_filled) if total_filled > 0 else 0.0
    return total_filled, avg_price, last_ts, order_ids, last_msg


class TqOrderAdapter:
    """Cancel-confirm-replace and account waits around the pure strategy engine.

    TqSim fills an individual order atomically. Split SHFE close groups can still
    finish with one failed leg; only their confirmed volume is applied.
    """
    def __init__(self, engine: NPatternBacktestEngine, api: Any, quotes: dict[str, Any]):
        self.engine, self.api, self.quotes = engine, api, quotes
        self.pending: list[_PendingBrokerGroup] = []
        self.queue: list[OrderInstruction] = []
        self.last_action = -math.inf

    def enqueue(self, instructions: list[OrderInstruction]) -> None:
        for instr in instructions:
            if instr.intent == "close" and instr.closing_all:
                pos = self.engine.active_positions.get(instr.ref_id)
                if pos:
                    pos.pending_exit_reason = pos.pending_exit_reason or instr.reason
            existing = next((i for i in self.queue if i.intent == instr.intent and i.ref_id == instr.ref_id), None)
            if existing is None:
                self.queue.append(instr)
            elif instr.intent == "close" and instr.closing_all:
                self.queue[self.queue.index(existing)] = instr

    def valid_quote(self, symbol: str, ts: str, now_epoch: Optional[float] = None) -> Optional[QuoteTick]:
        q = self.quotes.get(symbol)
        if q is None:
            return None
        try:
            now = now_epoch if now_epoch is not None else quote_timestamp(ts)
            at = quote_timestamp(str(q.datetime))
            if not 0 <= now - at <= 15 or at < self.engine.quote_floor:
                return None
            last, tick = float(q.last_price), float(q.price_tick)
            if not all(math.isfinite(value) and value > 0 for value in (last, tick)):
                return None
        except (AttributeError, TypeError, ValueError, OverflowError):
            return None
        def number(field: str) -> float:
            try:
                return float(getattr(q, field, float('nan')))
            except (ValueError, TypeError):
                return float('nan')
        return QuoteTick(symbol, ts, last, number('ask_price1'), number('bid_price1'),
                         number('upper_limit'), number('lower_limit'), price_tick=tick)

    def sync_exit_tasks(self) -> None:
        if not self.queue:
            return
        for index, instr in enumerate(self.queue):
            if instr.intent != "close":
                continue
            pos = self.engine.active_positions.get(instr.ref_id)
            if pos and pos.pending_exit_reason:
                self.queue[index] = dataclasses.replace(instr, reason=pos.pending_exit_reason,
                    volume=pos.remaining_lots, is_lot1=False, closing_all=True,
                    trigger_price=pos.initial_stop if pos.pending_exit_reason == "stop" else instr.trigger_price)

    def reconcile(self, ts: str) -> bool:
        retained = []
        changed = False
        for group in self.pending:
            if not all(_order_is_finished(o) for o in group.orders):
                retained.append(group)
                continue
            volume, price, at, ids, message = _extract_group_fill(group, ts)
            changed = True
            instr = group.instruction
            if instr.intent == "open":
                if volume:
                    self.engine.on_open_filled(instr.ref_id, volume, price, at, ids)
                else:
                    current_funds = self.engine.funds
                    current_frozen, current_revision = self.engine.frozen_margin, self.engine.funds_revision
                    self.engine.funds = group.funds_at_submit
                    self.engine.frozen_margin, self.engine.funds_revision = group.frozen_margin_at_submit, group.funds_revision_at_submit
                    cancelled = group.cancel_sent or "撤单" in message or "cancel" in message.lower()
                    self.engine.on_open_rejected(instr.ref_id, message or "unfilled", at, cancelled=cancelled)
                    self.engine.funds = current_funds
                    self.engine.frozen_margin, self.engine.funds_revision = current_frozen, current_revision
            else:
                if volume:
                    self.engine.on_close_filled(instr.ref_id, volume, price, at, instr.reason, instr.is_lot1, ids)
                # A cancelled/rejected remainder may be retried only now, after terminal confirmation.
                self.engine.on_close_rejected(instr.ref_id)
            self.engine.event(ts, "order_terminal", instr.ref_id, intent=instr.intent, volume=volume,
                              message=message, order_ids=ids)
        self.pending = retained
        return changed

    def _target_symbols(self, include_all_active_conditions: bool) -> set[str]:
        targets: set[str] = {g.instruction.actual_symbol for g in self.pending}
        targets.update(i.actual_symbol for i in self.queue)
        for sym, pos_map in self.engine._positions_by_actual.items():
            if pos_map:
                targets.add(sym)
        for sym, conds in self.engine._conditions_by_actual.items():
            if include_all_active_conditions:
                if any(c.is_active for c in conds):
                    targets.add(sym)
            elif any(c.state == "waiting" for c in conds):
                targets.add(sym)
        return targets

    def drive(
        self,
        ts: str,
        already_evaluated: Optional[set[str]] = None,
        now_epoch: Optional[float] = None,
    ) -> None:
        target_syms = self._target_symbols(include_all_active_conditions=already_evaluated is None)
        if not target_syms:
            return
        now = now_epoch if now_epoch is not None else quote_timestamp(ts)
        cfg = self.engine.cfg
        # Re-evaluate authorized retries and latched exits only for symbols with
        # active/waiting/pending state, avoiding O(N_symbols) work per idle tick.
        valid_quotes = {
            symbol: qt
            for symbol in target_syms
            if (qt := self.valid_quote(symbol, ts, now)) is not None
        }
        for sym, qt in valid_quotes.items():
            if already_evaluated is None or sym not in already_evaluated:
                self.enqueue(self.engine.on_quote(qt))
        self.sync_exit_tasks()
        if now - self.last_action < cfg.action_interval_seconds:
            return
        for group in sorted(self.pending, key=lambda g: g.instruction.intent == "open"):
            if group.cancel_sent:
                continue
            instr = group.instruction
            qt = valid_quotes.get(instr.actual_symbol)
            cond = self.engine.conditions.get(instr.ref_id) if instr.intent == "open" else None
            aborted = cond is not None and cond.state in ("cancelled", "expired")
            if cond and qt and (qt.last_price - cond.stop) * (1 if cond.direction == "up" else -1) <= 0:
                cond.state, cond.reject_reason = "cancelled", "stop_before_entry"
                aborted = True
            candidate = (compute_slippage_limit_price(instr.direction, qt, min(20, cfg.slippage_ticks)) if qt else None)
            reprice = candidate is not None and any((candidate - float(getattr(o, "limit_price", candidate))) *
                (1 if instr.direction == "BUY" else -1) > 0 for o in group.orders if not _order_is_finished(o))
            pos = self.engine.active_positions.get(instr.ref_id) if instr.intent == "close" else None
            upgrade = pos is not None and pos.pending_exit_reason and not instr.closing_all
            if aborted or upgrade or (now - group.submitted_at >= max(2, cfg.order_wait_seconds) and reprice):
                for order in group.orders:
                    if not _order_is_finished(order):
                        self.api.cancel_order(order)
                group.cancel_sent = True
                self.last_action = now
                self.engine.event(ts, "cancel_requested", instr.ref_id,
                                  reason="aborted" if aborted else "exit_upgrade" if upgrade else "reprice")
                return
        self.queue.sort(key=lambda i: (i.intent == "open", self.engine.conditions.get(i.ref_id).warning_ts
                        if i.intent == "open" else self.engine.positions[i.ref_id].open_ts, i.ref_id))
        for instr in list(self.queue):
            q = self.quotes[instr.actual_symbol]
            reference, max_ticks, attempts = instr.trigger_price, 0, 0
            if instr.intent == "open":
                cond = self.engine.conditions[instr.ref_id]
                if cond.state in ("cancelled", "expired", "rejected"):
                    self.queue.remove(instr)
                    continue
                if self.engine.cfg.max_positions is not None:
                    committed = len(self.engine.active_positions) + sum(
                        1 for g in self.pending if g.instruction.intent == "open" and not g.cancel_sent
                    )
                    if committed >= self.engine.cfg.max_positions:
                        self.queue.remove(instr)
                        cond.state = "cancelled"
                        cond.reject_reason = "max_positions_reached"
                        self.engine.analytics.inc_counter(cond.logical_symbol, "max_positions_blocked")
                        self.engine.event(ts, "entry_cancelled", cond.cond_id, reason="max_positions_reached")
                        continue
                qt = valid_quotes.get(instr.actual_symbol)
                if qt is None:
                    continue
                if not self.engine.entry_ready(cond, qt):
                    continue
                reference = cond.entry
                # Rust f64::round rounds halves away from zero.
                max_ticks = int(math.floor(max(cond.risk * .2, 2 * qt.price_tick) / qt.price_tick + .5))
            else:
                pos = self.engine.active_positions.get(instr.ref_id)
                if pos is None:
                    self.queue.remove(instr)
                    continue
                if instr.actual_symbol not in valid_quotes:
                    continue
                instr.volume = min(instr.volume, pos.remaining_lots)
                reference, max_ticks, attempts = instr.trigger_price, cfg.max_exit_slippage_ticks, pos.close_attempts
            qt = valid_quotes[instr.actual_symbol]
            orders = _submit_instruction_orders(self.api, instr, qt, min(20, cfg.slippage_ticks + attempts), reference, max_ticks)
            self.queue.remove(instr)
            if not orders:
                if instr.intent == "open":
                    self.engine.on_open_rejected(instr.ref_id, "no orders", ts)
                else:
                    self.engine.on_close_rejected(instr.ref_id)
                continue
            self.pending.append(_PendingBrokerGroup(instr, orders, now, funds_at_submit=self.engine.funds,
                frozen_margin_at_submit=self.engine.frozen_margin, funds_revision_at_submit=self.engine.funds_revision))
            if instr.intent == "close":
                pos.close_attempts += 1
            self.last_action = now
            self.engine.event(ts, "order_submitted", instr.ref_id, intent=instr.intent, direction=instr.direction,
                              volume=instr.volume, order_ids=[str(o.order_id) for o in orders],
                              limit_prices=[float(o.limit_price) for o in orders], reference=reference, max_ticks=max_ticks,
                              quote={"datetime": str(q.datetime), "last": qt.last_price,
                                     "ask": qt.ask_price1, "bid": qt.bid_price1})
            return


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
        intraday: dict[str, float] = {}
        for tr in sim_trades:
            oid = str(tr.get("order_id", ""))
            if oid:
                intraday[oid] = intraday.get(oid, 0.0) + float(tr.get("commission", 0.0) or 0.0)
        for oid, amount in intraday.items():
            if oid not in comm_by_order:
                comm_by_order[oid] = amount
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
    bj_zone = timezone(timedelta(hours=8))
    start_dt = start_dt.astimezone(bj_zone).replace(tzinfo=None) if start_dt.tzinfo else start_dt
    end_dt = end_dt.astimezone(bj_zone).replace(tzinfo=None) if end_dt.tzinfo else end_dt
    if start_dt >= end_dt:
        raise ValueError("Backtest end must be after start")
    start_bj_str = start_dt.strftime("%Y-%m-%d %H:%M:%S")
    gap_events = []
    if cfg.market_gap_events:
        for line in Path(cfg.market_gap_events).read_text(encoding="utf-8-sig").splitlines():
            if line.strip():
                event = json.loads(line)
                at = quote_timestamp(event["datetime"])
                if quote_timestamp(start_bj_str) <= at <= quote_timestamp(end_dt.isoformat(sep=" ")):
                    gap_events.append((at, event["datetime"]))
        gap_events.sort()
    next_gap = 0

    worker = SignalWorkerClient(cfg.worker_path)
    worker.start()
    engine = NPatternBacktestEngine(cfg, worker=worker)
    try:
        sim = TqSim(init_balance=cfg.init_balance)
        api = TqApi(
            sim,
            backtest=TqBacktest(start_dt=start_dt, end_dt=end_dt),
            auth=TqAuth(acc, pwd),
            web_gui=cfg.web_gui,
            disable_print=cfg.quiet,
        )
    except Exception:
        worker.close()
        raise

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
        actual_serials: dict[str, Any] = {}
        adapter = TqOrderAdapter(engine, api, actual_quotes)

        def ensure_actual_subscribed(actual_sym: str) -> None:
            if not actual_sym or actual_sym in actual_quotes:
                return
            if actual_sym.startswith("KQ."):
                return  # Never trade/subscribe execution ticks on a continuous placeholder.
            q_obj = api.get_quote(actual_sym)
            actual_quotes[actual_sym] = q_obj
            actual_serials[actual_sym] = (api.get_tick_serial(actual_sym, data_length=5)
                if cfg.quote_mode == "tick" else api.get_kline_serial(actual_sym, 60, data_length=5))

        for logical, tq_sym in symbol_specs:
            tq_sym_map[logical] = tq_sym
            cont_q = api.get_quote(tq_sym)
            cont_quotes[logical] = cont_q
            klines_map[logical] = api.get_kline_serial(tq_sym, cfg.bar_seconds, data_length=kline_len)
            init_actual = (getattr(cont_q, "underlying_symbol", "") or "").strip() or tq_sym
            last_underlying[logical] = init_actual
            ensure_actual_subscribed(init_actual)

        acct_obj = api.get_account()
        hold_after_finish = False
        end_ns = int(quote_timestamp(end_dt.isoformat(sep=" ")) * 1e9)
        run_t0 = _time.perf_counter()
        update_count = 0
        last_sim_ns = -1
        last_sim_ts = start_bj_str
        last_sim_epoch = quote_timestamp(start_bj_str)
        try:
            import psutil as _psutil
            _proc = _psutil.Process()
        except Exception:
            _proc = None

        def current_sim_ns() -> int:
            # SDK's event clock also advances for Kline CLOSE events without a tick.
            data = getattr(api, "_data", {})
            clock = data.get("_tqsdk_backtest", {}).get("current_dt", 0)
            if clock:
                # SDK advances its clock to a 2038 sentinel when exhausted.
                # It must not become a fill/equity timestamp in our final report.
                return min(int(clock), end_ns)
            return max([int(quote_timestamp(str(getattr(q, "datetime", ""))) * 1e9)
                for q in list(actual_quotes.values()) + list(cont_quotes.values()) if getattr(q, "datetime", "")]
                or [int(quote_timestamp(start_bj_str) * 1e9)])

        def current_sim_clock() -> tuple[int, str, float]:
            nonlocal last_sim_ns, last_sim_ts, last_sim_epoch
            now_ns = current_sim_ns()
            if now_ns != last_sim_ns:
                last_sim_ns = now_ns
                last_sim_epoch = now_ns / 1e9
                last_sim_ts = datetime.fromtimestamp(last_sim_epoch, tz=timezone(timedelta(hours=8))).isoformat(sep=" ")
            return now_ns, last_sim_ts, last_sim_epoch

        def current_sim_ts() -> str:
            return current_sim_clock()[1]

        def dispatch_instructions(instrs: list[OrderInstruction]) -> None:
            for instr in instrs:
                if instr.actual_symbol not in actual_quotes:
                    ensure_actual_subscribed(instr.actual_symbol)
            adapter.enqueue(instrs)

        if not cfg.quiet:
            print(f"[backtest] Entering {cfg.quote_mode} event loop ({len(symbol_specs)} symbols)...", flush=True)
            if cfg.web_gui:
                print(f"[web_gui] Web UI enabled (web_gui={cfg.web_gui}); see TQSDK URL output above.", flush=True)

        while True:
            api.wait_update()
            update_count += 1

            sim_ns, sim_ts, sim_epoch = current_sim_clock()
            sim_day = sim_ts[:10]
            if not cfg.quiet and sim_day and sim_day != last_progress_day and initialized_syms:
                last_progress_day = sim_day
                elapsed = _time.perf_counter() - run_t0
                mem_part = f" | rss={_proc.memory_info().rss / (1024 * 1024):.0f}MB" if _proc is not None else ""
                print(
                    f"[progress] {sim_day} | eq={last_equity:,.2f} | "
                    f"legs={len(engine.analytics.exit_legs)} | open={len(engine.active_positions)} | "
                    f"risk_ev={len(engine.analytics.risk_events)} | rej={len(engine.analytics.broker_rejections)} | "
                    f"upd={update_count:,} | {elapsed:.1f}s{mem_part}",
                    flush=True,
                )

            eq = float(getattr(acct_obj, "balance", last_equity))
            if math.isfinite(eq):
                last_equity = eq
                engine.analytics.record_equity(sim_ts, eq)
            comm_now = float(getattr(acct_obj, "commission", 0.0) or 0.0)
            if math.isfinite(comm_now):
                last_commission_reported = comm_now

            engine.update_funds(float(getattr(acct_obj, "available", float('nan'))),
                                float(getattr(acct_obj, "margin", float('nan'))), sim_epoch,
                                float(getattr(acct_obj, "frozen_margin", float('nan'))))
            broker_changed = adapter.reconcile(sim_ts)
            while next_gap < len(gap_events) and gap_events[next_gap][0] <= sim_epoch:
                engine.on_market_gap(gap_events[next_gap][1],
                    [g.instruction.ref_id for g in adapter.pending if g.instruction.intent == "open"])
                next_gap += 1

            # 2. Check rollover transitions on continuous contracts
            for logical, cont_q in cont_quotes.items():
                cur_actual = (getattr(cont_q, "underlying_symbol", "") or "").strip() or tq_sym_map[logical]
                if cur_actual not in actual_quotes:
                    ensure_actual_subscribed(cur_actual)
                prev_actual = last_underlying.get(logical, cur_actual)
                if cur_actual != prev_actual:
                    last_underlying[logical] = cur_actual
                    rollover_pending[logical] = True
                    cancelled = engine.cancel_pending_for_rollover(logical, reason="rollover")
                    if not cfg.quiet:
                        print(
                            f"[rollover] {logical} {prev_actual} -> {cur_actual} "
                            f"(cancelled_pending={len(cancelled)})",
                            flush=True,
                        )

            # 3. Check quote updates on subscribed actual contracts
            changed_actuals: set[str] = set()
            for actual_sym, q_obj in list(actual_quotes.items()):
                if not broker_changed and not api.is_changing(q_obj, ["datetime", "last_price", "ask_price1", "bid_price1"]):
                    continue
                lp = float(getattr(q_obj, "last_price", float("nan")))
                if not math.isfinite(lp) or lp <= 0:
                    continue
                q_dt_str = str(getattr(q_obj, "datetime", "") or "")
                latest_prices[actual_sym] = (lp, (q_dt_str or sim_ts)[:19])
                q_epoch = quote_timestamp(q_dt_str)
                if not 0 <= sim_epoch - q_epoch <= 15 or q_epoch < engine.quote_floor:
                    continue
                changed_actuals.add(actual_sym)
                q_tick = QuoteTick(
                    symbol=actual_sym,
                    datetime=sim_ts,
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
            bar_signal_actuals: set[str] = set()
            for logical, df in klines_map.items():
                if df is None or len(df) < 1:
                    continue
                if logical in initialized_syms and not api.is_changing(df):
                    continue
                completed = completed_tq_bars(df, current_sim_ns(), cfg.bar_seconds)
                if len(completed) == 0:
                    continue
                cur_last_id = int(completed.iloc[-1]["id"])

                tq_sym = tq_sym_map[logical]
                actual_sym = last_underlying.get(logical, tq_sym)
                if actual_sym not in actual_quotes:
                    continue
                q_meta = actual_quotes.get(actual_sym) or cont_quotes[logical]
                tq_tick = float(getattr(q_meta, "price_tick", 0.0) or getattr(cont_quotes[logical], "price_tick", 0.0) or 0.0)
                tq_mult = float(getattr(q_meta, "volume_multiple", 0.0) or getattr(cont_quotes[logical], "volume_multiple", 0.0) or 0.0)
                try:
                    tick = positive_float(tq_tick, getattr(cont_quotes[logical], "price_tick", 0))
                    multiplier = positive_float(tq_mult, getattr(cont_quotes[logical], "volume_multiple", 0))
                except ValueError:
                    continue  # Await valid exchange metadata rather than use stale static multipliers.

                def step_rows(rows_iter: Any, eff_tick: float, eff_mult: float) -> None:
                    for _, row in rows_iter:
                        ro = rollover_pending.pop(logical, False)
                        bar = _kline_row_to_bar(row, ro, cfg.bar_seconds)
                        instrs, created = engine.on_bar_closed(
                            logical_symbol=logical,
                            bar=bar,
                            actual_symbol=actual_sym,
                            tick=eff_tick,
                            multiplier=eff_mult,
                            current_equity=last_equity,
                        )
                        if created or instrs:
                            bar_signal_actuals.add(actual_sym)
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
                    w_tick = float(resp.get("tick", 0.0) or 0.0)
                    w_mult = float(resp.get("multiplier", 0.0) or 0.0)
                    eff_tick = positive_float(tq_tick, w_tick)
                    eff_mult = multiplier
                    if (not cfg.quiet) and w_mult > 0 and abs(eff_mult - w_mult) > 1e-6:
                        print(f"[warn] {logical} multiplier mismatch: worker={w_mult}, tqsdk={eff_mult} (using tqsdk={eff_mult})", flush=True)
                    sym_meta[logical] = (eff_tick, eff_mult)
                    initialized_syms.add(logical)
                    last_closed_id[logical] = cur_last_id
                    if active_rows:
                        step_rows(active_rows, *sym_meta[logical])
                    continue

                prev_id = last_closed_id.get(logical, -1)
                if cur_last_id <= prev_id:
                    continue

                new_rows = completed[completed["id"] > prev_id]
                eff_tick, eff_mult = tick, multiplier
                step_rows(new_rows.iterrows(), eff_tick, eff_mult)
                last_closed_id[logical] = cur_last_id

            adapter.drive(
                sim_ts,
                already_evaluated=changed_actuals - bar_signal_actuals,
                now_epoch=sim_epoch,
            )

    except BacktestFinished:
        # Account/order updates delivered with the final event must not be lost.
        final_ts = current_sim_ts()
        adapter.reconcile(final_ts)
        final_balance = float(getattr(acct_obj, "balance", last_equity))
        if math.isfinite(final_balance):
            last_equity = final_balance
            engine.analytics.record_equity(final_ts, final_balance)
        if not cfg.quiet:
            elapsed = _time.perf_counter() - run_t0
            print(f"[backtest] TqBacktest finished in {elapsed:.1f}s ({update_count:,} updates).", flush=True)
        # Refresh latest prices from final quote snapshot before building report
        for pos in engine.active_positions.values():
            q_obj = actual_quotes.get(pos.actual_symbol)
            if q_obj is not None:
                lp = float(getattr(q_obj, "last_price", float("nan")))
                if math.isfinite(lp) and lp > 0:
                    latest_prices[pos.actual_symbol] = (lp, str(getattr(q_obj, "datetime", "") or final_ts)[:19])
        hold_after_finish = bool(cfg.web_gui and cfg.hold_web)
    finally:
        if not hold_after_finish:
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
    report["events"] = engine.events
    report["conditions"] = [dataclasses.asdict(c) for c in engine.conditions.values()]
    report["unresolved_orders"] = [{"ref_id": g.instruction.ref_id, "intent": g.instruction.intent,
        "cancel_sent": g.cancel_sent, "order_ids": [str(o.order_id) for o in g.orders]} for g in adapter.pending]
    report["queued_instructions"] = [dataclasses.asdict(i) for i in adapter.queue]
    account_report = report["account"]
    accounting_difference = account_report["total_return_yuan"] - account_report["reconciled_strategy_net_yuan"]
    report["validation"] = {
        "accounting_difference_yuan": accounting_difference,
        "accounting_reconciled": abs(accounting_difference) <= 0.02,
        "no_unresolved_broker_orders": not adapter.pending,
        "tick_replay": cfg.quote_mode == "tick",
        "historical_live_equivalence_verified": False,
    }
    report["meta"] = {
        "strategy": cfg.strategy,
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
        "quote_mode": cfg.quote_mode,
        "order_wait_seconds": cfg.order_wait_seconds,
        "action_interval_seconds": cfg.action_interval_seconds,
        "max_exit_slippage_ticks": cfg.max_exit_slippage_ticks,
        "large_volume": cfg.large_volume,
        "small_volume": cfg.small_volume,
        "one_per_symbol": cfg.one_per_symbol,
        "max_positions": cfg.max_positions,
        "market_gap_events": cfg.market_gap_events,
        "market_gap_events_replayed": next_gap,
        "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "timezone": "Asia/Shanghai",
        "execution_model": "n_pattern_full_auto_tqsim_v3",
        "funding_recovery": "held_plus_frozen_margin_release_v1",
        "live_exit_policy": "n_exit_v1",
        "tqsdk_version": str(getattr(sys.modules.get("tqsdk"), "__version__", "unknown")),
        "worker_sha256": (hashlib.sha256(Path(worker.binary_path).read_bytes()).hexdigest()
                          if getattr(worker, "binary_path", None) else None),
        "limitations": ["TqSim atomic fills; no CTP queue/latency or historical disconnect events",
                        "TQ historical data and warmup may differ from the live signal database"],
    }
    if hold_after_finish:
        try:
            print_report(report)
            if cfg.export_dir:
                out = export_report(report, cfg.export_dir)
                print(f"[backtest] Reports exported to: {out}", flush=True)
            print("[web_gui] Backtest finished. Open TQSDK Web UI URL above (or report.html); press Ctrl+C to exit.", flush=True)
            while True:
                api.wait_update()
        except KeyboardInterrupt:
            pass
        finally:
            try:
                api.close()
            except Exception:
                pass
            worker.close()
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
    if report.get("validation"):
        check = report['validation']
        print(f"Validation      : clock={report.get('meta', {}).get('quote_mode')} | "
              f"account diff={check['accounting_difference_yuan']:+.4f} | "
              f"unresolved orders={len(report.get('unresolved_orders', []))}")

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
        "unresolved_orders": report.get("unresolved_orders", []),
        "queued_instructions": report.get("queued_instructions", []),
        "validation": report.get("validation", {}),
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary_payload, ensure_ascii=False, indent=2, default=json_scalar),
        encoding="utf-8",
    )

    _write_csv(out_dir / "trades.csv", report["trades"])
    _write_csv(out_dir / "legs.csv", report["legs"])
    _write_csv(out_dir / "risk_events.csv", report["risk_events"])
    (out_dir / "conditions.json").write_text(json.dumps(report.get("conditions", []), ensure_ascii=False, indent=2, default=json_scalar), encoding="utf-8")
    with (out_dir / "events.jsonl").open("w", encoding="utf-8") as stream:
        for event in report.get("events", []):
            stream.write(json.dumps(event, ensure_ascii=False, default=json_scalar) + "\n")

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
    (out_dir / "report.html").write_text(render_html_report(report), encoding="utf-8")
    return out_dir


def _downsample_equity_curve(points: list[Any], max_points: int = 1200) -> list[tuple[str, float, float]]:
    if not points:
        return []
    parsed = [(str(p[0]), float(p[1]), float(p[2])) for p in points if len(p) >= 3]
    if len(parsed) <= max_points:
        return parsed
    step = len(parsed) / max_points
    sampled = [parsed[int(i * step)] for i in range(max_points - 1)]
    sampled.append(parsed[-1])
    return sampled


def render_html_report(report: dict[str, Any]) -> str:
    """Render a self-contained offline HTML dashboard with SVG equity curve & trade tables."""
    acc = report.get("account", {})
    ov = report.get("overall", {})
    meta = report.get("meta", {})
    val = report.get("validation", {})
    curve = _downsample_equity_curve(report.get("equity_curve", []))

    svg_chart = "<div class='empty'>暂无净值曲线数据</div>"
    if len(curve) >= 2:
        w, h, pad = 960, 260, 28
        equities = [p[1] for p in curve]
        mn, mx = min(equities), max(equities)
        span = max(mx - mn, 1.0)
        coords = []
        dd_coords = []
        max_dd = max((p[2] for p in curve), default=0.0) or 0.01
        for i, (_, eq, dd_pct) in enumerate(curve):
            x = pad + (w - 2 * pad) * (i / (len(curve) - 1))
            y = h - pad - ((eq - mn) / span) * (h - 2 * pad)
            coords.append(f"{x:.1f},{y:.1f}")
            dd_y = pad + (dd_pct / max_dd) * (h - 2 * pad) * 0.35
            dd_coords.append(f"{x:.1f},{dd_y:.1f}")
        area_pts = f"{pad:.1f},{h - pad:.1f} " + " ".join(coords) + f" {w - pad:.1f},{h - pad:.1f}"
        init_bal = float(acc.get("init_balance", equities[0]) or equities[0])
        base_y = h - pad - ((max(mn, min(mx, init_bal)) - mn) / span) * (h - 2 * pad)
        svg_chart = (
            f"<svg viewBox='0 0 {w} {h}' class='chart'>"
            f"<line x1='{pad}' y1='{base_y:.1f}' x2='{w - pad}' y2='{base_y:.1f}' stroke='#475569' stroke-dasharray='4,4'/>"
            f"<polygon points='{area_pts}' fill='rgba(56,189,248,0.14)'/>"
            f"<polyline fill='none' stroke='#38bdf8' stroke-width='2' points='{' '.join(coords)}'/>"
            f"<polyline fill='none' stroke='rgba(248,113,113,0.65)' stroke-width='1.2' points='{' '.join(dd_coords)}'/>"
            f"<text x='{pad}' y='18' fill='#94a3b8' font-size='12'>Max: {mx:,.2f} | Min: {mn:,.2f} | Start: {_html.escape(curve[0][0][:16])} ~ End: {_html.escape(curve[-1][0][:16])}</text>"
            f"</svg>"
        )

    sym_rows_html = []
    for _, entry in report.get("by_symbol", {}).items():
        net = float(entry.get("net_pnl", 0.0) or 0.0)
        cls = "pos" if net > 0 else "neg" if net < 0 else ""
        lng = entry.get("long", {})
        sht = entry.get("short", {})
        sym_rows_html.append(
            f"<tr><td>{_html.escape(str(entry.get('display_name', entry.get('symbol', ''))))}</td>"
            f"<td>{int(entry.get('trades', 0))}</td>"
            f"<td>{float(entry.get('win_rate', 0.0)) * 100:.1f}%</td>"
            f"<td>{_html.escape(_fmt_pf(float(entry.get('profit_factor', 0.0))))}</td>"
            f"<td>{float(entry.get('avg_r', 0.0)):+.2f}R</td>"
            f"<td class='{cls}'>{net:+,.2f}</td>"
            f"<td>{float(entry.get('commission', 0.0)):,.2f}</td>"
            f"<td>{int(lng.get('trades', 0))} / {float(lng.get('net_pnl', 0.0)):+,.0f}</td>"
            f"<td>{int(sht.get('trades', 0))} / {float(sht.get('net_pnl', 0.0)):+,.0f}</td></tr>"
        )

    trade_rows_html = []
    for tr in report.get("trades", []):
        net = float(tr.get("net_pnl", 0.0) or 0.0)
        cls = "pos" if net > 0 else "neg" if net < 0 else ""
        trade_rows_html.append(
            f"<tr data-sym='{_html.escape(str(tr.get('logical_symbol', '')))}' data-res='{'win' if net > 0 else 'loss'}'>"
            f"<td>{_html.escape(str(tr.get('pos_id', '')))}</td>"
            f"<td>{_html.escape(str(tr.get('logical_symbol', '')))} ({_html.escape(str(tr.get('actual_symbol', '')))})</td>"
            f"<td>{_html.escape(str(tr.get('direction', '')))}</td>"
            f"<td>{int(tr.get('lots', 0))}</td>"
            f"<td>{_html.escape(str(tr.get('open_ts', ''))[:19])}</td>"
            f"<td>{float(tr.get('open_price', 0.0)):.2f}</td>"
            f"<td>{_html.escape(str(tr.get('close_ts', ''))[:19])}</td>"
            f"<td>{float(tr.get('avg_close_price', 0.0)):.2f}</td>"
            f"<td>{_html.escape(str(tr.get('exit_reasons', '')))}</td>"
            f"<td>{float(tr.get('net_r', 0.0)):+.2f}R</td>"
            f"<td class='{cls}'>{net:+,.2f}</td></tr>"
        )

    reasons_html = " ".join(
        f"<span class='badge'>{_html.escape(str(k))}: {int(v)}</span>"
        for k, v in sorted(report.get("exit_reasons", {}).items())
    )
    ret_yuan = float(acc.get("total_return_yuan", 0.0) or 0.0)
    ret_cls = "pos" if ret_yuan > 0 else "neg" if ret_yuan < 0 else ""
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<title>N-Pattern TQSDK Backtest Report</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#0f172a;color:#e2e8f0;margin:0;padding:24px;}}
h1,h2{{margin:0 0 12px 0;font-weight:600;}} .sub{{color:#94a3b8;font-size:13px;margin-bottom:20px;}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(175px,1fr));gap:12px;margin-bottom:20px;}}
.card{{background:#1e293b;border:1px solid #334155;border-radius:10px;padding:12px 14px;}}
.k{{color:#94a3b8;font-size:12px;}} .v{{font-size:20px;font-weight:700;margin-top:4px;}}
.pos{{color:#34d399;}} .neg{{color:#f87171;}}
.panel{{background:#1e293b;border:1px solid #334155;border-radius:10px;padding:16px;margin-bottom:20px;overflow-x:auto;}}
.chart{{width:100%;height:auto;display:block;background:#0b1220;border-radius:8px;}}
.badge{{display:inline-block;background:#334155;border-radius:999px;padding:4px 10px;font-size:12px;margin:2px 6px 2px 0;}}
table{{width:100%;border-collapse:collapse;font-size:13px;}}
th,td{{padding:8px 10px;border-bottom:1px solid #334155;text-align:right;white-space:nowrap;}}
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){{text-align:left;}}
th{{color:#94a3b8;font-weight:600;position:sticky;top:0;background:#1e293b;}}
input,select{{background:#0f172a;color:#e2e8f0;border:1px solid #475569;border-radius:6px;padding:5px 8px;margin-right:8px;}}
.empty{{color:#64748b;padding:24px;text-align:center;}}
</style>
</head>
<body>
<h1>N-Pattern Strategy TQSDK Backtest Report</h1>
<div class="sub">区间: {_html.escape(str(meta.get('start', '?')))} ~ {_html.escape(str(meta.get('end', '?')))} | 模式: {_html.escape(str(meta.get('quote_mode', 'tick')))} | 对账差值: {float(val.get('accounting_difference_yuan', 0.0)):+.4f} | 未决委托: {len(report.get('unresolved_orders', []))}</div>
<div class="cards">
  <div class="card"><div class="k">初始资金 / 期末权益</div><div class="v">{float(acc.get('init_balance', 0.0)):,.0f} / {float(acc.get('final_equity', 0.0)):,.2f}</div></div>
  <div class="card"><div class="k">净收益</div><div class="v {ret_cls}">{ret_yuan:+,.2f} ({float(acc.get('total_return_pct', 0.0))*100:+.2f}%)</div></div>
  <div class="card"><div class="k">最大回撤</div><div class="v neg">{float(acc.get('max_drawdown_yuan', 0.0)):,.2f} ({float(acc.get('max_drawdown_pct', 0.0))*100:.2f}%)</div></div>
  <div class="card"><div class="k">已平仓交易 / 胜率</div><div class="v">{int(ov.get('trades', 0))} 笔 / {float(ov.get('win_rate', 0.0))*100:.1f}%</div></div>
  <div class="card"><div class="k">盈亏比 / 平均R</div><div class="v">{_html.escape(_fmt_pf(float(ov.get('profit_factor', 0.0))))} / {float(ov.get('avg_r', 0.0)):+.2f}R</div></div>
  <div class="card"><div class="k">手续费 / 柜台拒单</div><div class="v">{float(acc.get('realized_commission', 0.0)):,.2f} / {int(report.get('broker_rejections_count', 0))}</div></div>
</div>
<div class="panel"><h2>净值曲线 (蓝) & 回撤 (红)</h2>{svg_chart}</div>
<div class="panel"><h2>平仓原因分布</h2>{reasons_html or "<span class='empty'>无平仓记录</span>"}</div>
<div class="panel"><h2>分品种表现</h2>
<table><thead><tr><th>品种</th><th>笔数</th><th>胜率</th><th>PF</th><th>AvgR</th><th>净盈亏</th><th>手续费</th><th>多单(笔/净利)</th><th>空单(笔/净利)</th></tr></thead>
<tbody>{''.join(sym_rows_html) or "<tr><td colspan='9' class='empty'>无品种记录</td></tr>"}</tbody></table></div>
<div class="panel"><h2>平仓成交明细</h2>
<div style="margin-bottom:10px;"><input id="fltSym" placeholder="按品种过滤 (如 RB0)" oninput="flt()"/><select id="fltRes" onchange="flt()"><option value="">全部</option><option value="win">盈利</option><option value="loss">亏损</option></select></div>
<table><thead><tr><th>持仓ID</th><th>合约</th><th>方向</th><th>手数</th><th>开仓时间</th><th>开仓价</th><th>平仓时间</th><th>平仓均价</th><th>退出原因</th><th>净R</th><th>净盈亏</th></tr></thead>
<tbody id="trBody">{''.join(trade_rows_html) or "<tr><td colspan='11' class='empty'>无交易记录</td></tr>"}</tbody></table></div>
<script>
function flt(){{var s=document.getElementById('fltSym').value.trim().toUpperCase(),r=document.getElementById('fltRes').value,rows=document.getElementById('trBody').rows;for(var i=0;i<rows.length;i++){{var tr=rows[i];if(!tr.dataset.sym)continue;var ok=(!s||tr.dataset.sym.indexOf(s)>=0)&&(!r||tr.dataset.res===r);tr.style.display=ok?'':'none';}}}}
</script>
</body></html>"""


def json_scalar(value: Any) -> Any:
    """Keep SDK/NumPy numeric scalars numeric in the exported audit trail."""
    if hasattr(value, 'item'):
        return value.item()
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}")


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
    p.add_argument("--strategy", choices=["n_pattern", "p2"], default="n_pattern", help="Strategy variant: n_pattern (default) or p2 (candidate strategy with ATR, friction, net RR filters and fixed S1 exit)")
    p.add_argument("--symbols", default="", help="Comma-separated symbols, e.g. RB0,EG0,AU0")
    p.add_argument("--symbols-db", default=None, help="Load enabled symbols from SQLite DB (e.g. data-dev/ntrend.db)")
    p.add_argument("--start", required=True, help="Backtest start time YYYY-MM-DD or 'YYYY-MM-DD HH:MM(:SS)'")
    p.add_argument("--end", required=True, help="Backtest end time YYYY-MM-DD or 'YYYY-MM-DD HH:MM(:SS)'")
    p.add_argument("--balance", type=float, default=50_000.0, help="Initial simulated balance (CNY)")
    p.add_argument("--warmup-bars", type=int, default=200, help="15m warmup bars before start date")
    p.add_argument("--min-score", type=float, default=DEFAULT_MIN_SCORE, help="Min N-pattern entry score")
    p.add_argument("--large-score", type=float, default=DEFAULT_LARGE_SCORE, help="Score threshold for 2-lot sizing")
    p.add_argument("--min-risk-ticks", type=int, default=DEFAULT_MIN_RISK_TICKS, help="Minimum stop distance in ticks")
    p.add_argument("--max-risk-yuan", type=float, default=None, help="Max risk per trade in CNY")
    p.add_argument("--max-risk-pct", type=float, default=None, help="Max risk per trade as fraction of equity (e.g. 0.01)")
    p.add_argument("--risk-action", choices=["downsize", "reject"], default="downsize", help="Action when 2-lot exceeds cap")
    p.add_argument("--slippage-ticks", type=int, default=None, help="Aggressive limit offset (live default 2); not extra realized fill cost")
    p.add_argument("--quote-mode", choices=["tick", "minute"], default="tick", help="Actual-contract replay granularity; minute is approximate")
    p.add_argument("--live-config", help="Read non-secret execution settings from a live trader_config.json")
    p.add_argument("--one-per-symbol", action="store_true", help="Optional portfolio restriction, absent from live NPatternStrategy")
    p.add_argument("--max-positions", type=int, default=None, help="Maximum concurrent open positions allowed across portfolio")
    p.add_argument("--market-gap-events", help="Recorded transport gaps JSONL, each with Beijing datetime; never inferred from session breaks")
    p.add_argument("--allow-rollover-overlap", action="store_true", help="Allow new contract entry while old contract open")
    p.add_argument("--worker-bin", default=None, help="Optional explicit path to n-signal-worker binary")
    p.add_argument("--web-gui", default="", help="Pass true or host:port to open TQSDK WebUI during backtest")
    p.add_argument("--hold-web", action="store_true", help="Keep TQSDK WebUI server running after finish until Ctrl+C")
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
        strategy=args.strategy,
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
        slippage_ticks=2 if args.slippage_ticks is None else args.slippage_ticks,
        quote_mode=args.quote_mode,
        one_per_symbol=args.one_per_symbol,
        max_positions=args.max_positions,
        market_gap_events=args.market_gap_events,
        allow_rollover_overlap=args.allow_rollover_overlap,
        worker_path=args.worker_bin,
        web_gui=_parse_web_gui(args.web_gui),
        hold_web=args.hold_web,
        export_dir=args.export_dir,
        quiet=args.quiet,
    )
    if args.live_config:
        live_cfg = json.loads(Path(args.live_config).read_text(encoding="utf-8-sig"))
        if live_cfg.get("mode", "FullAuto") != "FullAuto":
            parser.error("Only FullAuto N-pattern execution is supported by this backtest")
        for key, field_name in {"slippage_ticks": "slippage_ticks", "order_wait_seconds": "order_wait_seconds",
                                "max_exit_slippage_ticks": "max_exit_slippage_ticks",
                                "default_large_volume": "large_volume", "default_small_volume": "small_volume",
                                "min_entry_risk_ticks": "min_risk_ticks"}.items():
            if key in live_cfg:
                setattr(cfg, field_name, int(live_cfg[key]))
        if args.slippage_ticks is not None:
            cfg.slippage_ticks = args.slippage_ticks
    # max_risk_percent is deliberately not mapped: NScoreSizing does not enforce it.
    cfg.__post_init__()

    report = run_tq_backtest(cfg, account=args.account, password=args.password)
    if not (cfg.web_gui and cfg.hold_web):
        print_report(report)
        if cfg.export_dir:
            out = export_report(report, cfg.export_dir)
            print(f"[backtest] Reports exported to: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
