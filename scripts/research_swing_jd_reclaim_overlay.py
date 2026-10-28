"""Sequential union audit: E20 reference plus true-S0 reclaim, same JD slot."""
import collections
import copy
import json
import research_swing_jd_failure_reclaim as c

RUN=c.RUN/'overlay'
MODELS=['E20_PLUS_RECLAIM_S1','E20_PLUS_RECLAIM_R2']
write=lambda n,x:c.b.z.s.m.old.write(RUN/n,x)

def run():
    RUN.mkdir(exist_ok=False);p=c.R(c.RUN/'protocol.json');primary=p['primary'];cases=c.f.cases(p)
    write('protocol.json',dict(models=MODELS,tie_priority='OriginalE20beforeReclaim atsamewarningclock, viaexplicitkeyprefix.',
        declaration='Sequential AFTER observing constituentresults. No parameterselection orbestexitscan; testbothalreadydeclaredS1/R2variants. Merge ALLraworderdecisions thenreruncontinuousoneJDslot+sameparentA2settledstops. No summation ofstandaloneprofits.',
        source_results_sha256=c.sha(c.RUN/'main-results.json'),script_sha256=c.sha(__file__)))
    results={};july={};attribution={};combined={}
    for include in [False,True]:
        ctl,meta,data,ends,_=c.load(include);accepted={}
        for case in cases:
            pol,sf=case.split('/',1)
            for model,target in zip(MODELS,['S1','R2']):
                stream=[];mcopy=dict(meta)
                for prefix,source in [('0','E20S1'),('1','S0_RECLAIM_'+target)]:
                    for original in data[source,sf]:
                        row=copy.deepcopy(original);key=prefix+'|'+row['event_key'];mcopy[key]=meta[row['event_key']];row.update(original_event_key=row['event_key'],event_key=key,overlay_source=source);stream.append(row)
                aa,_=c.b.admit(stream,ends,pol,ctl,mcopy);accepted[case,model]=aa;q=c.b.z.metric(aa);q['blocks']={f['label']:c.b.z.metric([r for r in aa if f['start']<=r['warning_ts']<f['end']]) for f in c.b.z.x.blocks()}
                (combined if include else results)[case+'/'+model]=q
                if include:july[case+'/'+model]=c.b.z.metric([r for r in aa if '2026-07-13'<=r['warning_ts']<'2026-07-25'])
        if not include:
            with (RUN/'accepted-main-primary.jsonl').open('x',encoding='utf-8') as out:
                for m in MODELS:
                    for row in accepted[primary,m]:out.write(json.dumps(dict(model=m,row=row),ensure_ascii=False)+'\n')
            pol,sf=primary.split('/',1);base,_=c.b.admit(data['E20S1',sf],ends,pol,ctl,meta);base={r['event_key']:r for r in base if c.EV(r)['status']=='closed'}
            for m in MODELS:
                aa=[r for r in accepted[primary,m] if c.EV(r)['status']=='closed'];kept={r['original_event_key']:r for r in aa if r['overlay_source']=='E20S1'};new=[r for r in aa if r['overlay_source']!='E20S1'];removed=base.keys()-kept.keys()
                cash=lambda r:c.EV(r)['net_amount'];delta=sum(map(cash,aa))-sum(map(cash,base.values()));q=dict(kept_original_n=len(kept),new_reclaim_n=len(new),new_reclaim_net=sum(map(cash,new)),removed_original_n=len(removed),removed_original_net=sum(cash(base[k]) for k in removed),total_net_delta=delta)
                assert abs(delta-q['new_reclaim_net']+q['removed_original_net'])<1e-6;attribution[m]=q
    write('main-results.json',results);write('combined-results.json',combined);write('july-results.json',july);write('incremental-attribution.json',attribution)
    print(json.dumps(dict(results={m:{k:results[primary+'/'+m][k] for k in ['n','net','normalized_net','unknown']} for m in MODELS},attribution=attribution)))

if __name__=='__main__':run()
