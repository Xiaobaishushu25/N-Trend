import pathlib,sys,unittest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
from research_swing_cycle import online_gate

class CausalGate(unittest.TestCase):
    def rows(self):
        rows=[];events={}
        for i,net in enumerate([-1.,-1.,2.,2.,1.]):
            key=str(i);start=f'2026-01-05 10:{i*2:02}:00';end=f'2026-01-05 10:{i*2+1:02}:00'
            rows.append(dict(event_key=key,period='early' if i<3 else 'recent',symbol='RB0',execution_contract='rb',warning_ts=start,result=dict(evaluation=dict(status='closed',trigger_ts=start,entry_ts=start,exit_ts=end,net_r=net,net_amount=net))))
            events[key]=dict(symbol='RB0',direction='up',s0_ts='a',s1_ts='b')
        return rows,events,{'rb':'2026-12-31 15:00:00'}
    def test_shadow_keeps_learning_when_live_off_and_crosses_period(self):
        rows,events,ends=self.rows();a,l,s=online_gate(rows,events,ends,2)
        self.assertEqual([r['event_key'] for r in a],['3','4'])
        self.assertEqual(len(s),5);self.assertEqual(l[3]['past_shadow_closed'],3)
        self.assertEqual(l[3]['past_window_r'],.5)
    def test_future_outcome_cannot_change_past_decisions(self):
        rows,events,ends=self.rows();_,before,_=online_gate(rows,events,ends,2)
        rows[-1]['result']['evaluation']['net_r']=-99999.
        _,after,_=online_gate(rows,events,ends,2);self.assertEqual(before,after)
    def test_equal_exit_timestamp_is_not_yet_available(self):
        rows,events,ends=self.rows();rows[2]['result']['evaluation']['exit_ts']=rows[3]['result']['evaluation']['trigger_ts']
        _,ledger,_=online_gate(rows,events,ends,2)
        self.assertEqual(ledger[3]['past_shadow_closed'],2);self.assertFalse(ledger[3]['accepted'])
    def test_overlapping_paper_signal_is_not_a_shadow_result(self):
        rows,events,ends=self.rows();rows[0]['result']['evaluation']['exit_ts']='2026-01-05 10:03:30'
        _,ledger,shadow=online_gate(rows,events,ends,2)
        self.assertNotIn('1',[r['event_key'] for r in shadow]);self.assertEqual(ledger[2]['past_shadow_closed'],1)

if __name__=='__main__':unittest.main()
