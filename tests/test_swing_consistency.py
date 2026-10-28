import pathlib
import sys
import unittest

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
from research_swing_consistency import cluster, portfolio, recent_gate


def row(key, trigger, exit, net, status='closed', entry=None):
    return dict(event_key=key, symbol='RB0', warning_ts='2026-01-05 09:45:00', execution_contract='rb2605',
                result=dict(evaluation=dict(status=status,trigger_ts=trigger,entry_ts=entry or trigger,exit_ts=exit,net_amount=net)))


def events(keys):
    return {k:dict(symbol='RB0',direction='up',s0_ts='a',s1_ts='b') for k in keys}


class SwingConsistencyTests(unittest.TestCase):
    def test_delayed_fill_still_blocks_from_trigger(self):
        rs=[row('a','10:00','10:10',10,entry='10:03'),row('b','10:01','10:05',100)]
        accepted,ledger=portfolio(rs,events('ab'),{'rb2605':'11:00'})
        self.assertEqual([r['event_key'] for r in accepted],['a'])
        self.assertEqual(ledger[1]['reason'],'symbol_slot')

    def test_cluster_loss_uses_realized_outcome_only(self):
        rs=[row('a','10:00','10:10',-10),row('b','10:11','10:15',100)]
        normal,_=portfolio(rs,events('ab'),{'rb2605':'11:00'})
        stopped,ledger=portfolio(rs,events('ab'),{'rb2605':'11:00'},'stop_after_loss')
        self.assertEqual(len(normal),2)
        self.assertEqual(len(stopped),1)
        self.assertEqual(ledger[1]['reason'],'cluster_realized_loss')

    def test_unknown_occupies_until_contract_end(self):
        rs=[row('a','10:00','10:02',None,status='ambiguous'),row('b','10:50','10:55',100)]
        accepted,ledger=portfolio(rs,events('ab'),{'rb2605':'11:00'})
        self.assertEqual(len(accepted),1)
        self.assertEqual(ledger[0]['end'],'11:00')

    def test_same_minute_order_does_not_look_at_profit(self):
        rs=[row('a','10:00','10:10',-100),row('b','10:00','10:05',1000)]
        a,_=portfolio(rs,events('ab'),{'rb2605':'11:00'})
        b,_=portfolio(rs,events('ab'),{'rb2605':'11:00'},reverse=True)
        self.assertEqual(a[0]['event_key'],'a')
        self.assertEqual(b[0]['event_key'],'b')

    def test_retention_does_not_override_risk_gates(self):
        b=dict(n=100,net=100.,win_rate=.4,realized_cash_drawdown=50.,unknown=1,top3_product=.8,top3_day=.5,top5_trade=.4)
        m=dict(b,net=80.,top3_product=.7,top3_day=.4)
        self.assertTrue(recent_gate(m,b))
        self.assertFalse(recent_gate(dict(m,net=79.99),b))
        self.assertFalse(recent_gate(dict(m,unknown=2),b))
        self.assertFalse(recent_gate(dict(m,win_rate=.39),b))


if __name__=='__main__': unittest.main()
