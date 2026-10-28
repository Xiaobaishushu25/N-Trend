"""Run resumable history collection, then check and merge completed data."""
import datetime as dt
import json
import pathlib
import sqlite3
import subprocess
import sys
import time

import market_history_store as store

START=dt.date(2023,10,28)
END=dt.date(2025,10,28)
STAGE=store.ROOT/'data/research_market_backfill.sqlite'
STATUS=store.ROOT/'data/research_market_backfill_status.json'


def status(state,**extra):
    obj=dict(state=state,updated_at=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(),**extra)
    temp=STATUS.with_suffix('.tmp');temp.write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding='utf-8');temp.replace(STATUS)
    print(json.dumps(obj,ensure_ascii=False),flush=True)


def missing_windows(db):
    missing=[];day=START
    while day<=END:
        end=min(END,day+dt.timedelta(days=13))
        row=db.execute('SELECT status FROM backfill_windows WHERE start_date=? AND end_date=?',(day.isoformat(),end.isoformat())).fetchone()
        if not row or row[0]!='ok':missing.append([str(day),str(end),row[0] if row else 'missing'])
        day=end+dt.timedelta(days=1)
    return missing


def merge():
    stage=sqlite3.connect(STAGE)
    if stage.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RuntimeError('Backfill integrity check failed')
    missing=missing_windows(stage)
    if missing:raise RuntimeError('Incomplete windows: '+json.dumps(missing))
    warnings=[]
    for (detail,) in stage.execute("SELECT details FROM backfill_batches WHERE status='ok'"):
        obj=json.loads(detail)
        for product,contracts in obj['products'].items():
            if not any(obj['counts'].get(c,0)>0 for c in contracts):warnings.append(product)
    if warnings:raise RuntimeError('No observed bars for products in completed batches: '+','.join(warnings[:20]))
    stage.close()
    db=store.connect(store.DB);db.execute('ATTACH DATABASE ? AS history',(str(STAGE),))
    conflict_sql='''SELECT count(*) FROM history.bars h JOIN main.bars m
       ON h.contract=m.contract AND h.period_seconds=m.period_seconds AND h.start_ns=m.start_ns
       WHERE h.period_seconds=60 AND (abs(h.open-m.open)>1e-7 OR abs(h.high-m.high)>1e-7
       OR abs(h.low-m.low)>1e-7 OR abs(h.close-m.close)>1e-7 OR h.volume!=m.volume
       OR h.open_oi IS NOT m.open_oi OR h.close_oi IS NOT m.close_oi)'''
    conflicts=db.execute(conflict_sql).fetchone()[0]
    if conflicts:db.close();raise RuntimeError('Overlapping source bars conflict: '+str(conflicts))
    symbols=[r[0] for r in db.execute('SELECT DISTINCT contract FROM history.bars WHERE period_seconds=60')]
    with db:
        db.execute('INSERT OR IGNORE INTO main.bars SELECT * FROM history.bars WHERE period_seconds=60')
        db.execute('INSERT OR IGNORE INTO main.mappings SELECT * FROM history.mappings')
        db.execute('''CREATE TABLE IF NOT EXISTS backfill_batches(start_date TEXT,end_date TEXT,batch TEXT,status TEXT,
            details TEXT,PRIMARY KEY(start_date,end_date,batch))''')
        db.execute('INSERT OR REPLACE INTO main.backfill_batches SELECT * FROM history.backfill_batches')
    for index,symbol in enumerate(symbols):
        with db:store.aggregate(db,symbol)
        if index%20==0:status('aggregating',completed=index+1,total=len(symbols))
    integrity=db.execute('PRAGMA main.integrity_check').fetchone()[0]
    if integrity!='ok':db.close();raise RuntimeError('Research database integrity check failed')
    counts=db.execute('SELECT period_seconds,count(*),count(distinct contract),min(start_ns),max(start_ns) FROM bars GROUP BY period_seconds ORDER BY period_seconds').fetchall()
    db.close()
    status('complete',bars=counts,contracts_aggregated=len(symbols),overlap_conflicts=0,
           limitations='Calendar completeness, roll timing and historical contract specifications still require separate audit.')


def main():
    try:
        for attempt in range(1,4):
            status('collecting',attempt=attempt,start=str(START),end=str(END))
            command=[sys.executable,'-u',str(store.ROOT/'scripts/backfill_tq_minutes.py'),
                     '--start',str(START),'--end',str(END),'--db',str(STAGE),'--timeout','75']
            result=subprocess.run(command,cwd=store.ROOT)
            if result.returncode!=0:raise RuntimeError('Collector exited with code '+str(result.returncode))
            db=sqlite3.connect(STAGE);missing=missing_windows(db);db.close()
            if not missing:break
            status('retrying',attempt=attempt,missing_windows=len(missing),examples=missing[:5])
            time.sleep(10)
        else:raise RuntimeError('Still incomplete after three passes')
        status('validating');merge()
    except Exception as exc:
        status('failed',error_type=type(exc).__name__,message=str(exc)[:700]);raise

if __name__=='__main__':main()
