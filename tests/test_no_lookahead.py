"""S0 acceptance regressions; synthetic fixtures never access strategy returns."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from s0_engine import Bar, Order, Spec, Engine, completed_aggregate


def bar(t=300, contract='DCE.c2601', **kw):
    args = dict(start=t, end=t+300, contract=contract, trading_day='2026-11-04',
                session='day', session_end=1800, open=100, high=102, low=99, close=101)
    args.update(kw)
    return Bar(**args)


def spec(contract='DCE.c2601', **kw):
    args = dict(contract=contract, valid_from=0, valid_to=100000, multiplier=10,
                tick=1, margin_rate=.1, open_fee=2, today_fee=3, yesterday_fee=4,
                source='synthetic hand ledger')
    args.update(kw)
    return Spec(**args)


def order(contract='DCE.c2601', **kw):
    args = dict(id='o1', contract=contract, signal_end=300, side=1, stop=90, target=120)
    args.update(kw)
    return Order(**args)


class S0Tests(unittest.TestCase):
    def test_never_fill_signal_bar(self):
        e=Engine([spec()]); e.submit(order())
        e.step([bar(0)])
        self.assertFalse(e.positions)
        e.step([bar()]); self.assertEqual(e.fills[0]['time'], 300)

    def test_stop_first_and_gap(self):
        for b, expected in [(bar(high=130, low=80),89), (bar(600,open=80,high=130,low=79),79)]:
            e=Engine([spec()]); e.submit(order())
            if b.start==600:e.step([bar()])
            e.step([b]); self.assertEqual(e.trades[0]['exit'], expected)
            self.assertEqual(e.trades[0]['reason'],'stop')

    def test_future_mutation_prefix(self):
        def run(future):
            e=Engine([spec()]); e.submit(order()); e.step([bar()])
            prefix=(list(e.fills),list(e.snapshots))
            e.step([future]); return prefix
        self.assertEqual(run(bar(600)),run(bar(600,high=1000,low=1)))

    def test_completed_multitimeframe_and_signal_prefix(self):
        prefix=[bar(t,session_end=7200) for t in range(0,3600,300)]
        for period in (900,1800,3600):
            self.assertEqual(completed_aggregate(prefix,period,3599),completed_aggregate(prefix[:-1],period,3599))
            self.assertEqual(completed_aggregate(prefix,period,3600),completed_aggregate(prefix+[bar(3600,high=1000,session_end=7200)],period,3600))
        def run(bars):
            e=Engine([spec()]);signals=[]
            for b in bars:
                e.step([b])
                if b.close>b.open:
                    signals.append(b.end);e.submit(order(id=str(b.end),signal_end=b.end))
            return signals[:3], [x for x in e.fills if x['time']<900]
        self.assertEqual(run(prefix[:3]),run(prefix[:3]+[bar(900,high=1000,low=1,session_end=7200)]))

    def test_missing_bucket_and_roll_not_aggregated(self):
        self.assertEqual(completed_aggregate([bar(0),bar(600)],900,900),[])
        self.assertEqual(completed_aggregate([bar(0),bar(300),bar(600,'DCE.c2605')],900,900),[])

    def test_future_intrabar_profit_cannot_fund_simultaneous_open(self):
        cs=['DCE.c2601','DCE.eg2601']
        e=Engine([spec(c,margin_rate=25) for c in cs])
        e.submit(order());e.step([bar()])
        e.submit(order(cs[1],id='second',signal_end=600))
        e.step([bar(600,high=130),bar(600,cs[1])])
        self.assertEqual(e.rejections[-1]['reason'],'cash')

    def test_fee_effective_dates_and_close_yesterday(self):
        e=Engine([spec(valid_to=600),spec(valid_from=600,today_fee=8,yesterday_fee=9)])
        e.submit(order());e.step([bar()]);e.step([bar(600,high=130,trading_day='2026-11-05')])
        self.assertEqual(e.trades[0]['fees'],11)

    def test_night_disabled_and_metadata_expiry(self):
        e=Engine([spec()]);e.submit(order());e.step([bar(night=True)])
        self.assertEqual(e.rejections[0]['reason'],'night_disabled')
        e=Engine([spec(valid_to=300)]);e.submit(order());e.step([bar()])
        self.assertEqual(e.rejections[0]['reason'],'metadata')

    def test_daily_loss_halts_and_liquidates_at_available_open(self):
        e=Engine([spec(multiplier=20),spec('DCE.eg2601')])
        e.submit(order());e.step([bar()])
        e.submit(order('DCE.eg2601',id='second',signal_end=600))
        e.step([bar(600,open=40,high=41,low=39,close=40),bar(600,'DCE.eg2601')])
        self.assertTrue(e.halted)
        self.assertEqual(e.rejections[-1]['reason'],'risk_halt')
        self.assertGreater(-e.trades[0]['net'],900)

    def test_calendar_friday_midnight_holiday_and_unknown(self):
        import datetime as dt
        from factor_study_data import Calendar
        records=[dict(date=(dt.date(2026,9,25)+dt.timedelta(days=i)).isoformat(),trading=i in (0,3,4,5)) for i in range(16)]
        cal=Calendar(dict(records=records),{'PB0':60})
        self.assertEqual(cal.day('2026-09-25 21:00:00'),'2026-09-28')
        self.assertEqual(cal.day('2026-09-26 00:30:00'),'2026-09-28')
        self.assertTrue(cal.expected(dt.datetime(2026,9,26,0,30),'PB0'))
        self.assertFalse(cal.expected(dt.datetime(2026,9,30,21,0),'PB0'))
        self.assertFalse(cal.expected(dt.datetime(2026,10,1,9,0),'PB0'))
        with self.assertRaises(ValueError):cal.is_day(dt.date(2025,1,1))

    def test_risk_margin_cash_and_duplicate(self):
        for kwargs, skw, cash, reason in [({'stop':1},{},50000,'lot_risk'),
                ({},{'margin_rate':40},50000,'margin'), ({},{},50,'cash')]:
            e=Engine([spec(**skw)],capital=cash);e.submit(order(**kwargs));e.step([bar()])
            self.assertEqual(e.rejections[0]['reason'],reason)
        e=Engine([spec()]);e.submit(order());e.submit(order());e.step([bar()])
        self.assertEqual(len(e.positions),1)
        self.assertEqual(e.rejections[0]['reason'],'duplicate_order')

    def test_shared_account_900(self):
        cs=['DCE.c2601','DCE.eg2601','DCE.l2601']
        e=Engine([spec(c) for c in cs],trade_budget=600)
        for c in reversed(cs): e.submit(order(c,id=c,stop=65))
        e.step([bar(contract=c) for c in reversed(cs)])
        self.assertEqual(len(e.positions),2)
        self.assertEqual(e.rejections[-1]['reason'],'concurrent_risk')
        self.assertLessEqual(e.snapshots[-1]['risk'],900)

    def test_missing_bar_does_not_flatten_using_future(self):
        e=Engine([spec()]);e.submit(order());e.step([bar()]);e.step([bar(900)])
        self.assertTrue(e.invalid_evidence)
        self.assertEqual(e.trades,[])

    def test_roll_never_fills_new_contract_price(self):
        e=Engine([spec(),spec('DCE.c2605')]);e.submit(order());e.step([bar()])
        e.step([bar(600,'DCE.c2605',open=500,high=501,low=499,close=500)])
        self.assertEqual(e.trades,[])
        self.assertEqual(e.positions['DCE.c2601'].entry,101)

    def test_locked_limit_no_fake_exit(self):
        e=Engine([spec()]);e.submit(order());e.step([bar()])
        e.step([bar(600,open=80,high=80,low=80,close=80,executable=False)])
        self.assertEqual(e.trades,[]); self.assertTrue(e.positions)
        e.step([bar(900,open=75,high=76,low=74,close=75)])
        self.assertEqual(e.trades[0]['exit'],74)

    def test_session_flat_and_late_entry(self):
        e=Engine([spec()]); e.submit(order());e.step([bar(1200)])
        self.assertFalse(e.positions)
        e=Engine([spec()]);e.submit(order());e.step([bar()]);e.step([bar(1500)])
        self.assertEqual(e.trades[0]['reason'],'session_flat')

    def test_unknown_metadata_and_limit_order_fail_closed(self):
        e=Engine([]);e.submit(order());e.step([bar()])
        self.assertEqual(e.rejections[0]['reason'],'metadata')
        e=Engine([spec()]);e.submit(order(kind='limit'));e.step([bar()])
        self.assertEqual(e.rejections[0]['reason'],'limit_queue_unknown')

    def test_twelve_hand_calculated_trades(self):
        # entry 101/99, target 120/80, exit 119/81; gross 180, fees 2+3, net 175.
        e=Engine([spec()]);expected=50000
        for i in range(12):
            t=300+i*1200;side=1 if i%2==0 else -1
            e.submit(order(id=str(i),signal_end=t,side=side,stop=90 if side==1 else 110,target=120 if side==1 else 80))
            e.step([bar(t,session_end=t+1200)])
            self.assertAlmostEqual(e.balance,expected-2)
            e.step([bar(t+300,session_end=t+1200,high=121 if side==1 else 102,low=99 if side==1 else 79)])
            expected+=175
            self.assertAlmostEqual(e.balance,expected)
            self.assertAlmostEqual(e.trades[-1]['net'],175)
            self.assertEqual(e.snapshots[-1]['margin'],0)
        self.assertEqual(len(e.trades),12)


if __name__=='__main__':unittest.main()
