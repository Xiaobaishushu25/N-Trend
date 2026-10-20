"""Execution/accounting tests for SR research, independent of network or database."""
import importlib.util
import random
import sys
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "backtest_sr_dual_thrust.py"
spec = importlib.util.spec_from_file_location("sr_dual_thrust", SOURCE)
bt = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bt
spec.loader.exec_module(bt)


def bar(at, o, h=None, l=None, c=None):
    return bt.Bar(datetime.fromisoformat(at), o, o if h is None else h,
                  o if l is None else l, o if c is None else c, 100, 1000)


PREVIOUS = [bar("2026-09-18 09:00:00", 105, 110, 100, 105)]


class ExecutionTests(unittest.TestCase):
    def test_round_trip_costs_and_cash_reconcile(self):
        bars = [bar("2026-09-21 09:00:00", 100, 103, 99, 102),
                bar("2026-09-21 09:01:00", 102, 105, 101, 104),
                bar("2026-09-21 14:55:00", 114)]
        trades, curve, day = bt.replay_day(bars, PREVIOUS)
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual((t["entry_fill"], t["exit_fill"]), (105, 113))
        self.assertEqual((t["gross_pnl"], t["slippage_cost"], t["fees"], t["net_pnl"]), (100, 20, 6, 74))
        self.assertEqual(day["net_pnl"], 74)
        self.assertEqual(curve[1]["equity"], -13)  # entry fee plus mark-to-close slippage
        self.assertEqual(curve[-1]["equity"], 74)

    def test_current_bar_cannot_retroactively_fill_its_signal(self):
        bars = [bar("2026-09-21 09:00:00", 100, 108, 99, 102),
                bar("2026-09-21 09:01:00", 102, 103, 101, 102),
                bar("2026-09-21 14:55:00", 100)]
        self.assertEqual(bt.replay_day(bars, PREVIOUS)[0], [])

    def test_gap_uses_worse_open_instead_of_trigger(self):
        bars = [bar("2026-09-21 09:00:00", 100, 103, 99, 102),
                bar("2026-09-21 09:01:00", 110, 112, 109, 111),
                bar("2026-09-21 14:55:00", 115)]
        t = bt.replay_day(bars, PREVIOUS)[0][0]
        self.assertEqual((t["entry_base"], t["entry_fill"], t["net_pnl"]), (110, 111, 24))

    def test_reverse_pays_close_and_open_then_never_reenters_used_side(self):
        bars = [bar("2026-09-21 09:00:00", 100, 103, 99, 102),
                bar("2026-09-21 09:01:00", 102, 105, 101, 104),
                bar("2026-09-21 09:02:00", 104, 104, 93, 94),
                bar("2026-09-21 09:03:00", 95, 105, 95, 104),
                bar("2026-09-21 09:04:00", 104, 110, 103, 109),
                bar("2026-09-21 14:55:00", 110)]
        trades, _, day = bt.replay_day(bars, PREVIOUS)
        self.assertEqual([t["direction"] for t in trades], [1, -1])
        self.assertEqual([t["net_pnl"] for t in trades], [-126, -126])
        self.assertEqual(sum(t["fees"] for t in trades), 12)
        self.assertEqual((day["fills"], day["net_pnl"]), (4, -252))

    def test_night_flat_cancels_orders_and_day_session_shares_entry_limit(self):
        bars = [bar("2026-09-18 21:00:00", 100, 103, 99, 102),
                bar("2026-09-18 21:01:00", 102, 105, 101, 104),
                bar("2026-09-18 22:55:00", 114),
                bar("2026-09-18 22:56:00", 114, 115, 90, 94),
                bar("2026-09-21 09:00:00", 105, 108, 105, 107),
                bar("2026-09-21 09:01:00", 107, 110, 106, 109),
                bar("2026-09-21 14:55:00", 110)]
        trades, curve, day = bt.replay_day(bars, PREVIOUS)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["exit_reason"], "night_flat")
        self.assertEqual(trades[0]["trading_date"], "2026-09-21")
        self.assertTrue(all(r["position"] == 0 for r in curve[2:]))
        self.assertFalse(day["open_position"])

    def test_flat_open_precedes_same_bar_stop_touch_and_blocks_entries(self):
        bars = [bar("2026-09-21 09:00:00", 100, 103, 99, 102),
                bar("2026-09-21 09:01:00", 102, 105, 101, 104),
                bar("2026-09-21 14:55:00", 110, 111, 90, 91)]
        trades, _, _ = bt.replay_day(bars, PREVIOUS)
        self.assertEqual((len(trades), trades[0]["exit_base"], trades[0]["exit_reason"]), (1, 110, "day_flat"))

    def test_levels_are_rounded_outwards_to_executable_ticks(self):
        previous = [bar("2026-09-18 09:00:00", 105, 111, 100, 105)]
        self.assertEqual(bt.levels(previous, 100, bt.Config()), (11, 105, 93))

    def test_stop_fill_matches_independent_tick_walk_on_both_ohlc_paths(self):
        rng = random.Random(701)
        for _ in range(1000):
            lo = rng.randrange(50, 100)
            hi = rng.randrange(lo, 110)
            op, cl = rng.randint(lo, hi), rng.randint(lo, hi)
            b = bar("2026-09-21 09:01:00", op, hi, lo, cl)
            trigger = rng.randint(45, 115)
            for path in [[op, hi, lo, cl], [op, lo, hi, cl]]:
                prices = [op]
                for a, z in zip(path, path[1:]):
                    step = 1 if z > a else -1
                    prices.extend(range(a+step, z+step, step))
                for direction in [-1, 1]:
                    ref = next((p for p in prices if (p-trigger)*direction >= 0), None)
                    self.assertEqual(bt.stop_fill(b, direction, trigger), ref)


class CalendarAndStatisticsTests(unittest.TestCase):
    def test_weekend_and_holidays(self):
        self.assertEqual(bt.trading_date(datetime(2026, 9, 18, 21)), date(2026, 9, 21))
        self.assertEqual(len(bt.expected_minutes(date(2026, 9, 21))), 345)
        self.assertEqual(len(bt.expected_minutes(date(2026, 9, 28))), 225)
        self.assertEqual(len(bt.expected_minutes(date(2026, 10, 8))), 225)

    def test_payoff_ratio_is_different_from_profit_factor_and_uses_net_pnl(self):
        trades = [dict(net_pnl=p, gross_pnl=p+26, slippage_cost=20, fees=6) for p in [74, 74, -126, 0]]
        stats = bt.summarize(trades, [dict(equity=74), dict(equity=148), dict(equity=22)])
        self.assertEqual((stats["trades"], stats["wins"], stats["losses"], stats["breakeven"]), (4, 2, 1, 1))
        self.assertEqual(stats["win_rate"], .5)
        self.assertAlmostEqual(stats["payoff_ratio"], 74/126)
        self.assertAlmostEqual(stats["profit_factor"], 148/126)
        self.assertEqual(stats["minute_close_max_drawdown"], 126)

    def test_frozen_real_data_cash_costs_and_future_mutation(self):
        # Real fixtures are immutable public CSV snapshots saved beside the report.
        if not (bt.OUT / "sources.json").exists():
            self.skipTest("Run the public data download first")
        groups, dates, _ = bt.prepare()
        stats, trades, curve, days = bt.replay(groups, dates)
        self.assertEqual(stats["net_pnl"], stats["gross_pnl"]-stats["slippage_cost"]-stats["fees"])
        self.assertEqual(stats["fees"], len(trades)*6)
        self.assertEqual(stats["slippage_cost"], len(trades)*20)
        self.assertEqual(curve[-1]["equity"], sum(d["net_pnl"] for d in days))
        self.assertTrue(all(d["trades"] <= 2 for d in days))
        for d in dates:
            bars = groups[d]
            previous = groups[bt.previous_trading_day(d)]
            n = len(bars)//2
            reference = bt.replay_day(bars[:n], previous)
            # Change every future high/low/close by1000; all earlier state stays identical.
            mutated = bars[:n]+[replace(b, h=b.h+1000, l=b.l-1000, c=b.c+500) for b in bars[n:]]
            changed = bt.replay_day(mutated, previous)
            self.assertEqual(reference[1], changed[1][:n])
            cutoff = bars[n].dt.isoformat(sep=" ")
            self.assertEqual(reference[0], [t for t in changed[0] if t["exit_bar"] < cutoff])


if __name__ == "__main__":
    unittest.main()
