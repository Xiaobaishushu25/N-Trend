"""Frozen-candidate robustness, real minute replay at1..4slip and causal gates."""
import argparse
import collections
import copy
import datetime as dt
import json
import shutil
import subprocess
import unittest
import research_swing_jd_reclaim_overlay as o

c=o.c
f=c.f
b=f.b
R=f.R
RUN=b.ROOT/'research-runs/swing-jd-reclaim-robustness-20261026-v1'
SNAP=b.ROOT/'research-strategies/jd-abc-reclaim-observation-v2'
MODELS=['E20_ONLY','FULL','CAP400','CAP800','LONG_ONLY','SHORT_ONLY']
write=lambda n,x:b.z.s.m.old.write(RUN/n,x)
sha=f.sha

def gate(row,variant):
    if variant=='FULL':return True
    if variant in ['CAP400','CAP800']:return row['planned_risk']*row['multiplier']<=float(variant[3:])+1e-8
    if variant=='LONG_ONLY':return row['direction']=='up'
    if variant=='SHORT_ONLY':return row['direction']=='down'
    return False

def prepare():
    RUN.mkdir(exist_ok=False)
    # Frozen inputs are checked before using live workspace helpers to assemble.
    subprocess.run(['python',str(SNAP/'replay.py')],check=True)
    p=R(c.RUN/'protocol.json')
    write('protocol.json',dict(id=RUN.name,created_at=dt.datetime.now().isoformat(),snapshot=str(SNAP),snapshot_manifest_sha256=sha(SNAP/'manifest.json'),
        models=MODELS,primary='original/corrected_touch/slip1',slips=[1,2,3,4],policies=p['policies'],
        source_models=['E20S1','S0_RECLAIM_R2'],templates=dict(E20S1=R(f.RUN/'protocol.json')['templates']['E20S1'],S0_RECLAIM_R2=p['templates']['S0_RECLAIM_R2']),
        purpose='Robustness ofsavedcandidate, notparametersearch. Frozenreference retained. No entry/exit/recognition/target changes.',
        filters='Allfilters applyONLYnewreclaim decisions beforewarningtimeadmission. CAP400/CAP800 limitsknown plannedcashrisk perone lot, notaccountrecommendation oractualgaprisk guarantee. LONG_ONLY/SHORT_ONLY isolatesnewmodule, originalE20 bothdirectionsunchanged. No conjunctivefiltergrid.',
        execution='Replayactual1m withadverse slip1,2,3,4ticks PERFILL, samecorrected andextra1m delay modes; frozenplanfriction andentryguard assumptions retained, actualfill/chase/stop/target outcomes recomputed. NotposthocPnL subtraction. Two unknownoccupancy policies.',
        concentration='Descriptive dropbest1/3 wholeparentAclusters andbestweek fromacceptedsettlements, notnewstrategies/no readmission. Side andplannedrisk strata aredescriptive; unknownnot0.',
        validation='No promotionbasedonthissamehistory. MainandknownJuly separate+continuouscombined. Rawcontrol1/2slip nativeevaluations mustbeexact, frozenportfolioFULL/E20 exact. Frequency60 andrisk-normalizedstatsreported evenifcashpositive.',
        limits='Retrospective sequentialstresschecks, nofreshOOS. Riskcap decisions cannotusefuturefills orfutureprofit; actualriskmayovershootcap. Directionisolations mayreducefrequencyandchangereadmission.',
        production_before=p['production_before'],script_sha256=sha(__file__)))
    fork=RUN/'fork';shutil.copytree(b.RUN/'fork',fork)
    old=fork/'crates/n-core/src/bin/swing_jd_abc_execution.rs';src=old.read_text(encoding='utf-8');src=b.z.s.m.old.replace(src,'for slip in [1., 2.]','for slip in [1., 2., 3., 4.]')
    (old.with_name('swing_jd_reclaim_stress.rs')).write_text(src,encoding='utf-8')
    write('implementation-manifest.json',{q.relative_to(fork).as_posix():sha(q) for q in fork.rglob('*') if q.is_file()})

def build():
    # Native library is byte-identical tosavedbaseline; onlybin sliplist differs.
    for q in ['crates/n-core/src/v3/ablation.rs','crates/n-core/src/v3/geometry_research.rs']:assert sha(RUN/'fork'/q)==sha(SNAP/'native-source'/q)
    args=['--release','--offline','--manifest-path',str(RUN/'fork/Cargo.toml'),'--target-dir',str(b.ROOT/'target/swing-jd-reclaim-stress'),'-p','n-core','--bin','swing_jd_reclaim_stress']
    subprocess.run(['cargo','build',*args],check=True)
    binary=b.ROOT/'target/swing-jd-reclaim-stress/release/swing_jd_reclaim_stress.exe';write('binary.json',dict(path=str(binary),sha256=sha(binary)))

def generate():
    p=R(RUN/'protocol.json');assert p['script_sha256']==sha(__file__)
    for per in ['early','recent','july']:
        paths,recognition,seeds,dates=f.t.period_paths(per)
        for model in p['source_models']:
            pool=seeds if model=='E20S1' else R(c.RUN/per/model/(per+'-pool.json'))
            dest=RUN/per/model;dest.mkdir(parents=True);b.z.s.m.old.write(dest/(per+'-pool.json'),pool)
            b.z.s.m.old.write(dest/'execution.protocol.json',dict(templates=[p['templates'][model]],modes=['corrected_touch','next_minute_open'],periods={per:paths}))

def replay():
    binary=R(RUN/'binary.json');assert sha(binary['path'])==binary['sha256']
    for per in ['early','recent','july']:
        for m in ['E20S1','S0_RECLAIM_R2']:
            dest=RUN/per/m;subprocess.run([binary['path'],str(dest/'execution.protocol.json'),per,'replay',str(dest/'executions')],check=True)

def load():
    ctl,meta,old,ends,_=c.load(True);data=collections.defaultdict(list);fees=R(b.ROOT/'scripts/exchange_fees_20261022.json');checks=0
    oldlookup={(m,sf,r['period'],r['event_key']):r for (m,sf),rs in old.items() if m in ['E20S1','S0_RECLAIM_R2'] for r in rs}
    for per in ['early','recent','july']:
        cal=b.z.s.TradingDates({});cal.dates=f.t.period_paths(per)[3]
        for m in ['E20S1','S0_RECLAIM_R2']:
            dest=RUN/per/m
            for code,end in R(dest/'executions/manifest.json')['contract_ends'].items():ends[code]=max(ends.get(code,''),end)
            for line in (dest/'executions/evaluations.jsonl').open(encoding='utf-8'):
                r=b.z.s.reprice(json.loads(line),fees,cal);r['period']=per;model,suffix=r['scenario'].split('/',1);data[model,suffix].append(r)
                if r['slip']<=2:
                    previous=oldlookup[model,suffix,per,r['event_key']]
                    for k in ['result','planned_risk','planning_entry','entry','stop','target','new_friction','body_net_pass','economic_pass']:assert r[k]==previous[k],(k,model,per,r['event_key'])
                    checks+=1
    return ctl,meta,data,ends,checks

def admit_variant(data,suffix,variant,ctl,meta,ends,policy,combined):
    stream=[];mm=dict(meta)
    for prefix,model in [('0','E20S1'),('1','S0_RECLAIM_R2')]:
        for original in data[model,suffix]:
            if not combined and original['period']=='july':continue
            if model=='S0_RECLAIM_R2' and not gate(original,variant):continue
            r=copy.deepcopy(original);key=prefix+'|'+r['event_key'];mm[key]=meta[r['event_key']];r.update(original_event_key=r['event_key'],event_key=key,module=model);stream.append(r)
    return b.admit(stream,ends,policy,ctl,mm)[0],mm

def analyze():
    p=R(RUN/'protocol.json');ctl,meta,data,ends,checks=load();cases=[pol+'/'+mode+'/slip'+str(slip) for pol in p['policies'] for mode in ['corrected_touch','next_minute_open'] for slip in p['slips']]
    result={};july={};mainaccepted={};reference=R(SNAP/'expected-main.json');matched=0
    for combined in [False,True]:
        stats={}
        for case in cases:
            pol,suffix=case.split('/',1)
            for variant in MODELS:
                aa,mm=admit_variant(data,suffix,variant,ctl,meta,ends,pol,combined);q=b.z.metric(aa);q['blocks']={f['label']:b.z.metric([r for r in aa if f['start']<=r['warning_ts']<f['end']]) for f in b.z.x.blocks()};stats[case+'/'+variant]=q
                if combined:july[case+'/'+variant]=b.z.metric([r for r in aa if '2026-07-13'<=r['warning_ts']<'2026-07-25'])
                else:
                    mainaccepted[case,variant]=aa
                    if case in R(SNAP/'portfolio-data.json')['cases'] and variant in ['FULL','E20_ONLY']:
                        old=reference[case+'/'+('E20_PLUS_RECLAIM_R2' if variant=='FULL' else 'E20S1')]
                        for k in ['n','net','unknown','normalized_net','normalized_realized_dd','normalized_ex_top3','month_cash']:assert q[k]==old[k],(case,variant,k,q[k],old[k])
                        matched+=1
        write('combined-results.json' if combined else 'main-results.json',stats)
        if not combined:result=stats
    write('july-results.json',july)
    # Conditional diagnostic: do not turn deletion ofwinners intotradingrules.
    concentration={};primary=p['primary'];accepted=mainaccepted[primary,'FULL'];cs=[r for r in accepted if f.EV(r)['status']=='closed']
    for unit in ['Acluster','week']:
        bins=collections.defaultdict(lambda:dict(n=0,cash=0.,normalized=0.))
        for r in cs:
            key=meta[r['original_event_key']]['cluster'] if unit=='Acluster' else str(dt.date.fromisoformat(r['warning_ts'][:10]).isocalendar()[:2]);v=bins[key];v['n']+=1;v['cash']+=f.EV(r)['net_amount'];v['normalized']+=300*f.EV(r)['net_amount']/(r['planned_risk']*r['multiplier'])
        topcash=sorted(bins.values(),key=lambda q:q['cash'],reverse=True);topnorm=sorted(bins.values(),key=lambda q:q['normalized'],reverse=True);base=result[primary+'/FULL']
        concentration[unit]=dict(groups=dict(bins),ex_best_cash=base['net']-max([0]+[v['cash'] for v in bins.values()]),ex_top3_cash=base['net']-sum(max(0,v['cash']) for v in topcash[:3]),
            ex_best_normalized=base['normalized_net']-max([0]+[v['normalized'] for v in bins.values()]),ex_top3_normalized=base['normalized_net']-sum(max(0,v['normalized']) for v in topnorm[:3]))
    write('concentration.json',concentration)
    strata={};new=[r for r in cs if r['module']=='S0_RECLAIM_R2']
    for label,predicate in [('long',lambda r:r['direction']=='up'),('short',lambda r:r['direction']=='down'),('risk_le400',lambda r:r['planned_risk']*10<=400),('risk_400_800',lambda r:400<r['planned_risk']*10<=800),('risk_gt800',lambda r:r['planned_risk']*10>800)]:strata[label]=b.z.metric([r for r in new if predicate(r)])
    write('new-module-strata.json',strata)
    with (RUN/'accepted-main-primary.jsonl').open('x',encoding='utf-8') as out:
        for v in MODELS:
            for r in mainaccepted[primary,v]:out.write(json.dumps(dict(variant=v,row=r),ensure_ascii=False)+'\n')
    write('verification.json',dict(exact_prior_individual_rows=checks,exact_prior_portfolio_cases=matched,snapshot_manifest_unchanged=sha(SNAP/'manifest.json')==p['snapshot_manifest_sha256'],production_unchanged=all(sha(b.ROOT/q)==h for q,h in p['production_before'].items())))
    print(json.dumps({m:dict(n=result[primary+'/'+m]['n'],net=result[primary+'/'+m]['net'],worst16=min(result[case+'/'+m]['net'] for case in cases)) for m in MODELS}),flush=True)

class Tests(unittest.TestCase):
    def test_caps_use_warning_known_plan_and_keep_boundary(self):
        r=dict(planned_risk=40.,multiplier=10.,direction='up',result={'future':'irrelevant'});self.assertTrue(gate(r,'CAP400'));r['planned_risk']=40.1;self.assertFalse(gate(r,'CAP400'));self.assertTrue(gate(r,'CAP800'))
    def test_side_gates_and_original_only(self):
        r=dict(planned_risk=20.,multiplier=10.,direction='down');self.assertFalse(gate(r,'LONG_ONLY'));self.assertTrue(gate(r,'SHORT_ONLY'));self.assertFalse(gate(r,'E20_ONLY'));self.assertTrue(gate(r,'FULL'))

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('stage',choices=['test','prepare','build','generate','replay','analyze']);args=ap.parse_args()
    if args.stage=='test':unittest.main(argv=[__file__])
    else:globals()[args.stage]()
