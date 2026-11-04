import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import research_swing_expansion_20261104 as x


class IntegratedSwingTests(unittest.TestCase):
    def fixture(self):
        start=pd.Timestamp('2025-11-03 09:00',tz='Asia/Shanghai').value
        step=900_000_000_000
        tape=[]
        for i in range(3):
            r=dict(start_ns=start+i*step,end_ns=start+(i+1)*step,day='2025-11-03',contract='DCE.c2601',open=100.,high=105.,low=98.,close=102.,volume=100)
            tape.append((start+i*step,{'C':r}))
        signals={('DCE.c2601',15,'squeeze',16):{start:dict(side=1,stop=50.,atr=2.,signal_close=100.),start+step:dict(side=1,stop=85.,atr=2.,signal_close=100.)}}
        model=dict(tf=15,family='squeeze',n=16,risk=250,exit='trail')
        return tape,signals,model

    def test_rejected_trade_does_not_suppress_next_opportunity(self):
        tape,sig,m=self.fixture()
        trades,eq,limits=x.run(tape,sig,{}, {},m)
        self.assertEqual(limits['rejections']['risk_budget'],1)
        self.assertEqual(len(trades),1)
        self.assertEqual(trades[0]['entry_ns'],tape[1][0])
        self.assertAlmostEqual(eq.iloc[-1],sum(t['net'] for t in trades))

    def test_risk_budget_includes_stop_slippage_and_fees(self):
        tape,sig,m=self.fixture()
        # 240 yuan price risk alone fits 250; fees plus exit slippage do not.
        sig[('DCE.c2601',15,'squeeze',16)]={tape[0][0]:dict(side=1,stop=77.,atr=2.,signal_close=100.)}
        trades,_,limits=x.run(tape,sig,{}, {},m)
        self.assertEqual(trades,[])
        self.assertEqual(limits['rejections']['risk_budget'],1)

    def test_stop_wins_ambiguous_bar_against_target(self):
        tape,sig,m=self.fixture();m['exit']='target3'
        tape[1][1]['C'].update(high=200.,low=50.)
        trades,_,_=x.run(tape,sig,{}, {},m)
        self.assertEqual(len(trades),1)
        self.assertEqual(trades[0]['reason'],'stop')
        self.assertLess(trades[0]['net'],0)

    def test_future_ohlc_and_oi_cannot_change_earlier_features(self):
        rows=[];step=900_000_000_000
        for d,day in enumerate(pd.bdate_range('2025-08-01',periods=45)):
            start=pd.Timestamp(str(day.date())+' 09:00',tz='Asia/Shanghai').value
            for j in range(8):
                price=100+d+j/10
                rows.append(dict(start_ns=start+j*step,end_ns=start+(j+1)*step,day=str(day.date()),open=price,high=price+2,low=price-1,close=price+1,volume=100,oi=10000+d*100))
        z=pd.DataFrame(rows);changed=z.copy();cut=int(z.iloc[320].start_ns)
        changed.loc[changed.start_ns>=cut,['open','high','low','close','oi']]*=2
        for tf in [15,60]:
            a=x.feature_frame(z,tf);b=x.feature_frame(changed,tf)
            pd.testing.assert_frame_equal(a.loc[a.end_ns<=cut].reset_index(drop=True),b.loc[b.end_ns<=cut].reset_index(drop=True))


if __name__=='__main__':unittest.main()
