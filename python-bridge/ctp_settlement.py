"""Read-only CTP settlement session and statement decoder."""

import datetime
import os
import re
import tempfile
import threading
from decimal import Decimal

from openctp_ctp import thosttraderapi as td


VENUES = {
    "上期所": "SHFE", "上海期货交易所": "SHFE",
    "大商所": "DCE", "大连商品交易所": "DCE",
    "郑商所": "CZCE", "郑州商品交易所": "CZCE",
    "中金所": "CFFEX", "中国金融期货交易所": "CFFEX",
    "广期所": "GFEX", "广州期货交易所": "GFEX",
    "能源中心": "INE", "上期能源": "INE", "上海国际能源交易中心": "INE",
}
OFFSETS = {"开": "open", "开仓": "open", "平": "close", "平仓": "close",
           "平今": "close_today", "平今仓": "close_today",
           "平昨": "close_yesterday", "平昨仓": "close_yesterday"}
SIDES = {"买": "buy", "卖": "sell"}
DAY = re.compile(r"\d{4}[-/]?\d{2}[-/]?\d{2}")
TOTAL = re.compile(r"(?<![交割行权])手续费(?:\([^)]+\))?(?:Commission)?[:：]([+-]?[\d,.]+)", re.I)


def day_key(value):
    if not DAY.fullmatch(value):
        raise ValueError("invalid settlement date")
    day = value.replace("-", "").replace("/", "")
    datetime.datetime.strptime(day, "%Y%m%d")
    return day


def cents(value):
    amount = Decimal(value.replace(",", "")) * 100
    if not amount.is_finite() or amount != amount.to_integral_value():
        raise ValueError("invalid commission precision")
    return int(amount)


def settlement_price(value):
    price = Decimal(value.replace(",", ""))
    if not price.is_finite():
        raise ValueError("invalid settlement price")
    return str(price)


def parse_statement(content, trading_day):
    """Keep only accounting columns. A partial or unrecognized table is unsafe to total."""
    fills, closes, totals = [], [], []
    heading = None
    found_fills = False
    incomplete = False
    has_header = re.search(r"(?:结算日期|结算日|交易日|Date)\s*[:：]\s*(\d{4}[-/]?\d{2}[-/]?\d{2})", content, re.I)
    if has_header and day_key(has_header[1]) != trading_day:
        raise ValueError("statement date mismatch")
    for raw in content.splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            if re.search(r"[\u4e00-\u9fff]", line):
                heading = None
                totals.extend(cents(value) for value in TOTAL.findall(re.sub(r"\s+", "", line)))
            continue
        cols = [col.strip() for col in line.strip("|").split("|")]
        if {"合约", "成交价", "手续费"}.issubset(cols):
            heading = ("fill", cols)
            found_fills = True
            continue
        if {"合约", "成交价", "开仓价", "开仓日期"}.issubset(cols):
            heading = ("close", cols)
            continue
        if heading is None:
            continue
        kind, fields = heading
        if not cols or not DAY.fullmatch(cols[0]):
            continue
        if len(cols) != len(fields):
            incomplete = True
            continue
        row = dict(zip(fields, cols))
        try:
            qty = int(row["手数"])
            if qty <= 0:
                raise ValueError("invalid settlement quantity")
            exchange = row["交易所"]
            item = {"exchange": VENUES.get(exchange, exchange.upper()),
                    "instrument": row["合约"], "side": SIDES[row["买/卖"]],
                    "price": settlement_price(row["成交价"]), "quantity": qty}
            if kind == "fill":
                trade_id = row.get("成交序号") or row.get("成交编号") or row.get("成交号")
                if not trade_id:
                    raise ValueError("missing settlement trade id")
                item.update(trade_id=trade_id, offset=OFFSETS[row["开平"]], fee_cents=cents(row["手续费"]))
                fills.append(item)
            else:
                item.update(open_day=day_key(row["开仓日期"]), open_price=settlement_price(row["开仓价"]))
                closes.append(item)
        except (KeyError, ValueError, ArithmeticError):
            incomplete = True
    total = totals[0] if totals and len(set(totals)) == 1 else None
    identities = {(row["exchange"], row["instrument"].casefold(), row["side"], row["trade_id"]) for row in fills}
    verified = (found_fills and not incomplete and total is not None and len(identities) == len(fills)
                and sum(row["fee_cents"] for row in fills) == total)
    return {"trading_day": trading_day, "currency": "CNY", "total_fee_cents": total,
            "status": "verified" if verified else "missing_detail",
            "fills": fills if verified else [], "closes": closes if verified else []}


class SettlementSession(td.CThostFtdcTraderSpi):
    def __init__(self, api, config):
        super().__init__()
        self.api = api
        self.config = config
        self.done = threading.Event()
        self.ready = threading.Event()
        self.logged_in = False
        self.error = None
        self.chunks = []
        self.identity = None

    def OnFrontConnected(self):
        if self.ready.is_set():
            return
        request = td.CThostFtdcReqAuthenticateField()
        request.BrokerID, request.UserID, request.AppID, request.AuthCode = (
            self.config[key] for key in ("BrokerID", "UserID", "AppID", "AuthCode"))
        if self.api.ReqAuthenticate(request, 1) != 0:
            self.ready.set()

    def OnRspAuthenticate(self, response, info, request_id, last):
        if info and info.ErrorID:
            self.ready.set()
        elif last and not self.ready.is_set():
            request = td.CThostFtdcReqUserLoginField()
            request.BrokerID, request.UserID, request.Password = (
                self.config[key] for key in ("BrokerID", "UserID", "Password"))
            if self.api.ReqUserLogin(request, 2) != 0:
                self.ready.set()

    def OnRspUserLogin(self, response, info, request_id, last):
        if last or (info and info.ErrorID):
            self.logged_in = bool(response and not (info and info.ErrorID))
            if self.logged_in:
                self.identity = (str(response.BrokerID).strip(), str(response.UserID).strip())
            self.ready.set()

    def OnFrontDisconnected(self, reason):
        self.logged_in = False
        self.error = reason
        self.ready.set()
        self.done.set()

    def OnRspError(self, info, request_id, last):
        if request_id in (1, 2):
            self.ready.set()
        elif request_id == 3 and info and info.ErrorID:
            self.error = info.ErrorID
            if last:
                self.done.set()

    def OnRspQrySettlementInfo(self, response, info, request_id, last):
        if request_id != 3:
            return
        if info and info.ErrorID:
            self.error = info.ErrorID
        if response:
            try:
                content = response.Content
                if isinstance(content, str):
                    content = content.encode("utf-8", "surrogateescape")
                self.chunks.append((response.SequenceNo, str(response.TradingDay).strip(), content))
            except (UnicodeError, TypeError, ValueError):
                self.error = "invalid_encoding"
        if last:
            self.done.set()

    def query(self, trading_day):
        if not self.ready.wait(10) or not self.logged_in:
            return {"status": "retry"}
        identity = (self.config["BrokerID"], self.config["UserID"])
        if self.identity != identity:
            return {"status": "retry"}
        request = td.CThostFtdcQrySettlementInfoField()
        request.BrokerID, request.InvestorID = identity
        request.TradingDay = trading_day
        if self.api.ReqQrySettlementInfo(request, 3) != 0 or not self.done.wait(12) or self.error is not None:
            return {"status": "retry"}
        if not self.chunks:
            return {"status": "empty"}
        if any(day and day != trading_day for _, day, _ in self.chunks):
            return {"status": "retry"}
        data = b"".join(content for _, _, content in sorted(self.chunks, key=lambda chunk: chunk[0]))
        for encoding in ("utf-8", "gb18030"):
            try:
                return parse_statement(data.decode(encoding), trading_day)
            except UnicodeError:
                continue
            except (ValueError, ArithmeticError):
                break
        return {"status": "retry"}


def query_settlement(config, trading_day):
    with tempfile.TemporaryDirectory(prefix="ntrend-ctp-settlement-") as flow:
        api = td.CThostFtdcTraderApi.CreateFtdcTraderApi(flow + os.sep)
        session = SettlementSession(api, config)
        api.RegisterSpi(session)
        api.RegisterFront(config["TdFront"])
        api.Init()
        try:
            statement = session.query(trading_day)
            return dict(statement, broker_id=config["BrokerID"], investor_id=config["UserID"],
                        environment="simnow" if config["BrokerID"] == "9999" else "real")
        finally:
            api.Release()
