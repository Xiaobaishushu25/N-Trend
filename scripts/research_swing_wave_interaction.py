"""Small post-discovery interaction: near-A + conditional B confirmation."""
import argparse,collections,copy,datetime as dt,json,shutil,subprocess
import research_swing_wave_selection as w
import research_swing_abstop as s
RUN=w.RUN/'execution-interaction';WAVES=['legacy','near_start']

def prepare():
    RUN.mkdir(exist_ok=False);parent=w.read(w.RUN/'protocol.json');templates=[];base=next(t for t in parent['templates'] if t['variant']=='adaptive')
    for name,confirm,allk in [('adaptive',False,False),('adaptive_b_strong',True,False),('adaptive_b_all',True,True)]:
        t=copy.deepcopy(base);t.update(name='P2_wave_'+name,variant=name,b_confirm=confirm,b_all=allk);templates.append(t)
    p=copy.deepcopy(parent);p.update(id=RUN.name,created_at=dt.datetime.now(dt.timezone.utc).isoformat(),templates=templates,waves=WAVES,
        parent_results_sha256=w.sha(w.RUN/'results.json'),script_sha256=w.sha(__file__),executor_sha256=w.sha(w.REPO/'target/release/swing_wave_execution.exe'),
        historical='Exploratory AFTER main model outcomes, no heldout claim.',
        interaction='Add B-unweakened confirmation to existing adaptive execution, so only aligned/missing-hour opportunities not already confirmed can change. Same .75 weakening and original quote/cost/stop/target rules; no new threshold.',
        production_changed=False,qualified_for_trading=False)
    w.write(RUN/'protocol.json',p)
    for wave in WAVES:
        folder=RUN/wave;folder.mkdir();w.write(folder/'protocol.json',p)
        for q in ('early','recent'):shutil.copy2(w.RUN/wave/(q+'-pool.json'),folder/(q+'-pool.json'))
    print(RUN,flush=True)

def run():
    p=w.read(RUN/'protocol.json');assert w.sha(__file__)==p['script_sha256'];assert w.sha(w.REPO/'target/release/swing_wave_execution.exe')==p['executor_sha256']
    for q in ('early','recent'):
        assert all(w.sha(v)==p['hashes'][q][k] for k,v in p['periods'][q].items())
        for wave in WAVES:
            folder=RUN/wave;assert w.sha(folder/(q+'-pool.json'))==w.sha(w.RUN/wave/(q+'-pool.json'))
            subprocess.run([str(w.REPO/'target/release/swing_wave_execution.exe'),str(folder/'protocol.json'),q,'replay',str(folder/q)],check=True)

def report():
    p=w.read(RUN/'protocol.json');fees=w.read(p['periods']['early']['fees']);old=w.read(w.RUN/'results.json');stats={policy:{q:{} for q in ('early','recent')} for policy in ('original','two_paths_flat')};accepted={};qa=collections.Counter();leave={}
    for wave in WAVES:
        folder=RUN/wave;events={};cases=collections.defaultdict(list);ends={}
        for q in ('early','recent'):
            pool=w.read(folder/(q+'-pool.json'));snaps={x['event_key']:x for x in pool};events.update({k:x['event'] for k,x in snaps.items()});cal=s.TradingDates(w.read(p['periods'][q]['input'])['symbols']);reference={}
            for code,t in w.read(folder/q/'manifest.json')['contract_ends'].items():ends[code]=max(t,ends.get(code,''))
            for line in (folder/q/'evaluations.jsonl').open(encoding='utf-8'):
                r=json.loads(line);profile,suffix=r['scenario'].split('/',1);profile=profile.removeprefix('P2_wave_');snap=snaps[r['event_key']];e=snap['event'];h=(e.get('market_context') or {}).get('trend_alignment_60');weak=snap['b_weakening_ex_warning'];weak=weak is not None and weak<=.75
                key=(r['event_key'],suffix)
                if profile=='adaptive':reference[key]=r['result']
                elif weak or e['warning_kind']=='cumulative' or profile=='adaptive_b_strong' and e['warning_kind']!='strong' or h is not None and h<=0:
                    assert r['result']==reference[key];qa['unchanged_routes_exact']+=1
                r=s.reprice(r,fees,cal);r['period']=q
                if s.ev(r)['status']=='closed':r['exit_trading_day']=cal.day(s.ev(r)['exit_ts']);qa['closed_fills_repriced']+=1
                cases[profile+'/'+suffix].append(r)
        for case,rows in cases.items():
            label=wave+'/'+case
            for policy in stats:
                a,_=s.portfolio(rows,events,ends) if policy=='original' else s.flat.exposure_portfolio(rows,ends);accepted[(policy,label)]=a
                for q in ('early','recent'):
                    m=s.describe([r for r in a if r['period']==q],events);stats[policy][q][label]=m
                    if case.startswith('adaptive/'):
                        b=old[policy][q][label];assert all(m[k]==b[k] for k in m if k in b and k!='reverse_tie_net');qa['full_controls_exact']+=1
                leave[(policy,label)]={sym:{q:s.metrics([r for r in a if r['period']==q and r['symbol']!=sym]) for q in ('early','recent')} for sym in sorted({e['symbol'] for e in events.values()})}
    w.write(RUN/'results.json',stats);w.write(RUN/'leave-product-out.json',{str(k):v for k,v in leave.items()})
    with (RUN/'accepted.jsonl').open('x',encoding='utf-8') as f:
        for (policy,case),rows in accepted.items():
            for row in rows:f.write(json.dumps(dict(policy=policy,case=case,period=row['period'],row=row),ensure_ascii=False)+'\n')
    assert all(w.sha(k)==v for k,v in p['production_before'].items());w.write(RUN/'verification.json',dict(qa=dict(qa),production_unchanged=len(p['production_before']),qualified_for_trading=False,results_sha256=w.sha(RUN/'results.json')))
    print([(q,k,round(v['net'],2),round(v['mean_r'],4),v['n']) for q in ('early','recent') for k,v in stats['original'][q].items() if k.endswith('corrected_touch/slip1')],flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('stage',choices=['prepare','run','report']);globals()[p.parse_args().stage]()
