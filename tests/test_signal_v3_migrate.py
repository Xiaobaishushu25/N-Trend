import sys, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from migrate_signal_v3 import assert_quiescent, assert_managed_continuity, migrate
import json, tempfile, sqlite3
import copy, time
from contextlib import closing
class QuiescentTests(unittest.TestCase):
 def continuity_fixture(self):
  c={'id':'c','event_id':1,'symbol':'PB0','instrument_id':'pb2611','direction':'up','state':'filled','entry_price':1000.,'stop_price':980.,'target_price':1100.,'risk':20.,'volume':2,'score':4.,'created_at':1,'warning_bar_ts':'2026-10-22 09:15:00'}
  p={'id':'c','condition_order_id':'c','instrument_id':'pb2611','direction':'up','volume':1,'is_closed':False,'fill_price':1001.,'risk':21.,'initial_stop_price':980.,'current_stop_price':1001.,'target_price':1100.,'lot1_closed':True,'trailing_grade':1}
  state={'account':'9999:fixture','conditions':{'c':c},'positions':{'c':p},'orders':{'1':{'phase':'Terminal','filled':2,'reported_filled':2}},'history':[{'id':'first-leg','event_id':1,'realized_pnl_amount':105.}],'closes':{'historical':{'filled':1,'target':1}},'comparison':{'original':'preserved'},'seen_trades':['known-fill'],'last_ref':12345}
  snap={'health':{'ready':True,'positions_updated_at':time.time(),'broker_id':'9999','user_id':'fixture'},'positions':[{'instrument_id':'pb2611','direction':'buy','position':1}]}
  return state,snap
 def test_continuity_requires_fresh_matching_reconciled_counter(self):
  state,snapshot=self.continuity_fixture();assert_managed_continuity(state,snapshot)
  bad=copy.deepcopy(snapshot);bad['health']['positions_updated_at']-=31
  with self.assertRaises(ValueError):assert_managed_continuity(state,bad)
  bad=copy.deepcopy(snapshot);bad['positions'][0]['position']=2
  with self.assertRaises(ValueError):assert_managed_continuity(state,bad)
  bad=copy.deepcopy(snapshot);bad['health']['user_id']='other'
  with self.assertRaises(ValueError):assert_managed_continuity(state,bad)
  for phase in ['Unknown','Working','CancelPending']:
   bad=copy.deepcopy(state);bad['orders']['1']['phase']=phase
   with self.assertRaises(ValueError):assert_managed_continuity(bad,snapshot)
  bad=copy.deepcopy(state);bad['conditions']['new']={'state':'pending'}
  with self.assertRaises(ValueError):assert_managed_continuity(bad,snapshot)
 def test_continuity_keeps_position_protection_ledger_and_original_strategy(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp);data=root/'production';data.mkdir()
   with closing(sqlite3.connect(data/'ntrend.db')) as db:
    db.executescript("CREATE TABLE klines(symbol,timeframe,ts,source,close); INSERT INTO klines VALUES('PB0','5m','2026-10-22','raw',1000); CREATE TABLE pattern_events(id INTEGER PRIMARY KEY AUTOINCREMENT,entry); INSERT INTO pattern_events(entry) VALUES(1000); CREATE TABLE settings(key PRIMARY KEY,value); CREATE TABLE devices(id); INSERT INTO devices VALUES('original');")
    db.commit()
   state,snapshot=self.continuity_fixture();before=copy.deepcopy(state)
   (data/'trader_execution.json').write_text(json.dumps(state),encoding='utf-8')
   (data/'trader_config.json').write_text(json.dumps({'mode':'FullAuto','max_risk_percent':1,'default_large_volume':2,'default_small_volume':1,'daily_loss_limit_r':3}),encoding='utf-8')
   result=migrate(data,root/'archive',True,snapshot);self.assertTrue(result['preserved_managed_trades'])
   saved=json.loads((data/'trader_execution.json').read_text())
   for key in ['positions','orders','closes','comparison','seen_trades','last_ref']:self.assertEqual(saved[key],before[key])
   frozen=saved['conditions']['c']['frozen'];self.assertEqual(frozen['definition']['strategy_id'],'n_pattern');self.assertEqual(frozen['definition']['exit_policy'],'n_exit_v1')
   self.assertEqual(frozen['execution']['default_large_volume'],2)
   self.assertEqual(frozen['plan']['evidence']['source_evidence']['signal']['entry'],1000)
   self.assertEqual(saved['history'][0]['realized_pnl_amount'],105.)
   with closing(sqlite3.connect(data/'ntrend.db')) as db:
    self.assertEqual(db.execute('select count(*) from pattern_events').fetchone()[0],0)
    self.assertGreater(int(db.execute("select value from settings where key='execution_signal_not_before'").fetchone()[0]),0)
    self.assertEqual(db.execute('select id from devices').fetchone()[0],'original')
   self.assertEqual(json.loads((root/'archive/trader_execution.json').read_text()),before)
   self.assertEqual(migrate(data,root/'unused',True,snapshot)['status'],'already_migrated')
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
