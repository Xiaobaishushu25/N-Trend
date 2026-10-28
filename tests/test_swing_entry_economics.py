import copy,pathlib,sys,unittest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
from swing_entry_economics import warning_features,plan_features,allows,MODELS,eligible_rows

class EntryEconomicsTests(unittest.TestCase):
    def fixture(self):
        bars=[dict(datetime=f'2026-01-05 09:{m:02}:00',open=o,high=h,low=l,close=c) for m,o,h,l,c in [(0,100.,104.,98.,102.),(15,102.,110.,101.,109.),(30,109.,118.,108.,116.),(45,116.,117.,112.,114.)]]
        bars.append(dict(datetime='2026-01-05 10:00:00',open=114.,high=115.,low=110.,close=113.))
        e=dict(symbol='RB0',direction='up',s0_ts=bars[0]['datetime'],s1_ts=bars[2]['datetime'],warning_ts=bars[4]['datetime'],a_bars=3,b_bars=2,s0_price=98.,s1_price=118.,s2_price=110.,a_move=20.)
        p=dict(direction='up',entry=116.,stop=106.,target=136.,planned_risk=10.,new_friction=.2)
        return e,bars,p
    def test_identity_and_true_B_speed(self):
        e,b,p=self.fixture();f=warning_features(e,b);self.assertEqual(f['a_bars'],3);self.assertEqual(f['b_bars'],2);self.assertEqual(f['a_body_net_ex_s0'],14.);self.assertAlmostEqual(f['b_true_speed_to_A'],.6);self.assertEqual(f['b_true_extreme'],110.)
    def test_future_prices_and_outcome_payload_never_change_decision(self):
        e,b,p=self.fixture();f=warning_features(e,b);b.append(dict(datetime='2026-01-05 10:15:00',open=1000.,high=2000.,low=1.,close=1.));self.assertEqual(f,warning_features(e,b));q=copy.deepcopy(p);q['result']=dict(fill=1,net_amount=-999999.,status='closed');self.assertEqual(plan_features(p),plan_features(q));self.assertEqual([allows(n,f,plan_features(p)) for n in MODELS],[allows(n,f,plan_features(q)) for n in MODELS])
    def test_short_price_mirror_is_invariant(self):
        e,b,p=self.fixture();f=warning_features(e,b);e['direction']='down';e['s0_price']=240.-e['s0_price'];e['s1_price']=240.-e['s1_price'];e['s2_price']=240.-e['s2_price']
        for x in b:x.update(open=240.-x['open'],high=240.-x['low'],low=240.-x['high'],close=240.-x['close'])
        g=warning_features(e,b)
        for key in ['a_body_net_ex_s0','a_positive_session_gap_share','a_largest_abs_body_share','b_true_speed_to_A']:self.assertEqual(f[key],g[key])
    def test_duplicate_end_labels_resolve_from_bar_counts(self):
        e,b,p=self.fixture();fake=dict(b[2],high=115.,low=108.,close=114.);b.insert(2,fake);e['a_bars']=4;f=warning_features(e,b);self.assertEqual(f['a_bars'],4);self.assertEqual(f['b_bars'],2)
    def test_budget_depends_on_reward_space_not_stop_width_or_actual_fill(self):
        e,b,p=self.fixture();a=plan_features(p);self.assertAlmostEqual(a['cost_to_gross_space'],.1);q=dict(p,planned_risk=20.,new_friction=.1,stop=96.);self.assertEqual(a['cost_to_gross_space'],plan_features(q)['cost_to_gross_space']);self.assertTrue(allows('budget10',warning_features(e,b),a));q['target']=126.;self.assertFalse(allows('budget10',warning_features(e,b),plan_features(q)))
    def test_gap_majority_only_counts_known_aligned_session_gaps(self):
        e,b,p=self.fixture();b[1]['datetime']='2026-01-05 13:30:00';b[2]['datetime']='2026-01-05 13:45:00';b[3]['datetime']='2026-01-05 14:00:00';b[4]['datetime']='2026-01-05 14:15:00';b[1]['open']=114.;b[1]['high']=115.;e['s1_ts']=b[2]['datetime'];e['warning_ts']=b[4]['datetime'];f=warning_features(e,b);self.assertAlmostEqual(f['a_positive_session_gap_share'],.6);self.assertFalse(allows('no_gap_majority',f,plan_features(p)))
    def test_filter_recomputes_competition_instead_of_deleting_old_accepted_trades(self):
        from research_swing_consistency import portfolio
        def row(k,start,end):return dict(event_key=k,symbol='RB0',warning_ts=start,execution_contract='RB2605',result=dict(evaluation=dict(trigger_ts=start,entry_ts=start,exit_ts=end,status='closed',net_amount=-100.)))
        a=row('a','2026-01-05 09:01:00','2026-01-05 09:10:00');b=row('b','2026-01-05 09:02:00','2026-01-05 09:05:00');events={k:dict(symbol='RB0',direction='up',s0_ts=k,s1_ts=k) for k in ['a','b']};ends={'RB2605':'2026-01-05 15:00:00'}
        base,_=portfolio([a,b],events,ends);self.assertEqual([x['event_key'] for x in base],['a']);selected=eligible_rows([a,b],{'a':{'model':False},'b':{'model':True}},'model');changed,_=portfolio(selected,events,ends);self.assertEqual([x['event_key'] for x in changed],['b'])

if __name__=='__main__':unittest.main()
