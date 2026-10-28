"""Post-outcome robustness checks; no new rules or threshold selection."""
import collections,datetime as dt,hashlib,json
import research_swing_portfolio_selective as r
import research_swing_abstop as s
from swing_portfolio_selective import select
from research_swing_consistency import cluster
from report_swing_c_structure import SUFFIXES
w=r.w;RUN=r.RUN;MODELS=['sector2_counter','sector2_counter_stops'];SEEDS=[17,29,43]

def run():
    p=w.read(RUN/'protocol.json');out=RUN/'robustness-plan-before-checks.json'
    if not out.exists():w.write(out,dict(created_at=dt.datetime.now(dt.timezone.utc).isoformat(),after_variant_outcomes=True,models=MODELS,checks=['Each mapped product excluded BEFORE full chronological recompetition; source economic profile fixed to prior best budget_body','Three additional deterministic arbitrary tie permutations, only equal trigger and warning times affected','Known-closed repeated-wave first leg vs subsequent legs, descriptive only'],seeds=SEEDS,no_additional_strategy_or_parameter=True,script_sha256=w.sha(__file__)))
    else:
        assert not (RUN/'additional-robustness.json').exists()
        w.write(RUN/'validation-metadata-fix.json',dict(reason='Metrics require exit_trading_day for closed trades. Initial attempt failed on missing metadata before robustness outputs; populate same trading-calendar metadata as shared report. No strategy or permutation changes.',before_sha256=w.sha(RUN/'validation-before-metadata-fix.py'),after_sha256=w.sha(__file__)))
    events={};ends={};raw=collections.defaultdict(list);fees=w.read(p['periods']['early']['fees'])
    for q in ['early','recent']:
        paths=p['sources'][q];assert w.sha(paths['raw'])==paths['raw_sha256'] and w.sha(paths['pool'])==paths['pool_sha256'];events.update({z['event_key']:z['event'] for z in w.read(paths['pool'])});cal=s.TradingDates(w.read(p['periods'][q]['input'])['symbols'])
        for c,end in w.read(paths['manifest'])['contract_ends'].items():ends[c]=max(end,ends.get(c,''))
        for line in open(paths['raw'],encoding='utf-8'):
            z=json.loads(line)
            if not z['scenario'].startswith('P2_economics_budget_body/'):continue
            sf=z['scenario'].split('/',1)[1];z=s.reprice(z,fees,cal);z['period']=q
            if s.ev(z)['status']=='closed':z['exit_trading_day']=cal.day(s.ev(z)['exit_ts'])
            raw[sf].append(z)
    ref=w.read(RUN/'results.json');loo=[];ties=[];checks=0
    for pol in ['original','two_paths_flat']:
        for sf in SUFFIXES:
            rows=raw[sf]
            for model in ['base']+MODELS:
                aa,_,_=select(rows,events,ends,p['sector_map'],model,pol);cash=s.metrics(aa)['net'];assert abs(cash-ref[pol]['combined']['budget_body/'+model+'/'+sf]['net'])<1e-7;checks+=1
            for omitted in sorted(p['sector_map']):
                keep=[z for z in rows if z['symbol']!=omitted];bb,_,_=select(keep,events,ends,p['sector_map'],'base',pol);b=s.metrics(bb)['net']
                for model in MODELS:
                    aa,_,_=select(keep,events,ends,p['sector_map'],model,pol);m=s.metrics(aa)['net'];loo.append(dict(policy=pol,suffix=sf,model=model,omitted=omitted,base=b,net=m,delta=m-b))
            for seed in SEEDS:
                shuffled=[];ee={}
                for z in rows:
                    newkey=hashlib.sha256((str(seed)+'/'+z['event_key']).encode()).hexdigest();v=dict(z,event_key=newkey);shuffled.append(v);ee[newkey]=events[z['event_key']]
                bb,_,_=select(shuffled,ee,ends,p['sector_map'],'base',pol);b=s.metrics(bb)['net']
                for model in MODELS:
                    aa,_,_=select(shuffled,ee,ends,p['sector_map'],model,pol);m=s.metrics(aa)['net'];ties.append(dict(policy=pol,suffix=sf,model=model,seed=seed,base=b,net=m,delta=m-b))
    primary='corrected_touch/slip1';aa,ll,_=select(raw[primary],events,ends,p['sector_map'],'base','original');groups=collections.defaultdict(list)
    for z in aa:
        if s.ev(z)['status']=='closed':groups[cluster(events[z['event_key']])].append(z)
    repeats={k:sorted(v,key=lambda z:(s.ev(z)['entry_ts'],z['event_key'])) for k,v in groups.items() if len(v)>1}
    first=[v[0] for v in repeats.values()];later=[z for v in repeats.values() for z in v[1:]]
    wave_diag=dict(repeated_known_closed_clusters=len(repeats),first_legs=s.metrics(first),subsequent_legs=s.metrics(later),warning='Cluster membership uses future repeated trades; descriptive attribution only. Negative repeated-cluster total includes first losses; not the marginal value of retries. Actual first-attempt veto differs because trigger attempts can remain unfilled.')
    detail={};base={z['event_key']:z for z in aa if s.ev(z)['status']=='closed'}
    for model in MODELS:
        keep,ledger,_=select(raw[primary],events,ends,p['sector_map'],model,'original');keys={z['event_key'] for z in keep};removed=[z for k,z in base.items() if k not in keys];lk={z['event_key']:z for z in ledger}
        detail[model]=[dict(event_key=z['event_key'],symbol=z['symbol'],period=z['period'],net=s.ev(z)['net_amount'],warning_ts=z['warning_ts'],entry_ts=s.ev(z)['entry_ts'],exit_ts=s.ev(z)['exit_ts'],exit_reason=s.ev(z)['exit_reason'],decision=lk[z['event_key']]) for z in removed]
    summary={m:dict(leave_product_positive=sum(z['delta']>1e-7 for z in loo if z['model']==m),leave_product_total=sum(z['model']==m for z in loo),leave_product_min=min(z['delta'] for z in loo if z['model']==m),leave_product_max=max(z['delta'] for z in loo if z['model']==m),tie_positive=sum(z['delta']>1e-7 for z in ties if z['model']==m),tie_total=sum(z['model']==m for z in ties),tie_min=min(z['delta'] for z in ties if z['model']==m),tie_max=max(z['delta'] for z in ties if z['model']==m)) for m in MODELS}
    w.write(RUN/'additional-robustness.json',dict(control_metrics_exact=checks,summary=summary,leave_product_recompetition=loo,seeded_tie_permutations=ties,note='Internal perturbations of already searched history; not independent heldout instruments or new-time validation. All mapped-product leave-outs reported; no leave-out-dependent policy selection.'))
    w.write(RUN/'mechanism-diagnosis.json',dict(repeated_waves=wave_diag,removed_trade_details=detail))
    print(json.dumps(dict(summary=summary,repeated_clusters=wave_diag['repeated_known_closed_clusters'],first_net=wave_diag['first_legs']['net'],subsequent_net=wave_diag['subsequent_legs']['net']),indent=2),flush=True)

if __name__=='__main__':run()
