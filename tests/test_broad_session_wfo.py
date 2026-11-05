import sys
import unittest
import tempfile
import json
from unittest.mock import patch
from pathlib import Path
import numpy as np
import pandas as pd
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
import broad_session_wfo as w

S=dict(tick=1.,multiplier=10.,fees={kind:dict(fixed_yuan_per_lot=1.,notional_rate=0.) for kind in ['open','close_today','close_yesterday']})


def market():
    start=w.ns('2024-08-01 21:00')
    f=pd.DataFrame([dict(contract='DCE.c2409',start_ns=start+j*w.c.STEP,end_ns=start+(j+1)*w.c.STEP,open=100.,high=103.,low=99.,close=101.,volume=200,close_oi=10000+j,complete=1,constituent_count=5,valid=True) for j in range(180)])
    return f


def sample(f):
    return pd.DataFrame([dict(contract='DCE.c2409',product='C',entry_i=1,entry_ns=int(f.start_ns.iloc[1]),signal_ns=int(f.start_ns.iloc[1]),atr=1.,close=100.,month='2024-08')])


class BroadTests(unittest.TestCase):
    def test_features_prefix_and_no_cross_contract(self):
        f=market();a=w.features(f);b=w.features(f.iloc[:120]);cols=w.LOCAL+['oi12','price_oi','atr']
        np.testing.assert_allclose(a[cols].iloc[:120],b[cols],equal_nan=True)

    def test_midnight_selection_key_uses_previous_natural_day(self):
        f=w.features(market());q=f.loc[f.minute==5].iloc[0]
        self.assertEqual(q.market_date,'2024-08-01')
        self.assertEqual(q.date,'2024-08-02')

    def test_future_label_purge_and_past_only_imputation(self):
        n=60;f=pd.DataFrame({col:np.linspace(0,1,n) for col in w.LOCAL});f['entry_ns']=np.arange(n,dtype=np.int64);f['y12']=np.linspace(-2,2,n);f['yend12']=np.arange(n)+10
        a=w.fit_model(f,'local',12,.1,40)
        f.loc[f.yend12>=40,'y12']=1e9;f.loc[f.entry_ns>=40,w.LOCAL]=1e9
        b=w.fit_model(f,'local',12,.1,40)
        np.testing.assert_allclose(a['coef'],b['coef']);self.assertLess(a['last_label_ns'],40);self.assertEqual(a['n'],30)

    def test_missing_optional_information_is_past_imputed(self):
        f=pd.DataFrame({col:np.arange(40,dtype=float) for col in w.VIEWS['combined']});f['curve']=np.nan;f['entry_ns']=np.arange(40);f['y12']=np.arange(40)/100;f['yend12']=np.arange(40)+2
        m=w.fit_model(f,'combined',12,10.,40);pred=w.predict(f,m)
        self.assertTrue(np.isfinite(pred).all());self.assertEqual(m['median'][m['columns'].index('curve')],0.)

    def test_gap_stop_can_exceed600_without_tightening_stop(self):
        f=market();f.loc[2,['open','high','low','close']]=[20.,23.,18.,21.]
        t=w.paths(sample(f),{'DCE.c2409':f},12,2.,S,w.ns('2024-11-01'))[(0,1)]
        self.assertLess(t['planned_risk'],600);self.assertLess(t['net'],-600);self.assertEqual(t['exit'],18.);self.assertEqual(t['reason'],'gap_stop')

    def test_unknown_entry_not_silent_zero_return(self):
        f=market();f.loc[1,['high','low','open','close']]=100.
        t=w.paths(sample(f),{'DCE.c2409':f},12,2.,S,w.ns('2024-11-01'))[(0,1)]
        self.assertTrue(t['unknown']);self.assertNotIn('net',t)

    def test_same_held_contract_and_night_holding(self):
        f=market();s=sample(f);s['atr']=10.
        tables=w.paths(s,{'DCE.c2409':f},48,2.,S,w.ns('2024-11-01'));t=tables[(0,1)]
        self.assertFalse(t['unknown']);self.assertEqual(t['contract'],'DCE.c2409');self.assertEqual(t['exit_ns'],int(f.start_ns.iloc[49]));self.assertEqual(t['reason'],'time_or_window')

    def test_locked_exit_defers_instead_of_ideal_price(self):
        f=market();f.loc[2,['open','high','low','close']]=90.
        f.loc[3,['open','high','low','close']]=[97.,100.,95.,99.]
        t=w.paths(sample(f),{'DCE.c2409':f},12,2.,S,w.ns('2024-11-01'))[(0,1)]
        self.assertEqual(t['exit_ns'],int(f.start_ns.iloc[3]));self.assertEqual(t['exit'],95.)

    def test_late_window_no_entry_uses_fixed_clock(self):
        f=market();self.assertEqual(w.paths(sample(f),{'DCE.c2409':f},12,2.,S,w.ns('2024-08-07')), {})

    def test_fee_uses_larger_close_schedule(self):
        s=dict(S,fees=dict(S['fees']));s['fees']['close_yesterday']=dict(fixed_yuan_per_lot=10.,notional_rate=0.)
        self.assertEqual(w.fee(s,100.),11.)

    def test_search_count_is_fixed(self):
        self.assertEqual(len(w.VIEWS)*len(w.HOLDS)*len(w.ALPHAS)*len(w.QUANTILES)*len(w.STOPS),96)

    def test_real_synchronous_peer_excludes_own(self):
        dest=w.OUT/'prepared'
        if not (dest/'manifest.json').exists():self.skipTest('prepared input required')
        choices=pd.read_csv(dest/'contract_choices.csv')
        for product in ['C','EG','PB']:
            panel=pd.read_pickle(dest/f'{product}.pkl');row=panel.loc[(panel.month=='2025-05')&panel.peer12.notna()].iloc[0]
            values=[]
            for p in choices['product'].unique():
                if p==product:continue
                q=choices.loc[(choices['product']==p)&(choices.day==row.market_date)]
                if not len(q):continue
                code=q.contract.iloc[0];raw=pd.read_pickle(dest/'raw'/f'{code}.pkl')
                closed=raw.loc[(raw.end_ns==row.signal_ns)&raw.valid]
                if len(closed) and np.isfinite(closed.ret12.iloc[0]):values.append(closed.ret12.iloc[0])
            self.assertGreaterEqual(len(values),2);self.assertAlmostEqual(row.peer12,float(np.mean(values)),places=10)

    def test_simulator_keeps_held_contract_on_roll(self):
        a=market();b=market();b['contract']='DCE.c2501'
        b[['open','high','low','close']]+=900
        f=sample(a);f['atr']=10.
        second=f.copy();second['contract']='DCE.c2501';second['entry_i']=2;second['entry_ns']=int(b.start_ns.iloc[2]);second['signal_ns']=int(b.start_ns.iloc[2]);second['close']=1000.
        f=pd.concat([f,second],ignore_index=True)
        for col in w.LOCAL:f[col]=0.
        model=dict(columns=w.LOCAL,median=[0.]*len(w.LOCAL),scale=[1.]*len(w.LOCAL),coef=[1.]+[0.]*(2*len(w.LOCAL)),quantiles={'.6':0.,'0.6':0.})
        sel=dict(product='C',candidate_id='test',param=['local',48,.1,.6,2.],model=model)
        with tempfile.TemporaryDirectory() as td:
            out=Path(td);folder=out/'training/fold_01';folder.mkdir(parents=True)
            (folder/'selection.json').write_text(json.dumps(dict(selected=[sel])),encoding='utf8')
            with patch.object(w,'OUT',out),patch.object(w,'load_raw',return_value={'DCE.c2409':a,'DCE.c2501':b}):
                r=w.simulate([dict(fold=1,test='2024-08-01',end='2024-11-01')],{'C':f},{'C':S})
        self.assertEqual(len(r['trades']),1);self.assertEqual(r['trades'][0]['contract'],'DCE.c2409')
        self.assertLess(max(x['equity'] for x in r['equity']),51000)
        self.assertEqual(r['rejections'][0]['reason'],'active_product')


if __name__=='__main__':unittest.main()
