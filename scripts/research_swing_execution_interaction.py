"""Factorial control: confirmation vs session failure, without changing stop geometry."""
import argparse,copy,datetime as dt,json,pathlib
import audit_swing_flat_ambiguity as a
import research_swing_selective as s
RUN=s.RUN/'execution-interaction'
NAMES=['base','counter_kind','counter_kind_session','opposed_kind_session','strong_kind_session']

def choose(name,e):
    st=s.state(e)
    if name=='base' or not st['counter']:return 'base'
    if name=='counter_kind':return 'kind'
    active=name=='counter_kind_session' or name=='opposed_kind_session' and st['opposed'] or name=='strong_kind_session' and st['strong_counter']
    return 'kind_loss' if active else 'kind'

def prepare():
    RUN.mkdir(exist_ok=False);p=s.read(a.RUN/'protocol.json');p['profiles']={k:p['profiles'][k] for k in ('base','kind','kind_loss')}
    p.update(id=RUN.name,created_at=dt.datetime.now(dt.timezone.utc).isoformat(),cases=NAMES,audit_sha256=s.sha(a.__file__),supervisor_sha256=s.sha(__file__),
        rationale='After session/flatness findings, isolate kind-confirm plus session rule from tighter stop geometry. Fixed all-counter / opposed-only / strong-counter-only session activation. No new thresholds, warning filters or stop changes.',
        raw_source_sha256={str(pathlib.Path(v['root'])/period/'evaluations.jsonl'):s.sha(pathlib.Path(v['root'])/period/'evaluations.jsonl') for v in p['profiles'].values() for period in ('early','recent')},
        historical='Post-outcome exploratory interaction control, never independent validation.',qualified_for_trading=False,production_changed=False)
    s.write(RUN/'protocol.json',p);print(RUN,flush=True)

def run():
    p=s.read(RUN/'protocol.json');assert s.sha(__file__)==p['supervisor_sha256'] and s.sha(a.__file__)==p['audit_sha256']
    s.PROFILES={k:(pathlib.Path(v['root']),v['scenario']) for k,v in p['profiles'].items()};events,periods,ends,mapping,funnel=s.load(p)
    results={policy:{q:{} for q in ('early','recent')} for policy in ('original','two_paths_flat')};ledgers={};accepted={};leave={}
    for name in NAMES:
        for mode in ('corrected_touch','next_minute_open'):
            for slip in (1,2):
                suffix=f'{mode}/slip{slip}';case=name+'/'+suffix;rows=[mapping[(k,suffix,choose(name,e))] for k,e in events.items()]
                for policy in results:
                    selector=(lambda rr,reverse=False:s.portfolio(rr,events,ends,reverse=reverse)) if policy=='original' else (lambda rr,reverse=False:a.exposure_portfolio(rr,ends,reverse))
                    zz,ll=selector(rows);rev,_=selector(rows,True);ledgers[(policy,case)]=ll;accepted[(policy,case)]=zz;leave[(policy,case)]={}
                    for q in ('early','recent'):
                        m=s.describe([r for r in zz if r['period']==q],events);m['reverse_tie_net']=s.metrics([r for r in rev if r['period']==q])['net'];results[policy][q][case]=m
                    for sym in sorted({e['symbol'] for e in events.values()}):
                        aa,_=selector([r for r in rows if r['symbol']!=sym]);leave[(policy,case)][sym]={q:s.metrics([r for r in aa if r['period']==q]) for q in ('early','recent')}
    # Exact controls against completed audit, under both exposure policies.
    for policy,root in [('original',s.RUN),('two_paths_flat',a.RUN)]:
        previous=s.read(root/'results.json')
        for name in ('base','counter_kind'):
            for q in ('early','recent'):
                for mode in ('corrected_touch','next_minute_open'):
                    for slip in (1,2):
                        case=f'{name}/{mode}/slip{slip}';m=results[policy][q][case];b=previous[q][case]
                        assert all(m[k]==b[k] for k in b if k in m and k!='reverse_tie_net'),(policy,q,case)
    s.write(RUN/'results.json',results);s.write(RUN/'opportunity-ledgers.json',{str(k):v for k,v in ledgers.items()});s.write(RUN/'leave-product-out.json',{str(k):v for k,v in leave.items()})
    with (RUN/'accepted.jsonl').open('x',encoding='utf-8') as f:
        for (policy,case),rows in accepted.items():
            for r in rows:f.write(json.dumps(dict(policy=policy,case=case,period=r['period'],row=r),ensure_ascii=False)+'\n')
    assert all(s.sha(k)==h for k,h in p['production_before'].items())
    s.write(RUN/'verification.json',dict(control_scenarios_exact=32,source_profiles_unchanged=True,production_unchanged=len(p['production_before']),qualified_for_trading=False,
        results_sha256=s.sha(RUN/'results.json'),protocol_sha256=s.sha(RUN/'protocol.json')))
    for name in NAMES:
        print(name,[(policy,q,round(results[policy][q][name+'/corrected_touch/slip1']['net'],2),round(results[policy][q][name+'/corrected_touch/slip1']['mean_r'],4)) for policy in results for q in ('early','recent')],flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('stage',choices=['prepare','run']);globals()[p.parse_args().stage]()
