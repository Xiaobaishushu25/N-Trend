"""
TianQin (TqSdk) Local HTTP Bridge Service for N-Trend.
Provides fast local REST API endpoints for real-time quotes, K-lines, and contract discovery.
"""

import argparse
import asyncio
from collections import deque
import datetime
import itertools
import json
import logging
import os
import queue
import re
import sys
import threading
import time
import uuid
from typing import Dict, List, Optional, Tuple

from aiohttp import web
import pandas as pd
from tqsdk import TqApi, TqAuth
from tqsdk.exceptions import TqTimeoutError

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [TqBridge] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("TqBridge")
SERVER_STARTED_AT = time.time()
SERVICE_NAME = "ntrend-tq-bridge"
STREAM_ID = uuid.uuid4().hex
MARKET_STALE_SECS = 15.0
RECONNECT_INITIAL_DELAY_SECS = 1.0
RECONNECT_MAX_DELAY_SECS = 30.0


class BridgeCommandTimeout(TimeoutError):
    pass


class BridgeQueueFull(RuntimeError):
    pass

# --- Variety to Exchange Mapping ---
SHFE_VARIETIES = {"rb", "hc", "fu", "bu", "ru", "sp", "cu", "al", "zn", "pb", "ni", "sn", "au", "ag", "ss", "br", "ao", "wr"}
DCE_VARIETIES = {"m", "y", "a", "b", "p", "c", "cs", "jd", "l", "v", "pp", "j", "jm", "i", "eg", "eb", "pg", "lh", "rr", "fb", "bb"}
CZCE_VARIETIES = {"SR", "CF", "TA", "OI", "RM", "MA", "FG", "SA", "UR", "SF", "SM", "AP", "CJ", "PK", "SH", "PX", "PF", "WH", "RI", "JR", "LR", "PM", "RS", "CY", "ZC"}
CFFEX_VARIETIES = {"IF", "IH", "IC", "IM", "TF", "T", "TS", "TL"}
INE_VARIETIES = {"sc", "lu", "nr", "bc", "ec"}
GFEX_VARIETIES = {"si", "lc", "ps"}

VARIETY_TO_EXCHANGE = {}
for v in SHFE_VARIETIES:
    VARIETY_TO_EXCHANGE[v.upper()] = "SHFE"
for v in DCE_VARIETIES:
    VARIETY_TO_EXCHANGE[v.upper()] = "DCE"
for v in CZCE_VARIETIES:
    VARIETY_TO_EXCHANGE[v.upper()] = "CZCE"
for v in CFFEX_VARIETIES:
    VARIETY_TO_EXCHANGE[v.upper()] = "CFFEX"
for v in INE_VARIETIES:
    VARIETY_TO_EXCHANGE[v.upper()] = "INE"
for v in GFEX_VARIETIES:
    VARIETY_TO_EXCHANGE[v.upper()] = "GFEX"

# Chinese variety name dictionary for fallback
VARIETY_NAMES = {
    "RB": "螺纹钢", "HC": "热轧卷板", "FU": "燃料油", "BU": "石油沥青", "RU": "天然橡胶", "SP": "纸浆",
    "CU": "阴极铜", "AL": "铝", "ZN": "锌", "PB": "铅", "NI": "镍", "SN": "锡", "AU": "黄金", "AG": "白银",
    "SS": "不锈钢", "BR": "丁二烯橡胶", "AO": "氧化铝", "WR": "线材",
    "M": "豆粕", "Y": "豆油", "A": "豆一", "B": "豆二", "P": "棕榈油", "C": "玉米", "CS": "玉米淀粉",
    "JD": "鸡蛋", "L": "塑料", "V": "PVC", "PP": "聚丙烯", "J": "焦炭", "JM": "焦煤", "I": "铁矿石",
    "EG": "乙二醇", "EB": "苯乙烯", "PG": "液化石油气", "LH": "生猪",
    "SR": "白糖", "CF": "棉花", "TA": "PTA", "OI": "菜油", "RM": "菜粕", "MA": "甲醇", "FG": "玻璃",
    "SA": "纯碱", "UR": "尿素", "SF": "硅铁", "SM": "锰硅", "AP": "苹果", "CJ": "红枣", "PK": "花生",
    "SH": "烧碱", "PX": "对二甲苯", "PF": "短纤",
    "IF": "沪深300股指", "IH": "上证50股指", "IC": "中证500股指", "IM": "中证1000股指",
    "TF": "5年期国债", "T": "10年期国债", "TS": "2年期国债", "TL": "30年期国债",
    "SC": "原油", "LU": "低硫燃料油", "NR": "20号胶", "BC": "国际铜", "EC": "集运指数(欧线)",
    "SI": "工业硅", "LC": "碳酸锂", "PS": "多晶硅",
}


class SymbolMapper:
    """Handles symbol mapping between Sina/generic formats and TqSdk formats."""

    @staticmethod
    def parse_code(code: str) -> Tuple[str, str, str]:
        """
        Parses a symbol like 'RB0' or 'RB2605' or 'SHFE.rb2605' or 'KQ.m@SHFE.rb'
        Returns (exchange, variety_upper, tq_symbol)
        """
        raw = code.strip()
        if "@" in raw:
            # KQ.m@SHFE.rb -> exchange=SHFE, variety=RB
            parts = raw.split("@")
            ex_var = parts[1].split(".")
            exchange = ex_var[0].upper()
            var = ex_var[1].upper()
            return exchange, var, raw
        elif "." in raw:
            # SHFE.rb2605 -> exchange=SHFE, variety=RB
            parts = raw.split(".")
            exchange = parts[0].upper()
            var_match = re.match(r"^([a-zA-Z]+)", parts[1])
            var = var_match.group(1).upper() if var_match else parts[1].upper()
            return exchange, var, raw

        # Standard generic code (e.g. RB0, RB2605, IF0, MA609)
        clean = raw.upper()
        match = re.match(r"^([A-Z]+)(\d*)$", clean)
        if not match:
            return "", clean, clean

        variety = match.group(1)
        suffix = match.group(2)
        exchange = VARIETY_TO_EXCHANGE.get(variety, "")

        if not exchange:
            # Try 1-letter or 2-letter prefix fallback
            if len(variety) > 1 and variety[:1] in VARIETY_TO_EXCHANGE:
                exchange = VARIETY_TO_EXCHANGE[variety[:1]]
                suffix = variety[1:] + suffix
                variety = variety[:1]
            elif len(variety) > 2 and variety[:2] in VARIETY_TO_EXCHANGE:
                exchange = VARIETY_TO_EXCHANGE[variety[:2]]
                suffix = variety[2:] + suffix
                variety = variety[:2]

        if not exchange:
            # 未知映射不能猜成上期所，否则会为错误合约生成“闭合证明”。
            return "", variety, clean

        # If suffix is "0" or empty -> Continuous Main Contract
        if suffix == "0" or suffix == "":
            var_part = variety if exchange in ["CZCE", "CFFEX"] else variety.lower()
            tq_symbol = f"KQ.m@{exchange}.{var_part}"
        else:
            # 郑商所 CZCE 合约为 3 位 YMM (如 CJ701)，外部常传入 4 位 YYYYMM (如 CJ2701)
            # 需将 4 位压缩为 3 位，避免查询 CZCE.CJ2701 这类不存在合约。
            normalized_suffix = suffix
            if exchange == "CZCE" and len(suffix) == 4 and suffix.isdigit():
                normalized_suffix = suffix[1:]  # 2701 -> 701, 2605 -> 605
            var_part = variety if exchange in ["CZCE", "CFFEX"] else variety.lower()
            tq_symbol = f"{exchange}.{var_part}{normalized_suffix}"

        return exchange, variety, tq_symbol

    @staticmethod
    def to_generic(tq_symbol: str) -> str:
        """Converts TqSdk symbol back to generic code like RB0 or RB2605."""
        if "@" in tq_symbol:
            # KQ.m@SHFE.rb -> RB0
            parts = tq_symbol.split("@")
            var = parts[1].split(".")[1].upper()
            return f"{var}0"
        elif "." in tq_symbol:
            # SHFE.rb2605 -> RB2605
            code = tq_symbol.split(".")[1].upper()
            return code
        return tq_symbol


class TqDataWorker:
    """Dedicated background worker thread running TqApi event loop."""

    def __init__(self, account: str, password: str):
        self.account = account
        self.password = password
        self.running = False
        self.api: Optional[TqApi] = None
        self.thread: Optional[threading.Thread] = None
        self.quotes = {}
        self.klines = {}
        self.kline_meta = {}
        self.trading_statuses = {}
        self.quotes_lock = threading.Lock()
        self.cmd_queue = queue.Queue(maxsize=256)
        self.command_ids = itertools.count(1)
        self.connected = False
        self.reconnecting = False
        self.reconnect_requested = threading.Event()
        self.last_transport_error: Optional[str] = None
        self.startup_error: Optional[str] = None
        self.last_update_time = 0.0
        self.last_market_event_at = 0.0
        self.quote_symbols = set()
        self.subscription_specs = {}
        self.events = deque(maxlen=2048)
        self.events_lock = threading.Lock()
        self.next_event_id = itertools.count(1)
        self.emitted_bars = set()
        self.emitted_bar_order = deque()

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        self.reconnect_requested.set()
        if self.thread:
            self.thread.join(timeout=3.0)

    def _run(self):
        if not self._connect_and_restore(initial=True):
            return

        while self.running:
            try:
                if self.reconnect_requested.is_set():
                    self._reconnect_until_ready()
                    continue

                # 每轮最多处理少量命令，随后必须泵一次 TqApi；否则一波 HTTP 请求会
                # 长时间饿死 WebSocket，健康检查误判为心跳超时。
                for _ in range(8):
                    try:
                        request_id, cmd, args, resp_q, expires_at, cancelled = self.cmd_queue.get_nowait()
                    except queue.Empty:
                        break

                    if cancelled.is_set() or time.monotonic() >= expires_at:
                        logger.warning("Discarding expired command id=%s cmd=%s", request_id, cmd)
                        continue
                    try:
                        res = self._handle_cmd(cmd, args)
                        if not cancelled.is_set():
                            resp_q.put_nowait((True, res))
                    except Exception as ex:
                        logger.exception("Error executing command id=%s cmd=%s", request_id, cmd)
                        if self._is_transport_error(ex):
                            self._request_reconnect(
                                f"{type(ex).__name__}: {self._safe_error(ex)}"
                            )
                        if not cancelled.is_set():
                            resp_q.put_nowait((False, f"{type(ex).__name__}: {ex}"))

                if self.reconnect_requested.is_set():
                    continue

                # Pump TqApi event loop
                self._pump_update(time.time() + 0.05)
                self._expire_session_candidates()
            except Exception as e:
                logger.error("Error in TqApi worker loop: %s", e)
                self._request_reconnect(f"{type(e).__name__}: {self._safe_error(e)}")

        self._close_api()

    @staticmethod
    def _safe_error(error: Exception) -> str:
        return str(error).replace("\n", " ")[:500]

    @staticmethod
    def _is_transport_error(error: Exception) -> bool:
        if isinstance(error, TqTimeoutError):
            return True
        text = str(error).lower()
        return any(
            marker in text
            for marker in (
                "websocket",
                "connection",
                "disconnected",
                "timed out",
                "timeout",
                "连接失败",
                "连接断开",
                "超时",
            )
        )

    def _request_reconnect(self, reason: str):
        if not self.reconnect_requested.is_set():
            logger.warning("TqApi connection is unhealthy; scheduling reconnect: %s", reason)
        self.last_transport_error = reason
        self.connected = False
        self.reconnecting = True
        self.reconnect_requested.set()

    def _close_api(self):
        api = self.api
        self.api = None
        self.connected = False
        if api:
            try:
                api.close()
            except Exception as error:
                logger.debug("TqApi close failed during reconnect: %s", error)

    def _clear_runtime_state(self):
        # 旧 TqApi 的对象不能跨 WebSocket 会话复用，否则会把失效序列当成有效缓存。
        self.quotes.clear()
        self.klines.clear()
        self.kline_meta.clear()
        self.trading_statuses.clear()
        self.last_market_event_at = 0.0
        self.last_update_time = time.time()

    def _connect_and_restore(self, initial: bool = False) -> bool:
        if not initial:
            self._close_api()
            self._clear_runtime_state()

        try:
            logger.info("Starting TqApi connection with account: %s", self.account)
            self.api = TqApi(auth=TqAuth(self.account, self.password))
            self._restore_subscriptions()
            self.connected = True
            self.reconnecting = False
            self.reconnect_requested.clear()
            self.startup_error = None
            self.last_transport_error = None
            logger.info(
                "TqApi WebSocket connection established and subscriptions restored "
                "(quotes=%d klines=%d)",
                len(self.quote_symbols),
                len(self.subscription_specs),
            )
            return True
        except Exception as error:
            message = f"{type(error).__name__}: {self._safe_error(error)}"
            if self.password:
                message = message.replace(self.password, "***")
            self.startup_error = message
            self.last_transport_error = message
            self.connected = False
            self.reconnecting = not initial
            self._close_api()
            logger.error("Failed to initialize/reconnect TqApi: %s", message)
            return False

    def _reconnect_until_ready(self):
        self.reconnect_requested.clear()
        delay = RECONNECT_INITIAL_DELAY_SECS
        while self.running:
            if self._connect_and_restore(initial=False):
                return
            logger.warning("TqApi reconnect failed; retrying in %.1fs", delay)
            if self.reconnect_requested.wait(delay) and not self.running:
                return
            self.reconnect_requested.clear()
            delay = min(delay * 2.0, RECONNECT_MAX_DELAY_SECS)

    def _restore_subscriptions(self):
        for tq_symbol in sorted(self.quote_symbols):
            self.quotes[tq_symbol] = self.api.get_quote(tq_symbol)

        created = []
        for key, spec in list(self.subscription_specs.items()):
            try:
                self._prepare_subscription(spec)
                created.append(key)
            except Exception as error:
                if self._is_transport_error(error):
                    raise
                self.subscription_specs.pop(key, None)
                logger.warning(
                    "Skip restoring subscription %s -> %s: %s",
                    spec["symbol"],
                    spec["tq_symbol"],
                    self._safe_error(error),
                )

        if created:
            self._finalize_subscriptions(created)

    def _prepare_subscription(self, spec: dict):
        key = (spec["tq_symbol"], spec["duration_seconds"])
        self.klines[key] = self.api.get_kline_serial(
            spec["tq_symbol"],
            duration_seconds=spec["duration_seconds"],
            data_length=max(spec["data_length"], 1000),
        )
        if spec["tq_symbol"] not in self.quotes:
            self.quotes[spec["tq_symbol"]] = self.api.get_quote(spec["tq_symbol"])
        self.kline_meta[key] = {
            "symbol": spec["symbol"],
            "tq_symbol": spec["tq_symbol"],
            "period": spec["period"],
            "duration": spec["duration_seconds"],
            "initialized": False,
            "last_datetime": None,
            "last_trade_status": None,
            "session_candidate": None,
            "status_symbol": None,
        }

    def _finalize_subscriptions(self, created):
        # 全部序列创建后统一驱动，避免逐品种冷启动时重复等待。
        adaptive_deadline = max(8.0, min(30.0, 0.9 * len(created) + 8.0))
        deadline = time.time() + adaptive_deadline
        while time.time() < deadline:
            ready = all(
                len(self.klines[key]) > 1
                and not pd.isna(self.klines[key].iloc[-1]["close"])
                for key in created
            )
            if ready:
                break
            self._pump_update(time.time() + 0.05)

        ready_symbols = []
        for key in created:
            klines = self.klines.get(key)
            meta = self.kline_meta.get(key)
            if klines is None or meta is None:
                continue
            quote = self.quotes.get(meta["tq_symbol"])
            underlying = str(getattr(quote, "underlying_symbol", "") or "")
            status_symbol = underlying or meta["tq_symbol"]
            if status_symbol not in self.trading_statuses:
                try:
                    self.trading_statuses[status_symbol] = self.api.get_trading_status(status_symbol)
                except Exception as error:
                    logger.warning(
                        "Trading status unavailable for %s (%s)",
                        meta["symbol"],
                        status_symbol,
                    )
            meta["status_symbol"] = status_symbol if status_symbol in self.trading_statuses else None
            if (
                not meta.get("initialized")
                and len(klines) > 1
                and not pd.isna(klines.iloc[-1]["datetime"])
            ):
                meta["last_datetime"] = int(klines.iloc[-1]["datetime"])
                status = self.trading_statuses.get(meta.get("status_symbol"))
                meta["last_trade_status"] = getattr(status, "trade_status", None)
                meta["initialized"] = True
            if meta.get("initialized"):
                ready_symbols.append(meta["symbol"])
        return ready_symbols

    def _pump_update(self, deadline: float) -> bool:
        """推进一次 TqApi，并保证任何调用点都不会绕过闭合事件检测。"""
        updated = self.api.wait_update(deadline=deadline)
        self.last_update_time = time.time()
        if updated:
            self.last_market_event_at = time.time()
            self._detect_closed_bars()
        return updated

    def _handle_cmd(self, cmd: str, args: dict):
        if cmd == "subscribe_quotes":
            tq_symbols = args["tq_symbols"]
            self.quote_symbols.update(tq_symbols)
            new_sub = False
            for sym in tq_symbols:
                if sym not in self.quotes:
                    self.quotes[sym] = self.api.get_quote(sym)
                    new_sub = True
            if new_sub:
                t0 = time.time()
                while time.time() - t0 < 0.8:
                    self._pump_update(time.time() + 0.05)
                    if any(sym in self.quotes and not pd.isna(self.quotes[sym].last_price) for sym in tq_symbols):
                        break
            return self.quotes

        elif cmd == "get_kline":
            tq_symbol = args["tq_symbol"]
            duration = args["duration_seconds"]
            data_length = args["data_length"]
            key = (tq_symbol, duration)

            if key in self.klines:
                klines = self.klines[key]
                if len(klines) < data_length:
                    klines = self.api.get_kline_serial(tq_symbol, duration_seconds=duration, data_length=data_length)
                    self.klines[key] = klines
            else:
                # Subscribe to kline serial with a generous buffer
                klines = self.api.get_kline_serial(tq_symbol, duration_seconds=duration, data_length=max(data_length, 1000))
                self.klines[key] = klines

            # If already populated with valid data, return immediately without blocking!
            if len(klines) > 0 and not pd.isna(klines.iloc[-1]["close"]):
                return klines.copy()

            # Otherwise wait briefly for initial data download
            t0 = time.time()
            while time.time() - t0 < 1.5:
                self._pump_update(time.time() + 0.05)
                if len(klines) > 0 and not pd.isna(klines.iloc[-1]["close"]):
                    break
            return klines.copy()

        elif cmd == "subscribe_klines":
            subscriptions = args["subscriptions"]
            data_length = args.get("data_length", 1000)
            created = []
            failed = []
            for sub in subscriptions:
                generic_symbol = sub["symbol"]
                tq_symbol = sub["tq_symbol"]
                duration = sub["duration_seconds"]
                period = sub["period"]
                key = (tq_symbol, duration)
                spec = {
                    "symbol": generic_symbol,
                    "tq_symbol": tq_symbol,
                    "duration_seconds": duration,
                    "period": period,
                    "data_length": data_length,
                }
                self.subscription_specs[key] = spec
                try:
                    if key not in self.klines:
                        self._prepare_subscription(spec)
                    created.append(key)
                except Exception as error:
                    is_transport_error = self._is_transport_error(error)
                    if is_transport_error:
                        self._request_reconnect(
                            f"{type(error).__name__}: {self._safe_error(error)}"
                        )
                    else:
                        # 无效合约不应被永久带入后续重连恢复列表。
                        self.subscription_specs.pop(key, None)
                    # 单品种异常仅记为 subscription_missing，不污染整批。
                    logger.warning(
                        "Skip invalid subscription %s -> %s: %s",
                        generic_symbol, tq_symbol, self._safe_error(error),
                    )
                    failed.append(generic_symbol)
                    # 清理半初始化状态，避免残留 key 影响后续重试
                    self.klines.pop(key, None)
                    self.kline_meta.pop(key, None)
                    continue

            ready_symbols = self._finalize_subscriptions(created) if created else []
            if failed:
                logger.warning(
                    "[FAST_PATH] subscription_missing=%s fallback=legacy (invalid instrument isolated)",
                    failed,
                )
            return {"stream_id": STREAM_ID, "subscribed": ready_symbols, "failed": failed}

        elif cmd == "ping":
            return "pong"

        elif cmd == "search":
            keyword = args["keyword"].strip().upper()
            # Find matching symbols
            return self._search_symbols(keyword)

        return None

    @staticmethod
    def _row_to_bar(row, duration: int) -> dict:
        ts_sec = float(row["datetime"]) / 1e9
        dt_start = datetime.datetime.fromtimestamp(ts_sec, tz=datetime.timezone.utc)
        dt_end = dt_start + datetime.timedelta(seconds=duration)
        hold_val = row.get("close_oi", row.get("open_oi", 0.0))
        if pd.isna(hold_val):
            hold_val = 0.0
        return {
            "datetime": dt_end.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"]),
            "hold": float(hold_val),
        }

    def _emit_closed_bar(self, meta: dict, row, proof: str, next_row=None):
        bar = self._row_to_bar(row, meta["duration"])
        dedup_key = (meta["symbol"], meta["period"], bar["datetime"])
        if dedup_key in self.emitted_bars:
            return
        if not all(pd.notna(row.get(k)) for k in ("open", "high", "low", "close")):
            return

        next_bar_start = None
        if next_row is not None:
            next_ts = float(next_row["datetime"]) / 1e9
            next_bar_start = datetime.datetime.fromtimestamp(
                next_ts, tz=datetime.timezone.utc
            ).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        quote = self.quotes.get(meta["tq_symbol"])
        market_time = str(getattr(quote, "datetime", "") or "")
        now = time.time()
        event = {
            "event_id": next(self.next_event_id),
            "event_type": "bar_closed",
            "source": "tqsdk",
            "proof": proof,
            "symbol": meta["symbol"],
            "tq_symbol": meta["tq_symbol"],
            "period": meta["period"],
            "bar_end": bar["datetime"],
            "next_bar_start": next_bar_start,
            "kline": bar,
            "market_event_time": market_time,
            "emitted_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "event_lag_ms": max(0, int((now - self.last_market_event_at) * 1000)),
        }
        with self.events_lock:
            self.events.append(event)
        self.emitted_bars.add(dedup_key)
        self.emitted_bar_order.append(dedup_key)
        while len(self.emitted_bars) > 4096:
            self.emitted_bars.discard(self.emitted_bar_order.popleft())
        logger.info(
            "[TQ_BAR_CLOSED] %s 5m_end=%s proof=%s event_lag=%dms",
            meta["symbol"], bar["datetime"], proof, event["event_lag_ms"],
        )

    @staticmethod
    def _is_expected_session_end(quote, bar_end: str) -> bool:
        trading_time = getattr(quote, "trading_time", None)
        if not trading_time:
            return False
        expected = set()
        try:
            for group in ("day", "night"):
                intervals = trading_time.get(group, [])
                for interval in intervals:
                    if len(interval) >= 2:
                        expected.add(str(interval[1])[:8])
        except Exception:
            return False
        return bar_end[11:19] in expected

    def _detect_closed_bars(self):
        for key, meta in list(self.kline_meta.items()):
            if not meta.get("initialized"):
                continue
            klines = self.klines.get(key)
            if klines is None or len(klines) < 2:
                continue
            latest_dt = int(klines.iloc[-1]["datetime"])
            datetime_changed = self.api.is_changing(klines.iloc[-1], "datetime")
            if datetime_changed or latest_dt != meta.get("last_datetime"):
                self._emit_closed_bar(
                    meta,
                    klines.iloc[-2],
                    "next_bar_started",
                    klines.iloc[-1],
                )
                meta["last_datetime"] = latest_dt
                meta["session_candidate"] = None

            status_obj = self.trading_statuses.get(meta.get("status_symbol"))
            current_status = getattr(status_obj, "trade_status", None)
            previous_status = meta.get("last_trade_status")
            if current_status == "NOTRADING" and previous_status not in (None, "NOTRADING"):
                meta["session_candidate"] = {
                    "created": time.monotonic(),
                    "wait_updates": 0,
                }
            elif current_status == "NOTRADING" and meta.get("session_candidate"):
                candidate = meta["session_candidate"]
                candidate["wait_updates"] += 1
                quote = self.quotes.get(meta["tq_symbol"])
                bar = self._row_to_bar(klines.iloc[-1], meta["duration"])
                if (
                    candidate["wait_updates"] >= 1
                    and time.monotonic() - candidate["created"] <= 8.0
                    and self._is_expected_session_end(quote, bar["datetime"])
                    and time.time() - self.last_market_event_at <= MARKET_STALE_SECS
                ):
                    self._emit_closed_bar(meta, klines.iloc[-1], "session_not_trading")
                    meta["session_candidate"] = None
            meta["last_trade_status"] = current_status

    def _expire_session_candidates(self):
        for meta in self.kline_meta.values():
            candidate = meta.get("session_candidate")
            if candidate and time.monotonic() - candidate["created"] > 8.0:
                logger.warning(
                    "[FAST_PATH_TIMEOUT] %s expected_session_close waited=8000ms fallback=legacy",
                    meta["symbol"],
                )
                meta["session_candidate"] = None

    def snapshot_events(self, after_id: int) -> dict:
        with self.events_lock:
            events = [event.copy() for event in self.events if event["event_id"] > after_id]
            oldest = self.events[0]["event_id"] if self.events else 0
            latest = self.events[-1]["event_id"] if self.events else 0
        return {
            "stream_id": STREAM_ID,
            "oldest_event_id": oldest,
            "latest_event_id": latest,
            "events": events,
        }

    def _search_symbols(self, keyword: str) -> list:
        results = []
        if not keyword:
            return results

        # Match variety prefix
        for var, ex in VARIETY_TO_EXCHANGE.items():
            name = VARIETY_NAMES.get(var, var)
            if var.startswith(keyword) or keyword in var or keyword in name:
                results.append({
                    "code": f"{var}0",
                    "name": f"{name}主连",
                    "variety": name,
                    "exchange": ex,
                    "node": f"{var}0",
                })
        return results

    def exec_cmd(self, cmd: str, args: dict, timeout: float = 15.0):
        if not self.connected or not self.running:
            raise RuntimeError("TqApi is not connected")
        request_id = next(self.command_ids)
        resp_q = queue.Queue(maxsize=1)
        cancelled = threading.Event()
        expires_at = time.monotonic() + timeout
        try:
            self.cmd_queue.put(
                (request_id, cmd, args, resp_q, expires_at, cancelled),
                timeout=min(timeout, 0.25),
            )
        except queue.Full as exc:
            raise BridgeQueueFull(
                f"command queue full: id={request_id} cmd={cmd} size={self.cmd_queue.qsize()}"
            ) from exc

        try:
            ok, res = resp_q.get(timeout=timeout)
        except queue.Empty as exc:
            cancelled.set()
            symbol = args.get("tq_symbol") or ",".join(args.get("tq_symbols", [])[:3])
            raise BridgeCommandTimeout(
                f"command timed out after {timeout:.1f}s: id={request_id} cmd={cmd} "
                f"symbol={symbol or '-'} queue={self.cmd_queue.qsize()}"
            ) from exc
        if not ok:
            raise RuntimeError(res)
        return res


# Global worker instance
worker: Optional[TqDataWorker] = None


async def handle_health(request: web.Request) -> web.Response:
    global worker
    if not worker or not worker.running:
        return web.json_response({
            "service": SERVICE_NAME,
            "pid": os.getpid(),
            "status": "error",
            "tq_connected": False,
            "fatal": False,
            "reason": "TianQin worker is not initialized or connected"
        }, status=503)

    if worker.reconnecting or worker.reconnect_requested.is_set():
        return web.json_response({
            "service": SERVICE_NAME,
            "pid": os.getpid(),
            "status": "reconnecting",
            "tq_connected": False,
            "worker_alive": bool(worker.thread and worker.thread.is_alive()),
            "reconnecting": True,
            "last_transport_error": worker.last_transport_error,
        }, status=503)

    if worker.startup_error:
        return web.json_response({
            "service": SERVICE_NAME,
            "pid": os.getpid(),
            "status": "error",
            "tq_connected": False,
            "reconnecting": False,
            "fatal": True,
            "reason": f"TqApi initialization failed: {worker.startup_error}"
        }, status=503)

    if not worker.connected:
        return web.json_response({
            "service": SERVICE_NAME,
            "pid": os.getpid(),
            "status": "starting",
            "tq_connected": False,
            "reconnecting": False,
            "fatal": False,
            "reason": "TqApi connection is still initializing"
        }, status=503)

    if not worker.thread or not worker.thread.is_alive():
        return web.json_response({
            "service": SERVICE_NAME,
            "pid": os.getpid(),
            "status": "error",
            "tq_connected": False,
            "reconnecting": False,
            "fatal": True,
            "reason": "TianQin worker thread is dead"
        }, status=503)

    # Worker 循环心跳与真实市场事件心跳必须分开判断。
    heartbeat_age = time.time() - worker.last_update_time
    if heartbeat_age > 4.0:
        return web.json_response({
            "service": SERVICE_NAME,
            "pid": os.getpid(),
            "status": "error",
            "tq_connected": False,
            "reconnecting": False,
            "reason": f"TianQin worker heartbeat timed out ({heartbeat_age:.1f}s ago)"
        }, status=503)

    # Perform active probe to verify command queue responsiveness
    try:
        ping_res = await asyncio.to_thread(worker.exec_cmd, "ping", {}, 1.0)
        if ping_res != "pong":
            return web.json_response({
                "service": SERVICE_NAME,
                "pid": os.getpid(),
                "status": "error",
                "tq_connected": False,
                "reconnecting": False,
                "reason": "Worker ping response mismatch"
            }, status=503)
    except Exception as e:
        return web.json_response({
            "service": SERVICE_NAME,
            "pid": os.getpid(),
            "status": "error",
            "tq_connected": False,
            "reconnecting": False,
            "reason": f"Worker unresponsive to ping: {e}"
        }, status=503)

    market_event_age = (
        time.time() - worker.last_market_event_at
        if worker.last_market_event_at > 0
        else None
    )
    market_expected = any(
        getattr(status, "trade_status", None) == "CONTINOUS"
        for status in worker.trading_statuses.values()
    )
    stale = market_expected and (
        market_event_age is None or market_event_age > MARKET_STALE_SECS
    )
    payload = {
        "service": SERVICE_NAME,
        "pid": os.getpid(),
        "started_at": SERVER_STARTED_AT,
        "stream_id": STREAM_ID,
        "status": "stale" if stale else "ok",
        "tq_connected": True,
        "reconnecting": False,
        "fatal": False,
        "worker_alive": True,
        "data_ready": bool(worker.quotes or worker.klines),
        "subscriptions": len(worker.subscription_specs),
        "quotes_cached": len(worker.quotes),
        "klines_cached": len(worker.klines),
        "queue_size": worker.cmd_queue.qsize(),
        "heartbeat_age": round(heartbeat_age, 3),
        "last_wait_update_at": datetime.datetime.fromtimestamp(
            worker.last_update_time, tz=datetime.timezone.utc
        ).isoformat(),
        "last_market_event_at": (
            datetime.datetime.fromtimestamp(
                worker.last_market_event_at, tz=datetime.timezone.utc
            ).isoformat()
            if worker.last_market_event_at > 0 else None
        ),
        "event_lag_ms": int(market_event_age * 1000) if market_event_age is not None else None,
        "stale": stale,
        "server_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    if stale:
        payload["reason"] = "TianQin market event stream is stale during continuous trading"
    return web.json_response(payload, status=503 if stale else 200)


async def handle_subscribe_klines(request: web.Request) -> web.Response:
    global worker
    if not worker or not worker.connected:
        return web.json_response({"error": "TianQin data service not connected"}, status=503)
    try:
        data = await request.json()
    except Exception:
        data = {}
    symbols = data.get("symbols", [])
    period = str(data.get("period", "5m")).lower()
    data_length = min(max(int(data.get("data_length", 1000)), 10), 8000)
    duration_map = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "60m": 3600}
    duration = duration_map.get(period)
    if not symbols or duration is None:
        return web.json_response({"error": "symbols and a supported period are required"}, status=400)
    subscriptions = []
    pre_failed: list[str] = []
    for symbol in symbols:
        exchange, _, tq_symbol = SymbolMapper.parse_code(str(symbol))
        if not exchange:
            logger.warning("Skip unknown symbol mapping: %s", symbol)
            pre_failed.append(str(symbol).upper())
            continue
        subscriptions.append({
            "symbol": str(symbol).upper(),
            "tq_symbol": tq_symbol,
            "period": period,
            "duration_seconds": duration,
        })
    if not subscriptions:
        return web.json_response(
            {"error": f"no valid symbol mapping: {pre_failed}", "failed": pre_failed},
            status=400,
        )
    try:
        # 冷启动 22 品种需更长超时，按品种数自适应（与 Rust 客户端保持一致）
        adaptive_timeout = max(18.0, min(35.0, 15.0 + len(subscriptions)))
        result = await asyncio.to_thread(
            worker.exec_cmd,
            "subscribe_klines",
            {"subscriptions": subscriptions, "data_length": data_length},
            adaptive_timeout,
        )
        # 合并预检失败的映射与 worker 内失败
        if pre_failed:
            existing_failed = result.get("failed") or []
            result["failed"] = existing_failed + pre_failed
        return web.json_response(result)
    except Exception as e:
        status = 504 if isinstance(e, BridgeCommandTimeout) else 503
        return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=status)


async def handle_events(request: web.Request) -> web.Response:
    global worker
    if not worker or not worker.connected:
        return web.json_response({"error": "TianQin data service not connected"}, status=503)
    try:
        after_id = max(0, int(request.query.get("after_id", "0")))
        timeout = min(max(float(request.query.get("timeout", "25")), 0.0), 25.0)
    except ValueError:
        return web.json_response({"error": "invalid after_id or timeout"}, status=400)
    deadline = time.monotonic() + timeout
    while True:
        snapshot = worker.snapshot_events(after_id)
        if snapshot["events"] or time.monotonic() >= deadline:
            return web.json_response(snapshot)
        await asyncio.sleep(0.05)


async def handle_quotes(request: web.Request) -> web.Response:
    global worker
    if not worker or not worker.connected:
        return web.json_response({"error": "TianQin data service not connected"}, status=503)

    try:
        data = await request.json()
    except Exception:
        data = {}

    symbols = data.get("symbols", [])
    if isinstance(symbols, str):
        symbols = [symbols]

    if not symbols:
        return web.json_response({"quotes": {}})

    # Map all symbols to TqSdk symbols
    mapping = {}
    for code in symbols:
        _, _, tq_sym = SymbolMapper.parse_code(code)
        mapping[code] = tq_sym

    tq_symbols = list(set(mapping.values()))
    try:
        quotes_dict = await asyncio.to_thread(
            worker.exec_cmd, "subscribe_quotes", {"tq_symbols": tq_symbols}, 18.0
        )
    except Exception as e:
        logger.warning("Error subscribing quotes for %s: %s", symbols, e)
        status = 504 if isinstance(e, BridgeCommandTimeout) else 503
        return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=status)

    out = {}
    for code, tq_sym in mapping.items():
        q = quotes_dict.get(tq_sym)
        if q is None or pd.isna(q.last_price):
            continue

        pre_settle = q.pre_settlement if not pd.isna(q.pre_settlement) and q.pre_settlement > 0 else q.pre_close
        if pd.isna(pre_settle):
            pre_settle = 0.0

        change_pct = None
        if pre_settle > 0.0:
            change_pct = round((q.last_price - pre_settle) / pre_settle * 100.0, 4)

        _, var, _ = SymbolMapper.parse_code(code)
        name = VARIETY_NAMES.get(var, var)
        if code.endswith("0"):
            name = f"{name}主连"

        out[code] = {
            "code": code,
            "name": name,
            "latest": float(q.last_price),
            "prev_settle": float(pre_settle),
            "change_pct": change_pct,
            "volume": float(q.volume) if not pd.isna(q.volume) else 0.0,
            "hold": float(q.open_interest) if not pd.isna(q.open_interest) else 0.0,
            "high": float(q.highest) if not pd.isna(q.highest) else float(q.last_price),
            "low": float(q.lowest) if not pd.isna(q.lowest) else float(q.last_price),
            "open": float(q.open) if not pd.isna(q.open) else float(q.last_price),
            "datetime": str(q.datetime) if hasattr(q, "datetime") else "",
        }

    return web.json_response({"quotes": out})


async def handle_kline(request: web.Request) -> web.Response:
    global worker
    if not worker or not worker.connected:
        return web.json_response({"error": "TianQin data service not connected"}, status=503)

    symbol = request.query.get("symbol", "").strip()
    if not symbol:
        return web.json_response({"error": "Missing symbol parameter"}, status=400)

    period = request.query.get("period", "5m").strip().lower()
    count = int(request.query.get("count", "300"))
    count = min(max(count, 1), 8000)

    # Convert period to duration in seconds
    duration_map = {
        "1m": 60, "1": 60,
        "5m": 300, "5": 300,
        "15m": 900, "15": 900,
        "30m": 1800, "30": 1800,
        "60m": 3600, "60": 3600, "1h": 3600,
        "1d": 86400, "d": 86400, "day": 86400,
    }
    duration_secs = duration_map.get(period, 300)

    _, _, tq_symbol = SymbolMapper.parse_code(symbol)

    try:
        klines_df = await asyncio.to_thread(
            worker.exec_cmd,
            "get_kline",
            {"tq_symbol": tq_symbol, "duration_seconds": duration_secs, "data_length": count},
            15.0,
        )
    except Exception as e:
        logger.warning("Error fetching K-lines for %s (%s): %s", symbol, tq_symbol, e)
        status = 504 if isinstance(e, BridgeCommandTimeout) else 503
        return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=status)
    if klines_df is None or len(klines_df) == 0:
        return web.json_response({"symbol": symbol, "period": period, "klines": []})

    # Convert K-lines to standard format
    # Note: Align timestamp to Bar END TIME by adding duration_seconds to start time!
    rows = []
    # Drop rows with NaN close
    valid_df = klines_df.dropna(subset=["close"]).tail(count)

    for _, row in valid_df.iterrows():
        # datetime in TqSdk is nanoseconds epoch UTC
        ts_nano = row["datetime"]
        ts_sec = ts_nano / 1e9
        # Add duration_secs to convert from Bar Start Time to Bar End Time (matching Sina convention)
        dt_end = datetime.datetime.fromtimestamp(ts_sec + duration_secs + 28800, tz=datetime.timezone.utc)
        dt_str = dt_end.strftime("%Y-%m-%d %H:%M:%S")

        hold_val = row.get("close_oi", row.get("open_oi", 0.0))
        if pd.isna(hold_val):
            hold_val = 0.0

        rows.append({
            "datetime": dt_str,
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"]),
            "hold": float(hold_val),
        })

    return web.json_response({
        "symbol": symbol,
        "period": period,
        "includes_current": True,
        "klines": rows,
    })


async def handle_search(request: web.Request) -> web.Response:
    global worker
    keyword = request.query.get("keyword", "").strip()
    if not worker or not worker.connected:
        return web.json_response({"results": []})

    try:
        results = await asyncio.to_thread(
            worker.exec_cmd, "search", {"keyword": keyword}, 3.0
        )
        return web.json_response({"results": results})
    except Exception as e:
        status = 504 if isinstance(e, BridgeCommandTimeout) else 503
        return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=status)


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/health", handle_health)
    app.router.add_post("/api/subscribe-klines", handle_subscribe_klines)
    app.router.add_get("/api/events", handle_events)
    app.router.add_post("/api/quotes", handle_quotes)
    app.router.add_get("/api/quotes", handle_quotes)
    app.router.add_get("/api/kline", handle_kline)
    app.router.add_get("/api/search", handle_search)
    return app


def main():
    global worker
    parser = argparse.ArgumentParser(description="TqSdk Bridge for N-Trend")
    parser.add_argument("--port", type=int, default=8765, help="HTTP port to listen on")
    parser.add_argument("--parent-pid", type=int, default=0, help="Parent ntrend process PID")
    parser.add_argument("--account", type=str, default="", help="ShinnyTech account")
    parser.add_argument("--password", type=str, default="", help="ShinnyTech password")
    args = parser.parse_args()

    account = os.environ.get("TQ_ACCOUNT", args.account)
    password = os.environ.get("TQ_PASSWORD", args.password)
    port = int(os.environ.get("TQ_PORT", args.port))

    if args.parent_pid > 0:
        def parent_is_alive(parent_pid: int) -> bool:
            if os.name == "nt":
                # Windows 的 os.kill(pid, 0) 会调用 TerminateProcess，不能用于探活。
                # 用只读的进程同步句柄判断是否已经退出。
                import ctypes
                from ctypes import wintypes
                synchronize = 0x00100000
                wait_timeout = 0x00000102
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
                kernel32.OpenProcess.restype = wintypes.HANDLE
                kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
                kernel32.WaitForSingleObject.restype = wintypes.DWORD
                kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
                kernel32.CloseHandle.restype = wintypes.BOOL
                handle = kernel32.OpenProcess(synchronize, False, parent_pid)
                if not handle:
                    return False
                try:
                    return kernel32.WaitForSingleObject(handle, 0) == wait_timeout
                finally:
                    kernel32.CloseHandle(handle)
            try:
                os.kill(parent_pid, 0)
                return True
            except OSError:
                return False

        def watch_parent(parent_pid: int):
            while True:
                time.sleep(2.0)
                if not parent_is_alive(parent_pid):
                    logger.warning("Parent process %d exited; terminating bridge", parent_pid)
                    os._exit(0)

        threading.Thread(target=watch_parent, args=(args.parent_pid,), daemon=True).start()

    logger.info("Initializing TqBridge on 127.0.0.1:%d ...", port)
    worker = TqDataWorker(account=account, password=password)
    worker.start()

    app = create_app()
    try:
        web.run_app(app, host="127.0.0.1", port=port, print=logger.info)
    except OSError as e:
        logger.error("Port %d is already in use: %s", port, e)
    finally:
        if worker:
            worker.stop()


if __name__ == "__main__":
    main()
