import copy,pathlib,sys,unittest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
from swing_portfolio_balance import select
from research_swing_consistency import portfolio

class BalanceTests(unittest.TestCase):
    def row(self,key,sym,start,end,net=-20.,reason='stop',direction='up',status='closed'):
        return dict(event_key=key,symbol=sym,direction=direction,warning_ts=start,execution_contract=sym,planned_risk=10.,multiplier=10.,result=dict(evaluation=dict(trigger_ts=start,entry_ts=start,exit_ts=end,status=status,net_amount=net,exit_reason=reason)))
    def inputs(self,rows):
        events={r['event_key']:dict(symbol=r['symbol'],direction=r['direction'],s0_ts='same0',s1_ts='same1') for r in rows};ends={r['execution_contract']:'2026-01-05 15:00:00' for r in rows};sectors={r['symbol']:'chemical' for r in rows};return events,ends,sectors
    def test_control_exact_and_sector_only_blocks_live_same_direction(self):
        rows=[self.row('a','A','2026-01-05 09:01:00','2026-01-05 09:10:00'),self.row('b','B','2026-01-05 09:02:00','2026-01-05 09:05:00'),self.row('c','C','2026-01-05 09:03:00','2026-01-05 09:07:00',direction='down'),self.row('d','D','2026-01-05 09:11:00','2026-01-05 09:14:00')];e,x,g=self.inputs(rows);a,_=portfolio(rows,e,x);b,_,_=select(rows,e,x,g);self.assertEqual(a,b);b,_,risk=select(rows,e,x,g,'sector1_same');self.assertEqual([r['event_key'] for r in b],['a','c','d']);self.assertEqual(risk['peak_symbol_reservations'],2)
    def test_future_loss_is_not_visible_and_equal_timestamp_still_occupied(self):
        rows=[self.row('a','A','2026-01-05 09:01:00','2026-01-05 09:10:00'),self.row('b','A','2026-01-05 09:10:00','2026-01-05 09:12:00'),self.row('c','A','2026-01-05 09:11:00','2026-01-05 09:13:00')];e,x,g=self.inputs(rows);a,l,risk=select(rows,e,x,g,'after_loss');self.assertEqual([r['event_key'] for r in a],['a']);self.assertEqual(l[1]['visible_cluster_net'],0);self.assertEqual(l[1]['reason'],'symbol_slot');self.assertEqual(l[2]['visible_cluster_net'],-20.);self.assertEqual(l[2]['reason'],'cluster_realized_loss');self.assertLess(risk['realized_settlements'][0]['exit_ts'],risk['realized_settlements'][0]['visible_before'])
    def test_stop_budget_does_not_treat_no_follow_loss_as_stop(self):
        rows=[self.row('a','A','2026-01-05 09:01:00','2026-01-05 09:02:00',reason='no_follow'),self.row('b','A','2026-01-05 09:03:00','2026-01-05 09:05:00')];e,x,g=self.inputs(rows);a,_,_=select(rows,e,x,g,'after_stop');self.assertEqual(len(a),2);a,_,_=select(rows,e,x,g,'after_loss');self.assertEqual(len(a),1)
    def test_net_budget_uses_cumulative_realized_not_any_past_loss(self):
        rows=[self.row('a','A','2026-01-05 09:01:00','2026-01-05 09:02:00',net=50.,reason='target_s1'),self.row('b','A','2026-01-05 09:03:00','2026-01-05 09:04:00',net=-20.),self.row('c','A','2026-01-05 09:05:00','2026-01-05 09:06:00')];e,x,g=self.inputs(rows);a,_,_=select(rows,e,x,g,'after_loss');self.assertEqual(len(a),3)
    def test_unknown_reserves_sector_and_pnl_is_not_imputed(self):
        rows=[self.row('a','A','2026-01-05 09:01:00','2026-01-05 09:02:00',net=None,status='ambiguous'),self.row('b','B','2026-01-05 09:03:00','2026-01-05 09:04:00')];e,x,g=self.inputs(rows);a,l,risk=select(rows,e,x,g,'sector1_same');self.assertEqual(len(a),1);self.assertEqual(l[0]['end'],x['A']);self.assertFalse(risk['realized_settlements'])
    def test_two_flat_paths_release_sector_without_inserting_unknown_profit(self):
        a=self.row('a','A','2026-01-05 09:01:00','2026-01-05 09:02:00',net=None,status='ambiguous');a['result']['disagreement']=dict(ts='2026-01-05 09:02:00',open_high_low_close=dict(status='closed',entry_ts='2026-01-05 09:01:00',exit_ts='2026-01-05 09:02:00'),open_low_high_close=dict(status='invalidated',entry_ts=None,exit_ts='2026-01-05 09:02:00'));b=self.row('b','B','2026-01-05 09:03:00','2026-01-05 09:04:00');e,x,g=self.inputs([a,b]);z,l,risk=select([a,b],e,x,g,'sector1_same',policy='two_paths_flat');self.assertEqual(len(z),2);self.assertTrue(l[0]['two_paths_flat']);self.assertFalse(risk['realized_settlements'])
    def test_new_parent_identity_can_retry_and_no_period_reset(self):
        rows=[self.row('a','A','2026-06-30 14:01:00','2026-06-30 14:02:00'),self.row('b','A','2026-07-01 09:03:00','2026-07-01 09:04:00')];e,x,g=self.inputs(rows);x['A']='2026-07-15 15:00:00';a,_,_=select(rows,e,x,g,'after_loss');self.assertEqual(len(a),1);e['b']['s0_ts']='new0';a,_,_=select(rows,e,x,g,'after_loss');self.assertEqual(len(a),2)

if __name__=='__main__':unittest.main()
