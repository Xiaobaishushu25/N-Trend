import importlib.util
import pathlib
import tempfile
import unittest

PATH = pathlib.Path(__file__).resolve().parents[1] / 'scripts' / 'market_history_store.py'
spec = importlib.util.spec_from_file_location('market_history_store', PATH)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class AggregationTest(unittest.TestCase):
    def test_gap_splits_bucket_and_marks_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            db = m.connect(pathlib.Path(directory) / 'test.sqlite')
            minute = 60_000_000_000
            rows = [('TEST',60,n*minute,(n+1)*minute,n*minute,10+n,11+n,9+n,10+n,1,3,4,1,1,'fixture','raw-v1')
                    for n in (0,1,3,4,5,6,7,8,9)]
            db.executemany('INSERT INTO bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',rows)
            m.aggregate(db,'TEST')
            bars = db.execute('SELECT start_ns,end_ns,constituent_count,complete,volume FROM bars WHERE period_seconds=300 ORDER BY start_ns').fetchall()
            self.assertEqual(bars, [(0,2*minute,2,0,2),(3*minute,5*minute,2,0,2),(5*minute,10*minute,5,1,5)])
            m.aggregate(db,'TEST')
            self.assertEqual(db.execute('SELECT count(*) FROM bars WHERE period_seconds=300').fetchone()[0],3)
            db.close()

if __name__ == '__main__': unittest.main()
