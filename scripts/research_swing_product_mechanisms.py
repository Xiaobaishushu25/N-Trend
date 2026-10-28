"""Finite JD exit and SR signal-mechanism study on saved 2026 minutes.

All artifacts are new research files. No production or frozen evidence edits.
"""
import argparse
import bisect
import collections
import copy
import datetime as dt
import json
import math
import shutil
import statistics as st
import subprocess
import sys
import unittest

sys.dont_write_bytecode = True
import research_swing_incremental_edge as r
from report_swing_wave_selection import identification_bars

s = r.s
ROOT = s.ROOT
RUN = ROOT/'research-runs/swing-product-mechanisms-20261026-v1'
READ = r.READ
EV = r.EV
MONTHS = r.MONTHS
FOLDS = r.FOLDS
JD = ['B5', 'S1', 'E20S1', 'S1_N8', 'S1_H120', 'S1_PATIENT']
SR = ['B5', 'FAIL20', 'HOUR20']
LABELS = {'B5':'完整B5', 'S1':'原S1目标', 'E20S1':'原S1＋成本比20%',
          'S1_N8':'原S1＋第8桶无跟随', 'S1_H120':'原S1＋最长120桶',
          'S1_PATIENT':'原S1＋取消无跟随＋最长120桶',
          'FAIL20':'15分钟失败突破回归', 'HOUR20':'完整小时20柱突破'}

def write(name, obj): s.m.old.write(RUN/name, obj)

def grid(x, tick, up):
    return (math.ceil(x/tick-1e-9) if up else math.floor(x/tick+1e-9))*tick

def signal(bars, i, tick, model):
    """Current completed candle and strictly preceding lookback only."""
    assert i >= 20
    prior = bars[i-20:i]
    b = bars[i]
    hi = max(z['high'] for z in prior)
    lo = min(z['low'] for z in prior)
    atr = sum(max(bars[j]['high']-bars[j]['low'], abs(bars[j]['high']-bars[j-1]['close']),
                  abs(bars[j]['low']-bars[j-1]['close'])) for j in range(i-13, i+1))/14
    if model == 'FAIL20':
        # Reject two-sided excursions; the close must return strictly inside.
        if not lo < b['close'] < hi: return None
        top, bottom = b['high'] >= hi+tick-1e-9, b['low'] <= lo-tick+1e-9
        if top == bottom: return None
        direction = 'down' if top else 'up'
    else:
        direction = 'up' if b['close'] > hi else 'down' if b['close'] < lo else None
        if direction is None: return None
    up = direction == 'up'
    sign = 1 if up else -1
    entry = grid(b['high'], tick, True)+tick if up else grid(b['low'], tick, False)-tick
    if model == 'FAIL20':
        stop = grid(b['low'], tick, False)-tick if up else grid(b['high'], tick, True)+tick
        target = grid((hi+lo)/2, tick, not up)
        risk = abs(entry-stop)
    else:
        risk = grid(atr, tick, True)
        if risk <= 0: return None
        stop = entry-sign*risk
        target = entry+sign*2*risk
    if risk <= 0: return None
    return dict(direction=direction, entry=entry, stop=stop, target=target, risk=risk,
                rr=max(0., sign*(target-entry)/risk), atr14=atr, range_high=hi, range_low=lo,
                target_forward=sign*(target-entry)>0)

def full_hours(raw):
    """Only twelve consecutive, unique 5m close-stamped bars; no partial hours."""
    groups = collections.defaultdict(list)
    for b in raw:
        t = dt.datetime.fromisoformat(b['datetime'])
        end = t+dt.timedelta(minutes=(-t.minute)%60)
        groups[end.isoformat(sep=' ')].append(b)
    out = []
    for ts, bs in sorted(groups.items()):
        dates = [dt.datetime.fromisoformat(z['datetime']) for z in bs]
        if len(bs) != 12 or any((b-a).total_seconds()!=300 for a,b in zip(dates, dates[1:])): continue
        if bs[-1]['datetime'] != ts: continue
        out.append(dict(datetime=ts, open=bs[0]['open'], high=max(b['high'] for b in bs),
                        low=min(b['low'] for b in bs), close=bs[-1]['close']))
    return out

def visible(rows, cutoff):
    return [z for z in rows if z['warning_ts'] < cutoff and EV(z)['status']=='closed' and EV(z)['exit_ts'] < cutoff]

def metric(rows, events, months=MONTHS):
    z = r.metric(rows, {}, months)
    cs = [a for a in rows if EV(a)['status']=='closed']
    z['month_cash'] = {mo:sum(EV(a)['net_amount'] for a in cs if a['warning_ts'][:7]==mo) for mo in months}
    z['exits'] = {reason:dict(n=len(xs),net=sum(EV(a)['net_amount'] for a in xs))
                  for reason,xs in _by(cs,lambda a:EV(a)['exit_reason']).items()}
    z['normalized_ex_top3'] = z['normalized_net']-sum(sorted([max(0,300*EV(a)['net_amount']/(a['planned_risk']*a['multiplier'])) for a in cs],reverse=True)[:3])
    z['positive_active_months'] = sum(v>0 for v in z['month_cash'].values())
    z['active_months'] = len({a['warning_ts'][:7] for a in cs})
    return z

def _by(xs, key):
    out = collections.defaultdict(list)
    for x in xs: out[key(x)].append(x)
    return out

def prepare():
    RUN.mkdir(exist_ok=False)
    parent = READ(s.ARCHIVE/'protocol.json')
    base = copy.deepcopy(parent['templates'][0])
    templates = {}
    for name in JD:
        t = copy.deepcopy(base); t['name']=name
        if name!='B5': t['local']=False
        if name=='E20S1': t['max_cost_space']=.2
        if name=='S1_N8': t['no_follow_bar']=8
        if name in ['S1_H120','S1_PATIENT']: t['rules']['max_holding_buckets']=120
        if name=='S1_PATIENT': t['no_follow_bar']=None
        templates[name]=t
    for name in SR[1:]:
        t=copy.deepcopy(base); t.update(name=name,local=False,adaptive=False,body_net_gate=False)
        t['rules']['pending_buckets']=4
        if name=='HOUR20': t.update(no_follow_bar=20);t['rules']['max_holding_buckets']=240
        templates[name]=t
    production = {q:s.m.old.sha(ROOT/q) for q in parent['production_before']}
    protocol = dict(id=RUN.name,created_at=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(),
        purpose='Freeze EG/PVC B5; examine JD target/holding constraints and two SR mechanism hypotheses.',
        symbols=s.FOUR,months=MONTHS,folds=FOLDS,templates=templates,JD_variants=JD,SR_variants=SR,
        scenarios=s.m.SUFFIXES,policies=r.POLICIES,primary='original/'+s.m.PRIMARY,
        data='Only existing early/recent2026 raw actual-contract minute archives. Signals Jan1-Jun30/recent Jul1-Oct23 subject to per-symbol coverage; exit tails retained. No2025 replay without raw minutes.',
        frozen_chemicals='EG and V retain complete existing B5 rows and warning definitions. Synthetic SR has no ABC A-cluster interpretation; unique signal IDs, common one-symbol reservation and economic/execution guards.',
        JD='Six fixed variants including B5,S1,E20S1 previously inspected controls; three exit hypotheses. S1_N8 changes5 to8 observed15m buckets; H120 changes max60 to120; PATIENT additionally disables no-follow. Same initial stop, pending, cost/risk, hourly adaptive and holiday/weekend guards.',
        FAIL20='Completed15m candle exceeds one boundary of strictly preceding20-candle range by >=1tick, closes strictly back inside; reject two-sided failures. Opposite-direction trigger beyond current candle extreme1tick, stop beyond other extreme1tick, target tick-rounded prior range midpoint. Economic/risk/netRR guards unchanged. Pending4 observed15m buckets, no-follow5, maxhold60.',
        HOUR20='Only full60m candles composed of12 consecutive unique5m bars; close exceeds preceding20 complete hourly highs/lows, excluding current. Trigger candle extreme1tick, stop1ATR14 rounded outward, target2R. Pending4 observed15m buckets, no-follow20, maxhold240. Windows stay within same confirmed actual member; incomplete session hours excluded.',
        common='Frozen one-lot actual-contract execution; corrected touch and next-minute-open,1/2tick per-side; cost snapshot20261022, JD multiplier10, SR roundtrip2. No outcome-based signal selection. Unknown original/two_paths_flat; never impute unknown PnL. Same symbol reservations continue across months/folds.',
        risk='Primary comparison300yuan per opportunity PLANNED risk using fractional research quantity; q300/(planned_risk*mult). Also report one-lot cash, closed riskR, unknown and realized settlement drawdown. Not executable fractional orders/account equity; no margin allocation.',
        selection='Per product expanding train, initialQ1 B5. At each Q2/Q3/October cutoff use only closed exits strictly before cutoff from continuously replayed shadow portfolios. Need>=8train closed, positive worst8case normalized net, primary ex-best positive, normalized net>=B5 in all8cases, no extra unresolved reservations at cutoff. Maximize worst8case normalized_net-.25*max8case normalizedDD, require score>B5, declared order tie. Fallback B5. Warning-time choice fixed through fold; recompetition whole stream without resets.',
        acceptance='Report all failures and fixed candidates, then schedule tests. A static profitable curve is not sufficient; require cross-block, execution and concentration support. Half-EG is an evaluation reference, never search objective.',
        interpretation='History and products already repeatedly studied; chronological replay is not untouched OOS. S1/E20S1 selection contaminated by earlier research. Eight cases are sensitivities, not independent samples. Do not infer structural cause from finite variants; SR20bar settings and horizon are fixed unoptimized hypotheses.',
        production_before=production,script_sha256=s.m.old.sha(__file__),qualified_for_trading=False)
    write('protocol.json',protocol)
    fork=RUN/'fork';shutil.copytree(s.ARCHIVE/'fork',fork)
    old=fork/'crates/n-core/src/bin/swing_specialists.rs'
    src=old.read_text(encoding='utf-8')
    needle='no_follow_bar: Some(template["no_follow_bar"].as_u64().unwrap_or(5) as usize),'
    assert src.count(needle)==1
    src=src.replace(needle,'no_follow_bar: no_follow(template),')
    src+='\nfn no_follow(t: &Value)->Option<usize>{match t.get("no_follow_bar"){Some(Value::Null)=>None,Some(v)=>Some(v.as_u64().expect("no-follow integer") as usize),None=>Some(5)}}\n'
    src+='\n#[cfg(test)] mod hold_tests{use super::*;#[test] fn null_disables_only_explicit_no_follow(){assert_eq!(no_follow(&json!({})),Some(5));assert_eq!(no_follow(&json!({"no_follow_bar":8})),Some(8));assert_eq!(no_follow(&json!({"no_follow_bar":null})),None);}}\n'
    (fork/'crates/n-core/src/bin/swing_product_mechanisms.rs').write_text(src,encoding='utf-8')
    write('implementation-manifest.json',{q.relative_to(fork).as_posix():s.m.old.sha(q) for q in fork.rglob('*') if q.is_file()})
    print('Protocol frozen:6 JD variants incl3 old controls;2 SR mechanisms; EG/PVC fixed.',flush=True)

def build():
    target=ROOT/'target/swing-specialists-isolated'
    args=['--release','--offline','--manifest-path',str(RUN/'fork/Cargo.toml'),'--target-dir',str(target),'-p','n-core','--bin','swing_product_mechanisms']
    subprocess.run(['cargo','test',*args],check=True)
    subprocess.run(['cargo','build',*args],check=True)
    exe=target/'release/swing_product_mechanisms.exe'
    write('binary.json',dict(path=str(exe),sha256=s.m.old.sha(exe)))

def generate():
    p=READ(RUN/'protocol.json');assert p['script_sha256']==s.m.old.sha(__file__)
    pp=READ(ROOT/'research-strategies/best-a-portfolio-observation-v1/protocol.json')
    metadata={};qa=collections.Counter();sources={};coverage={}
    for period in ['early','recent']:
        paths=pp['periods'][period];inp=READ(paths['input']);segments=READ(paths['segments'])
        sources[period]={k:s.m.old.sha(paths[k]) for k in ['input','contracts','segments','fees','holidays']}
        pool=READ(s.NATIVE/(period+'-pool.json'))
        proto=copy.deepcopy(next(z['event'] for z in pool if z['event']['symbol']=='SR0'))
        start,end=('2026-01-01 00:00:00','2026-06-30 15:00:00') if period=='early' else ('2026-07-01 00:00:00','2026-10-23 11:05:00')
        tasks={'JD':[z for z in pool if z['event']['symbol']=='JD0']}
        raw=inp['symbols']['SR0']['bars'];tick=inp['symbols']['SR0']['tick_size']
        for model in SR[1:]:
            bars=identification_bars(raw) if model=='FAIL20' else full_hours(raw)
            labels=collections.Counter(b['datetime'] for b in bars)
            codes=[next((z['underlying'] for z in segments['SR0'] if z['first']<=b['datetime']<=z['last']),None) for b in bars]
            generated=[]
            for i in range(20,len(bars)):
                b=bars[i];ts=b['datetime']
                if not start<=ts<=end:continue
                if labels[ts]!=1 or any(labels[z['datetime']]!=1 for z in bars[i-20:i+1]):qa[model+'_duplicate_clock']+=1;continue
                if codes[i] is None or len(set(codes[i-20:i+1]))!=1:qa[model+'_cross_roll_lookback']+=1;continue
                qa[model+'_eligible_clocks']+=1
                g=signal(bars,i,tick,model)
                if g is None:continue
                assert g==signal(bars[:i+1],i,tick,model)
                direction=g['direction'];key='|'.join([model,'SR0',direction,ts]);e=copy.deepcopy(proto)
                geometry={k:g[k] for k in ['direction','entry','stop','target','risk','rr']}
                e.update(symbol='SR0',warning_ts=ts,s0_ts=bars[i-20]['datetime'],s1_ts=ts,s2_ts=ts,
                    warning_kind='benchmark',tick_size=tick,market_context=None,evaluation=None,outcome=None,
                    entry_ts=None,entry_price=None,trigger_features=None,trigger_bar_ts=None,
                    warning_extreme=b['high'] if direction=='up' else b['low'],trigger_level=g['entry'],**geometry)
                e['setup_features']['setup_quality']=3.;e['setup_features']['direction']=direction
                generated.append(dict(event_key=key,event=e,warning_candle=dict(b,volume=0.,hold=0.),prior=[],
                    atr14=g['atr14'],original_friction=0.,original_risk_atr=g['risk']/g['atr14'] if g['atr14'] else 0.,entry_quality={}))
                metadata[key]=dict(model=model,period=period,contract=codes[i],warning_ts=ts,**g)
                qa[model+'_signals']+=1;qa[model+'_backward_target']+=not g['target_forward']
            tasks[model]=generated
        for task,items in tasks.items():
            dest=RUN/period/task;dest.mkdir(parents=True,exist_ok=False)
            taskpaths=dict(paths,input=str(dest/'input-specs.json'))
            s.m.old.write(dest/'input-specs.json',dict(execution_config=inp['execution_config'],symbols={sym:{k:inp['symbols'][sym][k] for k in ['variety','tick_size']} for sym in s.FOUR}))
            s.m.old.write(dest/(period+'-pool.json'),items)
            templates=[p['templates'][n] for n in (JD if task=='JD' else [task])]
            s.m.old.write(dest/'execution.protocol.json',dict(templates=templates,modes=['corrected_touch','next_minute_open'],periods={period:taskpaths}))
            s.m.old.write(dest/'trading-dates.json',s.TradingDates(inp['symbols']).dates)
            print(period,task,'candidates',len(items),flush=True)
        coverage[period]={sym:dict(first=v['bars'][0]['datetime'],last=v['bars'][-1]['datetime'],bars5=len(v['bars'])) for sym,v in inp['symbols'].items() if sym in s.FOUR}
    write('signal-metadata.json',metadata);write('generation-checks.json',dict(counts=dict(qa),sources=sources,coverage=coverage))

def replay():
    b=READ(RUN/'binary.json');assert s.m.old.sha(b['path'])==b['sha256']
    for period in ['early','recent']:
        for task in ['JD','FAIL20','HOUR20']:
            dest=RUN/period/task
            subprocess.run([b['path'],str(dest/'execution.protocol.json'),period,'replay',str(dest/'executions')],check=True)

def load():
    c,pp,ee,dd,prior,_,_=s.m.load();events=ee['unmerged'];ends=dd['unmerged'];fees=READ(ROOT/'scripts/exchange_fees_20261022.json')
    data=collections.defaultdict(list);controls={};funnel=collections.defaultdict(collections.Counter);sources={}
    for sf in s.m.SUFFIXES:
        rows=[a for a in prior['unmerged']['budget_body',sf] if a['period']!='older' and a['symbol'] in s.FOUR]
        data['B5',sf]=rows
        for a in rows:funnel['B5/'+a['symbol']+'/'+sf][EV(a)['status']]+=1
    for period in ['early','recent']:
        for task in ['JD','FAIL20','HOUR20']:
            dest=RUN/period/task;events.update({z['event_key']:z['event'] for z in READ(dest/(period+'-pool.json'))})
            cal=s.TradingDates({});cal.dates=READ(dest/'trading-dates.json')
            for code,end in READ(dest/'executions/manifest.json')['contract_ends'].items():ends[code]=max(end,ends.get(code,''))
            path=dest/'executions/evaluations.jsonl';sources[str(path)]=s.m.old.sha(path)
            base={a['event_key']:a for a in data['B5',s.m.PRIMARY] if a['period']==period and a['symbol']=='JD0'}
            for line in path.open(encoding='utf-8'):
                a=s.reprice(json.loads(line),fees,cal);a['period']=period;model,sf=a['scenario'].split('/',1)
                if EV(a)['status']=='closed':
                    assert EV(a)['entry_ts']>a['warning_ts']
                    for f in a['result']['fills']:assert abs(f['price']/a['tick_size']-round(f['price']/a['tick_size']))<1e-7
                if model=='B5':
                    old=next(z for z in data['B5',sf] if z['period']==period and z['event_key']==a['event_key'])
                    for k in ['result','entry','stop','target','planned_risk','new_friction','cost_repricing']:assert a.get(k)==old.get(k),(period,sf,k,a['event_key'])
                    controls[(period,sf,a['event_key'])]=True
                else:
                    data[model,sf].append(a);funnel[model+'/'+a['symbol']+'/'+sf][EV(a)['status']]+=1
    return c,pp,events,ends,data,dict(funnel),sources,len(controls)

def unresolved(c,rows,ends,pol,cutoff):
    return sum(bool(z) and z[0]<cutoff<=z[1] for a in rows if a['warning_ts']<cutoff for z in [c.reservation(a,ends,pol)])

def analyze():
    p=READ(RUN/'protocol.json');assert p['script_sha256']==s.m.old.sha(__file__)
    c,pp,events,ends,data,funnel,sources,controls=load()
    accepted={};fixed={};checks=[];cases=[pol+'/'+sf for pol in r.POLICIES for sf in s.m.SUFFIXES]
    base_ref=READ(s.ARCHIVE/'native2026/fixed-results.json')
    for case in cases:
        pol,sf=case.split('/',1)
        for sym,names in [('JD0',JD),('SR0',SR)]:
            for name in names:
                # Chemicals remain B5; only one agricultural product changes in each shadow.
                rows=[a for a in data['B5',sf] if a['symbol']!=sym]+[a for a in data[name,sf] if a['symbol']==sym]
                aa,ll,_=c.select(rows,events,ends,pp['sector_map'],'sector2_counter_stops',pol);s.m.report.audit(ll,rows)
                accepted[case,sym,name]=aa
                product=[a for a in aa if a['symbol']==sym]
                z=metric(product,events);z['blocks']={f['label']:metric([a for a in product if f['start']<=a['warning_ts']<f['end']],events,[mo for mo in MONTHS if f['start'][:7]<=mo<f['end'][:7]]) for f in blocks()}
                z['test']=metric([a for a in product if a['warning_ts']>=FOLDS[0][0]],events,[mo for mo in MONTHS if mo>='2026-04'])
                fixed[case+'/'+sym+'/'+name]=z
                if name=='B5':
                    ref=base_ref['four/'+case+'/B5']['products'][sym]
                    for field,oldfield in [('n','n'),('net','net'),('unknown','unknown')]:assert s.m.equal(z[field],ref[oldfield])
                    checks.append(case+'/'+sym)
                baseline_chem=[a for a in accepted.get((case,sym,'B5'),aa) if a['symbol'] in ['EG0','V0']]
                chem=[a for a in aa if a['symbol'] in ['EG0','V0']]
                assert [(a['event_key'],a['result']) for a in chem]==[(a['event_key'],a['result']) for a in baseline_chem]
    # Decisions read only visible training settlements, never fixed test metrics.
    training=[];schedule=[]
    for start,end in FOLDS:
        choices={};detail={}
        for sym,names in [('JD0',JD),('SR0',SR)]:
            metrics={};pending={}
            for name in names:
                metrics[name]={};pending[name]={}
                for case in cases:
                    pol,_=case.split('/',1);aa=[a for a in accepted[case,sym,name] if a['symbol']==sym]
                    metrics[name][case]=metric(visible(aa,start),events,[mo for mo in MONTHS if mo<start[:7]])
                    pending[name][case]=unresolved(c,aa,ends,pol,start)
            primary=p['primary'];base=metrics['B5'];base_score=min(z['normalized_net'] for z in base.values())-.25*max(z['normalized_realized_dd'] for z in base.values())
            eligible=[];rejections={}
            for name in names[1:]:
                zs=metrics[name];q=zs[primary];why=[]
                if q['n']<8:why.append('train_closed<8')
                if min(z['normalized_net'] for z in zs.values())<=0:why.append('some_stress_nonpositive')
                if q['normalized_ex_best']<=0:why.append('ex_best_nonpositive')
                if any(z['normalized_net']<base[case]['normalized_net']-1e-7 for case,z in zs.items()):why.append('some_stress_below_B5')
                if any(pending[name][case]>pending['B5'][case] for case in cases):why.append('more_unresolved_at_cutoff')
                score=min(z['normalized_net'] for z in zs.values())-.25*max(z['normalized_realized_dd'] for z in zs.values())
                if score<=base_score+1e-7:why.append('score_not_above_B5')
                if why:rejections[name]=why
                else:eligible.append((score,name))
            eligible.sort(key=lambda q:(-q[0],names.index(q[1])))
            choices[sym]=eligible[0][1] if eligible else 'B5'
            detail[sym]=dict(selected=choices[sym],eligible=eligible,rejections=rejections,visible_metrics=metrics,unresolved=pending)
        schedule.append(dict(start=start,end=end,choices=choices));training.append(dict(cutoff=start,products=detail))
    write('selection-before-test.json',dict(schedule=schedule,training=training))
    wf={};ledgers={};wfrows={}
    for case in cases:
        pol,sf=case.split('/',1);rows=[a for a in data['B5',sf] if a['symbol'] in ['EG0','V0']]
        for sym,names in [('JD0',JD),('SR0',SR)]:
            for name in names:
                for a in data[name,sf]:
                    if a['symbol']!=sym:continue
                    chosen=next((f['choices'][sym] for f in schedule if f['start']<=a['warning_ts']<f['end']),'B5')
                    if name==chosen:rows.append(a)
        aa,ll,risk=c.select(rows,events,ends,pp['sector_map'],'sector2_counter_stops',pol);s.m.report.audit(ll,rows)
        scored=[a for a in aa if a['warning_ts']>=FOLDS[0][0]];z=metric(scored,events,[mo for mo in MONTHS if mo>='2026-04'])
        z['products']={sym:metric([a for a in scored if a['symbol']==sym],events,[mo for mo in MONTHS if mo>='2026-04']) for sym in s.FOUR}
        z['blocks']={f['label']:metric([a for a in scored if f['start']<=a['warning_ts']<f['end']],events) for f in blocks()[1:]}
        b=[a for a in accepted[case,'JD0','B5'] if a['warning_ts']>=FOLDS[0][0]]
        z['B5_test']=metric(b,events,[mo for mo in MONTHS if mo>='2026-04'])
        z['risk_full_history']={k:v for k,v in risk.items() if k!='realized_settlements'}
        assert abs(sum(q['net'] for q in z['products'].values())-z['net'])<1e-7
        assert abs(sum(z['month_cash'].values())-z['net'])<1e-7
        wf[case]=z;wfrows[case]=aa
        if case==p['primary']:ledgers['walkforward']=ll
    attr={}
    case=p['primary']
    for sym,names in [('JD0',JD),('SR0',SR)]:
        b={a['event_key']:a for a in accepted[case,sym,'B5'] if a['symbol']==sym and EV(a)['status']=='closed'}
        for name in names[1:]:
            new={a['event_key']:a for a in accepted[case,sym,name] if a['symbol']==sym and EV(a)['status']=='closed'}
            common=b.keys()&new.keys();added=new.keys()-b.keys();removed=b.keys()-new.keys()
            z=dict(common_n=len(common),common_execution_delta=sum(EV(new[k])['net_amount']-EV(b[k])['net_amount'] for k in common),
                added_n=len(added),added_net=sum(EV(new[k])['net_amount'] for k in added),removed_n=len(removed),removed_net=sum(EV(b[k])['net_amount'] for k in removed))
            z['net_delta']=fixed[case+'/'+sym+'/'+name]['net']-fixed[case+'/'+sym+'/B5']['net']
            assert abs(z['net_delta']-z['common_execution_delta']-z['added_net']+z['removed_net'])<1e-7
            attr[sym+'/'+name]=z
    write('fixed-results.json',fixed);write('walkforward-results.json',wf);write('funnel.json',funnel);write('attribution.json',attr)
    write('opportunity-ledgers.json',ledgers)
    with (RUN/'accepted-primary.jsonl').open('x',encoding='utf-8') as f:
        for (case,sym,name),aa in accepted.items():
            if case!=p['primary']:continue
            for a in aa:
                if a['symbol']==sym:f.write(json.dumps(dict(product=sym,model=name,row=a),ensure_ascii=False)+'\n')
        for a in wfrows[p['primary']]:f.write(json.dumps(dict(product=a['symbol'],model='walkforward',row=a),ensure_ascii=False)+'\n')
    diffs=[q for q,h in p['production_before'].items() if s.m.old.sha(ROOT/q)!=h];assert not diffs
    for file,h in READ(RUN/'implementation-manifest.json').items():assert s.m.old.sha(RUN/'fork'/file)==h
    write('verification.json',dict(exact_native_B5_JD_rows=controls,exact_product_baselines=checks,
        source_hashes=sources,causal_prefix_signal_checks=True,chemical_rows_unchanged=True,strict_visible_training=True,
        continuous_reservations=True,production_hash_differences=diffs,native_fork_unchanged=True,
        known_pnl_reconciled=True,no_claim_of_independent_OOS=True))
    print(json.dumps(dict(schedule=schedule,primary={sym:{name:{k:fixed[p['primary']+'/'+sym+'/'+name][k] for k in ['n','net','normalized_net','unknown']} for name in names} for sym,names in [('JD0',JD),('SR0',SR)]},walkforward=wf[p['primary']]['net']),ensure_ascii=False),flush=True)

def blocks():
    return [dict(label='Q1_train',start='2026-01-01 00:00:00',end='2026-04-01 00:00:00'),
            dict(label='Q2',start=FOLDS[0][0],end=FOLDS[0][1]),
            dict(label='Q3',start=FOLDS[1][0],end=FOLDS[1][1]),
            dict(label='October_partial',start=FOLDS[2][0],end=FOLDS[2][1])]

class MechanismTests(unittest.TestCase):
    def bars(self):
        return [dict(datetime=(dt.datetime(2026,1,5,9)+dt.timedelta(minutes=15*i)).isoformat(sep=' '),open=100.,high=110.,low=90.,close=100.) for i in range(22)]
    def test_failure_excludes_two_sides_and_requires_inside_close(self):
        bs=self.bars();bs[20].update(high=112.,low=94.,close=108.)
        a=signal(bs,20,1.,'FAIL20');self.assertEqual(a['direction'],'down');self.assertEqual((a['entry'],a['stop'],a['target']),(93.,113.,100.))
        self.assertFalse(a['target_forward'])
        bs[20].update(low=88.);self.assertIsNone(signal(bs,20,1.,'FAIL20'))
        bs[20].update(low=94.,close=110.);self.assertIsNone(signal(bs,20,1.,'FAIL20'))
    def test_prefix_and_breakout_excludes_current_range(self):
        bs=self.bars();bs[20].update(high=115.,close=114.);bs[21].update(high=9999.,low=1.)
        self.assertEqual(signal(bs,20,1.,'HOUR20'),signal(bs[:21],20,1.,'HOUR20'))
        self.assertEqual(signal(bs,20,1.,'HOUR20')['direction'],'up')
    def test_hour_rejects_partial_and_missing_or_duplicate_5m(self):
        raw=[dict(datetime=(dt.datetime(2026,1,5,9)+dt.timedelta(minutes=5*i)).isoformat(sep=' '),open=100.,high=102.,low=98.,close=101.) for i in range(1,13)]
        self.assertEqual(len(full_hours(raw)),1);self.assertFalse(full_hours(raw[:-1]));self.assertFalse(full_hours(raw[:5]+raw[6:]))
        self.assertFalse(full_hours(raw+[raw[0]]))
    def test_only_strictly_visible_settlements_train(self):
        cutoff='2026-04-01 00:00:00'
        xs=[dict(warning_ts='2026-03-31 15:00:00',result=dict(evaluation=dict(status='closed',exit_ts=t))) for t in ['2026-03-31 23:59:00',cutoff,'2026-04-01 00:01:00']]
        self.assertEqual(len(visible(xs,cutoff)),1)

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('stage',choices=['test','prepare','build','generate','replay','analyze']);args=ap.parse_args()
    if args.stage=='test':unittest.main(argv=[sys.argv[0]])
    else:globals()[args.stage]()
