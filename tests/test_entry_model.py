import copy
import datetime as dt
import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
from entry_model import (BASE, FEATURE_GROUPS, completed_context, features, fit_bundle,
                         geometry, logit, predict, price_limit, raw_probability)
from research_entry_model import accepted_rows, entry_preview, observed_training


FEE = dict(roundtrip_fixed_yuan_per_lot=2)


def event(direction="up"):
    return dict(symbol="SR0", direction=direction, warning_ts="2026-08-03 09:15:00",
                entry=100, stop=90 if direction == "up" else 110,
                target=130 if direction == "up" else 70, risk=10, tick_size=1, rr=3,
                setup_features=dict(a_move_atr=4, b_move_atr=2, a_speed=2, a_move=20,
                    b_move=10, a_bars=10, b_bars=5, retracement=.5, warning_volume_ratio=1.2,
                    setup_quality=3.2, warning_close_location=.75, warning_body_atr=.5,
                    warning_wick_ratio=1, trend60_state="STRONG_UP"))


def training_rows(n=100):
    first = dt.datetime(2026, 6, 1, 9, 15)
    rows = []
    for i in range(n):
        time = first + dt.timedelta(days=i)
        e = event()
        e["warning_ts"] = time.isoformat(sep=" ")
        e["setup_features"]["a_move_atr"] = 2 + i % 5
        f = features(e, FEE, 10, None)
        rows.append(dict(event_key=str(i), warning_ts=e["warning_ts"],
                         entry_ts=(time + dt.timedelta(minutes=5)).isoformat(sep=" "),
                         exit_ts=(time + dt.timedelta(hours=1)).isoformat(sep=" "),
                         features=f, net_r=2.5 if i % 3 == 0 else -1.1))
    return rows


class EntryModelTests(unittest.TestCase):
    def test_feature_extraction_does_not_read_outcomes(self):
        e = event()
        before = features(e, FEE, 10, None)
        e.update(evaluation=dict(net_r=1000, fill=9999), trigger_features=dict(mfe_r=999))
        self.assertEqual(before, features(e, FEE, 10, None))

    def test_normalized_speed_is_invariant_to_quote_units(self):
        a, b = event(), event()
        for field in ("entry", "stop", "target", "risk", "tick_size"):
            b[field] *= 100
        for field in ("a_move", "b_move", "a_speed"):
            b["setup_features"][field] *= 100
        fa, fb = features(a, FEE, 10, None), features(b, FEE, 10, None)
        self.assertNotEqual(fa["a_speed"], fb["a_speed"])
        self.assertEqual(fa["a_speed_atr"], fb["a_speed_atr"])
        self.assertNotIn("a_speed", FEATURE_GROUPS["context"])

    def test_long_short_price_limits_include_both_exit_slip_and_fees(self):
        self.assertEqual(price_limit(event(), FEE, 10), 108)
        self.assertEqual(price_limit(event("down"), FEE, 10), 92)
        for direction, good, bad in (("up", 108, 109), ("down", 92, 91)):
            e = event(direction)
            a, b = geometry(e, FEE, 10, good), geometry(e, FEE, 10, bad)
            self.assertGreaterEqual(a["net_reward_loss"], 1)
            self.assertLess(b["net_reward_loss"], 1)

    def test_complete_context_ignores_future_and_partial_buckets(self):
        raw = [dict(datetime=f"2026-08-03 09:{minute:02}:00", open=100, high=102, low=99, close=101)
               for minute in (5, 10, 15, 20, 25)]
        before = completed_context(raw[:3])
        after = completed_context(raw + [dict(datetime="2026-08-03 09:30:00", open=101,
                                             high=10000, low=1, close=9000)])
        self.assertEqual(before["2026-08-03 09:15:00"], after["2026-08-03 09:15:00"])
        self.assertNotIn("2026-08-03 09:30:00", completed_context(raw))

    def test_rollover_resets_volatility_history_without_changing_prior_features(self):
        start = dt.datetime(2026, 8, 3, 9, 5)
        raw = [dict(datetime=(start + dt.timedelta(minutes=5*i)).isoformat(sep=" "),
                    open=100, high=102, low=99, close=101) for i in range(210)]
        old = completed_context(raw)
        new = completed_context(raw, [dict(ts="2026-08-03 20:30:00", confirmed=True)])
        self.assertEqual(old["2026-08-03 20:15:00"], new["2026-08-03 20:15:00"])
        self.assertIsNone(new["2026-08-03 20:30:00"]["atr14"])

    def test_training_rejects_labels_from_prediction_period(self):
        rows = training_rows(40)
        cutoff = rows[-1]["exit_ts"]
        with self.assertRaisesRegex(ValueError, "label reaches"):
            fit_bundle(rows, BASE, cutoff)

    def test_calibration_is_time_separate_and_bundle_roundtrips(self):
        rows = training_rows()
        bundle = fit_bundle(rows, BASE, "2026-10-01 00:00:00")
        audit = bundle["audit"]
        self.assertGreater(audit["calibration_n"], 0)
        self.assertLess(audit["fit_max_label_end"], audit["calibration_start"] + " 00:00:00")
        self.assertEqual(predict(bundle, rows[0]["features"]),
                         predict(json.loads(json.dumps(bundle)), rows[0]["features"]))

    def test_validation_extremes_cannot_change_training_scaler(self):
        rows = training_rows(40)
        bundle = fit_bundle(rows, BASE, "2026-10-01 00:00:00")
        before = copy.deepcopy(bundle)
        changed = dict(rows[0]["features"], a_move_atr=1e100)
        self.assertTrue(0 <= raw_probability(bundle["classifier"], changed) <= 1)
        self.assertEqual(before, bundle)

    def test_positive_expected_return_does_not_require_fifty_percent_probability(self):
        row = training_rows(1)[0]
        model = dict(names=[], medians=[], means=[], stds=[], coefficients=[],
                     intercept=logit(.4), scaled_clip=5)
        bundle = dict(classifier=model, calibrator=dict(slope=1, intercept=0),
            payoffs=dict(groups={"3plus":dict(win_r=3, loss_r=1, n=100)}, losses_over2r=2))
        result = predict(bundle, row["features"])
        self.assertFalse(result["probability_accept"])
        self.assertTrue(result["expectancy_accept"])
        self.assertAlmostEqual(result["expected_net_r"], .6)

    def test_future_or_unknown_labels_are_not_training_losses(self):
        rows = training_rows(3)
        snapshots = {r["event_key"]:dict(features=r["features"]) for r in rows}
        cases = [dict(event_key=r["event_key"], warning_ts=r["warning_ts"],
                      result=dict(evaluation=dict(status="closed", net_r=r["net_r"],
                         entry_ts=r["entry_ts"], exit_ts=r["exit_ts"]))) for r in rows]
        cases[1]["result"]["evaluation"]["status"] = "ambiguous"
        cases[2]["result"]["evaluation"]["exit_ts"] = "2026-10-02 09:00:00"
        kept = observed_training(cases, snapshots, "2026-10-01 00:00:00")
        self.assertEqual([r["event_key"] for r in kept], ["0"])

    def test_capacity_is_recomputed_after_model_rejection(self):
        def trade(key, start, end):
            return dict(event_key=key, symbol="SR0", warning_ts="2026-08-03 09:15:00",
                result=dict(evaluation=dict(status="closed", net_r=1, net_amount=100,
                                            entry_ts=start, exit_ts=end)))
        a = trade("a", "2026-08-03 09:20:00", "2026-08-03 10:30:00")
        b = trade("b", "2026-08-03 09:25:00", "2026-08-03 10:00:00")
        self.assertEqual([r["event_key"] for r in accepted_rows([a,b], {})[1]], ["a"])
        self.assertEqual([r["event_key"] for r in accepted_rows([b], {})[1]], ["b"])

    def test_unknown_opportunity_still_reserves_capacity(self):
        unknown = dict(event_key="unknown", symbol="SR0", warning_ts="2026-08-03 09:15:00",
            result=dict(evaluation=dict(status="ambiguous", entry_ts="2026-08-03 09:20:00",
                                         exit_ts=None, net_r=None, net_amount=None)))
        candidate = dict(event_key="later", symbol="SR0", warning_ts="2026-08-03 09:30:00",
            result=dict(evaluation=dict(status="closed", entry_ts="2026-08-03 09:35:00",
                                         exit_ts="2026-08-03 10:00:00", net_r=1, net_amount=100)))
        symbols = dict(SR0=dict(bars=[dict(datetime="2026-08-03 09:15:00"),
                                      dict(datetime="2026-08-03 15:00:00")]))
        _, accepted = accepted_rows([unknown, candidate], symbols)
        self.assertEqual([r["event_key"] for r in accepted], ["unknown"])

    def test_preview_distinguishes_structural_limit_from_model_evidence(self):
        e = event()
        snapshot = dict(event_key="a", symbol=e["symbol"], warning_ts=e["warning_ts"],
            geometry=geometry(e, FEE, 10), stop=e["stop"], target=e["target"],
            structural_entry_price_limit=price_limit(e, FEE, 10),
            price_limit_kind="structural only", rule_warning_eligible=True)
        prediction = dict(p_win=.4, expected_net_r=.2, expectancy_accept=True,
                          break_even_probability=.35, payoff_group_n=20)
        preview = entry_preview(snapshot, prediction)
        self.assertFalse(preview["qualified_for_trading"])
        self.assertTrue(preview["price_changed_requires_rescore"])
        self.assertNotIn("net_r", preview)

    def test_future_outcome_mutation_cannot_change_selected_training_data(self):
        rows = training_rows(40)
        cutoff = "2026-07-01 00:00:00"
        snapshots = {r["event_key"]:dict(features=r["features"]) for r in rows}
        cases = [dict(event_key=r["event_key"], warning_ts=r["warning_ts"],
                      result=dict(evaluation=dict(status="closed", net_r=r["net_r"],
                      entry_ts=r["entry_ts"], exit_ts=r["exit_ts"]))) for r in rows]
        before = observed_training(cases, snapshots, cutoff)
        for case in cases:
            if case["result"]["evaluation"]["exit_ts"] >= cutoff:
                case["result"]["evaluation"]["net_r"] = 1000
        self.assertEqual(before, observed_training(cases, snapshots, cutoff))


if __name__ == "__main__":
    unittest.main()
