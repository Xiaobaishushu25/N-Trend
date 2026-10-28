import pathlib,sys,unittest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
from test_swing_portfolio_balance import BalanceTests
from swing_portfolio_selective import select
from swing_portfolio_balance import select as base

class SelectiveTests(unittest.TestCase):
    row=BalanceTests.row
    inputs=BalanceTests.inputs
    def test_cap_only_counts_counter_hour_and_allows_aligned_and_missing(self):
        rows=[self.row(k,k,f'2026-01-05 09:0{i}:00','2026-01-05 09:10:00') for i,k in enumerate('abcdef',1)]
        e,x,g=self.inputs(rows)
        for k in 'abcf':e[k]['market_context']={'trend_alignment_60':0 if k=='b' else -1}
        e['d']['market_context']={'trend_alignment_60':1}
        a,l,_=select(rows,e,x,g,'sector2_counter');self.assertEqual([r['event_key'] for r in a],list('abde'));self.assertEqual(l[2]['reason'],'sector_slot');self.assertEqual(l[-1]['sector_reservations_before'],2)
        self.assertEqual(select(rows,e,x,g,'base')[0],base(rows,e,x,g,'base')[0])
    def test_stop_counts_strictly_past_only_and_no_follow_not_stop(self):
        times=[('01','02','stop'),('02','03','stop'),('03','04','no_follow'),('05','06','stop'),('06','07','stop'),('07','08','stop')]
        rows=[self.row(str(i),'A',f'2026-01-05 09:{a}:00',f'2026-01-05 09:{b}:00',reason=why) for i,(a,b,why) in enumerate(times)]
        e,x,g=self.inputs(rows);a,l,_=select(rows,e,x,g,'after_two_stops');self.assertEqual([r['event_key'] for r in a],['0','2','3']);self.assertEqual([v['visible_cluster_stop_count'] for v in l],[0,0,1,1,1,2]);self.assertEqual(l[-1]['reason'],'cluster_two_realized_stops')
    def test_opposite_direction_and_strict_sector_release(self):
        rows=[self.row('a','A','2026-01-05 09:01:00','2026-01-05 09:05:00'),self.row('b','B','2026-01-05 09:02:00','2026-01-05 09:10:00'),self.row('c','C','2026-01-05 09:03:00','2026-01-05 09:10:00',direction='down'),self.row('d','D','2026-01-05 09:05:00','2026-01-05 09:08:00'),self.row('e','E','2026-01-05 09:06:00','2026-01-05 09:08:00')]
        e,x,g=self.inputs(rows)
        for v in e.values():v['market_context']={'trend_alignment_60':-1}
        a,_,_=select(rows,e,x,g,'sector2_counter_stops');self.assertEqual([z['event_key'] for z in a],list('abce'))

if __name__=='__main__':unittest.main()
