"""Standalone stdlib-only replay of frozen, fee-priced individual decisions."""
import sys
sys.dont_write_bytecode=True
import collections
import copy
import hashlib
import json
from pathlib import Path
import controller

ROOT=Path(__file__).resolve().parent
UNKNOWN={'rollover','pending','unfinished','ambiguous','missing_data'}
IMMEDIATE={'filtered','filtered_setup','filtered_geometry','filtered_entry_economics','filtered_A_net_body','filtered_net_rr_plan'}
def read(p):return json.loads(p.read_text(encoding='utf-8'))
def ev(r):return r['result']['evaluation']
def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()

def admit(rows,ends,policy,meta):
    accepted=[];active=None;until='';stops=collections.Counter()
    for r in sorted(rows,key=lambda r:(r['warning_ts'],r['event_key'])):
        ts=r['warning_ts']
        if active is not None and until<ts:
            if ev(active)['status']=='closed' and ev(active)['exit_reason']=='stop':stops[meta[active['event_key']]['cluster']]+=1
            active=None
        e=ev(r)
        if e['status'] in IMMEDIATE:
            assert e['entry_ts'] is None and e['exit_ts'] is None;continue
        flat=controller.ambiguity(r) if policy=='two_paths_flat' else None
        end=flat['end'] if flat else ends[r['execution_contract']] if e['status'] in UNKNOWN else e['exit_ts']
        assert end is not None and end>=ts
        if active is not None or stops[meta[r['event_key']]['cluster']]>=2:continue
        assert until<ts;accepted.append(r);active=r;until=end
    return accepted

def metric(rows,months):
    closed=[r for r in rows if ev(r)['status']=='closed'];values=[ev(r)['net_amount']*300/(r['planned_risk']*r['multiplier']) for r in closed]
    settles=sorted((ev(r)['exit_ts'],r['warning_ts'],r['event_key'],v) for r,v in zip(closed,values));cash=peak=dd=0.
    for _,_,_,v in settles:cash+=v;peak=max(peak,cash);dd=max(dd,peak-cash)
    bins={m:sum(v for r,v in zip(closed,values) if r['warning_ts'][:7]==m) for m in months}
    return dict(n=len(closed),net=sum(ev(r)['net_amount'] for r in closed),unknown=sum(ev(r)['status'] in UNKNOWN for r in rows),
        normalized_net=sum(values),normalized_realized_dd=dd,normalized_ex_top3=sum(values)-sum(sorted([max(0,v) for v in values],reverse=True)[:3]),
        normalized_ex_best_month=sum(values)-max([0]+list(bins.values())),month_cash={m:sum(ev(r)['net_amount'] for r in closed if r['warning_ts'][:7]==m) for m in months})

def same(actual,expected,path=''):
    for k,v in actual.items():
        if isinstance(v,dict):same(v,expected[k],path+'/'+k)
        elif isinstance(v,(int,float)) and not isinstance(v,bool):assert abs(v-expected[k])<1e-7,(path,k,v,expected[k])
        else:assert v==expected[k],(path,k,v,expected[k])

def run():
    manifest=read(ROOT/'manifest.json')
    for q,h in manifest['files'].items():assert digest(ROOT/q)==h,q
    spec=read(ROOT/'portfolio-data.json');data=collections.defaultdict(list)
    for line in (ROOT/'individual-decisions.jsonl').open(encoding='utf-8'):
        r=json.loads(line);data[r['model'],r['suffix']].append(r['row'])
    verified=0;main_primary={}
    for coverage in ['main','combined']:
        expected=read(ROOT/('expected-'+coverage+'.json'))
        for case in spec['cases']:
            policy,suffix=case.split('/',1)
            for model,source in [('E20S1',None),('E20_PLUS_RECLAIM_S1','S0_RECLAIM_S1'),('E20_PLUS_RECLAIM_R2','S0_RECLAIM_R2')]:
                meta=dict(spec['metadata']);stream=[]
                for prefix,component in ([('0','E20S1')] if source is None else [('0','E20S1'),('1',source)]):
                    for r0 in data[component,suffix]:
                        if coverage=='main' and r0['period']=='july':continue
                        r=copy.deepcopy(r0);key=prefix+'|'+r['event_key'];meta[key]=meta[r['event_key']];r['event_key']=key;stream.append(r)
                accepted=admit(stream,spec['ends'],policy,meta);q=metric(accepted,spec['months']);same(q,expected[case+'/'+model]);verified+=1
                for block in spec['blocks']:
                    same(metric([r for r in accepted if block['start']<=r['warning_ts']<block['end']],spec['months']),expected[case+'/'+model]['blocks'][block['label']]);verified+=1
                if coverage=='main' and case==spec['primary']:main_primary[model]={k:q[k] for k in ['n','net','unknown']}
    print(json.dumps(dict(verified_records_and_blocks=verified,all_snapshot_hashes_verified=len(manifest['files']),main_primary=main_primary),ensure_ascii=False))

if __name__=='__main__':run()
