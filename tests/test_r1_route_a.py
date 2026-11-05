import sys
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import r1_route_a as r
sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_s3_alpha import fixture

SPEC=dict(tick=1.,multiplier=10.)
RULE=dict(fixed_yuan_per_lot=1.,notional_rate=0.)
FEE=dict(open=RULE,close_today=RULE)


class RouteATests(unittest.TestCase):
    def arrays(self):
        raw,cal=fixture(1)
        raw['open']=100.;raw['close']=102.;raw['high']=103.;raw['low']=99.
        f=r.prep.prepare(raw,cal)
        return f,{k:f[k].to_numpy(copy=True) for k in f}

    def test_hand_cash_time_exit(self):
        f,a=self.arrays();x=r.trade(a,6,1,95,SPEC,FEE,2**63-1)
        self.assertEqual(x['status'],'complete');self.assertEqual(x['held_bars'],24)
        self.assertEqual(x['entry_ns'],int(f.start_ns.iloc[7]));self.assertEqual(x['net_yuan'],-2.)
        self.assertEqual(x['gross_yuan'],20.);self.assertEqual(x['slippage_yuan'],20.)

    def test_stop_gap_and_budget_no_clipping(self):
        _,a=self.arrays();a['open'][8]=10;a['low'][8]=9;a['high'][8]=11;a['close'][8]=10
        x=r.trade(a,6,1,95,SPEC,FEE,2**63-1)
        self.assertEqual(x['reason'],'gap_stop');self.assertLess(x['net_yuan'],-600)
        self.assertEqual(r.trade(a,6,1,10,SPEC,FEE,2**63-1)['reason'],'one_lot_budget')

    def test_missing_is_unknown_not_zero_and_late_reject(self):
        _,a=self.arrays();a['valid'][10]=False
        x=r.trade(a,6,1,95,SPEC,FEE,2**63-1)
        self.assertEqual(x['status'],'unknown');self.assertIsNone(x['net_yuan'])
        self.assertEqual(r.trade(a,35,1,95,SPEC,FEE,2**63-1)['reason'],'insufficient_scheduled_holding_time')

    def test_actual_prefix_both_structures(self):
        raw,cal=fixture(15);full=r.prep.prepare(raw,cal);short=r.prep.prepare(raw.iloc[:320],cal)
        for minutes in [30,60]:
            a=r.signals(full,minutes,.1);b=r.signals(short,minutes,.1)
            self.assertEqual([q for q in a if q['structure_index']<len(short)],b)

    def test_confirmation_only_adjacent_not_retry(self):
        _,a=self.arrays();q=dict(structure_index=5,structure_end_ns=int(a['end_ns'][5]),side=1,structure_close=101.)
        self.assertEqual(r.confirmation(a,q),(6,'confirmed'))
        a['close'][6]=100.;self.assertEqual(r.confirmation(a,q)[1],'confirmation_failed')
        a['close'][6]=102.;a['start_ns'][6]+=r.prep.STEP
        self.assertEqual(r.confirmation(a,q)[1],'confirmation_gap_day_or_invalid')

    def test_reserved_access_rejected(self):
        with self.assertRaises(ValueError):r.load(None,'2024-01-01','2026-01-01',None)


if __name__=='__main__':unittest.main()
