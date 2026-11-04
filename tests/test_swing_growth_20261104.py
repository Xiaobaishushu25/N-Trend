import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import research_swing_growth_20261104 as g


class GrowthAccountTests(unittest.TestCase):
    def fixture(self):
        start=pd.Timestamp('2025-11-03 09:00',tz='Asia/Shanghai').value;step=900_000_000_000
        tape=[]
        for i in range(3):
            r=dict(start_ns=start+i*step,end_ns=start+(i+1)*step,day='2025-11-03',contract='DCE.c2601',open=100.,high=105.,low=98.,close=102.,volume=100)
            tape.append((start+i*step,{'C':r}))
        sig=dict(side=1,stop=85.,atr=2.,signal_close=100.,stream='A',tf=15,eff=.4,strength=1.,contract='DCE.c2601')
        signals={('DCE.c2601','A'):{start:sig}}
        cfg=dict(next(c for c in g.configurations() if c['id']=='A_scale_750_m60'))
        return tape,signals,cfg

    def test_multilot_cash_equals_independent_fill_ledger(self):
        tape,sig,cfg=self.fixture()
        ts,eq,lim=g.run(tape,sig,{}, {},cfg)
        self.assertEqual(len(ts),1);self.assertGreater(ts[0]['lots'],1)
        row=ts[0];fills=row['fills'];mult=10
        cash=sum((-1 if f['kind'] in ['open','add'] else 1)*f['price']*mult*f['lots']-f['fee'] for f in fills)
        self.assertAlmostEqual(cash,eq.iloc[-1]);self.assertAlmostEqual(row['net'],cash)

    def test_pyramid_occurs_after_closed_profit_not_same_bar(self):
        tape,sig,cfg=self.fixture();cfg.update(pyramid=True,max_lots=1,trade_budget=250)
        tape[0][1]['C'].update(high=131.,low=98.,close=130.)
        for _,batch in tape[1:]:batch['C'].update(open=131.,high=134.,low=130.,close=132.)
        ts,eq,lim=g.run(tape,sig,{}, {},cfg)
        self.assertEqual(lim['pyramid_adds'],1)
        adds=[f for f in ts[0]['fills'] if f['kind']=='add']
        self.assertEqual(len(adds),1);self.assertEqual(adds[0]['ns'],tape[1][0])
        self.assertEqual(ts[0]['reason'],'end')
        self.assertAlmostEqual(sum(t['net'] for t in ts),eq.iloc[-1])

    def test_losing_position_cannot_pyramid(self):
        tape,sig,cfg=self.fixture();cfg.update(pyramid=True,max_lots=1,trade_budget=250)
        for _,batch in tape:batch['C'].update(close=95.,low=94.)
        ts,eq,lim=g.run(tape,sig,{}, {},cfg)
        self.assertEqual(lim['pyramid_adds'],0)
        self.assertEqual(ts[0]['lots'],1)

    def test_twenty_percent_margin_can_bind_before_risk_budget(self):
        tape,sig,cfg=self.fixture();cfg.update(margin_cap=.01,trade_budget=750)
        low=g.run(tape,sig,{}, {},cfg,margin=.15)[0]
        high=g.run(tape,sig,{}, {},cfg,margin=.2)[0]
        self.assertGreater(low[0]['lots'],high[0]['lots'])


if __name__=='__main__':unittest.main()
