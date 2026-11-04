"""Forward labels and discovery-only matched-day inference; no trade optimization."""
import collections
import numpy as np

HORIZONS=(6,12,24)


def fee(rule,price,mult):
    return rule['fixed_yuan_per_lot']+price*mult*rule['notional_rate']


def first_passage(op,hi,lo,entry,risk,side,reward):
    """OHLC path bounds, including the observable open before intrabar ambiguity."""
    for j,(o,h,l) in enumerate(zip(op,hi,lo)):
        opened=(o-entry)*side
        if opened<=-risk:return 0,0,False,j
        if opened>=reward*risk:return 1,1,False,j
        good=(h-entry if side==1 else entry-l)>=reward*risk
        bad=(entry-l if side==1 else h-entry)>=risk
        if good and bad:return 0,1,True,j
        if bad:return 0,0,False,j
        if good:return 1,1,False,j
    return 0,0,False,None


def label(f,index,side,spec,fees,window_end_ns,stop=None):
    """Outcome labeling only. A signal never depends on this function's result."""
    n=len(f['start_ns']);r=float(f['atr'][index]);j=index+1
    base=dict(signal_ns=int(f['end_ns'][index]),side=int(side),R_price=r,entry_ns=None,entry_price=None,
              calendar_known=bool(f['calendar_known'][index]),execution_verified=False,lot_planned_risk=None,one_lot_600_feasible=None)
    unavailable=None
    if j>=n:unavailable='no_next_bar'
    elif f['day'][j]!=f['day'][index]:unavailable='day_close'
    elif f['segment'][j]!=f['segment'][index] or not f['valid'][j]:unavailable='gap_or_roll'
    elif f['minute'][j]>=890:unavailable='near_close'
    elif f['volume'][j]<=0:unavailable='unknown_execution'
    if unavailable:
        for h in HORIZONS:base.update({f'status_{h}':unavailable,**{f'{k}_{h}':None for k in ('label_end_ns','gross_R','net_R','MFE_R','MAE_R','p1_lower','p1_upper','p2_lower','p2_upper','ambiguous','p1_unresolved','p2_unresolved')}})
        return base
    tick=spec['tick'];mult=spec['multiplier'];entry=float(f['open'][j]+side*tick)
    base.update(entry_ns=int(f['start_ns'][j]),entry_price=entry)
    if stop is not None:
        stopfill=stop-side*tick
        planned=(entry-stopfill)*side*mult+fee(fees['open'],entry,mult)+fee(fees['close_today'],stopfill,mult)
        base['lot_planned_risk']=float(planned)
        base['one_lot_600_feasible']=bool(0<(entry-stop)*side and planned<=600)
    last=j
    while last<n and last<j+24 and f['day'][last]==f['day'][index] and f['segment'][last]==f['segment'][index] and f['valid'][last]:last+=1
    for h in HORIZONS:
        end=j+h
        if end>last:status='censored_day_or_gap'
        elif int(f['end_ns'][end-1])>=window_end_ns:status='purged_boundary'
        else:status='complete'
        base[f'status_{h}']=status
        if status!='complete':
            for k in ('label_end_ns','gross_R','net_R','MFE_R','MAE_R','p1_lower','p1_upper','p2_lower','p2_upper','ambiguous','p1_unresolved','p2_unresolved'):base[f'{k}_{h}']=None
            continue
        exit_price=float(f['close'][end-1]-side*tick)
        cost=fee(fees['open'],entry,mult)+fee(fees['close_today'],exit_price,mult)
        gross=(float(f['close'][end-1])-entry)*side/r
        net=(exit_price-entry)*side/r-cost/(mult*r)
        favorable=(np.max(f['high'][j:end])-entry) if side==1 else (entry-np.min(f['low'][j:end]))
        adverse=(entry-np.min(f['low'][j:end])) if side==1 else (np.max(f['high'][j:end])-entry)
        a=first_passage(f['open'][j:end],f['high'][j:end],f['low'][j:end],entry,r,side,1)
        b=first_passage(f['open'][j:end],f['high'][j:end],f['low'][j:end],entry,r,side,2)
        values=dict(label_end_ns=int(f['end_ns'][end-1]),gross_R=float(gross),net_R=float(net),
                    MFE_R=float(max(0,favorable)/r),MAE_R=float(max(0,adverse)/r),
                    p1_lower=a[0],p1_upper=a[1],p2_lower=b[0],p2_upper=b[1],ambiguous=bool(a[2] or b[2]),p1_unresolved=a[3] is None,p2_unresolved=b[3] is None)
        for k,v in values.items():base[f'{k}_{h}']=v
    return base


def holm(pvalues):
    p=np.asarray(pvalues,float);order=np.argsort(p,kind='stable');adjusted=np.ones(len(p));maximum=0
    for rank,i in enumerate(order):
        maximum=max(maximum,float(p[i])*(len(p)-rank));adjusted[i]=min(1,maximum)
    return adjusted


def matching(events,pool,repetitions=200,seed=20261104):
    """Exact strata; a control timestamp is used once per replicate across both sides."""
    rng=np.random.default_rng(seed);groups=collections.defaultdict(list)
    for i,e in enumerate(events):groups[e['stratum']].append(i)
    result=np.full((repetitions,len(events)),-1,dtype=np.int32)
    for rep in range(repetitions):
        used=set()
        for key,indices in sorted(groups.items()):
            choices=[k for k in pool['by_stratum'].get(key,[]) if pool['rows'][k]['time_key'] not in used]
            targets=rng.permutation(indices);controls=rng.permutation(choices)
            for e,c in zip(targets,controls):
                result[rep,e]=int(c);used.add(pool['rows'][int(c)]['time_key'])
    return result


def inference(events,controls,matches,repetitions=1000,seed=20261105):
    """Both sides attributed to source date, preserving same-day cross-product dependence."""
    dates=sorted({e['day'] for e in events}|{c['day'] for c in controls});date_index={d:i for i,d in enumerate(dates)}
    matrix=np.zeros((len(matches),len(dates)));effects=[];raw=[];block_effects=[[] for _ in range(3)];block_counts=[[] for _ in range(3)]
    product_effects=collections.defaultdict(list);matched_days=[]
    for rep,row in enumerate(matches):
        indices=np.flatnonzero(row>=0);day_counts=collections.Counter(events[i]['day'] for i in indices);days=len(day_counts)
        matched_days.append(days)
        if not days:effects.append(np.nan);raw.append(np.nan);continue
        for i in indices:
            e=events[i];c=controls[int(row[i])];weight=1/(days*day_counts[e['day']])
            matrix[rep,date_index[e['day']]]+=weight*e['net_R_12']
            matrix[rep,date_index[c['day']]]-=weight*c['net_R_12']
        effects.append(float(matrix[rep].sum()))
        raw.append(float(sum(events[i]['net_R_12']/day_counts[events[i]['day']] for i in indices)/days))
        for block in range(3):
            subset=[i for i in indices if events[i]['discovery_block']==block]
            block_counts[block].append(len(subset))
            grouped=collections.defaultdict(list)
            for i in subset:grouped[events[i]['day']].append(events[i]['net_R_12']-controls[int(row[i])]['net_R_12'])
            block_effects[block].append(float(np.mean([np.mean(v) for v in grouped.values()])) if grouped else np.nan)
        pg=collections.defaultdict(list)
        for i in indices:pg[events[i]['product']].append(events[i]['net_R_12']-controls[int(row[i])]['net_R_12'])
        for product,v in pg.items():product_effects[product].append(float(np.mean(v)))
    valid=np.isfinite(effects)
    if not valid.any():return dict(effect=None,ci_lower=None,ci_upper=None,p_value=1.,signal_net_R=None,matched_fraction=0.,matched_days=0,positive_blocks=0,product_effects={}),[]
    effect=float(np.nanmean(effects));rng=np.random.default_rng(seed)
    # A common calendar-date count vector jointly resamples all products and both cohorts.
    boot=[];available=np.flatnonzero(valid)
    for _ in range(repetitions):
        rep=int(rng.choice(available));counts=rng.multinomial(len(dates),np.full(len(dates),1/len(dates)))
        boot.append(float(matrix[rep]@counts))
    lower,upper=np.quantile(boot,[.025,.975]);null=np.asarray(boot)-effect
    p=float((1+np.sum(null>=effect))/(len(boot)+1))
    stable=[]
    for b in range(3):
        count=min(block_counts[b],default=0);v=np.asarray(block_effects[b],float)
        avg=float(np.nanmean(v)) if np.isfinite(v).any() else None
        stable.append(dict(block=b+1,minimum_matched_events=count,effect=avg))
    return dict(effect=effect,ci_lower=float(lower),ci_upper=float(upper),p_value=p,
        signal_net_R=float(np.nanmean(raw)),matched_fraction=float(np.min((matches>=0).sum(axis=1))/len(events)),
        matched_days=int(min(matched_days)),positive_blocks=sum(b['minimum_matched_events']>=10 and b['effect'] is not None and b['effect']>0 for b in stable),
        discovery_blocks=stable,product_effects={p:float(np.mean(v)) for p,v in product_effects.items()}),boot
