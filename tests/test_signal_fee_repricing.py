import copy
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
from build_exchange_fee_profile import rule
from reprice_signal_costs import TradingDates, fee_for, reprice


def sample(symbol="RB0"):
    return dict(symbol=symbol, direction="up", multiplier=10,
        result=dict(evaluation=dict(status="closed", volume=2, gross_points=30., risk=10., net_amount=250., net_r=1.25),
                    fills=[dict(ts="2026-10-09 21:00:00", price=101., volume=2, offset="open"),
                           dict(ts="2026-10-10 01:00:00", price=111., volume=1, offset="close"),
                           dict(ts="2026-10-13 09:00:00", price=121., volume=1, offset="close")]))


class FeeRepricingTests(unittest.TestCase):
    def setUp(self):
        self.calendar = TradingDates({"RB0": dict(bars=[dict(datetime=d + " 09:00:00")
                                                       for d in ["2026-10-09", "2026-10-12", "2026-10-13"]])})

    def test_weekend_night_and_next_morning_share_exchange_trading_date(self):
        for ts in ["2026-10-09 21:00:00", "2026-10-10 01:00:00", "2026-10-12 00:30:00", "2026-10-12 09:00:00"]:
            self.assertEqual(self.calendar.day(ts), "2026-10-12")
        with self.assertRaises(ValueError):
            self.calendar.day("2026-10-13 21:00:00")

    def test_sugar_total_roundtrip_two_yuan_applies_once_per_initial_lot(self):
        row = sample("SR0")
        result = reprice(row, dict(products=dict(SR=dict(roundtrip_fixed_yuan_per_lot=2.))), self.calendar)
        self.assertEqual(result["cost_repricing"]["commission"], 4.)
        self.assertEqual(result["result"]["evaluation"]["net_amount"], 296.)
        self.assertEqual(row["result"]["evaluation"]["net_amount"], 250.)

    def test_notional_fee_uses_each_fill_and_mixed_today_yesterday_partial_exits(self):
        spec = dict(open=rule("1/万分之 (0.1元)"), close_today=rule("0元"), close_yesterday=rule("1/万分之"))
        result = reprice(sample(), dict(products=dict(RB=spec)), self.calendar)
        detail = result["cost_repricing"]
        self.assertAlmostEqual(detail["commission"], (101 * 2 + 121) * 10 * .0001)
        self.assertEqual([x["kind"] for x in detail["fee_legs"]], ["open", "close_today", "close_yesterday"])
        self.assertAlmostEqual(result["result"]["evaluation"]["net_amount"], 300. - .323)

    def test_malformed_ledger_or_missing_rate_cannot_be_treated_as_free(self):
        profile = dict(products=dict(SR=dict(roundtrip_fixed_yuan_per_lot=2.)))
        with self.assertRaises(KeyError):
            reprice(sample(), profile, self.calendar)
        row = sample("SR0")
        row["result"]["fills"][-1]["volume"] = 2
        with self.assertRaises(ValueError):
            reprice(row, profile, self.calendar)
        row = sample("SR0")
        row["result"]["evaluation"]["gross_points"] = 31.
        with self.assertRaises(ValueError):
            reprice(row, profile, self.calendar)

    def test_no_second_slippage_charge_and_short_cash_reconciles(self):
        row = sample("SR0")
        row["direction"] = "down"
        for fill in row["result"]["fills"]:
            fill["price"] = 202 - fill["price"]
        result = reprice(row, dict(products=dict(SR=dict(roundtrip_fixed_yuan_per_lot=2.))), self.calendar)
        self.assertEqual(result["result"]["evaluation"]["net_amount"], 296.)
        self.assertAlmostEqual(result["result"]["evaluation"]["net_r"], 1.48)

    def test_fee_table_units_are_parsed_without_using_rounded_example_amounts(self):
        self.assertEqual(rule("0.5/万分之 (2.48元)")["notional_rate"], .00005)
        self.assertEqual(fee_for(rule("4.3元"), 100, 2, 10), 8.6)
        with self.assertRaises(ValueError):
            rule("未知")


if __name__ == "__main__":
    unittest.main()
