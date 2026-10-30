"""Session-close recovery without exchange ticks, transitions or real accounts."""
import datetime
import pathlib
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import pandas as pd
from test_tq_execution import bridge


class SessionClose(unittest.TestCase):
    def worker(self, end="2026-10-30 11:30:00"):
        w = bridge.TqDataWorker("offline", "")
        w.connected = True
        w.api = types.SimpleNamespace(wait_update=lambda **kwargs: False, is_changing=lambda *args: False)
        end_epoch = datetime.datetime.fromisoformat(end).replace(
            tzinfo=datetime.timezone(datetime.timedelta(hours=8))).timestamp()
        rows = pd.DataFrame([dict(datetime=(end_epoch - 600 + i * 300) * 1e9,
            open=100., high=102., low=99., close=101., volume=5., close_oi=10.) for i in range(2)])
        key = ("KQ.m@SHFE.rb", 300)
        w.klines[key] = rows
        w.kline_meta[key] = dict(initialized=True, duration=300, symbol="RB0", period="5m",
            tq_symbol=key[0], last_datetime=int(rows.iloc[-1]["datetime"]), last_trade_status=None)
        w.quotes[key[0]] = types.SimpleNamespace(trading_time={"day": [["09:00:00", "11:30:00"]]})
        return w, end_epoch

    def test_idle_pump_recovers_missed_transition_and_deduplicates(self):
        w, end = self.worker()
        with patch.object(bridge.time, "time", return_value=end + 74):
            w._pump_update(end + 74)
        self.assertEqual(len(w.events), 0)
        w.last_close_check_at = 0
        with patch.object(bridge.time, "time", return_value=end + 75):
            w._pump_update(end + 75)
            w._detect_closed_bars()
        self.assertEqual(len(w.events), 1)
        self.assertEqual(w.events[0]["bar_end"], "2026-10-30 11:30:00")
        self.assertEqual(w.events[0]["proof"], "session_settled")

    def test_restarting_in_break_does_not_require_a_status_transition(self):
        w, end = self.worker()
        with patch.object(bridge.time, "time", return_value=end + 1200):
            w._detect_closed_bars()
        self.assertEqual(len(w.events), 1)

    def test_existing_http_serial_still_installs_close_event_metadata(self):
        w, _ = self.worker()
        key = next(iter(w.klines))
        rows = w.klines[key]
        w.kline_meta.clear()
        w.api.get_kline_serial = lambda *args, **kwargs: rows
        w.api.get_trading_status = lambda *args: types.SimpleNamespace(trade_status="NOTRADING")
        result = w._handle_cmd("subscribe_klines", {"subscriptions": [dict(symbol="RB0", tq_symbol=key[0],
            duration_seconds=300, period="5m")], "data_length": 300})
        self.assertTrue(w.kline_meta[key]["initialized"])
        self.assertEqual(result["subscribed"], ["RB0"])

    def test_ordinary_stalled_bar_and_disconnected_worker_stay_unconfirmed(self):
        w, end = self.worker("2026-10-30 11:25:00")
        with patch.object(bridge.time, "time", return_value=end + 1200):
            w._detect_closed_bars()
        self.assertFalse(w.events)
        w, end = self.worker()
        for field in ("connected", "reconnecting"):
            setattr(w, field, field == "reconnecting")
            with patch.object(bridge.time, "time", return_value=end + 1200):
                w._detect_closed_bars()
            self.assertFalse(w.events)
            w.connected = True


if __name__ == "__main__":
    unittest.main()
