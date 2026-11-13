"""Behavioral regressions against n-core execution and a production-policy oracle."""
from datetime import date
import json
from pathlib import Path
import random
import subprocess
from types import SimpleNamespace
import unittest

import pandas as pd

from backtest_n_pattern import (
    BacktestConfig, NPatternBacktestEngine, QuoteTick, TqOrderAdapter,
    completed_tq_bars, compute_slippage_limit_price, quote_timestamp, round_to_tick,
    AccountAnalytics, _collect_comm_by_order, main,
)


def make_engine(**overrides):
    config = dict(symbols=['RB0', 'EG0'], start=date(2026, 10, 19), end=date(2026, 11, 11),
                  quiet=True, slippage_ticks=0)
    config.update(overrides)
    return NPatternBacktestEngine(BacktestConfig(**config))


def signal(engine, direction='up', symbol='RB0', actual='SHFE.rb2611', index=50, target=None):
    sign = 1 if direction == 'up' else -1
    return engine.ingest_signals(symbol, '2026-10-19 09:30:00', actual, 1., 10., 50000., [dict(
        direction=direction, entry_score=4., entry=100., stop=100-sign*10, risk=10.,
        target=target or 100+sign*10, warning_index=index, warning_ts='2026-10-19 09:30:00',
        s0_ts='2026-10-19 09:00:00', s1_ts='2026-10-19 09:15:00', s2_ts='2026-10-19 09:30:00',
    )])[0]


def quote(price=100., ask=None, bid=None, seconds=0):
    return QuoteTick('SHFE.rb2611', f'2026-10-19 09:31:{seconds:02d}', price,
                     price+1 if ask is None else ask, price-1 if bid is None else bid,
                     upper_limit=200., lower_limit=1.)


class FakeApi:
    def __init__(self):
        self.orders = []
        self.cancelled = []

    def insert_order(self, **kwargs):
        order = SimpleNamespace(**kwargs, order_id=f'O{len(self.orders)}', status='ALIVE',
                                volume_orign=kwargs['volume'], volume_left=kwargs['volume'],
                                trade_price=float('nan'), last_msg='')
        self.orders.append(order)
        return order

    def cancel_order(self, order):
        self.cancelled.append(order.order_id)

    def get_position(self, symbol):
        return SimpleNamespace(volume_long_his=0, volume_long_today=2,
                               volume_short_his=0, volume_short_today=2)


class TestLiveBehavior(unittest.TestCase):
    def test_market_gap_requires_fresh_quotes_and_preserves_exit_reason(self):
        eng = make_engine()
        cond = signal(eng)
        pos = eng.on_open_filled(cond.cond_id, 2, 100., quote().datetime)
        eng.on_market_gap(quote(seconds=5).datetime, [])
        self.assertFalse(eng.on_quote(quote(89., seconds=4)))
        self.assertEqual(eng.on_quote(quote(89., seconds=6))[0].reason, 'market_gap')
        self.assertTrue(pos.closing_all_sent)

    def test_fill_racing_abort_still_exits_after_rebound(self):
        eng = make_engine()
        cond = signal(eng)
        eng.on_quote(quote())
        eng.on_quote(quote(89., seconds=1))
        pos = eng.on_open_filled(cond.cond_id, 2, 100., quote(seconds=2).datetime)
        self.assertEqual(pos.pending_exit_reason, 'stop')
        self.assertEqual(eng.on_quote(quote(101., seconds=3))[0].reason, 'stop')

    def test_equity_storage_bounded_but_drawdown_remains_tick_exact(self):
        analytics = AccountAnalytics(100.)
        analytics.record_equity('2026-10-19 09:31:00', 110.)
        analytics.record_equity('2026-10-19 09:31:01', 80.)
        analytics.record_equity('2026-10-19 09:31:02', 105.)
        self.assertEqual(len(analytics.equity_points), 1)
        self.assertEqual(analytics.max_drawdown_yuan, 30.)

    def test_multiple_intraday_commissions_same_order_are_summed(self):
        sim = SimpleNamespace(trade_log={}, _trades=[{'order_id': 'O1', 'commission': 3.},
                                                    {'order_id': 'O1', 'commission': 4.}])
        self.assertEqual(_collect_comm_by_order(sim), {'O1': 7.})

    def test_live_config_does_not_activate_unused_percent_risk_cap(self):
        from unittest.mock import patch
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.json'
            config.write_text(json.dumps(dict(mode='FullAuto', slippage_ticks=3, order_wait_seconds=5,
                default_large_volume=4, default_small_volume=2, max_risk_percent=1.0)), encoding='utf-8')
            with patch('backtest_n_pattern.run_tq_backtest', return_value={}) as run, \
                    patch('backtest_n_pattern.print_report'):
                main(['--symbols', 'RB0', '--start', '2026-10-19', '--end', '2026-10-20',
                      '--live-config', str(config), '--slippage-ticks', '2'])
            cfg = run.call_args.args[0]
            self.assertEqual(cfg.slippage_ticks, 2)
            self.assertEqual(cfg.large_volume, 4)
            self.assertEqual(cfg.order_wait_seconds, 5)
            self.assertIsNone(cfg.max_risk_pct)
            self.assertEqual(cfg.quote_mode, 'tick')

    def test_fill_risk_target_and_both_r_denominators(self):
        for direction, fill, stop_price, threshold in [('up', 102., 90., 114.), ('down', 98., 110., 86.)]:
            with self.subTest(direction=direction):
                eng = make_engine()
                cond = signal(eng, direction)
                pos = eng.on_open_filled(cond.cond_id, 2, fill, quote().datetime)
                self.assertEqual(pos.risk, 12.)
                self.assertEqual(pos.planned_risk, 10.)
                self.assertEqual(pos.target, threshold)
                self.assertFalse(eng.on_quote(quote(fill+(10 if direction == 'up' else -10))))
                self.assertEqual(eng.on_quote(quote(threshold))[0].reason, 'target_1r')
                leg = eng.on_close_filled(pos.pos_id, 2, stop_price, quote().datetime, 'stop', False)
                self.assertEqual(leg['r_multiple'], -1.)
                self.assertEqual(leg['planned_r_multiple'], -1.2)

    def test_structural_target_is_preserved(self):
        eng = make_engine()
        cond = signal(eng, target=140.)
        pos = eng.on_open_filled(cond.cond_id, 2, 102., quote().datetime)
        self.assertEqual(pos.target, 140.)

    def test_chase_wait_then_retrace_still_authorized_and_stop_aborts(self):
        eng = make_engine()
        cond = signal(eng)
        self.assertFalse(eng.on_quote(quote(110., ask=111.)))
        self.assertTrue(cond.entry_authorized)
        # Live Triggered entries retain authorization even below the breakout.
        self.assertEqual(eng.on_quote(quote(99., ask=100., seconds=1))[0].intent, 'open')
        eng.on_quote(quote(89., seconds=2))
        self.assertEqual(cond.state, 'cancelled')
        self.assertFalse(eng.on_quote(quote(100., seconds=3)))

    def test_untriggered_stop_is_not_shadow_invalidation(self):
        eng = make_engine()
        cond = signal(eng)
        self.assertFalse(eng.on_quote(quote(89.)))
        self.assertEqual(cond.state, 'pending')
        self.assertTrue(eng.on_quote(quote(100., seconds=1)))

    def test_funding_requires_actual_margin_release_and_new_confirmation(self):
        eng = make_engine()
        cond = signal(eng)
        eng.on_quote(quote())
        at = quote_timestamp(quote().datetime)
        eng.update_funds(100., 1000., at)
        eng.on_open_rejected(cond.cond_id, '资金不足', quote().datetime)
        eng.update_funds(200., 1000., at+3)  # Floating profit alone is insufficient.
        self.assertFalse(eng.on_quote(quote(seconds=3)))
        eng.update_funds(1000., 100., at+4)
        self.assertTrue(eng.on_quote(quote(seconds=4)))

    def test_numeric_funding_shortfall_and_backoff(self):
        eng = make_engine()
        cond = signal(eng)
        eng.on_quote(quote())
        at = quote_timestamp(quote().datetime)
        eng.update_funds(100., 1000., at)
        eng.on_open_rejected(cond.cond_id, 'CTP:[31]资金不足，资金[200.5]', quote().datetime)
        eng.update_funds(300.50, 1000., at+3)
        self.assertFalse(eng.on_quote(quote(seconds=3)))
        eng.update_funds(300.51, 1000., at+4)
        self.assertTrue(eng.on_quote(quote(seconds=4)))

    def test_timer_idempotence_and_latched_bar_exit(self):
        eng = make_engine()
        cond = signal(eng)
        pos = eng.on_open_filled(cond.cond_id, 2, 100., quote().datetime)
        for i in range(1, 6):
            eng.step_bar_timers('RB0', f'bar_{i}', 100.)
            eng.step_bar_timers('RB0', f'bar_{i}', 100.)
        self.assertEqual(pos.bars_held, 5)
        self.assertEqual(pos.pending_exit_reason, 'no_follow')
        eng.on_close_rejected(pos.pos_id)
        self.assertEqual(eng.on_quote(quote(106.))[0].reason, 'no_follow')

    def test_lot1_task_survives_rejection_and_mfe_updates_while_waiting(self):
        eng = make_engine()
        cond = signal(eng, target=140.)
        pos = eng.on_open_filled(cond.cond_id, 2, 100., quote().datetime)
        eng.on_quote(quote(110.))
        eng.on_quote(quote(117., seconds=1))
        self.assertEqual(pos.max_mfe_r, 1.7)
        eng.on_close_rejected(pos.pos_id)
        self.assertEqual(eng.on_quote(quote(105., seconds=2))[0].reason, 'target_1r')

    def test_live_n_pattern_has_no_added_symbol_exclusivity(self):
        eng = make_engine()
        first = signal(eng)
        eng.on_open_filled(first.cond_id, 2, 100., quote().datetime)
        second = signal(eng, direction='down', index=60)
        self.assertNotEqual(first.cond_id, second.cond_id)

    def test_bar_final_row_before_break_and_no_lookahead(self):
        start = int(quote_timestamp('2026-10-19 14:45:00') * 1e9)
        frame = pd.DataFrame([dict(id=1, datetime=start, close=100.)])
        end = start + 900_000_000_000 - 1000
        self.assertEqual(len(completed_tq_bars(frame, end-1000, 900)), 0)
        self.assertEqual(len(completed_tq_bars(frame, end, 900)), 1)

    def test_live_price_cap_rounding_and_daily_limits(self):
        q = quote(101., ask=102., bid=100.)
        self.assertEqual(compute_slippage_limit_price('BUY', q, 2, 100., 2), 102.)
        q.upper_limit = 101.
        self.assertEqual(compute_slippage_limit_price('BUY', q, 2), 101.)
        self.assertEqual(round_to_tick(100.5, 1.), 101.)


class TestOrderAdapter(unittest.TestCase):
    def setup_adapter(self):
        eng = make_engine()
        cond = signal(eng)
        api = FakeApi()
        q = quote(100., ask=100.)
        adapter = TqOrderAdapter(eng, api, {q.symbol: q})
        adapter.enqueue(eng.on_quote(q))
        adapter.drive(q.datetime)
        return eng, cond, api, q, adapter

    def test_no_replacement_until_cancel_confirmed(self):
        eng, cond, api, q, adapter = self.setup_adapter()
        q.datetime, q.last_price, q.ask_price1 = quote(seconds=3).datetime, 101., 102.
        adapter.drive(q.datetime)
        self.assertEqual(api.cancelled, ['O0'])
        adapter.reconcile(quote(seconds=4).datetime)
        adapter.drive(quote(seconds=4).datetime)
        self.assertEqual(len(api.orders), 1)
        api.orders[0].status = 'FINISHED'
        adapter.reconcile(quote(seconds=5).datetime)
        self.assertEqual(cond.state, 'waiting')
        q.datetime = quote(seconds=7).datetime
        adapter.enqueue(eng.on_quote(q))
        adapter.drive(q.datetime)
        self.assertEqual(len(api.orders), 2)

    def test_stop_crossing_is_latched_during_action_throttle(self):
        eng, cond, api, q, adapter = self.setup_adapter()
        eng.on_quote(quote(89., seconds=1))
        self.assertEqual(cond.state, 'cancelled')
        q.datetime = quote(seconds=3).datetime
        adapter.drive(q.datetime)  # Price has recovered, abort must still stand.
        self.assertEqual(api.cancelled, ['O0'])
        api.orders[0].status = 'FINISHED'
        adapter.reconcile(quote(seconds=4).datetime)
        self.assertEqual(cond.state, 'cancelled')
        self.assertFalse(eng.on_quote(quote(seconds=5)))

    def test_global_throttle_and_exit_priority(self):
        eng, cond, api, q, adapter = self.setup_adapter()
        api.orders[0].status, api.orders[0].volume_left, api.orders[0].trade_price = 'FINISHED', 0, 100.
        adapter.reconcile(quote(seconds=1).datetime)
        q.datetime, q.last_price = quote(seconds=1).datetime, 110.
        adapter.enqueue(eng.on_quote(q))
        adapter.drive(q.datetime)
        self.assertEqual(len(api.orders), 1)
        q.datetime = quote(seconds=2).datetime
        adapter.drive(q.datetime)
        self.assertEqual(len(api.orders), 2)
        self.assertNotEqual(api.orders[1].offset, 'OPEN')


class TestReviewRegressions(unittest.TestCase):
    def test_sdk_numpy_scalars_export_as_numbers(self):
        import numpy as np
        from backtest_n_pattern import json_scalar
        payload = json.loads(json.dumps({'count': np.int64(3), 'amount': np.float64(4.5)}, default=json_scalar))
        self.assertEqual(payload, {'count': 3, 'amount': 4.5})
        self.assertIsInstance(payload['count'], int)

    def adapter(self, **config):
        eng = make_engine(**config)
        cond = signal(eng)
        api = FakeApi()
        q = quote(100., ask=100.)
        return eng, cond, api, q, TqOrderAdapter(eng, api, {q.symbol: q})

    def test_queued_first_lot_upgrades_to_full_stop_and_survives_rebound(self):
        eng, cond, api, q, adapter = self.adapter()
        pos = eng.on_open_filled(cond.cond_id, 2, 100., q.datetime)
        adapter.enqueue(eng.on_quote(quote(110., seconds=1)))
        eng.on_quote(quote(89., seconds=2))
        q.datetime, q.last_price = quote(seconds=3).datetime, 105.
        adapter.drive(q.datetime)
        instruction = adapter.pending[0].instruction
        self.assertEqual((instruction.reason, instruction.volume, instruction.is_lot1), ('stop', 2, False))
        self.assertTrue(instruction.closing_all)
        self.assertEqual(pos.pending_exit_reason, 'stop')

    def test_working_first_lot_cancels_before_full_stop_replacement(self):
        eng, cond, api, q, adapter = self.adapter()
        eng.on_open_filled(cond.cond_id, 2, 100., q.datetime)
        q.datetime, q.last_price = quote(seconds=1).datetime, 110.
        adapter.drive(q.datetime)
        self.assertEqual(api.orders[0].volume, 1)
        q.datetime, q.last_price = quote(seconds=3).datetime, 89.
        adapter.drive(q.datetime)
        self.assertEqual(api.cancelled, ['O0'])
        adapter.drive(quote(seconds=5).datetime)
        self.assertEqual(len(api.orders), 1)  # No replacement before terminal ack.
        api.orders[0].status = 'FINISHED'
        adapter.reconcile(quote(seconds=5).datetime)
        adapter.drive(quote(seconds=5).datetime)
        self.assertEqual(len(api.orders), 2)
        self.assertEqual(adapter.pending[0].instruction.reason, 'stop')
        self.assertEqual(api.orders[1].volume, 2)

    def test_fill_racing_stop_upgrade_only_closes_remaining_volume(self):
        eng, cond, api, q, adapter = self.adapter()
        pos = eng.on_open_filled(cond.cond_id, 2, 100., q.datetime)
        q.datetime, q.last_price = quote(seconds=1).datetime, 110.
        adapter.drive(q.datetime)
        q.datetime, q.last_price = quote(seconds=3).datetime, 89.
        adapter.drive(q.datetime)
        api.orders[0].status, api.orders[0].volume_left, api.orders[0].trade_price = 'FINISHED', 0, 110.
        adapter.reconcile(quote(seconds=5).datetime)
        adapter.drive(quote(seconds=5).datetime)
        self.assertEqual(pos.remaining_lots, 1)
        self.assertEqual(api.orders[1].volume, 1)
        self.assertEqual(adapter.pending[0].instruction.reason, 'stop')

    def test_fill_on_or_beyond_stop_is_latched_for_both_directions(self):
        for direction, stop, fills in [('up', 90., [90., 89.]), ('down', 110., [110., 111.])]:
            for fill in fills:
                with self.subTest(direction=direction, fill=fill):
                    eng = make_engine()
                    cond = signal(eng, direction)
                    pos = eng.on_open_filled(cond.cond_id, 2, fill, quote().datetime)
                    self.assertEqual(pos.pending_exit_reason, 'stop')
                    self.assertEqual(eng.on_quote(quote(100., seconds=1))[0].reason, 'stop')

    def test_driver_retries_after_cooldown_without_new_symbol_tick(self):
        eng, cond, api, q, adapter = self.adapter()
        adapter.drive(q.datetime)
        api.orders[0].status, api.orders[0].last_msg = 'FINISHED', '已撤单'
        adapter.reconcile(quote(seconds=3).datetime)
        adapter.drive(quote(seconds=4).datetime)
        self.assertEqual(len(api.orders), 1)
        adapter.drive(quote(seconds=6).datetime)
        self.assertEqual(len(api.orders), 2)  # Book still at :00, no on_quote from caller.
        adapter.drive(quote(seconds=7).datetime)
        self.assertEqual(len(api.orders), 2)

    def test_retry_still_refuses_expired_books(self):
        eng, cond, api, q, adapter = self.adapter()
        adapter.drive(q.datetime)
        api.orders[0].status = 'FINISHED'
        adapter.reconcile(quote(seconds=3).datetime)
        adapter.drive(quote(seconds=16).datetime)
        self.assertEqual(len(api.orders), 1)
        self.assertEqual(cond.state, 'waiting')

    def test_invalid_quotes_do_not_authorize_submit_or_crash(self):
        for field, value in [('last_price', float('nan')), ('last_price', 0.),
                             ('price_tick', 0.), ('price_tick', float('nan')),
                             ('price_tick', None), ('datetime', 'bad timestamp')]:
            with self.subTest(field=field, value=value):
                eng, cond, api, q, adapter = self.adapter()
                adapter.enqueue(eng.on_quote(q))
                cond.entry_authorized = False
                setattr(q, field, value)
                adapter.drive(quote().datetime)
                self.assertFalse(cond.entry_authorized)
                self.assertFalse(api.orders)
                self.assertEqual(len(adapter.queue), 1)

    def test_direct_entry_ready_rejects_nan_without_authorization(self):
        eng = make_engine()
        cond = signal(eng)
        self.assertFalse(eng.entry_ready(cond, quote(float('nan'))))
        self.assertFalse(cond.entry_authorized)

    def test_cancellation_preserves_strategy_reason_and_is_not_broker_rejection(self):
        eng, cond, api, q, adapter = self.adapter()
        adapter.drive(q.datetime)
        eng.on_quote(quote(89., seconds=1))
        q.datetime = quote(seconds=3).datetime
        adapter.drive(q.datetime)
        api.orders[0].status, api.orders[0].last_msg = 'FINISHED', '已撤单'
        adapter.reconcile(quote(seconds=4).datetime)
        self.assertEqual(cond.reject_reason, 'stop_before_entry')
        self.assertEqual(cond.last_order_message, '已撤单')
        self.assertEqual(cond.state, 'cancelled')
        self.assertFalse(eng.analytics.broker_rejections)
        self.assertEqual(eng.analytics.symbol_counters['RB0']['entry_order_cancelled'], 1)

    def test_gfd_cancellation_not_counted_as_rejection(self):
        eng, cond, api, q, adapter = self.adapter()
        adapter.drive(q.datetime)
        api.orders[0].status, api.orders[0].last_msg = 'FINISHED', '已撤单'
        adapter.reconcile(quote(seconds=3).datetime)
        self.assertFalse(eng.analytics.broker_rejections)
        self.assertEqual(cond.state, 'waiting')

    def test_frozen_release_recovers_with_same_snapshot_timestamp(self):
        eng, cond, api, q, adapter = self.adapter()
        at = quote_timestamp(q.datetime)
        eng.update_funds(100., 1000., at, frozen_margin=1000.)
        adapter.drive(q.datetime)
        api.orders[0].status, api.orders[0].last_msg = 'FINISHED', '开仓资金不足'
        adapter.reconcile(quote(seconds=1).datetime)
        eng.update_funds(1100., 1000., at, frozen_margin=0.)
        adapter.drive(quote(seconds=3).datetime)
        self.assertEqual(len(api.orders), 2)
        self.assertEqual(len(eng.analytics.broker_rejections), 1)

    def test_frozen_transfer_to_position_margin_is_not_capital_release(self):
        eng, cond, api, q, adapter = self.adapter()
        at = quote_timestamp(q.datetime)
        eng.update_funds(100., 1000., at, frozen_margin=1000.)
        adapter.drive(q.datetime)
        api.orders[0].status, api.orders[0].last_msg = 'FINISHED', '开仓资金不足'
        adapter.reconcile(quote(seconds=1).datetime)
        eng.update_funds(200., 2000., at+3, frozen_margin=0.)
        adapter.drive(quote(seconds=3).datetime)
        self.assertEqual(len(api.orders), 1)
        self.assertEqual(cond.state, 'waiting')

    def test_waiting_occupies_optional_slot_without_blocking_itself(self):
        eng, cond, api, q, adapter = self.adapter(one_per_symbol=True)
        adapter.drive(q.datetime)
        api.orders[0].status = 'FINISHED'
        adapter.reconcile(quote(seconds=3).datetime)
        self.assertTrue(eng.has_active_slot('RB0'))
        self.assertFalse(eng.has_active_slot('RB0', cond.cond_id))
        adapter.drive(quote(seconds=6).datetime)
        self.assertEqual(len(api.orders), 2)

    def test_reprice_includes_aggressive_offset(self):
        eng, cond, api, q, adapter = self.adapter(slippage_ticks=2)
        adapter.drive(q.datetime)
        self.assertEqual(api.orders[0].limit_price, 102.)
        q.datetime, q.ask_price1 = quote(seconds=3).datetime, 101.
        adapter.drive(q.datetime)
        self.assertEqual(api.cancelled, ['O0'])

    def test_default_worker_chooses_latest_and_explicit_missing_fails(self):
        import os
        import tempfile
        from unittest.mock import patch
        from backtest_n_pattern import resolve_worker_binary
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'N_SIGNAL_WORKER_BIN': ''}):
            root = Path(directory)
            name = 'n-signal-worker.exe' if os.name == 'nt' else 'n-signal-worker'
            paths = [root / 'target' / mode / name for mode in ('debug', 'release')]
            for path in paths:
                path.parent.mkdir(parents=True)
                path.write_bytes(b'worker')
            os.utime(paths[0], (2000, 2000))
            os.utime(paths[1], (1000, 1000))
            with patch('backtest_n_pattern._REPO_ROOT', root):
                self.assertEqual(resolve_worker_binary(), paths[0])
                self.assertEqual(resolve_worker_binary(str(paths[1])), paths[1])
                with self.assertRaises(FileNotFoundError):
                    resolve_worker_binary(str(root / 'missing'))


class TestProductionPolicyOracle(unittest.TestCase):
    def test_python_exits_match_rust_production_policy(self):
        root = Path(__file__).resolve().parents[1]
        binary = root / 'target/debug/n_exit_oracle.exe'
        self.assertTrue(binary.exists(), 'Build cargo build --offline -p n-core --bin n_exit_oracle first')
        rng = random.Random(112026)
        inputs, actuals = [], []
        for _ in range(400):
            sign = rng.choice([-1., 1.])
            fill = 100.+sign*rng.choice([0., 1., 2.])
            large = rng.choice([False, True])
            first_closed = large and rng.choice([False, True])
            eng = make_engine()
            cond = signal(eng, 'up' if sign > 0 else 'down', target=100.+sign*40.)
            pos = eng.on_open_filled(cond.cond_id, 2 if large else 1, fill, quote().datetime)
            pos.lot1_closed = first_closed
            if first_closed:
                pos.remaining_lots = 1
                pos.current_stop = fill+sign*rng.choice([0., 2., 8.])
            price = fill+sign*rng.uniform(-15., 45.)
            pos.max_mfe_r = max(rng.uniform(0., 4.), (price-fill)*sign/pos.risk)
            inputs.append(dict(large=large, fill=fill, risk=pos.risk, sign=sign, price=price,
                target=pos.target, stop=pos.current_stop, max_mfe=pos.max_mfe_r, first_closed=first_closed))
            instructions = eng.on_quote(quote(price))
            actuals.append(dict(stop=pos.current_stop, reason=instructions[0].reason if instructions else None,
                                all=instructions[0].closing_all if instructions else None))
        result = subprocess.run([str(binary)], input=''.join(json.dumps(i)+'\n' for i in inputs),
                                text=True, capture_output=True, check=True)
        expected = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(expected), len(actuals))
        for index, (actual, reference) in enumerate(zip(actuals, expected)):
            with self.subTest(index=index, input=inputs[index]):
                self.assertEqual(actual['reason'], reference['reason'])
                self.assertEqual(actual['all'], reference['all'])
                self.assertAlmostEqual(actual['stop'], reference['stop'])


if __name__ == '__main__':
    unittest.main()
