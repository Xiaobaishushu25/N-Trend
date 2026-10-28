import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import backtest_three_sma_15m as s

class ThreeSmaTests(unittest.TestCase):
    def test_signal_order_and_warmup(self):
        self.assertEqual(s.signal(list(range(19))), 0)
        self.assertEqual(s.signal(list(range(20))), 1)
        self.assertEqual(s.signal(list(range(20, 0, -1))), -1)
        self.assertEqual(s.signal([1]*20), 0)

    def test_break_delays_signal_and_accounting(self):
        start = s.ns('2026-08-10 09:00:00')
        bars = [(start-(19-i)*900_000_000_000, float(i+1)) for i in range(19)]
        bars.append((s.ns('2026-08-10 10:15:00'), 20.0))
        minutes = [(s.ns('2026-08-10 10:14:00'), 100., 100., 1),
                   (s.ns('2026-08-10 10:30:00'), 101., 102., 1),
                   (s.ns('2026-08-10 10:31:00'), 102., 103., 1)]
        stats, trades, equity = s.run(minutes, bars)
        self.assertEqual(trades[0]['entry_time'], '2026-08-10 10:30+08:00')
        self.assertEqual(trades[0]['entry_price'], 102.)
        self.assertEqual(trades[0]['exit_price'], 102.)
        self.assertAlmostEqual(stats['net_pnl'], -0.31, places=2)
        self.assertAlmostEqual(sum(x['net_pnl'] for x in trades), equity[-1]['equity'], places=4)

if __name__ == '__main__':
    unittest.main()
