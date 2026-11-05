"""V4: distinguish quiet peers from cancellation, then test a fixed structural payoff."""
import itertools
import math
import numpy as np
import pandas as pd
import explore_v3_signals as v3


def templates(universe):
    return [dict(candidate_id=f'Q-C{i:02d}',hypothesis_id='Q',parameters=dict(peer_state=state,range_bars=n,confirmation_bars=confirm),
        universe=universe,directions=['long','short'],entry_sessions=['day'],allow_overnight=False)
        for i,(state,n,confirm) in enumerate(itertools.product(['quiet','divergent'],[12,24],[1,2]),1)]


def peers(frames):
    result=v3.peers(frames)
    for f in result.values():f['peer_abs_median']=np.nan;f['peer_mad']=np.nan
    for group in v3.GROUPS.values():
        names=[n for n in group if n in frames]
        if len(names)<3:continue
        panel=pd.concat({n:frames[n].set_index('end_ns').norm_day_move for n in names},axis=1)
        for n in names:
            others=panel.drop(columns=n);count=others.notna().sum(axis=1);median=others.median(axis=1)
            absolute=others.abs().median(axis=1).where(count>=2)
            mad=others.sub(median,axis=0).abs().median(axis=1).where(count>=2)
            result[n]['peer_abs_median']=result[n].end_ns.map(absolute)
            result[n]['peer_mad']=result[n].end_ns.map(mad)
    return result


def state_at(a,i,state):
    median=a['peer_median'][i];absolute=a['peer_abs_median'][i];mad=a['peer_mad'][i]
    if not np.isfinite(median+absolute+mad) or abs(median)>1.:return False
    return absolute<=1. if state=='quiet' else absolute>=2. and mad>=1.


def signals(f,candidate,tick):
    a={k:f[k].to_numpy() for k in f};p=candidate['parameters'];pending=None;cooldown=-1;day=None;events=[]
    for i in range(len(f)):
        if a['day'][i]!=day:day=a['day'][i];cooldown=-1;pending=None
        if not a['valid'][i]:pending=None;continue
        if i and a['segment'][i]!=a['segment'][i-1]:pending=None
        common=i>=cooldown and a['economic_gate'][i] and a['activity_known'][i] and a['minute'][i]+5<890
        emitted=None
        if pending is not None:
            q=pending;pending=None;d=q['side'];target=q['target'];stop=q['stop']
            favorable=a['high'][i] if d==1 else a['low'][i];adverse=a['low'][i] if d==1 else a['high'][i]
            already_hit=(favorable-target)*d>=0 or (adverse-stop)*d<=0
            # Only the very next active bar can confirm. No repeated retries.
            if common and not already_hit and q['lower']<=a['close'][i]<=q['upper'] and (a['close'][i]-a['close'][i-1])*d>0 and (a['close'][i]-a['open'][i])*d>0:
                emitted=q
            if emitted:
                emitted={**emitted,'index':i};events.append(emitted);cooldown=i+24
            continue
        if not common or not i or a['day'][i]!=a['day'][i-1] or int(a['end_ns'][i-1])!=int(a['start_ns'][i]):continue
        if not state_at(a,i-1,p['peer_state']) or not np.isfinite(a['er12'][i-1]) or a['er12'][i-1]>.4:continue
        n=p['range_bars'];upper=a[f'range_high{n}'][i];lower=a[f'range_low{n}'][i];atr=a['atr'][i-1]
        if not np.isfinite(upper+lower+atr) or upper<=lower or atr<=0:continue
        op,hi,lo,cl=[a[k][i] for k in ('open','high','low','close')]
        up=hi>=upper+.25*atr;down=lo<=lower-.25*atr;d=0
        if up and not down and lower<=cl<upper and cl<op and cl<a['close'][i-1]:d=-1
        elif down and not up and lower<cl<=upper and cl>op and cl>a['close'][i-1]:d=1
        if not d:continue
        q=dict(index=i,setup_index=i,side=d,stop=float(hi+tick if d==-1 else lo-tick),target=float((upper+lower)/2),
               upper=float(upper),lower=float(lower),origin_end=int(a['end_ns'][i]),peer_state=p['peer_state'])
        if p['confirmation_bars']==1:events.append(q);cooldown=i+24
        else:pending=q
    return events


def fee(rule,price,mult):return rule['fixed_yuan_per_lot']+price*mult*rule['notional_rate']


def round_price(price,tick,up):
    return (math.ceil(price/tick-1e-9) if up else math.floor(price/tick+1e-9))*tick


def trade(a,index,side,stop,target,spec,fees,end_ns,budget=400.):
    """One-lot prototype, not a shared account. No clipping actual gap losses."""
    result=dict(trade_status='unscorable',trade_entry_ns=None,trade_exit_ns=None,trade_entry=None,trade_exit=None,
        trade_stop=None,trade_target=None,trade_reason=None,trade_net_R=None,trade_net_cny=None,trade_fees_cny=None,
        trade_plan_risk_cny=None,trade_actual_loss_over_600=None,trade_ambiguous=False)
    j=index+1;n=len(a['start_ns']);tick=spec['tick'];mult=spec['multiplier'];R=a['atr'][index]
    reference=float(a['close'][index]+side*tick)
    if (reference-stop)*side<=0 or (target-reference)*side<=0:
        result['trade_reason']='signal_barrier_geometry';return result
    forecast_reward=(target-side*tick-reference)*side*mult-fee(fees['open'],reference,mult)-fee(fees['close_today'],target-side*tick,mult)
    if forecast_reward<=0:result['trade_reason']='signal_target_cannot_cover_cost';return result
    if j>=n:result['trade_reason']='no_next_bar';return result
    if a['day'][j]!=a['day'][index] or a['segment'][j]!=a['segment'][index] or not a['valid'][j]:result['trade_reason']='day_gap_or_roll';return result
    if a['minute'][j]>=890 or a['volume'][j]<=0:result['trade_reason']='late_or_unknown_entry';return result
    entry=float(a['open'][j]+side*tick)
    stop=round_price(stop,tick,side==-1);target=round_price(target,tick,side==-1)
    result.update(trade_entry_ns=int(a['start_ns'][j]),trade_entry=entry,trade_stop=float(stop),trade_target=float(target))
    if (entry-stop)*side<=0 or (target-entry)*side<=0:result['trade_reason']='gap_beyond_barrier';return result
    stopfill=stop-side*tick;openfee=fee(fees['open'],entry,mult)
    risk=(entry-stopfill)*side*mult+openfee+fee(fees['close_today'],stopfill,mult)
    reward=(target-side*tick-entry)*side*mult-openfee-fee(fees['close_today'],target-side*tick,mult)
    result['trade_plan_risk_cny']=float(risk)
    if reward<=0:result['trade_reason']='target_cannot_cover_cost';return result
    if risk>min(600.,budget):result['trade_reason']='integer_lot_budget';return result
    for k in range(j,min(n,j+12)):
        if a['day'][k]!=a['day'][index] or a['segment'][k]!=a['segment'][index] or not a['valid'][k]:result['trade_reason']='missing_path';return result
        if int(a['end_ns'][k])>=end_ns:result['trade_reason']='purged_boundary';return result
        op,hi,lo,cl=[a[c][k] for c in ('open','high','low','close')]
        stopped=(lo<=stop if side==1 else hi>=stop);hit=(hi>=target if side==1 else lo<=target)
        # Subsequent-bar open is observed before its range. Entry bar cannot be filled before j.
        if (op-stop)*side<=0:price=op-side*tick;reason='gap_stop'
        elif (op-target)*side>=0:price=target-side*tick;reason='gap_target_conservative'
        elif stopped:price=stop-side*tick;reason='stop';result['trade_ambiguous']=bool(hit)
        elif hit:price=target-side*tick;reason='target'
        elif k-j+1==12 or int(a['minute'][k])==895:price=cl-side*tick;reason='time_or_day_close'
        else:continue
        if a['volume'][k]<=0:result['trade_reason']='unknown_exit';return result
        cost=openfee+fee(fees['close_today'],price,mult);net=(price-entry)*side*mult-cost
        result.update(trade_status='complete',trade_exit_ns=int(a['start_ns'][k]) if reason.startswith('gap_') else int(a['end_ns'][k]),
            trade_exit=float(price),trade_reason=reason,trade_net_cny=float(net),trade_net_R=float(net/(mult*R)),trade_fees_cny=float(cost),
            trade_actual_loss_over_600=bool(net < -600.))
        return result
    result['trade_reason']='unfinished_path';return result


def projection(event,a,index,spec):
    """Copy only signal-time normalized barrier geometry, never future entry/fill information."""
    d=event['side'];ref=float(a['close'][index]+d*spec['tick']);r=float(a['atr'][index])
    return ref-d*event['forecast_stop_R']*r,ref+d*event['forecast_target_R']*r
