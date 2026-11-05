"""Causality and invariants for daily-context research, before candidate results."""
import importlib.util
import sys
import unittest
from pathlib import Path
import numpy as np
import pandas as pd

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
import daily_context_wfo as w
import simple_wfo as core


def synthetic():
    rows=[]
    for d,day in enumerate(pd.bdate_range('2024-01-02',periods=22)):
        for m in core.SLOTS:
            p=100+d+(m-540)/1000
            rows.append(dict(day=str(day.date()),month=str(day.to_period('M')),minute=m,contract='DCE.c2405',start_ns=core.ns(str(day.date()))+m*60_000_000_000,
                end_ns=core.ns(str(day.date()))+m*60_000_000_000+core.STEP,open=p,high=p+2,low=p-2,close=p+1,volume=100+d,valid=True))
    return pd.DataFrame(rows)


class CausalContextTests(unittest.TestCase):
    def test_cash_zero_and_negative_candidates_not_forced(self):
        self.assertEqual(w.utility(0,0,0,0,0,0),0.)
        self.assertLess(w.utility(-100,-100,-10,100,50,20),0.)

    def test_risk_and_cost_reduce_utility(self):
        baseline=w.utility(600,600,100,0,0,60)
        self.assertLess(w.utility(600,600,100,100,0,60),baseline)
        self.assertLess(w.utility(600,600,100,0,100,60),baseline)
        self.assertLess(w.utility(600,600,100,0,0,10),baseline)

    def test_candidate_count_and_budget_fixed(self):
        self.assertEqual(len(w.FEATURES)*len(w.LOOKBACK)*3*2*len(w.HOLD)*len(w.STOP),648)
        self.assertEqual(8*20*648,103680)

    def test_prefix_recompute_current_day_without_future_bars(self):
        f=synthetic();full,_=w.context_frame(f)
        cut=f.loc[(f.day=='2024-01-25')&(f.minute==545)].index[0]
        prefix,_=w.context_frame(f.iloc[:cut+1])
        for feat in w.FEATURES:
            for lb in w.LOOKBACK:
                np.testing.assert_allclose(prefix[f'{feat}_{lb}'],full[f'{feat}_{lb}'].iloc[:cut+1],equal_nan=True)
        np.testing.assert_allclose(prefix.atr,full.atr.iloc[:cut+1],equal_nan=True)

    def test_today_extrema_close_volume_do_not_change_signals(self):
        f=synthetic();a,_=w.context_frame(f);last=f.day.iloc[-1]
        f.loc[f.day==last,['high','low','close','volume']]=[999.,1.,500.,99999]
        b,_=w.context_frame(f)
        cols=[f'{feat}_{lb}' for feat in w.FEATURES for lb in w.LOOKBACK]+['atr']
        np.testing.assert_allclose(a.loc[a.day==last,cols],b.loc[b.day==last,cols],equal_nan=True)

    def test_today_late_invalid_bar_not_future_entry_veto(self):
        f=synthetic();a,_=w.context_frame(f);f.loc[f.index[-1],'valid']=False;b,_=w.context_frame(f)
        cols=[f'{feat}_{lb}' for feat in w.FEATURES for lb in w.LOOKBACK]+['atr']
        np.testing.assert_allclose(a.loc[a.day==a.day.iloc[-1],cols],b.loc[b.day==b.day.iloc[-1],cols],equal_nan=True)

    def test_roll_never_uses_prior_contract_prices(self):
        f=synthetic();f.loc[f.day>=f.day.iloc[-1],'contract']='DCE.c2409';a,_=w.context_frame(f)
        for feat in w.FEATURES:
            for lb in w.LOOKBACK:self.assertTrue(a.loc[a.day==a.day.iloc[-1],f'{feat}_{lb}'].isna().all())

    def test_prior_incomplete_day_invalidates_rolling_context(self):
        f=synthetic();yesterday=sorted(f.day.unique())[-2];f.loc[(f.day==yesterday)&(f.minute==895),'valid']=False
        a,_=w.context_frame(f)
        self.assertTrue(a.loc[a.day==a.day.iloc[-1],'atr'].isna().all())
        self.assertTrue(a.loc[a.day==a.day.iloc[-1],'daily_location_3'].isna().all())

    def test_current_gap_known_only_after_previous_bar(self):
        w.configure();f=synthetic();a,_=w.context_frame(f)
        day=a.day.iloc[-1];i=a.loc[(a.day==day)&(a.minute==545)].index[0]
        a.loc[i-1,'open_gap_3']=2.;a.loc[i,'open_gap_3']=-2.
        inds=core.signals(a,['open_gap',3,2,1,12,.5],[-.5,.5])
        self.assertIn(i,inds)

    def test_real_data_prefix_matches(self):
        for p in ['C','JD','PB']:
            f=pd.read_pickle(w.PRIOR/f'prepared/{p}.pkl')
            cut=f.loc[(f.day=='2025-05-08')&(f.minute==545)].index[0]
            # Bound runtime, retain sufficient old history with identical starting state.
            f=f.iloc[max(0,cut-45*40):cut+45*3].reset_index(drop=True)
            cut=f.loc[(f.day=='2025-05-08')&(f.minute==545)].index[0]
            a,_=w.context_frame(f);b,_=w.context_frame(f.iloc[:cut+1])
            cols=[f'{feat}_{lb}' for feat in w.FEATURES for lb in w.LOOKBACK]+['atr']
            np.testing.assert_allclose(a.iloc[:cut+1][cols],b[cols],equal_nan=True)


if __name__=='__main__':unittest.main()
