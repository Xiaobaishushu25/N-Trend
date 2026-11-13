"""Offline & worker integration tests for backtest_n_pattern.py."""

from datetime import date, datetime
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from backtest_n_pattern import (
    BacktestConfig,
    NPatternBacktestEngine,
    PENDING_BARS,
    QuoteTick,
    RiskGate,
    SignalWorkerClient,
    compute_close_offsets,
    compute_slippage_limit_price,
    export_report,
    load_symbols_from_db,
    parse_cli_datetime,
    tq_bar_end_datetime,
)


def _make_cfg(**overrides) -> BacktestConfig:
    defaults = dict(
        symbols=["RB0", "EG0"],
        start=date(2025, 1, 2),
        end=date(2025, 2, 1),
        init_balance=1_000_000.0,
        min_score=2.5,
        large_score=3.5,
        min_risk_ticks=10,
        max_risk_yuan=None,
        max_risk_pct=None,
        risk_action="downsize",
        slippage_ticks=0,
        allow_rollover_overlap=False,
        quiet=True,
    )
    defaults.update(overrides)
    return BacktestConfig(**defaults)


def _sample_signal(
    *,
    symbol: str = "RB0",
    direction: str = "up",
    entry: float = 100.0,
    stop: float = 90.0,
    target: float = 110.0,
    risk: float = 10.0,
    score: float = 4.0,
    warning_index: int = 50,
) -> dict:
    return {
        "symbol": symbol,
        "direction": direction,
        "grade": "A",
        "level": "L1",
        "warning_kind": "breakout",
        "entry_score": score,
        "dim_a": 1.2,
        "dim_b": 1.4,
        "dim_warning": 1.4,
        "entry": entry,
        "stop": stop,
        "target": target,
        "risk": risk,
        "rr": 1.0,
        "trend_state": "up",
        "warning_index": warning_index,
        "warning_ts": "2025-01-02 09:30:00",
        "s0_ts": "2025-01-02 09:00:00",
        "s1_ts": "2025-01-02 09:15:00",
        "s2_ts": "2025-01-02 09:30:00",
        "s0_price": 88.0,
        "s1_price": 100.0,
        "s2_price": 90.0,
    }


class TestPureHelpers(unittest.TestCase):
    def test_tq_bar_end_datetime_beijing(self):
        # 2025-01-02 01:00:00 UTC == 09:00:00 BJ start -> 09:15:00 BJ bar end
        # Unix ts for 2025-01-02 01:00:00 UTC: 1735779600
        start_ns = 1735779600 * 1_000_000_000
        self.assertEqual(tq_bar_end_datetime(start_ns, 900), "2025-01-02 09:15:00")

    def test_compute_close_offsets_shfe_ine_vs_other(self):
        self.assertEqual(
            compute_close_offsets("SHFE", 2, avail_his=1, avail_today=1),
            [("CLOSE", 1), ("CLOSETODAY", 1)],
        )
        self.assertEqual(
            compute_close_offsets("INE", 2, avail_his=0, avail_today=2),
            [("CLOSETODAY", 2)],
        )
        self.assertEqual(
            compute_close_offsets("DCE", 2, avail_his=1, avail_today=1),
            [("CLOSE", 2)],
        )

    def test_compute_slippage_limit_price(self):
        q = QuoteTick(
            symbol="SHFE.rb2505",
            datetime="2025-01-02 09:30:00",
            last_price=3500.0,
            ask_price1=3501.0,
            bid_price1=3499.0,
            upper_limit=3700.0,
            lower_limit=3300.0,
            price_tick=1.0,
            volume_multiple=10.0,
        )
        self.assertEqual(compute_slippage_limit_price("BUY", q, 0), 3501.0)
        self.assertEqual(compute_slippage_limit_price("SELL", q, 0), 3499.0)
        self.assertEqual(compute_slippage_limit_price("BUY", q, 1), 3502.0)
        self.assertEqual(compute_slippage_limit_price("SELL", q, 2), 3497.0)

    def test_parse_cli_datetime_and_load_symbols_from_db(self):
        self.assertEqual(parse_cli_datetime("2026-10-19", is_end=False), datetime(2026, 10, 19, 0, 0, 0))
        self.assertEqual(parse_cli_datetime("2026-11-11", is_end=True), datetime(2026, 11, 11, 23, 59, 59))
        self.assertEqual(parse_cli_datetime("2026-11-11 15:00", is_end=True), datetime(2026, 11, 11, 15, 0, 0))
        self.assertEqual(parse_cli_datetime("2026-11-11 15:00:00", is_end=True), datetime(2026, 11, 11, 15, 0, 0))

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "syms.db"
            con = sqlite3.connect(str(db_path))
            con.execute("CREATE TABLE symbols (code TEXT, enabled INTEGER)")
            con.executemany("INSERT INTO symbols VALUES (?, ?)", [("rb0", 1), ("au0", 0), ("eg0", 1)])
            con.commit()
            con.close()
            self.assertEqual(load_symbols_from_db(db_path), ["RB0", "EG0"])


class TestRiskGate(unittest.TestCase):
    def test_min_risk_ticks_rejected(self):
        cfg = _make_cfg(min_risk_ticks=10)
        d = RiskGate.evaluate(4.0, risk=9.0, tick=1.0, multiplier=10.0, cfg=cfg, current_equity=1_000_000.0)
        self.assertFalse(d.allowed)
        self.assertIn("risk_below_10_ticks", d.reason)

    def test_max_risk_yuan_downsize_vs_reject(self):
        # risk=50, mult=10 -> 500/lot, 1000 for 2 lots
        cfg_down = _make_cfg(max_risk_yuan=700.0, risk_action="downsize")
        d1 = RiskGate.evaluate(4.0, risk=50.0, tick=1.0, multiplier=10.0, cfg=cfg_down, current_equity=1_000_000.0)
        self.assertTrue(d1.allowed)
        self.assertEqual(d1.lots, 1)
        self.assertTrue(d1.downsized)

        cfg_rej = _make_cfg(max_risk_yuan=700.0, risk_action="reject")
        d2 = RiskGate.evaluate(4.0, risk=50.0, tick=1.0, multiplier=10.0, cfg=cfg_rej, current_equity=1_000_000.0)
        self.assertFalse(d2.allowed)

        # Even 1 lot exceeds cap -> rejected even under downsize
        cfg_tight = _make_cfg(max_risk_yuan=400.0, risk_action="downsize")
        d3 = RiskGate.evaluate(4.0, risk=50.0, tick=1.0, multiplier=10.0, cfg=cfg_tight, current_equity=1_000_000.0)
        self.assertFalse(d3.allowed)

    def test_max_risk_pct_cap(self):
        cfg = _make_cfg(max_risk_pct=0.01, risk_action="downsize")
        # equity=100_000 -> cap=1000. 1 lot = 600, 2 lots = 1200 -> downsize to 1
        d = RiskGate.evaluate(3.8, risk=60.0, tick=1.0, multiplier=10.0, cfg=cfg, current_equity=100_000.0)
        self.assertTrue(d.allowed)
        self.assertEqual(d.lots, 1)
        self.assertEqual(d.effective_cap_yuan, 1000.0)


class TestNExitAndLifecycle(unittest.TestCase):
    def _open_position_from_signal(
        self,
        engine: NPatternBacktestEngine,
        *,
        symbol: str = "RB0",
        actual: str = "SHFE.rb2505",
        direction: str = "up",
        entry: float = 100.0,
        stop: float = 90.0,
        target: float = 125.0,
        risk: float = 10.0,
        score: float = 4.0,
        multiplier: float = 10.0,
    ) -> str:
        created = engine.ingest_signals(
            logical_symbol=symbol,
            bar_ts="2025-01-02 09:30:00",
            actual_symbol=actual,
            tick=1.0,
            multiplier=multiplier,
            current_equity=1_000_000.0,
            signals=[_sample_signal(
                symbol=symbol, direction=direction, entry=entry, stop=stop, target=target, risk=risk, score=score
            )],
        )
        self.assertEqual(len(created), 1)
        cond = created[0]
        instrs = engine.on_quote(QuoteTick(actual, "2025-01-02 09:31:00", last_price=entry))
        self.assertEqual(len(instrs), 1)
        self.assertEqual(instrs[0].intent, "open")
        pos = engine.on_open_filled(
            cond.cond_id,
            filled_volume=instrs[0].volume,
            fill_price=entry,
            fill_ts="2025-01-02 09:31:00",
            order_ids=[f"OPEN_{cond.cond_id}"],
        )
        self.assertIsNotNone(pos)
        return pos.pos_id

    def test_protection_requires_confirmed_lot1_fill_and_breakeven(self):
        engine = NPatternBacktestEngine(_make_cfg())
        pos_id = self._open_position_from_signal(engine, target=125.0)
        pos = engine.positions[pos_id]

        # Hit 1.0R (110.0): emits 1-lot target_1r close
        instrs = engine.on_quote(QuoteTick("SHFE.rb2505", "2025-01-02 09:35:00", last_price=110.0))
        self.assertEqual(len(instrs), 1)
        self.assertEqual(instrs[0].reason, "target_1r")
        self.assertTrue(instrs[0].is_lot1)
        self.assertEqual(instrs[0].volume, 1)

        # BEFORE lot1 fill confirmation, stop must remain at 90.0 and no duplicate exit emits
        instrs_pending = engine.on_quote(QuoteTick("SHFE.rb2505", "2025-01-02 09:36:00", last_price=100.0))
        self.assertEqual(instrs_pending, [])
        self.assertEqual(pos.current_stop, 90.0)

        # Confirm Lot 1 fill -> stop moves to breakeven (100.0)
        engine.on_close_filled(
            pos_id, filled_volume=1, fill_price=110.0, fill_ts="2025-01-02 09:35:00",
            reason="target_1r", is_lot1=True, order_ids=["CLOSE_1"]
        )
        self.assertTrue(pos.lot1_closed)
        self.assertEqual(pos.current_stop, 100.0)

        # Price returns to 100.0 -> exits Lot 2 at breakeven
        instrs2 = engine.on_quote(QuoteTick("SHFE.rb2505", "2025-01-02 09:40:00", last_price=100.0))
        self.assertEqual(len(instrs2), 1)
        self.assertEqual(instrs2[0].reason, "breakeven")
        self.assertEqual(instrs2[0].volume, 1)

    def test_trailing_stop_long_and_short(self):
        # Long: fill=100, risk=10, target=200
        eng_l = NPatternBacktestEngine(_make_cfg())
        pid_l = self._open_position_from_signal(eng_l, direction="up", entry=100.0, stop=90.0, target=200.0)
        i1 = eng_l.on_quote(QuoteTick("SHFE.rb2505", "2025-01-02 09:35:00", last_price=110.0))
        eng_l.on_close_filled(pid_l, 1, 110.0, "2025-01-02 09:35:00", "target_1r", is_lot1=True)
        self.assertEqual(len(i1), 1)
        # Advance to +2.0R (120) -> trail locks 100 + (2.0 - 0.8)*10 = 112
        self.assertEqual(eng_l.on_quote(QuoteTick("SHFE.rb2505", "2025-01-02 09:38:00", last_price=120.0)), [])
        self.assertAlmostEqual(eng_l.positions[pid_l].current_stop, 112.0)
        # Pullback to 112 triggers trailing exit
        i2 = eng_l.on_quote(QuoteTick("SHFE.rb2505", "2025-01-02 09:40:00", last_price=112.0))
        self.assertEqual(len(i2), 1)
        self.assertEqual(i2[0].reason, "trailing")

        # Short: fill=100, risk=10, target=50
        eng_s = NPatternBacktestEngine(_make_cfg())
        pid_s = self._open_position_from_signal(eng_s, direction="down", entry=100.0, stop=110.0, target=50.0)
        eng_s.on_quote(QuoteTick("SHFE.rb2505", "2025-01-02 09:35:00", last_price=90.0))
        eng_s.on_close_filled(pid_s, 1, 90.0, "2025-01-02 09:35:00", "target_1r", is_lot1=True)
        # Drop to 80 (+2.0R) -> trail locks 100 - (2.0 - 0.8)*10 = 88
        self.assertEqual(eng_s.on_quote(QuoteTick("SHFE.rb2505", "2025-01-02 09:38:00", last_price=80.0)), [])
        self.assertAlmostEqual(eng_s.positions[pid_s].current_stop, 88.0)
        i_s2 = eng_s.on_quote(QuoteTick("SHFE.rb2505", "2025-01-02 09:40:00", last_price=88.0))
        self.assertEqual(len(i_s2), 1)
        self.assertEqual(i_s2[0].reason, "trailing")

    def test_target_s1_and_small_tier_full_exit(self):
        # Small tier closes all 1 lot at target_1r
        eng_sm = NPatternBacktestEngine(_make_cfg())
        self._open_position_from_signal(eng_sm, score=2.8, target=125.0)
        instrs = eng_sm.on_quote(QuoteTick("SHFE.rb2505", "2025-01-02 09:35:00", last_price=110.0))
        self.assertEqual(len(instrs), 1)
        self.assertEqual(instrs[0].reason, "target_1r")
        self.assertFalse(instrs[0].is_lot1)
        self.assertTrue(instrs[0].closing_all)

        # Large tier hits target_s1 on Lot 2
        eng_lg = NPatternBacktestEngine(_make_cfg())
        pid = self._open_position_from_signal(eng_lg, score=4.0, target=114.0)
        eng_lg.on_quote(QuoteTick("SHFE.rb2505", "2025-01-02 09:35:00", last_price=110.0))
        eng_lg.on_close_filled(pid, 1, 110.0, "2025-01-02 09:35:00", "target_1r", is_lot1=True)
        instrs_s1 = eng_lg.on_quote(QuoteTick("SHFE.rb2505", "2025-01-02 09:37:00", last_price=114.0))
        self.assertEqual(len(instrs_s1), 1)
        self.assertEqual(instrs_s1[0].reason, "target_s1")

    def test_no_follow_and_time_exit(self):
        # No follow at bar 5 when max_mfe_r < 0.5
        eng = NPatternBacktestEngine(_make_cfg())
        self._open_position_from_signal(eng)
        for b in range(1, 5):
            self.assertEqual(eng.step_bar_timers("RB0", f"2025-01-02 10:{b:02d}:00", 102.0), [])
        instrs = eng.step_bar_timers("RB0", "2025-01-02 11:00:00", 102.0)
        self.assertEqual(len(instrs), 1)
        self.assertEqual(instrs[0].reason, "no_follow")

        # Time exit at bar 60 if MFE reached >= 0.5
        eng2 = NPatternBacktestEngine(_make_cfg())
        pid2 = self._open_position_from_signal(eng2, target=200.0)
        eng2.on_quote(QuoteTick("SHFE.rb2505", "2025-01-02 09:32:00", last_price=106.0))  # MFE = 0.6R
        for b in range(1, 60):
            out = eng2.step_bar_timers("RB0", f"bar_{b}", 105.0)
            self.assertEqual(out, [], f"unexpected exit at bar {b}")
        out60 = eng2.step_bar_timers("RB0", "bar_60", 105.0)
        self.assertEqual(len(out60), 1)
        self.assertEqual(out60[0].reason, "time_exit")
        self.assertEqual(eng2.positions[pid2].bars_held, 60)

    def test_pending_expiry_and_dedup(self):
        eng = NPatternBacktestEngine(_make_cfg())
        sig1 = _sample_signal(entry=100.0, risk=10.0, warning_index=50)
        c1 = eng.ingest_signals("RB0", "2025-01-02 09:30:00", "SHFE.rb2505", 1.0, 10.0, 1_000_000.0, [sig1])
        self.assertEqual(len(c1), 1)

        # Duplicate while active (bar_gap <= 5) is skipped even if entry differs slightly more
        sig_dup_active = _sample_signal(entry=105.0, risk=10.0, warning_index=53)
        self.assertEqual(
            eng.ingest_signals("RB0", "2025-01-02 09:45:00", "SHFE.rb2505", 1.0, 10.0, 1_000_000.0, [sig_dup_active]),
            [],
        )

        # Live expires only after PENDING_BARS, so bar 12 is still valid.
        for i in range(PENDING_BARS):
            eng.step_bar_timers("RB0", f"bar_{i}", 95.0)
        self.assertEqual(c1[0].state, "pending")
        eng.step_bar_timers("RB0", "bar_12", 95.0)
        self.assertEqual(c1[0].state, "expired")

        # Duplicate after expiry with |entry - prev.entry| <= 0.3 * prev.risk -> still skipped
        sig_dup_price = _sample_signal(entry=102.0, risk=10.0, warning_index=54)
        self.assertEqual(
            eng.ingest_signals("RB0", "2025-01-02 10:00:00", "SHFE.rb2505", 1.0, 10.0, 1_000_000.0, [sig_dup_price]),
            [],
        )

        # Non-duplicate after expiry with |entry - prev.entry| > 0.3 * prev.risk -> allowed
        sig_far = _sample_signal(entry=106.0, risk=10.0, warning_index=55)
        c2 = eng.ingest_signals("RB0", "2025-01-02 10:15:00", "SHFE.rb2505", 1.0, 10.0, 1_000_000.0, [sig_far])
        self.assertEqual(len(c2), 1)


class TestRolloverHoldAndAnalytics(unittest.TestCase):
    def test_rollover_hold_cancels_pending_keeps_open_and_tracks_old_contract(self):
        eng = NPatternBacktestEngine(_make_cfg(allow_rollover_overlap=False, one_per_symbol=True))

        # Open position on old contract rb2501
        c1 = eng.ingest_signals(
            "RB0", "2025-01-02 09:30:00", "SHFE.rb2501", 1.0, 10.0, 1_000_000.0,
            [_sample_signal(symbol="RB0", entry=3500.0, stop=3480.0, target=3540.0, risk=20.0, score=2.8, warning_index=10)],
        )[0]
        i_open = eng.on_quote(QuoteTick("SHFE.rb2501", "2025-01-02 09:31:00", last_price=3500.0))
        pos = eng.on_open_filled(c1.cond_id, 1, 3500.0, "2025-01-02 09:31:00", order_ids=["O1"])

        # Suppose an unrelated pending condition also existed on rb2501
        # Simulate rollover (using allow_rollover_overlap=False: open position remains alive!)
        cancelled = eng.cancel_pending_for_rollover("RB0")
        self.assertEqual(cancelled, [])
        self.assertIn(pos.pos_id, eng.active_positions)

        # New signal after rollover on rb2505 is blocked while rb2501 position is still open
        c_new = eng.ingest_signals(
            "RB0", "2025-01-03 09:30:00", "SHFE.rb2505", 1.0, 10.0, 1_000_000.0,
            [_sample_signal(symbol="RB0", entry=3550.0, stop=3530.0, target=3590.0, risk=20.0, score=2.8, warning_index=50)],
        )
        self.assertEqual(c_new, [])

        # Quotes on rb2501 still trigger 1R exit for old open position!
        i_close = eng.on_quote(QuoteTick("SHFE.rb2501", "2025-01-03 10:00:00", last_price=3520.0))
        self.assertEqual(len(i_close), 1)
        self.assertEqual(i_close[0].actual_symbol, "SHFE.rb2501")
        self.assertEqual(i_close[0].reason, "target_1r")
        eng.on_close_filled(pos.pos_id, 1, 3520.0, "2025-01-03 10:00:00", "target_1r", is_lot1=False, order_ids=["C1"])
        self.assertEqual(len(eng.active_positions), 0)

        # Now rb2505 signals are allowed again, AND rollover cancels any pending rb2505 order
        c_rb2505 = eng.ingest_signals(
            "RB0", "2025-01-03 10:15:00", "SHFE.rb2505", 1.0, 10.0, 1_000_000.0,
            [_sample_signal(symbol="RB0", entry=3550.0, stop=3530.0, target=3590.0, risk=20.0, score=2.8, warning_index=60)],
        )
        self.assertEqual(len(c_rb2505), 1)
        cancelled2 = eng.cancel_pending_for_rollover("RB0")
        self.assertEqual(len(cancelled2), 1)
        self.assertEqual(c_rb2505[0].state, "cancelled")

    def test_grouped_analytics_commission_reconciliation_and_export(self):
        eng = NPatternBacktestEngine(_make_cfg())

        # Trade 1 (RB0, 2 lots): Lot1 +1R (+200), Lot2 +2R (+400) -> Gross = +600
        c1 = eng.ingest_signals(
            "RB0", "2025-01-02 09:30:00", "SHFE.rb2505", 1.0, 10.0, 1_000_000.0,
            [_sample_signal(symbol="RB0", entry=100.0, stop=90.0, target=150.0, risk=10.0, score=4.0, warning_index=10)],
        )[0]
        p1 = eng.on_open_filled(c1.cond_id, 2, 100.0, "2025-01-02 09:31:00", order_ids=["ORD_O1"])
        eng.on_close_filled(p1.pos_id, 1, 110.0, "2025-01-02 09:40:00", "target_1r", is_lot1=True, order_ids=["ORD_C1A"])
        eng.on_close_filled(p1.pos_id, 1, 120.0, "2025-01-02 09:50:00", "trailing", is_lot1=False, order_ids=["ORD_C1B"])

        # Trade 2 (EG0, 1 lot short): stopped out -1R (-100)
        c2 = eng.ingest_signals(
            "EG0", "2025-01-02 10:00:00", "DCE.eg2505", 1.0, 10.0, 1_000_000.0,
            [_sample_signal(symbol="EG0", direction="down", entry=4500.0, stop=4510.0, target=4480.0, risk=10.0, score=2.8, warning_index=20)],
        )[0]
        p2 = eng.on_open_filled(c2.cond_id, 1, 4500.0, "2025-01-02 10:01:00", order_ids=["ORD_O2"])
        eng.on_close_filled(p2.pos_id, 1, 4510.0, "2025-01-02 10:15:00", "stop", is_lot1=False, order_ids=["ORD_C2"])

        comm_map = {
            "ORD_O1": 10.0,
            "ORD_C1A": 5.0,
            "ORD_C1B": 5.0,
            "ORD_O2": 4.0,
            "ORD_C2": 4.0,
        }
        eng.analytics.reconcile_commissions(eng.positions, comm_map)
        eng.analytics.final_equity = 1_000_000.0 + (300.0 - 20.0) + (-100.0 - 8.0)
        report = eng.analytics.build_report(["RB0", "EG0"])

        # RB0 trade gross = (110-100)*10 + (120-100)*10 = 300; net = 300 - 20 = 280
        # EG0 trade gross = -100; net = -108
        ov = report["overall"]
        self.assertEqual(ov["trades"], 2)  # Grouped by cond_id, not 3 exit legs!
        self.assertEqual(ov["wins"], 1)
        self.assertEqual(ov["losses"], 1)
        self.assertAlmostEqual(ov["win_rate"], 0.5)
        self.assertAlmostEqual(ov["gross_pnl"], 200.0)
        self.assertAlmostEqual(ov["commission"], 28.0)
        self.assertAlmostEqual(ov["net_pnl"], 172.0)
        self.assertAlmostEqual(ov["profit_factor"], 280.0 / 108.0)

        self.assertAlmostEqual(report["by_symbol"]["RB0"]["net_pnl"], 280.0)
        self.assertAlmostEqual(report["by_symbol"]["EG0"]["short"]["net_pnl"], -108.0)

        with tempfile.TemporaryDirectory() as tmp:
            out = export_report(report, tmp)
            for fname in ("summary.json", "trades.csv", "legs.csv", "by_symbol.csv", "risk_events.csv"):
                self.assertTrue((out / fname).is_file(), f"missing {fname}")
            summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["overall"]["trades"], 2)


class TestLiveWorkerBinary(unittest.TestCase):
    def test_worker_init_and_step_bar(self):
        warmup = []
        base_px = 3500.0
        for i in range(60):
            hh = 9 + (i * 15) // 60
            mm = (i * 15) % 60
            close_px = base_px + (i % 7) - 3
            warmup.append({
                "datetime": f"2025-01-{(i // 24) + 2:02d} {hh % 24:02d}:{mm:02d}:00",
                "open": close_px - 1.0,
                "high": close_px + 2.0,
                "low": close_px - 2.0,
                "close": close_px,
                "volume": 1000.0 + i * 10,
                "hold": 50000.0,
                "rollover": False,
            })

        with SignalWorkerClient() as client:
            init_resp = client.init_symbol("RB0", 1.0, warmup)
            self.assertTrue(init_resp["ok"])
            self.assertEqual(init_resp["bar_count"], 60)
            self.assertAlmostEqual(init_resp["multiplier"], 10.0)

            sigs = client.step_bar("RB0", {
                "datetime": "2025-01-05 15:00:00",
                "open": 3502.0,
                "high": 3510.0,
                "low": 3500.0,
                "close": 3508.0,
                "volume": 1500.0,
                "hold": 50100.0,
                "rollover": False,
            })
            self.assertIsInstance(sigs, list)


class TestMockedTqBacktestDriver(unittest.TestCase):
    def test_run_tq_backtest_rollover_hold_and_shfe_closetoday(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        import sys
        import pandas as pd

        from backtest_n_pattern import print_report, run_tq_backtest

        class FakeBacktestFinished(Exception):
            pass

        # 2025-01-02 01:00 UTC -> 09:15 BJ bar end
        # Warmup: 2025-01-01 01:00 UTC -> 2025-01-01 09:15 BJ (< 2025-01-02)
        t_warmup = 1735693200 * 1_000_000_000
        t_active1 = 1735779600 * 1_000_000_000
        t_active2 = (1735779600 + 900) * 1_000_000_000
        t_active3 = (1735779600 + 1800) * 1_000_000_000

        df_klines = pd.DataFrame([
            {"id": 0, "datetime": t_warmup, "open": 3490.0, "high": 3495.0, "low": 3485.0, "close": 3492.0, "volume": 100.0, "close_oi": 1000.0},
            {"id": 1, "datetime": t_active1, "open": 3495.0, "high": 3505.0, "low": 3494.0, "close": 3500.0, "volume": 120.0, "close_oi": 1000.0},
            {"id": 2, "datetime": t_active2, "open": 3500.0, "high": 3505.0, "low": 3499.0, "close": 3501.0, "volume": 110.0, "close_oi": 1000.0},
        ])

        cont_quote = SimpleNamespace(
            datetime="2025-01-02 09:15:00",
            last_price=3492.0,
            price_tick=1.0,
            volume_multiple=10.0,
            underlying_symbol="SHFE.rb2501",
            _changing=False,
        )
        rb2501_quote = SimpleNamespace(
            datetime="2025-01-02 09:15:00",
            last_price=3495.0,
            ask_price1=3496.0,
            bid_price1=3494.0,
            upper_limit=3800.0,
            lower_limit=3200.0,
            price_tick=1.0,
            volume_multiple=10.0,
            underlying_symbol="",
            _changing=False,
        )
        rb2505_quote = SimpleNamespace(
            datetime="2025-01-02 09:30:00",
            last_price=3550.0,
            ask_price1=3551.0,
            bid_price1=3549.0,
            upper_limit=3800.0,
            lower_limit=3200.0,
            price_tick=1.0,
            volume_multiple=10.0,
            underlying_symbol="",
            _changing=False,
        )
        account_obj = SimpleNamespace(balance=1_000_000.0, commission=0.0)
        pos_rb2501 = SimpleNamespace(
            volume_long_his=0,
            volume_long_frozen_his=0,
            volume_long_today=1,
            volume_long_frozen_today=0,
            volume_short_his=0,
            volume_short_frozen_his=0,
            volume_short_today=0,
            volume_short_frozen_today=0,
        )
        recorded_orders = []
        tick_subscriptions = []

        class FakeSim:
            def __init__(self, init_balance=1_000_000.0):
                self.init_balance = init_balance
                self.trade_log = {}
                self.tqsdk_stat = {"winning_rate": 1.0, "sharpe_ratio": 2.5, "annual_yield": 0.12, "max_drawdown": 0.01, "sortino_ratio": 3.0}

        class FakeApi:
            def __init__(self, sim, **_kwargs):
                self.sim = sim
                self._step = 0
                self._orders_seq = 0
                self.df = df_klines

            def get_quote(self, sym):
                if sym == "KQ.m@SHFE.rb":
                    return cont_quote
                if sym == "SHFE.rb2501":
                    return rb2501_quote
                return rb2505_quote

            def get_kline_serial(self, sym, dur, data_length=200):
                if dur == 900:
                    return self.df
                return pd.DataFrame([])

            def get_tick_serial(self, sym, data_length=5):
                tick_subscriptions.append(sym)
                return pd.DataFrame([])

            def get_account(self):
                return account_obj

            def get_position(self, sym):
                return pos_rb2501

            def is_changing(self, obj, _fields=None):
                if obj is self.df:
                    return self._step in (1, 3)
                return bool(getattr(obj, "_changing", False))

            def insert_order(self, *, symbol, direction, offset, volume, limit_price=None):
                self._orders_seq += 1
                oid = f"SIM_{self._orders_seq}"
                fill_px = 3500.0 if offset == "OPEN" else 3510.0
                recorded_orders.append((oid, symbol, direction, offset, volume))
                day = self.sim.trade_log.setdefault("2025-01-02", {"trades": []})
                day["trades"].append({"order_id": oid, "commission": 3.5})
                account_obj.commission += 3.5
                return SimpleNamespace(
                    order_id=oid,
                    status="FINISHED",
                    volume_orign=volume,
                    volume_left=0,
                    trade_price=fill_px,
                    limit_price=limit_price,
                    last_msg="全部成交",
                    insert_date_time=t_active1,
                )

            def wait_update(self):
                self._step += 1
                rb2501_quote._changing = False
                rb2505_quote._changing = False
                if self._step == 1:
                    # Emit initial bars -> worker step_bar returns Small long signal entry=3500, stop=3490
                    return True
                if self._step == 2:
                    # 1m quote triggers entry on SHFE.rb2501
                    rb2501_quote.datetime = "2025-01-02 09:16:00"
                    rb2501_quote.last_price = 3500.0
                    rb2501_quote._changing = True
                    return True
                if self._step == 3:
                    # Continuous contract rolls over to SHFE.rb2505; old position stays on rb2501!
                    cont_quote.underlying_symbol = "SHFE.rb2505"
                    # Append forming bar id=3 so completed bar id=2 carries rollover=True
                    self.df.loc[len(self.df)] = {
                        "id": 3, "datetime": t_active3, "open": 3506.0, "high": 3512.0,
                        "low": 3505.0, "close": 3510.0, "volume": 80.0, "close_oi": 1000.0,
                    }
                    return True
                if self._step == 4:
                    # rb2501 hits 1.0R target (3510.0) -> triggers CLOSETODAY on SHFE.rb2501
                    rb2501_quote.datetime = "2025-01-02 09:31:00"
                    rb2501_quote.last_price = 3510.0
                    rb2501_quote._changing = True
                    return True
                if self._step == 5:
                    # Settle close fill
                    account_obj.balance = 1_000_000.0 + 100.0 - 7.0
                    return True
                raise FakeBacktestFinished()

            def close(self):
                pass

        class FakeWorker:
            def __init__(self, _path=None):
                self.rollovers_seen = []

            def start(self):
                pass

            def close(self):
                pass

            def init_symbol(self, symbol, tick, bars):
                # Deliberately return stale multiplier (like JD0 5.0 vs real 10.0)
                return {"ok": True, "tick": tick, "multiplier": 5.0, "bar_count": len(bars)}

            def step_bar(self, symbol, bar):
                self.rollovers_seen.append(bar["rollover"])
                if len(self.rollovers_seen) == 1:
                    return [_sample_signal(
                        symbol="RB0", direction="up", entry=3500.0, stop=3490.0, target=3510.0, risk=10.0, score=2.8, warning_index=1
                    )]
                return []

        fake_tqsdk = SimpleNamespace(
            BacktestFinished=FakeBacktestFinished,
            TqApi=FakeApi,
            TqAuth=lambda u, p: (u, p),
            TqBacktest=lambda start_dt, end_dt: (start_dt, end_dt),
            TqSim=FakeSim,
        )
        worker_instance = FakeWorker()
        with patch.dict(sys.modules, {"tqsdk": fake_tqsdk}), patch(
            "backtest_n_pattern.SignalWorkerClient", return_value=worker_instance
        ):
            report = run_tq_backtest(_make_cfg(symbols=["RB0"]), account="demo", password="pw")

        self.assertEqual(worker_instance.rollovers_seen, [False, True])
        self.assertEqual(tick_subscriptions, ["SHFE.rb2501", "SHFE.rb2505"])
        self.assertEqual(recorded_orders, [
            ("SIM_1", "SHFE.rb2501", "BUY", "OPEN", 1),
            ("SIM_2", "SHFE.rb2501", "SELL", "CLOSETODAY", 1),
        ])
        self.assertEqual(report["overall"]["trades"], 1)
        self.assertEqual(report["overall"]["wins"], 1)
        self.assertAlmostEqual(report["overall"]["gross_pnl"], 100.0)
        self.assertAlmostEqual(report["overall"]["commission"], 7.0)
        self.assertAlmostEqual(report["overall"]["net_pnl"], 93.0)
        self.assertTrue(report["validation"]["accounting_reconciled"])
        self.assertTrue(report["validation"]["no_unresolved_broker_orders"])
        self.assertTrue(report["validation"]["tick_replay"])
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_report(report)
        self.assertIn("RB0", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
