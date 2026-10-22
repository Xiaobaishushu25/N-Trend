"""Hourly forming-bar causality, next-open fills, session policy, and accounting."""
import sys
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import backtest_sr_dual_thrust_article as a


def bar(at,o,h=None,l=None,c=None):
    return a.base.Bar(datetime.fromisoformat(at),o,o if h is None else h,
                      o if l is None else l,o if c is None else c,100,1000)


WARMUP = [bar("2026-09-18 09:00:00",100,110,90,100),
          bar("2026-09-18 10:00:00",100,105,95,102),
          bar("2026-09-18 11:00:00",102,108,96,104),
          bar("2026-09-18 13:30:00",104,109,97,106)]


class ArticleExecutionTests(unittest.TestCase):
    def test_window_is_four_completed_hours_plus_current_open(self):
        completed=[(110,90,100),(105,95,102),(108,96,104),(109,97,106)]
        self.assertEqual(a.make_bands(completed,107,a.Config()),(17,158,73))
        self.assertIsNone(a.make_bands(completed[:3],107,a.Config()))

    def test_hour_bucket_keeps_recess_partial_hours_without_filling_gaps(self):
        self.assertEqual(a.hour_key(datetime(2026,9,21,13,30)),datetime(2026,9,21,13))
        self.assertEqual(a.hour_key(datetime(2026,9,21,10,45)),datetime(2026,9,21,10))

    def test_opposite_signal_closes_first_not_an_atomic_reversal(self):
        self.assertEqual(a.signal(1,90,110,95),'close')
        self.assertEqual(a.signal(0,90,110,95),'open_short')
        self.assertIsNone(a.signal(-1,90,110,95))

    def test_next_open_costs_and_explicit_terminal_trade(self):
        bars=WARMUP+[bar("2026-09-18 21:00:00",100,120,100,120),
                     bar("2026-09-18 21:01:00",121),
                     bar("2026-09-18 21:02:00",131)]
        s,ts,eq,_=a.replay(bars,a.Config(ks=1))
        self.assertEqual((len(ts),ts[0]['entry_base'],ts[0]['entry_fill'],ts[0]['exit_fill']),(1,121,122,130))
        self.assertEqual((s['gross_pnl'],s['slippage_cost'],s['fees'],s['net_pnl']),(100,20,6,74))
        self.assertEqual((eq[0]['equity'],eq[-1]['equity']),(0,74))
        self.assertEqual(ts[0]['exit_reason'],'terminal')
        self.assertEqual(s['terminal_trade_pnl'],74)
        self.assertEqual(a.replay(bars,a.Config())[0]['trades'],0)

    def test_future_high_and_low_do_not_change_hour_opening_bands(self):
        original=WARMUP+[bar("2026-09-18 21:00:00",100,120,100,101),
                         bar("2026-09-18 21:01:00",101)]
        altered=original[:-2]+[replace(original[-2],h=1000,l=1),original[-1]]
        self.assertEqual(a.replay(original,a.Config(ks=1))[3],a.replay(altered,a.Config(ks=1))[3])

    def test_intraday_flat_prevents_night_to_day_carry(self):
        bars=WARMUP+[bar("2026-09-18 21:00:00",100,120,100,120),
                     bar("2026-09-18 21:01:00",121),
                     bar("2026-09-18 22:58:00",125),
                     bar("2026-09-21 09:00:00",130),
                     bar("2026-09-21 14:59:00",131)]
        s,ts,eq,_=a.replay(bars,a.Config(ks=1,intraday=True))
        self.assertEqual(ts[0]['exit_bar'],'2026-09-18 22:58:00')
        self.assertEqual(ts[0]['exit_reason'],'session_flat')
        self.assertEqual(s['cross_session_trades'],0)
        self.assertEqual(eq[2]['position'],0)

    def test_real_fixture_cash_and_intraday_controls(self):
        if not (a.OUT/'article_source.json').exists():
            self.skipTest('No public article fixtures')
        minutes=a.prepare()
        for cfg in a.CONFIGS:
            s,ts,eq,_=a.replay(minutes,cfg)
            self.assertEqual(s['net_pnl'],sum(t['net_pnl'] for t in ts))
            self.assertEqual(s['net_pnl'],eq[-1]['equity'])
            self.assertEqual(s['fees'],6*len(ts))
            self.assertEqual(s['slippage_cost'],20*len(ts))
            if cfg.intraday:self.assertEqual(s['cross_session_trades'],0)

    def test_real_fixture_future_mutation_leaves_previous_equity_and_bands(self):
        if not (a.OUT/'article_source.json').exists():
            self.skipTest('No public article fixtures')
        minutes=a.prepare()
        cutoff=datetime(2026,10,13,10,20)
        prefix=[b for b in minutes if b.dt<cutoff]
        altered=[b if b.dt<cutoff else replace(b,h=b.h+1000,l=b.l-1000,c=b.c+500) for b in minutes]
        for cfg in [a.Config(),a.Config(ks=1),a.Config(ks=1,intraday=True)]:
            _,old_ts,old_eq,old_bands=a.replay(prefix,cfg,terminal=False)
            _,new_ts,new_eq,new_bands=a.replay(altered,cfg,terminal=False)
            self.assertEqual(old_eq,[r for r in new_eq if datetime.fromisoformat(r['bar_end'])<=cutoff])
            self.assertEqual(old_ts,[t for t in new_ts if datetime.fromisoformat(t['exit_bar'])<cutoff])
            self.assertEqual(old_bands,[b for b in new_bands if datetime.fromisoformat(b['first_observation'])<cutoff])


if __name__=='__main__':unittest.main()
