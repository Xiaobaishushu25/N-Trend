"""Preserve ABC: finite entry/structural-stop experiment, actual-minute replay."""
import argparse
import collections
import copy
import datetime as dt
import importlib.util
import json
import math
import shutil
import statistics as st
import subprocess
import sys
import unittest
sys.dont_write_bytecode=True
import audit_swing_jd_signal_paths as a
from swing_entry_economics import warning_features

z=a.z
ROOT=a.ROOT
RUN=ROOT/'research-runs/swing-jd-abc-execution-20261026-v2'
READ=a.READ
EV=a.EV
ENTRIES=['TOUCH','CLOSE1','RETEST','C15']
STOPS=['LOCAL','BTRUE']
MODELS=[e+'_'+s for e in ENTRIES for s in STOPS]+['B5','E20S1']
CHALLENGERS=MODELS[:-2]
MONTHS=z.MONTHS
FOLDS=z.FOLDS

def write(n,x):z.s.m.old.write(RUN/n,x)
def cluster(row,meta):return meta[row['event_key']]['cluster']

def admit(rows,ends,policy,controller,meta):
    accepted=[];ledger=[];active=None;until='';stops=collections.Counter()
    for row in sorted(rows,key=lambda r:(r['warning_ts'],r['event_key'])):
        ts=row['warning_ts']
        if active is not None and until<ts:
            if EV(active)['status']=='closed' and EV(active)['exit_reason']=='stop':stops[cluster(active,meta)]+=1
            active=None
        interval=z.order_interval(row,ends,policy,controller)
        if interval is None:continue
        start,end=interval;c=cluster(row,meta)
        reason='working_order_or_position' if active is not None else 'two_realized_A_stops' if stops[c]>=2 else None
        ledger.append(dict(event_key=row['event_key'],warning_ts=ts,end=end,accepted=reason is None,reason=reason,prior_realized_stops=stops[c],cluster=c))
        if reason:continue
        assert until<start
        accepted.append(row);active=row;until=end
    return accepted,ledger

def c_confirmation(bars,start,end,seed,b_stop,b_extreme):
    """First1-wing confirmed pivot after a push, known only at current close."""
    sign=1 if seed['direction']=='up' else -1
    for j in range(start+1,min(start+13,end+1)):
        b=bars[j]
        if ((b['low'] if sign>0 else b['high'])-b_stop)*sign<=0:return None,'B_invalidated'
        if ((b['high'] if sign>0 else b['low'])-seed['target'])*sign>=0:return None,'S1_already_reached'
        if j<start+3:continue
        left,pivot,right=bars[j-2:j+1]
        if sign>0:
            shape=pivot['low']<=left['low'] and pivot['low']<right['low'] and pivot['low']>b_extreme
            push=max(t['high'] for t in bars[start+1:j-1])>=seed['entry']
        else:
            shape=pivot['high']>=left['high'] and pivot['high']>right['high'] and pivot['high']<b_extreme
            push=min(t['low'] for t in bars[start+1:j-1])<=seed['entry']
        if shape and push:return j,'confirmed'
    return None,'no_C_within12'

def patch_peak_equivalence(fork):
    path=fork/'crates/n-core/src/v3/ablation.rs';src=path.read_text(encoding='utf-8')
    src=z.s.m.old.replace(src,'pub struct ResearchRules {','pub struct ResearchRules {\n    pub allow_peak_only_agreement: bool,')
    helper='''
thread_local!{static PEAK_ONLY_MERGES:std::cell::Cell<usize>=const{std::cell::Cell::new(0)};}
pub fn research_peak_only_merge_count()->usize{PEAK_ONLY_MERGES.with(|v|v.get())}
fn merge_peak_only(paths:&mut[State;2],ex:Experiment,r:ResearchRules)->bool{
    if !r.allow_peak_only_agreement || ex.policy!=Policy::OriginalS1 || ex.no_follow_bar.is_some()
        || r.fast_fail_bucket.is_some() || r.protect_after_r.is_some() || r.protect_lock_r.is_some() || r.trail_distance_r.is_some()
        || paths[0].e.status!="open" || paths[1].e.status!="open" {return false;}
    let mut probe=paths[1].clone();probe.peak=paths[0].peak;
    if !agree(&paths[0],&probe) || paths[0].e.risk!=paths[1].e.risk || paths[0].target!=paths[1].target
        || paths[0].pending_body_exit!=paths[1].pending_body_exit || paths[0].pending_session_exit!=paths[1].pending_session_exit {return false;}
    let peak=paths[0].peak.min(paths[1].peak);let mfe=paths[0].e.mfe_r.min(paths[1].e.mfe_r);let mae=paths[0].e.mae_r.min(paths[1].e.mae_r);
    for p in paths.iter_mut(){p.peak=peak;p.e.mfe_r=mfe;p.e.mae_r=mae;}
    PEAK_ONLY_MERGES.with(|v|v.set(v.get()+1));true
}
'''
    src=z.s.m.old.replace(src,'pub fn evaluate(\n',helper+'\npub fn evaluate(\n')
    src=z.s.m.old.replace(src,'        if !agree(&paths[0], &paths[1]) {','        if !agree(&paths[0], &paths[1]) && !merge_peak_only(&mut paths,ex,rules) {')
    tests='''
    fn peak_only_case(allow:bool,no_follow:Option<usize>)->ResultRow{
        evaluate_research(&event(false,3.),&[bar(46,100.,101.,99.,100.),bar(47,102.,105.,97.,100.),bar(48,100.,131.,99.,130.)],&[],&ExecutionConfig::default(),
        Experiment{policy:Policy::OriginalS1,no_follow_bar:no_follow,slip_ticks:0.,fee_ticks:0.},10.,None,
        ResearchRules{entry_mode:EntryMode::BreakoutRetestQuarter,allow_peak_only_agreement:allow,..Default::default()})
    }
    #[test]fn peak_only_opt_in_continues_identical_execution_without_peak_rules(){
        assert_eq!(peak_only_case(false,None).evaluation.status,"ambiguous");
        let fixed=peak_only_case(true,None);assert_eq!(fixed.evaluation.status,"closed");assert_eq!(fixed.evaluation.exit_reason.as_deref(),Some("target_s1"));assert_eq!(fixed.evaluation.fill,Some(98.));
    }
    #[test]fn peak_only_merge_forbidden_if_no_follow_depends_on_peak(){assert_eq!(peak_only_case(true,Some(5)).evaluation.status,"ambiguous");}
    #[test]fn peak_only_real_trade_disagreement_remains_unknown(){
        let r=research(&event(false,3.),&[bar(46,99.,132.,89.,100.)],ResearchRules{allow_peak_only_agreement:true,..Default::default()});
        assert_eq!(r.evaluation.status,"ambiguous");assert!(r.disagreement.is_some());
    }
'''
    src=z.s.m.old.replace(src,'mod tests {\n    use super::*;','mod tests {\n    use super::*;\n'+tests)
    path.write_text(src,encoding='utf-8')
    runner=fork/'crates/n-core/src/bin/swing_jd_abc_execution.rs';src=runner.read_text(encoding='utf-8')
    src=z.s.m.old.replace(src,'                    let mut result = if eligible','                    let merges_before=ablation::research_peak_only_merge_count();\n                    let mut result = if eligible')
    src=z.s.m.old.replace(src,'"result":result})','"result":result,"peak_only_merges":ablation::research_peak_only_merge_count()-merges_before})')
    runner.write_text(src,encoding='utf-8')

def prepare():
    RUN.mkdir(exist_ok=False)
    parent=READ(z.x.RUN/'protocol.json');base=parent['templates']['E20S1'];templates={}
    for name in MODELS:
        if name in ['B5','E20S1']:templates[name]=copy.deepcopy(parent['templates'][name]);continue
        entry,stop=name.split('_');t=copy.deepcopy(base)
        t.update(name=name,adaptive=False,local=False,no_follow_bar=None,b_extreme=stop=='BTRUE')
        t['rules'].update(entry_mode={'TOUCH':'touch','CLOSE1':'close_confirm_next_open','RETEST':'breakout_retest_quarter','C15':'touch'}[entry],max_holding_buckets=60)
        t['rules']['allow_peak_only_agreement']=True
        templates[name]=t
    proto=dict(id=RUN.name,created_at=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(),
        models=MODELS,challengers=CHALLENGERS,templates=templates,primary='original/'+z.s.m.PRIMARY,policies=['original','two_paths_flat'],scenarios=z.s.m.SUFFIXES,
        purpose='Preserve all ABC detector/pivot/grade definitions. Separate entry and original-local vs entire-B stop anchor; seek higher-frequency robust JD without abandoning ABC.',
        shared='Score>=2.5,A-net-body>0; originalS1 target,nominal friction<=.20R,cost/space<=20%,plan/fill netRR>=1,min10tick actual/planrisk. New8variants have explicit entry modes,no hourly adaptive override,no no-follow; original60observed15m maxhold and holiday/weekend guards remain. Both old controls retain complete definitions. Do not attribute shared-family vs old-control differences to stop alone.',
        stops='LOCAL=unchanged original ABC trade stop. BTRUE=entire B extreme fromS1+1 throughoriginalwarning, preserve original S2-to-stop buffer, known before warning. No stop widened after losing.',
        entries=dict(TOUCH='Original trigger touch.',CLOSE1='A completed1m close beyond original trigger, next minute OPEN.',RETEST='Original trigger must first break in an earlier minute, then market-on-retest at entry-.25geometryR rounded toward breakout. Pre-entry stop/S1/missing/chase guards retained. Nominal expected retest risk and costs recomputed; min actual risk checked at fill.',
            C15='After ABC seed, within12completed15m bars, first1-wing confirmed higher-low/lower-high strictly within entireB; prior post-seed bar must have pushed past original trigger. EntireB invalidation or S1 reached before confirmation cancels seed. Three completed post-seed bars minimum. New signal at right-confirming bar close, trigger its extreme1tick. Frozen parentS1 and stop anchors. Duplicate direction/Acluster/confirmation clock keeps earliest seed.'),
        planning='q=300/(((warning-known expected executable entry-stop)*direction_sign)*10yuan_per_point), fractional research normalization only. Retest level computed with stop-specific geometry risk. Actual fill may differ; report actual/planned risk. No claim of account ROI/margin/fractional execution.',
        occupancy='One JD order/position, from actual decision warning time until terminal/cancel. C15 seeds are not orders; order starts only at confirmed C close. Strict previousend<nextwarning; same parentA cluster blocks after2original stops settled before nextwarning. No fold reset, no sum of independent models.',
        data='Existing2026 Jan-Jun andAug10-Oct23, plus ABC seeds in archiveJuly1-10; same actual-member minutes, no2025 forward-price invention. Early C confirmations may crossJune/July boundary but must be <=July10; recent <=Oct23 11:05. Exact original qualified seeds and control executions verified.',
        validation='Expanding Q1->Q2/Q3/October; decisions read only prior strict closed exits. Need>=20train closed,>=.35closed per covered shared day,positive normalized net all8cases,primary ex-best positive,all8normalized>=E20S1 shadow. Score=min8normalized-.25max8normalizedDD, declared order1e-8tie. FallbackE20S1 research reference, not live adoption.60full-covered-period trades evaluated separately.',
        limitation='Same retrospectively chosen product and repeatedly studied history,8new simultaneous hypotheses; no untouched OOS or calibrated significance. July ABC adds coverage, not an independent proof. Shared no-follow/adaptive differences explicitly separate from originalB5.',
        execution_correction='v1 retained: MFE-peak-only OHLC disagreement can prematurely mark a fully identical trade unknown. New8 opt in to merge only if PolicyOriginalS1,no no-follow,no fastfail/protect/trail,identical open trade state and fills; peak/MFE lower envelope,MAE adverse envelope for diagnostics only. Real trade/entry/stop/confirmation differences remain unknown. Old controls do not opt in. No signal/parameter changes.',
        production_before={q:z.s.m.old.sha(ROOT/q) for q in parent['production_before']},script_sha256=z.s.m.old.sha(__file__),qualified_for_trading=False)
    write('protocol.json',proto)
    fork=RUN/'fork';shutil.copytree(z.RUN/'fork',fork)
    src=(fork/'crates/n-core/src/bin/swing_jd_frequency.rs').read_text(encoding='utf-8')
    src=z.s.m.old.replace(src,'            let new_friction = friction(&ev, fee, mult);','''            let planning_rules: ResearchRules=serde_json::from_value(template["rules"].clone())?;
            let planning_entry=planned_entry(&ev,planning_rules);
            let sign=if ev.direction=="up"{1.}else{-1.};
            let planned_risk=(planning_entry-ev.stop)*sign;
            let mut planning=ev.clone();planning.entry=planning_entry;planning.risk=planned_risk;
            let new_friction = friction(&planning, fee, mult);''')
    src=z.s.m.old.replace(src,'let geometry_ok = ev.risk / ev.tick_size + 1e-6 >= min_risk','let geometry_ok = planned_risk / ev.tick_size + 1e-6 >= min_risk')
    src=z.s.m.old.replace(src,'room_covers_cost_limit(ev.entry,ev.target,if ev.direction=="up"{1.}else{-1.},new_friction*ev.risk,','room_covers_cost_limit(planning_entry,ev.target,if ev.direction=="up"{1.}else{-1.},new_friction*planned_risk,')
    src=z.s.m.old.replace(src,'"planned_risk":ev.risk,','"planned_risk":planned_risk,"planning_entry":planning_entry,"trigger_geometry_risk":ev.risk,')
    src+='''
fn planned_entry(e:&ReplayEvent,r:ResearchRules)->f64{
    if matches!(r.entry_mode,ablation::EntryMode::BreakoutRetestQuarter|ablation::EntryMode::BreakoutRetestReclaim){geom::retest_price(e,if e.direction=="up"{1.}else{-1.})}else{e.entry}
}
#[cfg(test)]mod planning_tests{use super::*;
#[test]fn expected_retest_risk_is_symmetric_and_not_original_risk(){
for short in [false,true]{let sign=if short{-1.}else{1.};let e=ReplayEvent{direction:if short{"down"}else{"up"}.into(),entry:100.,stop:100.-sign*20.,risk:20.,tick_size:1.,..Default::default()};
let r=ResearchRules{entry_mode:ablation::EntryMode::BreakoutRetestQuarter,..Default::default()};let p=planned_entry(&e,r);assert_eq!(p,100.-sign*5.);assert_eq!((p-e.stop)*sign,15.);assert_eq!(planned_entry(&e,ResearchRules::default()),100.);}}
#[test]fn retest_tick_rounding_is_toward_original_breakout(){
let e=ReplayEvent{direction:"up".into(),entry:100.,stop:83.,risk:17.,tick_size:1.,..Default::default()};let r=ResearchRules{entry_mode:ablation::EntryMode::BreakoutRetestQuarter,..Default::default()};assert_eq!(planned_entry(&e,r),96.);}
}
'''
    (fork/'crates/n-core/src/bin/swing_jd_abc_execution.rs').write_text(src,encoding='utf-8')
    patch_peak_equivalence(fork)
    write('implementation-manifest.json',{q.relative_to(fork).as_posix():z.s.m.old.sha(q) for q in fork.rglob('*') if q.is_file()})
    print('Frozen8 ABC entry/stop mechanisms,2 intact controls; no detector changes.',flush=True)

def july_source():
    dest=RUN/'july-source';dest.mkdir(exist_ok=False)
    p=READ(a.RUN/'generation.protocol.json');p['periods']['july']=p['periods']['early'];p['hashes']['july']=p['hashes']['early']
    z.s.m.old.write(dest/'protocol.json',p)
    b=READ(a.RUN/'generator-binary-v2.json');assert z.s.m.old.sha(b['path'])==b['sha256']
    subprocess.run([b['path'],str(dest/'protocol.json'),'july',str(dest/'symbols'),str(dest/'all-pool.json')],check=True)

def generate():
    p=READ(RUN/'protocol.json');assert p['script_sha256']==z.s.m.old.sha(__file__)
    july_source();extra=READ(RUN/'july-source/all-pool.json')
    pp=READ(ROOT/'research-strategies/best-a-portfolio-observation-v1/protocol.json');meta={};qa=collections.Counter();sources={};dates=set()
    for period in ['early','recent']:
        paths=pp['periods'][period];inp=READ(paths['input']);bs=z.x.identification_bars(inp['symbols']['JD0']['bars']);labels=collections.Counter(b['datetime'] for b in bs)
        seg=READ(paths['segments'])['JD0'];codes=[next((s['underlying'] for s in seg if s['first']<=b['datetime']<=s['last']),None) for b in bs]
        pool=READ(z.s.NATIVE/(period+'-pool.json'))
        if period=='early':pool=pool+[s for s in extra if '2026-07-01 00:00:00'<=s['event']['warning_ts']<='2026-07-10 15:00:00']
        pool=[s for s in pool if s['event']['symbol']=='JD0' and s['event']['setup_features']['setup_quality']>=2.5]
        pool.sort(key=lambda s:(s['event']['warning_ts'],s['event_key']))
        seen=set();parents=[];confirmed=[];cseen=set();hi='2026-07-10 15:00:00' if period=='early' else '2026-10-23 11:05:00'
        for snap in pool:
            s=copy.deepcopy(snap);e=s['event'];key=s['event_key']
            assert key not in seen;seen.add(key)
            f=warning_features(e,bs);s['entry_quality']=f
            sign=1 if e['direction']=='up' else -1;buffer=(e['s2_price']-e['stop'])*sign;bstop=f['b_true_extreme']-sign*buffer
            s['mechanism']=dict(true_b_stop=bstop)
            assert (e['stop']-bstop)*sign>=-1e-8
            parents.append(s)
            indices=[i for i,b in enumerate(bs) if b['datetime']==e['warning_ts']]
            assert len(indices)==1;at=indices[0];code=codes[at];end=at
            for j in range(at+1,min(at+13,len(bs))):
                if codes[j]!=code or labels[bs[j]['datetime']]!=1 or bs[j]['datetime']>hi:break
                end=j
            ci,reason=c_confirmation(bs,at,end,e,bstop,f['b_true_extreme']);qa['C15_'+reason]+=1
            cl='|'.join(e[t] for t in ['symbol','direction','s0_ts','s1_ts'])
            meta[key]=dict(parent_key=key,cluster=cl,seed_ts=e['warning_ts'],warning_ts=e['warning_ts'],period=period,contract=code,kind='seed')
            if ci is None:continue
            assert ci>=at+3
            # Prefix equality is the direct causal check; no future pivot label.
            ci2,_=c_confirmation(bs[:ci+1],at,ci,e,bstop,f['b_true_extreme']);assert ci2==ci
            c=bs[ci];cid=(cl,c['datetime'])
            if cid in cseen:qa['C15_duplicate_parent_cluster_clock']+=1;continue
            cseen.add(cid);t=copy.deepcopy(s);ce=t['event'];newkey='C15|'+key+'|'+c['datetime']
            newentry=c['high']+e['tick_size'] if sign>0 else c['low']-e['tick_size']
            ce.update(warning_ts=c['datetime'],entry=newentry,risk=(newentry-e['stop'])*sign,
                rr=abs(e['target']-newentry)/abs(newentry-e['stop']) if newentry!=e['stop'] else 0.,
                trigger_level=newentry,warning_extreme=c['high'] if sign>0 else c['low'],market_context=None)
            t.update(event_key=newkey,warning_candle=dict(c,volume=0.,hold=0.),prior=[])
            confirmed.append(t);meta[newkey]=dict(parent_key=key,cluster=cl,seed_ts=e['warning_ts'],warning_ts=c['datetime'],period=period,contract=code,kind='C15')
        for b,code in zip(bs,codes):
            if code is not None and '2026-01-01'<=b['datetime']<=hi:dates.add(b['datetime'][:10])
        # Fee trading-day lookup includes actual contract exit tails beyond input5m.
        contract_data=READ(paths['contracts']);calendar=sorted({b['datetime'][:10] for code,rec in contract_data.items() if code.startswith('DCE.jd') for b in rec['minutes'] if '09:00:00'<b['datetime'][11:]<='15:00:00'})
        for task,items in [('seed',parents),('C15',confirmed)]:
            dest=RUN/period/task;dest.mkdir(parents=True,exist_ok=False)
            runpaths=dict(paths,input=str(dest/'input-specs.json'))
            z.s.m.old.write(dest/(period+'-pool.json'),items)
            z.s.m.old.write(dest/'input-specs.json',dict(execution_config=inp['execution_config'],symbols={'JD0':{k:inp['symbols']['JD0'][k] for k in ['variety','tick_size']}}))
            z.s.m.old.write(dest/'trading-dates.json',calendar)
            names=[m for m in MODELS if (m.startswith('C15_'))==(task=='C15')]
            z.s.m.old.write(dest/'execution.protocol.json',dict(templates=[p['templates'][m] for m in names],modes=['corrected_touch','next_minute_open'],periods={period:runpaths}))
            print(period,task,len(items),'plans',flush=True)
        sources[period]={k:z.s.m.old.sha(paths[k]) for k in ['input','contracts','segments','fees','holidays']}
    write('signal-metadata.json',meta);write('generation-checks.json',dict(counts=dict(qa),source_hashes=sources,covered_dates=sorted(dates),july_seed_count=sum(m['kind']=='seed' and m['seed_ts'].startswith('2026-07') for m in meta.values())))

def build():
    args=['--release','--offline','--manifest-path',str(RUN/'fork/Cargo.toml'),'--target-dir',str(ROOT/'target/swing-jd-abc-execution-isolated'),'-p','n-core']
    subprocess.run(['cargo','test',*args,'--lib','v3::ablation::tests::peak_only_'],check=True)
    subprocess.run(['cargo','test',*args,'--bin','swing_jd_abc_execution'],check=True);subprocess.run(['cargo','build',*args,'--bin','swing_jd_abc_execution'],check=True)
    b=ROOT/'target/swing-jd-abc-execution-isolated/release/swing_jd_abc_execution.exe';write('binary.json',dict(path=str(b),sha256=z.s.m.old.sha(b)))

def replay():
    b=READ(RUN/'binary.json');assert z.s.m.old.sha(b['path'])==b['sha256']
    for period in ['early','recent']:
        for task in ['seed','C15']:
            dest=RUN/period/task
            subprocess.run([b['path'],str(dest/'execution.protocol.json'),period,'replay',str(dest/'executions')],check=True)

def load():
    folder=ROOT/'research-strategies/best-a-portfolio-observation-v1';spec=importlib.util.spec_from_file_location('abc_execution_controller',folder/'controller.py');c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)
    meta=READ(RUN/'signal-metadata.json');data=collections.defaultdict(list);ends={};funnel=collections.defaultdict(collections.Counter);controls=0;sources={}
    fees=READ(ROOT/'scripts/exchange_fees_20261022.json')
    for period in ['early','recent']:
        old={}
        for line in (z.x.RUN/period/'JD/executions/evaluations.jsonl').open(encoding='utf-8'):
            r=json.loads(line)
            if r['scenario'].split('/')[0] in ['B5','E20S1']:old[r['event_key'],r['scenario']]=r
        for task in ['seed','C15']:
            dest=RUN/period/task;cal=z.s.TradingDates({});cal.dates=READ(dest/'trading-dates.json')
            for code,t in READ(dest/'executions/manifest.json')['contract_ends'].items():ends[code]=max(t,ends.get(code,''))
            path=dest/'executions/evaluations.jsonl';sources[str(path)]=z.s.m.old.sha(path)
            for line in path.open(encoding='utf-8'):
                raw=json.loads(line);model,sf=raw['scenario'].split('/',1)
                if (raw['event_key'],raw['scenario']) in old:
                    prev=old[raw['event_key'],raw['scenario']]
                    for f in ['result','entry','stop','target','planned_risk','new_friction','economic_pass','body_net_pass']:assert raw[f]==prev[f],(f,raw['event_key'],raw['scenario'])
                    controls+=1
                r=z.s.reprice(raw,fees,cal);r['period']=period
                if EV(r)['status']=='closed':
                    assert EV(r)['entry_ts']>r['warning_ts']
                    for fill in r['result']['fills']:assert abs(fill['price']-round(fill['price']))<1e-7
                data[model,sf].append(r);funnel[model+'/'+sf][EV(r)['status']]+=1
    assert controls==1760,controls
    return c,meta,data,ends,{k:dict(v) for k,v in funnel.items()},controls,sources

def analyze():
    p=READ(RUN/'protocol.json');assert p['script_sha256']==z.s.m.old.sha(__file__)
    c,meta,data,ends,funnel,controls,sources=load();cases=[pol+'/'+sf for pol in p['policies'] for sf in p['scenarios']]
    fixed={};accepted={};ledgers={};dates=READ(RUN/'generation-checks.json')['covered_dates']
    for case in cases:
        pol,sf=case.split('/',1)
        for model in MODELS:
            aa,ll=admit(data[model,sf],ends,pol,c,meta);accepted[case,model]=aa
            q=z.metric(aa);q['blocks']={f['label']:z.metric([r for r in aa if f['start']<=r['warning_ts']<f['end']]) for f in z.x.blocks()}
            q['test']=z.metric([r for r in aa if r['warning_ts']>=FOLDS[0][0]],[m for m in MONTHS if m>='2026-04'])
            q['july']=z.metric([r for r in aa if r['warning_ts'].startswith('2026-07')],['2026-07'])
            fixed[case+'/'+model]=q
            if case==p['primary']:ledgers[model]=ll
    schedule=[];training=[]
    for start,end in FOLDS:
        ndays=sum(d<start[:10] for d in dates);visible={};eligible=[];rejected={}
        for model in MODELS:visible[model]={case:z.metric(z.x.visible(accepted[case,model],start),[m for m in MONTHS if m<start[:7]]) for case in cases}
        baseline=visible['E20S1']
        for model in CHALLENGERS:
            zs=visible[model];q=zs[p['primary']];why=[]
            if q['n']<20:why.append('train_closed<20')
            if q['n']/ndays<.35:why.append('frequency<.35/covered_day')
            if min(x['normalized_net'] for x in zs.values())<=0:why.append('some_stress_nonpositive')
            if q['normalized_ex_best']<=0:why.append('ex_best_nonpositive')
            if any(x['normalized_net']<baseline[case]['normalized_net']-1e-7 for case,x in zs.items()):why.append('some_stress_below_E20S1')
            if why:rejected[model]=why
            else:eligible.append((min(x['normalized_net'] for x in zs.values())-.25*max(x['normalized_realized_dd'] for x in zs.values()),model))
        eligible.sort(key=lambda q:(-round(q[0],8),CHALLENGERS.index(q[1])))
        chosen=eligible[0][1] if eligible else 'E20S1';schedule.append(dict(start=start,end=end,model=chosen));training.append(dict(cutoff=start,covered_days=ndays,selected=chosen,eligible=eligible,rejected=rejected,visible_metrics=visible))
    write('selection-before-test.json',dict(schedule=schedule,training=training))
    wf={};wfrows={}
    for case in cases:
        pol,sf=case.split('/',1);rows=[]
        for model in MODELS:
            for row in data[model,sf]:
                chosen=next((f['model'] for f in schedule if f['start']<=row['warning_ts']<f['end']),'E20S1')
                if chosen==model:rows.append(row)
        aa,ll=admit(rows,ends,pol,c,meta);test=[r for r in aa if r['warning_ts']>=FOLDS[0][0]]
        wf[case]=z.metric(test,[m for m in MONTHS if m>='2026-04']);wfrows[case]=aa
        if case==p['primary']:ledgers['walkforward']=ll
    write('fixed-results.json',fixed);write('walkforward-results.json',wf);write('funnel.json',funnel);write('opportunity-ledgers.json',ledgers)
    with (RUN/'accepted-primary.jsonl').open('x',encoding='utf-8') as f:
        for model in MODELS:
            for row in accepted[p['primary'],model]:f.write(json.dumps(dict(model=model,row=row),ensure_ascii=False)+'\n')
        for row in wfrows[p['primary']]:f.write(json.dumps(dict(model='walkforward',row=row),ensure_ascii=False)+'\n')
    for q,h in p['production_before'].items():assert z.s.m.old.sha(ROOT/q)==h
    for q,h in READ(RUN/'implementation-manifest.json').items():assert z.s.m.old.sha(RUN/'fork'/q)==h
    write('verification.json',dict(exact_native_control_rows=controls,production_unchanged=True,source_hashes=sources,
        entry_modes_and_stops_warning_causal=True,C15_prefix_checks=True,no_order_before_C_confirmation=True,no_fold_reset=True,qualified_for_trading=False))
    print(json.dumps(dict(schedule=schedule,main={m:{k:fixed[p['primary']+'/'+m][k] for k in ['n','net','normalized_net','unknown']} for m in MODELS}),ensure_ascii=False),flush=True)

class MechanismTests(unittest.TestCase):
    def bars(self):
        return [dict(datetime=f'2026-01-05 10:{i*15:02}:00',open=100.,high=105.,low=95.,close=101.) for i in range(4)]
    def test_C_is_confirmed_at_right_close_and_prefix_invariant(self):
        bs=self.bars();bs[1].update(high=110.,low=97.);bs[2].update(high=106.,low=96.);bs[3].update(high=108.,low=98.)
        seed=dict(direction='up',entry=107.,target=120.)
        self.assertEqual(c_confirmation(bs,0,2,seed,90.,92.)[0],None)
        self.assertEqual(c_confirmation(bs,0,3,seed,90.,92.)[0],3)
        self.assertEqual(c_confirmation(bs+[dict(bs[3],low=1.)],0,3,seed,90.,92.)[0],3)
    def test_B_break_and_prior_target_cancel_C(self):
        bs=self.bars();bs[1]['low']=89.;seed=dict(direction='up',entry=107.,target=120.)
        self.assertEqual(c_confirmation(bs,0,3,seed,90.,92.)[1],'B_invalidated')
        bs=self.bars();bs[1]['high']=121.;self.assertEqual(c_confirmation(bs,0,3,seed,90.,92.)[1],'S1_already_reached')
    def test_C_requires_post_seed_push_not_only_a_local_pivot(self):
        bs=self.bars();bs[2]['low']=94.;seed=dict(direction='up',entry=110.,target=120.)
        self.assertEqual(c_confirmation(bs,0,3,seed,90.,92.)[0],None)
    def test_two_stops_must_be_realized_strictly_before_next_warning(self):
        controller=type('C',(),{'ambiguity':lambda self,row:None})()
        def row(k,w,end):return dict(event_key=k,warning_ts=w,execution_contract='JD',result=dict(evaluation=dict(status='closed',entry_ts=w,exit_ts=end,exit_reason='stop')))
        rows=[row('a','2026-01-05 09:15:00','2026-01-05 09:20:00'),row('b','2026-01-05 09:30:00','2026-01-05 09:40:00'),row('c','2026-01-05 09:45:00','2026-01-05 10:00:00')]
        meta={k:dict(cluster='same') for k in ['a','b','c']};aa,ll=admit(rows,{},'original',controller,meta)
        self.assertEqual(len(aa),2);self.assertEqual(ll[-1]['reason'],'two_realized_A_stops')

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('stage',choices=['test','prepare','generate','build','replay','analyze']);args=ap.parse_args()
    if args.stage=='test':unittest.main(argv=[sys.argv[0]])
    else:globals()[args.stage]()
