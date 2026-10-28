"""Broad but finite JD15m mechanism discovery, with warning-time order occupancy."""
import argparse
import collections
import copy
import datetime as dt
import hashlib
import importlib.util
import json
import math
import re
import shutil
import statistics as st
import subprocess
import sys
import unittest

sys.dont_write_bytecode=True
import research_swing_product_mechanisms as x

s=x.s
ROOT=x.ROOT
RUN=ROOT/'research-runs/swing-jd-frequency-20261026-v1'
READ=x.READ
EV=x.EV
MONTHS=x.MONTHS
FOLDS=x.FOLDS
FAMILIES=['ABC_TIMING','BREAK8','PULLBACK','INSIDE','FADE','FAIL20','OR30','GAP','MOM16','COIN']
PACKS=['TREND_PACK','STATE_PACK']
PROFILES=['SPACE','QUICK']
MODELS=[f'{n}/{p}' for p in PROFILES for n in FAMILIES+PACKS]+['B5','E20S1']
CANDIDATES=[n for n in MODELS if n.split('/')[0] not in ['MOM16','COIN','B5','E20S1']]
LABELS={'ABC_TIMING':'ABC时点重做空间','BREAK8':'8柱通道突破','PULLBACK':'均线趋势回撤','INSIDE':'内包收缩突破',
        'FADE':'波动带回归','FAIL20':'失败突破回归','OR30':'开盘30分钟区间','GAP':'跳空回补',
        'MOM16':'16柱动量基准','COIN':'固定随机方向基准','TREND_PACK':'趋势机会合并','STATE_PACK':'趋势/区间状态切换',
        'B5':'原B5','E20S1':'原S1＋成本20%','CASH':'空仓'}
UNKNOWN={'rollover','pending','unfinished','ambiguous','missing_data'}
IMMEDIATE={'filtered','filtered_setup','filtered_geometry','filtered_entry_economics','filtered_A_net_body','filtered_net_rr_plan'}

def write(n,v):s.m.old.write(RUN/n,v)
def grid(v,tick,up):return x.grid(v,tick,up)
def check_source():
    permitted={READ(RUN/'protocol.json')['script_sha256']}|{READ(q)['script_sha256'] for q in RUN.glob('*amendment*.json')}
    assert s.m.old.sha(__file__) in permitted

def features(bars,i):
    assert i>=36
    b=bars[i];past=bars[i-20:i]
    atr=st.mean(max(bars[j]['high']-bars[j]['low'],abs(bars[j]['high']-bars[j-1]['close']),abs(bars[j]['low']-bars[j-1]['close'])) for j in range(i-13,i+1))
    mean=lambda a,z:st.mean(t['close'] for t in bars[a:z])
    m8=mean(i-7,i+1);m32=mean(i-31,i+1)
    prev8=mean(i-8,i);slow4=mean(i-35,i-3)
    moves=[abs(bars[j]['close']-bars[j-1]['close']) for j in range(i-15,i+1)]
    momentum=b['close']-bars[i-16]['close'];er=abs(momentum)/sum(moves) if sum(moves) else 0.
    return dict(atr=atr,ma8=m8,ma32=m32,prev_ma8=prev8,slow4=slow4,
        er16=er,momentum_sign=1 if momentum>0 else -1 if momentum<0 else 0,
        range_high=max(t['high'] for t in past),range_low=min(t['low'] for t in past),
        mean20=st.mean(t['close'] for t in past),std20=st.pstdev(t['close'] for t in past))

def opportunities(bars,i,f,abc):
    b=bars[i];prev=bars[i-1];ts=b['datetime'];out={}
    def put(n,sign,target=None):
        if sign:out[n]=dict(direction='up' if sign>0 else 'down',structural_target=target)
    trend=1 if f['ma8']>f['ma32'] else -1 if f['ma8']<f['ma32'] else 0
    abc_directions=[d for d in ['up','down'] if (ts,d) in abc]
    if len(abc_directions)==1:put('ABC_TIMING',1 if abc_directions[0]=='up' else -1)
    prior=bars[i-8:i]
    put('BREAK8',1 if b['close']>max(t['high'] for t in prior) else -1 if b['close']<min(t['low'] for t in prior) else 0)
    pull=(1 if trend>0 and f['ma32']>f['slow4'] and prev['close']<=f['prev_ma8'] and b['close']>f['ma8'] else
         -1 if trend<0 and f['ma32']<f['slow4'] and prev['close']>=f['prev_ma8'] and b['close']<f['ma8'] else 0)
    put('PULLBACK',pull)
    if b['high']<=prev['high'] and b['low']>=prev['low']:put('INSIDE',trend)
    if f['er16']<.30 and f['std20']>0:
        fade=1 if b['close']<f['mean20']-1.5*f['std20'] and b['close']>b['open'] else -1 if b['close']>f['mean20']+1.5*f['std20'] and b['close']<b['open'] else 0
        put('FADE',fade,f['mean20'])
    top=b['high']>=f['range_high']+1;bottom=b['low']<=f['range_low']-1
    if top!=bottom and f['range_low']<b['close']<f['range_high']:
        put('FAIL20',-1 if top else 1,(f['range_high']+f['range_low'])/2)
    day=ts[:10]
    today=[z for z in bars[:i+1] if z['datetime'][:10]==day]
    opening=[z for z in today if z['datetime'][11:16] in ['09:15','09:30']]
    if len(opening)==2 and '09:45'<=ts[11:16]<='14:30':
        hi=max(z['high'] for z in opening);lo=min(z['low'] for z in opening)
        put('OR30',1 if b['close']>hi and prev['close']<=hi else -1 if b['close']<lo and prev['close']>=lo else 0)
    if ts[11:16]=='09:15' and prev['datetime'][:10]<day and prev['datetime'][11:16]=='15:00':
        gap=b['open']-prev['close']
        if abs(gap)>=f['atr']:
            put('GAP',-1 if gap>0 and b['close']<b['open'] else 1 if gap<0 and b['close']>b['open'] else 0,prev['close'])
    put('MOM16',f['momentum_sign'])
    put('COIN',1 if int(hashlib.sha256(('20261026|'+ts).encode()).hexdigest()[:8],16)%2 else -1)
    return out

def geometry(b,f,op,profile,tick):
    sign=1 if op['direction']=='up' else -1;up=sign>0
    entry=b['close'] if profile=='QUICK' else grid(b['high'],tick,True)+tick if up else grid(b['low'],tick,False)-tick
    cap=.30 if profile=='QUICK' else .20
    atr_scale=1. if profile=='QUICK' else 1.5
    opposite=b['low']-tick if up else b['high']+tick
    risk=grid(max(atr_scale*f['atr'],10*tick,(entry-opposite)*sign),tick,True)
    # JD fee snapshot: both legs .00015 notional. Include planned1tick each side.
    for _ in range(100):
        cost_points=2*tick+.00015*(2*entry-sign*risk)
        required=grid(cost_points/cap,tick,True)
        if required<=risk+1e-9:break
        risk=required
    else:raise AssertionError('fee floor convergence')
    target=entry+sign*(1.8 if profile=='QUICK' else 2.)*risk
    if op['structural_target'] is not None:target=op['structural_target']
    target=grid(target,tick,not up)
    return dict(entry=entry,stop=entry-sign*risk,target=target,risk=risk,
        rr=max(0.,(target-entry)*sign/risk),warning_extreme=b['high'] if up else b['low'],trigger_level=entry)

def choose_pack(ops,f,pack):
    order=['PULLBACK','INSIDE','OR30','BREAK8'] if pack=='TREND_PACK' or f['er16']>=.30 else ['GAP','FAIL20','FADE']
    if pack=='TREND_PACK' or f['er16']>=.30:
        signs={ops[n]['direction'] for n in order if n in ops}
        if len(signs)>1:return None
    return next((n for n in order if n in ops),None)

def order_interval(row,ends,policy,controller):
    """Order admission at warning, including orders later untriggered/rejected."""
    e=EV(row)
    if e['status'] in IMMEDIATE:
        assert e['entry_ts'] is None and e['exit_ts'] is None
        return None
    flat=controller.ambiguity(row) if policy=='two_paths_flat' else None
    end=flat['end'] if flat else ends[row['execution_contract']] if e['status'] in UNKNOWN else e['exit_ts']
    assert end is not None and end>=row['warning_ts'],(e['status'],row['event_key'])
    return row['warning_ts'],end

def select_orders(rows,ends,policy,controller):
    aa=[];ledger=[];until='';checks=0
    for a in sorted(rows,key=lambda z:(z['warning_ts'],z['event_key'])):
        interval=order_interval(a,ends,policy,controller)
        if interval is None:continue
        start,end=interval;take=until<start
        ledger.append(dict(event_key=a['event_key'],warning_ts=start,end=end,accepted=take,
            previous_release=until,reason=None if take else 'working_order_or_position'))
        if take:
            assert not until or until<start
            aa.append(a);until=end;checks+=1
    return aa,ledger,checks

def metric(rows,months=MONTHS):
    z=x.metric(rows,{},months)
    z['orders_admitted']=len(rows)
    z['unfilled_completed_orders']=sum(EV(a)['status'] not in UNKNOWN|{'closed'} for a in rows)
    z['median_holding_minutes']=st.median((dt.datetime.fromisoformat(EV(a)['exit_ts'])-dt.datetime.fromisoformat(EV(a)['entry_ts'])).total_seconds()/60 for a in rows if EV(a)['status']=='closed') if z['n'] else None
    cs=[a for a in rows if EV(a)['status']=='closed']
    cash=peak=dd=0.
    for a in sorted(cs,key=lambda z:(EV(z)['exit_ts'],z['event_key'])):
        cash+=EV(a)['net_amount'];peak=max(peak,cash);dd=max(dd,peak-cash)
    z['cash_realized_dd']=dd
    return z

def prepare():
    RUN.mkdir(exist_ok=False)
    parent=READ(x.RUN/'protocol.json');templates={}
    for profile in PROFILES:
        t=copy.deepcopy(parent['templates']['B5'])
        t.update(name='EXEC',local=False,adaptive=False,body_net_gate=False,no_follow_bar=4 if profile=='QUICK' else 8,
            max_cost_space=.20 if profile=='QUICK' else .10,max_friction=.30 if profile=='QUICK' else .20)
        t['rules'].update(entry_mode='signal_next_open' if profile=='QUICK' else 'touch',
            pending_buckets=2 if profile=='QUICK' else 4,max_holding_buckets=12 if profile=='QUICK' else 32)
        templates[profile]=t
    write('protocol.json',dict(id=RUN.name,created_at=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(),
        symbols=['JD0'],models=MODELS,candidates=CANDIDATES,profiles=templates,months=MONTHS,folds=FOLDS,
        primary='original/'+s.m.PRIMARY,policies=['original','two_paths_flat'],scenarios=s.m.SUFFIXES,
        purpose='Expand15m JD signal sources; target>=60closed in actual saved2026 coverage, not annualization and not a reason to force losing trades.',
        breadth='8 signal sources plus2 predetermined signal packs, each under2 operational profiles; MOM16 and fixed COIN are2 baselines per profile; B5/E20S1 controls.24new fixed systems,20 selectable; no parameter grid or post-result rescue edits.',
        signals=dict(ABC_TIMING='Existing >=2.5ABC warning times, shared unique same-member clock; completely re-plan stop/target, no A net-body gate.',
            BREAK8='Completed close above/below preceding8bar high/low.',
            PULLBACK='SMA8>SMA32 and SMA32>its4bar-lag, previous close<=previousSMA8, current close>SMA8; symmetric short.',
            INSIDE='Completed bar inside previous high/low, direction=sign(SMA8-SMA32).',
            FADE='ER16<.30, current close outside prior20close mean +/-1.5 population std, candle body points toward mean. Target prior20mean.',
            FAIL20='One-sided >=1tick excursion outside prior20range, closes strictly inside. Reverse direction; target range midpoint.',
            OR30='09:15 and09:30 complete bars define opening range; first close crossing its boundary relative to preceding close,09:45-14:30. Further fresh crossings allowed.',
            GAP='09:15 completed reversal body toward previous same-member full day15:00close, opening gap>=ATR14. Target previousclose.',
            MOM16='Every shared clock direction sign(close-close16), a deliberately dense momentum reference.',
            COIN='Every shared clock direction fixed SHA256(20261026|timestamp) coin; one descriptive path, not a statistical null distribution.',
            TREND_PACK='Warning-time priority PULLBACK,INSIDE,OR30,BREAK8; reject clock with contradictory directions; one plan per clock.',
            STATE_PACK='ER16>=.30 uses TREND_PACK, otherwise GAP,FAIL20,FADE in priority order; one plan per clock. No outcome-dependent merging.'),
        common_geometry='SPACE trigger signal bar extreme1tick, QUICK decision at signal close and fill next observed1m OPEN irrespective of direction (stress1 extra minute). Stop >=1.5ATR/1ATR respectively, >=10ticks, outside signal adverse extreme1tick and enough to keep nominal fee+2tick friction <=.20R/.30R. Fixedpoint fee floor uses warning plan only. Targets2R/1.8R, except mean-return mechanisms use their original mean/midpoint/previousclose. Tick-round target conservatively; no target moved outward after rejection.',
        QUICK='Signals no later than14:15; pending2quarter buckets, no-follow4, maxhold12. Each known day block entries14:30, flatten14:55 if actual quote available. Missing predeclared quote =>unknown. Same formal holiday/weekend guards retained; no fictitious daily close fills.',
        SPACE='Pending4quarter buckets, no-follow8, maxhold32; may carry within week, original holiday/weekend guards. Aimed at short swing space, not a daily timeframe signal.',
        occupancy='Each model independently has one JD order/position slot. At warning timestamp reserve until its actual terminal/cancel timestamp; unresolved through actual contract archive end or two-path-flat policy. Immediate warning-known failures skip. Sort warnings causally; never choose by future fill time. No boundary reset, no stacking profits of independent systems.',
        validation='Q1 initial CASH for discovery schedule. Before each Q2/Q3/October boundary use only shadow exits strictly before cutoff; >=20closed, closed/covered_shared_days>=.35, positive normalized net in all8cases, primary ex-best and ex-best-month positive, no already observed unknown errors. Maximize min8 normalized net-.25max8 realizedDD; declared order with1e-8score tie. Fallback CASH, not ex-post best model. Preserve working orders across changes. Cash zero means no signal allocation, not zero unknown PnL.',
        metrics='One lot net plus300yuan warning PLANNED-risk fractional research normalization; trade count, every month/block, fee drag, realized settlementDD, exwinner and unknown. Not account ROI/margin allocation or executable fractional lots. Frequency>=60 is retrospective evaluation of actual covered period, not a payoff optimization target.',
        limitations='Retrospectively selected symbol, already repeatedly studied2026 history,24 simultaneous fixed hypotheses. No untouched OOS or selection-adjusted significance. New2025 rules not replayed without raw minutes. Coverage Jan-Jun thenAug10-Oct23, not complete calendar2026; lookup warmup reset by actual member and duplicate timestamps excluded.',
        production_before={q:s.m.old.sha(ROOT/q) for q in parent['production_before']},script_sha256=s.m.old.sha(__file__),qualified_for_trading=False))
    fork=RUN/'fork';shutil.copytree(x.RUN/'fork',fork)
    lib=fork/'crates/n-core/src/v3/ablation.rs';src=lib.read_text(encoding='utf-8')
    src=s.m.old.replace(src,'    Touch,\n    CloseConfirmNextOpen,','    Touch,\n    SignalNextOpen,\n    CloseConfirmNextOpen,')
    needle='                EntryMode::CloseConfirmNextOpen => {'
    assert src.count(needle)==2
    first=src.index(needle)
    src=src[:first]+'                EntryMode::SignalNextOpen => { self.enter(price, ev, ex, sign, ts); }\n'+src[first:]
    last=src.rfind(needle);src=src[:last]+src[last:].replace(needle,'                EntryMode::CloseConfirmNextOpen | EntryMode::SignalNextOpen => {',1)
    tests='''
    #[test] fn signal_open_enters_adverse_open_without_waiting_for_price_reclaim() {
        for short in [false,true] {
            let sign=if short {-1.}else{1.};let e=event(short,3.);
            let open=100.-sign;
            let result=research(&e,&[bar(46,open,101.,99.,open)],ResearchRules{entry_mode:EntryMode::SignalNextOpen,..Default::default()});
            assert_eq!(result.evaluation.entry_ts.as_deref(),Some("2026-01-05 09:46:00"));
            assert_eq!(result.evaluation.fill,Some(open));
        }
    }
    #[test] fn signal_open_delay_uses_second_minute_open_not_first_intrabar_extreme() {
        let result=research(&event(false,3.),&[bar(46,99.,101.,98.,100.),bar(47,101.,102.,100.,101.)],ResearchRules{entry_mode:EntryMode::SignalNextOpen,next_minute_open:true,..Default::default()});
        assert_eq!(result.evaluation.trigger_ts.as_deref(),Some("2026-01-05 09:46:00"));
        assert_eq!(result.evaluation.entry_ts.as_deref(),Some("2026-01-05 09:47:00"));
        assert_eq!(result.evaluation.fill,Some(101.));
    }
    #[test] fn signal_open_keeps_stop_gap_and_chase_guard() {
        let rules=ResearchRules{entry_mode:EntryMode::SignalNextOpen,..Default::default()};
        let invalid=research(&event(false,3.),&[bar(46,89.,89.,88.,89.)],rules);
        assert_eq!(invalid.evaluation.status,"invalidated");assert!(invalid.evaluation.fill.is_none());
        let chase=research(&event(false,3.),&[bar(46,104.,105.,103.,104.)],rules);
        assert_eq!(chase.evaluation.status,"unfilled");assert!(chase.evaluation.fill.is_none());
    }
'''
    src=s.m.old.replace(src,'mod tests {\n    use super::*;','mod tests {\n    use super::*;\n'+tests)
    lib.write_text(src,encoding='utf-8')
    # Same caller as prior study; new enum behavior is opt-in only.
    caller=(fork/'crates/n-core/src/bin/swing_product_mechanisms.rs').read_text(encoding='utf-8')
    (fork/'crates/n-core/src/bin/swing_jd_frequency.rs').write_text(caller,encoding='utf-8')
    write('implementation-manifest.json',{q.relative_to(fork).as_posix():s.m.old.sha(q) for q in fork.rglob('*') if q.is_file()})
    print('Frozen24 JD15m systems,20 selectable,2 known controls; warning-time single-order admission.',flush=True)

def build():
    args=['--release','--offline','--manifest-path',str(RUN/'fork/Cargo.toml'),'--target-dir',str(ROOT/'target/swing-jd-frequency-isolated'),'-p','n-core']
    log=subprocess.run(['cargo','test',*args,'--lib','v3::ablation::tests'],capture_output=True,text=True)
    (RUN/'new-library-fixture-tests.log').write_text(log.stdout+'\n'+log.stderr,encoding='utf-8')
    old=(RUN/'original-library-fixture-tests.log').read_text(encoding='utf-8')
    failures=lambda text:set(re.findall(r'^test (\S+) \.\.\. FAILED',text,re.M))
    assert failures(log.stdout)==failures(old) and len(failures(old))==25
    assert log.stdout.count('test v3::ablation::tests::signal_open_')==3
    assert all(' ... ok' in line for line in log.stdout.splitlines() if line.startswith('test v3::ablation::tests::signal_open_'))
    write('fixture-test-equivalence.json',dict(original_failures=sorted(failures(old)),new_failures=sorted(failures(log.stdout)),
        original_tests=31,new_tests=34,new_signal_open_tests_passed=3,
        limitation='25 existing sparse5m-fixture tests fail minute-continuity validation in both genuinely compiled versions. They were not repaired or counted as passing.'))
    subprocess.run(['cargo','test',*args,'--bin','swing_jd_frequency'],check=True)
    subprocess.run(['cargo','build',*args,'--bin','swing_jd_frequency'],check=True)
    exe=ROOT/'target/swing-jd-frequency-isolated/release/swing_jd_frequency.exe';write('binary.json',dict(path=str(exe),sha256=s.m.old.sha(exe)))

def generate():
    p=READ(RUN/'protocol.json');check_source()
    pp=READ(ROOT/'research-strategies/best-a-portfolio-observation-v1/protocol.json')
    meta={};sources={};qa=collections.Counter();days=set();coverage={}
    for period in ['early','recent']:
        paths=pp['periods'][period];inp=READ(paths['input']);segments=READ(paths['segments'])['JD0']
        sources[period]={k:s.m.old.sha(paths[k]) for k in ['input','contracts','segments','fees','holidays']}
        pool=READ(s.NATIVE/(period+'-pool.json'));abc={(z['event']['warning_ts'],z['event']['direction']) for z in pool if z['event']['symbol']=='JD0'}
        proto=copy.deepcopy(next(z['event'] for z in pool if z['event']['symbol']=='JD0'))
        bars=x.identification_bars(inp['symbols']['JD0']['bars']);counts=collections.Counter(z['datetime'] for z in bars)
        codes=[next((z['underlying'] for z in segments if z['first']<=b['datetime']<=z['last']),None) for b in bars]
        lo,hi=('2026-01-01 00:00:00','2026-06-30 15:00:00') if period=='early' else ('2026-07-01 00:00:00','2026-10-23 11:05:00')
        generated={pr:[] for pr in PROFILES};eligible=[]
        for i in range(36,len(bars)):
            b=bars[i];ts=b['datetime']
            if not lo<=ts<=hi:continue
            if any(counts[z['datetime']]!=1 for z in bars[i-36:i+1]):qa['excluded_duplicate_window']+=1;continue
            if codes[i] is None or len(set(codes[i-36:i+1]))!=1:qa['excluded_member_window']+=1;continue
            f=features(bars,i)
            if f['atr']<=0:continue
            assert f==features(bars[:i+1],i)
            ops=opportunities(bars,i,f,abc);assert ops==opportunities(bars[:i+1],i,f,abc)
            eligible.append(ts);days.add(ts[:10]);qa['eligible_clocks']+=1
            for profile in PROFILES:
                if profile=='QUICK' and ts[11:16]>'14:15':continue
                routes={pack:choose_pack(ops,f,pack) for pack in PACKS}
                for family,op in ops.items():
                    geometry0=geometry(b,f,op,profile,inp['symbols']['JD0']['tick_size']);key='|'.join([profile,family,'JD0',op['direction'],ts])
                    e=copy.deepcopy(proto);e.update(symbol='JD0',direction=op['direction'],warning_ts=ts,s0_ts=ts,s1_ts=ts,s2_ts=ts,
                        warning_kind='benchmark',market_context=None,evaluation=None,outcome=None,entry_ts=None,entry_price=None,
                        trigger_features=None,trigger_bar_ts=None,**geometry0)
                    e['setup_features']['setup_quality']=3.;e['setup_features']['direction']=op['direction']
                    generated[profile].append(dict(event_key=key,event=e,warning_candle=dict(b,volume=0.,hold=0.),prior=[],
                        atr14=f['atr'],original_friction=0.,original_risk_atr=geometry0['risk']/f['atr'],entry_quality={}))
                    models=[family+'/'+profile]+[pack+'/'+profile for pack,child in routes.items() if child==family]
                    meta[key]=dict(models=models,family=family,profile=profile,warning_ts=ts,contract=codes[i],period=period,
                        structural_target=op['structural_target'],**f,**geometry0)
                    qa[family+'/'+profile]+=1
        for profile,items in generated.items():
            dest=RUN/period/profile;dest.mkdir(parents=True,exist_ok=False)
            s.m.old.write(dest/(period+'-pool.json'),items)
            s.m.old.write(dest/'input-specs.json',dict(execution_config=inp['execution_config'],symbols={'JD0':{k:inp['symbols']['JD0'][k] for k in ['variety','tick_size']}}))
            s.m.old.write(dest/'trading-dates.json',s.TradingDates(inp['symbols']).dates)
            runpaths=dict(paths,input=str(dest/'input-specs.json'))
            if profile=='QUICK':
                h=READ(paths['holidays']);daily=sorted({z['datetime'][:10] for z in inp['symbols']['JD0']['bars']})
                for date in daily:h['windows'].append(dict(name='JD predeclared daily flat '+date,block_from=date+' 14:30:00',flat_at=date+' 14:55:00',last_close=date+' 15:00:00',reopen=date+' 23:59:59'))
                s.m.old.write(dest/'daily-holidays.json',h);runpaths['holidays']=str(dest/'daily-holidays.json')
            s.m.old.write(dest/'execution.protocol.json',dict(templates=[p['profiles'][profile]],modes=['corrected_touch','next_minute_open'],periods={period:runpaths}))
            print(period,profile,len(items),'native candidate plans',flush=True)
        coverage[period]=dict(raw_first=inp['symbols']['JD0']['bars'][0]['datetime'],raw_last=inp['symbols']['JD0']['bars'][-1]['datetime'],shared_clocks=eligible,shared_days=len({t[:10] for t in eligible}))
    write('signal-metadata.json',meta);write('generation-checks.json',dict(counts=dict(qa),sources=sources,coverage=coverage,shared_dates=sorted(days)))

def replay():
    b=READ(RUN/'binary.json');assert s.m.old.sha(b['path'])==b['sha256']
    for period in ['early','recent']:
        for profile in PROFILES:
            dest=RUN/period/profile
            subprocess.run([b['path'],str(dest/'execution.protocol.json'),period,'replay',str(dest/'executions')],check=True)

def load():
    folder=ROOT/'research-strategies/best-a-portfolio-observation-v1'
    spec=importlib.util.spec_from_file_location('jd_frequency_control',folder/'controller.py');c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)
    meta=READ(RUN/'signal-metadata.json');ends={};data=collections.defaultdict(list);fees=READ(ROOT/'scripts/exchange_fees_20261022.json');sources={};funnel=collections.defaultdict(collections.Counter)
    for period in ['early','recent']:
        for profile in PROFILES:
            dest=RUN/period/profile;cal=s.TradingDates({});cal.dates=READ(dest/'trading-dates.json')
            for code,end in READ(dest/'executions/manifest.json')['contract_ends'].items():ends[code]=max(end,ends.get(code,''))
            path=dest/'executions/evaluations.jsonl';sources[str(path)]=s.m.old.sha(path)
            for line in path.open(encoding='utf-8'):
                a=s.reprice(json.loads(line),fees,cal);sf=a['scenario'].split('/',1)[1];a['period']=period
                assert a['execution_contract']==meta[a['event_key']]['contract']
                if EV(a)['status']=='closed':
                    assert EV(a)['entry_ts']>a['warning_ts']
                    for f in a['result']['fills']:assert abs(f['price']-round(f['price']))<1e-7
                    if profile=='QUICK':assert EV(a)['entry_ts'][:10]==EV(a)['exit_ts'][:10]
                for model in meta[a['event_key']]['models']:data[model,sf].append(a);funnel[model+'/'+sf][EV(a)['status']]+=1
        dest=x.RUN/period/'JD';cal=s.TradingDates({});cal.dates=READ(dest/'trading-dates.json')
        for line in (dest/'executions/evaluations.jsonl').open(encoding='utf-8'):
            a=json.loads(line);model,sf=a['scenario'].split('/',1)
            if model not in ['B5','E20S1']:continue
            a=s.reprice(a,fees,cal);a['period']=period;data[model,sf].append(a)
    return c,ends,data,meta,sources,{k:dict(v) for k,v in funnel.items()}

def analyze():
    p=READ(RUN/'protocol.json');check_source()
    c,ends,data,meta,sources,funnel=load();days=READ(RUN/'generation-checks.json')['shared_dates']
    cases=[pol+'/'+sf for pol in p['policies'] for sf in p['scenarios']];fixed={};accepted={};ledgers={};qa=0
    for case in cases:
        pol,sf=case.split('/',1)
        for model in MODELS:
            aa,ll,n=select_orders(data[model,sf],ends,pol,c);qa+=n;accepted[case,model]=aa
            z=metric(aa);z['closed_per_shared_day']=z['n']/len(days)
            z['blocks']={f['label']:metric([a for a in aa if f['start']<=a['warning_ts']<f['end']]) for f in x.blocks()}
            z['test']=metric([a for a in aa if a['warning_ts']>=FOLDS[0][0]],[mo for mo in MONTHS if mo>='2026-04'])
            fixed[case+'/'+model]=z
            if case==p['primary']:ledgers[model]=ll
    selection=[];training=[]
    for start,end in FOLDS:
        ndays=sum(day<start[:10] for day in days);metrics={};eligible=[];reject={}
        for model in CANDIDATES:
            zs={case:metric(x.visible(accepted[case,model],start),[mo for mo in MONTHS if mo<start[:7]]) for case in cases};metrics[model]=zs
            q=zs[p['primary']];why=[]
            if q['n']<20:why.append('train_closed<20')
            if q['n']/ndays<.35:why.append('frequency<.35/shared_day')
            if min(z['normalized_net'] for z in zs.values())<=0:why.append('some_stress_nonpositive')
            if q['normalized_ex_best']<=0:why.append('ex_best_nonpositive')
            if q['normalized_ex_best_month']<=0:why.append('ex_best_month_nonpositive')
            errors=sum(EV(a)['status'] in UNKNOWN and EV(a)['exit_ts'] is not None and EV(a)['exit_ts']<start for case in cases for a in accepted[case,model] if a['warning_ts']<start)
            if errors:why.append('observed_unknown_before_cutoff')
            score=min(z['normalized_net'] for z in zs.values())-.25*max(z['normalized_realized_dd'] for z in zs.values())
            if why:reject[model]=why
            else:eligible.append((score,model))
        eligible.sort(key=lambda z:(-round(z[0],8),CANDIDATES.index(z[1])))
        chosen=eligible[0][1] if eligible else 'CASH'
        selection.append(dict(start=start,end=end,model=chosen));training.append(dict(cutoff=start,shared_days=ndays,selected=chosen,eligible=eligible,rejected=reject,visible_metrics=metrics))
    write('selection-before-test.json',dict(schedule=selection,training=training))
    wf={};wfrows={}
    for case in cases:
        pol,sf=case.split('/',1);rows=[]
        for f in selection:
            if f['model']=='CASH':continue
            rows.extend(a for a in data[f['model'],sf] if f['start']<=a['warning_ts']<f['end'])
        aa,ll,n=select_orders(rows,ends,pol,c);qa+=n
        wf[case]=metric(aa,[mo for mo in MONTHS if mo>='2026-04']);wf[case]['blocks']={f['label']:metric([a for a in aa if f['start']<=a['warning_ts']<f['end']]) for f in x.blocks()[1:]};wfrows[case]=aa
        if case==p['primary']:ledgers['walkforward']=ll
    write('fixed-results.json',fixed);write('walkforward-results.json',wf);write('funnel.json',funnel);write('opportunity-ledgers.json',ledgers)
    with (RUN/'accepted-primary.jsonl').open('x',encoding='utf-8') as f:
        for model in MODELS:
            for a in accepted[p['primary'],model]:f.write(json.dumps(dict(model=model,row=a),ensure_ascii=False)+'\n')
        for a in wfrows[p['primary']]:f.write(json.dumps(dict(model='walkforward',row=a),ensure_ascii=False)+'\n')
    immediate_checks=0
    for a in data['MOM16/QUICK',s.m.PRIMARY]:
        if EV(a)['status'] in IMMEDIATE:assert EV(a)['exit_ts'] is None;immediate_checks+=1
    for q,h in p['production_before'].items():assert s.m.old.sha(ROOT/q)==h
    for q,h in READ(RUN/'implementation-manifest.json').items():assert s.m.old.sha(RUN/'fork'/q)==h
    write('verification.json',dict(single_order_causal_admissions=qa,immediate_plan_failure_checks=immediate_checks,
        source_hashes=sources,strict_visible_training=True,no_fold_resets=True,daily_flat_same_date_closed=True,
        production_changed=False,not_untouched_OOS=True))
    print(json.dumps(dict(schedule=selection,main={m:{k:fixed[p['primary']+'/'+m][k] for k in ['n','net','normalized_net','unknown']} for m in MODELS}),ensure_ascii=False),flush=True)

class MechanismTests(unittest.TestCase):
    def bars(self):
        return [dict(datetime=(dt.datetime(2026,1,5,9)+dt.timedelta(minutes=15*i)).isoformat(sep=' '),open=100.,high=102.,low=98.,close=100.) for i in range(40)]
    def test_indicators_and_signals_do_not_read_future(self):
        bs=self.bars();bs[36].update(close=106.,high=107.)
        a=features(bs,36);ops=opportunities(bs,36,a,set());bs[39].update(close=9999.,high=99999.)
        self.assertEqual(a,features(bs,36));self.assertEqual(ops,opportunities(bs[:37],36,a,set()))
        self.assertEqual(ops['BREAK8']['direction'],'up')
    def test_cost_floor_and_structural_target_not_artificially_moved(self):
        b=dict(open=3500.,close=3500.,high=3503.,low=3497.);f=dict(atr=2.)
        for pr in PROFILES:
            g=geometry(b,f,dict(direction='up',structural_target=3505.),pr,1.)
            self.assertEqual(g['target'],3505.);self.assertGreaterEqual(g['risk'],10.)
            self.assertLessEqual((2+.00015*(2*g['entry']-g['risk']))/g['risk'],.30 if pr=='QUICK' else .20)
    def test_order_admission_starts_before_trigger_and_waits_after_equal_release(self):
        c=type('Controller',(),{'ambiguity':lambda self,z:None})()
        def row(k,w,end,trigger):return dict(event_key=k,warning_ts=w,execution_contract='JD',result=dict(evaluation=dict(status='closed',exit_ts=end,entry_ts=trigger,trigger_ts=trigger)))
        a=row('a','2026-01-05 09:15:00','2026-01-05 10:00:00','2026-01-05 09:45:00')
        b=row('b','2026-01-05 09:30:00','2026-01-05 09:40:00','2026-01-05 09:31:00')
        d=row('d','2026-01-05 10:00:00','2026-01-05 10:15:00','2026-01-05 10:01:00')
        e=row('e','2026-01-05 10:15:00','2026-01-05 10:30:00','2026-01-05 10:16:00')
        aa,_,_=select_orders([a,b,d,e],{},'original',c);self.assertEqual([z['event_key'] for z in aa],['a','e'])
    def test_unfilled_order_still_blocks_later_warning(self):
        c=type('Controller',(),{'ambiguity':lambda self,z:None})()
        def row(k,w,end):return dict(event_key=k,warning_ts=w,execution_contract='JD',result=dict(evaluation=dict(status='untriggered',entry_ts=None,exit_ts=end)))
        aa,_,_=select_orders([row('a','2026-01-05 09:15:00','2026-01-05 10:15:00'),row('b','2026-01-05 09:30:00','2026-01-05 09:45:00')],{},'original',c)
        self.assertEqual(len(aa),1)
    def test_signal_pack_never_chooses_conflicting_trend_directions(self):
        ops={'PULLBACK':dict(direction='up'),'BREAK8':dict(direction='down')}
        self.assertIsNone(choose_pack(ops,dict(er16=.5),'TREND_PACK'))
        self.assertIsNone(choose_pack(ops,dict(er16=.5),'STATE_PACK'))

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('stage',choices=['test','prepare','build','generate','replay','analyze']);a=ap.parse_args()
    if a.stage=='test':unittest.main(argv=[sys.argv[0]])
    else:globals()[a.stage]()
