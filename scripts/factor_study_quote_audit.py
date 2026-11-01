"""Independent source-quote and Decimal fee checks; never reuses the replay evaluator."""
import collections
import datetime as dt
from decimal import Decimal

from factor_study_data import read,rows,Calendar


def check_quote(snapshot,label,entry_bar,exit_bar,spec,calendar,evidence=None):
    if label['status']!='closed':raise ValueError('quote audit requires closed label')
    sign=1 if snapshot['event']['direction']=='up' else -1
    tick,slip=snapshot['tick'],label['slip_ticks']
    trigger=snapshot['event']['entry']
    entry_base=label['entry']-sign*slip*tick
    exit_base=label['exit']+sign*slip*tick
    if entry_bar['start']<snapshot['available_at']:raise ValueError('entry uses warning candle')
    mode=label.get('entry_mode','breakout')
    if mode=='close_confirm':
        confirmation=(evidence or {}).get('confirmation')
        if (not confirmation or confirmation['ts']!=label['confirmation_ts']
            or confirmation['start']<snapshot['available_at']
            or sign*(confirmation['close']-trigger)<0
            or confirmation['ts']>entry_bar['start']
            or not evidence.get('next_observed_minute')):
            raise ValueError('confirmation not causal or entry not next observed open')
        expected_entry=entry_bar['open']
    elif mode=='retest25':
        breakout=(evidence or {}).get('breakout')
        extreme=(breakout['high'] if sign>0 else breakout['low']) if breakout else None
        if (not breakout or breakout['ts']!=label['break_ts']
            or breakout['start']<snapshot['available_at'] or breakout['ts']>entry_bar['ts']
            or sign*(extreme-trigger)<0):
            raise ValueError('retest entered before a supported breakout')
        D=lambda value:Decimal(str(value))
        raw=D(trigger)-(D(trigger)-D(label['plan']['stop']))/D(4)
        units=(raw/D(tick)).to_integral_value(rounding='ROUND_FLOOR' if sign>0 else 'ROUND_CEILING')
        level=float(units*D(tick))
        if abs(level-label['pullback_price'])>1e-7:raise ValueError('wrong frozen quarter-retest level')
        expected_entry=(entry_bar['open'] if breakout['ts']<entry_bar['ts']
                        and sign*(entry_bar['open']-level)<=0 else level)
    elif mode=='breakout':
        expected_entry=entry_bar['open'] if sign*(entry_bar['open']-trigger)>=0 else trigger
    else:raise ValueError('unknown quote audit entry mode')
    if abs(entry_base-expected_entry)>1e-7:raise ValueError('entry not supported by quote and trigger')
    if not entry_bar['low']-1e-7<=entry_base<=entry_bar['high']+1e-7:raise ValueError('entry outside observed range before slippage')
    target_gap=False
    if label['reason']=='stop':
        stop=label['plan']['stop']
        expected_exit=exit_bar['open'] if sign*(exit_bar['open']-stop)<=0 else stop
    elif label['reason']=='target':
        expected_exit=label['target']
        extreme=exit_bar['high'] if sign>0 else exit_bar['low']
        if sign*(extreme-label['target']) < -1e-7:raise ValueError('target never reached in exit minute')
        target_gap=sign*(exit_bar['open']-label['target'])>1e-7
    elif label['reason'] in {'no_follow','time_exit'}:
        expected_exit=exit_bar['close']
        if dt.datetime.fromisoformat(exit_bar['ts']).minute%15:raise ValueError('time exit before completed 15m bucket')
    else:raise ValueError('unknown exit reason')
    if abs(exit_base-expected_exit)>1e-7:raise ValueError('exit not supported by frozen execution convention')
    if not target_gap and not exit_bar['low']-1e-7<=exit_base<=exit_bar['high']+1e-7:
        raise ValueError('exit outside observed range before slippage')
    D=lambda value:Decimal(str(value))
    if 'roundtrip_fixed_yuan_per_lot' in spec:
        fee=D(spec['roundtrip_fixed_yuan_per_lot'])
    else:
        close_kind='close_today' if calendar.day(label['entry_ts'])==calendar.day(label['exit_ts']) else 'close_yesterday'
        def charge(rule,price):return D(rule['fixed_yuan_per_lot'])+D(rule['notional_rate'])*D(price)*D(snapshot['multiplier'])
        fee=charge(spec['open'],label['entry'])+charge(spec[close_kind],label['exit'])
    if abs(float(fee)-label['fees'])>1e-6:raise ValueError('fees do not match frozen profile and exchange day')
    return target_gap


def audit_quotes(prepared,snapshots,maps):
    prepared=__import__('pathlib').Path(prepared)
    protocol=read(prepared/'protocol.json');coverage=read(prepared/'coverage.json')
    calendar=Calendar(read(prepared/'calendar.json'),protocol['night_ends']);fees=read(prepared/'fees.json')
    snapshots={s['candidate_id']:s for s in snapshots};bycontract=collections.defaultdict(list)
    for labels in maps.values():
        for label in labels.values():
            if label['status']=='closed':bycontract[label['contract']].append(label)
    checked=0;target_gaps=[]
    for contract in coverage['contracts']:
        if not bycontract[contract['contract']]:continue
        quotes={r['ts']:r for r in rows(prepared/contract['minutes_file'])}
        for label in bycontract[contract['contract']]:
            snapshot=snapshots[label['candidate_id']]
            if label['entry_ts'] not in quotes or label['exit_ts'] not in quotes:
                raise ValueError('fill minute not present in source contract')
            entry_bar,exit_bar=quotes[label['entry_ts']],quotes[label['exit_ts']]
            is_gap=check_quote(snapshot,label,entry_bar,exit_bar,fees['products'][label['symbol'][:-1]],calendar)
            checked+=1
            if is_gap:
                target_gaps.append(dict(candidate_id=label['candidate_id'],scenario=label['scenario'],
                                       target=label['target'],exit_minute=exit_bar,
                                       convention='Favorable gap: frozen target less adverse slip, no opening price improvement claimed.'))
    return dict(status='passed',closed_quote_and_fee_checks=checked,
                conservative_target_gap_count=len(target_gaps),target_gap_conventions=target_gaps,
                note='Quotes validate the declared model, not actual tick ordering, queue fills or historical fee availability.')
