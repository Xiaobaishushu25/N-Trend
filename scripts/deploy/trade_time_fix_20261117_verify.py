"""Read-only verification after the approved server cutover."""
import hashlib
import datetime as dt
import json
import pathlib
import sqlite3
import subprocess
import sys
import urllib.request

stage = pathlib.Path(sys.argv[1]).resolve()
assert stage.parent == pathlib.Path('/home/ubuntu/pjf/ntrend-deployments').resolve()
live = pathlib.Path('/home/ubuntu/pjf/N-Trend')
manifest = json.loads((stage/'backup/manifest.json').read_text())
status = json.loads((stage/'status.json').read_text())
assert status['phase'] == 'deployed'
pid = subprocess.check_output(['systemctl','--user','show','ntrend-server.service','--property=MainPID','--value'], text=True).strip()
assert int(pid) > 0
running_hash = hashlib.sha256((pathlib.Path('/proc')/pid/'exe').read_bytes()).hexdigest()
assert running_hash == status['binary_sha256']
c = sqlite3.connect((live/'data/ntrend.db').as_uri()+'?mode=ro', uri=True)
registry = json.loads(c.execute('select value from settings where key=?', ('trader_instance_registry_v1',)).fetchone()[0]); c.close()
result = {'verified_at':dt.datetime.now().astimezone().isoformat(),'binary_sha256':running_hash,'pid':pid,'instances':{},'production_verification_read_only':True}
baseline = json.loads((stage/'before-brokers.json').read_text())['instances']
def managed_positions(state):
    book = {}
    for p in state['positions'].values():
        if not p['is_closed'] and p['volume'] > 0:
            key = (p['instrument_id'].upper(), 'buy' if p['direction']=='up' else 'sell')
            book[key] = book.get(key,0) + p['volume']
    return book
for ident in manifest['states']:
    before=json.loads((stage/'backup/data'/ident/'trader_execution.json').read_text())
    after=json.loads((live/'data/trader_instances'/ident/'trader_execution.json').read_text())
    assert managed_positions(before)==managed_positions(after), ('strategy holdings changed',ident)
    old={h['id']:h for h in before['history']};new={h['id']:h for h in after['history']}
    assert old.keys()==new.keys(), ('trade count changed',ident)
    for key,a in old.items():
        for field in ['volume','fill_price','exit_price','risk','r_multiple','realized_pnl_pts','realized_pnl_amount','exit_reason']:
            assert a[field]==new[key][field], (ident,key,field)
    assert before['orders'].keys()==after['orders'].keys(), ('unexpected strategy order',ident)
    aid=registry['instances'][ident]['account_id'];port=registry['accounts'][aid].get('port') or 8766
    broker={};error=None;ready=None
    try:
        with urllib.request.urlopen('http://127.0.0.1:'+str(port)+'/snapshot',timeout=8) as f:snapshot=json.load(f)
        ready=snapshot.get('health',{}).get('ready')
        for p in snapshot.get('broker_positions',[]):
            if p.get('position',0)>0:
                key=(p['instrument_id'].upper(),p['direction']);broker[key]=broker.get(key,0)+p['position']
    except Exception as e:error=str(e)
    expected=managed_positions(after)
    known = {(p['instrument_id'].upper(),p['direction']):p['position'] for p in baseline.get(ident,{}).get('broker',[])}
    result['instances'][ident]={'history_rows':len(new),'holdings_match_broker':(expected==broker and error is None) if ready else None,
        'holdings_match_last_known_broker':expected==known,
        'broker_ready_before_deployment':baseline.get(ident,{}).get('ready'),
        'managed':{':'.join(k):v for k,v in expected.items()},'broker':{':'.join(k):v for k,v in broker.items()},
        'broker_ready':ready,'broker_error':error,'time_version':after.get('time_normalization_version'),
        'unresolved_times':sum(x.get('timestamp') is None for x in after.get('trade_times',{}).values())}
    for cid in ['7bc7c497-a451-46d6-8997-2e4532aac431','568849dc-4a2c-49f0-9704-7abd6bb7db5c']:
        if cid in after['positions']:
            p=after['positions'][cid];s=after['overnight_snapshots'][cid]
            assert p['open_ts']=='2026-11-16 21:45:59'
            assert s['next_open_price']==9000 and s['gap_pts']==76 and s['open_vs_checkpoint_pts']==84
            assert s['execution_gap'] and s['overnight_delta_pts'] is None
            assert s['first_observed_price']==9040
            result['instances'][ident]['pp_case']={'open_ts':p['open_ts'],'exit_price':p['exit_price'],
                'true_open':s['next_open_price'],'true_gap_pts':s['gap_pts'],'recovery_observation':s['first_observed_price']}
try:
    request=urllib.request.Request('http://127.0.0.1:8081/api/v2/trader/snapshot',headers={'x-ntrend-version':'3.0.4','x-ntrend-api-version':'v2'})
    with urllib.request.urlopen(request,timeout=8) as f:result['api_status']=f.status
except Exception as e:result['api_error']=str(e)
ready = [v for v in result['instances'].values() if v['broker_ready']]
result['ready_broker_count'] = len(ready)
result['all_ready_brokers_match'] = bool(ready) and all(v['holdings_match_broker'] for v in ready)
result['all_holdings_match_baseline']=all(v['holdings_match_last_known_broker'] for v in result['instances'].values())
result['not_ready_instances']=[k for k,v in result['instances'].items() if not v['broker_ready']]
(stage/'post-deployment-verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
print(json.dumps(result,ensure_ascii=True))
