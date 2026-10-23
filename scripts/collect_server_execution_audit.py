"""Collect current server execution evidence; never mutate the running server.

Only a disposable database backup is scrubbed. Binary SSH output is written
directly to disk, avoiding shell text conversion and accidental secret output.
"""
import argparse
import hashlib
import json
import pathlib
import subprocess
import tarfile


REMOTE = r'''
import datetime as dt, hashlib, io, json, pathlib, sqlite3, subprocess, sys, tarfile, tempfile, urllib.request
root=pathlib.Path('/home/ubuntu/pjf/N-Trend')
now=lambda:dt.datetime.now().astimezone().isoformat()
manifest={'started_at':now(),'host':'wlyserver','root':str(root),'files':{},'errors':{},'production_writes':False}
def api(path):
    with urllib.request.urlopen('http://127.0.0.1:8081/api/v1'+path,timeout=20) as response:return json.load(response)
def add_bytes(tar,name,body):
    info=tarfile.TarInfo(name);info.size=len(body);tar.addfile(info,io.BytesIO(body))
    manifest['files'][name]={'bytes':len(body),'sha256':hashlib.sha256(body).hexdigest(),'captured_at':now()}
with tempfile.TemporaryDirectory(prefix='ntrend-readonly-audit-') as tmp:
    backup=pathlib.Path(tmp)/'source.sqlite'
    source=sqlite3.connect((root/'data/ntrend.db').as_uri()+'?mode=ro',uri=True,timeout=20)
    dest=sqlite3.connect(backup);source.backup(dest);source.close()
    manifest['database_captured_at']=now()
    manifest['raw_5m']=dest.execute("select count(*),min(ts),max(ts),count(distinct symbol) from klines where timeframe='5m' and source='raw'").fetchone()
    tables={r[0] for r in dest.execute("select name from sqlite_master where type='table'")}
    manifest['removed_private_tables_from_copy']=[]
    for table in ['settings','devices','server_settings','idempotency_requests']:
        if table in tables:
            dest.execute('DELETE FROM "'+table+'"');manifest['removed_private_tables_from_copy'].append(table)
    dest.commit();dest.execute('VACUUM')
    assert dest.execute('pragma integrity_check').fetchone()[0]=='ok';dest.close()
    with tarfile.open(fileobj=sys.stdout.buffer,mode='w|gz') as tar:
        add_bytes(tar,'source.sqlite',backup.read_bytes())
        for path in ['/trader/history','/trader/snapshot','/trader/conditions','/trader/positions','/trader/orders?page=1&page_size=10000']:
            name={'/trader/history':'trade-history.json','/trader/snapshot':'execution-snapshot.json','/trader/conditions':'conditions-api.json','/trader/positions':'positions-api.json'}.get(path,'orders-api.json')
            try:add_bytes(tar,name,json.dumps(api(path),ensure_ascii=False).encode())
            except Exception as e:manifest['errors'][name]=str(e)
        for name in ['trader_execution.json','trader_config.json','strategy_comparison.jsonl','overnight_attribution.jsonl','overnight_tracking.jsonl','trading_calendar.json']:
            p=root/'data'/name
            if p.exists():add_bytes(tar,name,p.read_bytes())
        for p in sorted((root/'data').glob('ntrend.log.*')):
            add_bytes(tar,'logs/'+p.name,p.read_bytes())
        for rel in ['crates/n-core/src/trader/execution.rs','crates/n-core/src/trader/policy.rs','crates/n-core/src/trader/trailing.rs','crates/n-headless/src/runtime.rs','python-bridge/ctp_trader.py']:
            p=root/rel
            if p.exists():add_bytes(tar,'deployed-source/'+rel,p.read_bytes())
        manifest['server_commit']=subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD'],text=True).strip()
        manifest['finished_at']=now()
        add_bytes(tar,'collection-manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2).encode())
'''


def run(host,output):
    output=pathlib.Path(output).resolve()
    if output.exists():raise FileExistsError('Choose a new archive directory')
    output.mkdir(parents=True)
    archive=output/'server-evidence.tar.gz'
    with archive.open('xb') as out:
        r=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=15',host,'python3 -'],
                         input=REMOTE.encode(),stdout=out,stderr=subprocess.PIPE,timeout=240)
    if r.returncode:raise RuntimeError(r.stderr.decode(errors='replace'))
    with tarfile.open(archive,'r:gz') as tar:
        for member in tar.getmembers():
            target=(output/member.name).resolve()
            if not target.is_relative_to(output) or not member.isfile():raise ValueError('Unsafe archive member')
        tar.extractall(output,filter='data')
    manifest=json.loads((output/'collection-manifest.json').read_text(encoding='utf-8'))
    for name,info in manifest['files'].items():
        if hashlib.sha256((output/name).read_bytes()).hexdigest()!=info['sha256']:raise ValueError('Checksum mismatch: '+name)
    state=json.loads((output/'trader_execution.json').read_text(encoding='utf-8'))
    print(json.dumps(dict(archive=str(output),raw_5m=manifest['raw_5m'],started_at=manifest['started_at'],
                         finished_at=manifest['finished_at'],errors=manifest['errors'],
                         counts={k:len(state.get(k,[])) for k in ['history','orders','conditions','positions','seen_trades']},
                         archive_bytes=archive.stat().st_size,checksums_verified=len(manifest['files'])),ensure_ascii=True,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--host',default='wlyserver');p.add_argument('--output',required=True)
    a=p.parse_args();run(a.host,a.output)
