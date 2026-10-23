import pathlib
import sys
import unittest

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
from report_profit_concentration import concentration, gap_attribution, gap_index


def row(direction='up',entry='2026-09-30 14:20:00',reason='target_s1',price=119.):
    return dict(direction=direction,multiplier=10.,tick_size=1.,result=dict(
        evaluation=dict(status='closed',volume=1,entry_ts=entry,exit_ts='2026-10-08 09:05:00',
            exit_reason=reason,risk=10.,net_amount=180.),
        fills=[dict(price=101.),dict(price=price)]))


def gap(previous=100.,opened=150.):
    return dict(kind='holiday',previous_ts='2026-09-30 15:00:00',next_ts='2026-10-08 09:05:00',
        previous_close=previous,next_open=opened)


class ProfitConcentrationTests(unittest.TestCase):
    def test_positive_and_net_profit_denominators_are_not_confused(self):
        c=concentration([100.,50.,-120.])
        self.assertEqual(c['net'],30.)
        self.assertAlmostEqual(c['top']['1']['share_of_positive_profit'],2/3)
        self.assertAlmostEqual(c['top']['1']['share_of_net'],10/3)
        self.assertEqual(c['top']['5']['net_without'],-120.)
        self.assertEqual(c['largest_winners_needed_to_erase_net'],1)

    def test_favorable_reopening_gap_is_capped_at_target_before_slippage(self):
        g=gap_attribution(row(),[gap()],1)
        self.assertEqual(g['signed_gap_cash']['holiday'],200.)
        self.assertTrue(g['components'][0]['target_capped'])
        self.assertEqual(g['residual_after_fees_and_slippage'],-20.)

    def test_short_target_and_adverse_stop_use_correct_sign_and_fill(self):
        g=gap_attribution(row('down',price=81.),[gap(100.,50.)],1)
        self.assertEqual(g['signed_gap_cash']['holiday'],200.)
        g=gap_attribution(row(reason='stop',price=79.),[gap(100.,80.)],1)
        self.assertEqual(g['signed_gap_cash']['holiday'],-200.)
        self.assertFalse(g['components'][0]['target_capped'])

    def test_new_position_after_reopening_gets_no_holiday_gap_profit(self):
        g=gap_attribution(row(entry='2026-10-08 09:05:00'),[gap()],1)
        self.assertFalse(g['holiday_carry'])
        self.assertEqual(g['signed_gap_cash']['holiday'],0.)

    def test_known_holiday_and_ordinary_weekend_remain_distinct(self):
        raw={'X':dict(bars=[dict(datetime=t,open=100.,close=100.) for t in [
            '2026-09-24 15:00:00','2026-09-28 09:05:00','2026-10-09 23:00:00','2026-10-12 09:05:00']])}
        windows=[dict(name='mid-autumn',last_close='2026-09-24 15:00:00',reopen='2026-09-28 09:00:00')]
        gaps=gap_index(raw,windows)['X']
        self.assertEqual(gaps[0]['kind'],'holiday')
        self.assertTrue(gaps[0]['exact_holiday_boundary'])
        self.assertEqual(gaps[-1]['kind'],'weekend')


if __name__=='__main__':unittest.main()
