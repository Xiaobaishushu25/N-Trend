"""Chronological reservations and realized-only wave budgets, no resizing."""
import collections,heapq
from research_swing_consistency import ev,UNKNOWN,cluster
from audit_swing_flat_ambiguity import ambiguity

RULES={
    'base':dict(),
    'one_attempt':dict(cluster='one'),
    'after_loss':dict(cluster='loss'),
    'after_stop':dict(cluster='stop'),
    'sector1_same':dict(cap=1,same_direction=True),
    'sector2_same':dict(cap=2,same_direction=True),
    'sector1_any':dict(cap=1,same_direction=False),
    'sector2_loss':dict(cap=2,same_direction=True,cluster='loss'),
}

def reservation(row,ends,policy):
    e=ev(row);amb=ambiguity(row) if policy=='two_paths_flat' else None
    if not(e.get('trigger_ts') or e.get('entry_ts')) and e['status'] not in UNKNOWN:return None
    start=e.get('trigger_ts') or e.get('entry_ts') or (amb['start'] if amb else row['warning_ts'])
    end=amb['end'] if amb else ends[row['execution_contract']] if e['status'] in UNKNOWN else e['exit_ts']
    assert end and start<=end
    return start,end,bool(amb)

def select(rows,events,ends,sector_map,rule='base',policy='original',reverse=False):
    config=RULES[rule];items=[]
    for row in rows:
        rr=reservation(row,ends,policy)
        if rr:items.append((row,rr))
    items.sort(key=lambda x:x[0]['event_key'],reverse=reverse);items.sort(key=lambda x:(x[1][0],x[0]['warning_ts']))
    active={};symbols={};heap=[];attempts=collections.Counter();realized=collections.defaultdict(float);stopped=set();accepted=[];ledger=[];settlements=[];seq=0;peak_risk=0.;peak_sector=0.;peak_count=0
    for row,(start,end,flat) in items:
        while heap and heap[0][0]<start:
            ended,idx=heapq.heappop(heap);old=active.pop(idx);r0=old['row'];c0=old['cluster'];e0=ev(r0)
            if symbols.get(r0['symbol'])==idx:del symbols[r0['symbol']]
            if e0['status']=='closed':
                assert e0['exit_ts']==ended and ended<start
                realized[c0]+=e0['net_amount']
                if e0['exit_reason']=='stop':stopped.add(c0)
                settlements.append(dict(event_key=r0['event_key'],exit_ts=ended,visible_before=start,cluster=c0,net=e0['net_amount']))
        e=events[row['event_key']];c=cluster(e);sector=sector_map[row['symbol']];sk=(sector,row['direction'] if config.get('same_direction',True) else 'all')
        sector_active=[a for a in active.values() if a['sector']==sector and (not config.get('same_direction',True) or a['direction']==row['direction'])]
        why=('symbol_slot' if row['symbol'] in symbols else
            'cluster_attempt' if config.get('cluster')=='one' and attempts[c]>0 else
            'cluster_realized_loss' if config.get('cluster')=='loss' and realized[c]<0 else
            'cluster_realized_stop' if config.get('cluster')=='stop' and c in stopped else
            'sector_slot' if config.get('cap') is not None and len(sector_active)>=config['cap'] else None)
        nominal_cash_risk=row['planned_risk']*row['multiplier'];before_risk=sum(a['risk'] for a in active.values())
        ledger.append(dict(event_key=row['event_key'],start=start,end=end,accepted=why is None,reason=why,cluster=c,sector=sector,direction=row['direction'],prior_attempts=attempts[c],visible_cluster_net=realized[c],visible_cluster_stop=c in stopped,sector_reservations_before=len(sector_active),total_risk_before=before_risk,planned_cash_risk=nominal_cash_risk,two_paths_flat=flat,unknown_pnl=ev(row)['status'] in UNKNOWN))
        if why:continue
        seq+=1;active[seq]=dict(row=row,cluster=c,sector=sector,direction=row['direction'],risk=nominal_cash_risk);symbols[row['symbol']]=seq;heapq.heappush(heap,(end,seq));attempts[c]+=1;accepted.append(row)
        peak_risk=max(peak_risk,before_risk+nominal_cash_risk);peak_sector=max(peak_sector,sum(a['risk'] for a in active.values() if a['sector']==sector));peak_count=max(peak_count,len(active))
    return accepted,ledger,dict(peak_reserved_nominal_cashR=peak_risk,peak_one_sector_reserved_nominal_cashR=peak_sector,peak_symbol_reservations=peak_count,realized_settlements=settlements)
