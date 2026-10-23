import copy
import pathlib
import sys
import unittest

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/"scripts"))
from validate_capital_capacity import Prices, simulate
from validate_execution_research import capacity
from validate_score_sizing import band, high_low


def row(key="a",same_bar=False):
    start="2026-10-09 09:05:00"
    end=start if same_bar else "2026-10-09 09:10:00"
    return dict(event_key=key,symbol="RB0",direction="up",warning_ts="2026-10-09 09:00:00",multiplier=10.,
        planned_entry=100.,score=3.,tick_size=1.,result=dict(
            evaluation=dict(status="closed",entry_ts=start,exit_ts=end,exit_reason="target_1r",fill=100.,
                            risk=10.,volume=1,gross_points=10.,net_amount=98.,net_r=.98),
            disagreement=None,fills=[dict(ts=start,price=100.,volume=1,offset="open",reason="entry"),
                                     dict(ts=end,price=110.,volume=1,offset="close",reason="target_1r")]),
        cost_repricing=dict(commission=2.,fee_legs=[dict(kind="open",fee=1.),dict(kind="close_today",fee=1.)]))


class ExecutionValidationTests(unittest.TestCase):
    def setUp(self):
        self.symbols=dict(RB0=dict(bars=[dict(datetime=f"2026-10-09 09:{m:02}:00",close=100.)
                                         for m in range(0,35,5)]))
        self.margins=dict(RB=dict(long=.1,short=.1))

    def test_same_bar_roundtrip_must_open_before_own_exit_under_both_orders(self):
        for exits_first in (False,True):
            s=simulate([row(same_bar=True)],self.symbols,self.margins,capital=200.,exits_first=exits_first)
            self.assertEqual(s["remaining_known_positions"],0)
            self.assertAlmostEqual(s["total_account_final_equity"],298.)
            self.assertAlmostEqual(s["accepted_priced"]["net_amount"],98.)

    def test_margin_is_price_times_multiplier_times_rate_times_relative_buffer(self):
        s=simulate([row()],self.symbols,self.margins,capital=200.)
        self.assertAlmostEqual(s["decisions"][0]["required_margin"],110.)
        self.assertAlmostEqual(s["decisions"][0]["rate_after_buffer"],.11)

    def test_symbol_slot_and_shared_funding_are_evaluated_in_one_loop(self):
        s=simulate([row("a"),row("b")],self.symbols,self.margins,capital=1000.,slots_per_symbol=1)
        self.assertEqual(s["accepted_priced"]["n"],1)
        self.assertEqual(s["rejected_by_symbol_capacity"],1)
        self.assertEqual(s["rejected_capital"],0)

    def test_capital_rejection_does_not_leave_a_ghost_symbol_slot(self):
        expensive=row("a")
        expensive["result"]["fills"][0]["price"]=1000.
        expensive["result"]["evaluation"]["fill"]=1000.
        s=simulate([expensive,row("b")],self.symbols,self.margins,capital=200.,slots_per_symbol=1)
        self.assertEqual(s["rejected_capital"],1)
        self.assertEqual(s["rejected_by_symbol_capacity"],0)
        self.assertEqual(s["accepted_priced"]["n"],1)

    def test_score_cannot_change_one_lot_admission_or_funding(self):
        low,high=row(),row()
        low["score"],high["score"]=2.5,4.5
        a=simulate([low],self.symbols,self.margins,capital=200.,slots_per_symbol=1)
        b=simulate([high],self.symbols,self.margins,capital=200.,slots_per_symbol=1)
        self.assertEqual(a["decisions"],b["decisions"])
        self.assertEqual(a["known_component_net_realized"],b["known_component_net_realized"])

    def test_simultaneous_orders_share_budget_and_reject_before_overspending(self):
        s=simulate([row("a"),row("b")],self.symbols,self.margins,capital=200.)
        self.assertEqual(s["rejected_capital"],1)
        self.assertEqual(s["accepted_priced"]["n"],1)
        self.assertEqual(s["maximum_concurrent_lots"],1)

    def test_future_exit_fee_does_not_change_opening_admission(self):
        a,b=row(),row()
        b["cost_repricing"]["fee_legs"][1]["fee"]=10000.
        b["cost_repricing"]["commission"]=10001.
        left=simulate([a],self.symbols,self.margins,capital=120.)
        right=simulate([b],self.symbols,self.margins,capital=120.)
        self.assertTrue(left["decisions"][0]["accepted"])
        self.assertEqual(left["decisions"],right["decisions"])

    def test_unknown_economics_reserve_margin_and_prohibit_total_equity_claim(self):
        unknown=row()
        unknown["result"]["evaluation"].update(status="ambiguous",entry_ts=None,fill=None,net_amount=None,net_r=None)
        s=simulate([unknown],self.symbols,self.margins,capital=200.)
        self.assertEqual(s["accepted_unknown_count"],1)
        self.assertIsNone(s["total_account_final_equity"])
        self.assertEqual(s["accepted_priced"]["n"],0)
        self.assertGreater(s["maximum_observed_margin"],0.)

    def test_marks_cannot_use_current_bar_close_or_pre_entry_price_for_new_position(self):
        symbols=copy.deepcopy(self.symbols)
        symbols["RB0"]["bars"][1]["close"]=9999.
        p=Prices(symbols)
        self.assertEqual(p.prior("RB0","2026-10-09 09:05:00",101.,not_before="2026-10-09 09:05:00"),101.)
        self.assertEqual(p.prior("RB0","2026-10-09 09:10:00",101.,not_before="2026-10-09 09:05:00"),9999.)

    def test_capacity_warning_and_entry_reservations_are_distinct(self):
        a,b=row("a"),row("b")
        a["result"]["evaluation"].update(entry_ts="2026-10-09 09:20:00",exit_ts="2026-10-09 09:25:00")
        b["warning_ts"]="2026-10-09 09:05:00"
        warning=capacity([a,b],self.symbols,"warning")
        entry=capacity([a,b],self.symbols,"entry")
        self.assertEqual(warning["accepted"]["n"],1)
        self.assertEqual(entry["accepted"]["n"],2)

    def test_score_group_boundaries_are_fixed_not_selected_from_results(self):
        self.assertEqual([band(s) for s in (2.5,2.999,3.,3.499,3.5,3.999,4.)],
                         ["2.5-3","2.5-3","3-3.5","3-3.5","3.5-4","3.5-4","4+"])

    def test_higher_win_rate_does_not_imply_higher_expected_r(self):
        low,low2,high1,high2=row("a"),row("a2"),row("b"),row("c")
        low["result"]["evaluation"]["net_r"]=2.
        low2["result"]["evaluation"]["net_r"]=-1.
        high1["score"]=high2["score"]=3.5
        high1["result"]["evaluation"]["net_r"]=.01
        high2["result"]["evaluation"]["net_r"]=.01
        s=high_low([low,low2,high1,high2])
        self.assertGreater(s["delta_win_rate"],0.)
        self.assertLess(s["delta_mean_r"],0.)


if __name__=="__main__":
    unittest.main()
