import pathlib,sys,unittest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
import research_swing_failure as f
from test_swing_selective import Selective
class FailureRouting(unittest.TestCase):
    def test_graded_choice_preserves_aligned_and_unknown_entries(self):
        t=dict(condition='graded',profile='graded_half');helper=Selective()
        self.assertEqual(f.choose(t,helper.event(h=-2.)),'half')
        self.assertEqual(f.choose(t,helper.event(h=-.2)),'kind')
        self.assertEqual(f.choose(t,helper.event(h=.2)),'base')
        self.assertEqual(f.choose(t,dict(direction='up')),'base')
    def test_negative_completed_body_exit_blocks_only_after_exit(self):
        rows,events,ends=Selective().rows();rows[0]['result']['evaluation']['exit_reason']='warning_body_failed_close'
        f._original_counter=f.s.counter_stop_portfolio
        a,l=f.wrapped_failure_portfolio(rows,events,ends)
        self.assertEqual([r['event_key'] for r in a],['0'])
        self.assertEqual(a[0]['result']['evaluation']['exit_reason'],'warning_body_failed_close')
        self.assertFalse(l[1]['blocked_prior_stop']);self.assertTrue(l[2]['blocked_prior_stop'])
    def test_positive_body_exit_cannot_poison_budget(self):
        rows,events,ends=Selective().rows();rows[0]['result']['evaluation'].update(exit_reason='warning_body_failed_close',net_amount=1.)
        f._original_counter=f.s.counter_stop_portfolio
        a,_=f.wrapped_failure_portfolio(rows,events,ends);self.assertIn('2',[r['event_key'] for r in a])
if __name__=='__main__':unittest.main()
