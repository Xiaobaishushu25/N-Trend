import copy
import datetime as dt
import pathlib
import sys
import unittest

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
from test_factor_study import snapshot,bar,calendar,CONFIG,SPEC
from factor_study_execution import evaluate,audit_label,MinuteBars
from factor_study_quote_audit import check_quote
from research_factor_study_entry import quote_evidence,matched_paths,candidate_status,bounded_release,guard_intervals


class EntryTimingTests(unittest.TestCase):
    def run_trade(self,bars,mode='close_confirm',s=None,slip=1,config=None,policy='s1',gaps=None):
        return evaluate(s or snapshot(),MinuteBars(bars),gaps or [],SPEC,calendar(),
                        dict(config or CONFIG,entry_mode=mode),policy,slip)

    def audited(self,s,bars,label):
        audit_label(s,label)
        quotes={b['ts']:b for b in bars};indices={b['ts']:i for i,b in enumerate(bars)}
        return check_quote(s,label,quotes[label['entry_ts']],quotes[label['exit_ts']],SPEC,
                           calendar(),quote_evidence(label,quotes,indices))

    def test_confirmation_enters_next_open_never_signal_close(self):
        bs=[bar(15,99,103,98,101),bar(16,102,111,101,110)]
        r=self.run_trade(bs)
        self.assertEqual(r['status'],'closed');self.assertEqual(r['entry'],103)
        self.assertEqual(r['confirmation_ts'],bs[0]['ts']);self.assertEqual(r['entry_ts'],bs[1]['ts'])
        self.audited(snapshot(),bs,r)

    def test_intraminute_breakout_without_close_is_not_confirmation(self):
        r=self.run_trade([bar(15,99,102,98,99),bar(16,99,103,98,99)])
        self.assertIsNone(r['entry']);self.assertIsNone(r['confirmation_ts'])

    def test_confirmation_at_end_waits_for_new_minute(self):
        r=self.run_trade([bar(15,99,103,98,101)])
        self.assertEqual(r['status'],'pending');self.assertIsNone(r['entry'])

    def test_confirmation_gap_below_stop_invalidates(self):
        r=self.run_trade([bar(15,99,103,98,101),bar(16,94,97,92,96)])
        self.assertEqual(r['status'],'invalidated');self.assertIsNone(r['entry'])

    def test_confirmation_can_enter_below_trigger_at_next_open(self):
        bs=[bar(15,99,103,98,101),bar(16,98,111,97,110)]
        r=self.run_trade(bs)
        self.assertEqual(r['entry'],99);self.audited(snapshot(),bs,r)

    def test_short_confirmation_symmetric(self):
        s=snapshot(direction='down');bs=[bar(15,101,102,98,99),bar(16,98,99,89,90)]
        r=self.run_trade(bs,s=s)
        self.assertEqual(r['entry'],97);self.assertEqual(r['reason'],'target');self.audited(s,bs,r)

    def test_retest_requires_prior_breakout(self):
        r=self.run_trade([bar(15,99,99,97,98),bar(16,98,99,96,98)],mode='retest25')
        self.assertIsNone(r['break_ts']);self.assertIsNone(r['entry'])

    def test_retest_quarter_rounds_away_from_trigger(self):
        bs=[bar(15,100,102,100,101),bar(16,99,99,98,98),bar(17,99,111,99,110)]
        r=self.run_trade(bs,mode='retest25')
        self.assertEqual(r['pullback_price'],98);self.assertEqual(r['entry'],99)
        self.assertEqual(r['reason'],'target');self.audited(snapshot(),bs,r)

    def test_retest_crosses_entry_before_stop_on_continuous_descent(self):
        bs=[bar(15,100,102,100,101),bar(16,99,99,94,94)]
        r=self.run_trade(bs,mode='retest25')
        self.assertEqual(r['status'],'closed');self.assertEqual(r['entry'],99)
        self.assertEqual(r['reason'],'stop');self.audited(snapshot(),bs,r)

    def test_retest_gap_already_past_stop_cancels(self):
        r=self.run_trade([bar(15,100,102,100,101),bar(16,94,97,92,96)],mode='retest25')
        self.assertEqual(r['status'],'invalidated');self.assertIsNone(r['entry'])

    def test_retest_gap_above_stop_fills_observed_open(self):
        bs=[bar(15,100,102,100,101),bar(16,97,111,96,110)]
        r=self.run_trade(bs,mode='retest25')
        self.assertEqual(r['entry'],98);self.audited(snapshot(),bs,r)

    def test_short_retest_symmetric(self):
        s=snapshot(direction='down')
        bs=[bar(15,100,100,98,99),bar(16,101,102,101,102),bar(17,101,101,89,90)]
        r=self.run_trade(bs,mode='retest25',s=s)
        self.assertEqual(r['pullback_price'],102);self.assertEqual(r['entry'],101)
        self.audited(s,bs,r)

    def test_same_minute_break_retest_order_uncertain(self):
        r=self.run_trade([bar(15,99,101,97,100)],mode='retest25')
        self.assertEqual(r['status'],'ambiguous');self.assertIsNone(r['net_yuan'])

    def test_peak_difference_below_follow_threshold_is_execution_equivalent(self):
        bs=[bar(15,100,100,98,99),bar(16,99,111,99,110)]
        r=self.run_trade(bs,mode='retest25')
        self.assertEqual(r['status'],'closed');self.audited(snapshot(),bs,r)

    def test_peak_difference_above_follow_threshold_is_execution_equivalent(self):
        r=self.run_trade([bar(15,100,102,98,101)],mode='retest25')
        self.assertEqual(r['status'],'unfinished')
        self.assertGreaterEqual(r['peak'],.5);self.assertGreater(r['peak_upper'],r['peak'])

    def test_different_follow_threshold_flags_remain_unknown(self):
        r=self.run_trade([bar(15,100,101,98,99)],mode='retest25')
        self.assertEqual(r['status'],'ambiguous');self.assertIsNone(r['net_yuan'])

    def test_retest_cannot_rescue_warning_risk_over_cap(self):
        s=snapshot();s['event']['stop']=40
        r=self.run_trade([bar(15,100,101,80,99)],mode='retest25',s=s)
        self.assertEqual(r['status'],'filtered_risk');self.assertFalse(r['fills'])

    def test_retest_that_rounds_to_stop_is_known_invalid(self):
        s=snapshot();s['event']['stop']=99
        r=self.run_trade([bar(15,100,111,98,110)],mode='retest25',s=s)
        self.assertEqual(r['status'],'invalid_target');self.assertEqual(r['release_ts'],s['warning_ts'])

    def test_confirmation_does_not_extend_pending_expiry(self):
        bs=[bar(m,99,103,98,101) for m in range(15,30)]
        # Only final close confirms. Earlier bars stay below the trigger.
        for b in bs[:-1]:b['close']=99
        r=self.run_trade(bs+[bar(30,102,111,101,110)],config=dict(CONFIG,pending_buckets=1))
        self.assertEqual(r['status'],'untriggered');self.assertIsNone(r['entry'])

    def test_missing_minute_after_confirmation_has_no_fill(self):
        r=self.run_trade([bar(15,99,103,98,101),bar(17,102,111,101,110)],gaps=['2026-08-03 09:16:00'])
        self.assertEqual(r['status'],'missing_data');self.assertFalse(r['fills'])

    def test_cash_target_is_same_under_stress_for_both_modes(self):
        cases={'close_confirm':[bar(15,99,103,98,101),bar(16,102,104,101,103),bar(17,103,140,102,139)],
               'retest25':[bar(15,100,102,100,101),bar(16,99,99,98,98),bar(17,99,140,99,139)]}
        for mode,bs in cases.items():
            a,b=[self.run_trade(bs,mode=mode,policy='cash200',slip=t) for t in (1,2)]
            self.assertEqual(a['target'],b['target']);self.assertLess(b['net_yuan'],a['net_yuan'])
            self.assertGreaterEqual(a['net_yuan'],200)

    def test_quote_audit_rejects_fabricated_confirmation(self):
        bs=[bar(15,99,103,98,101),bar(16,102,111,101,110)]
        r=self.run_trade(bs);quotes={b['ts']:b for b in bs};indices={b['ts']:i for i,b in enumerate(bs)}
        evidence=copy.deepcopy(quote_evidence(r,quotes,indices));evidence['confirmation']['close']=99
        with self.assertRaisesRegex(ValueError,'confirmation'):
            check_quote(snapshot(),r,bs[1],bs[1],SPEC,calendar(),evidence)

    def test_matched_paths_retains_unknown_instead_of_zero_cash(self):
        base=[dict(candidate_id='a',warning_ts='2026-08-03 09:15:00',decision='accepted',
                   label=dict(status='closed',net_yuan=-50))]
        r=matched_paths(base,{'a':dict(status='ambiguous',net_yuan=None)},'2026-08-01')
        self.assertEqual(r['both_closed'],0);self.assertEqual(r['status_transitions'],{'closed->ambiguous':1})

    def test_candidate_requires_coverage_profit_and_both_blocks(self):
        p=dict(minimum_closed=100,minimum_known_fraction=.95,fdr=.1)
        m=dict(closed=200,known_fraction=.99,net_yuan=1000)
        positive={str(b):dict(interval95=[10,2000]) for b in (5,10)}
        self.assertEqual(candidate_status(m,m,positive,positive,p,.01,.01),'development_candidate_only')
        self.assertEqual(candidate_status(m,dict(m,net_yuan=-1),positive,positive,p,.01,.01),'not_supported')
        self.assertEqual(candidate_status(dict(m,known_fraction=.9),m,positive,positive,p,.01,.01),'insufficient_priced_coverage')
        self.assertEqual(candidate_status(m,m,positive,positive,p,.01,.5),'no_supported_entry_improvement')

    def expiry_fixture(self):
        bs=[];start=dt.datetime(2026,8,3,9,15)
        for i in range(60):
            t=start+dt.timedelta(minutes=i)
            bs.append(dict(start=t.isoformat(sep=' '),ts=(t+dt.timedelta(minutes=1)).isoformat(sep=' '),
                           open=100,high=101,low=99,close=100))
        label=dict(status='ambiguous',release_ts=None,warning_ts=start.isoformat(sep=' '),net_yuan=None,
                   disagreement=dict(paths=[dict(status='open',entry=101,entry_bucket=1),
                                            dict(status='pending',entry=None,entry_bucket=None)]))
        return label,MinuteBars(bs),dict(CONFIG,pending_buckets=2,holding_buckets=3)

    def test_ambiguous_cash_remains_unknown_after_provable_latest_expiry(self):
        l,bs,c=self.expiry_fixture();r=bounded_release(l,bs,[],c)
        self.assertEqual(r['release_ts'],'2026-08-03 10:15:00')
        self.assertEqual(r['status'],'ambiguous');self.assertIsNone(r['net_yuan'])
        self.assertIsNone(l['release_ts']);self.assertEqual(r['capacity_bound']['last_bucket'],4)

    def test_ambiguous_expiry_requires_final_observed_bucket_close(self):
        l,bs,c=self.expiry_fixture()
        self.assertIsNone(bounded_release(l,MinuteBars(bs[:-1]),[],c)['release_ts'])

    def test_source_gap_prevents_expiry_proof(self):
        l,bs,c=self.expiry_fixture()
        self.assertIsNone(bounded_release(l,bs,['2026-08-03 09:50:00'],c)['release_ts'])

    def test_both_open_branches_have_earlier_expiry_bound(self):
        l,bs,c=self.expiry_fixture()
        l['disagreement']['paths'][1]=dict(status='open',entry=102,entry_bucket=1)
        self.assertEqual(bounded_release(l,bs,[],c)['release_ts'],'2026-08-03 10:00:00')

    def test_low_coverage_subset_cannot_produce_a_strategy_p_value(self):
        c={'5':dict(delta_total_yuan=1000,interval95=[100,2000],centered_bootstrap_p=.01)}
        r=guard_intervals(c,False)['5']
        self.assertNotIn('interval95',r);self.assertNotIn('centered_bootstrap_p',r)
        self.assertEqual(r['priced_subset_diagnostic']['centered_bootstrap_p'],.01)
        self.assertEqual(r['status'],'insufficient_priced_coverage')


if __name__=='__main__':unittest.main()
