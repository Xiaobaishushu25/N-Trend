import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
from report_score_scan import ranks, associations, stats


class ScoreAssociationTest(unittest.TestCase):
    def rows(self):
        return [dict(scores=dict(s=x), outcomes=dict(t=dict(status='closed', net_r=y, exit_ts='2026-01-01')))
                for x, y in [(1, -1), (2, -1), (3, 1), (4, 1)]]

    def test_tied_ranks_and_perfect_separation(self):
        self.assertEqual(ranks([3, 1, 1, 4]), [3, 1.5, 1.5, 4])
        self.assertEqual(associations(self.rows(), 's', 't')['auc_net_win'], 1)

    def test_ties_have_no_auc_edge_and_unknowns_are_not_losses(self):
        rows = self.rows()
        for row in rows:
            row['scores']['s'] = 2
        rows.append(dict(scores=dict(s=0), outcomes=dict(t=dict(status='ambiguous', net_r=None))))
        self.assertEqual(associations(rows, 's', 't')['auc_net_win'], .5)
        result = stats(rows, 't')
        self.assertEqual(result['candidates'], 5)
        self.assertEqual(result['closed_priced'], 4)
        self.assertEqual(result['win_rate'], .5)
        self.assertEqual(result['mean_net_r'], 0)


if __name__ == '__main__':
    unittest.main()
