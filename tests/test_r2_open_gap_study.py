import sys
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
sys.path.insert(0,str(Path(__file__).resolve().parent))
import r2_open_gap_study as r
from test_s3_alpha import fixture


class GapTests(unittest.TestCase):
    def test_known_gap_uses_previous_full_same_contract_day(self):
        raw,cal=fixture(3)
        raw.loc[:44,['open','close']]=100.;raw.loc[:44,'high']=102.;raw.loc[:44,'low']=98.
        raw.loc[45,'open']=104.;raw.loc[45,'high']=105.;raw.loc[45,'close']=104.5;raw.loc[45,'low']=103.
        f=r.prep.prepare(raw,cal);dates=sorted(f.day.unique());q=r.days(f,dates)
        self.assertEqual(q[0]['gap'],4.);self.assertEqual(q[0]['gap_ratio'],1.);self.assertTrue(q[0]['is_large'])
        raw.loc[45:,'contract']='DCE.c2509';g=r.prep.prepare(raw,cal)
        self.assertFalse(any(x['day']==dates[1] for x in r.days(g,dates)))

    def test_missing_previous_bar_or_trading_date_rejected(self):
        raw,cal=fixture(3);f=r.prep.prepare(raw,cal);dates=sorted(f.day.unique())
        f.loc[3,'valid']=False
        self.assertFalse(any(x['day']==dates[1] for x in r.days(f,dates)))
        full=r.prep.prepare(raw,cal);last=full.loc[full.day!=dates[1]].copy()
        self.assertFalse(any(x['day']==dates[2] for x in r.days(last,dates)))

    def test_control_uses_only_earlier_features_and_no_outcomes(self):
        event=dict(day='2024-03-04',observed_day_index=80,prior_range=10.)
        records=[dict(day=d,observed_day_index=i,prior_range=width,is_control=True) for d,i,width in
            [('2024-03-01',79,10.),('2024-03-05',81,10.),('2024-02-01',59,20.)]]
        self.assertEqual(r.match(event,records)['day'],'2024-03-01')
        records[0]['is_control']=False;self.assertIsNone(r.match(event,records))

    def test_fixed_label_cost_and_two_directions(self):
        raw,cal=fixture(1);raw['open']=100.;raw['close']=103.;raw['high']=104.;raw['low']=99.
        f=r.prep.prepare(raw,cal);a={k:f[k].to_numpy() for k in f};spec=dict(tick=1.,multiplier=10.)
        rule=dict(fixed_yuan_per_lot=1.,notional_rate=0.);fees=dict(open=rule,close_today=rule)
        long=r.label(a,12,1,6,spec,fees,2**63-1);short=r.label(a,12,-1,6,spec,fees,2**63-1)
        self.assertEqual(long['gross_yuan'],-short['gross_yuan'])
        self.assertEqual(long['net_yuan']+short['net_yuan'],-44.)
        self.assertGreater(long['net_yuan'],long['net_2tick_yuan'])

    def test_features_prefix_and_current_day_later_bars_unused(self):
        raw,cal=fixture(8);f=r.prep.prepare(raw,cal);dates=sorted(f.day.unique());cut=46
        short=r.prep.prepare(raw.iloc[:cut],cal)
        self.assertEqual([q for q in r.days(f,dates) if q['index']<cut],r.days(short,sorted(short.day.unique())))

    def test_reserved_data_guard(self):
        with self.assertRaises(ValueError):r.prior.load(None,'2024-01-01','2026-01-01',None)


if __name__=='__main__':unittest.main()
