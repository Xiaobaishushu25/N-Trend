"""Explain code/geometry/path differences without treating diagnostics as strategies."""
import bisect,collections,copy,datetime as dt,json,math,pathlib,statistics
import research_swing_abstop as s
from diagnose_swing_periods import composition
RUN=s.RUN

def mean(xs):return statistics.mean(xs) if xs else None
def summarize(rows):
    closed=[r for r in rows if s.ev(r)['status']=='closed'];m=s.metrics(rows)
    m['mean_r_original_denominator']=mean([s.ev(r)['net_amount']/(r['frozen_risk']*r['multiplier']) for r in closed]) if closed and 'frozen_risk' in closed[0] else None
    m['status_counts']=dict(collections.Counter(s.ev(r)['status'] for r in rows));m['exit_reasons']=dict(collections.Counter(s.ev(r)['exit_reason'] for r in closed));return m

def run():
    p=s.read(RUN/'protocol.json');fees=s.read(p['periods']['early']['fees']);snapshots={};events={};cohorts={};records={};diag=collections.defaultdict(list);recover=collections.defaultdict(list);controls=0;fullraw=collections.defaultdict(list);ends={};entrychecks=0
    for period in ('early','recent'):
        pool=s.read(RUN/(period+'-pool.json'));snapshots.update({x['event_key']:x for x in pool});events.update({x['event_key']:x['event'] for x in pool})
        bykey={(x['label'],x['event_key']):x for x in s.read(RUN/(period+'-cohorts.json'))};cohorts[period]=bykey
        cal=s.TradingDates(s.read(p['periods'][period]['input'])['symbols'])
        for code,t in s.read(RUN/period/'manifest.json')['contract_ends'].items():ends[code]=max(ends.get(code,''),t)
        for line in (RUN/period/'evaluations.jsonl').open(encoding='utf-8'):
            r=s.reprice(json.loads(line),fees,cal);r['period']=period
            if s.ev(r)['status']=='closed':r['exit_trading_day']=cal.day(s.ev(r)['exit_ts'])
            fullraw[r['scenario']].append(r)
        for line in (RUN/(period+'-diagnostics.jsonl')).open(encoding='utf-8'):
            z=json.loads(line);c=bykey[(z['label'],z['event_key'])];baseline=c['baseline']
            if z['kind']=='recovery':recover[(period,z['label'],z['recovery']['horizon'])].append(dict(z,baseline=baseline));continue
            r=copy.deepcopy(baseline);r['result']=z['result'];r=s.reprice(r,fees,cal)
            if z['kind']=='control':
                assert r['result']==baseline['result'],('control full result',period,z['label'],z['event_key']);assert r['cost_repricing']==baseline['cost_repricing'];controls+=1;continue
            if s.ev(r)['status']=='closed':
                r['exit_trading_day']=cal.day(s.ev(r)['exit_ts']);assert s.ev(r)['fill']==s.ev(baseline)['fill'];assert s.ev(r)['entry_ts']==s.ev(baseline)['entry_ts'];entrychecks+=1
            r['frozen_risk']=z['fixed_risk'];r['counterfactual_stop']=z['stop'];r['counterfactual_target']=z['target'];diag[(period,z['label'],z['scenario'])].append(r)
        for (label,key),c in bykey.items():
            r=c['baseline'];e=s.ev(r);event=c['event'];f=dict(c['mechanism']);sign=1 if r['direction']=='up' else -1;trueB=f.get('b_extreme',event['s2_price']);den=abs(event['s1_price']-trueB)
            f.update(actual_entry_recovered_b=(e['fill']-trueB)*sign/den if den else None,actual_target_r=(r['target']-e['fill'])*sign/e['risk'],actual_s1_r=(event['s1_price']-e['fill'])*sign/e['risk'],actual_risk_atr=e['risk']/snapshots[key]['atr14'],
                a_clean_bin='clean' if (f.get('a_body_eff') or -1)>.5 else 'other',b_speed_bin='faster_than_A' if f.get('b_speed_to_a',0)>1 else 'slower',
                b_weak_bin='unknown' if f.get('b_weakening_ex_warning') is None else 'weakened' if f['b_weakening_ex_warning']<=.75 else 'not_weakened',
                inside_bin=str(f.get('stop_inside_b_range')),warning_kind=event['warning_kind'],level=event['level'],product=r['symbol'],net_r=e['net_r'])
            records[(period,label,key)]=dict(period=period,label=label,row=r,event=event,features=f)
    desc={};groups={};mix={}
    for period,title,include in [('early','early',lambda z:True),('recent','recent_all',lambda z:True),('recent','recent_aug_oct',lambda z:z['row']['warning_ts']>='2026-08-01 00:00:00')]:
        for label in ('reference','observation'):
            zz=[z for (q,l,k),z in records.items() if q==period and l==label and include(z)];rs=[z['row'] for z in zz];m=summarize(rs)
            for field in ['a_bars','b_bars','a_body_eff','a_body_eff_ex_s0','a_gap_share','a_largest_body_share','b_speed_to_a','b_weakening_ex_warning','warning_range_atr','warning_range_to_b','warning_recovered_b','actual_entry_recovered_b','actual_target_r','actual_s1_r','actual_risk_atr','true_b_risk_multiple']:
                m['mean_'+field]=mean([z['features'][field] for z in zz if z['features'].get(field) is not None])
            m['stop_inside_b_count']=sum(z['features']['stop_inside_b_range'] for z in zz);m['s2_warning_count']=sum(z['features']['s2_is_warning'] for z in zz);m['count_mismatch']=sum(not z['features']['anchor_counts_match'] for z in zz);desc[(title,label)]=m
            for field in ['a_clean_bin','b_speed_bin','b_weak_bin','inside_bin','warning_kind','level']:
                bins=collections.defaultdict(list)
                for z in zz:bins[z['features'][field]].append(z['row'])
                groups[(title,label,field)]={k:summarize(v) for k,v in bins.items()}
    early=[z['features'] for (q,l,k),z in records.items() if q=='early' and l=='reference'];recent=[z['features'] for (q,l,k),z in records.items() if q=='recent' and l=='reference' and z['row']['warning_ts']>='2026-08-01 00:00:00']
    for label,fields,n in [('product',['product'],3),('warning',['warning_kind'],1),('inside',['inside_bin'],1),('A_B',['a_clean_bin','b_speed_bin'],3),('product_warning_inside',['product','warning_kind','inside_bin'],3)]:
        mix[label]=composition(early,recent,fields,n)
    diagstats={str(k):summarize(rows) for k,rows in diag.items()};recstats={}
    for key,rows in recover.items():
        rr=[r['recovery'] for r in rows];valid=[r for r in rr if r['quality']=='ok'];recstats[str(key)]=dict(stopped=len(rr),quality_ok=len(valid),complete=sum(r['complete'] for r in valid),
            entry_return=sum(r['entry_recovery'] is not None for r in valid),one_r_return=sum(r['one_r_recovery'] is not None for r in valid),
            entry_before_2r=dict(collections.Counter(r['entry_vs_two_r'] for r in valid)),one_r_before_2r=dict(collections.Counter(r['one_r_vs_two_r'] for r in valid)),
            same_session_n=sum(not r['crossed_long_break'] for r in valid),same_session_entry_return=sum(not r['crossed_long_break'] and r['entry_recovery'] is not None for r in valid),
            same_session_one_r_return=sum(not r['crossed_long_break'] and r['one_r_recovery'] is not None for r in valid))
    full={policy:{q:{} for q in ('early','recent')} for policy in ('original','two_paths_flat')};accepted={};ledgers={};baselinechecks=0
    old=s.read(SOURCE_RESULTS:=s.SOURCE/'results.json');obs=s.read(s.ROOT/'execution-interaction/results.json')['original']
    for case,raw in fullraw.items():
        for policy in full:
            a,l=s.portfolio(raw,events,ends) if policy=='original' else s.flat.exposure_portfolio(raw,ends);accepted[(policy,case)]=a;ledgers[(policy,case)]=l
            for q in ('early','recent'):
                rows=[r for r in a if r['period']==q];full[policy][q][case]=s.describe(rows,events)
                if policy=='original' and case.split('/')[0] in ('P2_ab_base','P2_ab_adaptive'):
                    b=old[q]['P2_cycle_base/'+case.split('/',1)[1]] if case.startswith('P2_ab_base/') else obs[q]['strong_kind_session/'+case.split('/',1)[1]]
                    assert all(full[policy][q][case][k]==b[k] for k in b if k in full[policy][q][case] and k!='reverse_tie_net'),('full control',q,case);baselinechecks+=1
    s.write(RUN/'descriptive.json',{str(k):v for k,v in desc.items()});s.write(RUN/'groups.json',{str(k):v for k,v in groups.items()});s.write(RUN/'composition.json',mix)
    s.write(RUN/'fixed-entry-diagnostics.json',diagstats);s.write(RUN/'post-stop-recovery.json',recstats);s.write(RUN/'full-portfolio.json',full);s.write(RUN/'opportunity-ledgers.json',{str(k):v for k,v in ledgers.items()})
    s.write(RUN/'feature-trades.json',list(records.values()))
    with (RUN/'accepted.jsonl').open('x',encoding='utf-8') as f:
        for (policy,case),a in accepted.items():
            for r in a:f.write(json.dumps(dict(policy=policy,case=case,period=r['period'],row=r),ensure_ascii=False)+'\n')
    assert all(s.sha(k)==h for k,h in p['production_before'].items());assert all(s.sha(RUN/'fork'/k)==h for k,h in s.read(RUN/'implementation-manifest.json').items())
    s.write(RUN/'verification.json',dict(actual_trade_controls_exact=controls,diagnostic_closed_entries_exact=entrychecks,full_portfolio_controls_exact=baselinechecks,production_unchanged=len(p['production_before']),
        protocol_sha256=s.sha(RUN/'protocol.json'),diagnostics_sha256=s.sha(RUN/'fixed-entry-diagnostics.json'),production_changed=False,qualified_for_trading=False))
    print('Descriptive',json.dumps({str(k):{f:v[f] for f in ['n','mean_r','mean_a_bars','mean_b_bars','mean_a_body_eff','mean_b_speed_to_a','stop_inside_b_count','mean_actual_entry_recovered_b','mean_actual_target_r']} for k,v in desc.items()},ensure_ascii=False),flush=True)
    print('Full portfolios',[(q,k,round(v['net'],2),round(v['mean_r'],4) if v['mean_r'] is not None else None,v['n'],v['unknown']) for q in ('early','recent') for k,v in full['original'][q].items() if k.endswith('corrected_touch/slip1')],flush=True)

if __name__=='__main__':run()
