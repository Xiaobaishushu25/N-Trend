import copy
import datetime as dt
from pathlib import Path
import sys
import unittest
import numpy as np
import pandas as pd

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import s3_signals as s
import s3_alpha as a
import s2_preregister as p
from factor_study_data import Calendar


def fixture(days=10):
    dates=pd.bdate_range('2025-01-02',periods=days);rows=[];rng=np.random.default_rng(31);price=100.
    for date in dates:
        for m in s.SLOTS:
            t=pd.Timestamp(date.date().isoformat(),tz='Asia/Shanghai').value+int(m)*60*s.NS
            op=price;price=max(50,price+rng.normal(0,.7))
            rows.append(dict(contract='DCE.c2505',start_ns=t,end_ns=t+s.STEP,open=op,high=max(op,price)+.5,
                             low=min(op,price)-.5,close=price,volume=int(rng.integers(10,100)),complete=1,constituent_count=5))
    records=[dict(date=(dt.date(2024,12,1)+dt.timedelta(days=i)).isoformat(),trading=(dt.date(2024,12,1)+dt.timedelta(days=i)).weekday()<5) for i in range(120)]
    return pd.DataFrame(rows),Calendar(dict(records=records),{'C0':None})


def custom(f,rows30=None):
    h={m:s.aggregate(f,m) for m in (5,15,30,60)}
    if rows30 is not None:h[30]=pd.DataFrame(rows30)
    return h


class S3Tests(unittest.TestCase):
    def test_complete_buckets_only(self):
        raw,cal=fixture(1);f=s.prepare(raw,cal)
        self.assertEqual(len(s.aggregate(f,30)),7)
        self.assertEqual(len(s.aggregate(f,60)),2)
        self.assertEqual(len(s.aggregate(f,15)),15)
        self.assertEqual(len(s.aggregate(s.prepare(raw.iloc[:2],cal),15)),0)

    def test_actual_pipeline_prefix_all_24(self):
        raw,cal=fixture(12);full=s.prepare(raw,cal);cut=430;prefix=s.prepare(raw.iloc[:cut],cal)
        pd.testing.assert_frame_equal(full.iloc[:cut],prefix)
        hs={m:s.aggregate(full,m) for m in (5,15,30,60)};hp={m:s.aggregate(prefix,m) for m in (5,15,30,60)}
        observed=0
        for c in p.candidates(p.hypotheses(),['C']):
            e,b=s.signals(full,c,1,hs);x,y=s.signals(prefix,c,1,hp)
            self.assertEqual([q for q in e if q['index']<cut],x,c['candidate_id'])
            self.assertEqual([q for q in b if q['index']<cut],y,c['candidate_id']);observed+=len(e)
        self.assertGreater(observed,0)

    def test_h1_counter_bar_cannot_confirm_itself(self):
        raw,cal=fixture(1);f=s.prepare(raw,cal);f['atr']=2.
        f.loc[13,['open','high','low','close']]=[109,109.5,107,108]
        f.loc[14,['open','high','low','close']]=[108,111,108,110]
        h=custom(f);h[30]=h[30].iloc[:0].copy()
        h[30]=pd.DataFrame([dict(end_i=12,end_ns=int(f.end_ns.iloc[12]),open=100,close=109,high=110,low=100,atr_prior=4)])
        c=p.candidates(p.hypotheses(),['C'])[1];events,_=s.signals(f,c,1,h)
        self.assertEqual(events[0]['index'],14);self.assertEqual(events[0]['stop'],106)

    def test_h2_keeps_failed_first_breakouts(self):
        raw,cal=fixture(1);f=s.prepare(raw,cal);f['atr']=2.;f['volume_prior20']=10.
        f.loc[:24,'close']=105.;f.loc[25,['open','high','low','close','volume']]=[109,112,108,111,100]
        f.loc[26,['open','high','low','close']]=[111,113,110,112]
        h=custom(f);hist=[]
        for i in (5,11,20,23):hist.append(dict(end_i=i,end_ns=int(f.end_ns.iloc[i]),segment=1,open=104,close=105,high=110,low=100))
        h[30]=pd.DataFrame(hist)
        c=p.candidates(p.hypotheses(),['C'])[8];events,br=s.signals(f,c,1,h)
        self.assertTrue(any(q['index']==25 for q in br));self.assertTrue(any(q['index']==26 for q in events))
        f.loc[26,'low']=90
        events,br=s.signals(f,c,1,h)
        self.assertTrue(any(q['index']==25 for q in br));self.assertFalse(any(q['index']==26 for q in events))

    def test_h3_requires_completed_aligned_trend(self):
        raw,cal=fixture(1);f=s.prepare(raw,cal);f['atr']=2.
        h=custom(f);h[5]=pd.DataFrame([dict(end_i=20,end_ns=int(f.end_ns.iloc[20]),open=100,close=105,high=106,low=99,tr=7,median_prior20=2,mean_prior5=1)])
        h[30]=pd.DataFrame([dict(end_i=11,end_ns=int(f.end_ns.iloc[11]),segment=1,open=100,close=101,atr_before_two=2),
                            dict(end_i=20,end_ns=int(f.end_ns.iloc[20]),segment=1,open=101,close=105,atr_before_two=2)])
        c=p.candidates(p.hypotheses(),['C'])[16];events,_=s.signals(f,c,1,h)
        self.assertEqual(events[0]['index'],20)
        h[30].loc[1,'end_ns']+=s.STEP
        events,_=s.signals(f,c,1,h);self.assertEqual(events,[])

    def test_label_next_bar_hand_net_and_purge(self):
        raw,cal=fixture(2);f=s.prepare(raw,cal);f['atr']=2.
        arr={c:f[c].to_numpy() for c in f};spec=dict(tick=1,multiplier=10)
        fees={'open':dict(fixed_yuan_per_lot=2,notional_rate=0),'close_today':dict(fixed_yuan_per_lot=3,notional_rate=0)}
        z=a.label(arr,12,1,spec,fees,2**63-1,stop=90)
        self.assertEqual(z['entry_ns'],int(f.start_ns.iloc[13]))
        entry=f.open.iloc[13]+1;exit=f.close.iloc[24]-1
        self.assertAlmostEqual(z['net_R_12'],(exit-entry)/2-5/20)
        q=a.label(arr,12,1,spec,fees,int(f.end_ns.iloc[24]))
        self.assertEqual(q['status_12'],'purged_boundary')

    def test_censor_and_gap_never_fill_zero(self):
        raw,cal=fixture(2);f=s.prepare(raw,cal);f['atr']=2.
        spec=dict(tick=1,multiplier=10);fees={k:dict(fixed_yuan_per_lot=0,notional_rate=0) for k in ('open','close_today')}
        z=a.label({c:f[c].to_numpy() for c in f},40,1,spec,fees,2**63-1)
        self.assertNotEqual(z['status_12'],'complete');self.assertIsNone(z['net_R_12'])
        raw=raw.drop(index=15);f=s.prepare(raw,cal);f['atr']=2.
        z=a.label({c:f[c].to_numpy() for c in f},12,1,spec,fees,2**63-1)
        self.assertNotEqual(z['status_6'],'complete')

    def test_stop_first_bounds_and_gap_open_order(self):
        self.assertEqual(a.first_passage([100],[103],[97],100,2,1,1)[:3],(0,1,True))
        self.assertEqual(a.first_passage([103],[104],[97],100,2,1,1)[:3],(1,1,False))
        self.assertEqual(a.first_passage([97],[104],[96],100,2,1,1)[:3],(0,0,False))
        self.assertEqual(a.first_passage([100],[101],[99],100,2,1,1)[:2],(0,0))

    def test_exact_match_no_reuse_and_reproducible(self):
        events=[dict(stratum=('C','2025-01',540,1,1)) for _ in range(3)]
        pool=dict(rows=[dict(time_key=('C',i)) for i in range(2)],by_stratum={events[0]['stratum']:[0,1]})
        z=a.matching(events,pool,5);self.assertTrue(np.array_equal(z,a.matching(events,pool,5)))
        self.assertTrue(all(len(set(r[r>=0]))==2 for r in z));self.assertTrue((z<0).any())

    def test_holm_and_joint_day_bootstrap(self):
        self.assertTrue(np.allclose(a.holm([.01,.04,.03]),[.03,.06,.06]))
        events=[dict(day=f'2025-01-{i+1:02}',product='C',net_R_12=1.,discovery_block=i//10) for i in range(30)]
        controls=[dict(day=e['day'],net_R_12=0.) for e in events]
        match=np.tile(np.arange(30),(10,1))
        result,boot=a.inference(events,controls,match,100)
        self.assertAlmostEqual(result['effect'],1.)
        self.assertAlmostEqual(result['ci_lower'],1.)
        self.assertEqual(result['positive_blocks'],3)
        self.assertEqual(result['matched_fraction'],1.)


if __name__=='__main__':unittest.main()
