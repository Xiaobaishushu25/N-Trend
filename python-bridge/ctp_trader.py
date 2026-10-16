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
        self.is_ready = False
        self.emit_event("disconnected", {"reason": nReason})

    def OnRspAuthenticate(self, pRspAuthenticateField, pRspInfo, nRequestID, bIsLast):
        if pRspInfo and pRspInfo.ErrorID != 0:
            logger.error(f"[穿透认证] 认证失败: {pRspInfo.ErrorMsg}")
            self.is_authenticated = False
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
            return
        self.is_logged_in = True
        self.trading_day = str(pRspUserLogin.TradingDay)
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
        logger.info("[结算确认] 投资者结算结果已确认！交易服务就绪 (READY)")
        self.is_ready = True
        self.emit_event("ready", {"trading_day": self.trading_day})
        # 启动后台常驻资金与持仓定期查询轮询
        self._start_periodic_query()

    def _start_periodic_query(self):
        def loop():
            time.sleep(0.5)
            while not self._stop_requested:
                if self.is_ready:
                    try:
                        self.query_account()
                        time.sleep(1.5)
                        self.query_positions()
                    except Exception as e:
                        logger.error(f"定期查询异常: {e}")
                time.sleep(3.5)
        t = threading.Thread(target=loop, daemon=True)
        t.start()

    def query_account(self):
        if not self.is_logged_in:
            return
        qry_req = tdapi.CThostFtdcQryTradingAccountField()
        qry_req.BrokerID = self.config.get("BrokerID", "9999")
        qry_req.InvestorID = self.config.get("UserID", "")
        self.api.ReqQryTradingAccount(qry_req, self.get_req_id())

    def query_positions(self):
        if not self.is_logged_in:
            return
        qry_req = tdapi.CThostFtdcQryInvestorPositionField()
        qry_req.BrokerID = self.config.get("BrokerID", "9999")
        qry_req.InvestorID = self.config.get("UserID", "")
        self.api.ReqQryInvestorPosition(qry_req, self.get_req_id())

    def OnRspQryTradingAccount(self, pAccount, pRspInfo, nRequestID, bIsLast):
        if pAccount:
            with self.lock:
                balance = float(pAccount.Balance)
                margin = float(pAccount.CurrMargin)
                risk_ratio = (margin / balance) if balance > 0 else 0.0
                self.account_data = {
                    "account_id": self.config.get("UserID", "278272"),
                    "pre_balance": float(pAccount.PreBalance),
                    "balance": balance,
                    "available": float(pAccount.Available),
                    "curr_margin": margin,
                    "margin": margin,
                    "close_profit": float(pAccount.CloseProfit),
                    "position_profit": float(pAccount.PositionProfit),
                    "commission": float(pAccount.Commission),
                    "risk_ratio": risk_ratio,
                    "updated_at": time.time(),
                }
            logger.info(
                f"[资金查询] 权益: {pAccount.Balance:.2f}, 可用: {pAccount.Available:.2f}, 保证金: {pAccount.CurrMargin:.2f}, 风险度: {risk_ratio*100:.2f}%"
            )
            self.emit_event("account", self.account_data)

    def OnRspQryInvestorPosition(self, pPosition, pRspInfo, nRequestID, bIsLast):
        if pPosition:
            inst = str(pPosition.InstrumentID)
            posi_dir = "buy" if pPosition.PosiDirection == tdapi.THOST_FTDC_PD_Long else "sell"
            pos_key = f"{inst}_{posi_dir}"
            pos_info = {
                "instrument_id": inst,
                "direction": posi_dir,
                "position": int(pPosition.Position),
                "yd_position": int(pPosition.YdPosition),
                "today_position": int(pPosition.TodayPosition),
                "open_cost": float(pPosition.OpenCost),
                "position_profit": float(pPosition.PositionProfit),
                "margin": float(pPosition.UseMargin),
            }
            with self.lock:
                if pPosition.Position > 0:
                    self.positions[pos_key] = pos_info
                else:
                    self.positions.pop(pos_key, None)
        if bIsLast:
            logger.info(f"[持仓查询完成] 当前持仓项数: {len(self.positions)}")
            self.emit_event("positions", list(self.positions.values()))

    # ── 报单状态回报 ──
    def OnRtnOrder(self, pOrder):
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
        dir_str = "buy" if pOrder.Direction == tdapi.THOST_FTDC_D_Buy else "sell"
        offset_map = {
            tdapi.THOST_FTDC_OF_Open: "open",
            tdapi.THOST_FTDC_OF_Close: "close",
            tdapi.THOST_FTDC_OF_CloseToday: "close_today",
            tdapi.THOST_FTDC_OF_CloseYesterday: "close_yesterday",
        }
        offset_str = offset_map.get(pOrder.CombOffsetFlag, "unknown")

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
        with self.lock:
            self.orders[ref] = order_record

        logger.info(
            f"[报单状态] 合约: {inst} | 引用: {ref} | 状态: {status_code} ({pOrder.StatusMsg}) | 成交: {pOrder.VolumeTraded}/{pOrder.VolumeTotalOriginal}"
        )
        self.emit_event("order_update", order_record)

    # ── 成交回报 ──
    def OnRtnTrade(self, pTrade):
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
        }
        with self.lock:
            self.trades.append(trade_record)

        logger.info(
            f"[🎉 撮合成交] 合约: {inst} | 方向: {offset_str} {dir_str} | 成交价: {pTrade.Price:.1f} | 成交量: {pTrade.Volume} | 时间: {pTrade.TradeTime}"
        )
        self.emit_event("trade", trade_record)
        # 撮合成交后，异步刷新一次资金与持仓
        threading.Thread(target=self._refresh_after_trade, daemon=True).start()

    def _refresh_after_trade(self):
        time.sleep(1.0)
        self.query_account()
        time.sleep(0.5)
        self.query_positions()

    def OnRspOrderInsert(self, pInputOrder, pRspInfo, nRequestID, bIsLast):
        if pRspInfo and pRspInfo.ErrorID != 0:
            msg = f"柜台拒绝报单 [{pRspInfo.ErrorID}]: {pRspInfo.ErrorMsg}"
            logger.error(f"[错误] {msg}")
            ref = str(pInputOrder.OrderRef) if pInputOrder else ""
            self.emit_event("order_error", {"order_ref": ref, "error": msg})

    def OnErrRtnOrderInsert(self, pInputOrder, pRspInfo):
        if pRspInfo and pRspInfo.ErrorID != 0:
            msg = f"交易所拒绝报单 [{pRspInfo.ErrorID}]: {pRspInfo.ErrorMsg}"
            logger.error(f"[错误] {msg}")
            ref = str(pInputOrder.OrderRef) if pInputOrder else ""
            self.emit_event("order_error", {"order_ref": ref, "error": msg})

    # ── 下单方法 ──
    def send_order(
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
            # 上期所 (SHFE) 与 能源中心 (INE) 区分平今仓与平昨仓
            if exch in ("SHFE", "INE"):
                # 查询该合约持仓：如果持有今仓，优先平今仓
                pos_key = f"{inst}_{'sell' if direction.lower() == 'buy' else 'buy'}"
                pos = self.positions.get(pos_key)
                if pos and pos.get("today_position", 0) > 0:
                    c_offset = tdapi.THOST_FTDC_OF_CloseToday
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

        res = self.api.ReqOrderInsert(req, self.get_req_id())
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
        if not self.is_ready:
            return {"success": False, "msg": "CTP未就绪"}
        req = tdapi.CThostFtdcInputOrderActionField()
        req.BrokerID = self.config.get("BrokerID", "9999")
        req.InvestorID = self.config.get("UserID", "")
        req.UserID = self.config.get("UserID", "")
        req.OrderRef = order_ref
        req.InstrumentID = instrument_id
        req.ExchangeID = exchange_id or guess_exchange_id(instrument_id)
        req.FrontID = front_id if front_id is not None else self.front_id
        req.SessionID = session_id if session_id is not None else self.session_id
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
        if path == "/health":
            return self._send_json(200, {
                "service": SERVICE_NAME,
                "healthy": g_trader_mgr.is_ready,
                "pid": os.getpid(),
                "uptime": time.time() - SERVER_STARTED_AT,
                "connected": g_trader_mgr.is_connected,
                "authenticated": g_trader_mgr.is_authenticated,
                "logged_in": g_trader_mgr.is_logged_in,
                "ready": g_trader_mgr.is_ready,
                "trading_day": g_trader_mgr.trading_day,
                "broker_id": g_trader_mgr.config.get("BrokerID"),
                "user_id": g_trader_mgr.config.get("UserID"),
            })
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
            # 主动触发资金与持仓同步
            g_trader_mgr.query_account()
            time.sleep(0.3)
            g_trader_mgr.query_positions()
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
    g_trader_mgr = CtpTraderManager(config)
    g_trader_mgr.api = trader_api
    trader_api.RegisterSpi(g_trader_mgr)
    trader_api.RegisterFront(config["TdFront"])
    trader_api.SubscribePublicTopic(tdapi.THOST_TERT_QUICK)
    trader_api.SubscribePrivateTopic(tdapi.THOST_TERT_QUICK)
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
        if trader_api:
            trader_api.Release()
        httpd.server_close()


if __name__ == "__main__":
    main()
