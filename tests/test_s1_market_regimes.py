import datetime as dt
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
from pandas.testing import assert_frame_equal

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import s1_market_regimes as s
from factor_study_data import Calendar


def frame(n=100,start='2025-01-02 09:00',contract='DCE.c2505'):
    t=pd.Timestamp(start,tz='Asia/Shanghai').value+np.arange(n)*s.BAR
    close=100+np.arange(n)*.1
    return pd.DataFrame(dict(contract=contract,start_ns=t,end_ns=t+s.BAR,
        open=close-.05,high=close+.2,low=close-.2,close=close,volume=np.arange(n)+10,
        constituent_count=5,complete=1))


class S1Tests(unittest.TestCase):
    def test_bar_prefix_is_unchanged_by_future_mutation(self):
        f=frame();a=s.bar_features(f)
        for n in (1,12,13,30,80):
            assert_frame_equal(a.iloc[:n],s.bar_features(f.iloc[:n]),check_dtype=False)
        changed=f.copy();changed.loc[60:,['open','high','low','close']]*=50
        assert_frame_equal(a.iloc[:60],s.bar_features(changed).iloc[:60])

    def test_hand_er_atr_and_constant_prices(self):
        f=frame(30);z=s.bar_features(f)
        self.assertTrue(z.er.iloc[:12].isna().all())
        self.assertAlmostEqual(z.er.iloc[12],1)
        self.assertAlmostEqual(z.atr_pct.iloc[12],.4/f.close.iloc[12])
        for c in ('open','high','low','close'):f[c]=100.
        self.assertEqual(s.bar_features(f).er.iloc[12],0)

    def test_roll_gap_invalid_reset_history(self):
        for mode in ('roll','gap','partial'):
            f=frame(60)
            if mode=='roll':f.loc[30:,'contract']='DCE.c2509'
            elif mode=='gap':f.loc[30:,['start_ns','end_ns']]+=s.BAR
            else:f.loc[30,'complete']=0
            z=s.bar_features(f)
            self.assertTrue(z.er.iloc[30:42].isna().all(),mode)
            if mode=='partial':self.assertFalse(z.valid_bar.iloc[30])

    def test_future_mapping_never_changes_prior_selection(self):
        f=frame(3)
        m=pd.DataFrame([dict(effective_date='2025-01-01',contract='DCE.c2505')])
        a=s.select_main(f,m)
        future=pd.DataFrame([dict(effective_date='2025-01-02',contract='DCE.c2509')])
        assert_frame_equal(a,s.select_main(f,pd.concat([m,future])))
        self.assertEqual(len(a),3)

    def test_overlapping_contracts_select_only_one(self):
        f=frame(3);other=f.copy();other['contract']='DCE.c2509'
        m=pd.DataFrame([dict(effective_date='2025-01-01',contract='DCE.c2505')])
        self.assertEqual(len(s.select_main(pd.concat([f,other]),m)),3)

    def test_thresholds_frozen_before_classification(self):
        f=s.bar_features(frame());freeze=int(f.asof_ns.iloc[50])
        t=s.freeze_thresholds(f,freeze,minimum=10)
        changed=f.copy();changed.loc[50:,'er']=999;changed.loc[50:,'atr_pct']=999
        self.assertEqual(t,s.freeze_thresholds(changed,freeze,minimum=10))
        z=s.classify(f,t)
        self.assertFalse(z.iloc[:50].market_state.isin(['directional','range','high_volatility']).any())
        self.assertLess(t['maximum_source_asof_ns'],t['available_at_ns'])

    def test_unknown_calendar_does_not_infer_weekdays(self):
        cal=Calendar(dict(records=[dict(date='2025-01-02',trading=True),dict(date='2025-01-03',trading=True)]),{'C0':None})
        z=s.label_calendar(s.bar_features(frame(3,start='2024-01-02 09:00')),cal,'C0')
        self.assertEqual(z.trading_day.tolist(),['']*3)
        self.assertEqual(set(z.calendar_status),{'calendar_unknown'})

    def test_completed_day_context_excludes_current_day_totals(self):
        days=[dt.date(2025,1,1)+dt.timedelta(days=i) for i in range(65)]
        cal=Calendar(dict(records=[dict(date=d.isoformat(),trading=d.weekday()<5) for d in days]),{'C0':None})
        chunks=[]
        for day in cal.trading[1:30]:
            starts=s.expected_day_starts(cal,day,'C0')
            q=frame(len(starts));q.start_ns=starts;q.end_ns=q.start_ns+s.BAR
            chunks.append(q)
        f=s.label_calendar(s.bar_features(pd.concat(chunks,ignore_index=True)),cal,'C0')
        full,d=s.daily_context(f,cal,'C0')
        cut=len(f)-20
        prefix,_=s.daily_context(f.iloc[:cut],cal,'C0')
        assert_frame_equal(full.iloc[:cut],prefix,check_dtype=False)
        self.assertTrue(full.past20_return.notna().any())
        self.assertTrue((d.day_complete).all())
        self.assertAlmostEqual(full.loc[full.time_slot=='09:00','volume_relative_past20_same_slot'].dropna().iloc[-1],1.)

    def test_sr_uses_published_exchange_schedule(self):
        snap=dict(products={'SR':dict(published_schedule_before_override={'open':{'fixed_yuan_per_lot':2}},roundtrip_fixed_yuan_per_lot=999)})
        self.assertEqual(s.fee_rule(snap,'SR'),{'open':{'fixed_yuan_per_lot':2}})


if __name__=='__main__':unittest.main()
