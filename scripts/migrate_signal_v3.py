"""Offline production upgrade. Never stops a server, cancels an order or closes a position."""
import argparse, json, os, shutil, sqlite3, time
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

def migrate(data_dir,archive_dir):
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
    old_bytes=state_path.read_bytes();state=json.loads(old_bytes);assert_quiescent(state)
    with ro(database) as db:
        marker=db.execute("SELECT value FROM settings WHERE key='signal_v3_prepared'").fetchone()
        if marker and marker[0]=='1':return {"status":"already_migrated"}
        db.row_factory=sqlite3.Row
        old_signals={r["id"]:dict(r) for r in db.execute("SELECT * FROM pattern_events")}
    staging=data_dir/"signal-v3-prepared"
    result=prepare(database,archive_dir,staging,origin="offline-production-upgrade",research=False)
    (archive_dir/"trader_execution.json").write_bytes(old_bytes)
    # Factual history remains in the operational state with the original signal evidence.
    for record in state.get("history",[]):
        event_id=record.get("event_id")
        if event_id is not None:record["signal_evidence"]={"origin":"pre-v3-archive","event_id":event_id,"archive_sha256":result["source_sha256"],"signal":old_signals.get(event_id)}
    state["authorized_entries"]=[]
    state["aborted_entries"]=list(set(state.get("aborted_entries",[]))|set(state.get("conditions",{})))
    state["comparison"]={}
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
    return {"status":"migrated","generation":result["generation"],"source_sha256":result["source_sha256"],"execution_archive_sha256":digest(archive_dir/"trader_execution.json")}

def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--data-dir',required=True,type=Path);ap.add_argument('--archive-dir',required=True,type=Path);a=ap.parse_args();print(json.dumps(migrate(a.data_dir,a.archive_dir),indent=2))
if __name__=='__main__':main()
