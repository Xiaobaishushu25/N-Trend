"""Frozen ABC incremental-edge benchmark, common adapter and matched timing null.

No production edits, new data download, parameter search or live orders.
"""
import argparse
import bisect
import collections
import copy
import datetime as dt
import json
import math
import pathlib
import random
import statistics as st
import subprocess
import sys
import unittest

sys.dont_write_bytecode=True
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parent))
import research_swing_specialists as s
from report_swing_wave_selection import identification_bars

ROOT=s.ROOT
RUN=ROOT/'research-runs/swing-incremental-edge-20261026-v1'
READ=s.m.old.read
EV=s.m.report.s.ev
SYMS=s.FOUR
MULT={'EG0':10.,'V0':5.,'JD0':10.,'SR0':10.}
MONTHS=s.m.report.OLD_MONTHS
PRIMARY=s.m.PRIMARY
POLICIES=['original','two_paths_flat']
MODELS=['ABC_common','MOM16','BREAK20']
RISK_YUAN=300.
RANDOM_N=199
FOLDS=s.FOLDS[2:]

def write(name,obj):s.m.old.write(RUN/name,obj)

def group(meta):
    risk_bin=int(math.floor(math.log2(max(meta['planned_risk'],1e-12))))
    return '|'.join([meta['symbol'],meta['warning_ts'][:7],meta['direction'],meta['warning_ts'][11:16],str(meta['momentum_sign']),str(risk_bin)])

def geometry(bar,atr,tick,direction):
    sign=1 if direction=='up' else -1
    entry=(math.ceil(bar['high']/tick-1e-9)+1)*tick if sign>0 else (math.floor(bar['low']/tick+1e-9)-1)*tick
    risk=math.ceil(atr/tick-1e-9)*tick
    return dict(entry=entry,stop=entry-sign*risk,target=entry+sign*2*risk,risk=risk,rr=2.,
                warning_extreme=bar['high'] if sign>0 else bar['low'],trigger_level=entry)

def features(bars,i):
    assert i>=20
    prior=bars[i-20:i];b=bars[i]
    tr=[max(bars[j]['high']-bars[j]['low'],abs(bars[j]['high']-bars[j-1]['close']),abs(bars[j]['low']-bars[j-1]['close'])) for j in range(i-13,i+1)]
    atr=sum(tr)/14
    movement=b['close']-bars[i-16]['close']
    total=sum(abs(bars[j]['close']-bars[j-1]['close']) for j in range(i-15,i+1))
    er=abs(movement)/total if total else 0.
    sign=1 if movement>0 else -1 if movement<0 else 0
    breakout=1 if b['close']>max(z['high'] for z in prior) else -1 if b['close']<min(z['low'] for z in prior) else 0
    regime=('up_trend' if sign>0 else 'down_trend') if er>=.30 and sign else 'range'
    return dict(atr14=atr,momentum_sign=sign,breakout_sign=breakout,er16=er,regime=regime)

def prepare():
    RUN.mkdir(exist_ok=False)
    parent=READ(s.ARCHIVE/'protocol.json')
    native=READ(s.ARCHIVE/'binary.json')
    assert s.m.old.sha(native['path'])==native['sha256']
    t=copy.deepcopy(parent['templates'][0])
    t.update(name='COMMON',local=False,adaptive=False,body_net_gate=False,no_follow_bar=5)
    write('protocol.json',dict(id=RUN.name,created_at=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(),
        symbols=SYMS,periods=['early','recent'],months=MONTHS,folds=FOLDS,
        primary='original/'+PRIMARY,policies=POLICIES,scenarios=s.m.SUFFIXES,
        common_template=t,native=native,script_sha256=s.m.old.sha(__file__),
        causal_models=dict(ABC_common='Existing unmerged >=2.5 score warnings and A-net-body>0, mapped to shared clock; no B5 cost/geometry/holding outcome used to choose warning times.',
                           MOM16='At every eligible completed15m candle, direction=sign(close-close16bars ago), zero means no signal.',
                           BREAK20='At each eligible completed15m candle, long if close>max high of preceding20 candles, short if close<min low; otherwise no signal.'),
        common_adapter='Signal candle extreme +/-1tick trigger,1ATR14 initial stop rounded outward to tick,2 plannedR absolute target; fixed5-bucket no-follow,60-bucket max hold,12-bucket pending, original missing/session/holiday/weekend/chase/netRR/actual-risk guards. Touch and additional-minute-delay modes,1/2tick per-side. No hourly adaptive special case, local target, industry or A-cluster stop gate in COMMON. Original B5 retained separately.',
        clock='Unique completed identification15m candle; >=21 candles in same confirmed actual-member segment; no cross-roll lookback; score windows Jan1-Jun30 and Jul1-Oct23 11:05 subject to actual symbol coverage. All COMMON signals share the same clock and inputs.',
        plan_cost='Same10tick minimum risk, nominal friction<=.2R, nominal cost/target<=10%, plan/fill netRR>=1. Planning cost fixed1tick in every execution stress.',
        risk=dict(primary_measure='300yuan per opportunity PLANNED initial risk, fractional-lot research normalization only; q=300/(planned_risk*multiplier) fixed from warning plan, applied to each accepted one-lot net. No claim of deployable fractional contracts or account return.',
                  original_single_lot_cash_also_reported=True,per_symbol_reservation_limit=1,maximum_common_planned_reserved_risk=1200,unknown_pnl_not_zero=True),
        random_null=dict(repetitions=RANDOM_N,seed_base=2026102600,
             strata=['symbol','warning_calendar_month','direction','HH:MM','past16bar_momentum_sign'],
             count='Match all eligible ABC_common warning counts per stratum, not accepted/profitable trades. Sample without replacement from all shared-clock opportunities. Include actual ABC times in candidate population.',
             interpretation='Retrospective conditional timing-null diagnostic; monthly counts and direction distribution condition on observed ABC warnings. NOT a deployable causal random strategy, untouched OOS or calibrated p-value. Retains ABC directional/context information; only tests extra precise timing. Recompetition can alter actual fills/exposure/unknowns.'),
        walkforward='Freeze MOM16 vs BREAK20 using only shadow CLOSED exits strictly before each quarter cutoff. Score=min8case normalized_net-.25*max8case normalized_realizedDD, stable MOM16 tie. Choose at WARNING time, reselect entire stream without resetting reservations. Initial Q1 routing=MOM16, score only Q2/Q3/October. ABC parameters unchanged; no best-in-full-sample comparator selection.',
        significance='Report paired time-block comparisons, all scenarios, per-product/regime, matched-null percentiles, known/unknown exposure. Existing history/products repeatedly researched; no claim of independent strategy discovery or selection-adjusted significance.',
        production_before=parent['production_before'],production_changed=False,qualified_for_trading=False))
    print('Frozen: original B5,3 common-adapter strategies,199 matched timing-null samples; no parameter grid.',flush=True)

def generate():
    p=READ(RUN/'protocol.json')
    assert s.m.old.sha(__file__)==p['script_sha256']
    pp=READ(ROOT/'research-strategies/best-a-portfolio-observation-v1/protocol.json')
    meta={};qa=collections.Counter();sources={}
    for period in ['early','recent']:
        dest=RUN/period;dest.mkdir(exist_ok=False)
        paths=pp['periods'][period];inp=READ(paths['input']);segments=READ(paths['segments'])
        sources[period]={k:s.m.old.sha(v) for k,v in paths.items() if k in ['input','contracts','segments','fees','holidays']}
        pool=READ(s.NATIVE/(period+'-pool.json'))
        abc={(z['event']['symbol'],z['event']['warning_ts'],z['event']['direction']):z for z in pool if z['event']['symbol'] in SYMS and z['entry_quality']['a_body_net_ex_s0']>0}
        assert len(abc)==sum(z['event']['symbol'] in SYMS and z['entry_quality']['a_body_net_ex_s0']>0 for z in pool)
        proto=copy.deepcopy(next(z['event'] for z in pool if z['event']['symbol'] in SYMS))
        synth=[];present=set()
        start,end=('2026-01-01 00:00:00','2026-06-30 15:00:00') if period=='early' else ('2026-07-01 00:00:00','2026-10-23 11:05:00')
        for sym in SYMS:
            bars=identification_bars(inp['symbols'][sym]['bars']);labels=collections.Counter(b['datetime'] for b in bars)
            tick=inp['symbols'][sym]['tick_size'];ss=segments[sym]
            codes=[next((g['underlying'] for g in ss if g['first']<=b['datetime']<=g['last']),None) for b in bars]
            for i in range(20,len(bars)):
                b=bars[i];ts=b['datetime']
                if not start<=ts<=end:continue
                if labels[ts]!=1:qa['excluded_duplicate_label_clock']+=1;continue
                if len(set(codes[i-20:i+1]))!=1 or codes[i] is None:qa['excluded_cross_roll_clock']+=1;continue
                f=features(bars,i)
                if f['atr14']<=0:continue
                for direction in ['up','down']:
                    key='|'.join(['CLOCK',sym,direction,ts]);sign=1 if direction=='up' else -1
                    e=copy.deepcopy(proto);g=geometry(b,f['atr14'],tick,direction)
                    e.update(symbol=sym,direction=direction,warning_ts=ts,s0_ts=ts,s1_ts=ts,s2_ts=ts,
                             warning_kind='benchmark',tick_size=tick,market_context=None,evaluation=None,outcome=None,
                             entry_ts=None,entry_price=None,trigger_features=None,trigger_bar_ts=None,**g)
                    e['setup_features']['setup_quality']=3.
                    e['setup_features']['direction']=direction
                    warning=dict(b,volume=0.,hold=0.)
                    isabc=(sym,ts,direction) in abc
                    m=dict(symbol=sym,warning_ts=ts,direction=direction,period=period,contract=codes[i],
                           planned_risk=g['risk'],**f,ABC_common=isabc,MOM16=f['momentum_sign']==sign,BREAK20=f['breakout_sign']==sign)
                    m['stratum']=group(m);meta[key]=m
                    synth.append(dict(event_key=key,event=e,warning_candle=warning,prior=[],atr14=f['atr14'],
                                      original_friction=0.,original_risk_atr=1.,entry_quality={}))
                    if isabc:present.add((sym,ts,direction))
                    qa['universal_candidates']+=1
                qa['shared_clocks']+=1
        qa[period+'_ABC_matched']=len(present);qa[period+'_ABC_excluded_clock']=len(abc)-len(present)
        s.m.old.write(dest/(period+'-pool.json'),synth)
        specs=dict(execution_config=inp['execution_config'],symbols={sym:{k:inp['symbols'][sym][k] for k in ['variety','tick_size']} for sym in SYMS})
        s.m.old.write(dest/'input-specs.json',specs)
        dates=s.TradingDates(inp['symbols']).dates;s.m.old.write(dest/'trading-dates.json',dates)
        runpaths=dict(paths,input=str(dest/'input-specs.json'))
        s.m.old.write(dest/'execution.protocol.json',dict(templates=[p['common_template']],modes=['corrected_touch','next_minute_open'],periods={period:runpaths}))
        print(period,'shared directional opportunities',len(synth),'ABC warnings',len(present),'excluded',len(abc)-len(present),flush=True)
    write('clock-metadata.json',meta);write('generation-checks.json',dict(counts=dict(qa),source_hashes=sources))

def replay():
    p=READ(RUN/'protocol.json');exe=p['native']['path']
    assert s.m.old.sha(exe)==p['native']['sha256']
    for period in ['early','recent']:
        dest=RUN/period
        subprocess.run([exe,str(dest/'execution.protocol.json'),period,'replay',str(dest/'executions')],check=True)

def metric(rows,meta,months=MONTHS):
    cs=[r for r in rows if EV(r)['status']=='closed'];es=[EV(r) for r in cs]
    nominal=[EV(r)['net_amount']/(r['planned_risk']*r['multiplier']) for r in cs]
    settlements=sorted([(EV(r)['exit_ts'],r['warning_ts'],r['event_key'],EV(r)['net_amount']*RISK_YUAN/(r['planned_risk']*r['multiplier'])) for r in cs])
    cash=peak=dd=0.
    for _,_,_,v in settlements:cash+=v;peak=max(peak,cash);dd=max(dd,peak-cash)
    bins={mo:0. for mo in months};prod=collections.defaultdict(float);regs=collections.defaultdict(list);hours=0.
    for r,n in zip(cs,nominal):
        if r['warning_ts'][:7] in bins:bins[r['warning_ts'][:7]]+=RISK_YUAN*n
        prod[r['symbol']]+=RISK_YUAN*n
        if r['event_key'] in meta:regs[meta[r['event_key']]['regime']].append(n)
        hours+=(dt.datetime.fromisoformat(EV(r)['exit_ts'])-dt.datetime.fromisoformat(EV(r)['entry_ts'])).total_seconds()/3600
    pos=[max(0,v) for v in prod.values()];den=sum(pos)
    return dict(n=len(cs),net=sum(e['net_amount'] for e in es),sum_net_r=sum(e['net_r'] for e in es),mean_net_r=st.mean(e['net_r'] for e in es) if es else None,
                normalized_net=RISK_YUAN*sum(nominal),normalized_mean_per_trade=RISK_YUAN*st.mean(nominal) if nominal else None,
                normalized_realized_dd=dd,mean_nominal_net_r=st.mean(nominal) if nominal else None,
                unknown=sum(EV(r)['status'] in s.m.UNKNOWN for r in rows),accepted_reservations=len(rows),
                wins=sum(e['net_amount']>0 for e in es),commission=sum(r['cost_repricing']['commission'] for r in cs),
                normalized_commission=sum(r['cost_repricing']['commission']*RISK_YUAN/(r['planned_risk']*r['multiplier']) for r in cs),
                mean_planned_cash_risk=st.mean(r['planned_risk']*r['multiplier'] for r in cs) if cs else None,
                mean_actual_to_planned_risk=st.mean(EV(r)['risk']/r['planned_risk'] for r in cs) if cs else None,
                closed_elapsed_holding_hours=hours,warning_month_normalized_net=bins,product_normalized_net=dict(prod),
                positive_product_hhi=sum((v/den)**2 for v in pos) if den else None,
                regimes={k:dict(n=len(v),normalized_net=RISK_YUAN*sum(v),mean_nominal_r=st.mean(v)) for k,v in regs.items()},
                normalized_ex_best=RISK_YUAN*(sum(nominal)-max([0]+nominal)),
                normalized_ex_best_month=RISK_YUAN*sum(nominal)-max([0]+list(bins.values())))

def load():
    p=READ(RUN/'protocol.json');meta=READ(RUN/'clock-metadata.json')
    for z in meta.values():z['stratum']=group(z)
    native_events={};ends={};data=collections.defaultdict(list);sources={}
    fees=READ(ROOT/'scripts/exchange_fees_20261022.json')
    for period in ['early','recent']:
        dest=RUN/period
        native_events.update({z['event_key']:z['event'] for z in READ(dest/(period+'-pool.json'))})
        for c,end in READ(dest/'executions/manifest.json')['contract_ends'].items():ends[c]=max(end,ends.get(c,''))
        cal=s.TradingDates({});cal.dates=READ(dest/'trading-dates.json')
        path=dest/'executions/evaluations.jsonl';sources[str(path)]=s.m.old.sha(path)
        for line in path.open(encoding='utf-8'):
            r=s.reprice(json.loads(line),fees,cal);_,sf=r['scenario'].split('/',1)
            assert r['execution_contract']==meta[r['event_key']]['contract']
            if EV(r)['status']=='closed':
                assert EV(r)['entry_ts']>r['warning_ts']
                for f in r['result']['fills']:assert abs(f['price']/r['tick_size']-round(f['price']/r['tick_size']))<1e-6
            data[sf].append(r)
    c,pp,events,e0,raw,_,_=s.m.load()
    original={sf:[r for r in raw['unmerged']['budget_body',sf] if r['period']!='older' and r['symbol'] in SYMS] for sf in s.m.SUFFIXES}
    for code,end in e0['unmerged'].items():ends[code]=max(end,ends.get(code,''))
    native_events.update(events['unmerged'])
    return c,pp,native_events,ends,data,original,meta,sources

def sample_null(meta):
    populations=collections.defaultdict(list);counts=collections.Counter()
    for key,z in meta.items():
        populations[z['stratum']].append(key)
        if z['ABC_common']:counts[z['stratum']]+=1
    for v in populations.values():v.sort()
    out=[]
    for rep in range(RANDOM_N):
        rng=random.Random(2026102600+rep);keys=set()
        for group0,n in sorted(counts.items()):keys.update(rng.sample(populations[group0],n))
        assert collections.Counter(meta[k]['stratum'] for k in keys)==counts
        out.append(keys)
    return out

def analyze():
    p=READ(RUN/'protocol.json')
    hashes={p['script_sha256']}|{READ(q)['script_sha256'] for q in RUN.glob('*amendment*.json')}
    assert s.m.old.sha(__file__) in hashes
    c,pp,events,ends,data,original,meta,sources=load()
    samples=sample_null(meta);write('random-sample-hashes.json',[s.m.old.digest(sorted(k)) for k in samples])
    results={};accepted={};ledgers={};controls=[]
    ref=READ(s.ARCHIVE/'native2026/fixed-results.json')
    for pol in POLICIES:
        for sf in s.m.SUFFIXES:
            case=pol+'/'+sf;results[case]={}
            for model in MODELS+['B5_original']:
                rows=original[sf] if model=='B5_original' else [r for r in data[sf] if meta[r['event_key']][model]]
                rule='sector2_counter_stops' if model=='B5_original' else 'base'
                aa,ll,risk=c.select(rows,events,ends,pp['sector_map'],rule,pol)
                assert risk['peak_symbol_reservations']<=len(SYMS)
                s.m.report.audit(ll,rows)
                z=metric(aa,meta);z['products']={sym:metric([r for r in aa if r['symbol']==sym],meta) for sym in SYMS}
                results[case][model]=z;accepted[case,model]=aa
                if pol=='original' and sf==PRIMARY:ledgers[model]=ll
                if model=='B5_original':
                    expect=ref['four/'+case+'/B5']
                    for k in ['n','net','unknown']:assert s.m.equal(z[k],expect[k]),(case,k,z[k],expect[k])
                    controls.append(dict(case=case,exact=True))
            print('Fixed causal portfolios complete:',case,flush=True)
    write('fixed-results.json',results)
    selections=[];training=[]
    for start,end in FOLDS:
        scores={};visible={}
        for model in ['MOM16','BREAK20']:
            cases={}
            for pol in POLICIES:
                for sf in s.m.SUFFIXES:
                    rs=[r for r in accepted[pol+'/'+sf,model] if r['warning_ts']<start and EV(r)['status']=='closed' and EV(r)['exit_ts']<start]
                    cases[pol+'/'+sf]=metric(rs,meta,[mo for mo in MONTHS if mo<start[:7]])
            score=min(z['normalized_net'] for z in cases.values())-.25*max(z['normalized_realized_dd'] for z in cases.values())
            scores[model]=score;visible[model]=cases
        chosen=max(['MOM16','BREAK20'],key=lambda model:scores[model])
        selections.append(dict(start=start,end=end,model=chosen));training.append(dict(start=start,scores=scores,visible_metrics=visible))
    write('walkforward-selection.json',dict(schedule=selections,training=training))
    folds={};test={};null={};candidate={}
    test_start=FOLDS[0][0];test_months=[mo for mo in MONTHS if mo>=test_start[:7]]
    for pol in POLICIES:
        for sf in s.m.SUFFIXES:
            case=pol+'/'+sf;folds[case]={};test[case]={}
            rr=[]
            for r in data[sf]:
                name=next((f['model'] for f in selections if f['start']<=r['warning_ts']<f['end']),'MOM16')
                if meta[r['event_key']][name]:rr.append(r)
            aa,ll,risk=c.select(rr,events,ends,pp['sector_map'],'base',pol);s.m.report.audit(ll,rr)
            assert risk['peak_symbol_reservations']<=len(SYMS)
            accepted[case,'simple_walkforward']=aa
            if pol=='original' and sf==PRIMARY:ledgers['simple_walkforward']=ll
            for model in MODELS+['B5_original','simple_walkforward']:
                rs=[r for r in accepted[case,model] if r['warning_ts']>=test_start]
                z=metric(rs,meta,test_months);z['products']={sym:metric([r for r in rs if r['symbol']==sym],meta,test_months) for sym in SYMS}
                test[case][model]=z;folds[case][model]=[]
                for start,end in FOLDS:
                    group0=[r for r in rs if start<=r['warning_ts']<end]
                    folds[case][model].append(dict(start=start,end=end,metrics=metric(group0,meta,[mo for mo in test_months if start[:7]<=mo<end[:7]])))
            # Candidate-level null is separate from implementable re-competed returns.
            abc_rows=[r for r in data[sf] if meta[r['event_key']]['ABC_common'] and r['warning_ts']>=test_start]
            candidate[case]=dict(ABC=metric(abc_rows,meta,test_months),null=[])
            null[case]=[]
            bykey={r['event_key']:r for r in data[sf]}
            for rep,keys in enumerate(samples):
                rr=[bykey[k] for k in sorted(keys)]
                aa,ll,risk=c.select(rr,events,ends,pp['sector_map'],'base',pol)
                # Full causal interval audit for every random portfolio as well.
                s.m.report.audit(ll,rr)
                rs=[r for r in aa if r['warning_ts']>=test_start]
                z=metric(rs,meta,test_months)
                z['products']={sym:metric([r for r in rs if r['symbol']==sym],meta,test_months) for sym in SYMS}
                null[case].append(z)
                candidate[case]['null'].append(metric([r for r in rr if r['warning_ts']>=test_start],meta,test_months))
            print('199 matched portfolios complete:',case,flush=True)
    write('test-results.json',test);write('fold-results.json',folds);write('matched-null-results.json',null);write('candidate-null-results.json',candidate)
    write('opportunity-ledgers.json',ledgers)
    with (RUN/'accepted-primary.jsonl').open('x',encoding='utf-8') as f:
        for (case,name),rs in accepted.items():
            if case!='original/'+PRIMARY:continue
            for r in rs:f.write(json.dumps(dict(model=name,row=r),ensure_ascii=False)+'\n')
    differences=[q for q,h in p['production_before'].items() if s.m.old.sha(ROOT/q)!=h]
    assert not differences,differences
    write('verification.json',dict(original_B5_controls=controls,source_hashes=sources,
        causal_clocks=True,random_strata_counts_exact=True,all_interval_and_stop_audits=True,
        all_fill_times_and_tick_grids=True,train_exits_strict_before_cutoff=True,production_hash_differences=differences,
        note='ABC_common changes execution adapter and cannot replace original B5. Matched-null not causal implementable OOS. Continuous planned-risk scaling uses hypothetical fractional lots.'))
    print('All causal, matched-null, fold and production checks complete.',flush=True)

class MechanismTests(unittest.TestCase):
    def test_prefix_features_do_not_read_future(self):
        bars=[dict(datetime=f'2026-01-01 {i}',open=100+i,high=102+i,low=99+i,close=101+i) for i in range(25)]
        a=features(bars[:21],20);b=features(bars+[dict(open=1e9,high=1e9,low=0,close=0)],20)
        self.assertEqual(a,b)
        self.assertEqual(a['momentum_sign'],1)
    def test_geometry_is_symmetric_tick_grid_and_two_R(self):
        b=dict(high=111.,low=99.)
        up=geometry(b,10.1,1.,'up');down=geometry(b,10.1,1.,'down')
        self.assertEqual(up['risk'],11.)
        self.assertEqual(up['target']-up['entry'],22.)
        self.assertEqual(down['entry']-down['target'],22.)
        self.assertEqual(up['entry'],112.)
        self.assertEqual(down['entry'],98.)
    def test_breakout_excludes_current_candle(self):
        bars=[dict(open=100,high=102,low=98,close=100) for _ in range(21)]
        bars[-1]=dict(open=100,high=110,low=99,close=105)
        self.assertEqual(features(bars,20)['breakout_sign'],1)
    def test_planned_risk_scaling_is_plan_based(self):
        # Actual fill risk changes do not change a previously planned quantity.
        planned=20.;mult=10.;q=RISK_YUAN/(planned*mult)
        self.assertEqual(q,1.5)
        self.assertEqual(q*100.,150.)

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('stage',choices=['prepare','generate','replay','analyze','test']);a=ap.parse_args()
    if a.stage=='test':unittest.main(argv=[sys.argv[0]])
    else:globals()[a.stage]()
