"""Offline regression tests; no CTP library, credentials, or counter connection."""
import importlib.util
import json
import pathlib
import socketserver
import sys
import threading
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
    api.CThostFtdcInputOrderActionField = types.SimpleNamespace
    api.THOST_FTDC_AF_Delete = "0"
    api.THOST_FTDC_PD_Long = "2"
    package = types.ModuleType("openctp_ctp")
    package.thosttraderapi = api
    spec = importlib.util.spec_from_file_location("ctp_bridge_test", pathlib.Path(__file__).with_name("ctp_trader.py"))
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"openctp_ctp": package, "openctp_ctp.thosttraderapi": api}):
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

    def ReqOrderAction(self, req, req_id):
        return self.send("cancel", req_id)

    def wait_for(self, count):
        with self.changed:
            return self.changed.wait_for(lambda: len(self.calls) >= count, timeout=2)


def position(today=1, volume=1, cost=100):
    return types.SimpleNamespace(InstrumentID="pb2611", PosiDirection="2", Position=volume,
                                 TodayPosition=today, OpenCost=cost, PositionCost=cost,
                                 PositionProfit=5, UseMargin=10)


def account():
    return types.SimpleNamespace(Balance=1000, CurrMargin=100, PreBalance=900, Available=850,
                                 CloseProfit=0, PositionProfit=100, Commission=0, CurrencyID="CNY")


class QueryTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
