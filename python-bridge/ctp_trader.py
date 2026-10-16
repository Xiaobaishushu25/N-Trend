"""
SimNow CTP Local HTTP Bridge Service for N-Trend.
Provides fast local REST API endpoints for automated order routing, position tracking, and account querying.
Powered by openctp_ctp.
"""

import argparse
import http.server
import json
import logging
import os
import socketserver
import sys
import threading
import time
from typing import Dict, List, Optional, Any

import openctp_ctp.thosttraderapi as tdapi

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [CtpTrader] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("CtpTrader")

SERVER_STARTED_AT = time.time()
SERVICE_NAME = "ntrend-ctp-trader"

# Exchange Mapping: deduce exchange by instrument prefix if not provided
EXCHANGE_PREFIX_MAP = {
    # SHFE (上期所)
    "cu": "SHFE", "al": "SHFE", "zn": "SHFE", "pb": "SHFE", "ni": "SHFE", "sn": "SHFE",
    "au": "SHFE", "ag": "SHFE", "rb": "SHFE", "wr": "SHFE", "hc": "SHFE", "ss": "SHFE",
    "bu": "SHFE", "ru": "SHFE", "sp": "SHFE", "fu": "SHFE",
    # INE (能源中心)
    "sc": "INE", "nr": "INE", "lu": "INE", "bc": "INE", "ec": "INE",
    # DCE (大商所)
    "c": "DCE", "cs": "DCE", "a": "DCE", "b": "DCE", "m": "DCE", "y": "DCE",
    "p": "DCE", "fb": "DCE", "bb": "DCE", "jd": "DCE", "rr": "DCE", "l": "DCE",
    "v": "DCE", "pp": "DCE", "j": "DCE", "jm": "DCE", "i": "DCE", "eg": "DCE",
    "eb": "DCE", "pg": "DCE", "lh": "DCE",
    # CZCE (郑商所)
    "sr": "CZCE", "cf": "CZCE", "cy": "CZCE", "pm": "CZCE", "wh": "CZCE", "ri": "CZCE",
    "lr": "CZCE", "jr": "CZCE", "oi": "CZCE", "rs": "CZCE", "rm": "CZCE", "ta": "CZCE",
    "ma": "CZCE", "fg": "CZCE", "sf": "CZCE", "sm": "CZCE", "ur": "CZCE", "sa": "CZCE",
    "cz": "CZCE", "pf": "CZCE", "pk": "CZCE", "sh": "CZCE", "px": "CZCE", "ap": "CZCE",
    "cj": "CZCE",
    # CFFEX (中金所)
    "if": "CFFEX", "ih": "CFFEX", "ic": "CFFEX", "im": "CFFEX", "tf": "CFFEX", "t": "CFFEX",
    "ts": "CFFEX", "tl": "CFFEX",
    # GFEX (广期所)
    "si": "GFEX", "lc": "GFEX",
}

def guess_exchange_id(instrument_id: str) -> str:
    cleaned = instrument_id.strip().lower()
    prefix = ""
    for ch in cleaned:
        if ch.isalpha():
            prefix += ch
        else:
            break
    return EXCHANGE_PREFIX_MAP.get(prefix, "SHFE")


class CtpTraderManager(tdapi.CThostFtdcTraderSpi):
    def __init__(self, config: Dict[str, Any]):
        super().__init__()
        self.config = config
        self.api = None
        self.req_id = 100
        self.order_ref_counter = 1000
        self.lock = threading.RLock()

        # State flags
        self.is_connected = False
        self.is_authenticated = False
        self.is_logged_in = False
        self.is_ready = False
        self.trading_day = ""
        self.front_id = 0
        self.session_id = 0
        self._stop_requested = False

        # Memory store
        self.account_data: Dict[str, Any] = {
            "balance": 0.0,
            "available": 0.0,
            "curr_margin": 0.0,
            "close_profit": 0.0,
            "position_profit": 0.0,
            "updated_at": 0,
        }
        # key: (instrument_id, direction)
        self.positions: Dict[str, Dict[str, Any]] = {}
        # key: order_ref
        self.orders: Dict[str, Dict[str, Any]] = {}
        # list of trades
        self.trades: List[Dict[str, Any]] = []
        # updates event queue for incremental polling
        self.update_events: List[Dict[str, Any]] = []
        self.event_counter = 0
        self._condition = threading.Condition(self.lock)
        self._query_thread = None
        self._settlement_confirmed = False
        self._pending_queries = {}
        self._inflight = None
        self._last_query_sent = 0.0
        self._query_interval = max(1.5, float(config.get("QueryIntervalSeconds", 1.5)))
        self._reconcile_interval = max(10.0, float(config.get("ReconcileSeconds", 30)))
        self._query_timeout = max(5.0, float(config.get("QueryTimeoutSeconds", 15)))
        self._synced = set()
        self._data_revision = 0
        self.positions_updated_at = 0.0
        self.sync_error = None
        self._seen_trades = set()
        self._order_versions = {}
        self._order_requests = {}
        self.last_trade_at = 0.0
        self.orders_updated_at = 0.0
        self.trades_updated_at = 0.0
        self._journal_path = config.get("OrderJournalPath")
        self._submitted_requests = {}
        if self._journal_path and os.path.exists(self._journal_path):
            with open(self._journal_path, "r", encoding="utf-8") as journal:
                self._submitted_requests = json.load(journal)

    def get_req_id(self) -> int:
        with self.lock:
            self.req_id += 1
            return self.req_id

    def get_order_ref(self) -> str:
        with self.lock:
            self.order_ref_counter += 1
            return str(self.order_ref_counter)

    def emit_event(self, event_type: str, data: Any):
        with self.lock:
            self.event_counter += 1
            evt = {
                "id": self.event_counter,
                "type": event_type,
                "data": data,
                "ts": time.time(),
            }
            self.update_events.append(evt)
            # keep max 500 events
            if len(self.update_events) > 500:
                self.update_events.pop(0)
            self._data_revision += 1
            self._condition.notify_all()

    # ── CTP SPI Callbacks ──

    def OnFrontConnected(self):
        logger.info("[交易前置] 前置机连接成功，开始穿透式认证...")
        self.is_connected = True
        req = tdapi.CThostFtdcReqAuthenticateField()
        req.BrokerID = self.config.get("BrokerID", "9999")
        req.UserID = self.config.get("UserID", "")
        req.AppID = self.config.get("AppID", "simnow_client_test")
        req.AuthCode = self.config.get("AuthCode", "0000000000000000")
        self.api.ReqAuthenticate(req, self.get_req_id())

    def OnFrontDisconnected(self, nReason: int):
        logger.warning(f"[交易前置] 前置机连接断开 (代码: {nReason})")
        self.is_connected = False
        self.is_authenticated = False
        self.is_logged_in = False
        with self._condition:
            self.is_ready = False
            self._settlement_confirmed = False
            self._synced.clear()
            self._inflight = None
            self._pending_queries.clear()
            self._condition.notify_all()
        self.emit_event("disconnected", {"reason": nReason})

    def OnRspAuthenticate(self, pRspAuthenticateField, pRspInfo, nRequestID, bIsLast):
        if pRspInfo and pRspInfo.ErrorID != 0:
            logger.error(f"[穿透认证] 认证失败: {pRspInfo.ErrorMsg}")
            self.is_authenticated = False
            # 柜台前置可能未初始化或不活跃，延迟 10 秒后自动重试穿透认证
            def retry_auth():
                time.sleep(10)
                if self.is_connected and not self.is_authenticated and not self._stop_requested:
                    logger.info("[穿透认证] 正在自动重试穿透式认证...")
                    req = tdapi.CThostFtdcReqAuthenticateField()
                    req.BrokerID = self.config.get("BrokerID", "9999")
                    req.UserID = self.config.get("UserID", "")
                    req.AppID = self.config.get("AppID", "simnow_client_test")
                    req.AuthCode = self.config.get("AuthCode", "0000000000000000")
                    self.api.ReqAuthenticate(req, self.get_req_id())
            threading.Thread(target=retry_auth, daemon=True).start()
            return
        logger.info("[穿透认证] 穿透式认证成功，开始用户登录...")
        self.is_authenticated = True
        login_req = tdapi.CThostFtdcReqUserLoginField()
        login_req.BrokerID = self.config.get("BrokerID", "9999")
        login_req.UserID = self.config.get("UserID", "")
        login_req.Password = self.config.get("Password", "")
        self.api.ReqUserLogin(login_req, self.get_req_id())

    def OnRspUserLogin(self, pRspUserLogin, pRspInfo, nRequestID, bIsLast):
        if pRspInfo and pRspInfo.ErrorID != 0:
            logger.error(f"[用户登录] 登录失败: {pRspInfo.ErrorMsg}")
            self.is_logged_in = False
            # 延迟 10 秒后自动重试登录
            def retry_login():
                time.sleep(10)
                if self.is_connected and self.is_authenticated and not self.is_logged_in and not self._stop_requested:
                    logger.info("[用户登录] 正在自动重试用户登录...")
                    login_req = tdapi.CThostFtdcReqUserLoginField()
                    login_req.BrokerID = self.config.get("BrokerID", "9999")
                    login_req.UserID = self.config.get("UserID", "")
                    login_req.Password = self.config.get("Password", "")
                    self.api.ReqUserLogin(login_req, self.get_req_id())
            threading.Thread(target=retry_login, daemon=True).start()
            return
        self.is_logged_in = True
        day = str(pRspUserLogin.TradingDay)
        if self.trading_day and self.trading_day != day:
            with self.lock:
                self.orders.clear()
                self.trades.clear()
                self._seen_trades.clear()
                self._order_versions.clear()
                self._synced.clear()
                self.is_ready = False
        self.trading_day = day
        self.front_id = int(pRspUserLogin.FrontID)
        self.session_id = int(pRspUserLogin.SessionID)
        # Initialize order_ref offset based on MaxOrderRef to prevent conflict
        try:
            max_ref = int(pRspUserLogin.MaxOrderRef)
            with self.lock:
                if max_ref >= self.order_ref_counter:
                    self.order_ref_counter = max_ref + 100
        except Exception:
            pass

        logger.info(
            f"[用户登录] 登录成功！交易日: {self.trading_day}，FrontID: {self.front_id}，SessionID: {self.session_id}，初始OrderRef: {self.order_ref_counter}"
        )
        # 确认结算单
        settle_req = tdapi.CThostFtdcSettlementInfoConfirmField()
        settle_req.BrokerID = self.config.get("BrokerID", "9999")
        settle_req.InvestorID = self.config.get("UserID", "")
        self.api.ReqSettlementInfoConfirm(settle_req, self.get_req_id())

    def OnRspSettlementInfoConfirm(self, pConfirm, pRspInfo, nRequestID, bIsLast):
        if pRspInfo and pRspInfo.ErrorID != 0:
            self.sync_error = f"结算确认失败: {pRspInfo.ErrorMsg}"
            self.emit_event("sync_error", self.sync_error)
            return
        if not bIsLast:
            return
        self._settlement_confirmed = True
        logger.info("[结算确认] 开始初始化资金与持仓快照")
        self._start_periodic_query()
        self.request_sync()

    def request_sync(self, delay: float = 0.0):
        # Merge bursts without postponing the first pending refresh indefinitely.
        with self._condition:
            due = time.monotonic() + delay
            for kind in ("positions", "account", "orders", "trades"):
                self._pending_queries[kind] = min(self._pending_queries.get(kind, due), due)
            self._condition.notify_all()

    def query_account(self, force: bool = False):
        # Compatibility: force requests a refresh but never bypasses flow control.
        self._enqueue_query("account")

    def query_positions(self, force: bool = False):
        self._enqueue_query("positions")

    def _enqueue_query(self, kind):
        with self._condition:
            self._pending_queries.setdefault(kind, time.monotonic())
            self._condition.notify_all()

    def _start_periodic_query(self):
        with self._condition:
            if self._query_thread and self._query_thread.is_alive():
                return
            self._query_thread = threading.Thread(target=self._query_loop, daemon=True)
            self._query_thread.start()

    def _query_loop(self):
        next_reconcile = time.monotonic() + self._reconcile_interval
        while not self._stop_requested:
            with self._condition:
                now = time.monotonic()
                if now >= next_reconcile:
                    if self._settlement_confirmed:
                        self.request_sync()
                    next_reconcile = now + self._reconcile_interval
                if self._inflight:
                    if now - self._inflight["started"] >= self._query_timeout and not self._inflight.get("timed_out"):
                        # A timeout does not prove the CTP query has finished. Keep
                        # the slot occupied until its final response or reconnect.
                        self._inflight["timed_out"] = True
                        self.sync_error = "柜台查询响应超时，等待结束回报或重连"
                        self.is_ready = False
                        self.emit_event("sync_error", self.sync_error)
                    self._condition.wait(0.5)
                    continue
                if not self.is_logged_in or not self._settlement_confirmed or not self._pending_queries:
                    self._condition.wait(0.5)
                    continue
                kind = min(self._pending_queries, key=self._pending_queries.get)
                due = max(self._pending_queries[kind], self._last_query_sent + self._query_interval)
                if now < due:
                    self._condition.wait(min(due - now, 0.5))
                    continue
                del self._pending_queries[kind]
                req_id = self.get_req_id()
                self._inflight = {"id": req_id, "kind": kind, "started": now, "rows": {}, "error": None,
                                  "revision": self._data_revision}
                self._last_query_sent = now
                try:
                    if kind == "account":
                        req = tdapi.CThostFtdcQryTradingAccountField()
                        send = self.api.ReqQryTradingAccount
                    elif kind == "positions":
                        req = tdapi.CThostFtdcQryInvestorPositionField()
                        send = self.api.ReqQryInvestorPosition
                    elif kind == "orders":
                        req = tdapi.CThostFtdcQryOrderField()
                        send = self.api.ReqQryOrder
                    else:
                        req = tdapi.CThostFtdcQryTradeField()
                        send = self.api.ReqQryTrade
                    req.BrokerID = self.config.get("BrokerID", "9999")
                    req.InvestorID = self.config.get("UserID", "")
                    ret = send(req, req_id)
                    if ret != 0:
                        self._finish_query(req_id, f"{kind} 查询发送失败: {ret}")
                except Exception as exc:
                    self._finish_query(req_id, str(exc))

    def _finish_query(self, req_id, error=None):
        # Caller holds the shared RLock; stale responses cannot modify snapshots.
        query = self._inflight
        if not query or query["id"] != req_id:
            return
        kind = query["kind"]
        error = error or query["error"]
        self._inflight = None
        if error:
            self.sync_error = error
            self.is_ready = False
            self._pending_queries[kind] = time.monotonic() + max(3.0, self._query_interval)
            logger.warning("[同步失败] %s", error)
            self.emit_event("sync_error", error)
        else:
            if kind == "positions":
                self.positions = query["rows"]
                self.positions_updated_at = time.time()
                self.emit_event("positions", list(self.positions.values()))
            elif kind == "orders":
                for ref, record in query["rows"].items():
                    if self._order_versions.get(ref, 0) <= query["revision"]:
                        self.orders[ref] = record
                self.orders_updated_at = time.time()
                self.emit_event("orders", list(self.orders.values()))
            elif kind == "trades":
                for record in query["rows"].values():
                    self._merge_trade(record)
                self.trades_updated_at = time.time()
                self.emit_event("trades", self.trades)
            elif query["rows"]:
                self.account_data = query["rows"]
                self.emit_event("account", self.account_data)
            else:
                self._pending_queries[kind] = time.monotonic() + 3.0
                self.sync_error = "资金查询返回空结果"
                self.is_ready = False
                self.emit_event("sync_error", self.sync_error)
                self._condition.notify_all()
                return
            self._synced.add(kind)
            if self._synced == {"account", "positions", "orders", "trades"} and not self._pending_queries:
                self.sync_error = None
                self.is_ready = True
                self.emit_event("ready", {"trading_day": self.trading_day})
        self._condition.notify_all()

    def OnRspError(self, pRspInfo, nRequestID, bIsLast):
        if pRspInfo and pRspInfo.ErrorID:
            with self._condition:
                query = self._inflight
                if query and query["id"] == nRequestID:
                    query["error"] = f"柜台查询错误 [{pRspInfo.ErrorID}]: {pRspInfo.ErrorMsg}"
                    if bIsLast:
                        self._finish_query(nRequestID)
                elif nRequestID in self._order_requests:
                    self._record_order_error(self._order_requests[nRequestID], f"柜台拒单 [{pRspInfo.ErrorID}]: {pRspInfo.ErrorMsg}")

    def OnRspQryTradingAccount(self, pAccount, pRspInfo, nRequestID, bIsLast):
        with self._condition:
            query = self._inflight
            if not query or query["id"] != nRequestID or query["kind"] != "account":
                return
            if pRspInfo and pRspInfo.ErrorID:
                query["error"] = f"资金查询错误 [{pRspInfo.ErrorID}]: {pRspInfo.ErrorMsg}"
            elif pAccount and str(getattr(pAccount, "CurrencyID", "CNY")) in ("", "CNY"):
                balance = float(pAccount.Balance)
                margin = float(pAccount.CurrMargin)
                query["rows"] = {
                    "account_id": self.config.get("UserID", ""),
                    "pre_balance": float(pAccount.PreBalance),
                    "balance": balance, "available": float(pAccount.Available),
                    "curr_margin": margin, "margin": margin,
                    "close_profit": float(pAccount.CloseProfit),
                    "position_profit": float(pAccount.PositionProfit),
                    "commission": float(pAccount.Commission),
                    "risk_ratio": margin / balance if balance > 0 else 0.0,
                    "updated_at": time.time(),
                }
            if bIsLast:
                self._finish_query(nRequestID)

    def OnRspQryInvestorPosition(self, pPosition, pRspInfo, nRequestID, bIsLast):
        with self._condition:
            query = self._inflight
            if not query or query["id"] != nRequestID or query["kind"] != "positions":
                return
            if pRspInfo and pRspInfo.ErrorID:
                query["error"] = f"持仓查询错误 [{pRspInfo.ErrorID}]: {pRspInfo.ErrorMsg}"
            elif pPosition and pPosition.Position > 0:
                inst = str(pPosition.InstrumentID)
                direction = "buy" if pPosition.PosiDirection == tdapi.THOST_FTDC_PD_Long else "sell"
                key = f"{inst}_{direction}"
                row = query["rows"].setdefault(key, {
                    "instrument_id": inst, "direction": direction,
                    "position": 0, "yd_position": 0, "today_position": 0,
                    "open_cost": 0.0, "position_cost": 0.0,
                    "position_profit": 0.0, "margin": 0.0,
                })
                volume = int(pPosition.Position)
                today = min(volume, max(0, int(pPosition.TodayPosition)))
                row["position"] += volume
                row["today_position"] += today
                # YdPosition is a historical field; use the current remaining lots.
                row["yd_position"] += volume - today
                row["open_cost"] += float(pPosition.OpenCost)
                row["position_cost"] += float(pPosition.PositionCost)
                row["position_profit"] += float(pPosition.PositionProfit)
                row["margin"] += float(pPosition.UseMargin)
            if bIsLast:
                self._finish_query(nRequestID)

    def health_snapshot(self):
        with self.lock:
            return {
                "service": SERVICE_NAME, "healthy": self.is_ready,
                "pid": os.getpid(), "uptime": time.time() - SERVER_STARTED_AT,
                "connected": self.is_connected, "authenticated": self.is_authenticated,
                "logged_in": self.is_logged_in, "ready": self.is_ready,
                "settlement_confirmed": self._settlement_confirmed,
                "trading_day": self.trading_day,
                "broker_id": self.config.get("BrokerID"), "user_id": self.config.get("UserID"),
                "account_id": self.config.get("UserID"),
                "account_updated_at": self.account_data.get("updated_at", 0),
                "positions_updated_at": self.positions_updated_at,
                "orders_updated_at": self.orders_updated_at,
                "trades_updated_at": self.trades_updated_at,
                "sync_pending": bool(self._pending_queries or self._inflight),
                "sync_error": self.sync_error,
            }

    def stream_snapshot(self):
        with self.lock:
            return {"health": self.health_snapshot(), "account": dict(self.account_data),
                    "broker_positions": list(self.positions.values()), "revision": self._data_revision,
                    "orders": list(self.orders.values()), "trades": list(self.trades), "last_trade_at": self.last_trade_at}

    def stop(self):
        with self._condition:
            self._stop_requested = True
            self._condition.notify_all()
        if self._query_thread:
            self._query_thread.join(timeout=2.0)

    # ── 报单状态回报 ──
    def _order_record(self, pOrder):
        if not pOrder:
            return
        ref = str(pOrder.OrderRef)
        inst = str(pOrder.InstrumentID)
        status_map = {
            tdapi.THOST_FTDC_OST_AllTraded: "all_traded",
            tdapi.THOST_FTDC_OST_PartTradedQueueing: "part_traded",
            tdapi.THOST_FTDC_OST_PartTradedNotQueueing: "part_traded_canceled",
            tdapi.THOST_FTDC_OST_NoTradeQueueing: "queueing",
            tdapi.THOST_FTDC_OST_NoTradeNotQueueing: "canceled",
            tdapi.THOST_FTDC_OST_Canceled: "canceled",
            tdapi.THOST_FTDC_OST_Touched: "touched",
            tdapi.THOST_FTDC_OST_Unknown: "unknown",
        }
        status_code = status_map.get(pOrder.OrderStatus, "unknown")
        if getattr(pOrder, "OrderSubmitStatus", None) == getattr(tdapi, "THOST_FTDC_OSS_InsertRejected", "4"):
            status_code = "rejected"
        dir_str = "buy" if pOrder.Direction == tdapi.THOST_FTDC_D_Buy else "sell"
        offset_map = {
            tdapi.THOST_FTDC_OF_Open: "open",
            tdapi.THOST_FTDC_OF_Close: "close",
            tdapi.THOST_FTDC_OF_CloseToday: "close_today",
            tdapi.THOST_FTDC_OF_CloseYesterday: "close_yesterday",
        }
        offset_str = offset_map.get(str(pOrder.CombOffsetFlag)[:1], "unknown")

        order_record = {
            "order_ref": ref,
            "instrument_id": inst,
            "exchange_id": str(pOrder.ExchangeID),
            "front_id": int(pOrder.FrontID),
            "session_id": int(pOrder.SessionID),
            "order_sys_id": str(pOrder.OrderSysID).strip(),
            "direction": dir_str,
            "offset": offset_str,
            "limit_price": float(pOrder.LimitPrice),
            "volume_total_original": int(pOrder.VolumeTotalOriginal),
            "volume_traded": int(pOrder.VolumeTraded),
            "volume_total": int(pOrder.VolumeTotal),
            "status": status_code,
            "status_msg": str(pOrder.StatusMsg),
            "insert_time": str(pOrder.InsertTime),
            "update_time": str(pOrder.UpdateTime),
        }
        return order_record

    def OnRtnOrder(self, pOrder):
        if not pOrder:
            return
        record = self._order_record(pOrder)
        with self.lock:
            self.orders[record["order_ref"]] = record
            self._order_versions[record["order_ref"]] = self._data_revision + 1
        self.request_sync(delay=0.5)
        self.emit_event("order_update", record)

    def OnRspQryOrder(self, pOrder, pRspInfo, nRequestID, bIsLast):
        with self._condition:
            query = self._inflight
            if not query or query["id"] != nRequestID or query["kind"] != "orders":
                return
            if pRspInfo and pRspInfo.ErrorID:
                query["error"] = f"委托查询失败: {pRspInfo.ErrorMsg}"
            elif pOrder:
                record = self._order_record(pOrder)
                query["rows"][record["order_ref"]] = record
            if bIsLast:
                self._finish_query(nRequestID)

    # ── 成交回报 ──
    def _trade_record(self, pTrade):
        if not pTrade:
            return
        inst = str(pTrade.InstrumentID)
        ref = str(pTrade.OrderRef)
        dir_str = "buy" if pTrade.Direction == tdapi.THOST_FTDC_D_Buy else "sell"
        offset_map = {
            tdapi.THOST_FTDC_OF_Open: "open",
            tdapi.THOST_FTDC_OF_Close: "close",
            tdapi.THOST_FTDC_OF_CloseToday: "close_today",
            tdapi.THOST_FTDC_OF_CloseYesterday: "close_yesterday",
        }
        offset_str = offset_map.get(pTrade.OffsetFlag, "unknown")
        trade_record = {
            "trade_id": str(pTrade.TradeID),
            "order_ref": ref,
            "instrument_id": inst,
            "exchange_id": str(pTrade.ExchangeID),
            "direction": dir_str,
            "offset": offset_str,
            "price": float(pTrade.Price),
            "volume": int(pTrade.Volume),
            "trade_time": str(pTrade.TradeTime),
            "trade_date": str(pTrade.TradeDate),
            "trading_day": str(getattr(pTrade, "TradingDay", "") or pTrade.TradeDate),
        }
        return trade_record

    def _merge_trade(self, record):
        key = (record.get("trading_day") or record["trade_date"], record["exchange_id"], record["trade_id"], record["instrument_id"], record["direction"])
        if key in self._seen_trades:
            return False
        self._seen_trades.add(key)
        self.trades.append(record)
        return True

    def OnRtnTrade(self, pTrade):
        if not pTrade:
            return
        record = self._trade_record(pTrade)
        with self.lock:
            if not self._merge_trade(record):
                return
            self.last_trade_at = time.time()
        self.request_sync(delay=0.5)
        self.emit_event("trade", record)

    def OnRspQryTrade(self, pTrade, pRspInfo, nRequestID, bIsLast):
        with self._condition:
            query = self._inflight
            if not query or query["id"] != nRequestID or query["kind"] != "trades":
                return
            if pRspInfo and pRspInfo.ErrorID:
                query["error"] = f"成交查询失败: {pRspInfo.ErrorMsg}"
            elif pTrade:
                record = self._trade_record(pTrade)
                key = (record.get("trading_day") or record["trade_date"], record["exchange_id"], record["trade_id"], record["instrument_id"], record["direction"])
                query["rows"][key] = record
            if bIsLast:
                self._finish_query(nRequestID)

    def _record_order_error(self, pInputOrder, msg):
        if not pInputOrder:
            return
        ref = str(pInputOrder.OrderRef)
        with self.lock:
            record = dict(self.orders.get(ref, {}))
            record.update(order_ref=ref, instrument_id=str(pInputOrder.InstrumentID),
                          status="rejected", status_msg=msg, volume_traded=record.get("volume_traded", 0))
            self.orders[ref] = record
            self._order_versions[ref] = self._data_revision + 1
        self.emit_event("order_error", record)

    def OnRspOrderInsert(self, pInputOrder, pRspInfo, nRequestID, bIsLast):
        if pRspInfo and pRspInfo.ErrorID != 0:
            msg = f"柜台拒绝报单 [{pRspInfo.ErrorID}]: {pRspInfo.ErrorMsg}"
            logger.error(f"[错误] {msg}")
            ref = str(pInputOrder.OrderRef) if pInputOrder else ""
            self._record_order_error(pInputOrder, msg)

    def OnErrRtnOrderInsert(self, pInputOrder, pRspInfo):
        if pRspInfo and pRspInfo.ErrorID != 0:
            msg = f"交易所拒绝报单 [{pRspInfo.ErrorID}]: {pRspInfo.ErrorMsg}"
            logger.error(f"[错误] {msg}")
            ref = str(pInputOrder.OrderRef) if pInputOrder else ""
            self._record_order_error(pInputOrder, msg)

    # ── 下单方法 ──
    def _save_journal(self):
        if not self._journal_path:
            return
        temp = self._journal_path + ".tmp"
        with open(temp, "w", encoding="utf-8") as file:
            json.dump(self._submitted_requests, file, ensure_ascii=False)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, self._journal_path)

    def send_order(self, instrument_id, direction, offset, price, volume, exchange_id=None, custom_ref=None):
        with self.lock:
            fingerprint = json.dumps([instrument_id, direction, offset, price, volume, exchange_id])
            if custom_ref and custom_ref in self._submitted_requests:
                old = self._submitted_requests[custom_ref]
                if old["fingerprint"] != fingerprint:
                    return {"success": False, "msg": "委托标识已用于不同报单参数"}
                return old["result"]
            if custom_ref:
                self._submitted_requests[custom_ref] = {"fingerprint": fingerprint, "result": {
                    "success": True, "order_ref": custom_ref, "msg": "提交结果待查询确认，不能重复提交"}}
                try:
                    self._save_journal()  # Before any call which could reach the counter.
                except OSError as exc:
                    self._submitted_requests.pop(custom_ref, None)
                    return {"success": False, "order_ref": custom_ref, "msg": f"委托日志保存失败，未提交: {exc}"}
            result = self._send_order(instrument_id, direction, offset, price, volume, exchange_id, custom_ref)
            if custom_ref:
                self._submitted_requests[custom_ref]["result"] = result
                try:
                    self._save_journal()
                except OSError:
                    logger.exception("报单结果保存失败；初始提交意图仍保留，禁止盲目重报")
            return result

    def _send_order(
        self,
        instrument_id: str,
        direction: str,  # "buy" or "sell"
        offset: str,     # "open", "close", "close_today", "auto"
        price: float,
        volume: int,
        exchange_id: Optional[str] = None,
        custom_ref: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not self.is_ready:
            return {"success": False, "msg": "CTP交易前置未就绪 (尚未登录或结算单未确认)"}

        inst = instrument_id.strip()
        exch = exchange_id or guess_exchange_id(inst)
        order_ref = custom_ref or self.get_order_ref()
        if custom_ref:
            self.order_ref_counter = max(self.order_ref_counter, int(custom_ref))

        # 处理开平方向
        c_dir = tdapi.THOST_FTDC_D_Buy if direction.lower() == "buy" else tdapi.THOST_FTDC_D_Sell

        # 智能平仓推导
        c_offset = tdapi.THOST_FTDC_OF_Open
        if offset.lower() == "open":
            c_offset = tdapi.THOST_FTDC_OF_Open
        elif offset.lower() == "close_today":
            c_offset = tdapi.THOST_FTDC_OF_CloseToday
        elif offset.lower() == "close":
            c_offset = tdapi.THOST_FTDC_OF_Close
        elif offset.lower() == "auto":
            if self.positions_updated_at <= self.last_trade_at:
                return {"success": False, "order_ref": order_ref, "msg": "等待成交后的柜台持仓同步，未提交平仓"}
            # 上期所 (SHFE) 与 能源中心 (INE) 区分平今仓与平昨仓
            if exch in ("SHFE", "INE"):
                # 查询该合约持仓：如果持有今仓，优先平今仓
                pos_key = f"{inst}_{'sell' if direction.lower() == 'buy' else 'buy'}"
                pos = self.positions.get(pos_key)
                if pos and pos.get("today_position", 0) > 0:
                    c_offset = tdapi.THOST_FTDC_OF_CloseToday
                    volume = min(volume, int(pos["today_position"]))
                else:
                    c_offset = tdapi.THOST_FTDC_OF_Close
            else:
                c_offset = tdapi.THOST_FTDC_OF_Close

        req = tdapi.CThostFtdcInputOrderField()
        req.BrokerID = self.config.get("BrokerID", "9999")
        req.InvestorID = self.config.get("UserID", "")
        req.UserID = self.config.get("UserID", "")
        req.InstrumentID = inst
        req.ExchangeID = exch
        req.OrderRef = order_ref

        req.OrderPriceType = tdapi.THOST_FTDC_OPT_LimitPrice
        req.Direction = c_dir
        req.CombOffsetFlag = c_offset
        req.CombHedgeFlag = tdapi.THOST_FTDC_HF_Speculation

        req.LimitPrice = float(price)
        req.VolumeTotalOriginal = int(volume)

        req.TimeCondition = tdapi.THOST_FTDC_TC_GFD
        req.VolumeCondition = tdapi.THOST_FTDC_VC_AV
        req.MinVolume = 1
        req.ContingentCondition = tdapi.THOST_FTDC_CC_Immediately
        req.StopPrice = 0.0
        req.ForceCloseReason = tdapi.THOST_FTDC_FCC_NotForceClose
        req.IsAutoSuspend = 0
        req.UserForceClose = 0

        req_id = self.get_req_id()
        self._order_requests[req_id] = req
        res = self.api.ReqOrderInsert(req, req_id)
        offset_label = "开仓" if c_offset == tdapi.THOST_FTDC_OF_Open else "平仓"
        dir_label = "买" if c_dir == tdapi.THOST_FTDC_D_Buy else "卖"
        logger.info(
            f"[发送委托] -> {offset_label}{dir_label} {inst} {volume}手 @ {price:.1f} (OrderRef: {order_ref}, Exch: {exch})，调用返回码: {res}"
        )
        if res == 0:
            return {"success": True, "order_ref": order_ref, "msg": "报单已发送到柜台"}
        else:
            return {"success": False, "order_ref": order_ref, "msg": f"报单提交失败 (错误码: {res})"}

    def cancel_order(
        self,
        order_ref: str,
        instrument_id: str,
        exchange_id: Optional[str] = None,
        front_id: Optional[int] = None,
        session_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        # A stale/failed snapshot must not prevent cancelling a live order.
        if not self.is_logged_in or not self._settlement_confirmed:
            return {"success": False, "msg": "CTP交易连接未就绪"}
        req = tdapi.CThostFtdcInputOrderActionField()
        req.BrokerID = self.config.get("BrokerID", "9999")
        req.InvestorID = self.config.get("UserID", "")
        req.UserID = self.config.get("UserID", "")
        req.OrderRef = order_ref
        req.InstrumentID = instrument_id
        req.ExchangeID = exchange_id or guess_exchange_id(instrument_id)
        known = self.orders.get(order_ref, {})
        req.FrontID = front_id if front_id is not None else known.get("front_id", self.front_id)
        req.SessionID = session_id if session_id is not None else known.get("session_id", self.session_id)
        if known.get("order_sys_id"):
            req.OrderSysID = known["order_sys_id"]
        req.ActionFlag = tdapi.THOST_FTDC_AF_Delete
        res = self.api.ReqOrderAction(req, self.get_req_id())
        logger.info(f"[撤单请求] 撤销 OrderRef: {order_ref} ({instrument_id})，返回值: {res}")
        if res == 0:
            return {"success": True, "msg": "撤单指令已发送"}
        else:
            return {"success": False, "msg": f"撤单指令失败 (错误码: {res})"}


# ── HTTP Server Request Handler ──
g_trader_mgr: Optional[CtpTraderManager] = None

class CtpHttpHandler(http.server.BaseHTTPRequestHandler):
    def _send_json(self, status_code: int, data: Any):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        global g_trader_mgr
        if not g_trader_mgr:
            return self._send_json(500, {"error": "Trader manager not initialized"})

        path = self.path.split("?")[0]
        if path == "/events":
            # SSE stays on loopback. Each message is a full snapshot, so reconnects
            # and slow consumers recover without replaying a truncated event log.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            revision = -1
            try:
                self.connection.settimeout(20)
                while not g_trader_mgr._stop_requested:
                    with g_trader_mgr._condition:
                        if revision == g_trader_mgr._data_revision:
                            g_trader_mgr._condition.wait(15)
                        snapshot = g_trader_mgr.stream_snapshot()
                        revision = snapshot["revision"]
                    data = json.dumps(snapshot, ensure_ascii=False)
                    self.wfile.write(("data: " + data + "\n\n").encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return
        elif path == "/snapshot":
            return self._send_json(200, g_trader_mgr.stream_snapshot())
        elif path == "/health":
            return self._send_json(200, g_trader_mgr.health_snapshot())
        elif path == "/account":
            return self._send_json(200, g_trader_mgr.account_data)
        elif path == "/positions":
            with g_trader_mgr.lock:
                return self._send_json(200, list(g_trader_mgr.positions.values()))
        elif path == "/orders":
            with g_trader_mgr.lock:
                return self._send_json(200, list(g_trader_mgr.orders.values()))
        elif path == "/trades":
            with g_trader_mgr.lock:
                return self._send_json(200, g_trader_mgr.trades[-100:])
        elif path == "/updates":
            # parse ?since=ID
            since_id = 0
            if "?" in self.path:
                try:
                    query = self.path.split("?")[1]
                    for param in query.split("&"):
                        if param.startswith("since="):
                            since_id = int(param.split("=")[1])
                except Exception:
                    pass
            with g_trader_mgr.lock:
                new_events = [e for e in g_trader_mgr.update_events if e["id"] > since_id]
                return self._send_json(200, {"events": new_events, "last_id": g_trader_mgr.event_counter})
        else:
            return self._send_json(404, {"error": f"Unknown endpoint: {path}"})

    def do_POST(self):
        global g_trader_mgr
        if not g_trader_mgr:
            return self._send_json(500, {"error": "Trader manager not initialized"})

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b"{}"
        try:
            params = json.loads(body.decode("utf-8")) if body else {}
        except Exception as e:
            return self._send_json(400, {"error": f"Invalid JSON payload: {e}"})

        path = self.path.split("?")[0]
        if path == "/order/insert":
            inst = params.get("instrument_id")
            dir_str = params.get("direction")
            offset_str = params.get("offset", "auto")
            price = float(params.get("price", 0.0))
            vol = int(params.get("volume", 1))
            exchange = params.get("exchange_id")
            ref = params.get("order_ref")

            if not inst or not dir_str or price <= 0.0 or vol <= 0:
                return self._send_json(400, {"error": "Missing or invalid order parameters"})

            res = g_trader_mgr.send_order(
                instrument_id=inst,
                direction=dir_str,
                offset=offset_str,
                price=price,
                volume=vol,
                exchange_id=exchange,
                custom_ref=ref,
            )
            return self._send_json(200, res)

        elif path == "/order/cancel":
            ref = params.get("order_ref")
            inst = params.get("instrument_id")
            exch = params.get("exchange_id")
            if not ref or not inst:
                return self._send_json(400, {"error": "Missing order_ref or instrument_id"})
            res = g_trader_mgr.cancel_order(order_ref=ref, instrument_id=inst, exchange_id=exch)
            return self._send_json(200, res)

        elif path == "/emergency/cancel_all":
            # 撤销所有排队中订单
            canceled_count = 0
            with g_trader_mgr.lock:
                for ref, ord_info in list(g_trader_mgr.orders.items()):
                    if ord_info.get("status") in ("queueing", "part_traded"):
                        g_trader_mgr.cancel_order(
                            order_ref=ref,
                            instrument_id=ord_info["instrument_id"],
                            exchange_id=ord_info.get("exchange_id"),
                        )
                        canceled_count += 1
            return self._send_json(200, {"success": True, "canceled_count": canceled_count})

        elif path == "/emergency/close_all":
            # 一键全平所有持仓
            closed_orders = []
            with g_trader_mgr.lock:
                for pos in list(g_trader_mgr.positions.values()):
                    inst = pos["instrument_id"]
                    vol = pos["position"]
                    if vol <= 0:
                        continue
                    # 如果持有多头，平仓为卖平；如果持有空头，平仓为买平
                    close_dir = "sell" if pos["direction"] == "buy" else "buy"
                    # 以涨跌停/超滑点对价平仓
                    price = pos["open_cost"] # 参考价，实际可由客户端传入最新价
                    res = g_trader_mgr.send_order(
                        instrument_id=inst,
                        direction=close_dir,
                        offset="auto",
                        price=price,
                        volume=vol,
                    )
                    closed_orders.append({"instrument_id": inst, "result": res})
            return self._send_json(200, {"success": True, "closed_orders": closed_orders})

        elif path == "/sync":
            g_trader_mgr.request_sync()
            return self._send_json(200, {"success": True, "msg": "Sync requested"})

        else:
            return self._send_json(404, {"error": f"Unknown POST endpoint: {path}"})


def main():
    parser = argparse.ArgumentParser(description="SimNow CTP Local HTTP Bridge")
    parser.add_argument("--port", type=int, default=8766, help="HTTP service port (default: 8766)")
    parser.add_argument("--config", type=str, default="", help="Path to config JSON")
    args = parser.parse_args()

    # Load configuration
    config = {
        "BrokerID": "9999",
        "UserID": "278272",
        "Password": "qwertyuiop123*",
        "AppID": "simnow_client_test",
        "AuthCode": "0000000000000000",
        "TdFront": "tcp://182.254.243.31:30001",
        "MdFront": "tcp://182.254.243.31:30011",
    }
    if args.config and os.path.exists(args.config):
        try:
            with open(args.config, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                config.update(loaded)
                logger.info(f"Loaded config from {args.config}")
        except Exception as e:
            logger.error(f"Failed to load config {args.config}: {e}")

    # Create CTP Trader API
    global g_trader_mgr
    logger.info(f"Connecting to CTP Trader Front: {config['TdFront']} ...")
    flow_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "flow_ctp")
    os.makedirs(flow_dir, exist_ok=True)

    trader_api = tdapi.CThostFtdcTraderApi.CreateFtdcTraderApi(flow_dir + os.sep)
    config.setdefault("OrderJournalPath", os.path.join(flow_dir, "order_requests.json"))
    g_trader_mgr = CtpTraderManager(config)
    g_trader_mgr.api = trader_api
    trader_api.RegisterSpi(g_trader_mgr)
    trader_api.RegisterFront(config["TdFront"])
    trader_api.SubscribePublicTopic(tdapi.THOST_TERT_QUICK)
    trader_api.SubscribePrivateTopic(tdapi.THOST_TERT_RESUME)
    trader_api.Init()

    # Start HTTP Server
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    server_address = ("127.0.0.1", args.port)
    httpd = socketserver.ThreadingTCPServer(server_address, CtpHttpHandler)
    httpd.daemon_threads = True
    logger.info(f"🚀 SimNow CTP Bridge HTTP Service running at http://127.0.0.1:{args.port}")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Stopping CTP Bridge...")
    finally:
        if g_trader_mgr:
            g_trader_mgr.stop()
        if trader_api:
            trader_api.Release()
        httpd.server_close()


if __name__ == "__main__":
    main()
