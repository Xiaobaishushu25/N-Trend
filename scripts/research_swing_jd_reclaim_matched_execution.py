"""Fixed-context, translated-plan native controls; descriptive, not a strategy."""
import sys
sys.dont_write_bytecode=True
import collections
import copy
import datetime as dt
import json
import random
import statistics as st
import subprocess
import unittest
import audit_swing_jd_reclaim_forward as a

r=a.r
R=a.R
x=a.routing.x
v=a.routing.v
RUN=r.b.ROOT/'research-runs/swing-jd-reclaim-matched-execution-20261027-v2'
N=199
SCORE={'early':('2026-01-01','2026-07-10 15:00:00'),
       'recent':('2026-08-10','2026-10-23 11:05:00'),
       'validation2025':(v.START,v.END)}


def write(n,z):
    (RUN/n).parent.mkdir(parents=True,exist_ok=True)
    r.b.z.s.m.old.write(RUN/n,z)
def sign(z):return 1 if z>0 else -1 if z<0 else 0


def translate(source, host, key):
    s=copy.deepcopy(source);e=s['event'];delta=host['close']-e['entry']
    for k in ['entry','stop','target','trigger_level','s0_price','s1_price','s2_price','warning_extreme']:
        if e.get(k) is not None:e[k]+=delta
    shift=dt.datetime.fromisoformat(host['ts'])-dt.datetime.fromisoformat(e['warning_ts'])
    for k in ['s0_ts','s1_ts','s2_ts','warning_ts']:
        e[k]=(dt.datetime.fromisoformat(e[k])+shift).isoformat(sep=' ')
    # Inherited structure and A-body qualification are conditioning variables,
    # not a claim that the host has its own ABC structure.
    s.update(event_key=key,warning_candle=host['bar'],prior=[])
    assert abs(e['entry']-host['close'])<1e-8
    for k in ['risk','rr','direction']:assert e[k]==source['event'][k]
    assert abs((e['target']-e['entry'])-(source['event']['target']-source['event']['entry']))<1e-8
    return s


def prefix_lookup(contracts,dates,segments):
    maps={c:{q['datetime']:q for q in z['minutes']} for c,z in contracts.items()}
    labels=[f'{d} {m//60:02}:{m%60:02}:00' for d in dates for m in a.paths.SLOTS]
    bars=[];codes=[]
    for label in labels:
        code=next((s['underlying'] for s in segments if s['first']<=label<=s['last']),None)
        codes.append(code);end=dt.datetime.fromisoformat(label)
        mins=[maps.get(code,{}).get((end-dt.timedelta(minutes=j)).isoformat(sep=' ')) for j in range(14,-1,-1)]
        if not all(q is not None and q['volume']>0 for q in mins):bars.append(None);continue
        bars.append(dict(datetime=label,open=mins[0]['open'],close=mins[-1]['close'],high=max(q['high'] for q in mins),
            low=min(q['low'] for q in mins),volume=sum(q['volume'] for q in mins),hold=mins[-1]['hold']))
    lookup={}
    for i in range(20,len(labels)):
        if not all(bars[i-20:i+1]) or not codes[i] or any(c!=codes[i] for c in codes[i-20:i+1]):continue
        atr=st.mean(max(bars[j]['high']-bars[j]['low'],abs(bars[j]['high']-bars[j-1]['close']),abs(bars[j]['low']-bars[j-1]['close'])) for j in range(i-13,i+1))
        if atr>0:lookup[i]=dict(index=i,ts=labels[i],code=codes[i],atr=atr,
            momentum_sign=sign(bars[i]['close']-bars[i-8]['close']),close=bars[i]['close'],bar=bars[i])
    return maps,labels,lookup


def path_outcomes(maps,dates,segments,host,source):
    groups,status=a.forward(maps,dates,segments,host['code'],host['ts'],32)
    out={}
    for n in a.HORIZONS:
        out[str(n)]=dict(status='ok' if len(groups)>=n else status)
        if len(groups)>=n:
            raw=[q for group in groups[:n] for q in group]
            out[str(n)].update(a.measures(raw,host['close'],source['event']['risk'],r.f.direction(source['event'])))
    return out


def prepare():
    RUN.mkdir(exist_ok=False)
    exe=R(x.RUN/'binary.json');assert r.sha(exe['path'])==exe['sha256']
    templates=[R(x.RUN/'protocol.json')['templates']['BASE'],R(a.routing.COMBO/'protocol.json')['template']]
    before={p:r.sha(r.b.ROOT/p) for p in R(x.RUN/'protocol.json')['production_before']}
    write('protocol.json',dict(created_at=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(),
        hypothesis='Incremental information in S0 reclaim clock, conditional on original direction, risk geometry and original A qualification.',
        universe='All42 frozen reclaim decisions, including filtered, occupied and unknown; no new rule/side filter.',
        match='Same actual contract, period, month,15m clock, sign of trailing8bar close momentum; ATR14 ratio host/signal [0.5,2]. Every last21 bars verified positive-volume actual minutes and same member. Need>=3 hosts; no relaxation.',
        exclusions='Host within32 trading-bar indices of ANY original reclaim decision excluded. Hosts selected on prefix only, NEVER future outcome or complete future. Calendar can contain later dates: retrospective control, not causal deployable training.',
        transplant='Preserve source point risk, planned2R, direction, S0/entry offset, original setup/A-body qualification. Translate price levels by host-close minus source entry, shift anchor times equally. Host actual candle, source prior empty. This isolates timing conditional on source plan, NOT an ordinary ABC control and NOT proof of causal advantage.',
        native='Exact frozen binary and BASE/F2S8 templates. 2 entry modes x1..4 tick adverse fills; frozen fees, actual risk/chase/netRR, weekend/holiday/maxhold and missing/ambiguity handling. No portfolio addition or occupancy claims.',
        unknown='Closed native orders use fee-priced net/planned cash risk; definitely filtered/unentered are known zero cash decisions, not zero trades. Unfinished/ambiguous/missing remain unknown, never zero. Compare only paired known outcomes per case, disclose exclusions.',
        estimation='Each signal has equal weight; average its matched hosts. Also199 seeded unique-host draws across matched signals; report paired-known difference and unknown counts. Descriptive spread, NOT p-values or independent market samples. No outcome-based host removal.',
        path='4/8/16/32 planned-risk paths; paired complete windows only, censor counts disclosed.',
        primary='BASE/corrected_touch/slip1, mean paired known per-decision netR difference. F2S8, all stress, per-fill, months, directions, clusters and best-pair removal diagnostic.',
        templates=templates,binary=exe,seed=20261027,replications=N,script_sha256=r.sha(__file__),
        production_before=before,qualified_for_trading=False))


def process(per,contracts,inp,paths,dates,segments,pool,dataset):
    p=R(RUN/'protocol.json');maps,labels,lookup=prefix_lookup(contracts,dates,segments)
    index={t:i for i,t in enumerate(labels)};signal_indices=[index[s['event']['warning_ts']] for s in pool]
    matches=[];native_pool=list(pool);known_paths={}
    for number,s in enumerate(pool):
        key=s['event_key'];e=s['event'];at=index[e['warning_ts']];real=lookup.get(at)
        rec=dict(period=per,year=e['warning_ts'][:4],month=e['warning_ts'][:7],direction=e['direction'],
            key=key,cluster=dataset['meta'][key]['cluster'],ts=e['warning_ts'],hosts=[],status='signal_prefix_unverified')
        if real:
            assert real['code']==dataset['meta'][key]['contract']
            assert all(abs(real['bar'][k]-s['warning_candle'][k])<1e-7 for k in ['open','high','low','close'])
            rec['prefix']={k:z for k,z in real.items() if k!='bar'}
            known_paths[key]=path_outcomes(maps,dates,segments,real,s)
            for j,h in lookup.items():
                if not SCORE[per][0]<=h['ts']<=SCORE[per][1]:continue
                if h['code']!=real['code'] or h['ts'][:7]!=real['ts'][:7] or h['ts'][11:]!=real['ts'][11:]:continue
                if h['momentum_sign']!=real['momentum_sign'] or not .5<=h['atr']/real['atr']<=2:continue
                if any(abs(j-k)<=32 for k in signal_indices):continue
                hostkey=f'MATCH|{per}|{number}|{h["ts"]}'
                rec['hosts'].append(dict(key=hostkey,host_id=per+'|'+h['ts'],prefix={k:z for k,z in h.items() if k!='bar'}))
            rec['status']='matched' if len(rec['hosts'])>=3 else 'insufficient_hosts'
            if rec['status']=='matched':
                for h in rec['hosts']:
                    host=lookup[h['prefix']['index']]
                    native_pool.append(translate(s,host,h['key']))
                    known_paths[h['key']]=path_outcomes(maps,dates,segments,host,s)
        matches.append(rec)
    write(per+'/matches.json',matches);write(per+'/paths.json',known_paths)
    write(per+'/'+per+'-pool.json',native_pool)
    protocol=dict(templates=p['templates'],modes=['corrected_touch','next_minute_open'],periods={per:paths})
    write(per+'/execution.protocol.json',protocol)
    payload=None
    if paths['contracts'].startswith('memory:'):
        payload=json.dumps(dict(input=inp,contracts=contracts),ensure_ascii=False,separators=(',',':')).encode()
    assert r.sha(p['binary']['path'])==p['binary']['sha256']
    subprocess.run([p['binary']['path'],str(RUN/per/'execution.protocol.json'),per,'replay',str(RUN/per/'executions')],input=payload,check=True)
    cal=r.b.z.s.TradingDates({});cal.dates=dates;fees=R(r.SNAP/'fees.json');evals=[];checks=0
    source_keys={s['event_key'] for s in pool}
    prior={(m,sf,q['event_key']):q for (m,sf),rs in dataset['data'].items() for q in rs if m in ['BASE','F2S8']}
    for line in (RUN/per/'executions/evaluations.jsonl').open(encoding='utf-8'):
        row=r.b.z.s.reprice(json.loads(line),fees,cal);m,sf=row['scenario'].split('/',1)
        model='F2S8' if m=='REFAIL2' else m
        if row['event_key'] in source_keys:
            old=prior[model,sf,row['event_key']]
            for k in ['result','planned_risk','planning_entry','entry','stop','target','economic_pass','body_net_pass']:
                assert row[k]==old[k],(per,row['event_key'],model,sf,k)
            checks+=1
        evals.append(dict(model=model,suffix=sf,row=row))
    write(per+'/fee-priced-decisions.json',evals)
    write(per+'/verification.json',dict(exact_original_native_rows=checks,original_decisions=len(pool),
        matched=sum(z['status']=='matched' for z in matches),controls=len(native_pool)-len(pool)))
    print(per,dict(collections.Counter(q['status'] for q in matches)),'controls',len(native_pool)-len(pool),'exact baseline rows',checks,flush=True)


def run():
    prepare();_,datasets,qa=a.routing.load();write('baseline-load-checks.json',qa);sources={}
    for per in ['early','recent']:
        paths,recognition,_,dates=r.f.t.period_paths(per)
        poolfile=r.c.RUN/per/'S0_RECLAIM_R2'/(per+'-pool.json')
        process(per,R(paths['contracts']),R(paths['input']),paths,dates,R(paths['segments'])['JD0'],R(poolfile),datasets['Y2026'])
        for f in [paths['contracts'],paths['input'],paths['segments'],poolfile]:sources[str(f)]=r.sha(f)
    v.write=lambda n,z:write('2025-source/'+n,z)
    contracts=v.collect()
    assert R(RUN/'2025-source/actual-contract-coverage.json')['contracts']==R(v.RUN/'actual-contract-coverage.json')['contracts']
    inp,segments=v.stitch(contracts)
    assert v.digest(inp)==R(v.RUN/'generation.protocol.json')['hashes']['validation2025']['input']
    paths=R(v.RUN/'generation.protocol.json')['periods']['validation2025']
    poolfile=v.RUN/'reclaim/validation2025-pool.json'
    process('validation2025',contracts,inp,paths,R(v.RUN/'trading-dates.json'),segments,R(poolfile),datasets['Y2025'])
    for f in [poolfile,v.RUN/'protocol.json',v.RUN/'metadata.json',v.RUN/'generation.protocol.json']:sources[str(f)]=r.sha(f)
    write('sources.json',sources)
    before=R(RUN/'protocol.json')['production_before']
    assert all(r.sha(r.b.ROOT/f)==h for f,h in before.items())
    write('execution-complete.json',dict(production_unchanged=True,raw2025_hash_exact=True,
        script_unchanged=R(RUN/'protocol.json')['script_sha256']==r.sha(__file__),qualified_for_trading=False))


class Tests(unittest.TestCase):
    def test_translation_both_directions(self):
        for direction in ['up','down']:
            d=1 if direction=='up' else -1
            source=dict(event=dict(entry=100,stop=100-d*10,target=100+d*20,risk=10,rr=2,direction=direction,
                s0_price=100-d*2,s1_price=100+d*15,s2_price=100-d*5,trigger_level=100,warning_extreme=100+d,
                s0_ts='2026-01-01 09:15:00',s1_ts='2026-01-01 09:30:00',s2_ts='2026-01-01 09:45:00',warning_ts='2026-01-01 10:00:00'),prior=[])
            host=dict(close=300,ts='2026-01-08 10:00:00',bar={})
            e=translate(source,host,'host')['event']
            self.assertEqual(e['stop'],300-d*10)
            self.assertEqual(e['s0_price'],300-d*2)
            self.assertEqual(e['s0_ts'],'2026-01-08 09:15:00')
            self.assertEqual(source['event']['entry'],100)


if __name__=='__main__':
    if '--test' in sys.argv:unittest.main(argv=[__file__])
    else:run()
