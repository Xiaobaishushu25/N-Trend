import datetime as dt
import hashlib
import pathlib
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
from audit_score_history import ZONE, crosscheck
from reprice_signal_costs import TradingDates


class HistoryAuditTest(unittest.TestCase):
    def test_inventory_discrepancies_night_mapping_and_read_only_source(self):
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / 'source.sqlite'
            conn = sqlite3.connect(path)
            conn.executescript('''CREATE TABLE mappings(product,effective_date,contract,source,source_sha256);
                CREATE TABLE bars(contract,period_seconds,start_ns,end_ns,open,high,low,close,complete,constituent_count);
                CREATE TABLE metadata(key,value); CREATE TABLE requests(status);''')
            conn.executemany('INSERT INTO mappings VALUES (?,?,?,?,?)', [
                ('EX.t', '2026-09-29', 'EX.t01', 'test', 'h'),
                ('EX.t', '2026-09-30', 'EX.t02', 'test', 'h')])
            def insert(ts, price=100, complete=1, contract='EX.t01'):
                end = int(dt.datetime.fromisoformat(ts).replace(tzinfo=ZONE).timestamp()) * 1_000_000_000
                conn.execute('INSERT INTO bars VALUES (?,?,?,?,?,?,?,?,?,?)',
                             (contract, 300, end - 300_000_000_000, end, price, price + 1, price - 1,
                              price, complete, 5 if complete else 4))
            insert('2026-09-29 09:05:00')
            insert('2026-09-29 09:10:00', price=90)
            insert('2026-09-29 09:20:00', complete=0)
            insert('2026-09-29 21:05:00', contract='EX.t02')
            conn.commit()
            conn.close()
            class Market:
                pass
            market = Market()
            market.bars = dict(T0=[dict(ts=ts, open=100, high=101, low=99, close=100) for ts in
                                  ['2026-09-29 09:05:00', '2026-09-29 09:10:00', '2026-09-29 09:15:00',
                                   '2026-09-29 09:20:00', '2026-09-29 21:05:00']])
            market.calendar = TradingDates(dict(all=dict(bars=[dict(datetime='2026-09-29 09:00:00'),
                                                               dict(datetime='2026-09-30 09:00:00')])))
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            inventory, discrepancies = crosscheck(market, path)
            self.assertEqual(inventory['counts']['T0'], dict(ohlc_match=2, ohlc_mismatch=1,
                                                            source_bar_absent=1, source_bar_incomplete=1))
            self.assertEqual(len(discrepancies), 3)
            self.assertEqual(inventory['bars_5m'], 4)
            self.assertEqual(sum(r['incomplete_5m'] for r in inventory['contracts']), 1)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)


if __name__ == '__main__':
    unittest.main()
