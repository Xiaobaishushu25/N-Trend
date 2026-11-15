"""Offline regression tests; no CTP library, credentials, or counter connection."""
import importlib.util
import json
import pathlib
import socketserver
import subprocess
import sys
import threading
import tempfile
import time
import types
import unittest
import urllib.request
from unittest.mock import patch


def load_bridge():
    api = types.ModuleType("openctp_ctp.thosttraderapi")
    api.CThostFtdcTraderSpi = object
    api.CThostFtdcQryTradingAccountField = types.SimpleNamespace
    api.CThostFtdcQryInvestorPositionField = types.SimpleNamespace
    api.CThostFtdcQryInstrumentField = types.SimpleNamespace
    api.CThostFtdcInputOrderActionField = types.SimpleNamespace
    api.CThostFtdcInputOrderField = types.SimpleNamespace
    for name in ["D_Buy", "D_Sell", "OF_Open", "OF_CloseToday", "OF_Close", "OPT_LimitPrice",
                 "HF_Speculation", "TC_GFD", "VC_AV", "CC_Immediately", "FCC_NotForceClose"]:
        setattr(api, "THOST_FTDC_" + name, name)
    api.THOST_FTDC_AF_Delete = "0"
    api.THOST_FTDC_PD_Long = "2"

    md_api = types.ModuleType("openctp_ctp.thostmduserapi")
    md_api.CThostFtdcMdSpi = object
    md_api.CThostFtdcReqUserLoginField = types.SimpleNamespace

    package = types.ModuleType("openctp_ctp")
    package.thosttraderapi = api
    package.thostmduserapi = md_api
    spec = importlib.util.spec_from_file_location("ctp_bridge_test", pathlib.Path(__file__).with_name("ctp_trader.py"))
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {
        "openctp_ctp": package,
        "openctp_ctp.thosttraderapi": api,
        "openctp_ctp.thostmduserapi": md_api,
    }):
        spec.loader.exec_module(module)
    return module


bridge = load_bridge()


class FakeApi:
    def __init__(self):
        self.calls = []
        self.return_code = 0
        self.changed = threading.Condition()

    def send(self, kind, req_id):
        with self.changed:
            self.calls.append((kind, req_id, time.monotonic()))
            self.changed.notify_all()
        return self.return_code

    def ReqQryTradingAccount(self, req, req_id):
        return self.send("account", req_id)

    def ReqQryInvestorPosition(self, req, req_id):
        return self.send("positions", req_id)

    def ReqQryInstrument(self, req, req_id):
        return self.send("instrument", req_id)

    def ReqOrderAction(self, req, req_id):
        return self.send("cancel", req_id)

    def wait_for(self, count):
        with self.changed:
            return self.changed.wait_for(lambda: len(self.calls) >= count, timeout=2)


class FakeMdApi:
    def __init__(self):
        self.calls = []
        self.return_code = 0
        self.subscribed = []
        self.unsubscribed = []

    def ReqUserLogin(self, req, req_id):
        self.calls.append(("login", req_id))
        return self.return_code

    def SubscribeMarketData(self, symbols, count):
        self.subscribed.extend([s.decode("utf-8") if isinstance(s, bytes) else str(s) for s in symbols])
        return self.return_code

    def UnSubscribeMarketData(self, symbols, count):
        self.unsubscribed.extend([s.decode("utf-8") if isinstance(s, bytes) else str(s) for s in symbols])
        return self.return_code


def position(today=1, volume=1, cost=100):
    return types.SimpleNamespace(InstrumentID="pb2611", PosiDirection="2", Position=volume,
                                 TodayPosition=today, OpenCost=cost, PositionCost=cost,
                                 PositionProfit=5, UseMargin=10)


def account():
    return types.SimpleNamespace(Balance=1000, CurrMargin=100, PreBalance=900, Available=850,
                                 CloseProfit=0, PositionProfit=100, Commission=0, CurrencyID="CNY")


class QueryTests(unittest.TestCase):
    def test_login_failure_is_published_with_code_and_without_credentials(self):
        self.m.config.update(Password="fixture-password", AuthCode="fixture-auth")
        info=types.SimpleNamespace(ErrorID=3,ErrorMsg="reject fixture-password fixture-auth")
        self.m.OnRspUserLogin(None,info,1,True)
        health=self.m.health_snapshot()
        self.assertFalse(health["logged_in"])
        self.assertEqual(health["login_error_code"],3)
        self.assertNotIn("fixture-password",json.dumps(health))
        self.assertNotIn("fixture-auth",json.dumps(self.m.update_events))
        self.assertEqual(self.m.update_events[-1]["type"],"login_error")

    def setUp(self):
        self.m = bridge.CtpTraderManager({})
        self.m.api = FakeApi()
        self.m.is_logged_in = True
        self.m._settlement_confirmed = True
        # Only the fake counter uses shortened timing for deterministic tests.
        self.m._query_interval = 0.03
        self.m._reconcile_interval = 10000

    def tearDown(self):
        self.m.stop()

    def begin(self, kind, req_id=1):
        self.m._inflight = dict(id=req_id, kind=kind, started=time.monotonic(), rows={}, error=None)

    def test_parent_probe_does_not_kill_live_parent_and_detects_exited_process(self):
        self.assertTrue(bridge._parent_is_alive(bridge.os.getpid()))
        process = subprocess.Popen([sys.executable, "-c", "pass"])
        process.wait(timeout=5)
        self.assertFalse(bridge._parent_is_alive(process.pid))

    def test_bridge_exits_when_owning_backend_disappears(self):
        with patch.object(bridge, "_parent_is_alive", side_effect=[True, False]), \
             patch.object(bridge.time, "sleep"), patch.object(bridge.os, "_exit") as terminate:
            bridge._watch_parent(123)
        terminate.assert_called_once_with(0)

    def test_order_rejection_refreshes_funds_before_new_entry_decisions(self):
        rejected = types.SimpleNamespace(OrderRef="funds-test", InstrumentID="pb2611")
        self.m._record_order_error(rejected, "柜台拒绝报单 [31]: CTP:资金不足")
        self.assertEqual(self.m.orders["funds-test"]["status"], "rejected")
        self.assertIn("account", self.m._pending_queries)
        self.assertFalse(self.m.is_ready)

    def test_forced_bursts_coalesce_and_share_one_inflight_slot(self):
        for _ in range(100):
            self.m.query_account(force=True)
            self.m.query_positions(force=True)
        self.m._start_periodic_query()
        self.assertTrue(self.m.api.wait_for(1))
        time.sleep(0.07)
        self.assertEqual(len(self.m.api.calls), 1)
        req_id = self.m.api.calls[0][1]
        self.m.OnRspQryTradingAccount(account(), None, req_id, False)
        time.sleep(0.04)
        self.assertEqual(len(self.m.api.calls), 1)
        self.m.OnRspQryTradingAccount(None, None, req_id, True)
        self.assertTrue(self.m.api.wait_for(2))
        self.assertEqual([c[0] for c in self.m.api.calls], ["account", "positions"])
        self.assertGreaterEqual(self.m.api.calls[1][2] - self.m.api.calls[0][2], 0.03)

    def test_query_sdk_call_allows_a_callback_to_acquire_state_lock(self):
        callback_finished = threading.Event()
        blocked = []
        callbacks = []

        def send(req, req_id):
            def callback():
                self.m.OnRspQryTradingAccount(account(), None, req_id, True)
                callback_finished.set()
            worker = threading.Thread(target=callback, daemon=True)
            callbacks.append(worker)
            worker.start()
            blocked.append(not callback_finished.wait(0.5))
            return 0

        self.m.api.ReqQryTradingAccount = send
        self.m.query_account()
        self.m._start_periodic_query()
        self.assertTrue(callback_finished.wait(2))
        self.m.stop()
        for worker in callbacks:
            worker.join(timeout=1)
        self.assertEqual(blocked, [False])
        self.assertIsNone(self.m._inflight)

    def test_concurrent_order_replay_is_idempotent_without_blocking_spi_callback(self):
        self.m.is_ready = True
        callback_finished = threading.Event()
        blocked = []
        calls = []
        results = []
        start = threading.Barrier(3)
        with tempfile.TemporaryDirectory() as directory:
            self.m._journal_path = str(pathlib.Path(directory) / "journal.json")

            def send(req, req_id):
                calls.append(req.OrderRef)
                saved = json.loads(pathlib.Path(self.m._journal_path).read_text(encoding="utf-8"))
                self.assertIn("1001", saved)
                worker = threading.Thread(target=lambda: (self.m.health_snapshot(), callback_finished.set()), daemon=True)
                worker.start()
                blocked.append(not callback_finished.wait(0.5))
                return 0

            def submit():
                start.wait()
                results.append(self.m.send_order("pb2611", "buy", "open", 1000, 1, custom_ref="1001"))

            self.m.api.ReqOrderInsert = send
            workers = [threading.Thread(target=submit, daemon=True) for _ in range(2)]
            for worker in workers:
                worker.start()
            start.wait()
            for worker in workers:
                worker.join(timeout=2)
            self.assertFalse(any(worker.is_alive() for worker in workers))
            self.assertEqual(calls, ["1001"])
            self.assertEqual(blocked, [False])
            self.assertEqual(len(results), 2)
            self.assertTrue(all(result["success"] for result in results))

    def test_position_snapshot_is_atomic_aggregated_and_can_be_empty(self):
        self.m.positions = {"old": {"position": 1}}
        self.begin("positions")
        self.m.OnRspQryInvestorPosition(position(), None, 1, False)
        self.assertIn("old", self.m.positions)
        self.m.OnRspQryInvestorPosition(position(today=0, volume=2, cost=200), None, 1, True)
        row = self.m.positions["pb2611_buy"]
        self.assertEqual((row["position"], row["today_position"], row["yd_position"]), (3, 1, 2))
        self.assertEqual(row["open_cost"], 300)
        self.begin("positions", 2)
        self.m.OnRspQryInvestorPosition(None, None, 2, True)
        self.assertEqual(self.m.positions, {})

    def test_error_discards_partial_snapshot_and_schedules_retry(self):
        self.m.positions = {"old": {"position": 1}}
        self.begin("positions")
        self.m.OnRspQryInvestorPosition(position(), None, 1, False)
        error = types.SimpleNamespace(ErrorID=1, ErrorMsg="test error")
        self.m.OnRspQryInvestorPosition(None, error, 1, True)
        self.assertIn("old", self.m.positions)
        self.assertIn("positions", self.m._pending_queries)
        self.assertFalse(self.m.is_ready)

    def test_synchronous_failure_is_not_silently_ignored(self):
        self.m.api.return_code = -3
        self.m.query_account()
        self.m._start_periodic_query()
        self.assertTrue(self.m.api.wait_for(1))
        with self.m._condition:
            self.m._condition.wait_for(lambda: self.m._inflight is None, timeout=1)
            self.assertIn("account", self.m._pending_queries)
            self.assertIn("-3", self.m.sync_error)
            self.m.api.return_code = 0
            self.m._pending_queries["account"] = time.monotonic()
            self.m._condition.notify_all()
        self.assertTrue(self.m.api.wait_for(2))

    def test_stale_response_after_disconnect_is_ignored(self):
        self.begin("positions")
        self.m.OnFrontDisconnected(4097)
        self.m.OnRspQryInvestorPosition(position(), None, 1, True)
        self.assertEqual(self.m.positions, {})
        self.assertFalse(self.m.is_ready)

    def test_reconnect_does_not_spawn_extra_workers(self):
        self.m._start_periodic_query()
        worker = self.m._query_thread
        self.m.OnFrontDisconnected(4097)
        self.m.OnRspSettlementInfoConfirm(None, None, 10, True)
        self.assertIs(worker, self.m._query_thread)

    def test_timeout_keeps_query_slot_occupied_until_final_response(self):
        self.m._query_timeout = 0.02
        self.m.request_sync()
        self.m._start_periodic_query()
        self.assertTrue(self.m.api.wait_for(1))
        with self.m._condition:
            self.m._inflight["started"] -= 10
            self.m._condition.notify_all()
            self.m._condition.wait_for(lambda: bool(self.m.sync_error), timeout=1)
        self.assertIsNotNone(self.m._inflight)
        self.assertEqual(len(self.m.api.calls), 1)
        self.assertFalse(self.m.is_ready)

    def test_ready_requires_all_initial_snapshots(self):
        self.begin("account")
        self.m.OnRspQryTradingAccount(account(), None, 1, True)
        self.assertFalse(self.m.is_ready)
        self.begin("positions", 2)
        self.m.OnRspQryInvestorPosition(None, None, 2, True)
        self.assertFalse(self.m.is_ready)
        self.begin("orders", 3)
        self.m.OnRspQryOrder(None, None, 3, True)
        self.begin("trades", 4)
        self.m.OnRspQryTrade(None, None, 4, True)
        self.assertTrue(self.m.is_ready)

    def test_refresh_during_inflight_is_not_lost(self):
        self.begin("account")
        self.m.request_sync()
        self.m.OnRspQryTradingAccount(account(), None, 1, True)
        self.assertEqual(set(self.m._pending_queries), {"account", "positions", "orders", "trades"})

    def test_snapshot_failure_does_not_block_order_cancellation(self):
        self.m.is_ready = False
        self.m.sync_error = "query timed out"
        result = self.m.cancel_order("1001", "pb2611")
        self.assertTrue(result["success"])
        self.assertEqual(self.m.api.calls[0][0], "cancel")

    def test_http_write_rejects_mismatched_account_identity_before_sending(self):
        bridge.g_trader_mgr = self.m
        self.m.config.update({"BrokerID": "broker-a", "UserID": "user-a"})
        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), bridge.CtpHttpHandler)
        server.daemon_threads = True
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}"
            request = urllib.request.Request(url + "/order/insert", data=b"{}", headers={
                "X-Ntrend-Broker": "broker-a", "X-Ntrend-User": "wrong-user",
                "Content-Type": "application/json"}, method="POST")
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=2)
            self.assertEqual(caught.exception.code, 409)
            caught.exception.close()
            self.assertEqual(self.m.api.calls, [])
        finally:
            server.shutdown()
            server.server_close()

    def test_sse_initial_snapshot_and_event_update_without_counter_queries(self):
        bridge.g_trader_mgr = self.m
        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), bridge.CtpHttpHandler)
        server.daemon_threads = True
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}"
            with urllib.request.urlopen(url + "/events", timeout=2) as response:
                initial = json.loads(response.readline().decode()[6:])
                response.readline()
                self.m.emit_event("test", {})
                updated = json.loads(response.readline().decode()[6:])
                self.assertGreater(updated["revision"], initial["revision"])
            with urllib.request.urlopen(url + "/snapshot", timeout=2) as response:
                self.assertIn("health", json.load(response))
            self.assertEqual(self.m.api.calls, [])
        finally:
            self.m.stop()
            server.shutdown()
            server.server_close()


class MarketDataTests(unittest.TestCase):
    def setUp(self):
        self.md = bridge.CtpMdManager({"BrokerID": "7090", "UserID": "85530059"})
        self.md.api = FakeMdApi()
        self.md.instruments_meta = {
            "rb2501": {
                "instrument_id": "rb2501",
                "exchange_id": "SHFE",
                "price_tick": 1.0,
                "volume_multiple": 10,
            }
        }

    def tearDown(self):
        self.md.stop()

    def test_timestamp_parsing_beijing_time(self):
        ts, dt_str = bridge.parse_ctp_timestamp("20261113", "20261116", "21:15:30", 500)
        self.assertIn("+08:00", dt_str)
        self.assertIn("2026-11-13T21:15:30.500", dt_str)
        self.assertGreater(ts, 1700000000)

    def test_depth_data_processing_and_sanitization(self):
        tick = types.SimpleNamespace(
            InstrumentID="rb2501",
            ExchangeID="SHFE",
            LastPrice=3500.0,
            Volume=12000,
            Turnover=420000000.0,
            OpenInterest=50000.0,
            BidPrice1=3499.0,
            BidVolume1=50,
            AskPrice1=3501.0,
            AskVolume1=40,
            UpperLimitPrice=3800.0,
            LowerLimitPrice=3200.0,
            ActionDay="20261113",
            TradingDay="20261113",
            UpdateTime="09:30:00",
            UpdateMillisec=500,
        )
        self.md.OnRtnDepthMarketData(tick)
        self.assertEqual(len(self.md.ring_buffer), 1)
        evt = self.md.ring_buffer[0]
        self.assertEqual(evt["type"], "tick")
        self.assertEqual(evt["instrument_id"], "rb2501")
        self.assertEqual(evt["price_tick"], 1.0)
        self.assertEqual(evt["last_price"], 3500.0)
        self.assertEqual(evt["upper_limit_price"], 3800.0)
        self.assertEqual(evt["seq_id"], 1)

        # Extreme limit prices (> 1e10 or <= 0) sanitized to 0.0
        abnormal_tick = types.SimpleNamespace(
            InstrumentID="rb2501",
            ExchangeID="",
            LastPrice=1e300,
            Volume=0,
            Turnover=0.0,
            OpenInterest=0.0,
            BidPrice1=1e300,
            BidVolume1=0,
            AskPrice1=1e300,
            AskVolume1=0,
            UpperLimitPrice=1.7976931348623157e308,
            LowerLimitPrice=-1.0,
            ActionDay="",
            TradingDay="",
            UpdateTime="08:59:00",
            UpdateMillisec=0,
        )
        self.md.OnRtnDepthMarketData(abnormal_tick)
        self.assertEqual(len(self.md.ring_buffer), 2)
        evt2 = self.md.ring_buffer[1]
        self.assertEqual(evt2["last_price"], 0.0)
        self.assertEqual(evt2["upper_limit_price"], 0.0)
        self.assertEqual(evt2["lower_limit_price"], 0.0)

    def test_ring_buffer_gap_detection(self):
        self.md.max_buffer_size = 5
        for i in range(10):
            tick = types.SimpleNamespace(
                InstrumentID="rb2501",
                LastPrice=3500.0 + i,
                Volume=i,
                Turnover=0.0,
                OpenInterest=0.0,
                BidPrice1=3500.0,
                BidVolume1=1,
                AskPrice1=3501.0,
                AskVolume1=1,
                UpperLimitPrice=3800.0,
                LowerLimitPrice=3200.0,
                ActionDay="20261113",
                TradingDay="20261113",
                UpdateTime="09:30:00",
                UpdateMillisec=i,
            )
            self.md.OnRtnDepthMarketData(tick)

        # Buffer only holds the last 5 ticks (seq 6 to 10)
        self.assertEqual(len(self.md.ring_buffer), 5)
        self.assertEqual(self.md.ring_buffer[0]["seq_id"], 6)

        # Asking for after_id = 2 should detect GAP because 3..5 were evicted
        res = self.md.get_ticks(self.md.stream_id, after_id=2)
        self.assertTrue(res["gap"])

        # Asking for after_id = 5 is continuous with oldest=6, so NO gap
        res_ok = self.md.get_ticks(self.md.stream_id, after_id=5)
        self.assertFalse(res_ok["gap"])
        self.assertEqual(len(res_ok["events"]), 5)

        # Stream id mismatch triggers GAP
        res_stream_mismatch = self.md.get_ticks("different_stream_id", after_id=5)
        self.assertTrue(res_stream_mismatch["gap"])

    def test_md_status_events(self):
        self.md.OnFrontDisconnected(4097)
        self.assertFalse(self.md.is_connected)
        self.assertEqual(len(self.md.ring_buffer), 1)
        evt = self.md.ring_buffer[0]
        self.assertEqual(evt["type"], "md_status")
        self.assertEqual(evt["status"], "disconnected")
        self.assertEqual(evt["reason"], 4097)

        # Login event & auto re-subscription
        self.md.subscribed_symbols = {"rb2501", "hc2501"}
        login_field = types.SimpleNamespace(TradingDay="20261113")
        self.md.OnRspUserLogin(login_field, None, 1, True)
        self.assertTrue(self.md.is_logged_in)
        self.assertEqual(len(self.md.ring_buffer), 2)
        self.assertEqual(self.md.ring_buffer[1]["status"], "logged_in")
        self.assertIn("rb2501", self.md.api.subscribed)
        self.assertIn("hc2501", self.md.api.subscribed)

    def test_http_market_endpoints(self):
        bridge.g_md_mgr = self.md
        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), bridge.CtpHttpHandler)
        server.daemon_threads = True
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            base_url = f"http://127.0.0.1:{server.server_address[1]}"

            # Health
            with urllib.request.urlopen(base_url + "/market/health", timeout=2) as resp:
                health = json.load(resp)
                self.assertIn("stream_id", health)
                self.assertIn("latest_seq_id", health)

            # Subscribe
            sub_req = urllib.request.Request(
                base_url + "/market/subscribe",
                data=json.dumps({"symbols": ["rb2501", "cu2412"]}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(sub_req, timeout=2) as resp:
                sub_res = json.load(resp)
                self.assertTrue(sub_res["success"])
                self.assertIn("rb2501", sub_res["subscribed"])
                self.assertIn("cu2412", sub_res["subscribed"])

            # Unsubscribe
            unsub_req = urllib.request.Request(
                base_url + "/market/unsubscribe",
                data=json.dumps({"symbols": ["cu2412"]}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(unsub_req, timeout=2) as resp:
                unsub_res = json.load(resp)
                self.assertTrue(unsub_res["success"])
                self.assertNotIn("cu2412", unsub_res["subscribed"])
                self.assertIn("rb2501", unsub_res["subscribed"])

            # Ticks endpoint
            with urllib.request.urlopen(base_url + "/market/ticks", timeout=2) as resp:
                ticks = json.load(resp)
                self.assertEqual(ticks["stream_id"], self.md.stream_id)
                self.assertFalse(ticks["gap"])
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
