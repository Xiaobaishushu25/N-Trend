import sys
from pathlib import Path
import unittest
import numpy as np
import pandas as pd
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from test_s3_alpha import fixture
import s3_signals as prep
import explore_v3_signals as v

SPEC=dict(tick=.1,multiplier=10)
FEES={k:dict(fixed_yuan_per_lot=1,notional_rate=0) for k in ('open','close_today')}


def base():
    raw,cal=fixture(2);f=v.features(prep.prepare(raw,cal),SPEC,FEES)
    f['economic_gate']=False;f.loc[20,'economic_gate']=True;f['activity_known']=True
    f['peer_median']=0.;f['peer_breadth']=1.;f['peer_count']=2;f['atr']=2.
    return f


class V3Tests(unittest.TestCase):
    def test_budget_and_universe(self):
        universe=sum(v.GROUPS.values(),[])
        self.assertEqual(len(set(universe)),22)
        cs=v.templates(universe);self.assertEqual(len(cs),24);self.assertEqual(len({c['candidate_id'] for c in cs}),24)

    def test_peer_self_exclusion_and_missing_not_forward_filled(self):
        f=base();f['norm_day_move']=1.
        frames={n:f.copy() for n in ('C','JD','SR')};frames['SR']['norm_day_move']=3.
        a=v.peers(frames)['C'];self.assertEqual(a.peer_median.iloc[20],2.)
        frames['C']['norm_day_move']=10000
        pd.testing.assert_series_equal(a.peer_median,v.peers(frames)['C'].peer_median)
        frames['JD']=frames['JD'].drop(index=20)
        z=v.peers(frames)['C'];self.assertTrue(np.isnan(z.peer_median.iloc[20]));self.assertEqual(z.peer_count.iloc[20],1)

    def test_all24_synchronous_prefix(self):
        raw,cal=fixture(16);frames={}
        for name,drift in [('C',0),('JD',.05),('SR',.06)]:
            q=raw.copy();move=np.arange(len(q))*drift
            for k in ('open','high','low','close'):q[k]+=move
            frames[name]=v.features(prep.prepare(q,cal),SPEC,FEES)
        cut=508;full=v.peers(frames);short=v.peers({n:f.iloc[:cut].copy() for n,f in frames.items()})
        observed=0
        for n in frames:
            pd.testing.assert_frame_equal(full[n].iloc[:cut],short[n])
            for c in v.templates(['C','JD','SR']):
                expected=v.signals(full[n],c,.1);actual=v.signals(short[n],c,.1)
                self.assertEqual([q for q in expected if q['index']<cut],actual);observed+=len(expected)
        self.assertGreater(observed,0)

    def test_peer_pullback_requires_context(self):
        f=base();f['peer_median']=2.;f['peer_breadth']=.8;f['lagged_move3']=-1.
        f.loc[19,'high']=105;f.loc[20,['open','high','low','close']]=[105,107,104,106]
        c=v.templates(['C'])[0];self.assertTrue(any(q['index']==20 for q in v.signals(f,c,.1)))
        f['peer_breadth']=.5;self.assertFalse(v.signals(f,c,.1))

    def test_close_location_pressure_break(self):
        f=base();f['pressure6']=.5;f['lagged_move6']=0.;f['box_high6']=105.;f['box_low6']=95.;f['activity_ratio']=2.
        f.loc[20,['open','high','low','close']]=[105,107,104,106]
        c=v.templates(['C'])[8];self.assertTrue(any(q['index']==20 for q in v.signals(f,c,.1)))
        f.loc[20,'activity_ratio']=.5;self.assertFalse(v.signals(f,c,.1))

    def test_quiet_context_rejection(self):
        f=base();f['peer_median']=.3;f['er12']=.1;f['range_high12']=110.;f['range_low12']=100.
        f.loc[19,'close']=110;f.loc[20,['open','high','low','close']]=[111,112,105,108]
        c=v.templates(['C'])[16];self.assertTrue(any(q['index']==20 for q in v.signals(f,c,.1)))
        f['peer_median']=2.;self.assertFalse(v.signals(f,c,.1))

    def test_calipers_and_no_control_reuse(self):
        key=('C',0,540,1)
        e=[dict(stratum=key,atr_pct_value=.001,activity_ratio=1.) for _ in range(3)]
        rows=[dict(time_key=('C',i),atr_pct_value=atr,activity_ratio=activity) for i,(atr,activity) in enumerate([(.001,1),(.001,.8),(.003,1),(.001,3)])]
        pool=dict(rows=rows,by_stratum={key:list(range(4))})
        z=v.matching(e,pool,10);self.assertTrue(np.array_equal(z,v.matching(e,pool,10)))
        for row in z:self.assertEqual(set(row[row>=0]),{0,1});self.assertEqual(int((row>=0).sum()),2)


if __name__=='__main__':unittest.main()
