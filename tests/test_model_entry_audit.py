import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
from report_model_entry_audit import stats, validate_oof


def row(key, net_r):
    return dict(event_id=key, r_multiple=net_r, label_win=int(net_r > 0),
                trigger_bar_ts="2026-10-20 09:15:00", exit_ts="2026-10-20 09:20:00")


class ModelEntryAuditTests(unittest.TestCase):
    def test_missing_or_misaligned_predictions_are_rejected(self):
        with self.assertRaises(ValueError):
            validate_oof(dict(rows=[row("a", 1)], p_win=[]))

    def test_duplicate_predictions_cannot_inflate_sample_size(self):
        with self.assertRaises(ValueError):
            validate_oof(dict(rows=[row("a", 1), row("a", 1)], p_win=[.6, .6]))

    def test_unknown_return_is_not_treated_as_zero(self):
        sample = row("a", 1)
        sample["r_multiple"] = None
        with self.assertRaises(ValueError):
            validate_oof(dict(rows=[sample], p_win=[.6]))

    def test_inconsistent_label_or_probability_is_rejected(self):
        sample = row("a", -1)
        sample["label_win"] = 1
        with self.assertRaises(ValueError):
            validate_oof(dict(rows=[sample], p_win=[.6]))
        with self.assertRaises(ValueError):
            validate_oof(dict(rows=[row("a", 1)], p_win=[float("nan")]))

    def test_high_win_rate_can_still_have_negative_expectancy(self):
        pairs = [(row(str(i), r), .7) for i, r in enumerate([1, 1, 1, -4])]
        result = stats(pairs)
        self.assertEqual(result["net_win_rate"], .75)
        self.assertEqual(result["mean_net_r"], -.25)
        self.assertEqual(result["descriptive_break_even_probability"], .8)
        self.assertEqual(result["net_r_without_top5"], -4)

    def test_empty_group_has_no_fabricated_win_rate(self):
        self.assertIsNone(stats([])["net_win_rate"])
        self.assertIsNone(stats([])["mean_net_r"])


if __name__ == "__main__":
    unittest.main()
