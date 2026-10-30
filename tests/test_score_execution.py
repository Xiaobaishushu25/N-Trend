import datetime as dt
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
from research_score_execution import (INF, audit_fill, block_difference, metrics, period_account, release,
                                      maximum_positions, replay, score_variants, top_cut)


def row(i, warning, end, score=75, symbol='T0', **changes):
    r = dict(id=i, candidate_key=f'{i:03d}', symbol=symbol, warning_ts=warning,
             scores=dict(v1=score), conditions=dict(planned_risk_yuan_per_lot=100),
             entry_ts=(dt.datetime.fromisoformat(warning) + dt.timedelta(minutes=5)).isoformat(' '),
             exit_ts=end, release_ts=end, uncertain_from=None,
             status='closed', fill=100, net_r=1, net_amount=100, risk_yuan_per_lot=100,
             commission=2, commission_r=.02, scenario='s1_1tick', tick=1, multiplier=10,
             direction='up', fills=[])
    r.update(changes)
    return r


class ExecutionTest(unittest.TestCase):
    def test_warning_selection_precedes_fill_outcome_and_same_time_prefers_high_score(self):
        a = row(1, '2026-09-29 10:00:00', '2026-09-30 11:00:00', status='invalidated', entry_ts=None)
        b = row(2, a['warning_ts'], '2026-09-29 11:00:00', score=80)
        c = row(3, '2026-09-29 10:30:00', '2026-09-29 10:45:00', score=99)
        d = row(4, '2026-09-29 11:00:00', '2026-09-29 11:30:00', symbol='T0')
        ledger = replay([a, b, c, d], 'v1', 70)
        self.assertEqual([r['id'] for r in ledger if r['decision'] == 'accepted'], [2, 4])
        self.assertEqual(ledger[2]['decision'], 'skipped_occupied')

    def test_history_warms_month_and_unknown_does_not_release_occupancy(self):
        a = row(1, '2026-09-29 10:00:00', '2026-09-30 10:00:00',
                status='ambiguous', release_ts=INF, uncertain_from='2026-09-30 10:00:00')
        b = row(2, '2026-09-30 11:00:00', '2026-09-30 12:00:00', score=99)
        ledger = replay([b, a], 'v1', 70)
        self.assertEqual(ledger[1]['decision'], 'unconfirmed_segment')
        self.assertEqual(ledger[1]['blocked_by'], 1)

    def test_both_terminal_paths_release_but_open_path_does_not(self):
        a = row(1, '2026-09-29 10:00:00', '2026-09-29 11:00:00', status='ambiguous')
        a['disagreement'] = dict(open_high_low_close=dict(status='closed', exit_ts=a['exit_ts']),
                                 open_low_high_close=dict(status='invalidated', exit_ts=a['exit_ts']))
        self.assertEqual(release(a), (a['exit_ts'], None))
        a['disagreement']['open_low_high_close']['status'] = 'open'
        self.assertEqual(release(a), (INF, a['exit_ts']))

    def test_budget_cancellation_releases_at_proposed_fill_not_counterfactual_exit(self):
        a = row(1, '2026-09-29 10:00:00', '2026-09-29 12:00:00',
                entry_ts='2026-09-29 10:20:00', risk_yuan_per_lot=200)
        b = row(2, '2026-09-29 10:10:00', '2026-09-29 11:00:00')
        c = row(3, '2026-09-29 10:30:00', '2026-09-29 11:00:00')
        ledger = replay([a, b, c], 'v1', 70, budget=150)
        self.assertEqual(ledger[0]['status'], 'risk_budget_cancelled_at_entry')
        self.assertEqual(ledger[0]['release_ts'], '2026-09-29 10:20:00')
        self.assertEqual(ledger[1]['decision'], 'skipped_occupied')
        self.assertEqual(ledger[2]['decision'], 'accepted')
        self.assertEqual(a['status'], 'closed')

    def test_budget_cannot_increase_warned_size_using_later_smaller_risk(self):
        a = row(1, '2026-09-29 10:00:00', '2026-09-29 12:00:00', risk_yuan_per_lot=50)
        self.assertEqual(replay([a], 'v1', 70, budget=150)[0]['lots'], 1)

    def test_ties_and_nonclosed_results_are_retained(self):
        rows = [row(i, '2026-09-29 10:00:00', '2026-09-29 12:00:00', score=s)
                for i, s in enumerate([1, 2, 3, 4, 5, 5])]
        self.assertEqual(sum(r['scores']['v1'] >= top_cut(rows, 'v1') for r in rows), 2)
        rows[0].update(status='ambiguous', net_r=None, net_amount=None)
        s = metrics(rows)
        self.assertEqual(s['signals'], 6)
        self.assertEqual(s['completed'], 5)
        self.assertEqual(s['mean_net_r'], 1)

    def test_cash_dates_carry_and_mark_deduct_costs_once(self):
        class Market:
            def quote(self, symbol, end):
                return dict(ts='2026-09-30 14:55:00', close=110)
        profile = dict(products=dict(T=dict(roundtrip_fixed_yuan_per_lot=2)))
        market = Market()
        market.calendar = None
        a = row(1, '2026-09-29 10:00:00', None, status='unfinished',
                entry_ts='2026-09-29 10:05:00', net_r=None, net_amount=None, release_ts=INF)
        b = row(2, '2026-09-29 11:00:00', '2026-09-30 11:00:00',
                entry_ts='2026-09-29 11:05:00', symbol='T0', net_amount=98)
        result = period_account([a, b], '2026-09-30 00:00:00', '2026-10-01 00:00:00', market, profile)
        self.assertEqual(result['known_cash_flow_yuan'], 99)  # Opening fee paid before month.
        self.assertEqual(result['gross_unrealized_yuan'], 100)
        self.assertEqual(result['estimated_exit_cost_yuan'], 11)
        self.assertEqual(result['unrealized_after_entry_exit_cost_yuan'], 88)
        self.assertEqual(result['ending_positions'][0]['valuation_ts'], '2026-09-30 14:55:00')

    def test_score_drop_is_flat_contribution_renormalization(self):
        raw = dict(quality=dict(trend_score=10, a_score=50, b_score=80, space_score=90),
                   scores=dict(quality_v1=57.2, quality_v2=51.55, a_v2=40, b_v2=60, levels=None))
        # v1 morph=49, quality=57.2; v2 morph=38.5 -> (30.8+13.5)/.95.
        raw['scores']['quality_v2'] = 44.3 / .95
        rules = dict(morphology_weights=[.25, .45, .3], quality_weights=[.8, .2])
        candidate = dict(morphology_weights=[.25, .45, .3], quality_weights=[.8, .15, .05])
        s = score_variants(raw, rules, candidate)
        self.assertAlmostEqual(s['v1_no_trend'], (57.2 - 2) / .8)
        self.assertAlmostEqual(s['v1_no_space'], 49)
        self.assertAlmostEqual(s['v2_no_levels'], s['v2'])

    def test_block_interval_has_no_artificial_advantage_against_itself(self):
        rows = [row(1, '2026-09-29 10:00:00', '2026-09-29 11:00:00')]
        ci = block_difference(rows, rows, {'2026-09-29'}, draws=20)
        self.assertEqual((ci['lower'], ci['upper']), (0, 0))

    def test_gap_loss_is_reconciled_without_one_r_cap(self):
        class Market:
            def window(self, symbol, start, end):
                return [dict(open=100, high=102, low=98, close=100) if start.endswith('05:00')
                        else dict(open=70, high=72, low=68, close=69)]
        r = row(1, '2026-09-29 10:00:00', '2026-09-29 11:00:00', entry_ts='2026-09-29 10:05:00',
                stop=90, fill=101, risk=11, risk_yuan_per_lot=110, net_amount=-322, net_r=-322/110,
                fills=[dict(offset='open', ts='2026-09-29 10:05:00', price=101, volume=1, reason='entry'),
                       dict(offset='close', ts='2026-09-29 11:00:00', price=69, volume=1, reason='stop')])
        self.assertAlmostEqual(audit_fill(r, Market())['net_r'], -322/110)
        self.assertLess(r['net_r'], -1.5)

    def test_target_gap_convention_is_explicit_not_arbitrary_outside_bar_fill(self):
        class Market:
            def window(self, symbol, start, end):
                return [dict(open=100, high=102, low=98, close=100) if start.endswith('05:00')
                        else dict(open=130, high=132, low=128, close=130)]
        r = row(1, '2026-09-29 10:00:00', '2026-09-29 11:00:00', entry_ts='2026-09-29 10:05:00',
                stop=90, target=120, fill=101, risk=11, risk_yuan_per_lot=110, net_amount=178, net_r=178/110,
                fills=[dict(offset='open', ts='2026-09-29 10:05:00', price=101, volume=1, reason='entry'),
                       dict(offset='close', ts='2026-09-29 11:00:00', price=119, volume=1, reason='target_s1')])
        self.assertEqual(len(audit_fill(r, Market())['conservative_target_gap_fills']), 1)
        r['target'] = 119
        with self.assertRaisesRegex(ValueError, 'outside OHLC'):
            audit_fill(r, Market())

    def test_unknown_position_is_not_marked_as_zero_or_released(self):
        class Market:
            def quote(self, symbol, end):
                raise AssertionError('must not fabricate a tail valuation after missing data')
        r = row(1, '2026-09-29 10:00:00', '2026-09-29 11:00:00', status='missing_data',
                uncertain_from='2026-09-29 11:00:00', release_ts=INF)
        result = period_account([r], '2026-09-29 00:00:00', '2026-10-01 00:00:00',
                                Market(), dict(products=dict(T=dict(roundtrip_fixed_yuan_per_lot=2))))
        self.assertFalse(result['equity_confirmed'])
        self.assertEqual(result['unknown_result_ids'], [1])
        self.assertEqual(result['ending_positions'], [])

    def test_concurrent_bound_includes_unknown_prior_positions(self):
        a = row(1, '2026-09-29 10:00:00', '2026-09-29 11:00:00',
                entry_ts='2026-09-29 10:05:00', status='missing_data',
                uncertain_from='2026-09-29 11:00:00', release_ts=INF)
        b = row(2, '2026-09-30 10:00:00', '2026-09-30 11:00:00',
                entry_ts='2026-09-30 10:05:00', symbol='U0')
        counts = maximum_positions([a, b], '2026-09-30 00:00:00', '2026-10-01 00:00:00')
        self.assertEqual(counts['confirmed_interval_positions_bar_order_upper_bound'], 1)
        self.assertEqual(counts['possible_positions_upper_bound'], 2)
        a['risk_budget_yuan'] = 1000
        counts = maximum_positions([a, b], '2026-09-30 00:00:00', '2026-10-01 00:00:00')
        self.assertIsNone(counts['possible_lots_upper_bound'])


if __name__ == '__main__':
    unittest.main()
