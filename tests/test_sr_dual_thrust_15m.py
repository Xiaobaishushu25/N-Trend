"""Quarter-hour alignment, causality and independent-window money accounting."""
import json
import sys
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"scripts"))
import backtest_sr_dual_thrust_15m as experiment

a = experiment.a
opt = experiment.opt


def bar(at, o, h=None, l=None, c=None):
    return a.base.Bar(datetime.fromisoformat(at), o, o if h is None else h,
                      o if l is None else l, o if c is None else c, 100, 1000)


class QuarterHourTests(unittest.TestCase):
    def test_clock_alignment_and_recess_boundaries(self):
        for observed, expected in [("2026-09-21 09:14:00", "2026-09-21 09:00:00"),
                                   ("2026-09-21 09:15:00", "2026-09-21 09:15:00"),
                                   ("2026-09-21 10:30:00", "2026-09-21 10:30:00"),
                                   ("2026-09-21 13:30:00", "2026-09-21 13:30:00")]:
            self.assertEqual(a.period_key(datetime.fromisoformat(observed), 15), datetime.fromisoformat(expected))
        for invalid in [0, -15, 7, 61, 15.5]:
            with self.assertRaises(ValueError):
                a.period_key(datetime(2026, 9, 21, 9, 1), invalid)

    def test_quarter_hour_history_open_snapshot_and_next_minute_execution(self):
        warmup = [bar("2026-09-18 13:30:00", 100, 110, 90, 100),
                  bar("2026-09-18 13:45:00", 100, 105, 95, 102),
                  bar("2026-09-18 14:00:00", 102, 108, 96, 104),
                  bar("2026-09-18 14:15:00", 104, 109, 97, 106)]
        bars = warmup+[bar("2026-09-18 21:00:00", 107, 113, 107, 113),
                       bar("2026-09-18 21:01:00", 114),
                       bar("2026-09-18 21:02:00", 124)]
        stats, trades, _, bands = a.replay(bars, experiment.FIXED, period_minutes=15)
        self.assertEqual(len(bands), 1)  # No synthetic bars in the recess.
        self.assertEqual((bands[0]["range"], bands[0]["upper"], bands[0]["lower"]), (17, 112.1, 98.5))
        self.assertEqual(trades[0]["entry_bar"], "2026-09-18 21:01:00")
        self.assertEqual((stats["gross_pnl"], stats["slippage_cost"], stats["fees"], stats["net_pnl"]), (100, 20, 6, 74))
        altered = warmup+[replace(bars[-3], h=1000, l=1), *bars[-2:]]
        self.assertEqual(bands, a.replay(altered, experiment.FIXED, period_minutes=15)[3])

    def test_grid_retains_costs_and_only_searches_intraday_policy(self):
        candidates = experiment.grid()
        self.assertEqual(len(candidates), 648)
        self.assertTrue(all(c.intraday and c.slip_ticks == 1 for c in candidates))
        self.assertEqual(len({opt.key(c) for c in candidates}), 648)


class FrozenFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (a.OUT/"article_source.json").exists():
            raise unittest.SkipTest("No frozen public fixtures")
        cls.minutes = a.prepare()
        cls.features = opt.Features(cls.minutes, period_minutes=15)

    def test_legacy_hourly_runs_match_previous_saved_results(self):
        prior = json.loads((a.OUT/"results.json").read_text(encoding="utf-8"))["results"]
        for cfg in a.CONFIGS:
            default = a.replay(self.minutes, cfg)
            self.assertEqual(default, a.replay(self.minutes, cfg, period_minutes=60))
            self.assertEqual(default[0], prior[cfg.label])

    def test_quarter_hour_fast_and_detailed_accounting_across_split_and_costs(self):
        configs = [experiment.FIXED, a.Config(cycle=2, ks=.1, kx=3, intraday=True),
                   a.Config(cycle=16, ks=3, kx=.1, intraday=True),
                   a.Config(cycle=8, ks=.75, kx=1.5, intraday=True)]
        for cfg in configs:
            for start, end in [(opt.base.START, opt.base.END), (opt.base.START, opt.TRAIN_END),
                               (opt.VALIDATION_START, opt.base.END)]:
                with self.subTest(config=cfg, start=start, end=end):
                    s, ts, eq, _ = opt.assert_reference(self.features, self.minutes, replace(cfg, slip_ticks=2), start, end)
                    self.assertEqual(s["net_pnl"], sum(t["net_pnl"] for t in ts))
                    self.assertEqual(s["net_pnl"], s["gross_pnl"]-s["slippage_cost"]-s["fees"])
                    self.assertEqual(s["slippage_cost"], 40*len(ts))
                    self.assertEqual(s["fees"], 6*len(ts))
                    self.assertEqual(s["cross_session_trades"], 0)
                    self.assertEqual(eq[0]["position"], 0)

    def test_future_minutes_cannot_change_previous_quarter_hour_bands_or_fills(self):
        cutoff = datetime(2026, 10, 13, 9, 20)
        prefix = [b for b in self.minutes if b.dt < cutoff]
        altered = [b if b.dt < cutoff else replace(b, h=b.h+1000, l=b.l-1000, c=b.c+500) for b in self.minutes]
        other = opt.Features(altered, period_minutes=15)
        stop = sum(b.dt < cutoff for b in self.minutes)
        self.assertEqual(self.features.anchor[:stop], other.anchor[:stop])
        for cycle in opt.CYCLES:
            self.assertEqual(self.features.spans[cycle][:stop], other.spans[cycle][:stop])
        for cfg in [experiment.FIXED, a.Config(cycle=16, ks=.75, kx=1.5, intraday=True)]:
            _, old_ts, old_eq, old_bands = a.replay(prefix, cfg, terminal=False, period_minutes=15)
            _, new_ts, new_eq, new_bands = a.replay(altered, cfg, terminal=False, period_minutes=15)
            self.assertEqual(old_eq, [r for r in new_eq if datetime.fromisoformat(r["bar_end"]) <= cutoff])
            self.assertEqual(old_ts, [t for t in new_ts if datetime.fromisoformat(t["exit_bar"]) < cutoff])
            self.assertEqual(old_bands, [b for b in new_bands if datetime.fromisoformat(b["first_observation"]) < cutoff])


if __name__ == "__main__":
    unittest.main()
