import pathlib
import sys
import unittest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
from research_swing_geometry import stop_once,definitions

def row(key,start,end,reason='stop',status='closed'):
    return dict(event_key=key,symbol='RB0',execution_contract='rb',warning_ts='09:00',result=dict(evaluation=dict(status=status,trigger_ts=start,entry_ts=start,exit_ts=end,exit_reason=reason)))
def events(keys):return {k:dict(symbol='RB0',direction='up',s0_ts='a',s1_ts='b') for k in keys}
class GeometryTests(unittest.TestCase):
    def test_only_a_realized_accepted_stop_blocks(self):
        accepted,ledger=stop_once([row('a','10:00','10:10'),row('b','10:11','10:20')],events('ab'),{'rb':'11:00'})
        self.assertEqual(len(accepted),1);self.assertEqual(ledger[1]['reason'],'cluster_stop_once')
    def test_no_follow_loss_does_not_block(self):
        accepted,_=stop_once([row('a','10:00','10:10','no_follow'),row('b','10:11','10:20')],events('ab'),{'rb':'11:00'})
        self.assertEqual(len(accepted),2)
    def test_blocked_opportunity_does_not_create_a_future_stop(self):
        accepted,ledger=stop_once([row('a','10:00','10:10','target_s1'),row('b','10:01','10:05'),row('c','10:11','10:20')],events('abc'),{'rb':'11:00'})
        self.assertEqual([r['event_key'] for r in accepted],['a','c'])
    def test_unknown_exposure_not_freed_at_reported_disagreement(self):
        accepted,_=stop_once([row('a','10:00','10:02',status='ambiguous'),row('b','10:50','10:55')],events('ab'),{'rb':'11:00'})
        self.assertEqual(len(accepted),1)
    def test_fixed_definition_count(self):
        self.assertEqual(len(definitions()),16);self.assertEqual(sum(x['wide'] for x in definitions()),2)
if __name__=='__main__':unittest.main()
