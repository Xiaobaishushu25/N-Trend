import sys
from pathlib import Path
import unittest
import numpy as np
import pandas as pd
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import explore_v4_logic as q
import explore_v3_signals as v3
import s3_signals as prep
from test_s3_alpha import fixture

SPEC=dict(tick=1.,multiplier=10.)
FEES={k:dict(fixed_yuan_per_lot=1.,notional_rate=0.) for k in ('open','close_today')}


def trading_fixture():
    raw,cal=fixture(1)
    for name in ('open','close'):raw[name]=100.
    raw['high']=101.;raw['low']=99.;raw['volume']=100
    f=prep.prepare(raw,cal);f['atr']=2.
    return f,{k:f[k].to_numpy(copy=True) for k in f}


def signal_fixture():
    raw,cal=fixture(2);f=v3.features(prep.prepare(raw,cal),dict(tick=.1,multiplier=10),FEES)
    f['economic_gate']=False;f.loc[[20,21],'economic_gate']=True;f['activity_known']=True
    f['peer_median']=.2;f['peer_abs_median']=.4;f['peer_mad']=.2;f['peer_count']=2
    f['er12']=.2;f['range_high12']=110.;f['range_low12']=100.;f['atr']=2.
    f.loc[19,'close']=110.;f.loc[20,['open','high','low','close']]=[111,112,107,108]
    f.loc[21,['open','high','low','close']]=[108,109,106,107]
    return f


class V4Tests(unittest.TestCase):
    def test_budget_8_and_groups(self):self.assertEqual(len(q.templates(['C'])),8)

    def test_true_quiet_versus_cancellation(self):
        f=signal_fixture();frames={n:f.copy() for n in ('C','JD','SR')}
        for n,m in [('C',1000.),('JD',3.),('SR',-3.)]:frames[n]['norm_day_move']=m
        a=q.peers(frames)['C'];self.assertEqual(a.peer_median.iloc[20],0);self.assertEqual(a.peer_abs_median.iloc[20],3)
        arr={k:a[k].to_numpy() for k in a}
        self.assertTrue(q.state_at(arr,20,'divergent'));self.assertFalse(q.state_at(arr,20,'quiet'))

    def test_confirmation_is_next_bar_and_never_counts_past_target(self):
        f=signal_fixture();cs=q.templates(['C'])
        self.assertEqual(q.signals(f,cs[0],.1)[0]['index'],20)
        self.assertEqual(q.signals(f,cs[1],.1)[0]['index'],21)
        f.loc[21,'low']=104
        self.assertFalse(q.signals(f,cs[1],.1))

    def test_setup_uses_previous_peer_state(self):
        f=signal_fixture();c=q.templates(['C'])[0]
        f.loc[20,'peer_abs_median']=10.;f.loc[20,'peer_median']=10.
        self.assertEqual(q.signals(f,c,.1)[0]['index'],20)
        f.loc[19,'peer_abs_median']=10.;self.assertFalse(q.signals(f,c,.1))

    def test_full_prefix_all8(self):
        raw,cal=fixture(12);spec=dict(tick=.1,multiplier=10)
        frames={n:v3.features(prep.prepare(raw.copy(),cal),spec,FEES) for n in ('C','JD','SR')}
        full=q.peers(frames);cut=410;short=q.peers({n:f.iloc[:cut].copy() for n,f in frames.items()})
        for n in frames:
            pd.testing.assert_frame_equal(full[n].iloc[:cut],short[n])
            for c in q.templates(['C','JD','SR']):self.assertEqual([x for x in q.signals(full[n],c,.1) if x['index']<cut],q.signals(short[n],c,.1))

    def test_entry_next_bar_and_hand_target_cash(self):
        f,a=trading_fixture();a['high'][13]=106
        r=q.trade(a,12,1,97,105,SPEC,FEES,2**63-1)
        self.assertEqual(r['trade_entry_ns'],int(f.start_ns.iloc[13]));self.assertEqual(r['trade_reason'],'target')
        self.assertAlmostEqual(r['trade_net_cny'],28.);self.assertAlmostEqual(r['trade_net_R'],1.4)

    def test_ambiguous_stop_first_and_gap_not_clipped(self):
        _,a=trading_fixture();a['high'][13]=106;a['low'][13]=96
        r=q.trade(a,12,1,97,105,SPEC,FEES,2**63-1)
        self.assertEqual(r['trade_reason'],'stop');self.assertEqual(r['trade_net_cny'],-52.);self.assertTrue(r['trade_ambiguous'])
        _,a=trading_fixture()
        for name,value in [('open',10.),('high',11.),('low',9.),('close',10.)]:a[name][14]=value
        r=q.trade(a,12,1,97,105,SPEC,FEES,2**63-1)
        self.assertEqual(r['trade_reason'],'gap_stop');self.assertTrue(r['trade_actual_loss_over_600']);self.assertEqual(r['trade_net_cny'],-922.)

    def test_unaffordable_and_unknown_path_not_zero_return(self):
        _,a=trading_fixture();r=q.trade(a,12,1,50,105,SPEC,FEES,2**63-1)
        self.assertEqual(r['trade_reason'],'integer_lot_budget');self.assertIsNone(r['trade_net_R'])
        a['segment'][15:]+=1;r=q.trade(a,12,1,97,105,SPEC,FEES,2**63-1)
        self.assertEqual(r['trade_reason'],'missing_path');self.assertIsNone(r['trade_net_R'])

    def test_projection_uses_signal_reference(self):
        _,a=trading_fixture();event=dict(side=1,forecast_stop_R=2.,forecast_target_R=2.)
        self.assertEqual(q.projection(event,a,20,SPEC),(97.,105.))


if __name__=='__main__':unittest.main()
