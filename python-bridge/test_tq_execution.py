"""Execution stream regressions with fake SDK books; no account or network."""
import importlib.util
import json
import pathlib
import sys
import types
import unittest
from unittest.mock import patch


def load_bridge():
    sdk = types.ModuleType("tqsdk")
    sdk.TqApi = sdk.TqAuth = object
    exceptions = types.ModuleType("tqsdk.exceptions")
    exceptions.TqTimeoutError = type("TqTimeoutError", (Exception,), {})
    spec = importlib.util.spec_from_file_location("tq_execution_test", pathlib.Path(__file__).with_name("tq_bridge.py"))
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"tqsdk": sdk, "tqsdk.exceptions": exceptions}):
        spec.loader.exec_module(module)
    return module


bridge = load_bridge()


def book(price=1000., **kwargs):
    return types.SimpleNamespace(last_price=price, bid_price1=price-1, ask_price1=price+1,
        price_tick=1., lower_limit=500., upper_limit=2000., datetime="2026-10-27 22:59:59.000000",
        **kwargs)


class FakeApi:
    def __init__(self, books):
        self.books = books
        self.calls = []

    def get_quote(self, symbol):
        self.calls.append(symbol)
        return self.books.setdefault(symbol, types.SimpleNamespace())

    def wait_update(self, **kwargs):
        return True


class ExecutionBooks(unittest.TestCase):
    def worker(self):
        w = bridge.TqDataWorker("offline", "")
        w.connected = True
        main = book(1400., exchange_id="KQ", underlying_symbol="DCE.eg2701")
        actual = book(1000., exchange_id="DCE", underlying_symbol="")
        w.api = FakeApi({"KQ.m@DCE.eg": main, "DCE.eg2701": actual})
        w._detect_closed_bars = lambda: None
        return w

    def subscribe(self, w):
        w._handle_cmd("subscribe_execution", {"mapping": {"EG0": "KQ.m@DCE.eg"}})
        return w.execution_latest["EG0"]

    def test_alias_subscribes_actual_book_and_keeps_continuous_display_book(self):
        w = self.worker()
        q = self.subscribe(w)
        self.assertEqual((q["instrument_id"], q["exchange_id"], q["underlying_symbol"]),
                         ("eg2701", "DCE", "DCE.eg2701"))
        self.assertEqual((q["latest"], q["bid"], q["ask"]), (1000., 999., 1001.))
        self.assertEqual(w.quotes["KQ.m@DCE.eg"].last_price, 1400.)
        self.assertIn("DCE.eg2701", w.api.calls)
        self.assertEqual(w.execution_events[-1]["code"], "EG0")

    def test_direct_actual_contract_for_p2_remains_supported(self):
        w = self.worker()
        w._handle_cmd("subscribe_execution", {"mapping": {"EG2701": "DCE.eg2701"}})
        self.assertEqual(w.execution_latest["EG2701"]["latest"], 1000.)
        self.assertNotIn("KQ.m@DCE.eg", w.api.calls)

    def test_roll_invalidates_old_alias_until_new_actual_book_arrives(self):
        w = self.worker()
        self.subscribe(w)
        w.quotes["KQ.m@DCE.eg"].underlying_symbol = "DCE.eg2705"
        w._pump_update(0)
        q = w.execution_latest["EG0"]
        self.assertEqual(q["instrument_id"], "eg2705")
        self.assertEqual(q["latest"], 0.)
        self.assertEqual(q["datetime"], "")
        w.quotes["DCE.eg2705"].__dict__.update(book(1100., exchange_id="DCE").__dict__)
        w._pump_update(0)
        self.assertEqual(w.execution_latest["EG0"]["latest"], 1100.)

    def test_unresolved_main_never_publishes_continuous_prices(self):
        w = self.worker()
        self.subscribe(w)
        for underlying in ["", "KQ.m@DCE.eg", "DCE.eg"]:
            w.quotes["KQ.m@DCE.eg"].underlying_symbol = underlying
            w._publish_execution_quotes()
            self.assertEqual(w.execution_latest["EG0"]["latest"], 0.)
            self.assertEqual(w.execution_latest["EG0"]["instrument_id"], "")

    def test_idle_resubscribe_does_not_duplicate_events_and_reconnect_restores_actual(self):
        w = self.worker()
        self.subscribe(w)
        before = w.execution_seq
        self.subscribe(w)
        self.assertEqual(w.execution_seq, before)
        w._clear_runtime_state()
        w._restore_subscriptions()
        w._publish_execution_quotes()
        self.assertIn("DCE.eg2701", w.quotes)
        self.assertEqual(w.execution_latest["EG0"]["latest"], 1000.)

    def test_main_and_direct_subscriptions_share_a_tick_without_fabricating_next_quote(self):
        w = self.worker()
        self.subscribe(w)
        before = w.execution_seq
        w._handle_cmd("subscribe_execution", {"mapping": {"EG2701": "DCE.eg2701"}})
        self.assertEqual(w.execution_seq, before)
        w.quotes["DCE.eg2701"].datetime = "2026-10-27 23:00:00.100000"
        w._pump_update(0)
        self.assertEqual(w.execution_seq, before+1)
        event = w.execution_events[-1]
        direct = bridge.requested_execution_event(event, {"EG2701": "DCE.eg2701"})
        both = bridge.requested_execution_event(event, {"EG2701": "DCE.eg2701", "EG0": "KQ.m@DCE.eg"})
        self.assertEqual(direct["code"], "EG2701")
        self.assertEqual(both["code"], "EG0")
        self.assertEqual(len(w.execution_books), 1)

    def test_snapshot_after_roll_has_no_stale_alias_book(self):
        w = self.worker()
        self.subscribe(w)
        w.quotes["KQ.m@DCE.eg"].underlying_symbol = "DCE.eg2705"
        w._pump_update(0)
        self.assertNotIn("DCE.eg2701", w.execution_books)
        self.assertEqual(len(w.execution_books), 1)


class StreamHealth(unittest.IsolatedAsyncioTestCase):
    async def test_reset_and_direct_poll_route_one_book_for_shared_subscriptions(self):
        w = ExecutionBooks().worker()
        w._handle_cmd("subscribe_execution", {"mapping": {"EG0": "KQ.m@DCE.eg", "EG2701": "DCE.eg2701"}})
        w.exec_cmd = lambda *args: None
        payload = {"symbols": ["EG0", "EG2701"], "stream_id": "", "wait_seconds": 0}
        async def read():
            return payload
        with patch.object(bridge, "worker", w):
            response = await bridge.handle_execution_events(types.SimpleNamespace(json=read))
            events = json.loads(response.body)["events"]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["code"], "EG0")
            payload.update(symbols=["EG2701"], stream_id=bridge.STREAM_ID, after_id=0)
            response = await bridge.handle_execution_events(types.SimpleNamespace(json=read))
            events = json.loads(response.body)["events"]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["code"], "EG2701")

    async def test_idle_connected_stream_returns_success_without_fresh_quotes(self):
        w = ExecutionBooks().worker()
        w.exec_cmd = lambda *args: None
        request = types.SimpleNamespace(json=self.payload)
        with patch.object(bridge, "worker", w):
            response = await bridge.handle_execution_events(request)
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.body)["events"], [])

    async def test_disconnect_during_subscribe_is_not_reported_as_success(self):
        w = ExecutionBooks().worker()
        w.exec_cmd = lambda *args: setattr(w, "connected", False)
        with patch.object(bridge, "worker", w):
            response = await bridge.handle_execution_events(types.SimpleNamespace(json=self.payload))
        self.assertEqual(response.status, 503)

    async def payload(self):
        return {"symbols": ["EG0"], "stream_id": bridge.STREAM_ID, "after_id": 0, "wait_seconds": 0}


if __name__ == "__main__":
    unittest.main()
