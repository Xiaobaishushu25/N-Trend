import os
import time
import threading
import openctp_ctp.thosttraderapi as tdapi
import openctp_ctp.thostmduserapi as mdapi

# ================= 配置区域 =================
CONFIG = {
    # 经纪商代码（统一为 9999）
    "BrokerID": "9999",
    # 你的 6 位数字资金账号
    "UserID": "278272",
    # 你的密码
    "Password": "qwertyuiop123*",
    # 穿透式认证参数
    "AppID": "simnow_client_test",
    "AuthCode": "0000000000000000",
    # 交易前置地址
    "TdFront": "tcp://182.254.243.31:30001",
    # 行情前置地址
    "MdFront": "tcp://182.254.243.31:30011",
}

# 沪铅夜盘交易至凌晨 01:00，当前活跃主力一般为 pb2611 或 pb2612
TEST_INSTRUMENTS = ["pb2611", "pb2612"]
TEST_INSTRUMENT = "pb2611"

# 全局共享行情变量
g_last_price = 0.0
g_trade_spi = None


# ================= 1. 交易客户端 (TraderSpi) =================
class MyTraderSpi(tdapi.CThostFtdcTraderSpi):
    def __init__(self, api):
        super().__init__()
        self.api = api
        self.req_id = 0
        self.order_ref = 1000
        self.has_traded = False

    def get_req_id(self):
        self.req_id += 1
        return self.req_id

    def get_order_ref(self):
        self.order_ref += 1
        return str(self.order_ref)

    # 1. 前置连接成功
    def OnFrontConnected(self):
        print("[交易] 前置机连接成功，开始穿透式认证...", flush=True)
        req = tdapi.CThostFtdcReqAuthenticateField()
        req.BrokerID = CONFIG["BrokerID"]
        req.UserID = CONFIG["UserID"]
        req.AppID = CONFIG["AppID"]
        req.AuthCode = CONFIG["AuthCode"]
        self.api.ReqAuthenticate(req, self.get_req_id())

    def OnFrontDisconnected(self, nReason):
        print(f"[交易] 前置机连接断开 (代码: {nReason})", flush=True)

    # 2. 穿透式认证回报
    def OnRspAuthenticate(self, pRspAuthenticateField, pRspInfo, nRequestID, bIsLast):
        if pRspInfo and pRspInfo.ErrorID != 0:
            print(f"[交易] 认证失败: {pRspInfo.ErrorMsg}", flush=True)
            return
        print("[交易] 穿透式认证成功，开始登录...", flush=True)
        login_req = tdapi.CThostFtdcReqUserLoginField()
        login_req.BrokerID = CONFIG["BrokerID"]
        login_req.UserID = CONFIG["UserID"]
        login_req.Password = CONFIG["Password"]
        self.api.ReqUserLogin(login_req, self.get_req_id())

    # 3. 登录回报
    def OnRspUserLogin(self, pRspUserLogin, pRspInfo, nRequestID, bIsLast):
        if pRspInfo and pRspInfo.ErrorID != 0:
            print(f"[交易] 登录失败: {pRspInfo.ErrorMsg}", flush=True)
            return
        print(f"[交易] 登录成功！交易日: {pRspUserLogin.TradingDay}，前置编号: {pRspUserLogin.FrontID}", flush=True)

        # CTP下单前必须先确认结算结果
        settle_req = tdapi.CThostFtdcSettlementInfoConfirmField()
        settle_req.BrokerID = CONFIG["BrokerID"]
        settle_req.InvestorID = CONFIG["UserID"]
        self.api.ReqSettlementInfoConfirm(settle_req, self.get_req_id())

    # 4. 结算单确认回报
    def OnRspSettlementInfoConfirm(self, pConfirm, pRspInfo, nRequestID, bIsLast):
        print("[交易] 投资者结算结果已确认！", flush=True)
        time.sleep(1)
        self.query_account()

    # 查询资金
    def query_account(self):
        print("[交易] 正在查询账户可用资金...", flush=True)
        qry_req = tdapi.CThostFtdcQryTradingAccountField()
        qry_req.BrokerID = CONFIG["BrokerID"]
        qry_req.InvestorID = CONFIG["UserID"]
        self.api.ReqQryTradingAccount(qry_req, self.get_req_id())

    # 5. 资金账户查询回报
    def OnRspQryTradingAccount(self, pAccount, pRspInfo, nRequestID, bIsLast):
        if pAccount:
            print("=" * 45, flush=True)
            print("【账户资金信息】", flush=True)
            print(f"动态权益 (Balance) : {pAccount.Balance:.2f}", flush=True)
            print(f"可用资金 (Available): {pAccount.Available:.2f}", flush=True)
            print(f"当前保证金 (CurrMargin): {pAccount.CurrMargin:.2f}", flush=True)
            print("=" * 45, flush=True)

            # 资金查询完毕后，启动下单测试线程
            threading.Thread(target=self.start_trade_experiment, daemon=True).start()

    # ================= 下单与回报逻辑 =================

    # 核心下单函数
    def send_order(self, instrument_id, direction, offset_flag, price, volume, exchange_id="SHFE"):
        req = tdapi.CThostFtdcInputOrderField()
        req.BrokerID = CONFIG["BrokerID"]
        req.InvestorID = CONFIG["UserID"]
        req.UserID = CONFIG["UserID"]
        req.InstrumentID = instrument_id
        req.ExchangeID = exchange_id  # 显式填入交易所代码 (如 SHFE)
        req.OrderRef = self.get_order_ref()

        # 报单类型：限价单
        req.OrderPriceType = tdapi.THOST_FTDC_OPT_LimitPrice
        req.Direction = direction
        req.CombOffsetFlag = offset_flag  # 开仓或平仓
        req.CombHedgeFlag = tdapi.THOST_FTDC_HF_Speculation  # 投机

        req.LimitPrice = float(price)
        req.VolumeTotalOriginal = int(volume)

        # 有效期：当日有效 (GFD)
        req.TimeCondition = tdapi.THOST_FTDC_TC_GFD
        req.VolumeCondition = tdapi.THOST_FTDC_VC_AV
        req.MinVolume = 1
        req.ContingentCondition = tdapi.THOST_FTDC_CC_Immediately
        req.StopPrice = 0.0
        req.ForceCloseReason = tdapi.THOST_FTDC_FCC_NotForceClose  # 核心必填：非强平
        req.IsAutoSuspend = 0
        req.UserForceClose = 0

        res = self.api.ReqOrderInsert(req, self.get_req_id())
        dir_str = "买" if direction == tdapi.THOST_FTDC_D_Buy else "卖"
        offset_str = "开仓" if offset_flag == tdapi.THOST_FTDC_OF_Open else "平仓"
        print(f"\n[报单请求] 发送指令 -> {offset_str} {dir_str} {instrument_id} {volume}手 @ {price:.1f}，返回值: {res}", flush=True)

    # 报单状态回报（推送订单排队、部成、全成或已撤单状态）
    def OnRtnOrder(self, pOrder):
        if pOrder:
            status_map = {
                tdapi.THOST_FTDC_OST_AllTraded: "全部成交",
                tdapi.THOST_FTDC_OST_PartTradedQueueing: "部分成交还在排队",
                tdapi.THOST_FTDC_OST_NoTradeQueueing: "未成交正在排队",
                tdapi.THOST_FTDC_OST_Canceled: "已撤单",
            }
            status_desc = status_map.get(pOrder.OrderStatus, f"状态码: {pOrder.OrderStatus}")
            print(f"[报单状态] 合约: {pOrder.InstrumentID} | 引用: {pOrder.OrderRef} | 状态: {status_desc} | 详情: {pOrder.StatusMsg}", flush=True)

    # 成交回报（【最关键】只有撮合成交了才会收到这个回调）
    def OnRtnTrade(self, pTrade):
        if pTrade:
            dir_str = "买入" if pTrade.Direction == tdapi.THOST_FTDC_D_Buy else "卖出"
            offset_str = "开仓" if pTrade.OffsetFlag == tdapi.THOST_FTDC_OF_Open else "平仓"

            # 【重要避坑】必须立即将 C++ 指针字段拷贝为 Python 变量，否则子线程中指针会被释放成空值
            trade_inst = str(pTrade.InstrumentID)
            trade_price = float(pTrade.Price)
            trade_exchange = str(pTrade.ExchangeID) if pTrade.ExchangeID else "SHFE"

            print("*" * 50, flush=True)
            print(f"【🎉 撮合成交回报】", flush=True)
            print(f"  合约: {trade_inst}")
            print(f"  方向: {offset_str} {dir_str}")
            print(f"  成交价: {trade_price:.1f} 元/吨")
            print(f"  成交量: {pTrade.Volume} 手")
            print(f"  成交时间: {pTrade.TradeTime}")
            print(f"  成交编号: {pTrade.TradeID}")
            print("*" * 50, flush=True)

            # 如果刚刚买开仓成交，等待 3 秒后自动发平仓单
            if pTrade.OffsetFlag == tdapi.THOST_FTDC_OF_Open and not self.has_traded:
                self.has_traded = True
                def close_later(inst, exch, fill_price):
                    print("\n>>> 等待 3 秒观察仓位，即将执行自动平仓...", flush=True)
                    time.sleep(3)
                    # 卖平仓（取最新价 - 5元对价吃单，保证立刻成交）
                    close_price = (g_last_price - 5.0) if g_last_price > 0 else fill_price
                    print(f">>> 正在以市价对价 {close_price:.1f} 发送【卖平仓】指令 (合约: {inst})...", flush=True)
                    self.send_order(
                        instrument_id=inst,
                        direction=tdapi.THOST_FTDC_D_Sell,
                        offset_flag=tdapi.THOST_FTDC_OF_CloseToday,  # 平今仓
                        price=close_price,
                        volume=1,
                        exchange_id=exch
                    )
                threading.Thread(target=close_later, args=(trade_inst, trade_exchange, trade_price), daemon=True).start()
            elif pTrade.OffsetFlag != tdapi.THOST_FTDC_OF_Open:
                print("\n" + "=" * 50, flush=True)
                print("【✅ 恭喜！买开仓 + 卖平仓 完整交易闭环测试大获全胜！】", flush=True)
                print("=" * 50 + "\n", flush=True)

    # 报单录入错误回报（柜台拒绝）
    def OnRspOrderInsert(self, pInputOrder, pRspInfo, nRequestID, bIsLast):
        if pRspInfo and pRspInfo.ErrorID != 0:
            print(f"[错误] 柜台拒绝报单: {pRspInfo.ErrorMsg}", flush=True)

    # 交易所报单错误回报（交易所拒绝）
    def OnErrRtnOrderInsert(self, pInputOrder, pRspInfo):
        if pRspInfo and pRspInfo.ErrorID != 0:
            print(f"[错误] 交易所拒绝报单: {pRspInfo.ErrorMsg}", flush=True)

    # 下单实验控制流程
    def start_trade_experiment(self):
        global g_last_price
        print("\n>>> 等待行情数据到达以确定开仓价格...", flush=True)
        wait_count = 0
        while g_last_price <= 0 and wait_count < 10:
            time.sleep(1)
            wait_count += 1

        # 若已收到行情，买入稍微上浮一个最小变动价位（5元），保证模拟盘对手价立刻撮合成交
        trade_price = (g_last_price + 5.0) if g_last_price > 0 else 16800.0

        print(f"\n>>> 准备执行【买开仓 1手 {TEST_INSTRUMENT}】，下单参考价: {trade_price:.1f}")
        time.sleep(1)
        # 买开仓 1手
        self.send_order(
            instrument_id=TEST_INSTRUMENT,
            direction=tdapi.THOST_FTDC_D_Buy,
            offset_flag=tdapi.THOST_FTDC_OF_Open,
            price=trade_price,
            volume=1
        )


# ================= 2. 行情客户端 (MdSpi) =================
class MyMdSpi(mdapi.CThostFtdcMdSpi):
    def __init__(self, api):
        super().__init__()
        self.api = api

    def OnFrontConnected(self):
        print("[行情] 前置连接成功，登录行情账号...", flush=True)
        req = mdapi.CThostFtdcReqUserLoginField()
        req.BrokerID = CONFIG["BrokerID"]
        req.UserID = CONFIG["UserID"]
        req.Password = CONFIG["Password"]
        self.api.ReqUserLogin(req, 1)

    def OnFrontDisconnected(self, nReason):
        print(f"[行情] 前置机连接断开 (代码: {nReason})", flush=True)

    def OnRspUserLogin(self, pRspUserLogin, pRspInfo, nRequestID, bIsLast):
        print(f"[行情] 登录成功，订阅沪铅合约行情: {TEST_INSTRUMENTS}...", flush=True)
        sub_list = [inst.encode("utf-8") for inst in TEST_INSTRUMENTS]
        self.api.SubscribeMarketData(sub_list, len(sub_list))

    # 行情深度数据推送 (Tick 数据)
    def OnRtnDepthMarketData(self, pData):
        global g_last_price, TEST_INSTRUMENT
        if pData and pData.LastPrice > 0:
            g_last_price = pData.LastPrice
            TEST_INSTRUMENT = pData.InstrumentID
            print(
                f"[Tick 推送] {pData.InstrumentID} | "
                f"最新价: {pData.LastPrice:.1f} | "
                f"买一: {pData.BidPrice1:.1f}({pData.BidVolume1}手) | "
                f"卖一: {pData.AskPrice1:.1f}({pData.AskVolume1}手)",
                flush=True
            )


# ================= 主执行入口 =================
if __name__ == "__main__":
    print("正在启动 CTP 客户端...", flush=True)

    os.makedirs("flow_td", exist_ok=True)
    os.makedirs("flow_md", exist_ok=True)

    # 1. 初始化交易接口
    td_api = tdapi.CThostFtdcTraderApi.CreateFtdcTraderApi("flow_td/")
    td_spi = MyTraderSpi(td_api)
    g_trade_spi = td_spi
    td_api.RegisterSpi(td_spi)
    td_api.RegisterFront(CONFIG["TdFront"])
    td_api.SubscribePublicTopic(tdapi.THOST_TERT_QUICK)
    td_api.SubscribePrivateTopic(tdapi.THOST_TERT_QUICK)
    td_api.Init()

    # 2. 初始化行情接口
    md_api = mdapi.CThostFtdcMdApi.CreateFtdcMdApi("flow_md/")
    md_spi = MyMdSpi(md_api)
    md_api.RegisterSpi(md_spi)
    md_api.RegisterFront(CONFIG["MdFront"])
    md_api.Init()

    # 保持主进程运行
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("用户退出。")
