"""Bidirectional S0reclaim+planned2R, finite execution/exit mechanism experiment."""
import argparse
import collections
import copy
import datetime as dt
import json
import shutil
import subprocess
import unittest
import research_swing_jd_reclaim_robustness as r

b=r.b
R=r.R
EV=r.f.EV
RUN=b.ROOT/'research-runs/swing-jd-reclaim-execution-opt-20261027-v1'
NEW=['ENTRY_HOLD','REFAIL1','REFAIL2','STALE8','BE1']
MODELS=['BASE']+NEW+['E20_ONLY']
write=lambda n,x:b.z.s.m.old.write(RUN/n,x)
sha=r.sha

def prepare():
    RUN.mkdir(exist_ok=False);parent=R(r.c.RUN/'protocol.json');templates={}
    for model in MODELS[:-1]:
        t=copy.deepcopy(parent['templates']['S0_RECLAIM_R2']);t['name']=model
        if model=='STALE8':t['rules']['fast_fail_bucket']=8
        if model=='BE1':t['rules'].update(protect_after_r=1.,protect_lock_r=0.)
        templates[model]=t
    p=dict(id=RUN.name,created_at=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(),models=MODELS,templates=templates,
        primary='original/corrected_touch/slip1',slips=[1,2,3,4],policies=['original','two_paths_flat'],
        request='Stopfixedlong-onlyexploration. OptimizecompletebidirectionalE20+S0reclaimplanned2Rfamily, nofixedsideveto ornewtrendfitting.',
        common='Same32main+2knownJulyS0reclaimdecisions,originalentrycloseplanning,realbreakextremestop,planned2Rtargetandallfees/risk/holiday/maxholdguards. E20unchanged. Onlynewmodulevaries, fullcontinuousjointadmissionrerun.',
        variants=dict(BASE='Frozencompletebidirectionalreference.',ENTRY_HOLD='Fromdecisionuntilfill, cancelifobservedquote touches/crossesS0againstoriginalA. Usesexistingpending_body_invalidation=S0; adverseintrabarbefore delayedfill alsoinvalidates. Doesnotfilterusingfuturefill/winner.',
            REFAIL1='Afterentry, firstFULLpostentry15mclose >=1tick beyondS0againstoriginalA armsmarketexit atNEXTobserved1mOPEN. Neverpartialentrybar.',
            REFAIL2='TwoFULLpostentry15mcloses >=1tick beyondS0againstoriginalA, adjacent15wallclockminutes. Recoveredclose resetsstreak; sessiongaps resetconsecutiveness. Nextobserved1mOPEN exit.',
            STALE8='Existingnativefastfail: atheldbucket8close, peak<.5actualR ANDcloseworsethanoriginalplanentry, exitobservedclose lessadverseslip. Entrybucketcounts1evenpartial; not8FULLpostentrybars. Sameobservation-close executionassumptionasexistingtimeexit.',
            BE1='At1actualR MFE,protectivestopmoves togrossfillprice, planned2Rtargetunchanged. Fees/slipremain, trueOHLCorderconflictstaysunknown, notclaimedcostfreebreakeven.'),
        design='5singlechanges, notconjunctivegrid. Alllong+shortkept. Entryeconomicsandpricegeometryunchanged. Nativecorebyteidenticaltosavedreference; calleronlymapsS0toexistingrulesandextends1..4slip.',
        validation='Retrospectivesequentialhypotheses, olddataisnotfreshOOS. ExpandingApr/Jul/Oct strictclosedexits; >=20trainclosed,>=.35closedpercoveredday,positiveall16normalized,exbestpositive,all16normalized>=BASEshadow. Maxmin16normalized-.25max16DD,declaredtieorder; fallbackBASE. Noquarterreset.',
        concentration='Reporteachdirection,all4blocks,exbest3A/week,conditionaldeleteQ2net,sameparentopportunityattribution,minutecloseMTMrisk. No60frequencyguarantee, noaccountreturn/marginguarantee.',
        snapshot_manifest_sha256=sha(r.SNAP/'manifest.json'),production_before=parent['production_before'],script_sha256=sha(__file__))
    write('protocol.json',p)
    fork=RUN/'fork';shutil.copytree(r.SNAP/'native-source',fork)
    src=(fork/'crates/n-core/src/bin/swing_jd_abc_execution.rs').read_text(encoding='utf-8')
    src=b.z.s.m.old.replace(src,'for slip in [1., 2.]','for slip in [1., 2., 3., 4.]')
    src=b.z.s.m.old.replace(src,'                for slip in [1., 2., 3., 4.] {','                configure_reclaim_rules(&mut rules,template,&original);\n                for slip in [1., 2., 3., 4.] {')
    src+='''
fn configure_reclaim_rules(r:&mut ResearchRules,t:&Value,e:&ReplayEvent){
    let sign=if e.direction=="up"{1.}else{-1.};
    match t["name"].as_str().unwrap(){
        "ENTRY_HOLD"=>r.pending_body_invalidation=Some(e.s0_price),
        "REFAIL1"|"REFAIL2"=>{r.body_failure_level=Some(e.s0_price-sign*e.tick_size);r.body_failure_count=Some(if t["name"]=="REFAIL1"{1}else{2});},
        _=>{}
    }
}
#[cfg(test)]mod reclaim_rules_tests{use super::*;use chrono::{NaiveDateTime,Duration};
fn sample(short:bool,count:usize)->ablation::ResultRow{
    let sign=if short{-1.}else{1.};let mut e=ReplayEvent{symbol:"JD0".into(),direction:if short{"down"}else{"up"}.into(),warning_ts:"2026-01-05 09:15:00".into(),entry:100.,stop:100.-sign*20.,target:100.+sign*40.,risk:20.,s0_price:100.,tick_size:1.,..Default::default()};
    e.setup_features.setup_quality=3.;let mut rules=ResearchRules{entry_mode:ablation::EntryMode::SignalNextOpen,..Default::default()};configure_reclaim_rules(&mut rules,&json!({"name":if count==1{"REFAIL1"}else{"REFAIL2"}}),&e);
    let start=NaiveDateTime::parse_from_str("2026-01-05 09:15:00","%Y-%m-%d %H:%M:%S").unwrap();let mut bars=vec![];
    for n in 1..=46{let mut b=Kline{datetime:(start+Duration::minutes(n)).format("%Y-%m-%d %H:%M:%S").to_string(),open:100.,high:102.,low:97.,close:101.,volume:10.,hold:10.};
    if [15,30,45].contains(&n){b.close=98.;}if n==31||n==46{b.open=97.;}
    if short{b.open=200.-b.open;b.close=200.-b.close;let old=b.high;b.high=200.-b.low;b.low=200.-old;}bars.push(b);}
    ablation::evaluate_research(&e,&bars,&[],&ExecutionConfig::default(),Experiment{policy:Policy::OriginalS1,no_follow_bar:None,slip_ticks:1.,fee_ticks:0.},10.,None,rules)
}
#[test]fn reclaim_fail_exit_ignores_partial_entry_bar_and_uses_next_open_both_sides(){for short in [false,true]{let r=sample(short,1);assert_eq!(r.evaluation.exit_ts.as_deref(),Some("2026-01-05 09:46:00"));assert_eq!(r.evaluation.exit_reason.as_deref(),Some("warning_body_failed_close"));assert_eq!(r.evaluation.net_amount,Some(-50.));}}
#[test]fn reclaim_fail_two_requires_two_full_adjacent_closes(){for short in [false,true]{let r=sample(short,2);assert_eq!(r.evaluation.exit_ts.as_deref(),Some("2026-01-05 10:01:00"));}}
#[test]fn rules_use_true_S0_and_preserve_signed_tick_buffer(){for short in [false,true]{let e=ReplayEvent{direction:if short{"down"}else{"up"}.into(),s0_price:3000.,tick_size:1.,..Default::default()};let mut r=ResearchRules::default();configure_reclaim_rules(&mut r,&json!({"name":"REFAIL2"}),&e);assert_eq!(r.body_failure_level,Some(if short{3001.}else{2999.}));assert_eq!(r.body_failure_count,Some(2));configure_reclaim_rules(&mut r,&json!({"name":"ENTRY_HOLD"}),&e);assert_eq!(r.pending_body_invalidation,Some(3000.));}}
}
'''
    (fork/'crates/n-core/src/bin/swing_jd_reclaim_execution_opt.rs').write_text(src,encoding='utf-8')
    write('implementation-manifest.json',{q.relative_to(fork).as_posix():sha(q) for q in fork.rglob('*') if q.is_file()})

def build():
    for q in ['crates/n-core/src/v3/ablation.rs','crates/n-core/src/v3/ablation/warning_failure.rs']:assert sha(RUN/'fork'/q)==sha(r.SNAP/'native-source'/q)
    args=['--release','--offline','--manifest-path',str(RUN/'fork/Cargo.toml'),'--target-dir',str(b.ROOT/'target/swing-jd-reclaim-stress'),'-p','n-core']
    subprocess.run(['cargo','test',*args,'--bin','swing_jd_reclaim_execution_opt'],check=True)
    subprocess.run(['cargo','build',*args,'--bin','swing_jd_reclaim_execution_opt'],check=True)
    path=b.ROOT/'target/swing-jd-reclaim-stress/release/swing_jd_reclaim_execution_opt.exe';write('binary.json',dict(path=str(path),sha256=sha(path)))

def generate():
    p=R(RUN/'protocol.json')
    for per in ['early','recent','july']:
        dest=RUN/per;dest.mkdir();paths=r.f.t.period_paths(per)[0];pool=R(r.c.RUN/per/'S0_RECLAIM_R2'/(per+'-pool.json'))
        write(per+'/'+per+'-pool.json',pool);write(per+'/execution.protocol.json',dict(templates=[p['templates'][m] for m in MODELS[:-1]],modes=['corrected_touch','next_minute_open'],periods={per:paths}))

def replay():
    exe=R(RUN/'binary.json');assert sha(exe['path'])==exe['sha256']
    for per in ['early','recent','july']:subprocess.run([exe['path'],str(RUN/per/'execution.protocol.json'),per,'replay',str(RUN/per/'executions')],check=True)

def load():
    ctl,meta,prior,ends,oldchecks=r.load();data=collections.defaultdict(list);fees=R(r.SNAP/'fees.json');qa=collections.Counter()
    for (m,suffix),rs in prior.items():
        if m=='E20S1':data[m,suffix]=rs
    lookup={(suffix,q['period'],q['event_key']):q for (m,suffix),rs in prior.items() if m=='S0_RECLAIM_R2' for q in rs}
    for per in ['early','recent','july']:
        cal=b.z.s.TradingDates({});cal.dates=r.f.t.period_paths(per)[3]
        for code,end in R(RUN/per/'executions/manifest.json')['contract_ends'].items():ends[code]=max(ends.get(code,''),end)
        for line in (RUN/per/'executions/evaluations.jsonl').open(encoding='utf-8'):
            row=b.z.s.reprice(json.loads(line),fees,cal);row['period']=per;m,suffix=row['scenario'].split('/',1);base=lookup[suffix,per,row['event_key']]
            for k in ['entry','stop','target','planned_risk','planning_entry','new_friction','body_net_pass','economic_pass']:assert row[k]==base[k],(m,k)
            qa['geometry_and_economics_unchanged']+=1
            if m=='BASE':
                assert row['result']==base['result'],(per,suffix,row['event_key']);qa['exact_BASE_rows']+=1
            if m not in ['BASE','ENTRY_HOLD'] and EV(row)['status']!='ambiguous':
                assert EV(row)['entry_ts']==EV(base)['entry_ts'];assert EV(row)['fill']==EV(base)['fill'];qa['exit_only_entry_equal']+=1
            data[m,suffix].append(row)
    return ctl,meta,data,ends,dict(qa)

def admit(data,suffix,model,ctl,meta,ends,policy,combined):
    stream=[];mm=dict(meta)
    for prefix,source in [('0','E20S1')]+([] if model=='E20_ONLY' else [('1',model)]):
        for original in data[source,suffix]:
            if original['period']=='july' and not combined:continue
            row=copy.deepcopy(original);key=prefix+'|'+row['event_key'];mm[key]=meta[row['event_key']];row.update(original_event_key=row['event_key'],event_key=key,module='E20S1' if source=='E20S1' else 'S0_RECLAIM_R2');stream.append(row)
    return b.admit(stream,ends,policy,ctl,mm)

def analyze():
    p=R(RUN/'protocol.json');assert p['script_sha256']==sha(__file__);ctl,meta,data,ends,qa=load();cases=[pol+'/'+mode+'/slip'+str(s) for pol in p['policies'] for mode in ['corrected_touch','next_minute_open'] for s in p['slips']]
    fixed={};allaccepted={};july={};baseline=R(r.RUN/'main-results.json')
    for combined in [False,True]:
        result={}
        for case in cases:
            policy,suffix=case.split('/',1)
            for m in MODELS:
                aa,ll=admit(data,suffix,m,ctl,meta,ends,policy,combined);q=b.z.metric(aa);q['blocks']={z['label']:b.z.metric([v for v in aa if z['start']<=v['warning_ts']<z['end']]) for z in b.z.x.blocks()};result[case+'/'+m]=q
                if combined:july[case+'/'+m]=b.z.metric([v for v in aa if '2026-07-13'<=v['warning_ts']<'2026-07-25'])
                else:
                    allaccepted[case,m]=aa
                    if m in ['BASE','E20_ONLY']:
                        old=baseline[case+'/'+('FULL' if m=='BASE' else 'E20_ONLY')]
                        for k in ['n','net','normalized_net','unknown','normalized_realized_dd','month_cash']:assert q[k]==old[k],(m,k)
        write('combined-results.json' if combined else 'main-results.json',result)
        if not combined:fixed=result
    write('july-results.json',july)
    # Previousclosed-only expandingvalidation, strongsavedbaseline required.
    schedule=[];dates=R(b.RUN/'generation-checks.json')['covered_dates']
    for start,end in b.FOLDS:
        visible={m:{case:b.z.metric(b.z.x.visible(allaccepted[case,m],start)) for case in cases} for m in MODELS};nday=sum(d<start[:10] for d in dates);eligible=[];reject={}
        for m in NEW:
            xs=visible[m];q=xs[p['primary']];why=[]
            if q['n']<20 or q['n']/nday<.35:why.append('train_frequency')
            if min(v['normalized_net'] for v in xs.values())<=0:why.append('stress_nonpositive')
            if q['normalized_ex_best']<=0:why.append('ex_best_nonpositive')
            if any(v['normalized_net']<visible['BASE'][case]['normalized_net']-1e-7 for case,v in xs.items()):why.append('below_frozen_baseline')
            if why:reject[m]=why
            else:eligible.append((min(v['normalized_net'] for v in xs.values())-.25*max(v['normalized_realized_dd'] for v in xs.values()),m))
        eligible.sort(key=lambda z:(-round(z[0],8),NEW.index(z[1])));schedule.append(dict(start=start,end=end,chosen=eligible[0][1] if eligible else 'BASE',rejections=reject))
    write('chronological-selection.json',schedule)
    wf={}
    for case in cases:
        policy,suffix=case.split('/',1);stream=[];mm=dict(meta)
        for m in MODELS:
            if m=='E20_ONLY':continue
            for row in data[m,suffix]:
                if row['period']=='july':continue
                chosen=next((q['chosen'] for q in schedule if q['start']<=row['warning_ts']<q['end']),'BASE')
                if chosen==m:stream.append(row)
        temp=dict(data);temp['schedule',suffix]=stream;aa,_=admit(temp,suffix,'schedule',ctl,meta,ends,policy,False);wf[case]=b.z.metric([q for q in aa if q['warning_ts']>=b.FOLDS[0][0]])
    write('walkforward-results.json',wf)
    with (RUN/'accepted-main-primary.jsonl').open('x',encoding='utf-8') as out:
        for m in MODELS:
            for row in allaccepted[p['primary'],m]:out.write(json.dumps(dict(model=m,row=row),ensure_ascii=False)+'\n')
    write('verification.json',dict(native_checks=qa,exact_original_portfolio_cases=32,frozen_candidate_unchanged=sha(r.SNAP/'manifest.json')==p['snapshot_manifest_sha256'],production_unchanged=all(sha(b.ROOT/q)==h for q,h in p['production_before'].items())))
    print(json.dumps({m:dict(n=fixed[p['primary']+'/'+m]['n'],net=fixed[p['primary']+'/'+m]['net'],unknown=fixed[p['primary']+'/'+m]['unknown'],min16=min(fixed[case+'/'+m]['net'] for case in cases)) for m in MODELS}),flush=True)

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('stage',choices=['prepare','build','generate','replay','analyze']);args=ap.parse_args();globals()[args.stage]()
