"""Conditional plan-preserving timing null, following common-adapter results.

Not a causal random trading strategy: the observed warning plan/policy is carried
to a matched clock, possibly earlier in its month. No future trade outcome selects
the plan, clock, thresholds, or treatment. All interpretations remain conditional.
"""
import argparse
import collections
import copy
import datetime as dt
import json
import math
import random
import statistics as st
import subprocess
import sys
import research_swing_incremental_edge as r

RUN=r.RUN/'plan-preserving-null'
N=199
READ=r.READ
EV=r.EV

def write(name,obj):r.s.m.old.write(RUN/name,obj)

def prepare():
    RUN.mkdir(exist_ok=False)
    p=READ(r.RUN/'protocol.json')
    t=copy.deepcopy(READ(r.s.ARCHIVE/'protocol.json')['templates'][0])
    t.update(name='PLAN',local=False)
    write('protocol.json',dict(id=RUN.name,created_at=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(),
        script_sha256=r.s.m.old.sha(__file__),parent_protocol_sha256=r.s.m.old.sha(r.RUN/'protocol.json'),native=p['native'],
        reason='Follow-up after common-ATR results: common ATR removes original structural stop/target and hourly execution. Test original plan/policy preserved at conditional random clocks; not a revision of first-stage outcomes or thresholds.',
        templates=[t],modes=['corrected_touch','next_minute_open'],repetitions=N,seed_base=2026102650,
        plan_selection='Whitelist warning/plan fields only from frozen budget_body PRIMARY rows; require original body/economic passes and >=10tick risk/friction<=.2, shared-clock availability. No trigger/status/fill/exit/MFE/net used. Native plan netRR guard still applied.',
        match='Use parent shared clock group: symbol/month/direction/HH:MM/momentum-sign/coarse ATR-risk-width bucket. Sample without replacement per stratum; preserve each source planned risk and target distance exactly and entry buffer relative to warning extreme.',
        execution='Copy original warning kind/hourly adaptive execution instruction and A-cluster label as conditioned plan attributes. Keep original5bucket no-follow,60hold,weekend/holiday/gaps/chase, actual-cost guard. Recompute plan costs at new prices. Native original-clock controls must exactly match frozen individual results; same-sector/B5two-stop controller applied to each replicate.',
        limitation='Monthly counts, plan widths and execution-style labels condition on observed ABC plans, including plans from later in month. Not deployable/causal OOS or calibrated significance. Does not preserve true ABC structure at random clock; tests conditional observed plan+timing package. Coarse risk matching cannot remove every regime difference.',
        risk_normalization=p['risk'],test_start=r.FOLDS[0][0],production_before=p['production_before'],production_changed=False))

def source_plans(period,clock):
    pools={z['event_key']:z for z in READ(r.s.NATIVE/(period+'-pool.json')) if z['event']['symbol'] in r.SYMS}
    fields=['event_key','symbol','direction','warning_ts','entry','stop','target','planned_risk','tick_size','new_friction','economic_pass','body_net_pass','execution_contract']
    plans=[]
    for line in (r.s.NATIVE/period/'evaluations.jsonl').open(encoding='utf-8'):
        raw=json.loads(line)
        if raw['scenario']!='P2_economics_budget_body/'+r.PRIMARY or raw['symbol'] not in r.SYMS:continue
        z={k:raw[k] for k in fields}
        key='|'.join(['CLOCK',z['symbol'],z['direction'],z['warning_ts']])
        if key not in clock:continue
        if not(z['economic_pass'] and z['body_net_pass'] and z['planned_risk']/z['tick_size']+1e-6>=10 and z['new_friction']<=.2+1e-9):continue
        plans.append(dict(plan=z,source=pools[z['event_key']],clock_key=key))
    return plans

def generate():
    p=READ(RUN/'protocol.json');meta=READ(r.RUN/'clock-metadata.json')
    for z in meta.values():z['stratum']=r.group(z)
    allmeta={};receipts={}
    for period in ['early','recent']:
        dest=RUN/period;dest.mkdir(exist_ok=False)
        clock={z['event_key']:z for z in READ(r.RUN/period/(period+'-pool.json'))}
        plans=source_plans(period,clock)
        populations=collections.defaultdict(list);groups=collections.defaultdict(list)
        for key in clock:populations[meta[key]['stratum']].append(key)
        for z in plans:groups[meta[z['clock_key']]['stratum']].append(z)
        for xs in populations.values():xs.sort()
        out=[]
        for rep in range(-1,N):
            rng=random.Random(2026102650+rep);pairs=[]
            for group,ps in sorted(groups.items()):
                ps=sorted(ps,key=lambda z:z['plan']['event_key'])
                keys=[z['clock_key'] for z in ps] if rep==-1 else rng.sample(populations[group],len(ps))
                assert len(set(keys))==len(keys)
                pairs.extend(zip(ps,keys))
            for src,newkey in pairs:
                old=src['plan'];snap=copy.deepcopy(src['source']);e=snap['event'];cl=clock[newkey];cm=meta[newkey]
                sign=1 if old['direction']=='up' else -1
                old_extreme=src['source']['warning_candle']['high' if sign>0 else 'low']
                buffer=sign*(old['entry']-old_extreme)
                extreme=cl['warning_candle']['high' if sign>0 else 'low']
                entry=extreme+sign*buffer;shift=entry-old['entry'];ts=cm['warning_ts']
                e.update(warning_ts=ts,s2_ts=ts,entry=entry,stop=old['stop']+shift,target=old['target']+shift,
                         risk=old['planned_risk'],rr=(old['target']-old['entry'])*sign/old['planned_risk'],
                         warning_extreme=extreme,trigger_level=entry,evaluation=None,outcome=None,entry_ts=None,entry_price=None)
                # Preserve an opaque source cluster ID; its timestamps are labels only.
                # Updating as_of avoids presenting copied context as fresh market features.
                if e.get('market_context'):e['market_context']['as_of_ts']=ts
                key=f'P{rep:03d}|'+old['event_key']+'|AT|'+ts
                snap.update(event_key=key,event=e,warning_candle=cl['warning_candle'],prior=[])
                out.append(snap)
                allmeta[key]=dict(cm,replicate=rep,source_key=old['event_key'],source_warning_ts=old['warning_ts'],
                                  planned_risk=old['planned_risk'],source_clock=src['clock_key'],assigned_clock=newkey,
                                  copied_execution_instruction=True)
                if rep==-1:
                    assert abs(shift)<1e-8
                assert abs((e['target']-e['entry'])-(old['target']-old['entry']))<1e-7
                assert abs((e['entry']-e['stop'])*sign-old['planned_risk'])<1e-7
        r.s.m.old.write(dest/(period+'-pool.json'),out)
        native=READ(r.RUN/period/'execution.protocol.json')
        native['templates']=p['templates']
        r.s.m.old.write(dest/'execution.protocol.json',native)
        receipts[period]=dict(plan_candidates=len(plans),replicates_with_control=N+1,native_pool_size=len(out),
                              per_source_no_outcome_selection=True,all_geometry_translations_exact=True)
        print(period,'conditioned plans',len(plans),'all replica opportunities',len(out),flush=True)
    write('metadata.json',allmeta);write('generation-verification.json',receipts)

def replay():
    p=READ(RUN/'protocol.json');exe=p['native']['path']
    assert r.s.m.old.sha(exe)==p['native']['sha256']
    for period in ['early','recent']:
        dest=RUN/period
        subprocess.run([exe,str(dest/'execution.protocol.json'),period,'replay',str(dest/'executions')],check=True)

def analyze():
    p=READ(RUN/'protocol.json');meta=READ(RUN/'metadata.json');events={};ends={};rows=collections.defaultdict(list)
    fees=READ(r.ROOT/'scripts/exchange_fees_20261022.json');qa=collections.Counter();sources={}
    # Independently load source individual results for exact original-clock controls.
    expected={}
    for period in ['early','recent']:
        cal=r.s.TradingDates({});cal.dates=READ(r.RUN/period/'trading-dates.json')
        for line in (r.s.NATIVE/period/'evaluations.jsonl').open(encoding='utf-8'):
            raw=json.loads(line);prefix,sf=raw['scenario'].split('/',1)
            if prefix=='P2_economics_budget_body' and raw['symbol'] in r.SYMS:expected[raw['event_key'],sf]=r.s.reprice(raw,fees,cal)
        dest=RUN/period
        for z in READ(dest/(period+'-pool.json')):events[z['event_key']]=z['event']
        for c,end in READ(dest/'executions/manifest.json')['contract_ends'].items():ends[c]=max(end,ends.get(c,''))
        path=dest/'executions/evaluations.jsonl';sources[str(path)]=r.s.m.old.sha(path)
        for line in path.open(encoding='utf-8'):
            z=r.s.reprice(json.loads(line),fees,cal);_,sf=z['scenario'].split('/',1);m=meta[z['event_key']]
            if m['replicate']==-1:
                ref=expected[m['source_key'],sf]
                for k in ['result','entry','stop','target','planned_risk','new_friction','cost_repricing']:
                    assert z.get(k)==ref.get(k),(m['source_key'],sf,k)
                qa['exact_original_clock_individuals']+=1
            if EV(z)['status']=='closed':
                assert EV(z)['entry_ts']>z['warning_ts']
                for f in z['result']['fills']:assert abs(f['price']/z['tick_size']-round(f['price']/z['tick_size']))<1e-6
            rows[m['replicate'],sf].append(z)
    c,pp,_,_,_,_,_=r.s.m.load()
    result={};candidate={};counts={};test_start=p['test_start'];test_months=[mo for mo in r.MONTHS if mo>=test_start[:7]]
    for pol in r.POLICIES:
        for sf in r.s.m.SUFFIXES:
            case=pol+'/'+sf;result[case]={};candidate[case]={};counts[case]=[]
            for rep in range(-1,N):
                rr=rows[rep,sf]
                aa,ll,risk=c.select(rr,events,ends,pp['sector_map'],'sector2_counter_stops',pol)
                r.s.m.report.audit(ll,rr)
                scored=[z for z in aa if z['warning_ts']>=test_start]
                z=r.metric(scored,meta,test_months)
                z['products']={sym:r.metric([v for v in scored if v['symbol']==sym],meta,test_months) for sym in r.SYMS}
                result[case][str(rep)]=z
                candidate[case][str(rep)]=r.metric([v for v in rr if v['warning_ts']>=test_start],meta,test_months)
                if rep==-1:
                    write('accepted-'+pol+'-'+sf.replace('/','-')+'.jsonl',[dict(source_key=meta[v['event_key']]['source_key'],row=v) for v in aa])
            print('Plan-preserving199 replicas complete:',case,flush=True)
    write('results.json',result);write('candidate-results.json',candidate)
    diffs=[q for q,h in p['production_before'].items() if r.s.m.old.sha(r.ROOT/q)!=h];assert not diffs
    write('verification.json',dict(counts=dict(qa),all_original_clock_controls_exact=True,
        all_fill_tick_and_time_checks=True,all_causal_interval_audits=True,source_hashes=sources,production_hash_differences=diffs))
    print('Plan-preserving null complete.',flush=True)

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('stage',choices=['prepare','generate','replay','analyze']);a=ap.parse_args();globals()[a.stage]()
