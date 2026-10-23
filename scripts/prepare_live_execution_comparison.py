"""Freeze original live signal inputs and audit actual execution, without rewriting originals."""
import argparse
import collections
import copy
import datetime as dt
import json
import math
import pathlib
import sqlite3

from report_signal_ablation import jsonl, read_json, key
from reprice_signal_costs import TradingDates, fee_for
from report_paper_candidate_history import csv_write, digest


def timestamp(value):
    for fmt in ('%Y%m%d %H:%M:%S','%Y-%m-%d %H:%M:%S'):
        try:return dt.datetime.strptime(value,fmt).strftime('%Y-%m-%d %H:%M:%S')
        except ValueError:pass
    raise ValueError('Invalid timestamp: '+value)


def local_epoch(value):
    return dt.datetime.fromtimestamp(value,dt.timezone(dt.timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')


def prepare(archive,input_path,rebuilt,legacy_rows,output):
    archive,input_path,rebuilt,legacy_rows,output=map(pathlib.Path,(archive,input_path,rebuilt,legacy_rows,output))
    if output.exists():raise FileExistsError('Use a new directory')
    source=read_json(input_path);state=read_json(archive/'trader_execution.json')
    snapshot=read_json(archive/'execution-snapshot.json')['data']
    profile=read_json(pathlib.Path(__file__).with_name('exchange_fees_20261022.json'))
    events=read_json(rebuilt/'signals.json');forward={key(e):e for e in events}
    symbols=source['symbols'];calendar=TradingDates(symbols)
    ticks={e['symbol']:e['tick_size'] for e in events}
    multipliers={r['symbol']:r['multiplier'] for r in jsonl(legacy_rows/'evaluations.jsonl')}
    con=sqlite3.connect((archive/'source.sqlite').resolve().as_uri()+'?mode=ro',uri=True);con.row_factory=sqlite3.Row
    old={r['id']:dict(r) for r in con.execute('select * from pattern_events')};con.close()
    live=[];lineage=[];by_condition={};condition_by_key={}
    for cid,c in sorted(state['conditions'].items()):
        e=old[c['event_id']]
        assert e['symbol']==c['symbol'] and e['direction']==c['direction'] and e['warning_ts']==c['warning_bar_ts']
        for a,b in [('entry_price','entry'),('stop_price','stop'),('score','entry_score')]:
            if abs(c[a]-e[b])>1e-7:raise ValueError('Condition and archived signal geometry differ')
        # Only original warning information is read. Do not use historical outcome/trigger values.
        ev=copy.deepcopy(events[0])
        for f in ('symbol','direction','grade','level','s0_ts','s1_ts','s2_ts','s0_price','s1_price','s2_price',
                  'a_move','b_move','a_bars','b_bars','retracement','warning_ts','warning_kind','entry','stop','target','risk','rr'):
            ev[f]=e[f]
        ev['entry']=c['entry_price'];ev['stop']=c['stop_price'];ev['risk']=c['risk']
        # The old order's effective target may have been raised to 1R. Candidates must use original S1.
        ev['target']=e['s1_price'];sign=1 if ev['direction']=='up' else -1
        ev['rr']=sign*(ev['target']-ev['entry'])/ev['risk']
        ev['warning_extreme']=ev['entry']-sign*ticks[ev['symbol']]
        ev['trigger_level']=ev['entry'];ev['tick_size']=ticks[ev['symbol']]
        ev['setup_features']['setup_quality']=c['score']
        for f in ('a_move','b_move','a_bars','b_bars','retracement','grade','level','direction'):
            ev['setup_features'][f]=e[f]
        for f in ('evaluation','trigger_features','trigger_bar_ts','entry_ts','entry_price','outcome','market_context'):ev[f]=None
        ev['pattern_version']='server-live-warning-v6-frozen';ev['execution_version']='research-v5'
        event_key=key(ev)
        if event_key in condition_by_key:raise ValueError('Multiple conditions for one immutable event key')
        condition_by_key[event_key]=cid;by_condition[cid]=ev;live.append(ev)
        present=event_key in forward
        geometry_matches=present and all(abs(ev[f]-forward[event_key][f])<1e-7 for f in ('entry','stop','target','risk'))
        created=local_epoch(c['created_at'])
        lineage.append(dict(condition_id=cid,event_id=c['event_id'],event_key=event_key,
                            symbol=c['symbol'],warning_ts=c['warning_bar_ts'],condition_created_at=created,
                            safe_available_at=max(created,c['warning_bar_ts']),
                            original_target_s1=e['s1_price'],legacy_effective_target=c['target_price'],
                            reset_effective_target_to_original_s1=abs(c['target_price']-e['s1_price'])>1e-7,
                            present_in_current_forward_detector=present,forward_geometry_matches=geometry_matches))
    orders_by=collections.defaultdict(list)
    for ref,o in state['orders'].items():orders_by[o['condition_id']].append(dict(order_ref=ref,**o))
    groups=collections.defaultdict(list)
    for r in state['history']:groups[r['condition_order_id']].append(r)
    if state['history']!=read_json(archive/'trade-history.json')['data']:
        raise ValueError('History API and persistent state differ at collection')
    if len({r['id'] for r in state['history']})!=len(state['history']):raise ValueError('Duplicate fill history ID')
    actual=[];leg_audit=[];anomalies=[];actual_as_rows=[]
    for cid,records in sorted(groups.items()):
        c=state['conditions'][cid];pos=state['positions'][cid];ev=by_condition[cid]
        symbol=c['symbol'];sign=1 if c['direction']=='up' else -1;mult=multipliers[symbol];tick=ticks[symbol]
        records=sorted(records,key=lambda r:(timestamp(r['exit_ts']),r['id']))
        if not pos['is_closed']:raise ValueError('Partially closed position requires separate realized-leg accounting')
        filled_open=sum(o['filled'] for o in orders_by[cid] if o['reason'] is None)
        volume=sum(r['volume'] for r in records)
        if filled_open!=volume:raise ValueError('Open/close lot volumes do not reconcile: '+cid)
        initialrisk=records[0]['risk']*mult*volume
        if any(abs(r['fill_price']-records[0]['fill_price'])>1e-7 or abs(r['risk']-records[0]['risk'])>1e-7 for r in records):
            raise ValueError('Different entry bases within condition; reconstruct weighted fills first')
        flags=[];fees=[];gross=[]
        for r in records:
            points=sign*(r['exit_price']-r['fill_price'])
            if abs(points-r['realized_pnl_pts'])>1e-7:raise ValueError('Recorded point PnL differs from fills')
            g=points*mult*r['volume'];gross.append(g)
            prod=symbol.rstrip('0123456789');rules=profile['products'][prod]
            opened,closed=timestamp(r['open_ts']),timestamp(r['exit_ts'])
            if 'roundtrip_fixed_yuan_per_lot' in rules:f=rules['roundtrip_fixed_yuan_per_lot']*r['volume']
            else:
                close_kind='close_today' if calendar.day(opened)==calendar.day(closed) else 'close_yesterday'
                f=fee_for(rules['open'],r['fill_price'],r['volume'],mult)+fee_for(rules[close_kind],r['exit_price'],r['volume'],mult)
            fees.append(f)
            correction=abs(g-r['realized_pnl_amount'])>1e-7
            if correction:flags.append('stored_cash_amount_incorrect')
            if opened<ev['warning_ts']:flags.append('entry_before_warning_close')
            if r.get('event_id') is None:flags.append('legacy_lineage_backfilled_from_condition')
            if r['exit_reason']=='target_1r' and sign*(r['exit_price']-(r['fill_price']+sign*r['risk'])) < -2*tick-1e-7:
                flags.append('target1r_fill_more_than2ticks_adverse')
            if r['exit_reason']=='stop' and sign*(r['initial_stop_price']-r['exit_price'])>2*tick+1e-7:
                flags.append('stop_fill_more_than2ticks_adverse')
            leg_audit.append(dict(condition_id=cid,trade_id=r['id'],symbol=symbol,volume=r['volume'],
                                  open_ts=opened,exit_ts=closed,entry=r['fill_price'],exit=r['exit_price'],
                                  gross_points_per_lot=points,multiplier=mult,stored_amount=r['realized_pnl_amount'],
                                  corrected_gross=g,fee_snapshot_estimate=f,net_estimate=g-f,cash_correction=g-r['realized_pnl_amount']))
        rejects=[o for o in orders_by[cid] if o['phase']=='Rejected']
        nonfund=[o for o in rejects if '[31]' not in o['note'] and not o['note'].startswith('31:')]
        if any(o['reason'] is not None for o in nonfund):flags.append('documented_exit_reject_or_sync_block')
        if any('[31]' in o['note'] for o in rejects):flags.append('documented_funding_rejections')
        adverse=sign*(records[0]['fill_price']-ev['entry'])/tick
        if adverse>max(.2*ev['risk']/tick,2)+1e-6:flags.append('entry_exceeds_candidate_chase_limit')
        open_ts=timestamp(records[0]['open_ts']);exit_ts=max(timestamp(r['exit_ts']) for r in records)
        gross_sum=math.fsum(gross);fee_sum=math.fsum(fees);net=gross_sum-fee_sum
        actual.append(dict(condition_id=cid,event_key=key(ev),symbol=symbol,direction=c['direction'],tier=c['tier'],
                           warning_ts=ev['warning_ts'],open_ts=open_ts,exit_ts=exit_ts,
                           exit_trading_day=calendar.day(exit_ts),volume=volume,close_legs=len(records),
                           stored_cash_sum=math.fsum(r['realized_pnl_amount'] for r in records),gross=gross_sum,
                           fees_estimate=fee_sum,net_estimate=net,net_r=net/initialrisk,
                           initial_risk_yuan=initialrisk,adverse_entry_ticks=adverse,
                           flags=';'.join(sorted(set(flags))),has_nonfund_exit_reject=bool(nonfund),
                           rejected_orders=len(rejects)))
        actual_as_rows.append(dict(event_key=key(ev),symbol=symbol,warning_ts=ev['warning_ts'],direction=c['direction'],
                                   exit_trading_day=calendar.day(exit_ts),
                                   result={'evaluation':dict(status='closed',net_amount=net,net_r=net/initialrisk,
                                       entry_ts=open_ts,exit_ts=exit_ts,volume=volume,exit_reason=records[-1]['exit_reason'])},
                                   cost_repricing=dict(gross_amount_after_slippage=gross_sum,commission=fee_sum)))
        for flag in sorted(set(flags)):
            anomalies.append(dict(condition_id=cid,symbol=symbol,flag=flag,event_key=key(ev),
                                  gross=gross_sum,net_estimate=net,evidence='persisted fills/condition/order records',
                                  note='execution deviations are retained in actual PnL; replay is a separate counterfactual'))
    order_exceptions=[]
    for cid,orders in orders_by.items():
        for o in orders:
            if o['phase']=='Rejected':order_exceptions.append(dict(condition_id=cid,order_ref=o['order_ref'],
                symbol=state['conditions'].get(cid,{}).get('symbol'),reason=o['reason'],
                offset=o['request']['offset'],at=local_epoch(o['action_at']),note=o['note']))
    output.mkdir(parents=True)
    for name,value in [('live-signals.json',live),('lineage.json',lineage),('actual-as-rows.json',actual_as_rows)]:
        (output/name).write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    csv_write(output/'actual-trades-corrected.csv',actual)
    csv_write(output/'actual-fill-accounting-corrections.csv',leg_audit)
    csv_write(output/'execution-anomalies.csv',anomalies)
    csv_write(output/'rejected-orders.csv',order_exceptions)
    csv_write(output/'signal-lineage.csv',lineage)
    summary=dict(server_filled_conditions=len(state['positions']),server_closed_conditions=len(groups),
                 server_active_conditions=sum(not r['is_closed'] for r in state['positions'].values()),
                 history_close_legs=len(state['history']),conditions=len(live),orders=len(state['orders']),
                 cash_corrected_legs=sum(abs(r['cash_correction'])>1e-7 for r in leg_audit),
                 stored_gross_amount=math.fsum(r['realized_pnl_amount'] for r in state['history']),
                 corrected_gross=math.fsum(r['gross'] for r in actual),
                 estimated_fees=math.fsum(r['fees_estimate'] for r in actual),
                 estimated_net=math.fsum(r['net_estimate'] for r in actual),
                 anomaly_counts=dict(collections.Counter(r['flag'] for r in anomalies)),
                 rejection_counts=dict(collections.Counter(r['note'].split(':')[0] if '资金' not in r['note'] else 'funding_error31' for r in order_exceptions)),
                 full_history_source=source['provenance'],
                 lineage=dict(all_conditions_resolve_to_archived_event=True,
                              current_detector_identity_matches=sum(r['present_in_current_forward_detector'] for r in lineage),
                              current_detector_geometry_matches=sum(r['forward_geometry_matches'] for r in lineage),
                              restored_original_s1_targets=sum(r['reset_effective_target_to_original_s1'] for r in lineage)),
                 collection=read_json(archive/'collection-manifest.json'),
                 hashes={str(p):digest(p) for p in [input_path,archive/'trader_execution.json',archive/'trade-history.json',pathlib.Path(__file__)]})
    (output/'audit-summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in summary.items() if k not in ['collection','hashes','full_history_source']},ensure_ascii=True,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ['archive','input','rebuilt','legacy-rows','output']:p.add_argument('--'+name,required=True)
    a=p.parse_args();prepare(a.archive,a.input,a.rebuilt,a.legacy_rows,a.output)
