import pathlib,sys,unittest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
from audit_swing_flat_ambiguity import ambiguity,exposure_portfolio
class FlatUnknown(unittest.TestCase):
    def row(self):
        d=dict(status='invalidated',entry_ts=None,trigger_ts=None,exit_ts='2026-01-05 10:01:00')
        c=dict(status='closed',entry_ts='2026-01-05 10:01:00',trigger_ts='2026-01-05 10:01:00',exit_ts='2026-01-05 10:01:00')
        return dict(event_key='0',symbol='RB0',execution_contract='rb',warning_ts='2026-01-05 09:45:00',result=dict(evaluation=dict(status='ambiguous',entry_ts=None,trigger_ts=None),disagreement=dict(ts='2026-01-05 10:01:00',open_high_low_close=d,open_low_high_close=c)))
    def test_flatness_does_not_turn_unknown_pnl_into_zero(self):
        r=self.row();a=ambiguity(r);self.assertEqual(a['end'],'2026-01-05 10:01:00');self.assertEqual(a['start'],a['end'])
        accepted,l=exposure_portfolio([r],{'rb':'2026-12-31 15:00:00'});self.assertEqual(accepted[0]['result']['evaluation']['status'],'ambiguous');self.assertTrue(l[0]['pnl_unknown'])
    def test_any_possible_open_keeps_unknown_exposure(self):
        r=self.row();r['result']['disagreement']['open_low_high_close']['status']='open'
        self.assertIsNone(ambiguity(r));_,l=exposure_portfolio([r],{'rb':'2026-12-31 15:00:00'});self.assertEqual(l[0]['end'],'2026-12-31 15:00:00')
    def test_next_minute_can_trade_but_same_minute_remains_reserved(self):
        r=self.row();rows=[r]
        for key,t in [('1','2026-01-05 10:01:00'),('2','2026-01-05 10:02:00')]:
            rows.append(dict(event_key=key,symbol='RB0',execution_contract='rb',warning_ts=t,result=dict(evaluation=dict(status='closed',entry_ts=t,trigger_ts=t,exit_ts='2026-01-05 10:03:00'))))
        a,_=exposure_portfolio(rows,{'rb':'2026-12-31 15:00:00'});self.assertEqual([r['event_key'] for r in a],['0','2'])
if __name__=='__main__':unittest.main()
