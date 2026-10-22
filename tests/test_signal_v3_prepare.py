from contextlib import closing
import importlib.util, sqlite3, tempfile, unittest
from pathlib import Path
spec=importlib.util.spec_from_file_location("prepare",Path(__file__).resolve().parents[1]/"scripts/prepare_signal_v3.py")
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class PrepareTests(unittest.TestCase):
 def test_archive_cleanup_and_identity(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp); source=root/"source.db"
   with closing(sqlite3.connect(source)) as db:
    db.executescript("CREATE TABLE klines(symbol,timeframe,ts,source,close); INSERT INTO klines VALUES('RB','5m','2026-01-01','raw',1); INSERT INTO klines VALUES('RB','15m','2026-01-01','derived',1); CREATE TABLE pattern_events(id INTEGER PRIMARY KEY AUTOINCREMENT); INSERT INTO pattern_events DEFAULT VALUES; CREATE TABLE settings(key PRIMARY KEY,value); CREATE TABLE signal_annotations(id,event_id,content); INSERT INTO signal_annotations VALUES(1,1,'note'); CREATE TABLE trades(id,amount); INSERT INTO trades VALUES(1,10);")
    db.commit()
   result=m.prepare(source,root/"archive",root/"work")
   self.assertEqual(result['before']['signal_annotations'],1)
   with m.ro(source) as db: self.assertEqual(db.execute('SELECT count(*) FROM pattern_events').fetchone()[0],1)
   with closing(sqlite3.connect(root/"work/ntrend.db")) as db:
    self.assertEqual(db.execute('SELECT count(*) FROM signal_annotations').fetchone()[0],0)
    self.assertEqual(db.execute('SELECT amount FROM trades').fetchone()[0],10)
    self.assertEqual(db.execute('SELECT count(*) FROM klines').fetchone()[0],1)
    db.execute('INSERT INTO pattern_events DEFAULT VALUES');self.assertEqual(db.execute('SELECT id FROM pattern_events').fetchone()[0],2)
   with self.assertRaises(ValueError): m.prepare(source,root/"archive2",root/"work")
if __name__=='__main__': unittest.main()
