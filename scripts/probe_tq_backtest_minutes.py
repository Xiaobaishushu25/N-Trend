"""Check historical TqBacktest minute access and compare with an EDB snapshot.

The small probe writes only to an isolated SQLite file; it places no orders.
"""
import argparse
import datetime as dt
import hashlib
import json
import math
import pathlib
import sqlite3
import time

import market_history_store as store

ZONE = dt.timezone(dt.timedelta(hours=8))


def run(symbol, start, end, output, reference, timeout):
    from tqsdk import BacktestFinished, TqApi, TqAuth, TqBacktest, TqSim
    root = store.ROOT
    config = json.loads((root/'data/config.json').read_text(encoding='utf-8'))['data_source']
    secrets = json.loads((root/'data/secrets.json').read_text(encoding='utf-8'))
    begin = int(dt.datetime.combine(start, dt.time(), ZONE).timestamp()*1e9)
    finish = int(dt.datetime.combine(end+dt.timedelta(days=1), dt.time(), ZONE).timestamp()*1e9)
    rows = {}; last_id = None; gaps = []; complete = False; api = None
    started = time.monotonic()
    try:
        api = TqApi(TqSim(), auth=TqAuth(config['tq_account'],config.get('tq_password') or secrets.get('tq_password')),
                    backtest=TqBacktest(start_dt=start,end_dt=end),disable_print=True,web_gui=False)
        frame = api.get_kline_serial(symbol,60,data_length=128)

        def absorb(include_last=False):
            nonlocal last_id
            for r in (frame if include_last else frame.iloc[:-1]).itertuples():
                ts = int(r.datetime); identifier = int(r.id)
                if not begin <= ts < finish or identifier < 0 or ts in rows:
                    continue
                values = [float(r.open),float(r.high),float(r.low),float(r.close),float(r.volume),float(r.open_oi),float(r.close_oi)]
                if not all(math.isfinite(v) for v in values): continue
                op,hi,lo,cl,vol,oi0,oi1=values
                if min(op,hi,lo,cl)<=0 or lo>min(op,cl) or hi<max(op,cl) or vol<0: continue
                if last_id is not None and identifier != last_id+1: gaps.append([last_id,identifier])
                last_id=identifier
                rows[ts]=(symbol,60,ts,ts+60_000_000_000,ts,op,hi,lo,cl,int(vol),int(oi0),int(oi1),1,1,'tqsdk-backtest','probe-v1')

        deadline=time.monotonic()+timeout
        try:
            while time.monotonic()<deadline:
                api.wait_update(deadline=time.time()+2)
                absorb()
        except BacktestFinished:
            # End-of-backtest close update has arrived; keep only bars ending within the requested calendar interval.
            absorb(include_last=True)
            complete=True
    finally:
        if api is not None: api.close()

    db=store.connect(output)
    with db:
        db.executemany('INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',rows.values())
        store.aggregate(db,symbol)
    db.close()
    comparison={'matched':0,'different':0,'missing_reference':0,'examples':[]}
    if reference and reference.exists():
        ref=sqlite3.connect('file:'+reference.resolve().as_posix()+'?mode=ro',uri=True)
        try:
            for ts,r in sorted(rows.items()):
                prior=ref.execute('SELECT open,high,low,close,volume,open_oi,close_oi FROM bars WHERE contract=? AND period_seconds=60 AND start_ns=?',(symbol,ts)).fetchone()
                if prior is None:comparison['missing_reference']+=1;continue
                if all(a==b or (a is not None and b is not None and abs(a-b)<1e-7) for a,b in zip(r[5:12],prior)):
                    comparison['matched']+=1
                else:
                    comparison['different']+=1
                    if len(comparison['examples'])<5:comparison['examples'].append(dt.datetime.fromtimestamp(ts/1e9,ZONE).isoformat())
        finally:ref.close()
    digest=hashlib.sha256(json.dumps([list(rows[k]) for k in sorted(rows)],separators=(',',':')).encode()).hexdigest()
    return dict(symbol=symbol,requested_start=str(start),requested_end=str(end),finished=complete,rows=len(rows),
                first=dt.datetime.fromtimestamp(min(rows)/1e9,ZONE).isoformat() if rows else None,
                last=dt.datetime.fromtimestamp(max(rows)/1e9,ZONE).isoformat() if rows else None,
                id_gaps=gaps,sha256=digest,comparison=comparison,elapsed_seconds=round(time.monotonic()-started,2))


def main():
    p=argparse.ArgumentParser();p.add_argument('--symbol',required=True);p.add_argument('--start',type=dt.date.fromisoformat,required=True)
    p.add_argument('--end',type=dt.date.fromisoformat,required=True);p.add_argument('--output',type=pathlib.Path,required=True)
    p.add_argument('--reference',type=pathlib.Path);p.add_argument('--timeout',type=int,default=90)
    args=p.parse_args()
    if args.start>args.end:p.error('start must not exceed end')
    if args.output.resolve()==store.DB.resolve():p.error('probe output must be separate from the research database')
    result=run(args.symbol,args.start,args.end,args.output,args.reference,args.timeout)
    args.output.with_suffix('.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False),flush=True)

if __name__=='__main__':main()
