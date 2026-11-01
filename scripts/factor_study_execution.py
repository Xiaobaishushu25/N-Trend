"""One-lot minute replay, fixed cash targets, causal capacity and cash/R audit."""
import bisect
import collections
import copy
import datetime as dt
import math
import itertools
from decimal import Decimal

from factor_study_data import Calendar, read, rows, write, write_rows, new_output, seal, verify_seal
from reprice_signal_costs import fee_for

UNKNOWN = {'ambiguous','missing_data','unfinished','pending'}
TERMINAL = {'closed','invalidated','untriggered','invalid_target','filtered_risk','unscorable'}


class MinuteBars(list):
    def __init__(self,values):
        super().__init__(values)
        self.starts=[b['start'] for b in self]
        self.ends=[b['ts'] for b in self]


def round_tick(price, tick, upward):
    value = Decimal(str(price))/Decimal(str(tick))
    return float(value.to_integral_value(rounding='ROUND_CEILING' if upward else 'ROUND_FLOOR')*Decimal(str(tick)))


def fee_bound(spec, entry, exit_price, multiplier):
    if 'roundtrip_fixed_yuan_per_lot' in spec:
        return spec['roundtrip_fixed_yuan_per_lot']
    return fee_for(spec['open'],entry,1,multiplier)+max(fee_for(spec[k],exit_price,1,multiplier)
                                                      for k in ('close_today','close_yesterday'))


def commission(spec, entry, exit_price, multiplier, entry_ts, exit_ts, calendar):
    if 'roundtrip_fixed_yuan_per_lot' in spec:
        half = spec['roundtrip_fixed_yuan_per_lot']/2
        return half,half
    kind = 'close_today' if calendar.day(entry_ts)==calendar.day(exit_ts) else 'close_yesterday'
    return fee_for(spec['open'],entry,1,multiplier),fee_for(spec[kind],exit_price,1,multiplier)


def planned(snapshot, spec, config):
    event = snapshot['event']
    sign = 1 if event['direction']=='up' else -1
    tick, multiplier = snapshot['tick'],snapshot['multiplier']
    stop = round_tick(event['stop'],tick,sign<0)
    entry = event['entry']+sign*config['forecast_slip_ticks']*tick
    stop_fill = stop-sign*config['forecast_slip_ticks']*tick
    risk = sign*(entry-stop)*multiplier
    loss = sign*(entry-stop_fill)*multiplier+fee_bound(spec,entry,stop_fill,multiplier)
    return dict(stop=stop,planned_loss_yuan=loss,planned_risk_yuan=risk,
                risk_ticks=sign*(entry-stop)/tick,
                cost_r=(loss-risk)/risk if risk>0 else None,
                planned_risk_ok=0 < loss <= config['planned_loss_cap_yuan']+1e-8)


def cash_target(base, sign, tick, multiplier, spec, config):
    """Forecast always uses 1 tick, including in 0/2 tick execution stress."""
    slip = config['forecast_slip_ticks']*tick
    forecast_entry = base+sign*slip
    guessed = forecast_entry+sign*(config['target_net_yuan']/multiplier+slip)
    for _ in range(20):
        fees = fee_bound(spec,forecast_entry,guessed-sign*slip,multiplier)
        value = round_tick(forecast_entry+sign*((config['target_net_yuan']+fees)/multiplier+slip),tick,sign>0)
        if sign*(value-forecast_entry)*multiplier > 1e9:
            raise ValueError('invalid cash target')
        if sign*((value-sign*slip)-forecast_entry)*multiplier-fee_bound(spec,forecast_entry,value-sign*slip,multiplier) >= config['target_net_yuan']-1e-7:
            return value
        guessed = value+sign*tick
    raise ValueError('cash target did not converge')


def evaluate(snapshot, minutes, gaps, spec, calendar, config, policy, slip):
    event = snapshot['event']; sign = 1 if event['direction']=='up' else -1
    tick, mult = snapshot['tick'],snapshot['multiplier']
    plan = planned(snapshot,spec,config)
    state = dict(status='pending',entry=None,entry_ts=None,exit=None,exit_ts=None,reason=None,
                 target=None,risk_yuan=None,peak=0.0,fills=[],entry_bucket=None)
    if snapshot['quality']['status']!='ok': state.update(status='unscorable',reason=snapshot['quality']['status'])
    elif not plan['planned_risk_ok']: state.update(status='filtered_risk',reason='planned_loss_cap')
    warning = snapshot['warning_ts']
    mode=config.get('entry_mode','breakout')
    if mode not in {'breakout','close_confirm','retest25'}:
        raise ValueError('unknown research entry mode: '+mode)
    if mode!='breakout':
        state.update(entry_mode=mode,break_ts=None,confirmation_ts=None,peak_upper=0.0)
        if mode=='retest25':
            level=round_tick(event['entry']-sign*.25*sign*(event['entry']-plan['stop']),tick,sign<0)
            state['pullback_price']=level
            if state['status']=='pending' and sign*(level-plan['stop'])<=0:
                state.update(status='invalid_target',exit_ts=warning,reason='pullback_at_stop')
    last_ts, divergence, possibly_entered = warning,None,False

    def finish(s,status,ts,reason):
        s.update(status=status,exit_ts=ts,reason=reason)

    def close(s,price,ts,reason):
        s.update(status='closed',exit=price,exit_ts=ts,reason=reason)
        s['fills'].append(dict(offset='close',price=price,ts=ts,volume=1))

    def enter(s,base,bucket,ts):
        target = cash_target(base,sign,tick,mult,spec,config) if policy=='cash200' else round_tick(event['target'],tick,sign<0)
        fill = base+sign*slip*tick
        if sign*(target-fill) <= 0:
            finish(s,'invalid_target',ts,'gap_beyond_target'); return
        s.update(status='open',entry=fill,entry_ts=ts,target=target,risk_yuan=sign*(fill-plan['stop'])*mult,entry_bucket=bucket)
        s['fills'].append(dict(offset='open',price=fill,ts=ts,volume=1))

    def advance(s,point,bucket,ts,is_open=False):
        if s['status'] in TERMINAL: return
        if s['entry'] is None:
            # Retest is a market-on-touch order armed only after the original
            # breakout. On an intraminute descent it crosses the retest before
            # the stop; a gap already past the stop invalidates the pending order.
            if mode=='retest25' and s['break_ts'] is not None:
                if is_open and sign*(point-plan['stop'])<=0:
                    finish(s,'invalidated',ts,'stop_before_entry'); return
                if sign*(point-s['pullback_price'])<=0:
                    enter(s,point if is_open else s['pullback_price'],bucket,ts)
                else:return
            else:
                if sign*(point-plan['stop']) <= 0:
                    finish(s,'invalidated',ts,'stop_before_entry'); return
                if mode=='close_confirm':
                    if is_open and s['confirmation_ts'] is not None:
                        enter(s,point,bucket,ts)
                    else:return
                elif sign*(point-event['entry']) >= 0:
                    if mode=='retest25':
                        s['break_ts']=ts;return
                    enter(s,point if is_open else event['entry'],bucket,ts)
                else: return
        if s['status']!='open': return
        if sign*(point-plan['stop']) <= 0:
            close(s,(point if is_open else plan['stop'])-sign*slip*tick,ts,'stop'); return
        s['peak']=max(s['peak'],sign*(point-s['entry'])*mult/s['risk_yuan'])
        if mode!='breakout':
            s['peak_upper']=max(s['peak_upper'],sign*(point-s['entry'])*mult/s['risk_yuan'])
        if sign*(point-s['target']) >= 0:
            close(s,s['target']-sign*slip*tick,ts,'target')

    waiting, current_bucket = 0,None
    gap_index = bisect.bisect_left(gaps,warning)
    if state['status'] not in TERMINAL:
        first = bisect.bisect_left(minutes.starts if isinstance(minutes,MinuteBars) else [b['start'] for b in minutes],warning)
        for b in itertools.islice(minutes,first,None):
            time=dt.datetime.fromisoformat(b['start'])
            bucket=(time.replace(minute=time.minute//15*15)+dt.timedelta(minutes=15)).isoformat(sep=' ')
            if gap_index<len(gaps) and gaps[gap_index] < b['start']:
                finish(state,'missing_data',gaps[gap_index],'expected_minute_missing'); break
            if current_bucket!=bucket:
                current_bucket=bucket
                waiting+=1
            if state['entry'] is None and waiting>config['pending_buckets']:
                finish(state,'untriggered',b['start'],'pending_expired'); break
            alternatives=[]
            for vertices in ((b['open'],b['high'],b['low'],b['close']),
                             (b['open'],b['low'],b['high'],b['close'])):
                s=state.copy();s['fills']=list(state['fills'])
                for i,point in enumerate(vertices):
                    advance(s,point,waiting,b['ts'],i==0)
                alternatives.append(s)
            # Peaks after terminal exits do not affect subsequent execution.
            def comparable(s):
                value=s.copy()
                if mode!='breakout':
                    value.pop('peak_upper')
                    # Only crossing the fixed no-follow threshold affects any
                    # future exit. Retain the full peak range in the label,
                    # while merging states with identical execution and flag.
                    value['peak']=s['peak']>=config['no_follow_mfe_r']
                if value['status'] in TERMINAL: value.pop('peak')
                return value
            if comparable(alternatives[0])!=comparable(alternatives[1]):
                possibly_entered=any(s['entry'] is not None for s in alternatives)
                divergence=dict(ts=b['ts'],paths=alternatives)
                finish(state,'ambiguous',b['ts'],'intraminute_order'); break
            state=alternatives[0]; last_ts=b['ts']
            if mode!='breakout':
                state['peak']=min(s['peak'] for s in alternatives)
                state['peak_upper']=max(s['peak_upper'] for s in alternatives)
            if state['status'] in TERMINAL: break
            if (mode=='close_confirm' and state['entry'] is None
                and state['confirmation_ts'] is None and sign*(b['close']-event['entry'])>=0):
                state['confirmation_ts']=b['ts']
            if b['ts']==bucket:
                if state['entry'] is None and waiting==config['pending_buckets']:
                    finish(state,'untriggered',b['ts'],'pending_expired'); break
                if state['entry'] is not None:
                    held=waiting-state['entry_bucket']+1
                    reason=('time_exit' if held>=config['holding_buckets'] else
                            'no_follow' if held==config['no_follow_bucket'] and state['peak']<config['no_follow_mfe_r'] else None)
                    if reason: close(state,b['close']-sign*slip*tick,b['ts'],reason); break
        if state['status']=='open': state['status']='unfinished'
    state.update(schema='TradeLabelV1',candidate_id=snapshot['candidate_id'],symbol=snapshot['symbol'],
                 contract=snapshot['contract'],warning_ts=warning,policy=policy,slip_ticks=slip,
                 scenario=f'{policy}_{slip}tick',plan=plan,disagreement=divergence,
                 target_net_yuan=config['target_net_yuan'],
                 possibly_entered=possibly_entered or state['entry'] is not None,
                 net_yuan=None,net_r=None,fees=None,gross_yuan=None,hit200=None,
                 last_observed_ts=last_ts)
    state['release_ts']=state['exit_ts'] if state['status'] in TERMINAL else None
    if divergence and all(p['status'] in TERMINAL and p['exit_ts'] for p in divergence['paths']):
        # Profit may remain unknown even though both paths have already released capacity.
        state['release_ts']=max(p['exit_ts'] for p in divergence['paths'])
    if state['status']=='closed':
        opened,closed=commission(spec,state['entry'],state['exit'],mult,state['entry_ts'],state['exit_ts'],calendar)
        gross=sign*(state['exit']-state['entry'])*mult
        state.update(gross_yuan=gross,fees=opened+closed,open_fee=opened,close_fee=closed,
                     net_yuan=gross-opened-closed,net_r=(gross-opened-closed)/state['risk_yuan'],
                     hit200=gross-opened-closed>=config['target_net_yuan']-1e-7)
    elif state['entry'] is not None and minutes:
        b=minutes[bisect.bisect_right(minutes.ends if isinstance(minutes,MinuteBars) else [x['ts'] for x in minutes],last_ts)-1]
        exit_est=b['close']-sign*slip*tick
        opened,closed=commission(spec,state['entry'],exit_est,mult,state['entry_ts'],b['ts'],calendar)
        state['mark_to_market_yuan']=sign*(exit_est-state['entry'])*mult-opened-closed
    return state


def replay_stage(prepared,scan,output):
    from pathlib import Path
    prepared,scan=Path(prepared),Path(scan)
    verify_seal(prepared)
    out=new_output(output)
    protocol=read(prepared/'protocol.json'); profile=read(prepared/'fees.json')
    calendar=Calendar(read(prepared/'calendar.json'),protocol['night_ends'])
    coverage=read(prepared/'coverage.json')
    bycontract=collections.defaultdict(list)
    snapshots=rows(scan/'snapshots.jsonl')
    for s in snapshots: bycontract[s['contract']].append(s)
    def labels():
        for info in coverage['contracts']:
            minutes=MinuteBars(rows(prepared/info['minutes_file'])); gaps=read(prepared/info['gaps_file'])
            spec=profile['products'][info['symbol'][:-1]]
            for s in bycontract[info['contract']]:
                for policy in ('cash200','s1'):
                    for slip in protocol['execution']['slip_scenarios']:
                        yield evaluate(s,minutes,gaps,spec,calendar,protocol['execution'],policy,slip)
            print(info['contract']+': replayed '+str(len(bycontract[info['contract']]))+' candidates',flush=True)
    write_rows(out/'labels.jsonl',labels())
    write(out/'context.json',dict(prepared=str(prepared.resolve()),scan=str(scan.resolve())))
    seal(out,'replay',[prepared/'manifest.json',scan/'manifest.json',scan/'snapshots.jsonl'],
         dict(protocol_id=protocol['id']))
    return out


def capacity(snapshots,labels,scores,thresholds,initial_active=None):
    """Each score/threshold must be known at warning. Accepted unknowns reserve symbol."""
    active=dict(initial_active or {}); ledger=[]
    order=sorted(snapshots,key=lambda s:(s['warning_ts'],
                 -(scores[s['candidate_id']] if scores.get(s['candidate_id']) is not None else -math.inf),s['candidate_id']))
    for s in order:
        key=s['candidate_id']; score=scores.get(key)
        if score is None or score<thresholds.get(key,math.inf): continue
        label=labels[key]
        row=dict(candidate_id=key,symbol=s['symbol'],warning_ts=s['warning_ts'],score=score,
                 group_id=s['group_id'],label=label,decision='accepted')
        if not label['plan']['planned_risk_ok'] or s['quality']['status']!='ok':
            row['decision']='ineligible'
        elif active.get(s['symbol'],'')>s['warning_ts']:
            row['decision']='skipped_occupied'
        else:
            active[s['symbol']]=(label.get('release_ts') or
                                 (label['exit_ts'] if label['status'] in TERMINAL and label['exit_ts'] else
                                  '9999-12-31 23:59:59'))
        ledger.append(row)
    return ledger


def metrics(labels):
    labels=list(labels)
    closed=[x for x in labels if x['status']=='closed']
    amounts=[x['net_yuan'] for x in closed]
    wins=[x for x in amounts if x>0]; losses=[x for x in amounts if x<0]
    balance=peak=drawdown=0.
    for label in sorted(closed,key=lambda x:(x['exit_ts'],x['candidate_id'])):
        balance+=label['net_yuan']; peak=max(peak,balance); drawdown=max(drawdown,peak-balance)
    entered=sum(x['entry'] is not None or x.get('possibly_entered',False) for x in labels)
    unknown=sum(x['status'] in UNKNOWN for x in labels)
    def mean(values): return sum(values)/len(values) if values else None
    return dict(total=len(labels),entered=entered,closed=len(closed),unknown=unknown,
                known_fraction=len(closed)/(entered+sum(x['status'] in UNKNOWN and x['entry'] is None and not x.get('possibly_entered',False) for x in labels))
                    if entered+unknown else 0.,net_yuan=sum(amounts),
                mean_net_yuan=mean(amounts),mean_net_r=mean([x['net_r'] for x in closed]),
                win_rate=sum(x>0 for x in amounts)/len(closed) if closed else None,
                hit200_rate=sum(x['hit200'] for x in closed)/len(closed) if closed else None,
                mean_win=mean(wins),mean_loss=mean(losses),
                payoff_ratio=mean(wins)/abs(mean(losses)) if wins and losses else None,
                max_drawdown_yuan=drawdown,worst_yuan=min(amounts,default=None),
                statuses=dict(collections.Counter(x['status'] for x in labels)),
                realized_only=True,complete=unknown==0)


def audit_label(snapshot,label):
    if label['status']!='closed': return
    fills=label['fills']
    if len(fills)!=2 or fills[0]['offset']!='open' or fills[1]['offset']!='close':
        raise ValueError('invalid fill ledger')
    sign=Decimal(1 if snapshot['event']['direction']=='up' else -1)
    gross=sign*(Decimal(str(fills[1]['price']))-Decimal(str(fills[0]['price'])))*Decimal(str(snapshot['multiplier']))
    net=gross-Decimal(str(label['fees']))
    risk=sign*(Decimal(str(fills[0]['price']))-Decimal(str(label['plan']['stop'])))*Decimal(str(snapshot['multiplier']))
    if abs(float(net)-label['net_yuan'])>1e-6 or abs(float(net/risk)-label['net_r'])>1e-8:
        raise ValueError('Decimal cash/R reconciliation failed')
    if abs(float(risk)-label['risk_yuan'])>1e-6 or abs(label['fees']-label['open_fee']-label['close_fee'])>1e-6:
        raise ValueError('risk or fee legs do not reconcile')
    if any(f['volume']!=1 for f in fills):raise ValueError('one-lot constraint failed')
    if label['hit200']!=(float(net)>=label.get('target_net_yuan',200.)-1e-7):raise ValueError('cash success label mismatch')
    if not snapshot['available_at']<=label['entry_ts']<=label['exit_ts']:
        raise ValueError('noncausal fill chronology')
