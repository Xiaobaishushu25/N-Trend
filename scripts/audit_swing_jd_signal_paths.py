"""ABC warning path audit: excursions, barrier order, pure triggers and controls.

Descriptive, no new trading rules. Existing actual member archives only.
"""
import argparse
import bisect
import collections
import copy
import datetime as dt
import json
import math
import random
import shutil
import statistics as st
import subprocess
import sys
import unittest

sys.dont_write_bytecode=True
import research_swing_jd_frequency as z

ROOT=z.ROOT
RUN=ROOT/'research-runs/swing-jd-abc-path-audit-20261026-v2'
READ=z.READ
EV=z.EV
HORIZONS=[1,2,4,8,12,16,32,60]
THRESHOLDS=[.5,1.,1.5,2.,3.]
NULL_N=199
SYMS=['JD0','EG0']
MONTHS=z.MONTHS
MINUTES=list(range(9*60+1,10*60+16))+list(range(10*60+31,11*60+31))+list(range(13*60+31,15*60+1))
SLOTS=[9*60+15,9*60+30,9*60+45,10*60,10*60+15,10*60+45,11*60,11*60+15,11*60+30,13*60+45,14*60,14*60+15,14*60+30,14*60+45,15*60]

def write(n,v):z.s.m.old.write(RUN/n,v)
def bucket(ts):
    t=dt.datetime.fromisoformat(ts)
    return (t+dt.timedelta(minutes=(-t.minute)%15)).isoformat(sep=' ')

def quant(xs,p):
    if not xs:return None
    a=sorted(xs);v=(len(a)-1)*p;lo=int(v);hi=math.ceil(v)
    return a[lo]+(a[hi]-a[lo])*(v-lo)

def stats(xs):
    xs=list(xs)
    return dict(n=len(xs),mean=st.mean(xs) if xs else None,median=quant(xs,.5),p25=quant(xs,.25),p75=quant(xs,.75),p90=quant(xs,.9),maximum=max(xs) if xs else None)

def barrier(rows,anchor,risk,sign,upper=1.,lower=1.,stop=None):
    """OHLC open resolves gaps; both barriers inside same minute stays ambiguous."""
    high=anchor+sign*upper*risk;low=anchor-sign*lower*risk if stop is None else stop
    for b in rows:
        if (b['open']-high)*sign>=-1e-9:return 'upper_first',b['datetime']
        if (b['open']-low)*sign<=1e-9:return 'stop_first',b['datetime']
        good=b['high'] if sign>0 else b['low'];bad=b['low'] if sign>0 else b['high']
        up=(good-high)*sign>=-1e-9;down=(bad-low)*sign<=1e-9
        if up and down:return 'same_minute_unknown',b['datetime']
        if up:return 'upper_first',b['datetime']
        if down:return 'stop_first',b['datetime']
    return 'neither',None

def excursion(rows,anchor,risk,sign):
    if not rows:return dict(mfe=0.,mae=0.,end_r=0.,favorable_points=0.,adverse_points=0.)
    good=max(b['high'] for b in rows) if sign>0 else min(b['low'] for b in rows)
    bad=min(b['low'] for b in rows) if sign>0 else max(b['high'] for b in rows)
    fp=max(0.,(good-anchor)*sign);ap=max(0.,(anchor-bad)*sign)
    return dict(mfe=fp/risk,mae=ap/risk,end_r=(rows[-1]['close']-anchor)*sign/risk,
        favorable_points=fp,adverse_points=ap)

class Prices:
    def __init__(self,paths):
        inp=READ(paths['input']);self.calendar=z.s.TradingDates(inp['symbols']).dates
        self.labels=[]
        for date in self.calendar:
            for m in SLOTS:self.labels.append(f'{date} {m//60:02}:{m%60:02}:00')
        self.contracts={code:rec['minutes'] for code,rec in READ(paths['contracts']).items() if code.startswith(('DCE.jd','DCE.eg'))}
        self.maps={code:{b['datetime']:b for b in bs} for code,bs in self.contracts.items()}
        self.ends={code:bs[-1]['datetime'] for code,bs in self.contracts.items()}
        self.segments=READ(paths['segments'])
        self.input=inp
        nights=sorted({b['datetime'][:10] for b in inp['symbols']['EG0']['bars'] if '21:00:00'<b['datetime'][11:]<='23:00:00'})
        self.eg_labels=sorted(self.labels+[f'{d} {m//60:02}:{m%60:02}:00' for d in nights for m in range(21*60+15,23*60+1,15)])
        self.cache={}
    def code(self,sym,ts):
        return next(s['underlying'] for s in self.segments[sym] if s['first']<=ts<=s['last'])
    def path(self,code,ts,n=72):
        key=code,ts,n
        if key in self.cache:return self.cache[key]
        i=bisect.bisect_right(self.labels,ts);labels=self.labels[i:i+n];groups=[];quality='ok';missing=None
        lookup=self.maps[code]
        # EG night dates come from the archived continuous source calendar;
        # a missing whole quarter in an individual member is still censored.
        if code.startswith('DCE.eg'):
            at=bisect.bisect_right(self.eg_labels,ts);labels=self.eg_labels[at:at+n]
        for label in labels:
            end=dt.datetime.fromisoformat(label);wanted=[(end-dt.timedelta(minutes=j)).isoformat(sep=' ') for j in range(14,-1,-1)]
            bs=[]
            for t in wanted:
                b=lookup.get(t)
                if b is None:
                    quality='archive_end' if t>self.ends[code] else 'missing_minute';missing=t;break
                if b['volume']<=0:quality='zero_volume';missing=t;break
                bs.append(b)
            if quality!='ok':break
            groups.append(bs)
        if len(groups)<n and quality=='ok':quality='calendar_or_archive_end'
        out=dict(groups=groups,quality=quality,missing=missing)
        self.cache[key]=out
        return out

def window(path,n):
    return [b for group in path['groups'][:n] for b in group],len(path['groups'])>=n

def trigger(path,e):
    sign=1 if e['direction']=='up' else -1
    for gi,group in enumerate(path['groups'][:12]):
        for bi,b in enumerate(group):
            if (b['open']-e['stop'])*sign<=0:return dict(status='invalidated_before_trigger',ts=b['datetime'])
            if (b['open']-e['entry'])*sign>=0:
                return dict(status='triggered',ts=b['datetime'],group=gi,minute=bi,observed=b['open'],at_open=True)
            good=b['high'] if sign>0 else b['low'];bad=b['low'] if sign>0 else b['high']
            hit=(good-e['entry'])*sign>=0;invalid=(bad-e['stop'])*sign<=0
            if hit and invalid:return dict(status='trigger_order_unknown',ts=b['datetime'])
            if invalid:return dict(status='invalidated_before_trigger',ts=b['datetime'])
            if hit:return dict(status='triggered',ts=b['datetime'],group=gi,minute=bi,observed=e['entry'],at_open=False)
    return dict(status='untriggered' if len(path['groups'])>=12 else 'censored_pending',ts=None)

def suffix_vertices(b,tr,level,sign):
    out=[]
    for vertices in [[b['open'],b['high'],b['low'],b['close']],[b['open'],b['low'],b['high'],b['close']]]:
        if tr['at_open']:out.append(vertices);continue
        suffix=None
        for j,(a,c) in enumerate(zip(vertices,vertices[1:])):
            if (c-a)*sign>=0 and (c-level)*sign>=0 and (a-level)*sign<=0:
                suffix=[level]+vertices[j+1:];break
        assert suffix is not None
        out.append(suffix)
    return out

def post_trigger(path,e,tr,n):
    gi,mi=tr['group'],tr['minute'];groups=path['groups'][gi:gi+n]
    complete=len(groups)>=n
    raw=[b for j,g in enumerate(groups) for b in (g[mi:] if j==0 else g)]
    sign=1 if e['direction']=='up' else -1;fill=tr['observed']+sign*e['tick_size'];risk=(fill-e['stop'])*sign
    assert risk>0
    first=raw[0];tails=suffix_vertices(first,tr,e['entry'],sign);later=raw[1:]
    values=[]
    for tail in tails:
        good=max(tail) if sign>0 else min(tail);bad=min(tail) if sign>0 else max(tail)
        if later:
            good=max(good,max(b['high'] for b in later)) if sign>0 else min(good,min(b['low'] for b in later))
            bad=min(bad,min(b['low'] for b in later)) if sign>0 else max(bad,max(b['high'] for b in later))
        values.append((max(0.,(good-fill)*sign/risk),max(0.,(fill-bad)*sign/risk)))
    # An entry resolved at the minute OPEN can hit its stop later in that same
    # minute. Include it in the race; both barriers inside it remain unknown.
    # For intrabar entry, a pre-entry stop touch was already excluded by trigger().
    races={}
    for q in THRESHOLDS:
        result,at=barrier(raw,fill,risk,sign,q,stop=e['stop'])
        if result=='neither' and not complete:result='censored'
        races[str(q)]=result
    return dict(complete=complete,fill=fill,risk=risk,mfe_lower=min(v[0] for v in values),mfe_upper=max(v[0] for v in values),
        mae_lower=min(v[1] for v in values),mae_upper=max(v[1] for v in values),races=races)

def describe(records,h,view='warning'):
    xs=[a[view][str(h)] for a in records if view in a]
    full=[a for a in xs if a['complete']]
    out=dict(n=len(xs),complete=len(full),censored=len(xs)-len(full))
    if view=='warning':
        for name in ['mfe','mae','end_r','favorable_points','adverse_points']:out[name]=stats(a[name] for a in full)
        out['mfe_atr']=stats(a['mfe_atr'] for a in full);out['mae_atr']=stats(a['mae_atr'] for a in full)
        out['reverse_mfe']=out['mae'];out['reverse_mae']=out['mfe']
        for race_name in ['races','plan_races']:
            out[race_name]={str(q):dict(collections.Counter(a[race_name][str(q)] for a in xs)) for q in THRESHOLDS}
        out['reach']={str(q):dict(mfe=sum(a['mfe']>=q-1e-9 for a in full),mae=sum(a['mae']>=q-1e-9 for a in full)) for q in THRESHOLDS}
    else:
        for name in ['mfe_lower','mfe_upper','mae_lower','mae_upper']:out[name]=stats(a[name] for a in full)
        out['races']={str(q):dict(collections.Counter(a['races'][str(q)] for a in xs)) for q in THRESHOLDS}
    return out

def prepare():
    RUN.mkdir(exist_ok=False)
    parent=READ(ROOT/'research-strategies/best-a-portfolio-observation-v1/protocol.json')
    write('protocol.json',dict(created_at=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(),
        purpose='Preserve ABC, distinguish warning quality, trigger/stop geometry and monetization; descriptive paths, no optimized stop/target candidates.',
        horizons=HORIZONS,thresholds=THRESHOLDS,periods=parent['periods'],symbols=SYMS,
        signals='B5 research baseline unmerged selector4, service-kept warnings before any>=2.5grade/geometry/cost/body/execution filter. Re-generate low-score warning events; verify frozen service keys and qualified pools. These are historical reconstructed research warnings, not a claimed live notification log.',
        warning_reference='P0=completed warning15m CLOSE; original plannedR0=abs(original entry-originalstop), unaffected by target cap. MFE/MAE nonnegative gross peak distances/R0, start strictly next minute. Also points/ATR to expose narrow-R inflation. Full future horizon ignores stop/target; not obtainable trading profits.',
        barrier_order='At each1m OPEN resolve gap before high/low; both thresholds hit inside one minute with open between =>same_minute_unknown. Warn-close +/-qR vs-1R is symmetric signal diagnostic; plan-entry+qR vs originalstop is separate true structural-price diagnostic; a stop before entry is invalidation, not an executed loss.',
        trigger='Pure original entry touch before originalstop within12observed15m buckets, ignores plan economics/grade/hourly adaptive/target. Same-minute entry/stop ambiguous excluded. Fill=touch or favorable gap-open+1tick; actual fillR to same originalstop. Counterfactual horizons continue after any stop. Trigger-minute post-entry excursions bounded across2OHLC paths; never use pre-entry extrema as certain.',
        data='Existing actual member1m prices only; do not splice to next contract. JD expected225minute-end labels per observed trading day,15 complete15m bars; missing/zero-volume/archive tails censor all subsequent windows. EG night dates inferred from continuous archive, full expected night/day quarters checked per actual member. No2025 forward raw path without minute archives.',
        samples='All service-kept,score>=2.5,cost/geometry rejection buckets,pure-trigger validity,originalB5 accepted,E20S1 accepted and actual-held excursions. Report warning/month/quarter and first warning perAcluster to show overlaps; not independent trades.',
        null=dict(repetitions=NULL_N,seed=2026102617,strata=['month','HH:MM','direction','past16momentum_sign','floor(log2ATRticks)'],
            source='Saved shared-clock unlabelled features, all actual same-member unique15m clock candidates with complete16bar data. Original warning times retained in population. Without replacement within stratum; copy warning risk/ATR ratio to sampled clockATR.',
            metrics='16bar meanMFE,meanMAE,meancloseR and+1R-before-1R including interval for same-minute ambiguity. Match warning counts, never profitable/accepted counts.',
            limitation='Retrospective conditional timing diagnostic, not causal deployable random strategy, independent OOS or calibrated significance; completeness-matched support disclosed.'),
        limitations='Previously researched history, sparse/incomplete dates and overlapping ABCwaves; forward excursion alone cannot validate direction or choose TP ex post. Future MFE/MAE are outcome labels only, never pre-warning features.',
        production_before=READ(z.RUN/'protocol.json')['production_before'],script_sha256=z.s.m.old.sha(__file__),qualified_for_trading=False))
    fork=RUN/'fork';shutil.copytree(z.RUN/'fork',fork)
    src=(ROOT/'scripts/swing_wave_pool.rs').read_text(encoding='utf-8')
    src=z.s.m.old.replace(src,'for(symbol,v)in inp["symbols"].as_object().unwrap(){','for(symbol,v)in inp["symbols"].as_object().unwrap(){\n        if symbol!="JD0" && symbol!="EG0" {continue;}')
    src=z.s.m.old.replace(src,'if !allowed.contains(&k)||e.setup_features.setup_quality<2.5{continue;}','if !allowed.contains(&k){continue;}')
    (fork/'crates/n-core/src/bin/swing_abc_signal_audit.rs').write_text(src,encoding='utf-8')
    write('implementation-manifest.json',{q.relative_to(fork).as_posix():z.s.m.old.sha(q) for q in fork.rglob('*') if q.is_file()})
    write('generation.protocol.json',dict(selector=4,periods=parent['periods'],hashes=parent['hashes']))
    print('Frozen ABC path audit; no stop/target search.',flush=True)

def generate():
    # Generation requires the actual frozen unmerged pivot extractor. The exit
    # engine fork accepts already-generated pools and only has selectors0..3.
    fork=RUN/'generation-fork'
    if not fork.exists():
        shutil.copytree(ROOT/'research-runs/swing-wave-selection-20261025-v1/unmerged_extension/fork',fork)
        shutil.copy2(RUN/'fork/crates/n-core/src/bin/swing_abc_signal_audit.rs',fork/'crates/n-core/src/bin/swing_abc_signal_audit.rs')
        write('generation-implementation-manifest.json',{q.relative_to(fork).as_posix():z.s.m.old.sha(q) for q in fork.rglob('*') if q.is_file()})
    args=['cargo','build','--release','--offline','--manifest-path',str(fork/'Cargo.toml'),'--target-dir',str(ROOT/'target/swing-abc-path-generator-isolated'),'-p','n-core','--bin','swing_abc_signal_audit']
    subprocess.run(args,check=True)
    exe=ROOT/'target/swing-abc-path-generator-isolated/release/swing_abc_signal_audit.exe'
    write('generator-binary-v2.json',dict(path=str(exe),sha256=z.s.m.old.sha(exe)))
    for period in ['early','recent']:
        subprocess.run([str(exe),str(RUN/'generation.protocol.json'),period,str(RUN/(period+'-symbols')),str(RUN/(period+'-pool.json'))],check=True)

def summarize_groups(records):
    groups={'all':records,'qualified':[a for a in records if a['score']>=2.5],
        'low_score':[a for a in records if a['score']<2.5],
        'risk_lt10':[a for a in records if a['planned_risk_ticks']<10],
        'risk_ge10':[a for a in records if a['planned_risk_ticks']>=10]}
    for label in ['B5_accepted','E20S1_accepted']:
        groups[label]=[a for a in records if a[label]]
    for status in sorted({a['B5_status'] for a in records}):groups['B5_status/'+status]=[a for a in records if a['B5_status']==status]
    for kind in sorted({a['kind'] for a in records}):groups['kind/'+kind]=[a for a in records if a['kind']==kind]
    for name,lo,hi in [('Q1','2026-01','2026-04'),('Q2','2026-04','2026-07'),('Q3','2026-07','2026-10'),('Oct_partial','2026-10','2026-11')]:groups[name]=[a for a in records if lo<=a['warning_ts'][:7]<hi]
    first={}
    for a in sorted(records,key=lambda a:(a['warning_ts'],a['event_key'])):first.setdefault(a['cluster'],a)
    groups['first_per_Acluster']=list(first.values())
    return {name:dict(n=len(xs),trigger_status=dict(collections.Counter(a['trigger']['status'] for a in xs)),
        warning={str(h):describe(xs,h) for h in HORIZONS},post_trigger={str(h):describe(xs,h,'post_trigger') for h in HORIZONS}) for name,xs in groups.items()}

def analyze():
    p=READ(RUN/'protocol.json');assert z.s.m.old.sha(__file__) in {p['script_sha256']}|{READ(q)['script_sha256'] for q in RUN.glob('*amendment*.json')}
    clocks=READ(z.x.r.RUN/'clock-metadata.json');all_records=[];qa=collections.Counter();prices={};sources={};snaps={};known={}
    # Frozen native accepted labels are secondary strata, never warning selection.
    accepted={}
    for line in (z.x.RUN/'accepted-primary.jsonl').open(encoding='utf-8'):
        a=json.loads(line)
        if a['product']=='JD0' and a['model'] in ['B5','E20S1'] and EV(a['row'])['entry_ts'] is not None:
            accepted[a['model'],a['row']['event_key']]=a['row']
    for period in ['early','recent']:
        paths=p['periods'][period];pr=Prices(paths);prices[period]=pr
        sources[period]={k:z.s.m.old.sha(paths[k]) for k in ['input','contracts','segments','fees','holidays']}
        old={a['event_key']:a for a in READ(z.s.NATIVE/(period+'-pool.json'))}
        statuses={}
        for line in (z.x.RUN/period/'JD/executions/evaluations.jsonl').open(encoding='utf-8'):
            a=json.loads(line)
            if a['scenario'] in ['B5/corrected_touch/slip1','E20S1/corrected_touch/slip1']:statuses[a['scenario'].split('/')[0],a['event_key']]=a
        pool=READ(RUN/(period+'-pool.json'))
        for sym in SYMS:
            ref=READ(ROOT/f'research-runs/swing-wave-selection-20261025-v1/unmerged/{period}-symbols/{sym}.json')
            lo,hi=('2026-01-01 00:00:00','2026-06-30 15:00:00') if period=='early' else ('2026-07-01 00:00:00','2026-10-23 11:05:00')
            expected={k for k in ref['kept_keys'] if lo<=k.rsplit('|',1)[1]<=hi}
            actual={a['event_key'] for a in pool if a['event']['symbol']==sym}
            assert actual==expected,(period,sym,len(actual),len(expected))
            qa['exact_service_kept_keys']+=len(actual)
        for a in pool:
            e=a['event'];k=a['event_key'];snaps[k]=a
            if e['setup_features']['setup_quality']>=2.5:
                assert k in old
                for field in ['entry','stop','target','risk','warning_ts','direction','s0_ts','s1_ts']:assert e[field]==old[k]['event'][field]
                qa['qualified_geometry_equal']+=1
            assert e['evaluation'] is None and e['entry_ts'] is None
            if e['risk']<=0:qa['invalid_original_R']+=1;continue
            code=pr.code(e['symbol'],e['warning_ts']);path=pr.path(code,e['warning_ts'])
            sign=1 if e['direction']=='up' else -1;anchor=a['warning_candle']['close'];risk=e['risk']
            assert abs(pr.maps[code][e['warning_ts']]['close']-anchor)<1e-7,(code,e['warning_ts'],'signal/actual member close mismatch')
            qa['warning_close_equals_actual_member']+=1
            row=dict(event_key=k,symbol=e['symbol'],direction=e['direction'],warning_ts=e['warning_ts'],period=period,contract=code,
                score=e['setup_features']['setup_quality'],kind=e['warning_kind'],planned_entry=e['entry'],original_stop=e['stop'],original_target=e['target'],
                planned_risk=risk,planned_risk_ticks=risk/e['tick_size'],atr=a['atr14'],warning_close=anchor,
                cluster='|'.join(e[f] for f in ['symbol','direction','s0_ts','s1_ts']),
                B5_accepted=('B5',k) in accepted,E20S1_accepted=('E20S1',k) in accepted,
                B5_status=EV(statuses['B5',k])['status'] if ('B5',k) in statuses else 'below_score_or_comparator',
                path_quality=path['quality'],path_missing=path['missing'],warning={})
            for h in HORIZONS:
                raw,complete=window(path,h);v=excursion(raw,anchor,risk,sign)
                v.update(complete=complete,mfe_atr=v['favorable_points']/a['atr14'],mae_atr=v['adverse_points']/a['atr14'],races={},plan_races={})
                for q in THRESHOLDS:
                    status,ts=barrier(raw,anchor,risk,sign,q)
                    if status=='neither' and not complete:status='censored'
                    v['races'][str(q)]=status
                    status,ts=barrier(raw,e['entry'],risk,sign,q,stop=e['stop'])
                    if status=='neither' and not complete:status='censored'
                    v['plan_races'][str(q)]=status
                row['warning'][str(h)]=v
            tr=trigger(path,e);row['trigger']=tr
            if tr['status']=='triggered':row['post_trigger']={str(h):post_trigger(path,e,tr,h) for h in HORIZONS}
            for model in ['B5','E20S1']:
                if (model,k) in accepted:row[model+'_actual_held']=EV(accepted[model,k])
            all_records.append(row)
    results={sym:summarize_groups([a for a in all_records if a['symbol']==sym]) for sym in SYMS}
    write('results.json',results)
    with (RUN/'per-warning.jsonl').open('x',encoding='utf-8') as f:
        for a in all_records:f.write(json.dumps(a,ensure_ascii=False)+'\n')
    write('coverage.json',dict(sources=sources,counts=dict(qa),warning_count=dict(collections.Counter(a['symbol'] for a in all_records))))
    null=timing_null([a for a in all_records if a['symbol']=='JD0'],snaps,clocks,prices)
    write('matched-timing-null.json',null)
    for q,h in p['production_before'].items():assert z.s.m.old.sha(ROOT/q)==h
    for q,h in READ(RUN/'implementation-manifest.json').items():assert z.s.m.old.sha(RUN/'fork'/q)==h
    for q,h in READ(RUN/'generation-implementation-manifest.json').items():assert z.s.m.old.sha(RUN/'generation-fork'/q)==h
    write('verification.json',dict(exact_counts=dict(qa),production_changed=False,forward_labels_not_inputs=True,
        signal_and_entry_R_separate=True,warning_bar_excluded=True,minute_ambiguity_not_resolved_by_profit=True,
        censored_windows_separate=True,source_hashes=sources,not_new_strategy_backtest=True))
    print(json.dumps(dict(counts=dict(qa),JD={h:{k:results['JD0']['all']['warning'][h][k] for k in ['complete','censored','mfe','mae']} for h in ['4','8','16']},null={k:v for k,v in null.items() if k!='repetitions'}),ensure_ascii=False),flush=True)

def timing_null(records,snaps,clocks,prices):
    def group(ts,direction,m):return '|'.join([ts[:7],ts[11:16],direction,str(m['momentum_sign']),str(math.floor(math.log2(m['atr14'])))])
    pops=collections.defaultdict(list);warns=collections.defaultdict(list);candidate={}
    for key,m in clocks.items():
        if m['symbol']!='JD0':continue
        pr=prices[m['period']];path=pr.path(m['contract'],m['warning_ts'],16);raw,complete=window(path,16)
        if not complete:continue
        g=group(m['warning_ts'],m['direction'],m);pops[g].append(key);candidate[key]=(m,raw,pr.maps[m['contract']][m['warning_ts']]['close'])
    matched=[]
    for a in records:
        key='|'.join(['CLOCK','JD0',a['direction'],a['warning_ts']])
        if key not in candidate or not a['warning']['16']['complete']:continue
        m,_,_=candidate[key];g=group(a['warning_ts'],a['direction'],m)
        warns[g].append(a);matched.append(a)
    assert all(len(ws)<=len(pops[g]) for g,ws in warns.items())
    def one(rows):
        n=len(rows)
        return dict(n=n,mean_mfe=st.mean(a['mfe'] for a in rows),mean_mae=st.mean(a['mae'] for a in rows),mean_close_r=st.mean(a['end_r'] for a in rows),
            oneR_before_stop_lower=sum(a['race']=='upper_first' for a in rows)/n,
            oneR_before_stop_upper=sum(a['race'] in ['upper_first','same_minute_unknown'] for a in rows)/n)
    observed=one([dict(mfe=a['warning']['16']['mfe'],mae=a['warning']['16']['mae'],end_r=a['warning']['16']['end_r'],race=a['warning']['16']['races']['1.0']) for a in matched])
    reps=[]
    for rep in range(NULL_N):
        rng=random.Random(2026102617+rep);xs=[]
        for g,ws in sorted(warns.items()):
            chosen=rng.sample(sorted(pops[g]),len(ws))
            for a,key in zip(sorted(ws,key=lambda a:a['event_key']),chosen):
                m,raw,anchor=candidate[key];risk=a['planned_risk']/snaps[a['event_key']]['atr14']*m['atr14'];sign=1 if a['direction']=='up' else -1
                v=excursion(raw,anchor,risk,sign);v['race']=barrier(raw,anchor,risk,sign)[0];xs.append(v)
        reps.append(one(xs))
    diagnostics={}
    for field in observed:
        if field=='n':continue
        vals=[a[field] for a in reps]
        diagnostics[field]=dict(observed=observed[field],null_median=quant(vals,.5),null_p05=quant(vals,.05),null_p95=quant(vals,.95),percentile=100*sum(v<observed[field] for v in vals)/len(vals))
    return dict(matched_warning_count=len(matched),all_JD_warnings=len(records),strata=len(warns),
        locked_strata=sum(len(pops[g])==len(ws) for g,ws in warns.items()),observed=observed,summary=diagnostics,repetitions=reps,
        limitation='Matched full16bar retrospective support; not executable random strategy or calibrated significance. Large MFE is expected even at non-signal times.')

class PathTests(unittest.TestCase):
    def b(self,o,h,l,c,ts='2026-01-05 09:16:00'):
        return dict(datetime=ts,open=o,high=h,low=l,close=c,volume=1.)
    def test_both_barriers_in_one_minute_are_not_optimistically_ordered(self):
        self.assertEqual(barrier([self.b(100,112,88,101)],100,10,1)[0],'same_minute_unknown')
    def test_open_gap_decides_first_before_same_minute_other_extreme(self):
        self.assertEqual(barrier([self.b(111,112,88,100)],100,10,1)[0],'upper_first')
        self.assertEqual(barrier([self.b(89,112,88,100)],100,10,1)[0],'stop_first')
    def test_long_short_mirroring_and_inclusion_of_zero_start(self):
        a=excursion([self.b(100,112,94,103)],100,10,1);b=excursion([self.b(100,106,88,97)],100,10,-1)
        self.assertEqual(a,b)
        c=excursion([self.b(90,95,88,91)],100,10,1);self.assertEqual(c['mfe'],0.)
    def test_entry_stop_same_minute_is_trigger_unknown_not_a_fill(self):
        p=dict(groups=[[self.b(99,103,88,100)]]);e=dict(direction='up',entry=100,stop=90)
        self.assertEqual(trigger(p,e)['status'],'trigger_order_unknown')
    def test_pre_entry_low_is_not_certain_post_entry_MAE(self):
        b=self.b(99,105,95,104);tr=dict(at_open=False)
        tails=suffix_vertices(b,tr,100,1)
        self.assertEqual(sorted(min(a) for a in tails),[95,100])
    def test_censored_window_has_not_been_completed_by_later_bars(self):
        raw,complete=window(dict(groups=[[self.b(100,101,99,100)]]),4)
        self.assertFalse(complete);self.assertEqual(len(raw),1)
    def test_open_entry_stop_in_entry_minute_precedes_later_profit(self):
        e=dict(direction='up',entry=100.,stop=90.,tick_size=1.)
        path=dict(groups=[[self.b(101,104,89,100),self.b(100,120,99,119,'2026-01-05 09:17:00')]])
        tr=trigger(path,e);self.assertTrue(tr['at_open'])
        self.assertEqual(post_trigger(path,e,tr,1)['races']['1.0'],'stop_first')
    def test_open_entry_two_barriers_same_minute_stays_unknown(self):
        e=dict(direction='up',entry=100.,stop=90.,tick_size=1.)
        path=dict(groups=[[self.b(101,120,89,100)]])
        tr=trigger(path,e)
        self.assertEqual(post_trigger(path,e,tr,1)['races']['1.0'],'same_minute_unknown')

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('stage',choices=['test','prepare','generate','analyze']);a=ap.parse_args()
    if a.stage=='test':unittest.main(argv=[sys.argv[0]])
    else:globals()[a.stage]()
