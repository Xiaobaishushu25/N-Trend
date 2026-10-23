"""Offline production upgrade. Never stops a server, cancels an order or closes a position."""
import argparse, collections, json, math, os, shutil, sqlite3, time
from pathlib import Path
from contextlib import closing
from prepare_signal_v3 import prepare, ro, digest

def assert_quiescent(state):
    allowed={"expired","canceled","invalidated","closed","rejected"}
    if state.get("closes"): raise ValueError("pending close tasks; cleanup refused")
    for c in state.get("conditions",{}).values():
        if str(c.get("state","")).lower() not in allowed: raise ValueError("active/unknown condition; cleanup refused")
    for pos in state.get("positions",{}).values():
        if not pos.get("is_closed",False): raise ValueError("managed exposure; cleanup refused")
    for order in state.get("orders",{}).values():
        if order.get("phase") not in ("Terminal","Rejected") or order.get("filled",0)<order.get("reported_filled",0): raise ValueError("active/unreconciled order; cleanup refused")

def assert_managed_continuity(state, snapshot, now=None):
    """Allow existing managed trades only when a fresh counter snapshot reconciles.
    No working/unknown orders are accepted across this maintenance boundary.
    """
    now=time.time() if now is None else now
    health=snapshot.get('health',{})
    if not health.get('ready') or not 0<=now-health.get('positions_updated_at',0)<=30:
        raise ValueError('fresh synchronized broker positions are required')
    identity=state.get('account','')
    if identity and identity!=f"{health.get('broker_id','')}:{health.get('user_id','')}":
        raise ValueError('broker account identity mismatch')
    for order in state.get('orders',{}).values():
        if order.get('phase') not in ('Terminal','Rejected') or order.get('filled',0)!=order.get('reported_filled',0):
            raise ValueError('active/unreconciled order; continuity migration refused')
    expected=collections.Counter()
    for key,pos in state.get('positions',{}).items():
        if pos.get('is_closed'):continue
        cond=state.get('conditions',{}).get(pos.get('condition_order_id',key))
        if not cond or cond.get('state') not in ('filled','part_filled','closing'):
            raise ValueError('live position lacks its managed condition')
        qty=pos.get('volume',0)
        if not isinstance(qty,int) or qty<=0 or pos.get('direction') not in ('up','down'):
            raise ValueError('invalid managed position')
        if any(not isinstance(pos.get(k), (int,float)) or not math.isfinite(pos[k]) or pos[k]<=0 for k in ('fill_price','risk','initial_stop_price')):
            raise ValueError('invalid managed protection')
        if pos.get('instrument_id','').upper()!=cond.get('instrument_id','').upper() or pos.get('direction')!=cond.get('direction'):
            raise ValueError('position and condition disagree')
        expected[(pos['instrument_id'].upper(),'buy' if pos['direction']=='up' else 'sell')]+=qty
    actual=collections.Counter()
    for pos in snapshot.get('positions',[]):
        if pos.get('position',0)>0:actual[(pos['instrument_id'].upper(),pos['direction'])]+=pos['position']
    if actual!=expected:raise ValueError('broker and managed positions disagree')
    for cond in state.get('conditions',{}).values():
        if cond.get('state') in ('pending','triggered','waiting_confirm','working','unknown'):
            raise ValueError('cancel unfilled entry conditions before continuity migration')

def freeze_legacy_conditions(state,config,old_signals,archive_sha):
    definition={'strategy_id':'n_pattern','code_version':'1','revision':'1','exit_policy':'n_exit_v1','sizing_policy':'n_score_v1','parameters':None}
    for key,cond in state.get('conditions',{}).items():
        if cond.get('frozen') is not None:continue
        event_id=cond.get('event_id')
        evidence={'origin':'pre-v3-archive','event_id':event_id,'archive_sha256':archive_sha,'signal':old_signals.get(event_id)}
        cond['frozen']={'definition':definition,'execution':config,'plan':{
            'signal_id':str(event_id) if event_id is not None else key,'symbol':cond['symbol'],'instrument_id':cond['instrument_id'],
            'direction':'long' if cond['direction']=='up' else 'short','reference_price':cond['entry_price'],
            'trigger':{'kind':'breakout','price':cond['entry_price']},'stop_price':cond['stop_price'],'target_price':cond['target_price'],
            'risk_per_unit':cond['risk'],'requested_volume':cond['volume'],'created_at':cond['created_at'],'expires_at':None,
            'reason':cond.get('note',''),'evidence':{'entry_score':cond['score'],'id':event_id,'warning_ts':cond['warning_bar_ts'],'source_evidence':evidence}}}

def migrate(data_dir,archive_dir,preserve_managed=False,broker_snapshot=None):
    data_dir=Path(data_dir).resolve();archive_dir=Path(archive_dir).resolve();database=data_dir/"ntrend.db";state_path=data_dir/"trader_execution.json"
    if not database.is_file() or not state_path.is_file():raise ValueError("production database and execution state are required")
    flag=data_dir/"signal-v3-migration-in-progress"
    if flag.exists():raise ValueError("incomplete migration; restore verified database and execution-state archive before restarting")
    pid_path=data_dir/"ntrend.lock"
    if pid_path.exists():
        pid=int(pid_path.read_text().strip())
        if pid<=0:raise ValueError("invalid server lock PID")
        if os.name=='nt':
            import subprocess,csv,io
            found=subprocess.run(['tasklist','/FI',f'PID eq {pid}','/FO','CSV','/NH'],capture_output=True,text=True,creationflags=subprocess.CREATE_NO_WINDOW,check=True)
            running=any(len(row)>1 and row[1]==str(pid) for row in csv.reader(io.StringIO(found.stdout)))
        else:
            try:os.kill(pid,0);running=True
            except ProcessLookupError:running=False
        if running:raise ValueError("server is running; choose an offline maintenance window")
    old_bytes=state_path.read_bytes();state=json.loads(old_bytes)
    with ro(database) as db:
        marker=db.execute("SELECT value FROM settings WHERE key='signal_v3_prepared'").fetchone()
        if marker and marker[0]=='1':return {"status":"already_migrated"}
        db.row_factory=sqlite3.Row
        old_signals={r["id"]:dict(r) for r in db.execute("SELECT * FROM pattern_events")}
    if preserve_managed:
        if broker_snapshot is None:raise ValueError('continuity migration requires a broker snapshot')
        assert_managed_continuity(state,broker_snapshot)
    else:assert_quiescent(state)
    staging=data_dir/"signal-v3-prepared"
    result=prepare(database,archive_dir,staging,origin="offline-production-upgrade")
    (archive_dir/"trader_execution.json").write_bytes(old_bytes)
    if preserve_managed:
        (archive_dir/'broker-snapshot.json').write_text(json.dumps(broker_snapshot,ensure_ascii=False),encoding='utf-8')
        os.chmod(archive_dir/'broker-snapshot.json',0o600)
        config=json.loads((data_dir/'trader_config.json').read_text(encoding='utf-8'))
        freeze_legacy_conditions(state,config,old_signals,result['source_sha256'])
    # Factual history remains in the operational state with the original signal evidence.
    for record in state.get("history",[]):
        event_id=record.get("event_id")
        if event_id is not None and record.get('signal_evidence') is None:record["signal_evidence"]={"origin":"pre-v3-archive","event_id":event_id,"archive_sha256":result["source_sha256"],"signal":old_signals.get(event_id)}
    if not preserve_managed:
        state["authorized_entries"]=[]
        state["aborted_entries"]=list(set(state.get("aborted_entries",[]))|set(state.get("conditions",{})))
        state["comparison"]={}
    # Rebuilt past signals are research evidence, never new executable opportunities.
    with closing(sqlite3.connect(staging/'ntrend.db')) as db:
        db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('execution_signal_not_before',?)",(str(int(time.time())),));db.commit()
    # Checkpoint before atomic replacement; stale WAL must never be applied to a new main file.
    with closing(sqlite3.connect(database)) as db:db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    wal=Path(str(database)+"-wal")
    if wal.exists() and wal.stat().st_size:raise ValueError("WAL remains nonempty; migration refused")
    flag.write_text(json.dumps({"archive":str(archive_dir),"started_at":time.time()}),encoding="utf-8")
    try:
        replacement=data_dir/"ntrend.v3.sqlite";shutil.copyfile(staging/"ntrend.db",replacement);os.replace(replacement,database)
        temp=data_dir/"trader_execution.v3.json";temp.write_text(json.dumps(state,ensure_ascii=False),encoding="utf-8");os.replace(temp,state_path)
        with ro(database) as db:
            if db.execute("PRAGMA integrity_check").fetchone()[0]!="ok":raise ValueError("upgraded database failed integrity")
        flag.unlink()
    except BaseException:
        shutil.copyfile(archive_dir/"source.sqlite",database);state_path.write_bytes(old_bytes)
        # Keep the flag: an operator must inspect the restored files before a restart.
        raise
    return {"status":"migrated","preserved_managed_trades":preserve_managed,"generation":result["generation"],"source_sha256":result["source_sha256"],"execution_archive_sha256":digest(archive_dir/"trader_execution.json")}

def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--data-dir',required=True,type=Path);ap.add_argument('--archive-dir',required=True,type=Path)
    ap.add_argument('--preserve-managed-trades',action='store_true');ap.add_argument('--broker-snapshot',type=Path)
    a=ap.parse_args();snapshot=json.loads(a.broker_snapshot.read_text(encoding='utf-8')) if a.broker_snapshot else None
    print(json.dumps(migrate(a.data_dir,a.archive_dir,a.preserve_managed_trades,snapshot),indent=2))
if __name__=='__main__':main()
