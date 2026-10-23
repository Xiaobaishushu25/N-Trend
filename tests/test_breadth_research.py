import pathlib
import sys
import unittest

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
from report_breadth_research import sector_admission, symmetric_trim


def row(key,cash,r=None,symbol='A',day='2026-10-09',status='closed'):
    return dict(event_key=key,symbol=symbol,direction='up',warning_ts=day+' 09:00:00',
        result=dict(evaluation=dict(status=status,entry_ts=day+' 09:05:00',exit_ts=day+' 10:00:00',
            net_amount=cash,net_r=cash/100 if r is None and cash is not None else r)))


class BreadthResearchTests(unittest.TestCase):
    def test_symmetric_removal_keeps_both_tail_effects_and_counts(self):
        rows=[row(str(i),v) for i,v in enumerate([1000.,500.,-800.,-200.,50.])]
        t=symmetric_trim(rows)['trim']['1']
        self.assertEqual(t['without_best']['net_amount'],-450.)
        self.assertEqual(t['without_worst']['net_amount'],1350.)
        self.assertEqual(t['without_both']['net_amount'],350.)
        self.assertEqual(t['without_both']['n'],3)

    def test_day_removal_drops_whole_day_and_cash_r_rankings_differ(self):
        rows=[row('a',100.,10.,day='2026-10-09'),row('b',100.,10.,day='2026-10-09'),
            row('c',150.,1.,day='2026-10-12'),row('d',-50.,-1.,day='2026-10-13')]
        self.assertEqual(symmetric_trim(rows,'exit_date')['trim']['1']['without_best']['net_amount'],100.)
        self.assertEqual(symmetric_trim(rows,'trade','cash')['trim']['1']['best_keys'],['c'])
        self.assertEqual(symmetric_trim(rows,'trade','r')['trim']['1']['best_keys'],['a'])

    def test_night_and_next_day_aggregate_into_one_trading_day(self):
        a=row('night',100.,day='2026-10-09');b=row('day',200.,day='2026-10-12');c=row('other',250.,day='2026-10-13')
        a['exit_trading_day']=b['exit_trading_day']='2026-10-12';c['exit_trading_day']='2026-10-13'
        t=symmetric_trim([a,b,c],'trading_day')['trim']['1']
        self.assertEqual(t['best_keys'],['2026-10-12'])
        self.assertEqual(t['without_best']['net_amount'],250.)

    def test_unknown_is_never_a_zero_return_and_small_tails_do_not_overlap(self):
        rows=[row('a',20.),row('b',-5.),row('u',None,status='unfinished')]
        t=symmetric_trim(rows)['trim']['10']
        self.assertEqual(t['removed_best_units'],1)
        self.assertEqual(t['removed_worst_units'],1)
        self.assertEqual(t['without_both']['n'],0)

    def test_sector_admission_uses_time_and_fixed_ties_not_future_profit(self):
        rows=[row('a',-100.,symbol='A'),row('b',1000.,symbol='B')]
        sectors={'A':'same','B':'same'}
        selected,_=sector_admission(rows,{},sectors)
        self.assertEqual([r['event_key'] for r in selected],['a'])
        selected,_=sector_admission(rows,{},sectors,True)
        self.assertEqual([r['event_key'] for r in selected],['b'])
        rows[0]['result']['evaluation']['net_amount']=10000.
        selected,_=sector_admission(rows,{},sectors)
        self.assertEqual([r['event_key'] for r in selected],['a'])


if __name__=='__main__':unittest.main()
