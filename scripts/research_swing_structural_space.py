"""Isolate structural-space selection from widening a tactical stop."""
import argparse,collections,copy,datetime as dt,json,math
import research_swing_abstop as s
ROOT=s.RUN;RUN=ROOT/'structural-space'

def structural_rr(row,stop,fee):
    sign=1 if row['direction']=='up' else -1;tick=row['tick_size'];mult=row['multiplier'];entry=row['entry']+sign*tick
    target=row['target']-sign*tick;stop_fill=stop-sign*tick
    def charge(exit):
        if 'roundtrip_fixed_yuan_per_lot' in fee:return fee['roundtrip_fixed_yuan_per_lot']
        leg=lambda k,p:fee[k]['fixed_yuan_per_lot']+fee[k]['notional_rate']*p*mult
        return leg('open',entry)+max(leg('close_today',exit),leg('close_yesterday',exit))
    reward=(target-entry)*sign*mult-charge(target);loss=(entry-stop_fill)*sign*mult+charge(stop_fill)
    return reward/loss if loss>0 else None

def prepare():
    RUN.mkdir(exist_ok=False);p=s.read(ROOT/'protocol.json');p.update(id=RUN.name,created_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        templates=['base','space_base','adaptive','space_adaptive'],script_sha256=s.sha(__file__),
        frozen_sources={str(ROOT/q/'evaluations.jsonl'):s.sha(ROOT/q/'evaluations.jsonl') for q in ('early','recent')},
        hypothesis='Use full pre-warning B extreme only to assess forecast net reward/loss>=1, with adverse1tick entry/exit and worse close fees. Keep original tactical S2 stop and original entry/target/exits. No actual-fill/outcome labels used for filter, no fallback. Same fixed gate in both cost stress scenarios. Separate from the failed full widened-stop policy.',
        historical='Post-outcome exploratory ablation after B extreme stop and B confirmation results; not independent validation.',production_changed=False,qualified_for_trading=False)
    s.write(RUN/'protocol.json',p);print(RUN,flush=True)

def run():
    p=s.read(RUN/'protocol.json');assert s.sha(__file__)==p['script_sha256'];assert all(s.sha(k)==v for k,v in p['frozen_sources'].items())
    fees=s.read(p['periods']['early']['fees']);events={};stops={};ends={};cases=collections.defaultdict(list);counts=collections.Counter();stats={policy:{q:{} for q in ('early','recent')} for policy in ('original','two_paths_flat')};accepted={}
    for q in ('early','recent'):
        for snap in s.read(ROOT/(q+'-pool.json')):events[snap['event_key']]=snap['event'];stops[snap['event_key']]=snap['mechanism']['true_b_stop']
        for code,t in s.read(ROOT/q/'manifest.json')['contract_ends'].items():ends[code]=max(t,ends.get(code,''))
        cal=s.TradingDates(s.read(p['periods'][q]['input'])['symbols'])
        for line in (ROOT/q/'evaluations.jsonl').open(encoding='utf-8'):
            row=json.loads(line);name,suffix=row['scenario'].split('/',1)
            if name not in ('P2_ab_base','P2_ab_adaptive'):continue
            variant=name.removeprefix('P2_ab_');rr=structural_rr(row,stops[row['event_key']],fees['products'][row['symbol'].rstrip('0123456789')]);row=s.reprice(row,fees,cal);row['period']=q
            if s.ev(row)['status']=='closed':row['exit_trading_day']=cal.day(s.ev(row)['exit_ts'])
            cases[variant+'/'+suffix].append(row)
            if rr is not None and rr>=1.-1e-9:cases['space_'+variant+'/'+suffix].append(row)
            else:counts[(q,variant)]+=1
    prev=s.read(ROOT/'full-portfolio.json');checks=0
    for case,rows in cases.items():
        for policy in stats:
            a,_=s.portfolio(rows,events,ends) if policy=='original' else s.flat.exposure_portfolio(rows,ends);accepted[(policy,case)]=a
            for q in ('early','recent'):
                m=s.describe([r for r in a if r['period']==q],events);stats[policy][q][case]=m
                if case.split('/')[0] in ('base','adaptive'):
                    assert m==prev[policy][q]['P2_ab_'+case];checks+=1
    s.write(RUN/'results.json',stats);s.write(RUN/'filter-counts.json',{str(k):v for k,v in counts.items()})
    with (RUN/'accepted.jsonl').open('x',encoding='utf-8') as f:
        for (policy,case),rows in accepted.items():
            for row in rows:f.write(json.dumps(dict(policy=policy,case=case,period=row['period'],row=row),ensure_ascii=False)+'\n')
    assert all(s.sha(k)==h for k,h in p['production_before'].items())
    s.write(RUN/'verification.json',dict(control_scenarios_exact=checks,production_unchanged=len(p['production_before']),qualified_for_trading=False,results_sha256=s.sha(RUN/'results.json')))
    print([(q,k,round(v['net'],2),round(v['mean_r'],4),v['n']) for q in ('early','recent') for k,v in stats['original'][q].items() if k.endswith('corrected_touch/slip1')],flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('stage',choices=['prepare','run']);globals()[p.parse_args().stage]()
