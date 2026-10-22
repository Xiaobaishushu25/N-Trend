"""Independent checks for indicator causality, fill ordering and ATR sizing."""
import importlib.util
import sys
import unittest
from dataclasses import replace
from datetime import datetime,timedelta
from pathlib import Path

path=Path(__file__).resolve().parents[1]/"scripts/backtest_sr_donchian_atr.py"
spec=importlib.util.spec_from_file_location("sr_donchian_atr",path)
bt=importlib.util.module_from_spec(spec);sys.modules[spec.name]=bt;spec.loader.exec_module(bt)


def bar(o,h,l,c,v=100):
    return bt.Bar(datetime(2026,9,21,9),o,h,l,c,v)


class BaselineTests(unittest.TestCase):
    def setUp(self):
        self.cfg=replace(bt.Config(),multiplier=10,initial_capital=100000)
        self.levels=dict(n=5.,upper=110.,lower=90.,exit_long=80.,exit_short=120.)
        self.empty=dict(cash=100000.,position=None,trades=[])

    def test_n_unit_is_one_percent_per_n_and_two_percent_at_two_n_stop(self):
        lots,desired=bt.size(100000,50,5000,self.cfg)
        self.assertEqual((lots,desired),(2,2))
        self.assertEqual(lots*50*10,1000)
        self.assertEqual(lots*2*50*10,2000)

    def test_margin_cap_and_no_forced_minimum_one_lot(self):
        self.assertEqual(bt.size(100000,1,5000,self.cfg),(8,100))
        self.assertEqual(bt.size(1000,500,5000,self.cfg)[0],0)

    def test_prior_only_channels_and_wilder_recurrence(self):
        cfg=replace(self.cfg,entry=2,exit=1,atr=2)
        bs=[bt.Bar(datetime(2026,9,1)+timedelta(days=i),100,102,98,100) for i in range(4)]
        snapshots=bt.indicator_snapshots(bs,cfg)
        self.assertIsNone(snapshots[1]);self.assertEqual(snapshots[2]["n"],4)
        self.assertEqual(snapshots[2]["upper"],103)
        bs[2]=replace(bs[2],h=110)
        changed=bt.indicator_snapshots(bs,cfg)
        self.assertEqual(changed[2],snapshots[2])
        self.assertEqual(changed[3]["n"],8) # (old N4 + TR12)/2
        self.assertEqual(changed,bt.indicator_snapshots(bs+[replace(bs[-1],dt=bs[-1].dt+timedelta(days=1),h=1000)],cfg)[:4])

    def test_long_gap_entry_uses_open_and_costs_reconcile(self):
        s=bt.minute_path(self.empty,bar(115,118,114,117),self.levels,self.cfg,True)
        self.assertEqual((s["position"]["entry_base"],s["position"]["entry_fill"]),(115,116))
        p=s["position"];lots=p["lots"]
        bt.close_position(s,120,datetime(2026,9,21,10),"test",self.cfg)
        t=s["trades"][0]
        self.assertEqual(t["net_pnl"],(120-115)*lots*10-2*lots*10-6*lots)
        self.assertAlmostEqual(s["cash"]-100000,t["net_pnl"])

    def test_new_long_can_stop_out_within_same_minute(self):
        s=bt.minute_path(self.empty,bar(100,115,95,105),self.levels,self.cfg,True)
        self.assertIsNone(s["position"])
        self.assertEqual(len(s["trades"]),1)
        self.assertEqual(s["trades"][0]["exit_base"],101)
        self.assertEqual(s["trades"][0]["exit_reason"],"atr_stop")

    def test_two_ohlc_paths_expose_ambiguous_minute(self):
        a=bt.minute_path(self.empty,bar(100,115,95,105),self.levels,self.cfg,True)
        z=bt.minute_path(self.empty,bar(100,115,95,105),self.levels,self.cfg,False)
        self.assertNotEqual(bt.outcome_signature(a),bt.outcome_signature(z))
        self.assertIsNotNone(z["position"])

    def test_short_and_adverse_gap_exit(self):
        s=bt.minute_path(self.empty,bar(85,87,82,84),self.levels,self.cfg,False)
        self.assertEqual(s["position"]["direction"],-1)
        z=bt.minute_path(s,bar(110,115,108,112),self.levels,self.cfg,True)
        self.assertIsNone(z["position"])
        self.assertEqual((z["trades"][0]["exit_base"],z["trades"][0]["exit_fill"]),(110,111))

    def test_zero_volume_cannot_fill(self):
        s=bt.minute_path(self.empty,bar(115,115,115,115,0),self.levels,self.cfg,True)
        self.assertIsNone(s["position"])

    def test_channel_exit_can_be_tighter_than_initial_stop(self):
        s=bt.minute_path(self.empty,bar(115,118,114,117),self.levels,self.cfg,True)
        levels=dict(self.levels,exit_long=113)
        z=bt.minute_path(s,bar(117,118,110,112),levels,self.cfg,True)
        self.assertEqual((z["trades"][0]["exit_base"],z["trades"][0]["exit_reason"]),(113,"channel_exit"))

    def test_night_weekend_and_holiday_trading_dates(self):
        self.assertEqual(str(bt.trading_date(datetime(2026,9,18,21))),"2026-09-21")
        self.assertEqual(len(bt.expected_minutes(bt.START)),345)
        self.assertEqual(len(bt.expected_minutes(bt.date(2026,10,8))),225)


if __name__=="__main__":unittest.main()
