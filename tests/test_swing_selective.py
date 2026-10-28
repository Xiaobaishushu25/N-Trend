import pathlib,sys,unittest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
from research_swing_selective import state,choose,counter_stop_portfolio
class Selective(unittest.TestCase):
    def event(self,h=-2.,s=-1.,direction='up'):
        return dict(symbol='RB0',direction=direction,s0_ts='a',s1_ts='b',market_context=dict(trend_alignment_60=h,trend_slope_60=s))
    def test_signed_slope_is_symmetric_and_missing_passes_baseline(self):
        self.assertTrue(state(self.event())['dual']);self.assertTrue(state(self.event(s=1.,direction='down'))['dual'])
        self.assertFalse(state(self.event(s=1.))['opposed'])
        self.assertEqual(choose(dict(condition='opposed',profile='kind'),dict(direction='up')),'base')
    def test_router_never_reads_future_outcomes(self):
        e=self.event();t=dict(condition='dual',profile='body');self.assertEqual(choose(t,e),'body')
        e.update(outcome='loss',evaluation=dict(net_r=999999));self.assertEqual(choose(t,e),'body')
    def rows(self):
        rows=[];events={}
        for i in range(4):
            key=str(i);start=f'2026-01-05 10:0{i}:00';end=f'2026-01-05 10:0{i+1}:00'
            rows.append(dict(event_key=key,symbol='RB0',execution_contract='rb',warning_ts=start,result=dict(evaluation=dict(status='closed',trigger_ts=start,entry_ts=start,exit_ts=end,exit_reason='stop',net_amount=-1.))))
            events[key]=self.event()
        return rows,events,{'rb':'2026-12-31 15:00:00'}
    def test_stop_must_be_accepted_and_exit_strictly_before_next_trigger(self):
        rows,events,ends=self.rows();a,l=counter_stop_portfolio(rows,events,ends)
        self.assertEqual([r['event_key'] for r in a],['0'])
        self.assertEqual(l[1]['reason'],'symbol_slot');self.assertFalse(l[1]['blocked_prior_stop'])
        self.assertEqual(l[2]['reason'],'counter_cluster_stopped')
    def test_aligned_new_setup_can_trade_after_counter_stop(self):
        rows,events,ends=self.rows();events['2']['market_context']['trend_alignment_60']=1.
        a,_=counter_stop_portfolio(rows,events,ends);self.assertEqual([r['event_key'] for r in a],['0','2'])
    def test_unaccepted_stop_does_not_block_new_cluster(self):
        rows,events,ends=self.rows();events['1']['s0_ts']='x';events['2']['s0_ts']='x'
        a,_=counter_stop_portfolio(rows,events,ends);self.assertIn('2',[r['event_key'] for r in a])
if __name__=='__main__':unittest.main()
