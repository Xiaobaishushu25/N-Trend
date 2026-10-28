"""Build an isolated, auditable futures research archive from Tianqin EDB.

Only the documented free EDB endpoint is used; no credentials or orders.
"""
import argparse
import bisect
import csv
import datetime as dt
import hashlib
import io
import json
import math
import pathlib
import sqlite3
import time
import urllib.parse
import urllib.request
import urllib.error

ROOT = pathlib.Path(__file__).resolve().parents[1]
TABLE = ROOT / 'data' / 'research_market_main_mapping.json'
DB = ROOT / 'data' / 'research_market_history.sqlite'
ZONE = dt.timezone(dt.timedelta(hours=8))
PRODUCTS = {'SHFE':['bu','pb','rb','ss'], 'DCE':['c','eb','eg','jd','l','pp','v'],
            'CZCE':['CF','CJ','FG','MA','SA','SF','SH','SM','SR','UR'], 'GFEX':['si']}
SCHEMA = '''
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mappings(product TEXT NOT NULL, effective_date TEXT NOT NULL, contract TEXT NOT NULL,
  source TEXT NOT NULL, source_sha256 TEXT NOT NULL, PRIMARY KEY(product,effective_date));
CREATE TABLE IF NOT EXISTS bars(contract TEXT NOT NULL, period_seconds INTEGER NOT NULL, start_ns INTEGER NOT NULL,
  end_ns INTEGER NOT NULL, bucket_start_ns INTEGER NOT NULL, open REAL NOT NULL, high REAL NOT NULL,
  low REAL NOT NULL, close REAL NOT NULL, volume INTEGER NOT NULL, open_oi INTEGER, close_oi INTEGER,
  constituent_count INTEGER NOT NULL, complete INTEGER NOT NULL, source TEXT NOT NULL, version TEXT NOT NULL,
  PRIMARY KEY(contract,period_seconds,start_ns));
CREATE TABLE IF NOT EXISTS requests(id INTEGER PRIMARY KEY, contract TEXT NOT NULL, start_time TEXT NOT NULL,
  end_time TEXT NOT NULL, requested_at TEXT NOT NULL, status TEXT NOT NULL, rows_received INTEGER NOT NULL,
  response_sha256 TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS bars_period_time ON bars(period_seconds,start_ns,contract);
'''

def connect(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=60)
    db.executescript(SCHEMA)
    db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", ('schema_version','1'))
    db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", ('aggregation',
        'clock-aligned Asia/Shanghai buckets; split at gaps; start_ns is actual first minute; complete means count equals period; v1'))
    db.commit()
    return db

def mapping(db, start, end):
    raw = TABLE.read_bytes(); digest = hashlib.sha256(raw).hexdigest(); table = json.loads(raw)
    contracts = set()
    for exchange, names in PRODUCTS.items():
        for name in names:
            product = exchange+'.'+name
            entries = table.get(product, [])
            dates = [str(x[0]) for x in entries]
            index = max(0, bisect.bisect_right(dates, start.replace('-',''))-1)
            selected = entries[index:bisect.bisect_right(dates,end.replace('-',''))]
            if not selected:
                print('MISSING_MAPPING',product,flush=True); continue
            for date, contract in selected:
                date = dt.datetime.strptime(str(date),'%Y%m%d').date().isoformat()
                db.execute('INSERT OR REPLACE INTO mappings VALUES (?,?,?,?,?)',
                           (product,date,contract,'tianqin continuous table snapshot',digest))
                contracts.add(contract)
    db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',('mapping_snapshot_sha256',digest))
    db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',('mapping_latest_date',max(x[0] for v in table.values() for x in v)))
    db.commit()
    return sorted(contracts)

def get_data(symbol, start, end):
    params={'symbol':symbol,'period':60,'start_time':start+' 00:00:00','end_time':end+' 23:59:59'}
    url='https://edb.shinnytech.com/md/kline?'+urllib.parse.urlencode(params)
    request=urllib.request.Request(url,headers={'User-Agent':'ntrend-research-archive/1'})
    with urllib.request.urlopen(request,timeout=90) as response:
        payload=response.read()
    content=payload.decode('utf-8-sig')
    if not content.startswith('datetime_nano,'):
        raise ValueError('EDB returned non-CSV response: '+content[:180])
    rows=[]
    for r in csv.DictReader(io.StringIO(content)):
        ns=int(r['datetime_nano']); prices=[float(r[k]) for k in ('open','high','low','close')]
        volume=int(float(r['volume'])); oi=[int(float(r[k])) if r.get(k) else None for k in ('open_oi','close_oi')]
        if not all(math.isfinite(x) for x in prices) or min(prices)<=0 or prices[2]>min(prices[0],prices[3]) or prices[1]<max(prices[0],prices[3]) or volume<0:
            raise ValueError('invalid OHLCV in '+symbol)
        rows.append((symbol,60,ns,ns+60_000_000_000,ns,*prices,volume,*oi,1,1,'tianqin-edb','raw-v1'))
    if len({r[2] for r in rows})!=len(rows): raise ValueError('duplicate timestamps in '+symbol)
    return rows,hashlib.sha256(payload).hexdigest()

def aggregate(db, symbol):
    raw=db.execute('SELECT start_ns,open,high,low,close,volume,open_oi,close_oi FROM bars WHERE contract=? AND period_seconds=60 ORDER BY start_ns',(symbol,)).fetchall()
    minute=60_000_000_000
    # 5/15/60 minute UTC and Shanghai clock boundaries coincide.
    for period in (300,900,3600):
        result=[]; group=[]; key=None; prev=None
        def emit():
            if not group:return
            start=group[0][0]; end=group[-1][0]+minute
            result.append((symbol,period,start,end,key,group[0][1],max(x[2] for x in group),min(x[3] for x in group),group[-1][4],sum(x[5] for x in group),group[0][6],group[-1][7],len(group),int(len(group)==period//60),'derived-from-1m','clock-split-v1'))
        for row in raw:
            bucket=row[0]//(period*1_000_000_000)*(period*1_000_000_000)
            if group and (bucket!=key or row[0]!=prev+minute):emit();group=[]
            key=bucket;group.append(row);prev=row[0]
        emit()
        db.execute('DELETE FROM bars WHERE contract=? AND period_seconds=?',(symbol,period))
        db.executemany('INSERT INTO bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',result)

def collect(db, symbol, start, end, force=False):
    if not force and db.execute("SELECT 1 FROM requests WHERE contract=? AND start_time=? AND end_time=? AND status='ok'",(symbol,start,end)).fetchone():
        print('SKIP',symbol,flush=True); return
    now=dt.datetime.now(ZONE).isoformat(); status='ok';detail=None;rows=[];digest=None
    try:
        rows,digest=get_data(symbol,start,end)
        with db:
            db.executemany('INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',rows)
            aggregate(db,symbol)
            db.execute('INSERT INTO requests(contract,start_time,end_time,requested_at,status,rows_received,response_sha256,detail) VALUES (?,?,?,?,?,?,?,?)',
                       (symbol,start,end,now,status,len(rows),digest,detail))
    except Exception as exc:
        status='error'; detail=(type(exc).__name__+': '+str(exc))[:500]
        db.execute('INSERT INTO requests(contract,start_time,end_time,requested_at,status,rows_received,response_sha256,detail) VALUES (?,?,?,?,?,?,?,?)',
                   (symbol,start,end,now,status,0,None,detail));db.commit()
    print(status.upper(),symbol,len(rows),detail or '',flush=True)

def main():
    p=argparse.ArgumentParser();p.add_argument('--start',required=True);p.add_argument('--end',required=True)
    p.add_argument('--db',type=pathlib.Path,default=DB);p.add_argument('--symbol',action='append');p.add_argument('--force',action='store_true')
    args=p.parse_args();dt.date.fromisoformat(args.start);dt.date.fromisoformat(args.end)
    if args.start>args.end:p.error('start must not exceed end')
    db=connect(args.db)
    try:
        symbols=mapping(db,args.start,args.end)
        if args.symbol: symbols=[x for x in symbols if x in args.symbol]
        print('CONTRACTS',len(symbols),flush=True)
        for symbol in symbols:collect(db,symbol,args.start,args.end,args.force)
        print('SUMMARY',json.dumps(db.execute('SELECT period_seconds,count(*) FROM bars GROUP BY period_seconds ORDER BY 1').fetchall()),flush=True)
    finally:db.close()

if __name__=='__main__':main()
