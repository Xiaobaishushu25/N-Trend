"""Post-main extension: preserve aligned opportunities and allow one stop retry."""
import collections,heapq
from research_swing_consistency import ev,UNKNOWN,cluster
from swing_portfolio_balance import reservation

RULES={
    'base':{},
    'sector2_counter':dict(cap=2,counter_only=True),
    'after_two_stops':dict(stop_limit=2),
    'sector2_counter_stops':dict(cap=2,counter_only=True,stop_limit=2),
}

def counter_hour(event):
    v=(event.get('market_context') or {}).get('trend_alignment_60')
    return v is not None and v<=0

def select(rows,events,ends,sector_map,rule='base',policy='original',reverse=False):
    config=RULES[rule];items=[]
    for row in rows:
        rr=reservation(row,ends,policy)
        if rr:items.append((row,rr))
    items.sort(key=lambda x:x[0]['event_key'],reverse=reverse);items.sort(key=lambda x:(x[1][0],x[0]['warning_ts']))
    active={};symbols={};heap=[];attempts=collections.Counter();realized=collections.defaultdict(float);stops=collections.Counter();accepted=[];ledger=[];settlements=[];seq=0;peak_risk=0.;peak_sector=0.;peak_count=0
    for row,(start,end,flat) in items:
        while heap and heap[0][0]<start:
            ended,idx=heapq.heappop(heap);old=active.pop(idx);r0=old['row'];c0=old['cluster'];e0=ev(r0)
            if symbols.get(r0['symbol'])==idx:del symbols[r0['symbol']]
            if e0['status']=='closed':
                assert e0['exit_ts']==ended and ended<start
                realized[c0]+=e0['net_amount']
                if e0['exit_reason']=='stop':stops[c0]+=1
                settlements.append(dict(event_key=r0['event_key'],exit_ts=ended,visible_before=start,cluster=c0,net=e0['net_amount']))
        event=events[row['event_key']];c=cluster(event);sector=sector_map[row['symbol']];counter=counter_hour(event)
        sector_active=[a for a in active.values() if a['sector']==sector and a['direction']==row['direction'] and (not config.get('counter_only') or a['counter'])]
        why=('symbol_slot' if row['symbol'] in symbols else
             'cluster_two_realized_stops' if config.get('stop_limit') and stops[c]>=config['stop_limit'] else
             'sector_slot' if config.get('cap') and (not config.get('counter_only') or counter) and len(sector_active)>=config['cap'] else None)
        risk=row['planned_risk']*row['multiplier'];before=sum(a['risk'] for a in active.values())
        ledger.append(dict(event_key=row['event_key'],start=start,end=end,accepted=why is None,reason=why,cluster=c,sector=sector,direction=row['direction'],prior_attempts=attempts[c],visible_cluster_net=realized[c],visible_cluster_stop=stops[c]>0,visible_cluster_stop_count=stops[c],counter_hour=counter,sector_reservations_before=len(sector_active),total_risk_before=before,planned_cash_risk=risk,two_paths_flat=flat,unknown_pnl=ev(row)['status'] in UNKNOWN))
        if why:continue
        seq+=1;active[seq]=dict(row=row,cluster=c,sector=sector,direction=row['direction'],risk=risk,counter=counter);symbols[row['symbol']]=seq;heapq.heappush(heap,(end,seq));attempts[c]+=1;accepted.append(row)
        peak_risk=max(peak_risk,before+risk);peak_sector=max(peak_sector,sum(a['risk'] for a in active.values() if a['sector']==sector));peak_count=max(peak_count,len(active))
    return accepted,ledger,dict(peak_reserved_nominal_cashR=peak_risk,peak_one_sector_reserved_nominal_cashR=peak_sector,peak_symbol_reservations=peak_count,realized_settlements=settlements)
