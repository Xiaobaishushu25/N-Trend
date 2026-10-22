"""Money, causal execution, source rules, and selection isolation for DT revision."""
import sys
import unittest
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import optimize_sr_dual_thrust as v2


def b(at, o, h=None, l=None, c=None):
    return v2.base.Bar(datetime.fromisoformat(at), o, o if h is None else h,
                       o if l is None else l, o if c is None else c, 100, 1000)


PRIOR = [b("2026-09-18 00:00:00", 100, 110, 90, 100)]


class SourceRuleTests(unittest.TestCase):
    def test_classic_range_uses_closes_not_high_minus_low(self):
        self.assertEqual(v2.classic_range(PRIOR), 10)
        history = PRIOR+[b("2026-09-17 00:00:00", 108, 115, 95, 109)]
        self.assertEqual(v2.classic_range(history), 19)
        self.assertEqual(v2.bands(100, 19, .2), (104, 96))

    def test_next_open_instead_of_signal_close_or_intrabar_trigger(self):
        bars = [b("2026-09-21 09:00:00", 100, 106, 99, 103),
                b("2026-09-21 09:01:00", 104, 106, 103, 105),
                b("2026-09-21 14:55:00", 114)]
        ts, eq, day, _ = v2.replay_day(bars, PRIOR)
        t = ts[0]
        self.assertEqual((t["signal_bar"], t["entry_bar"]), ("2026-09-21 09:00:00", "2026-09-21 09:01:00"))
        self.assertEqual((t["entry_base"], t["entry_fill"], t["exit_fill"]), (104, 105, 113))
        self.assertEqual((t["gross_pnl"],t["slippage_cost"],t["fees"],t["net_pnl"]), (100,20,6,74))
        self.assertEqual((eq[0]["equity"], eq[-1]["equity"], day["fills"]), (0,74,2))

    def test_high_touch_without_close_confirmation_does_not_trade(self):
        bars = [b("2026-09-21 09:00:00", 100, 110, 99, 101),
                b("2026-09-21 09:01:00", 101, 108, 99, 101),
                b("2026-09-21 14:55:00", 100)]
        self.assertEqual(v2.replay_day(bars, PRIOR)[0], [])

    def test_cap_allows_three_opens_then_still_closes_on_opposite_signal(self):
        bars = [b("2026-09-21 09:00:00", 100, 104, 100, 103),
                b("2026-09-21 09:01:00", 103, 104, 96, 97),
                b("2026-09-21 09:02:00", 97, 104, 96, 103),
                b("2026-09-21 09:03:00", 103, 104, 96, 97),
                b("2026-09-21 09:04:00", 97, 104, 96, 103),
                b("2026-09-21 14:55:00", 110)]
        ts, _, day, _ = v2.replay_day(bars, PRIOR)
        self.assertEqual([t["direction"] for t in ts], [1,-1,1])
        self.assertEqual([t["exit_reason"] for t in ts], ["opposite_breakout"]*3)
        self.assertEqual((day["entries"], day["fills"],day["open_position"]), (3,6,False))
        self.assertEqual(sum(t["net_pnl"] for t in ts), -258)

    def test_after_entry_cutoff_exit_remains_enabled_but_no_reverse_open(self):
        bars = [b("2026-09-21 09:00:00", 100, 104, 100, 103),
                b("2026-09-21 09:01:00", 103),
                b("2026-09-21 13:59:00", 103, 103, 96, 97),
                b("2026-09-21 14:00:00", 96),
                b("2026-09-21 14:55:00", 95)]
        ts, _, day, _ = v2.replay_day(bars, PRIOR)
        self.assertEqual((len(ts),ts[0]["exit_bar"],ts[0]["exit_reason"]), (1,"2026-09-21 14:00:00","opposite_breakout"))
        self.assertEqual(day["entries"], 1)

    def test_flat_time_cancels_pending_entry_before_fill(self):
        # Night has no14:00 cutoff; a22:54 signal must NOT fill at22:55.
        bars = [b("2026-09-18 21:00:00", 100),
                b("2026-09-18 22:54:00", 100, 104, 100, 103),
                b("2026-09-18 22:55:00", 104)]
        self.assertEqual(v2.replay_day(bars, PRIOR)[0], [])

    def test_day_anchor_reset_and_repeat_same_direction_are_allowed(self):
        bars = [b("2026-09-18 21:00:00", 100, 104, 100, 103),
                b("2026-09-18 21:01:00", 104),
                b("2026-09-18 22:55:00", 110),
                b("2026-09-21 09:00:00", 120, 124, 120, 123),
                b("2026-09-21 09:01:00", 124),
                b("2026-09-21 14:55:00", 130)]
        ts, _, day, sessions = v2.replay_day(bars, PRIOR)
        self.assertEqual([t["direction"] for t in ts], [1,1])
        self.assertEqual([s["upper"] for s in sessions], [102,122])
        self.assertEqual(day["entries"], 2)
        day_only = v2.replay_day(bars, PRIOR, v2.Config(include_night=False))[0]
        self.assertEqual(len(day_only), 1)
        self.assertEqual(day_only[0]["entry_bar"], "2026-09-21 09:01:00")

    def test_complete_code_strict_breakout_and_later_flat_time(self):
        bars = [b("2026-09-21 09:00:00",100,102,100,102),
                b("2026-09-21 09:01:00",102,104,101,103),
                b("2026-09-21 09:02:00",104),
                b("2026-09-21 14:55:00",110),
                b("2026-09-21 14:58:00",114)]
        ts, _, day, _ = v2.replay_day(bars, PRIOR, v2.SourceCodeConfig())
        self.assertEqual((ts[0]['upper'],ts[0]['lower']), (103,97))
        self.assertEqual(ts[0]['entry_bar'], '2026-09-21 09:02:00')
        self.assertEqual(ts[0]['exit_bar'], '2026-09-21 14:58:00')
        self.assertEqual(day['net_pnl'],74)

    def test_complete_code_has_no_entry_cap_or_14oclock_cutoff(self):
        bars = [b("2026-09-21 09:00:00",100,104,100,104),
                b("2026-09-21 09:01:00",104,104,96,96),
                b("2026-09-21 09:02:00",96,104,96,104),
                b("2026-09-21 09:03:00",104,104,96,96),
                b("2026-09-21 14:00:00",96,104,96,104),
                b("2026-09-21 14:01:00",104),
                b("2026-09-21 14:58:00",110)]
        ts, _, day, _ = v2.replay_day(bars, PRIOR, v2.SourceCodeConfig())
        self.assertEqual(day['entries'],5)
        self.assertEqual(ts[-1]['entry_bar'],'2026-09-21 14:01:00')
        self.assertEqual(day['fills'],10)

    def test_complete_code_retains_exchange_trading_day_open_at_day_session(self):
        bars = [b("2026-09-18 21:00:00",100),
                b("2026-09-18 22:58:00",100),
                b("2026-09-21 09:00:00",120),
                b("2026-09-21 09:01:00",121),
                b("2026-09-21 14:58:00",122)]
        ts, _, _, sessions = v2.replay_day(bars, PRIOR, v2.SourceCodeConfig())
        self.assertEqual([s['opening'] for s in sessions],[100,100])
        self.assertEqual(ts[0]['entry_bar'],'2026-09-21 09:01:00')


class SelectionAndRealDataTests(unittest.TestCase):
    def test_selection_uses_training_profit_penalized_by_drawdown_and_trade_gate(self):
        a,b,c,d = [v2.Config(n=n) for n in [1,2,3,4]]
        rows = [(a,dict(trades=10,net_pnl=500,minute_close_max_drawdown=1000)),
                (b,dict(trades=10,net_pnl=300,minute_close_max_drawdown=100)),
                (c,dict(trades=5,net_pnl=10000,minute_close_max_drawdown=0)),
                (d,dict(trades=10,net_pnl=-10,minute_close_max_drawdown=0))]
        self.assertEqual(v2.choose(rows), b)
        self.assertIsNone(v2.choose(rows[2:]))

    def test_history_excludes_current_and_future_daily_prices(self):
        keys = [date(2026,9,d) for d in [14,15,16,17,18,21,22]]
        daily = {d:b(str(d)+" 00:00:00",100,110,90,100) for d in keys}
        before = v2.history(daily,date(2026,9,21),5)
        daily[date(2026,9,21)] = b("2026-09-21 00:00:00",9999)
        self.assertEqual(v2.history(daily,date(2026,9,21),5),before)
        del daily[date(2026,9,17)]
        with self.assertRaises(AssertionError):
            v2.history(daily,date(2026,9,21),5)

    def test_frozen_data_costs_and_future_prefix_invariance(self):
        if not (v2.base.OUT / "sources.json").exists():
            self.skipTest("No downloaded fixtures")
        groups,daily,dates = v2.prepare()
        for cfg in [v2.Config(),v2.Config(n=1,k=.1),v2.Config(n=4,k=.5),v2.Config(include_night=False),
                    v2.SourceCodeConfig(),v2.SourceCodeConfig(n=1,k=.5)]:
            s,ts,curve,days,_ = v2.replay(groups,daily,dates,cfg)
            self.assertEqual(s["net_pnl"],s["gross_pnl"]-26*len(ts))
            self.assertTrue(all(d["entries"] <= cfg.max_entries for d in days) if cfg.max_entries else True)
            self.assertEqual(curve[-1]["equity"], sum(t["net_pnl"] for t in ts))
        for d in dates:
            bars=groups[d]; prior=v2.history(daily,d,5); n=len(bars)//2
            for cfg in [v2.Config(),v2.SourceCodeConfig()]:
                reference=v2.replay_day(bars[:n],prior,cfg)
                changed=v2.replay_day(bars[:n]+[replace(b,h=b.h+1000,l=b.l-1000,c=b.c+500) for b in bars[n:]],prior,cfg)
                self.assertEqual(reference[1],changed[1][:n])


if __name__ == "__main__":
    unittest.main()
