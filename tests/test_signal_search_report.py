import pathlib
import sys
import unittest

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/"scripts"))
from report_signal_search import distribution


def row(key,r,cash,status="closed"):
    return dict(event_key=key,result=dict(evaluation=dict(status=status,net_r=r,net_amount=cash,exit_ts=key)))


class SearchDistributionTests(unittest.TestCase):
    def test_unknown_is_excluded_from_distribution_not_assigned_flat(self):
        s=distribution([row("a",None,None,"ambiguous")])
        self.assertEqual(s["n"],0)
        self.assertIsNone(s["median_net_r"])
        self.assertIsNone(s["worst10pct_mean_r"])

    def test_tail_and_top_five_are_computed_for_each_money_and_r_metric(self):
        s=distribution([row(str(i),float(i),10.*i) for i in range(-5,6)])
        self.assertAlmostEqual(s["worst10pct_mean_r"],-4.5)
        self.assertEqual(s["net_amount_without_top5"],-150.)
        self.assertEqual(s["longest_realized_loss_run"],5)
        self.assertEqual(s["worst_amount"],-50.)
        self.assertEqual(s["realized_exit_order_drawdown_amount"],150.)


if __name__=="__main__":
    unittest.main()
