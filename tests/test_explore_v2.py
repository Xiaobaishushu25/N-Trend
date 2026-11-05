import sys
from pathlib import Path
import unittest
import numpy as np
import pandas as pd

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import explore_v2_signals as v
import s3_signals as prep
from test_s3_alpha import fixture


SPEC=dict(tick=.1,multiplier=10)
FEES={k:dict(fixed_yuan_per_lot=1,notional_rate=0) for k in ('open','close_today')}


class V2Tests(unittest.TestCase):
    def test_grid_budget(self):
        c=v.templates(['C','JD'])
        self.assertEqual(len(c),24);self.assertEqual(len({r['candidate_id'] for r in c}),24)
        self.assertEqual([sum(r['hypothesis_id']==f for r in c) for f in ('N1','N2','N3')],[8,8,8])

    def test_full_pipeline_prefix_all24(self):
        raw,cal=fixture(15);f=v.features(prep.prepare(raw,cal),SPEC,FEES);cut=508
        short=v.features(prep.prepare(raw.iloc[:cut],cal),SPEC,FEES)
        pd.testing.assert_frame_equal(f.iloc[:cut],short)
        n=0
        for c in v.templates(['C']):
            a=v.signals(f,c,.1);b=v.signals(short,c,.1)
            self.assertEqual([r for r in a if r['index']<cut],b,c['candidate_id']);n+=len(a)
        self.assertGreater(n,0)

    def test_cost_gate_hand_calculation(self):
        raw,cal=fixture(2);f=prep.prepare(raw,cal);f['atr']=1.
        a=v.features(f,SPEC,FEES)
        # fees2 + two ticks * .1 * mult10 =4 yuan; ATR lot value10 =>0.4R.
        self.assertAlmostEqual(a.friction_R.iloc[20],.4);self.assertFalse(a.economic_gate.iloc[20])
        f['atr']=2.;a=v.features(f,SPEC,FEES)
        self.assertAlmostEqual(a.friction_R.iloc[20],.2);self.assertTrue(a.economic_gate.iloc[20])

    def test_opening_break_never_uses_future_range(self):
        raw,cal=fixture(2);f=prep.prepare(raw,cal);f['atr']=2.
        start=45
        for i in range(start,start+6):f.loc[i,['open','high','low','close']]=[100,110,100,108]
        f.loc[start+6,['open','high','low','close']]=[109,112,108,111]
        a=v.features(f,SPEC,FEES);a['economic_gate']=True;a['activity_known']=True
        c=v.templates(['C'])[0];events=v.signals(a,c,.1)
        self.assertTrue(any(r['index']==start+6 and r['side']==1 for r in events))
        changed=a.copy();changed.loc[start+7:,'high']=10000
        self.assertEqual([r for r in events if r['index']<=start+6],[r for r in v.signals(changed,c,.1) if r['index']<=start+6])

    def test_failed_break_reversal_and_two_sided_rejection(self):
        raw,cal=fixture(2);a=v.features(prep.prepare(raw,cal),SPEC,FEES)
        a['economic_gate']=False;a.loc[20,'economic_gate']=True;a['activity_known']=True;a['atr_ticks']=20.;a['atr']=2.
        a['range_high12']=110.;a['range_low12']=100.
        a.loc[20,['open','high','low','close']]=[111,112,105,108];a.loc[19,'close']=110
        c=v.templates(['C'])[8];events=v.signals(a,c,.1)
        self.assertTrue(any(r['index']==20 and r['side']==-1 for r in events))
        a.loc[20,'low']=98
        self.assertFalse(any(r['index']==20 for r in v.signals(a,c,.1)))

    def test_vwap_reversal_uses_previous_stretch(self):
        raw,cal=fixture(2);a=v.features(prep.prepare(raw,cal),SPEC,FEES)
        a['economic_gate']=False;a.loc[20,'economic_gate']=True;a['activity_known']=True;a['atr_ticks']=20.;a['atr']=2.;a['er12']=0.
        a['vwap_proxy']=100.;a.loc[19,'close']=105
        a.loc[20,['open','high','low','close']]=[105,106,102,103]
        c=v.templates(['C'])[16];events=v.signals(a,c,.1)
        self.assertTrue(any(r['index']==20 and r['side']==-1 for r in events))
        a.loc[19,'er12']=.8
        self.assertFalse(any(r['index']==20 for r in v.signals(a,c,.1)))


if __name__=='__main__':unittest.main()
