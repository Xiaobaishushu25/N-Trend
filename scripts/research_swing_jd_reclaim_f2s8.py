"""One explicit sequential combination of structural and time invalidation."""
import copy
import json
import subprocess
import research_swing_jd_reclaim_execution_opt as x

RUN=x.RUN/'F2S8-followup'
write=lambda n,z:x.b.z.s.m.old.write(RUN/n,z)

def run():
    RUN.mkdir(exist_ok=False);p=x.R(x.RUN/'protocol.json');template=copy.deepcopy(p['templates']['REFAIL2']);template['rules']['fast_fail_bucket']=8
    write('protocol.json',dict(model='F2S8',native_internal_name='REFAIL2',template=template,
        declaration='Sequential AFTER5singlechangeoutcomes: combineONLYREFAIL2andSTALE8, nofullcombinationgrid,noBE1. Explicitoldhistoryexploration,notfreshOOS.',
        reason='Twoindependentinvalidationmechanisms: consecutiveS0rebreakcloses vsstalledpositionwithpeak<.5R. Firstnativeexitwins. Sameentry,stop,planned2Rtarget,allbidirectional.',
        parent_results_sha256=x.sha(x.RUN/'main-results.json'),script_sha256=x.sha(__file__)))
    exe=x.R(x.RUN/'binary.json');assert x.sha(exe['path'])==exe['sha256']
    for per in ['early','recent','july']:
        dest=RUN/per;dest.mkdir();source=x.RUN/per;x.b.z.s.m.old.write(dest/(per+'-pool.json'),x.R(source/(per+'-pool.json')))
        proto=x.R(source/'execution.protocol.json');proto['templates']=[template];x.b.z.s.m.old.write(dest/'execution.protocol.json',proto)
        subprocess.run([exe['path'],str(dest/'execution.protocol.json'),per,'replay',str(dest/'executions')],check=True)
    ctl,meta,data,ends,qa=x.load();fees=x.R(x.r.SNAP/'fees.json')
    for per in ['early','recent','july']:
        cal=x.b.z.s.TradingDates({});cal.dates=x.r.f.t.period_paths(per)[3]
        for line in (RUN/per/'executions/evaluations.jsonl').open(encoding='utf-8'):
            row=x.b.z.s.reprice(json.loads(line),fees,cal);internal,suffix=row['scenario'].split('/',1);assert internal=='REFAIL2';row.update(period=per,scenario='F2S8/'+suffix);data['F2S8',suffix].append(row)
    cases=[pol+'/'+mode+'/slip'+str(s) for pol in p['policies'] for mode in ['corrected_touch','next_minute_open'] for s in p['slips']];july={}
    for combined in [False,True]:
        result={};accepted={}
        for case in cases:
            policy,suffix=case.split('/',1);aa,_=x.admit(data,suffix,'F2S8',ctl,meta,ends,policy,combined);q=x.b.z.metric(aa);q['blocks']={z['label']:x.b.z.metric([v for v in aa if z['start']<=v['warning_ts']<z['end']]) for z in x.b.z.x.blocks()};result[case+'/F2S8']=q;accepted[case]=aa
            if combined:july[case+'/F2S8']=x.b.z.metric([v for v in aa if '2026-07-13'<=v['warning_ts']<'2026-07-25'])
        write('combined-results.json' if combined else 'main-results.json',result)
        if not combined:
            with (RUN/'accepted-main-primary.jsonl').open('x',encoding='utf-8') as out:
                for q in accepted[p['primary']]:out.write(json.dumps(dict(model='F2S8',row=q),ensure_ascii=False)+'\n')
            # Exactsamepriorselectorwithoutfrequencyrelaxation.
            dates=x.R(x.b.RUN/'generation-checks.json')['covered_dates'];parent=x.R(x.RUN/'main-results.json');schedule=[]
            for start,end in x.b.FOLDS:
                ndays=sum(d<start[:10] for d in dates);zs={case:x.b.z.metric(x.b.z.x.visible(accepted[case],start)) for case in cases};primary=zs[p['primary']];why=[]
                if primary['n']<20 or primary['n']/ndays<.35:why.append('train_frequency')
                if min(v['normalized_net'] for v in zs.values())<=0:why.append('stress_nonpositive')
                if primary['normalized_ex_best']<=0:why.append('ex_best_nonpositive')
                for case in cases:
                    policy,suffix=case.split('/',1);base,_=x.admit(data,suffix,'BASE',ctl,meta,ends,policy,False);q=x.b.z.metric(x.b.z.x.visible(base,start))
                    if zs[case]['normalized_net']<q['normalized_net']-1e-7:why.append('below_frozen_baseline');break
                schedule.append(dict(start=start,end=end,chosen='BASE' if why else 'F2S8',rejections=why))
            write('chronological-selection.json',schedule)
    write('july-results.json',july)
    q=x.R(RUN/'main-results.json')[p['primary']+'/F2S8'];print(json.dumps({k:q[k] for k in ['n','net','normalized_net','normalized_realized_dd','unknown']}),flush=True)

if __name__=='__main__':run()
