"""Separate unknown PnL from known-flat exposure in the two frozen OHLC paths.

Sensitivity audit only. No unknown trade is priced at zero or made a known win.
"""
import argparse,collections,copy,datetime as dt,json,pathlib
import research_swing_selective as s
from reprice_signal_costs import TradingDates,reprice
RUN=s.RUN/'flat-ambiguity-audit'
STRUCT=s.RUN/'structural-supplement/combined'
SESSION=s.RUN/'session-supplement/combined'
CASES=['base','counter_kind','graded_body_kind','counter_loss','graded_body_kind_loss']

def ambiguity(row):
    e=s.ev(row);d=row['result'].get('disagreement')
    if e['status']!='ambiguous' or not d:return None
    paths=[d['open_high_low_close'],d['open_low_high_close']]
    for p in paths:
        if p['status']=='closed':
            if not(p['entry_ts'] and p['exit_ts'] and p['exit_ts']<=d['ts']):return None
        elif not(p['status'] in ('invalidated','invalidated_before_entry_body','missed_target','unfilled','filtered_net_rr_fill','filtered_actual_risk') and p['entry_ts'] is None):return None
    starts=[p.get('trigger_ts') or p['entry_ts'] for p in paths if p.get('trigger_ts') or p['entry_ts']]
    return dict(start=min(starts) if starts else row['warning_ts'],end=d['ts'],paths=paths)

def exposure_portfolio(rows,ends,reverse=False):
    def start(r):
        a=ambiguity(r);e=s.ev(r)
        return e.get('trigger_ts') or e['entry_ts'] or (a['start'] if a else r['warning_ts'])
    ordered=sorted(rows,key=lambda r:r['event_key'],reverse=reverse);ordered.sort(key=lambda r:(start(r),r['warning_ts']))
    a=[];ledger=[];active={}
    for r in ordered:
        e=s.ev(r);unknown=e['status'] in s.UNKNOWN;amb=ambiguity(r)
        if not(e.get('trigger_ts') or e['entry_ts']) and not unknown:continue
        t=start(r);end=amb['end'] if amb else ends[r['execution_contract']] if unknown else e['exit_ts'];assert end and end>=t
        reason='symbol_slot' if active.get(r['symbol'],'')>=t else None
        ledger.append(dict(event_key=r['event_key'],start=t,end=end,accepted=reason is None,reason=reason,two_paths_flat=bool(amb),pnl_unknown=unknown))
        if reason:continue
        a.append(r);active[r['symbol']]=end
    return a,ledger

def choose(name,e):
    st=s.state(e)
    if name=='base':return 'base'
    if name=='counter_kind':return 'kind' if st['counter'] else 'base'
    if name=='graded_body_kind':return 'body' if st['strong_counter'] else 'kind' if st['counter'] else 'base'
    if name=='counter_loss':return 'loss' if st['counter'] else 'base'
    if name=='graded_body_kind_loss':return 'body_loss' if st['strong_counter'] else 'kind_loss' if st['counter'] else 'base'
    raise ValueError(name)

def prepare():
    RUN.mkdir(exist_ok=False);p=s.read(SESSION/'protocol.json')
    profiles={k:p['profiles'][k] for k in ('base','kind','loss','body_loss','kind_loss')}
    profiles['body']=dict(root=str(s.GEOM),scenario='P2_body_local_wide')
    p.update(id=RUN.name,created_at=dt.datetime.now(dt.timezone.utc).isoformat(),profiles=profiles,cases=CASES,
        script_sha256=s.sha(s.__file__),audit_sha256=s.sha(__file__),
        raw_source_sha256={str(pathlib.Path(q['root'])/period/'evaluations.jsonl'):s.sha(pathlib.Path(q['root'])/period/'evaluations.jsonl') for q in profiles.values() for period in ('early','recent')},
        audit='Keep unknown PnL unknown. Release slot after ambiguous minute ONLY when both frozen paths independently terminate flat (closed or pre-entry cancelled). Start reservation at earliest possible path trigger if outer trigger absent. Any possible open/unknown exposure remains reserved to contract end. Compare BOTH baseline and candidates under identical policy. No adaptive budgets in this audit.',
        pnl='For accepted two-path-flat unknowns, show fee-repriced two-path cash/R scenarios separately from known closed stats. These are two monotone-path sensitivities, not exhaustive bounds on true ticks, spreads or orders. Any non-flat unknown is unpriced.',
        qualified_for_trading=False,production_changed=False)
    s.write(RUN/'protocol.json',p);print(RUN,flush=True)

def path_outcomes(row,fees,cal):
    a=ambiguity(row)
    if not a:return None
    outcomes=[]
    for p in a['paths']:
        if p['status']!='closed':outcomes.append(dict(net=0.,r=0.,trades=0));continue
        rr=copy.deepcopy(row);ee=copy.deepcopy(p);ee['net_amount']=0.;ee['net_r']=0.
        rr['result']=dict(evaluation=ee,disagreement=None,fills=[dict(offset='open',price=ee['fill'],volume=ee['volume'],ts=ee['entry_ts'],reason='entry'),
            dict(offset='close',price=ee['final_price'],volume=ee['volume'],ts=ee['exit_ts'],reason=ee['exit_reason'])])
        rr=reprice(rr,fees,cal);outcomes.append(dict(net=s.ev(rr)['net_amount'],r=s.ev(rr)['net_r'],trades=1))
    return outcomes

def run():
    p=s.read(RUN/'protocol.json');assert s.sha(__file__)==p['audit_sha256'];s.PROFILES={k:(pathlib.Path(v['root']),v['scenario']) for k,v in p['profiles'].items()}
    events,periods,ends,mapping,funnel=s.load(p);cal={q:TradingDates(s.read(p['periods'][q]['input'])['symbols']) for q in ('early','recent')};fees=s.read(p['periods']['early']['fees'])
    result={'early':{},'recent':{}};ledgers={};accepted={};details={};regression=0
    old={name:s.read(root/'results.json') for name,root in [('base',s.PARENT),('counter_kind',s.RUN),('graded_body_kind',STRUCT),('counter_loss',SESSION),('graded_body_kind_loss',SESSION)]}
    for name in CASES:
        for mode in ('corrected_touch','next_minute_open'):
            for slip in (1,2):
                suffix=f'{mode}/slip{slip}';case=name+'/'+suffix;rows=[mapping[(k,suffix,choose(name,e))] for k,e in events.items()]
                closed,ledger=exposure_portfolio(rows,ends);original,ol=s.portfolio(rows,events,ends);accepted[case]=closed;ledgers[case]=ledger
                details[case]=[]
                for period in ('early','recent'):
                    basecase=('P2_cycle_base' if name=='base' else name)+'/'+suffix;b=old[name][period][basecase];orig=s.describe([r for r in original if r['period']==period],events)
                    assert all(orig[k]==b[k] for k in b if k in orig and k!='reverse_tie_net'),(period,case);regression+=1
                    zz=[r for r in closed if r['period']==period];m=s.describe(zz,events);outcomes=[]
                    for r in zz:
                        if s.ev(r)['status'] not in s.UNKNOWN:continue
                        possibilities=path_outcomes(r,fees,cal[period])
                        details[case].append(dict(period=period,event_key=r['event_key'],known_flat_in_both_paths=possibilities is not None,outcomes=possibilities))
                        if possibilities:outcomes.append(possibilities)
                    cashlo=m['net']+sum(min(x['net'] for x in choices) for choices in outcomes);cashhi=m['net']+sum(max(x['net'] for x in choices) for choices in outcomes)
                    rlo=m['total_r']+sum(min(x['r'] for x in choices) for choices in outcomes);rhi=m['total_r']+sum(max(x['r'] for x in choices) for choices in outcomes)
                    nlo=m['n']+sum(min(x['trades'] for x in choices) for choices in outcomes);nhi=m['n']+sum(max(x['trades'] for x in choices) for choices in outcomes)
                    m.update(original_policy_net=orig['net'],original_policy_mean_r=orig['mean_r'],two_path_flat_unknown=len(outcomes),unpriced_nonflat_unknown=m['unknown']-len(outcomes),
                        two_path_cash_range=[cashlo,cashhi],two_path_mean_r_outer_range=[rlo/max(1,nlo if rlo<0 else nhi),rhi/max(1,nlo if rhi>=0 else nhi)])
                    result[period][case]=m
    s.write(RUN/'results.json',result);s.write(RUN/'opportunity-ledgers.json',ledgers);s.write(RUN/'unknown-two-path-scenarios.json',details)
    with (RUN/'accepted.jsonl').open('x',encoding='utf-8') as f:
        for case,rows in accepted.items():
            for r in rows:f.write(json.dumps(dict(case=case,period=r['period'],row=r),ensure_ascii=False)+'\n')
    assert all(s.sha(k)==h for k,h in p['production_before'].items())
    s.write(RUN/'verification.json',dict(original_policy_metrics_exact=regression,production_unchanged=len(p['production_before']),qualified_for_trading=False,
        protocol_sha256=s.sha(RUN/'protocol.json'),results_sha256=s.sha(RUN/'results.json')))
    for name in CASES:
        print(name,[(q,result[q][name+'/corrected_touch/slip1']['n'],round(result[q][name+'/corrected_touch/slip1']['net'],2),round(result[q][name+'/corrected_touch/slip1']['mean_r'],4),result[q][name+'/corrected_touch/slip1']['two_path_cash_range']) for q in ('early','recent')],flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('stage',choices=['prepare','run']);globals()[p.parse_args().stage]()
