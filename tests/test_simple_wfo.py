"""Adversarial checks for WFO execution, time ordering and shared funding."""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
import pandas as pd

spec=importlib.util.spec_from_file_location('simple_wfo',Path(__file__).resolve().parents[1]/'scripts/simple_wfo.py')
w=importlib.util.module_from_spec(spec);spec.loader.exec_module(w)


def frame():
    minutes=[565,570,575,580,585,590,595,600,605,610,630,635,640,645,650,655,660,665,670,675,680,685,810,815,820,825,830,835,840,845,850,855,860,865,870,875,880,885,890,895]
    start=w.ns('2024-08-01')
    f=pd.DataFrame([dict(contract='DCE.c2409',start_ns=start+m*60_000_000_000,end_ns=start+m*60_000_000_000+w.STEP,
                         day='2024-08-01',month='2024-08',minute=m,open=100.,high=103.,low=98.,close=101.,volume=100,valid=True,atr=5.) for m in minutes])
    f['location_6']=.9
    return f


S=dict(tick=1.,multiplier=10.,margin_rate=.2,fees={k:dict(fixed_yuan_per_lot=1.,notional_rate=0.) for k in ['open','close_today','close_yesterday']})
C=['location',6,2,1,6,1.]


class ExecutionTests(unittest.TestCase):
    def test_gap_stop_has_adverse_fill_and_can_exceed600(self):
        f=frame();f.loc[2,['open','high','low','close']]=[25.,28.,24.,27.]
        t=w.trade_paths(f,1,6,1.,S)[1]
        self.assertLessEqual(t['planned_risk'],600)
        self.assertEqual(t['exit'],23.)
        self.assertLess(t['net'],-600)
        self.assertEqual(t['reason'],'gap_stop')

    def test_stop_rounding_slippage_and_fees(self):
        f=frame();f.loc[2,'low']=90.
        t=w.trade_paths(f,1,6,1.,S)[1]
        self.assertEqual(t['entry'],102.)
        self.assertEqual(t['stop'],96.)
        self.assertEqual(t['exit'],94.)
        self.assertEqual(t['net'],-84.)
        self.assertEqual(t['fees'],4.)
        self.assertEqual(t['lots'],1)

    def test_signal_prefix_and_current_bar_ohlc_independence(self):
        f=frame();expected=w.signals(f,C,[.2,.8])
        altered=f.copy();altered.loc[1,['high','low','close','volume','valid']]=[999.,1.,500.,0,False]
        # Today's opening decision unchanged when this bar's eventual path changes.
        self.assertEqual(1 in expected,1 in w.signals(altered,C,[.2,.8]))
        np.testing.assert_array_equal(w.signals(f.iloc[:20].copy(),C,[.2,.8]),expected[expected<20])

    def test_no_entry_from_future_signal(self):
        f=frame();f['location_6']=0.;f.loc[1,'location_6']=1.
        self.assertNotIn(1,w.signals(f,C,[.2,.8]))

    def test_locked_entry_unresolved_never_zero_pnl(self):
        f=frame();f.loc[1,['high','low','open','close']]=100.
        t=w.trade_paths(f,1,6,1.,S)[1]
        self.assertTrue(t['unresolved']);self.assertNotIn('net',t)
        trades,unknown=w.isolated([1],{1:t})
        self.assertEqual(trades,[]);self.assertEqual(unknown,1)

    def test_locked_stop_defers_to_next_open(self):
        f=frame();f.loc[2,['high','low','open','close']]=90.
        f.loc[3,['open','high','low','close']]=[99.,103.,98.,101.]
        t=w.trade_paths(f,1,6,1.,S)[1]
        self.assertEqual(t['exit_i'],3)
        self.assertEqual(t['exit'],97.)

    def test_missing_slot_forces_next_actual_open(self):
        f=frame().drop(index=2).reset_index(drop=True)
        t=w.trade_paths(f,1,6,1.,S)[1]
        self.assertEqual(t['exit_i'],2)
        self.assertEqual(t['reason'],'time_or_session_or_missing')

    def test_600_cap_skips_not_tightens_stop(self):
        f=frame();f.loc[0,'atr']=100.
        self.assertNotIn(1,w.trade_paths(f,1,6,1.,S))

    def test_timed_exit_does_not_use_next_open_early(self):
        f=frame();f['location_6']=0.;f.loc[0,'location_6']=1.
        f.loc[7,['open','high','low','close']]=[120.,123.,118.,121.]
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp);d=out/'training/fold_01';d.mkdir(parents=True)
            (d/'selection.json').write_text(json.dumps(dict(selected=[dict(product='C',candidate_id='c',candidate=C,cuts=[.2,.8])])),encoding='utf8')
            with patch.object(w,'OUT',out):r=w.account([dict(fold=1,train='2023-11-01',test='2024-08-01',end='2024-11-01')],{'C':f},{'C':S})
        t=r['trades'][0]
        self.assertEqual(t['exit_ns'],int(f.start_ns.iloc[7]))
        prior=[s for s in r['equity'] if s['time_ns']==int(f.start_ns.iloc[7])][0]
        self.assertEqual(prior['balance'],49998.)  # exit isn't booked one bar early
        self.assertEqual(r['final_equity'],50156.)

    def test_shared_account_margin_rejects_third_product(self):
        f=frame();f['location_6']=0.;f.loc[0,'location_6']=1.
        expensive=dict(S,multiplier=1000.)
        # Increase price, retain low cash risk by a smaller price tick/ATR.
        f[['open','high','low','close']]=f[['open','high','low','close']]/100
        f['atr']=.05;expensive['tick']=.01
        expensive['multiplier']=50000.  # margin 10200/lot; stop risk would exceed cap
        # Use margin stress to force capacity independent of price risk.
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp);d=out/'training/fold_01';d.mkdir(parents=True)
            sels=[dict(product=p,candidate_id=p,candidate=C,cuts=[.2,.8]) for p in ['A','B','C']]
            (d/'selection.json').write_text(json.dumps(dict(selected=sels)),encoding='utf8')
            with patch.object(w,'OUT',out):r=w.account([dict(fold=1,train='2023-11-01',test='2024-08-01',end='2024-11-01')],{p:frame().assign(location_6=[1.]+[0.]*39) for p in ['A','B','C']},{p:S for p in ['A','B','C']},margin_rate=12.)
        self.assertEqual(len(r['trades']),2)
        self.assertEqual(r['rejections'][0]['reason'],'margin')


if __name__=='__main__':unittest.main()
