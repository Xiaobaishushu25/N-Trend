"""Predeclared grouped strategy research, using the shared Rust OHLC executor.

Stages: prepare -> Rust replay -> discover (development only) -> validate.
Old history has been researched before; held-out here means local selection
holdout, never a claim of newly observed independent evidence.
"""
import argparse
import bisect
import collections
import copy
import datetime as dt
import hashlib
import json
import math
import pathlib
import random
import statistics

from entry_model import completed_context, commission_bound
from report_signal_ablation import jsonl, key, read_json
from report_paper_candidate_history import summary, csv_write
from report_breadth_research import symmetric_trim
from reprice_signal_costs import TradingDates, reprice
from validate_execution_research import capacity, evaluation, occupancy

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROTOCOL = ROOT/'research-protocols/strategy-robustness-20261024-v1.json'
RUN = ROOT/'research-runs/strategy-robustness-20261024-v1'
SOURCE = ROOT/'research-runs/server-execution-20261023-input-v1.json'
SIGNALS = ROOT/'research-runs/server-execution-20261023-rebuild-v1/signals.json'
FEES = ROOT/'scripts/exchange_fees_20261022.json'
HOLIDAYS = ROOT/'research-protocols/holiday-windows-2026-jul-oct-v1.json'
UNKNOWN = {'ambiguous','missing_data','rollover','unfinished','pending'}


def digest(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, allow_nan=False)


def week(day):
    d = dt.date.fromisoformat(day[:10])
    return (d-dt.timedelta(days=d.weekday())).isoformat()


def definitions():
    templates = []
    def add(name, cap=None, policy='original_s1', nf=5, **rules):
        templates.append(dict(name=name, policy=policy, no_follow_bar=nf,
            uniform_two_lot=policy=='actual_tier', rules=dict(target_cap_r=cap,
            min_actual_risk_ticks=10., flat_before_weekend=True, **rules)))
    add('P2'); add('P3', 2.)
    for cap in (1.5, 2.5, 3.): add(f'cap{cap}', cap)
    add('protect_s1', policy='s1_protect_one_r')
    add('protect_cap2', 2., policy='s1_protect_one_r')
    for base, cap in (('s1',None),('cap2',2.)):
        add('confirm_'+base, cap, entry_mode='close_confirm_next_open')
        add('retest_'+base, cap, entry_mode='breakout_retest_quarter')
        add('nf_off_'+base, cap, nf=None)
    for nf in (3,8): add(f'nf{nf}_cap2',2.,nf=nf)
    for hold in (24,36): add(f'hold{hold}_cap2',2.,max_holding_buckets=hold)
    add('pending4_cap2',2.,pending_buckets=4)
    add('two_lot_legacy',policy='actual_tier')
    filters = [dict(name='base'),dict(name='score25',score=2.5),dict(name='score35',score=3.5)]
    for value in (2.5,3.5): filters.append(dict(name=f'a_score{value}',dim_a_min=value))
    for value in (3.,4.): filters.append(dict(name=f'b_score{value}',dim_b_min=value))
    for value in (.35,.55):
        filters.append(dict(name=f'a_eff{value}',a_eff_min=value))
        filters.append(dict(name=f'b_eff{value}',b_eff_min=value))
    for value in (.5,.66): filters.append(dict(name=f'retrace{value}',retracement_min=.2,retracement_max=value))
    for value in (.5,.8): filters.append(dict(name=f'b_speed{value}',speed_ratio_max=value))
    for weights in ([.4,.3,.3],[.4,.4,.2],[.7,.15,.15]):
        filters.append(dict(name='weight'+''.join(str(int(w*100)) for w in weights),weights=weights))
    candidates=[]
    for base in ('P2','P3'):
        for f in filters:
            candidates.append(dict(name=base if f['name']=='base' else base+'_'+f['name'],
                template=base,family='baseline' if f['name']=='base' else 'shape',filter=f,sizing='one'))
    for t in templates[2:]:
        candidates.append(dict(name=t['name'],template=t['name'],family='entry_exit',filter={},sizing='one'))
    for base in ('P2','P3'):
        for budget in (250,500):
            candidates.append(dict(name=f'{base}_risk{budget}',template=base,family='sizing',filter={},
                sizing='budget',risk_budget=budget,max_lots=3))
    candidates.append(dict(name='P2P3_equal_legs',template='P2P3_equal_legs',family='sizing',filter={},sizing='two_legs'))
    return templates,candidates


def prepare():
    source = read_json(SOURCE)
    calendar = TradingDates(source['symbols'])
    weeks = sorted({week(d) for d in calendar.dates if d<'2026-10-01'})
    shuffled = weeks.copy(); random.Random(20261024).shuffle(shuffled)
    split = {w: ('validation' if i%5==3 else 'test' if i%5==4 else 'development') for i,w in enumerate(shuffled)}
    templates,candidates=definitions()
    p=dict(id='strategy-robustness-20261024-v1',created_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        paths={k:str(p) for k,p in dict(input=SOURCE,signals=SIGNALS,fees=FEES,holidays=HOLIDAYS).items()},
        hashes={k:digest(p) for k,p in dict(input=SOURCE,signals=SIGNALS,fees=FEES,holidays=HOLIDAYS).items()},
        templates=templates,candidates=candidates,seed=20261024,week_splits=dict(sorted(split.items())),
        temporal_cut='2026-10-08',temporal_end='2026-10-23 11:05:00',
        split_contract='All symbols share trading-week groups. Entire warning-to-maximum-occupancy interval across ALL templates/slips must stay in one split. One-day embargo at each split transition. Full-history capacity runs before cohort attribution.',
        feature_contract='Actual A/B/warning score components reconstructed with the unchanged live warning generator; A/B body efficiency is sum(direction*body)/sum(abs(body)) over completed bars in each anchored leg. Existing warning pool score>=2.5 only; no new pivot/dedup logic.',
        selection=dict(families=['shape','entry_exit','sizing'],per_family=1,min_closed=40,min_baseline_fraction=.5,
            objective='min(mean_r_1,mean_r_2)*sqrt(min(n1,n2,100)/100)-.1*max(top3_positive_product_share)-.1*max(top3_positive_day_share)',
            gate='both slips positive cash and R; min closed; no validation/test/temporal outcomes in selection; retain top diagnostic even if gate fails'),
        qualification='validation, test and temporal EACH: >=10 closed at both slips; positive cash and mean R; candidate mean R and win rate >= P3 at both slips; full-history best3-day and best3-product removal positive, top3 positive product share<=.60; no promotion absent new prospective data',
        all_results_development_history=True,prior_history_already_inspected=True,new_independent_observations=0,
        production_changed=False,qualified_for_trading=False,
        costs='Frozen 2026-10-22 fees; SR roundtrip2yuan override. 1/2 tick per fill. No claim of current/historical broker billing.',
        sizing_contract='Integer risk budgets use warning entry+1tick, structural stop-1tick, conservative fees. floor(budget/loss), cap3, zero skips. Equal legs opens2 lots, 1 S1+1 cap2; normalized cash is per initial lot, not a tradable fractional position. No account capital cap.',
        repetitions=3000)
    write(PROTOCOL,p)
    print(json.dumps(dict(protocol=str(PROTOCOL),templates=len(templates),candidates=len(candidates),week_splits=p['week_splits'])))


def shape_snapshot(ev, dims, source, contexts, fees, multiplier):
    raw=source['symbols'][ev['symbol']]
    ctx=contexts[ev['symbol']].get(ev['warning_ts'])
    sign=1 if ev['direction']=='up' else -1
    def efficiency(start,end,direction,include_start=True):
        bars=[b for t,b in contexts[ev['symbol']].items() if (start<=t if include_start else start<t) and t<=end]
        bodies=[b['close']-b['open'] for b in bars]
        return direction*sum(bodies)/sum(map(abs,bodies)) if bodies and sum(map(abs,bodies))>0 else None
    f=dict(ev['setup_features'],**dims,retracement=ev['retracement'],
        a_eff=efficiency(ev['s0_ts'],ev['s1_ts'],sign),
        b_eff=efficiency(ev['s1_ts'],ev['s2_ts'],-sign,False),
        speed_ratio=(ev['b_move']/max(1,ev['b_bars']))/(ev['a_move']/max(1,ev['a_bars'])),
        risk_atr=ev['risk']/ctx['atr14'] if ctx and ctx['atr14'] else None)
    product=ev['symbol'].rstrip('0123456789').upper();fee=fees['products'][product]
    fill=ev['entry']+sign*ev['tick_size']; stop=ev['stop']-sign*ev['tick_size']
    commission=commission_bound(fee,fill,stop,multiplier)
    f['forecast_loss']=(fill-stop)*sign*multiplier+commission
    f['friction_r']=(2*ev['tick_size']*multiplier+commission)/(ev['risk']*multiplier)
    return f


def passes(f,c):
    cfg=c['filter']; score=f['total']
    if cfg.get('weights'):
        score=sum(w*f[k] for w,k in zip(cfg['weights'],('dim_a','dim_b','dim_warning')))
        # Preserve original warning-kind caps, inferred from original kind externally.
        score=min(score,f['kind_cap'])
    if score<cfg.get('score',3.)-1e-9 or f['friction_r']>.2+1e-9 or f['risk_atr'] is None or not .75<=f['risk_atr']<=2.5:
        return False
    for name,limit in cfg.items():
        if name.endswith('_min') or name.endswith('_max'):
            stem,operation=name.rsplit('_',1); value=f.get(stem)
            if value is None or (value<limit if operation=='min' else value>limit): return False
    return True


def combined_legs(a,b):
    """Two integer lots only when both independent legs have the same real entry."""
    a,b=copy.deepcopy(a),copy.deepcopy(b);ea,eb=evaluation(a),evaluation(b)
    if ea['status']!='closed' or eb['status']!='closed':
        # Retain unknown if either leg is unknown; rejected leg means no combined order.
        if ea['status'] in UNKNOWN:return a
        if eb['status'] in UNKNOWN:return b
        return a if ea['status']!='closed' else b
    if (ea['entry_ts'],ea['fill'],ea['risk'])!=(eb['entry_ts'],eb['fill'],eb['risk']):
        raise ValueError('equal legs entry differs')
    ea['volume']=2;ea['exit_ts']=max(ea['exit_ts'],eb['exit_ts'])
    ea['gross_points']+=eb['gross_points'];ea['net_amount']+=eb['net_amount']
    ea['net_r']=(ea['net_r']+eb['net_r'])/2
    ea['exit_reason']='equal_legs_s1_cap2'
    a['result']['fills'][0]['volume']=2
    a['result']['fills'].extend(b['result']['fills'][1:])
    for field in ('gross_amount_after_slippage','commission'):a['cost_repricing'][field]+=b['cost_repricing'][field]
    return a


def research_calendar(source, strict=False):
    cal=TradingDates(source['symbols'])
    if strict:
        # A lone zero-volume placeholder must not create a settlement day.
        # Require an actually complete daytime 15m triple somewhere in universe.
        cal.dates=sorted({t[:10] for v in source['symbols'].values()
            for t in completed_context(v['bars'],v['rollovers']) if 6<=int(t[11:13])<18})
    return cal


def load(p, strict=False):
    for field,path in p['paths'].items():
        if digest(path)!=p['hashes'][field]:raise ValueError('input changed: '+field)
    source=read_json(SOURCE);fees=read_json(FEES);cal=research_calendar(source,strict)
    events={key(e):e for e in read_json(SIGNALS) if e['risk']>0 and e['setup_features']['setup_quality']>=2.5}
    contexts={s:completed_context(v['bars'],v['rollovers']) for s,v in source['symbols'].items()}
    cases=collections.defaultdict(dict);features={}
    for r in jsonl(RUN/'replay/evaluations.jsonl'):
        k=r['event_key']; r=reprice(r,fees,cal)
        if k not in features:
            features[k]=shape_snapshot(events[k],r['score_components'],source,contexts,fees,r['multiplier'])
            features[k]['kind_cap']={'wick':3.,'cumulative':3.9}.get(events[k]['warning_kind'],5.)
        if evaluation(r)['status']=='closed':r['exit_trading_day']=cal.day(evaluation(r)['exit_ts'])
        cases[r['scenario']][k]=r
    if set(features)!=set(events):raise ValueError('cohort mismatch')
    if len(cases)!=len(p['templates'])*2 or any(set(m)!=set(events) for m in cases.values()):raise ValueError('case mismatch')
    # Strict execution regression, including filtered contexts, against latest P2/P3.
    checked=0
    for old in jsonl(ROOT/'research-runs/server-execution-20261023-full-candidates-v1/evaluations.jsonl'):
        base,slip=old['scenario'].split('/')
        base={'fric_atr_weekend':'P2','fric_atr_cap20_weekend':'P3'}.get(base)
        if not base or slip not in ('slip1','slip2'):continue
        k=old['event_key'];candidate=next(c for c in p['candidates'] if c['name']==base)
        if not passes(features[k],candidate):continue
        new=cases[base+'/'+slip][k]
        old=reprice(old,fees,cal)
        if old['result']!=new['result'] or old.get('cost_repricing')!=new.get('cost_repricing'):raise ValueError('P2/P3 regression: '+k)
        checked+=1
    # Label-independent week assignment, outcome-dependent purging uses MAXIMUM
    # occupancy over all templates and both costs, identically for every candidate.
    dates=cal.dates
    def assigned(day):return 'temporal' if day>=p['temporal_cut'] else p['week_splits'].get(week(day))
    daygroups={d:assigned(d) for d in dates}; embargo=set()
    for i in range(1,len(dates)):
        if daygroups[dates[i]]!=daygroups[dates[i-1]]:embargo.update(dates[max(0,i-1):i+1])
    groups={};purged=collections.Counter()
    for k,ev in events.items():
        start=cal.day(ev['warning_ts']);group=daygroups[start]
        ends=[occupancy(m[k],source['symbols'],'entry')[1] for m in cases.values()]
        end=max([ev['warning_ts']]+[t for t in ends if t])
        try:endday=cal.day(end)
        except ValueError:endday=dates[-1]
        span=dates[bisect.bisect_left(dates,start):bisect.bisect_right(dates,endday)]
        if not group:purged['unassigned']+=1
        elif any(daygroups[d]!=group for d in span):purged['cross_split']+=1
        elif any(d in embargo for d in span):purged['embargo']+=1
        else:groups[k]=group
    return source,cases,features,groups,dict(regression_rows=checked,purged=dict(purged),
        groups=dict(collections.Counter(groups.values())),embargo_dates=sorted(embargo),
        score_components_exact=len(events),calendar_strict_complete_day_triples=strict,
        dates_excluded_from_inferred_calendar=sorted(set(TradingDates(source['symbols']).dates)-set(cal.dates)))


def portfolio(c,slip,source,cases,features):
    if c['template']=='P2P3_equal_legs':
        a,b=[cases[f'{base}/slip{slip}'] for base in ('P2','P3')]
        mapping={k:combined_legs(a[k],b[k]) for k in a}
    else:mapping=cases[f"{c['template']}/slip{slip}"]
    rows=[]
    for k,row in mapping.items():
        f=features[k]
        if not passes(f,c):continue
        if c['sizing']=='budget':
            lots=min(c['max_lots'],math.floor(c['risk_budget']/f['forecast_loss']))
            if lots==0:continue
            row=copy.deepcopy(row);e=evaluation(row)
            e['volume']*=lots;e['gross_points']*=lots
            if e['status']=='closed':
                e['net_amount']*=lots
                for field in ('gross_amount_after_slippage','commission'):row['cost_repricing'][field]*=lots
            for fill in row['result']['fills']:fill['volume']*=lots
            row['sizing_lots']=lots
        rows.append(row)
    reservation=capacity(rows,source['symbols'],'entry',1)
    accepted={x['event_key'] for x in reservation['ledger'] if x['accepted']}
    return [r for r in rows if r['event_key'] in accepted],reservation


def describe(rows,universe,full=True):
    base=summary(rows);closed=[r for r in rows if evaluation(r)['status']=='closed']
    products={s:summary([r for r in rows if r['symbol']==s]) for s in sorted(universe)}
    def bins(fn):
        grouped=collections.defaultdict(list)
        for r in closed:grouped[fn(r)].append(r)
        return {k:summary(v) for k,v in sorted(grouped.items())}
    days=bins(lambda r:r['exit_trading_day'])
    positive_products=sorted([x['net'] for x in products.values() if x['net']>0],reverse=True)
    positive_days=sorted([x['net'] for x in days.values() if x['net']>0],reverse=True)
    def share(xs):return sum(xs[:3])/sum(xs) if xs else 1.
    out=dict(stats=base,unknown=sum(evaluation(r)['status'] in UNKNOWN for r in rows),
        products=products,months=bins(lambda r:r['exit_trading_day'][:7]),weeks=bins(lambda r:week(r['exit_trading_day'])),days=days,
        positive_products=sum(x['net']>0 for x in products.values()),active_products=sum(x['n']>0 for x in products.values()),
        top3_positive_product_share=share(positive_products),top3_positive_day_share=share(positive_days),
        per_initial_lot_net=sum(evaluation(r)['net_amount']/evaluation(r)['volume'] for r in closed))
    if full:
        out['trim']={u:{rank:symmetric_trim(rows,u,rank) for rank in ('cash','r')}
            for u in ('trade','trading_day','exit_date','symbol')}
    return out


def score(metrics,p3,p):
    a,b=[m['stats'] for m in metrics]
    minimum=max(p['selection']['min_closed'],p3[0]['stats']['n']*p['selection']['min_baseline_fraction'])
    eligible=min(a['n'],b['n'])>=minimum and min(a['net'],b['net'])>0 and min(a['mean_r'] or -1,b['mean_r'] or -1)>0
    objective=min(a['mean_r'] or -1,b['mean_r'] or -1)*math.sqrt(min(a['n'],b['n'],100)/100)
    objective-=.1*max(m['top3_positive_product_share'] for m in metrics)
    objective-=.1*max(m['top3_positive_day_share'] for m in metrics)
    return dict(eligible=eligible,objective=objective,minimum_closed=minimum)


def discover():
    p=read_json(PROTOCOL);source,cases,features,groups,checks=load(p)
    development={};ledgers={}
    for c in p['candidates']:
        pair=[]
        for slip in (1,2):
            rows,_=portfolio(c,slip,source,cases,features)
            pair.append(describe([r for r in rows if groups.get(r['event_key'])=='development'],source['symbols'],False))
        development[c['name']]=pair
    ranking={name:score(pair,development['P3'],p) for name,pair in development.items()}
    selected=[]
    for family in p['selection']['families']:
        names=[c['name'] for c in p['candidates'] if c['family']==family]
        selected.append(max(names,key=lambda n:(ranking[n]['eligible'],ranking[n]['objective'],n)))
    result=dict(protocol_sha256=digest(PROTOCOL),replay_sha256=digest(RUN/'replay/evaluations.jsonl'),
        checks=checks,selection_frozen_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        selected=selected,ranking=ranking,development=development,
        holdout_outcomes_consulted=False,qualified_for_trading=False)
    write(RUN/'selection.json',result)
    print(json.dumps(dict(selected=selected,checks=checks,leaders={n:ranking[n] for n in selected})))


def paired_interval(left,right,days,repetitions):
    values={d:[0.,0.,0] for d in days}
    for sign,rows in ((-1,left),(1,right)):
        for r in rows:
            e=evaluation(r)
            if e['status']=='closed':
                v=values[r['exit_trading_day']];v[0]+=sign*e['net_amount'];v[1]+=sign*e['net_r'];v[2]+=sign
    # Nonoverlapping calendar weeks jointly across products; include zero dates.
    blocks=collections.defaultdict(lambda:[0.,0.])
    for d,v in values.items():
        for j in (0,1):blocks[week(d)][j]+=v[j]
    vs=list(blocks.values());samples=[[],[]];rng=random.Random(20261024)
    for _ in range(repetitions):
        draw=[vs[rng.randrange(len(vs))] for _ in vs]
        for j in (0,1):samples[j].append(sum(v[j] for v in draw))
    def band(xs):
        xs.sort();return [xs[int(len(xs)*q)] for q in (.025,.975)]
    return dict(delta_cash=sum(v[0] for v in vs),delta_total_r=sum(v[1] for v in vs),
        cash_interval95=band(samples[0]),total_r_interval95=band(samples[1]),weeks=len(vs),
        method='Joint-symbol calendar-week bootstrap including zero-trade dates; fixed ledgers, unadjusted for multiplicity, descriptive')


def validate(strict=False):
    p=read_json(PROTOCOL);selection=read_json(RUN/'selection.json')
    if selection['protocol_sha256']!=digest(PROTOCOL) or selection['replay_sha256']!=digest(RUN/'replay/evaluations.jsonl'):raise ValueError('selection freeze changed')
    source,cases,features,groups,checks=load(p,strict);cal=research_calendar(source,strict)
    suffix='-calendar-audit' if strict else ''
    names=['P2','P3']+selection['selected'];result={};ledger=[];portfolios={}
    for name in names:
        c=next(c for c in p['candidates'] if c['name']==name);result[name]={}
        for slip in (1,2):
            rows,reservation=portfolio(c,slip,source,cases,features);portfolios[name,slip]=rows
            result[name][str(slip)]={g:describe(rows if g=='all' else [r for r in rows if groups.get(r['event_key'])==g],source['symbols'])
                for g in ('all','development','validation','test','temporal')}
            result[name][str(slip)]['capacity']=dict(blocked=reservation['blocked']['n'],blocked_by_unknown=reservation['blocked_by_unknown'],ignored=reservation['ignored_without_entry'])
            for r in rows:ledger.append(dict(candidate=name,slip=slip,split=groups.get(r['event_key'],'purged'),**r))
    intervals={};flags={}
    for name in selection['selected']:
        intervals[name]={};conditions={}
        for slip in (1,2):
            intervals[name][str(slip)]={}
            for group in ('validation','test','temporal'):
                a,b=[result[n][str(slip)][group]['stats'] for n in ('P3',name)]
                conditions[f'{group}_slip{slip}']=bool(b['n']>=10 and b['net']>0 and (b['mean_r'] or 0)>0
                    and (b['mean_r'] or -1)>=(a['mean_r'] or -1) and (b['win_rate'] or 0)>=(a['win_rate'] or 0))
                days=[d for d in cal.dates if ('temporal' if d>=p['temporal_cut'] else p['week_splits'].get(week(d)))==group]
                subsets=[[r for r in portfolios[n,slip] if groups.get(r['event_key'])==group] for n in ('P3',name)]
                intervals[name][str(slip)][group]=paired_interval(*subsets,days,p['repetitions'])
            m=result[name][str(slip)]['all']
            conditions[f'breadth_slip{slip}']=bool(m['top3_positive_product_share']<=.6 and all(
                m['trim'][unit]['cash']['trim']['3']['without_best']['net_amount']>0 for unit in ('trading_day','symbol')))
        flags[name]=dict(conditions=conditions,all_diagnostics_pass=all(conditions.values()),qualified_for_trading=False)
    write(RUN/f'validation{suffix}.json',dict(protocol=p,selection_sha256=digest(RUN/'selection.json'),checks=checks,
        cases=result,incremental_intervals_vs_P3=intervals,flags=flags,new_independent_observations=0,production_changed=False))
    with (RUN/f'accepted{suffix}.jsonl').open('x',encoding='utf-8') as f:
        for r in ledger:f.write(json.dumps(r,ensure_ascii=False,allow_nan=False)+'\n')
    csv_write(RUN/f'metrics{suffix}.csv',[dict(candidate=n,slip=s,split=g,**m['stats'],
        positive_products=m['positive_products'],active_products=m['active_products'],
        top3_positive_product_share=m['top3_positive_product_share'],unknown=m['unknown'])
        for n,ss in result.items() for s,gs in ss.items() for g,m in gs.items() if g!='capacity'])
    csv_write(RUN/f'products{suffix}.csv',[dict(candidate=n,slip=s,split=g,symbol=prod,**stats)
        for n,ss in result.items() for s,gs in ss.items() for g,m in gs.items() if g!='capacity' for prod,stats in m['products'].items()])
    csv_write(RUN/f'periods{suffix}.csv',[dict(candidate=n,slip=s,split=g,unit=unit,period=period,**stats)
        for n,ss in result.items() for s,gs in ss.items() for g,m in gs.items() if g!='capacity'
        for unit in ('months','weeks','days') for period,stats in m[unit].items()])
    print(json.dumps(dict(selected=selection['selected'],flags=flags,checks=checks)))


def calendar_audit():
    validate(strict=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('stage',choices=['prepare','discover','validate','calendar_audit'])
    globals()[parser.parse_args().stage]()
