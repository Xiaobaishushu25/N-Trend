"""Resumable research archive using public TqBacktest K-line serial windows.

Windows contain completed one-minute bars only. No account orders are placed.
Output is isolated from the existing EDB research database until separately audited.
"""
import argparse
import bisect
import datetime as dt
import hashlib
import json
import math
import pathlib
import sqlite3
import time

import market_history_store as store

ZONE=dt.timezone(dt.timedelta(hours=8))
NS=1_000_000_000
STEP=60*NS
DEFAULT=store.ROOT/'data/research_market_backfill.sqlite'


def products():
    return [exchange+'.'+name for exchange,names in store.PRODUCTS.items() for name in names]


def anchor_for_end(end):
    anchor=end+dt.timedelta(days=4)
    while anchor.weekday()>=5:anchor+=dt.timedelta(days=1)
    return anchor


def selections(table,start,end):
    selected={}
    for product in products():
        entries=table.get(product,[]);days=[x[0] for x in entries]
        left=bisect.bisect_right(days,start.strftime('%Y%m%d'))-1
        if left<0:continue
        right=bisect.bisect_right(days,end.strftime('%Y%m%d'))
        selected[product]=[x[1] for x in entries[left:right]]
    return selected


def gather(anchor, start, end, symbols, timeout):
    from tqsdk import TqApi,TqAuth,TqSim,TqBacktest
    cfg=json.loads((store.ROOT/'data/config.json').read_text(encoding='utf-8'))['data_source']
    secrets=json.loads((store.ROOT/'data/secrets.json').read_text(encoding='utf-8'))
    begin=int(dt.datetime.combine(start,dt.time(),ZONE).timestamp())*NS
    cutoff=int(dt.datetime.combine(end+dt.timedelta(days=1),dt.time(),ZONE).timestamp())*NS
    frames={};api=None;pending=set(symbols);started=time.monotonic()
    try:
        api=TqApi(TqSim(),auth=TqAuth(cfg['tq_account'],cfg.get('tq_password') or secrets.get('tq_password')),
                  backtest=TqBacktest(start_dt=anchor,end_dt=anchor+dt.timedelta(days=14)),
                  disable_print=True,web_gui=False)
        async def subscribe(symbol):
            frame=api.get_kline_serial(symbol,60,data_length=8000)
            frames[symbol]=frame
        tasks=[api.create_task(subscribe(s)) for s in symbols]
        deadline=time.monotonic()+timeout;last_report=time.monotonic()
        while pending and time.monotonic()<deadline:
            api.wait_update(deadline=time.time()+3)
            for task in tasks:
                if task.done() and task.exception() is not None: raise task.exception()
            for symbol in list(pending):
                if symbol in frames and api.is_serial_ready(frames[symbol]):
                    frames[symbol]=frames[symbol].copy(deep=True)
                    pending.discard(symbol)
            if time.monotonic()-last_report>=30:
                print('WAIT',start,end,'pending',len(pending),flush=True);last_report=time.monotonic()
        if pending:raise TimeoutError('Subscriptions not ready: '+','.join(sorted(pending)))
        results={}
        for symbol, frame in frames.items():
            records=[];ids=[];valid_full=[]
            for row in frame.iloc[:-1].itertuples():
                try:
                    ns=int(row.datetime);rid=int(row.id)
                    numbers=[float(row.open),float(row.high),float(row.low),float(row.close),float(row.volume),float(row.open_oi),float(row.close_oi)]
                except (ValueError,TypeError,OverflowError):continue
                if rid<0 or ns<=0 or not all(math.isfinite(v) for v in numbers):continue
                op,hi,lo,cl,vol,oi0,oi1=numbers
                if min(op,hi,lo,cl)<=0 or lo>min(op,cl) or hi<max(op,cl) or vol<0:raise ValueError('Invalid OHLCV: '+symbol)
                valid_full.append(ns)
                if begin<=ns<cutoff:
                    ids.append(rid)
                    records.append((symbol,60,ns,ns+STEP,ns,op,hi,lo,cl,int(vol),int(oi0),int(oi1),1,1,'tqsdk-backtest-serial','window-v1'))
            if len({r[2] for r in records})!=len(records):raise ValueError('Duplicate timestamp: '+symbol)
            gaps=[[a,b] for a,b in zip(ids,ids[1:]) if b!=a+1]
            if gaps:raise ValueError('Non-contiguous series IDs: '+symbol)
            # If the server returned a full sequence that starts inside our window,
            # older minutes may have fallen outside the serial's 8000-row limit.
            if len(valid_full)>=7999 and min(valid_full)>begin:raise ValueError('Window exceeds available serial: '+symbol)
            results[symbol]=records
        return results,round(time.monotonic()-started,2)
    finally:
        if api is not None:api.close()


def prepare(path):
    db=store.connect(path)
    db.execute('''CREATE TABLE IF NOT EXISTS backfill_windows(
        start_date TEXT NOT NULL,end_date TEXT NOT NULL,status TEXT NOT NULL,
        details TEXT NOT NULL,PRIMARY KEY(start_date,end_date))''')
    db.execute('''CREATE TABLE IF NOT EXISTS backfill_batches(
        start_date TEXT NOT NULL,end_date TEXT NOT NULL,batch TEXT NOT NULL,status TEXT NOT NULL,
        details TEXT NOT NULL,PRIMARY KEY(start_date,end_date,batch))''')
    db.commit();return db


def persist(db,start,end,batch,selected,results,elapsed,reference):
    overlap=0;conflicts=[]; insert=[];ref=sqlite3.connect('file:'+reference.resolve().as_posix()+'?mode=ro',uri=True) if reference and reference.exists() else None
    try:
        for symbol,rows in results.items():
            for row in rows:
                prev=db.execute('SELECT open,high,low,close,volume,open_oi,close_oi FROM bars WHERE contract=? AND period_seconds=60 AND start_ns=?',(symbol,row[2])).fetchone()
                if prev is None and ref is not None:prev=ref.execute('SELECT open,high,low,close,volume,open_oi,close_oi FROM bars WHERE contract=? AND period_seconds=60 AND start_ns=?',(symbol,row[2])).fetchone()
                if prev is not None:
                    overlap+=1
                    if any(a!=b and (a is None or b is None or abs(a-b)>1e-7) for a,b in zip(row[5:12],prev)):
                        if len(conflicts)<10:conflicts.append([symbol,row[2]])
                        continue
                insert.append(row)
    finally:
        if ref:ref.close()
    details=dict(products=selected,counts={k:len(v) for k,v in results.items()},overlap=overlap,conflicts=conflicts,elapsed=elapsed,
                 warning='Series ID continuity is not a trading-calendar completeness audit; empty contracts require review.')
    if conflicts:raise ValueError('OHLCV conflicts: '+json.dumps(conflicts))
    with db:
        db.executemany('INSERT OR IGNORE INTO bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',insert)
        for symbol in results:
            if results[symbol]:store.aggregate(db,symbol)
        db.execute('INSERT OR REPLACE INTO backfill_batches VALUES (?,?,?,?,?)',(start.isoformat(),end.isoformat(),batch,'ok',json.dumps(details)))
    return details


def main():
    p=argparse.ArgumentParser();p.add_argument('--start',type=dt.date.fromisoformat,required=True);p.add_argument('--end',type=dt.date.fromisoformat,required=True)
    p.add_argument('--db',type=pathlib.Path,default=DEFAULT);p.add_argument('--reference',type=pathlib.Path,default=store.DB)
    p.add_argument('--window-days',type=int,default=14);p.add_argument('--timeout',type=int,default=90);p.add_argument('--limit',type=int,default=0)
    p.add_argument('--batch-size',type=int,default=4)
    a=p.parse_args()
    if a.start>a.end or not 1<=a.window_days<=14 or not 1<=a.batch_size<=12:p.error('Invalid date range, window length or batch size')
    if a.db.resolve()==store.DB.resolve():p.error('Backfill database must be separate from existing research database')
    raw=store.TABLE.read_bytes();table=json.loads(raw);db=prepare(a.db)
    db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',('mapping_snapshot_sha256',hashlib.sha256(raw).hexdigest()));db.commit()
    store.mapping(db,a.start.isoformat(),a.end.isoformat())
    for (symbol,) in db.execute('SELECT DISTINCT contract FROM bars WHERE period_seconds=60').fetchall():
        if not db.execute('SELECT 1 FROM bars WHERE contract=? AND period_seconds=300 LIMIT 1',(symbol,)).fetchone():
            with db:store.aggregate(db,symbol)
    cursor=a.start;done=0
    try:
        while cursor<=a.end and (not a.limit or done<a.limit):
            end=min(a.end,cursor+dt.timedelta(days=a.window_days-1))
            selected=selections(table,cursor,end);names=sorted(selected);failures=0
            # A TqBacktest date is a trading day; its clock may start on the
            # previous evening (Friday night for Monday). Move well beyond the
            # calendar cutoff so all cutoff-day night bars have closed.
            anchor=anchor_for_end(end)
            for offset in range(0,len(names),a.batch_size):
                batch_names=names[offset:offset+a.batch_size];batch=','.join(batch_names);part={k:selected[k] for k in batch_names}
                if db.execute("SELECT 1 FROM backfill_batches WHERE start_date=? AND end_date=? AND batch=? AND status='ok'",(cursor.isoformat(),end.isoformat(),batch)).fetchone():
                    print('SKIP',cursor,end,batch,flush=True);continue
                symbols=sorted({s for v in part.values() for s in v})
                try:
                    rows,elapsed=gather(anchor,cursor,end,symbols,a.timeout)
                    if not any(rows.values()):raise ValueError('No bars observed in the requested window')
                    details=persist(db,cursor,end,batch,part,rows,elapsed,a.reference)
                    print('OK',cursor,end,batch,'rows',sum(details['counts'].values()),'overlap',details['overlap'],'seconds',elapsed,flush=True)
                except Exception as exc:
                    failures+=1;details=dict(error_type=type(exc).__name__,message=str(exc)[:300])
                    with db:db.execute('INSERT OR REPLACE INTO backfill_batches VALUES (?,?,?,?,?)',(cursor.isoformat(),end.isoformat(),batch,'error',json.dumps(details)))
                    print('ERROR',cursor,end,batch,json.dumps(details),flush=True)
            with db:db.execute('INSERT OR REPLACE INTO backfill_windows VALUES (?,?,?,?)',(cursor.isoformat(),end.isoformat(),'partial' if failures else 'ok',json.dumps(dict(failed_batches=failures))))
            cursor=end+dt.timedelta(days=1);done+=1
    finally:db.close()

if __name__=='__main__':main()
