import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import research_swing_year_20261104 as s


class SwingExecutionTests(unittest.TestCase):
    def fixture(self,gap=108):
        base=pd.Timestamp('2025-11-03 09:00',tz='Asia/Shanghai').value
        rows=[]
        for i,(op,hi,lo,cl) in enumerate([(100,103,99,102),(103,111,101,110),(gap,max(gap+1,109),gap-1,gap)]):
            rows.append(dict(start_ns=base+i*900_000_000_000,end_ns=base+(i+1)*900_000_000_000,day='2025-11-03',contract='DCE.c2601',open=op,high=hi,low=lo,close=cl,volume=100,atr=2.,e64=100.,x64=101.,h64=101. if i==0 else 10000.,l64=0.,lo64=0.,hi64=10000.))
        z=pd.DataFrame(rows)
        return {'C':z},{'DCE.c2601':z.set_index('start_ns',drop=False)}

    def test_new_trailing_stop_cannot_hit_earlier_in_same_bar(self):
        f,r=self.fixture()
        ts,_,_=s.simulate(f,r,dict(family='breakout',n=64,k=2))
        self.assertEqual(len(ts),1)
        self.assertEqual(ts[0]['reason'],'end')
        self.assertEqual(ts[0]['exit_ns'],int(f['C'].iloc[-1].end_ns))

    def test_gap_stop_fills_at_worse_open_plus_slippage(self):
        f,r=self.fixture(gap=95)
        ts,_,_=s.simulate(f,r,dict(family='breakout',n=64,k=2))
        self.assertEqual(ts[0]['reason'],'stop')
        self.assertEqual(ts[0]['entry_price'],104)
        self.assertEqual(ts[0]['exit_price'],94)
        acc,eq,_=s.portfolio(ts,risk=250)
        self.assertAlmostEqual(eq.iloc[-1],sum(t['net']*t['lots'] for t in acc))

    def test_integer_lot_risk_rejects_unaffordable_trade(self):
        f,r=self.fixture()
        ts,_,_=s.simulate(f,r,dict(family='breakout',n=64,k=2))
        acc,_,limits=s.portfolio(ts,risk=20)
        self.assertEqual(acc,[])
        self.assertEqual(limits['skipped']['lot_risk_or_margin'],1)

    def test_roll_closes_old_contract_without_booking_new_contract_gap(self):
        f,r=self.fixture(gap=95)
        z=f['C'].copy()
        z.loc[2,['contract','open','high','low','close']]=['DCE.c2605',500,501,499,500]
        ts,_,_=s.simulate({'C':z},r,dict(family='breakout',n=64,k=2))
        self.assertEqual(len(ts),1)
        self.assertEqual(ts[0]['reason'],'roll')
        self.assertEqual(ts[0]['exit_price'],94)
        self.assertLess(ts[0]['net'],0)

    def test_idle_month_has_zero_profit(self):
        old=s.CALENDAR;s.CALENDAR=[]
        try:
            ix=[pd.Timestamp(t,tz='Asia/Shanghai').value for t in ['2026-01-02','2026-04-02']]
            m=s.metrics(pd.Series([-100.,-50.],index=ix),[],'2026-01-01','2026-05-01')
            self.assertEqual(m['monthly'],{'2026-01':-100.,'2026-02':0.,'2026-03':0.,'2026-04':50.})
            self.assertEqual(sum(m['monthly'].values()),m['net'])
        finally:s.CALENDAR=old


if __name__=='__main__':unittest.main()
