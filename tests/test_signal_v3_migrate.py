import sys, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from migrate_signal_v3 import assert_quiescent, migrate
import json, tempfile, sqlite3
from contextlib import closing
class QuiescentTests(unittest.TestCase):
 def test_active_and_unknown_records_block_cleanup(self):
  for state in [{'conditions':{'x':{'state':'Pending'}}},{'positions':{'x':{'is_closed':False}}},{'orders':{'x':{'phase':'Unknown'}}},{'closes':{'x':{}}}]:
   with self.assertRaises(ValueError): assert_quiescent(state)
 def test_closed_records_are_retained(self):
  state={'conditions':{'x':{'state':'Closed'}},'positions':{'x':{'is_closed':True}},'orders':{'1':{'phase':'Terminal','filled':1,'reported_filled':1}},'history':[{'id':'trade'}]}
  assert_quiescent(state);self.assertEqual(state['history'][0]['id'],'trade')
 def test_offline_migration_keeps_trade_evidence_and_server_identity(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp);data=root/'production';data.mkdir();db_path=data/'ntrend.db'
   with closing(sqlite3.connect(db_path)) as db:
    db.executescript("CREATE TABLE klines(symbol,timeframe,ts,source,close); INSERT INTO klines VALUES('RB','5m','2026-01-01','raw',100); CREATE TABLE pattern_events(id INTEGER PRIMARY KEY AUTOINCREMENT,entry); INSERT INTO pattern_events(entry) VALUES(100); CREATE TABLE settings(key PRIMARY KEY,value); CREATE TABLE devices(id); INSERT INTO devices VALUES('original-device');")
    db.commit()
   state={'conditions':{},'positions':{},'orders':{},'history':[{'id':'real-fill','event_id':1,'realized_pnl_amount':120}],'comparison':{'old':'result'}}
   state_file=data/'trader_execution.json';state_file.write_text(json.dumps(state),encoding='utf-8')
   result=migrate(data,root/'archive');self.assertEqual(result['status'],'migrated')
   saved=json.loads(state_file.read_text());self.assertEqual(saved['history'][0]['realized_pnl_amount'],120);self.assertEqual(saved['history'][0]['signal_evidence']['signal']['entry'],100)
   with closing(sqlite3.connect(db_path)) as db:
    self.assertEqual(db.execute('SELECT id FROM devices').fetchone()[0],'original-device')
    db.execute('INSERT INTO pattern_events(entry) VALUES(200)');self.assertEqual(db.execute('SELECT id FROM pattern_events').fetchone()[0],2)
   self.assertEqual(migrate(data,root/'unused')['status'],'already_migrated')
if __name__=='__main__':unittest.main()
