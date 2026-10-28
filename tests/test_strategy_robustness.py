import copy
import pathlib
import sys
import unittest

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
from research_strategy_robustness import combined_legs, passes, definitions, score, paired_interval, research_calendar


def row(status='closed', amount=100, price=110):
    return dict(event_key='x',symbol='A',exit_trading_day='2026-10-09',
        result=dict(evaluation=dict(status=status,entry_ts='2026-10-09 09:05:00',
            exit_ts='2026-10-09 10:00:00',fill=100,risk=10,volume=1,gross_points=price-100,
            net_amount=amount,net_r=amount/100,exit_reason='target_s1'),
            fills=[dict(offset='open',price=100,volume=1),dict(offset='close',price=price,volume=1)]),
        cost_repricing=dict(gross_amount_after_slippage=amount+2,commission=2))


class RobustnessTests(unittest.TestCase):
    def test_two_lot_sum_and_r_normalization_are_different_units(self):
        a,b=row(amount=100),row(amount=200,price=120)
        a0=copy.deepcopy(a)
        combined=combined_legs(a,b);e=combined['result']['evaluation']
        self.assertEqual(e['net_amount'],300)
        self.assertEqual(e['net_r'],1.5)
        self.assertEqual(e['volume'],2)
        self.assertEqual(combined['cost_repricing']['commission'],4)
        self.assertEqual(sum(f['volume'] for f in combined['result']['fills'][1:]),2)
        self.assertEqual(a,a0)

    def test_two_leg_order_never_inherits_single_filled_leg_or_drops_unknown(self):
        for a,b in ((row('unfilled'),row()),(row(),row('filtered_net_rr_entry'))):
            self.assertNotEqual(combined_legs(a,b)['result']['evaluation']['status'],'closed')
        for a,b in ((row('ambiguous'),row()),(row(),row('unfinished'))):
            self.assertIn(combined_legs(a,b)['result']['evaluation']['status'],('ambiguous','unfinished'))

    def test_equal_legs_require_same_entry_and_structural_risk(self):
        a,b=row(),row();b['result']['evaluation']['fill']=101
        with self.assertRaises(ValueError):combined_legs(a,b)

    def test_scores_retain_warning_caps_and_all_friction_atr_gates(self):
        f=dict(total=3.1,dim_a=5,dim_b=5,dim_warning=5,kind_cap=3.,friction_r=.1,risk_atr=1.)
        c=dict(filter=dict(weights=[.7,.15,.15],score=3.5))
        self.assertFalse(passes(f,c))
        c['filter']['score']=3.
        self.assertTrue(passes(f,c))
        for name,value in (('risk_atr',None),('risk_atr',.74),('friction_r',.201)):
            other=dict(f);other[name]=value
            self.assertFalse(passes(other,c))

    def test_insufficient_samples_cannot_win_by_high_mean_r(self):
        p=dict(selection=dict(min_closed=40,min_baseline_fraction=.5))
        metric=dict(stats=dict(n=5,net=5000,mean_r=10),top3_positive_product_share=.3,top3_positive_day_share=.3)
        self.assertFalse(score([metric,metric],[metric,metric],p)['eligible'])

    def test_weekly_interval_keeps_zero_dates_and_joint_symbols(self):
        days=['2026-10-09','2026-10-12','2026-10-19']
        v=paired_interval([row(amount=100)],[row(amount=125)],days,100)
        self.assertEqual(v['weeks'],3)
        self.assertEqual(v['delta_cash'],25)

    def test_finite_unique_definitions_and_uniform_risk_floor(self):
        templates,candidates=definitions()
        self.assertEqual(len({t['name'] for t in templates}),len(templates))
        self.assertEqual(len({c['name'] for c in candidates}),len(candidates))
        self.assertTrue(all(t['rules']['min_actual_risk_ticks']==10 for t in templates))
        self.assertTrue(all(t['rules']['flat_before_weekend'] for t in templates))

    def test_incomplete_zero_volume_placeholder_cannot_create_settlement_day(self):
        bars=[]
        for date,times in [('2026-09-04',['09:05:00','09:10:00','09:15:00']),('2026-09-05',['09:05:00']),
                           ('2026-09-07',['09:05:00','09:10:00','09:15:00'])]:
            for t in times:bars.append(dict(datetime=date+' '+t,open=100,high=101,low=99,close=100,volume=0))
        src=dict(symbols=dict(A=dict(bars=bars,rollovers=[])))
        cal=research_calendar(src,True)
        self.assertEqual(cal.dates,['2026-09-04','2026-09-07'])
        self.assertEqual(cal.day('2026-09-04 21:00:00'),'2026-09-07')


if __name__=='__main__':unittest.main()
