import importlib.util
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ablation_report", ROOT / "scripts/report_signal_ablation.py")
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def row(k, warning, exit_ts, r, cash, status="closed"):
    return dict(event_key=k, symbol="RB0", warning_ts=warning,
                result=dict(evaluation=dict(status=status, exit_ts=exit_ts, net_r=r, net_amount=cash)))


class AblationReportTests(unittest.TestCase):
    def test_positive_r_can_coexist_with_negative_money_and_unknown_is_not_zero(self):
        rs = [row("a", "2026-01-05 09:00:00", "2026-01-05 10:00:00", 1., 10.),
              row("b", "2026-01-05 09:15:00", "2026-01-05 10:15:00", -.5, -100.),
              row("c", "2026-01-05 09:30:00", None, None, None, "unfinished")]
        s = M.stats(rs)
        self.assertEqual((s["n"], s["candidates"], s["net_r"], s["net_amount"]), (2, 3, .5, -90.))
        self.assertEqual(s["quality"]["unfinished"], 1)

    def test_pairs_exclude_unknown_from_both_sides_and_keep_counts(self):
        a = {"a": row("a", "t", "u", 1., 10.), "b": row("b", "t", "u", -1., -10.)}
        b = {"a": row("a", "t", "u", None, None, "ambiguous"), "b": row("b", "t", "u", .5, 5.)}
        s = M.paired(a, b)
        self.assertEqual(s["matched"], 1)
        self.assertEqual(s["left_unmatched"], 1)
        self.assertEqual(s["delta_net_r_right_minus_left"], 1.5)

    def test_reservation_blocks_overlap_even_if_blocked_signal_would_win(self):
        bars = {"RB0": dict(bars=[dict(datetime="2026-01-05 15:00:00")])}
        rs = [row("a", "2026-01-05 09:00:00", "2026-01-05 10:00:00", -1., -10.),
              row("b", "2026-01-05 09:15:00", "2026-01-05 09:30:00", 100., 1000.)]
        s = M.reserved_portfolio(rs, bars)
        self.assertEqual(s["rejected_overlap"], 1)
        self.assertEqual(s["accepted"]["net_r"], -1.)

    def test_realized_drawdown_follows_exit_time(self):
        rs = [row("a", "2026-01-05 09:00:00", "2026-01-05 12:00:00", -1., -10.),
              row("b", "2026-01-05 09:15:00", "2026-01-05 10:00:00", 2., 20.)]
        self.assertEqual(M.stats(rs)["realized_exit_order_drawdown_r"], 1.)


if __name__ == "__main__":
    unittest.main()
