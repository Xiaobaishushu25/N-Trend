"""Historical contract routing, holiday boundaries and continuous replay checks."""
import json
import sys
import unittest
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"scripts"))
import backtest_sr_dual_thrust_history as h


def daily(at, volume, oi):
    return h.base.Bar(datetime.fromisoformat(at), 100, 110, 90, 100, volume, oi)


class CalendarAndRoutingTests(unittest.TestCase):
    def test_dragon_boat_holiday_excludes_closed_day_and_preholiday_night(self):
        self.assertFalse(h.base.is_trading_day(date(2026,6,19)))
        self.assertEqual(h.base.previous_trading_day(date(2026,6,22)), date(2026,6,18))
        expected = h.base.expected_minutes(date(2026,6,22))
        self.assertEqual(len(expected), 225)
        self.assertTrue(all(t.date() == date(2026,6,22) and t.hour < 21 for t in expected))
        self.assertEqual(h.base.trading_date(datetime(2026,9,18,21)), date(2026,9,21))

    def test_route_waits_until_previous_day_volume_and_oi_agree(self):
        dates = [date(2026,7,d) for d in [1,2,3]]
        data = {
            "SR609": {date(2026,6,30):daily("2026-06-30 08:00:00",100,100),
                      date(2026,7,1):daily("2026-07-01 08:00:00",200,80),
                      date(2026,7,2):daily("2026-07-02 08:00:00",50,50)},
            "SR701": {date(2026,6,30):daily("2026-06-30 08:00:00",90,90),
                      date(2026,7,1):daily("2026-07-01 08:00:00",100,120),
                      date(2026,7,2):daily("2026-07-02 08:00:00",300,300)}}
        schedule = h.select_contracts(dates,data)
        self.assertEqual([r["contract"] for r in schedule], ["SR609","SR609","SR701"])
        self.assertEqual([r["known_at_date"] for r in schedule], ["2026-06-30","2026-07-01","2026-07-02"])
        # Change the third scored day's inputs and future daily snapshots.
        # The first two choices cannot observe those changes.
        altered = {c:dict(ds) for c,ds in data.items()}
        altered["SR609"][date(2026,7,2)] = replace(data["SR609"][date(2026,7,2)], v=10000, oi=10000)
        self.assertEqual(h.select_contracts(dates,altered)[:2],schedule[:2])
        self.assertEqual(h.select_contracts(dates,altered)[2]["contract"],"SR609")

    def test_subwindow_drawdown_uses_its_own_starting_cash(self):
        trades = [dict(trading_date="2026-07-01",net_pnl=100,gross_pnl=126,slippage_cost=20,fees=6),
                  dict(trading_date="2026-07-02",net_pnl=-25,gross_pnl=1,slippage_cost=20,fees=6)]
        eq = [dict(trading_date="2026-07-01",equity=100,realized_cash=100),
              dict(trading_date="2026-07-02",equity=70,realized_cash=100),
              dict(trading_date="2026-07-02",equity=75,realized_cash=75)]
        stats = h.window((None,trades,eq,None,None),date(2026,7,2),date(2026,7,2))
        self.assertEqual(stats["net_pnl"],-25)
        self.assertEqual(stats["minute_close_max_drawdown"],30)


class FrozenHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (h.OUT/"sources.json").exists():
            raise unittest.SkipTest("No frozen historical CSV fixtures")
        cls.minutes, cls.schedule = h.prepare()

    def test_all_days_are_complete_and_roll_uses_prior_completed_information(self):
        expected = h.trading_days(h.START,h.base.END)
        self.assertEqual(len(expected),81)
        self.assertEqual(len([d for d in expected if d <= h.END]),65)
        self.assertEqual(len(self.schedule),len(expected))
        for item in self.schedule:
            d = date.fromisoformat(item["trading_date"])
            self.assertLess(date.fromisoformat(item["known_at_date"]),d)
            self.assertEqual(item["contract"],"SR609" if d < date(2026,8,13) else "SR701")
            self.assertTrue(item["matches_supplier_main_daily_hlc_oi"])

    def test_fixed_contract_daily_replays_equal_one_continuous_replay(self):
        merged = h.replay_schedule(self.minutes,self.schedule,fixed_contract="SR701")
        continuous = h.a.replay(self.minutes["SR701"],h.CFG,start=h.START,end=h.base.END)
        for k,v in merged[0].items():
            self.assertEqual(v,continuous[0][k],k)
        self.assertEqual([{k:v for k,v in t.items() if k != "trading_date"} for t in merged[1]],continuous[1])
        for left,right in zip(merged[2],continuous[2]):
            self.assertEqual({k:left[k] for k in right},right)
        self.assertEqual(len(merged[2]),len(continuous[2]))

    def test_rolled_day_replays_equal_separate_continuous_contract_segments(self):
        merged = h.replay_schedule(self.minutes,self.schedule)
        left = h.a.replay(self.minutes["SR609"],h.CFG,start=h.START,end=date(2026,8,12),contract="SR609")
        right = h.a.replay(self.minutes["SR701"],h.CFG,start=date(2026,8,13),end=h.base.END,contract="SR701")
        self.assertEqual(merged[0]["net_pnl"],left[0]["net_pnl"]+right[0]["net_pnl"])
        self.assertEqual([{k:v for k,v in t.items() if k != "trading_date"} for t in merged[1]],left[1]+right[1])
        combined_eq = left[2]+[{**r,"realized_cash":r["realized_cash"]+left[0]["net_pnl"],
                                  "equity":r["equity"]+left[0]["net_pnl"]} for r in right[2]]
        self.assertEqual(h.base.summarize(left[1]+right[1],combined_eq),merged[0])

    def test_three_month_boundaries_exclude_original_scoring_night(self):
        expected = h.trading_days(h.START,h.END)
        earlier = {t for d in expected for t in h.base.expected_minutes(d)}
        original = {t for d in h.trading_days(h.base.START,h.base.END) for t in h.base.expected_minutes(d)}
        self.assertEqual(len(earlier),22305)
        self.assertEqual(len(original),5280)
        self.assertFalse(earlier & original)
        self.assertNotIn(datetime(2026,9,18,21),earlier)
        self.assertIn(datetime(2026,9,18,21),original)


if __name__ == "__main__":
    unittest.main()
