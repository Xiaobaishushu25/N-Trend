"""One internal-B confirmation hypothesis after the mechanism audit; no macro gate."""
import argparse,collections,copy,datetime as dt,json,pathlib,shutil,subprocess,math
import research_swing_abstop as s
ROOT=s.RUN;RUN=ROOT/'b-confirmation'

def anchor(snap,bars,index):
    e=snap['event'];sign=1 if e['direction']=='up' else -1;j=index[e['s1_ts']];w=index[e['warning_ts']]
    candidates=[b for b in bars[j+1:w] if (b['close']-b['open'])*sign<0]
    if not candidates:return dict(required=False,level=e['entry'],anchor_ts=None)
    # Stable tie break: the more recent equal-size body is used, all before warning.
    b=max(candidates,key=lambda b:(abs(b['close']-b['open']),b['datetime']));tick=e['tick_size'];price=b['open']+sign*tick
    price=(math.ceil(price/tick-1e-9) if sign>0 else math.floor(price/tick+1e-9))*tick
    ratio=snap['mechanism'].get('b_weakening_ex_warning');unproven=ratio is None or ratio>.75
    required=e['warning_kind']=='strong' and unproven and (price-e['entry'])*sign>1e-9
    return dict(required=required,level=price if required else e['entry'],anchor_ts=b['datetime'],anchor_open=b['open'],not_weakened=unproven)

def prepare():
    RUN.mkdir(exist_ok=False);fork=RUN/'fork';shutil.copytree(ROOT/'fork',fork)
    runner=(ROOT/'fork/crates/n-core/src/bin/swing_bextreme_runner.rs').read_text(encoding='utf-8')
    runner=s.replace_once(runner,'            let wide = template["wide"].as_bool().unwrap();','''            if template["b_anchor"]==true && snap["b_anchor"]["required"]==true {
                ev.entry=snap["b_anchor"]["level"].as_f64().unwrap();geom::normalize(&mut ev);
            }
            let wide = template["wide"].as_bool().unwrap();''')
    runner=s.replace_once(runner,'                rules.next_minute_open = mode == "next_minute_open";','''                rules.next_minute_open = mode == "next_minute_open";
                if template["b_anchor"]==true && snap["b_anchor"]["required"]==true {
                    rules.entry_mode=ablation::EntryMode::CloseConfirmNextOpen;
                }''')
    (fork/'crates/n-core/src/bin/swing_bconfirmation_runner.rs').write_text(runner,encoding='utf-8')
    p=s.read(ROOT/'protocol.json');base=p['templates'][0];templates=[]
    for name,adaptive,confirm in [('base',False,False),('anchor',False,True),('adaptive',True,False),('adaptive_anchor',True,True)]:
        t=copy.deepcopy(base);t.update(name='P2_confirm_'+name,variant=name,adaptive=adaptive,b_anchor=confirm,b_extreme=False);templates.append(t)
    audit={}
    for period in ('early','recent'):
        inp=s.read(p['periods'][period]['input']);bars={k:s.aggregate15(v['bars']) for k,v in inp['symbols'].items()};index={k:{b['datetime']:i for i,b in enumerate(v)} for k,v in bars.items()}
        pool=s.read(ROOT/(period+'-pool.json'))
        for snap in pool:snap['b_anchor']=anchor(snap,bars[snap['event']['symbol']],index[snap['event']['symbol']])
        audit[period]=dict(pool=len(pool),required=sum(x['b_anchor']['required'] for x in pool));s.write(RUN/(period+'-pool.json'),pool)
    p.update(id=RUN.name,created_at=dt.datetime.now(dt.timezone.utc).isoformat(),templates=templates,
        historical='Exploratory AFTER AB/stop audit; not independent validation or proof of pattern accuracy.',
        hypothesis='Only strong warnings with B weakening ratio unknown or>.75 AND warning entry has not cleared largest pre-warning opposing B-body open: raise planned entry one tick beyond that body open and require completed1m close then next1m open. Keep original S2 stop and nearby target; recheck costs/netRR, no fallback if selected opportunity fails. Wick/cumulative/other warnings unchanged. No A-breakout or per-product/month tuning.',
        source_results_sha256=s.sha(ROOT/'full-portfolio.json'),script_sha256=s.sha(__file__),anchor_counts=audit,
        production_changed=False,qualified_for_trading=False)
    s.write(RUN/'protocol.json',p);s.write(RUN/'implementation-manifest.json',{str(q.relative_to(fork)):s.sha(q) for q in fork.rglob('*') if q.is_file()});print('Frozen B-body confirmation',audit,flush=True)

def run():
    p=s.read(RUN/'protocol.json');assert s.sha(__file__)==p['script_sha256'];args=['--offline','--manifest-path',str(RUN/'fork/Cargo.toml'),'--target-dir',str(s.REPO/'target'),'-p','n-core']
    subprocess.run(['cargo','build',*args,'--bin','swing_bconfirmation_runner'],check=True)
    for period in ('early','recent'):
        assert all(s.sha(v)==p['hashes'][period][k] for k,v in p['periods'][period].items())
        subprocess.run([str(s.REPO/'target/debug/swing_bconfirmation_runner.exe'),str(RUN/'protocol.json'),period,'replay',str(RUN/period)],check=True)

def report():
    p=s.read(RUN/'protocol.json');fees=s.read(p['periods']['early']['fees']);cases=collections.defaultdict(list);events={};ends={};stats={policy:{q:{} for q in ('early','recent')} for policy in ('original','two_paths_flat')};accepted={};ledgers={}
    for period in ('early','recent'):
        events.update({x['event_key']:x['event'] for x in s.read(RUN/(period+'-pool.json'))});cal=s.TradingDates(s.read(p['periods'][period]['input'])['symbols'])
        for code,t in s.read(RUN/period/'manifest.json')['contract_ends'].items():ends[code]=max(t,ends.get(code,''))
        for line in (RUN/period/'evaluations.jsonl').open(encoding='utf-8'):
            r=s.reprice(json.loads(line),fees,cal);r['period']=period
            if s.ev(r)['status']=='closed':r['exit_trading_day']=cal.day(s.ev(r)['exit_ts'])
            cases[r['scenario']].append(r)
    previous=s.read(ROOT/'full-portfolio.json');checks=0
    for case,rows in cases.items():
        for policy in stats:
            a,l=s.portfolio(rows,events,ends) if policy=='original' else s.flat.exposure_portfolio(rows,ends);accepted[(policy,case)]=a;ledgers[(policy,case)]=l
            for q in ('early','recent'):
                m=s.describe([r for r in a if r['period']==q],events);stats[policy][q][case]=m
                if case.split('/')[0] in ('P2_confirm_base','P2_confirm_adaptive'):
                    old=previous[policy][q][case.replace('P2_confirm_','P2_ab_')];assert m==old,(policy,q,case);checks+=1
    s.write(RUN/'results.json',stats);s.write(RUN/'opportunity-ledgers.json',{str(k):v for k,v in ledgers.items()})
    with (RUN/'accepted.jsonl').open('x',encoding='utf-8') as f:
        for (policy,case),rows in accepted.items():
            for r in rows:f.write(json.dumps(dict(policy=policy,case=case,period=r['period'],row=r),ensure_ascii=False)+'\n')
    assert all(s.sha(k)==h for k,h in p['production_before'].items());assert all(s.sha(RUN/'fork'/k)==h for k,h in s.read(RUN/'implementation-manifest.json').items())
    s.write(RUN/'verification.json',dict(control_scenarios_exact=checks,production_unchanged=len(p['production_before']),qualified_for_trading=False,results_sha256=s.sha(RUN/'results.json')))
    print([(q,k,round(v['net'],2),round(v['mean_r'],4),v['n']) for q in ('early','recent') for k,v in stats['original'][q].items() if k.endswith('corrected_touch/slip1')],flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('stage',choices=['prepare','run','report']);globals()[p.parse_args().stage]()
