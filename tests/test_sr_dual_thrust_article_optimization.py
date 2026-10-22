"""Parameter eligibility and compact/detailed replay agreement on frozen bars."""
import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import optimize_sr_dual_thrust_article as opt


class SelectionTests(unittest.TestCase):
    def test_grid_is_unique_and_does_not_change_costs_or_candle_duration(self):
        candidates = opt.grid()
        self.assertEqual(len(candidates), 1296)
        self.assertEqual(len({opt.key(c) for c in candidates}), 1296)
        self.assertEqual({c.slip_ticks for c in candidates}, {1})
        self.assertEqual({c.intraday for c in candidates}, {False, True})

    def test_neighbors_stay_in_same_holding_mode_and_change_one_axis(self):
        cfg = opt.article.Config(cycle=5, ks=.3, kx=.5, intraday=True)
        training = {opt.key(c): {"net_pnl": 0} for c in opt.grid()}
        expected = {opt.key(cfg)}
        for axis, values in [(1, opt.CYCLES), (2, opt.COEFFICIENTS), (3, opt.COEFFICIENTS)]:
            i = values.index(opt.key(cfg)[axis])
            for j in [i-1, i+1]:
                k = list(opt.key(cfg)); k[axis] = values[j]
                expected.add(tuple(k))
        for k in expected:
            training[k]["net_pnl"] = 10
        training[opt.key(replace(cfg, intraday=False))]["net_pnl"] = 100000
        self.assertEqual(opt.neighbors(cfg, training), [10]*7)

    def test_selection_rejects_high_profit_with_too_few_training_trades(self):
        training = {opt.key(c): dict(net_pnl=0, trades=0, minute_close_max_drawdown=0) for c in opt.grid()}
        chosen = opt.article.Config(cycle=5, ks=.3, kx=.5, intraday=True)
        for c in opt.grid():
            if c.intraday:
                training[opt.key(c)]["net_pnl"] = 100
        training[opt.key(chosen)]["trades"] = 8
        too_few = replace(chosen, cycle=12)
        training[opt.key(too_few)].update(net_pnl=10000, trades=7)
        selected, _ = opt.select(training)
        self.assertEqual(selected[True], chosen)
        self.assertIsNone(selected[False])


class FixtureAgreementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (opt.article.OUT / "article_source.json").exists():
            raise unittest.SkipTest("No frozen public article fixtures")
        cls.minutes = opt.article.prepare()
        cls.features = opt.Features(cls.minutes)

    def test_fractional_coefficients_costs_and_window_resets_match_detailed_engine(self):
        # Include grid edges, both holding policies, fractional thresholds and
        # larger costs. Independent test windows must start flat, using only
        # prior bars as indicator warmup.
        configs = [opt.article.Config(cycle=2, ks=.1, kx=3),
                   opt.article.Config(cycle=16, ks=3, kx=.1),
                   opt.article.Config(cycle=5, ks=.2, kx=.5),
                   opt.article.Config(cycle=8, ks=.75, kx=1.5)]
        windows = [(opt.base.START, opt.base.END),
                   (opt.base.START, opt.TRAIN_END),
                   (opt.VALIDATION_START, opt.base.END)]
        for cfg in configs:
            for mode in [False, True]:
                for start, end in windows:
                    c = replace(cfg, intraday=mode, slip_ticks=2)
                    with self.subTest(config=c, start=start, end=end):
                        stats, trades, eq, _ = opt.assert_reference(self.features, self.minutes, c, start, end)
                        self.assertEqual(stats["net_pnl"], sum(t["net_pnl"] for t in trades))
                        self.assertEqual(eq[0]["position"], 0)
                        self.assertEqual(stats["fees"], 6*stats["trades"])
                        self.assertEqual(stats["slippage_cost"], 40*stats["trades"])
                        if mode:
                            self.assertEqual(stats["cross_session_trades"], 0)

    def test_validation_prices_cannot_change_training_features_or_pnl(self):
        altered = [b if opt.base.trading_date(b.dt) <= opt.TRAIN_END
                   else replace(b, o=b.o+500, h=b.h+500, l=b.l+500, c=b.c+500)
                   for b in self.minutes]
        other = opt.Features(altered)
        first, stop = self.features.bounds(opt.base.START, opt.TRAIN_END)
        self.assertEqual(self.features.anchor[first:stop], other.anchor[first:stop])
        for n in opt.CYCLES:
            self.assertEqual(self.features.spans[n][first:stop], other.spans[n][first:stop])
            for mode in [False, True]:
                cfg = opt.article.Config(cycle=n, ks=.3, kx=.75, intraday=mode)
                self.assertEqual(opt.compact(self.features, cfg, opt.base.START, opt.TRAIN_END),
                                 opt.compact(other, cfg, opt.base.START, opt.TRAIN_END))


if __name__ == "__main__":
    unittest.main()
