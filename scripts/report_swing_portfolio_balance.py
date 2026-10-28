"""Whole chronology portfolio budgets, risk reservations and product balance."""
import bisect,collections,json,statistics
import research_swing_portfolio_balance as r
import research_swing_entry_economics as econ
import research_swing_abstop as s
from swing_portfolio_balance import RULES,select
from research_swing_consistency import cluster
from report_swing_c_structure import distribution,equal,SUFFIXES
from research_swing_combination import weekly_delta
w=r.w;RUN=r.RUN

def interval_audit(ledger,rows,events,rule,qa):
    conf=RULES[rule];bykey={z['event_key']:z for z in rows};accepted=[l for l in ledger if l['accepted']];hist=collections.defaultdict(list);stops=collections.defaultdict(list)
    for l in accepted:
        z=bykey[l['event_key']];e=s.ev(z)
        if e['status']=='closed':
            hist[l['cluster']].append((e['exit_ts'],e['net_amount']))
            if e['exit_reason']=='stop':stops[l['cluster']].append(e['exit_ts'])
    pref={};tt={}
    for c,vals in hist.items():
        vals.sort();tt[c]=[t for t,v in vals];ss=[0.]
        for t,v in vals:ss.append(ss[-1]+v)
        pref[c]=ss
    for c in stops:stops[c].sort()
    for l in ledger:
        c=l['cluster'];i=bisect.bisect_left(tt.get(c,[]),l['start']);net=pref[c][i] if c in pref else 0.;assert abs(net-l['visible_cluster_net'])<1e-7;assert l['visible_cluster_stop']==(bisect.bisect_left(stops.get(c,[]),l['start'])>0);qa['independent_realized_only_history']+=1
    for i,l in enumerate(accepted):
        previous=[p for p in accepted[:i] if p['end']>=l['start']];sym=bykey[l['event_key']]['symbol'];assert not any(bykey[p['event_key']]['symbol']==sym for p in previous)
        if conf.get('cap'):
            active=[p for p in previous if p['sector']==l['sector'] and (not conf['same_direction'] or p['direction']==l['direction'])];assert len(active)<conf['cap'];qa['independent_sector_interval_limit']+=1

def risk_contributions(rows,sector_map):
    by=collections.defaultdict(list)
    for z in rows:
        if s.ev(z)['status']=='closed':by[z['symbol']].append(z)
    total=sum(s.ev(z)['risk']*z['multiplier'] for zz in by.values() for z in zz);out={};sector=collections.defaultdict(lambda:dict(n=0,initial_risk_sum=0.,net=0.,sum_netR=0.))
    for sym,zz in by.items():
        risk=sum(s.ev(z)['risk']*z['multiplier'] for z in zz);net=sum(s.ev(z)['net_amount'] for z in zz);rr=sum(s.ev(z)['net_r'] for z in zz);out[sym]=dict(n=len(zz),mean_actual_initial_cashR=risk/len(zz),initial_risk_share=risk/total if total else None,net=net,sum_netR=rr)
        v=sector[sector_map[sym]];v['n']+=len(zz);v['initial_risk_sum']+=risk;v['net']+=net;v['sum_netR']+=rr
    for v in sector.values():v['initial_risk_share']=v['initial_risk_sum']/total if total else None
    return dict(products=out,sectors=dict(sector),note='Cumulative initial risk of KNOWN CLOSED trades; not simultaneous exposure, capital, actual gap-loss bound or covariance diversification.')

def run():
    p=w.read(RUN/'protocol.json');assert w.sha(w.REPO/p.get('controller_source','scripts/swing_portfolio_balance.py'))==p['controller_sha256'];assert w.sha(r.SECTORS)==p['sector_map_sha256'];fees=w.read(p['periods']['early']['fees']);events={};ends={};raw=collections.defaultdict(list);qa=collections.Counter();references={'budget10':w.read(econ.RUN/'results.json'),'budget_body':w.read(econ.RUN/'body-interaction/results.json')};oldsets=collections.defaultdict(dict)
    for profile,folder in [('budget10',econ.RUN),('budget_body',econ.RUN/'body-interaction')]:
        for line in (folder/'accepted.jsonl').open(encoding='utf-8'):
            z=json.loads(line)
            if z['case'].startswith('unmerged/'+profile+'/'):oldsets[(z['policy'],profile+'/'+z['case'].split('/',2)[2])][z['row']['event_key']]=z['row']
    for q in ['early','recent']:
        paths=p['sources'][q];assert w.sha(paths['raw'])==paths['raw_sha256'] and w.sha(paths['pool'])==paths['pool_sha256'];assert w.sha(paths['manifest'])==paths['manifest_sha256'];events.update({z['event_key']:z['event'] for z in w.read(paths['pool'])});cal=s.TradingDates(w.read(p['periods'][q]['input'])['symbols'])
        for code,end in w.read(paths['manifest'])['contract_ends'].items():ends[code]=max(end,ends.get(code,''))
        for line in open(paths['raw'],encoding='utf-8'):
            z=json.loads(line);profile,suffix=z['scenario'].removeprefix('P2_economics_').split('/',1)
            if profile not in r.PROFILES:continue
            for fill in z['result']['fills']:assert fill['ts']>z['warning_ts'] and abs(fill['price']/z['tick_size']-round(fill['price']/z['tick_size']))<1e-6;qa['source_fill_tick_time']+=1
            z=s.reprice(z,fees,cal);z['period']=q
            if s.ev(z)['status']=='closed':z['exit_trading_day']=cal.day(s.ev(z)['exit_ts']);assert abs(s.ev(z)['net_r']-s.ev(z)['net_amount']/(s.ev(z)['risk']*z['multiplier']))<1e-9
            raw[profile+'/'+suffix].append(z)
    results={pol:{q:{} for q in ['early','recent','recent_aug_oct','combined']} for pol in ['original','two_paths_flat']};accepted={};ledger={};risk={};controls=[];contributions={};leave={}
    for source_case,rows in raw.items():
        profile,suffix=source_case.split('/',1)
        for rule in RULES:
            case=profile+'/'+rule+'/'+suffix
            for pol in results:
                aa,ll,rr=select(rows,events,ends,p['sector_map'],rule,pol);rev,_,_=select(rows,events,ends,p['sector_map'],rule,pol,True);interval_audit(ll,rows,events,rule,qa);accepted[(pol,case)]=aa;ledger[(pol,case)]=ll;risk[(pol,case)]=rr
                for q in results[pol]:
                    keep=lambda z:True if q=='combined' else z['period']=='recent' and z['warning_ts']>='2026-08-01 00:00:00' if q=='recent_aug_oct' else z['period']==q
                    zz=[z for z in aa if keep(z)];m=distribution(zz,events);m['reverse_tie_net']=s.metrics([z for z in rev if keep(z)])['net']
                    if q=='combined':m.update({k:v for k,v in rr.items() if k!='realized_settlements'})
                    results[pol][q][case]=m
                    if rule=='base':
                        ref=references[profile][pol][q]['unmerged/'+source_case];bad={k:[m[k],ref[k]] for k in ref if k in m and not equal(m[k],ref[k])};assert not bad,(pol,q,case,bad);controls.append(dict(policy=pol,period=q,case=case,exact=True))
                if rule=='base':
                    old=oldsets[(pol,source_case)];assert {z['event_key'] for z in aa}==set(old)
                    for z in aa:assert z['result']==old[z['event_key']]['result'] and z.get('cost_repricing')==old[z['event_key']].get('cost_repricing');qa['baseline_accepted_full_result_and_fee']+=1
                contributions[(pol,case)]=risk_contributions(aa,p['sector_map']);leave[(pol,case)]={sym:s.metrics([z for z in aa if z['symbol']!=sym]) for sym in sorted(p['sector_map'])}
    score=[];attribution={};evaluation={}
    for profile in r.PROFILES:
        for rule in RULES:
            name=profile+'/'+rule;attribution[name]={};evaluation[name]={}
            for pol in results:
                attribution[name][pol]={};evaluation[name][pol]={}
                for sf in SUFFIXES:
                    case=name+'/'+sf;base=profile+'/base/'+sf;m=results[pol]['combined'][case];b=results[pol]['combined'][base];best=results[pol]['combined']['budget_body/base/'+sf]
                    flags=dict(total_gain=m['net']>b['net']+1e-8,beats_highest_prior_total=m['net']>best['net']+1e-8,product_HHI_better=m['positive_product_hhi'] is not None and b['positive_product_hhi'] is not None and m['positive_product_hhi']<b['positive_product_hhi']-1e-12,top3_product_better=m['top3_product']<b['top3_product']-1e-12,drawdown_better=m['realized_cash_drawdown']<b['realized_cash_drawdown']-1e-8,month_loss_burden_better=m['negative_warning_month_burden']<b['negative_warning_month_burden']-1e-8,unknown_not_more=m['unknown']<=b['unknown'],reserved_risk_lower=m['peak_reserved_nominal_cashR']<b['peak_reserved_nominal_cashR']-1e-8)
                    evaluation[name][pol][sf]=dict(flags=flags,delta=m['net']-b['net'],reverse_tie_delta=m['reverse_tie_net']-b['reverse_tie_net'])
                    old=accepted[(pol,base)];new=accepted[(pol,case)];a={z['event_key']:z for z in old if s.ev(z)['status']=='closed'};bb={z['event_key']:z for z in new if s.ev(z)['status']=='closed'};common=a.keys()&bb.keys();removed=a.keys()-bb.keys();added=bb.keys()-a.keys();assert all(a[k]['result']==bb[k]['result'] and a[k]['cost_repricing']==bb[k]['cost_repricing'] for k in common);qa['unchanged_common_trade_and_fee']+=len(common)
                    add=sum(s.ev(bb[k])['net_amount'] for k in added);rem=sum(s.ev(a[k])['net_amount'] for k in removed);assert abs(m['net']-b['net']-(add-rem))<1e-6
                    prod=collections.defaultdict(float)
                    for sign,zz in [(1,new),(-1,old)]:
                        for z in zz:
                            if s.ev(z)['status']=='closed':prod[z['symbol']]+=sign*s.ev(z)['net_amount']
                    attribution[name][pol][sf]=dict(common_closed=len(common),common_cash_delta=0.,removed_n=len(removed),removed_net=rem,added_n=len(added),added_net=add,product_delta=dict(sorted(prod.items(),key=lambda kv:kv[1],reverse=True)),warning_week_delta_interval=weekly_delta(old,new))
            sf=SUFFIXES[0];m=results['original']['combined'][name+'/'+sf];b=results['original']['combined'][profile+'/base/'+sf];score.append(dict(name=name,profile=profile,rule=rule,total=m['net'],early=results['original']['early'][name+'/'+sf]['net'],recent=results['original']['recent'][name+'/'+sf]['net'],delta=m['net']-b['net'],delta_vs_highest_prior=m['net']-results['original']['combined']['budget_body/base/'+sf]['net'],n=m['n'],unknown=m['unknown'],drawdown=m['realized_cash_drawdown'],product_hhi=m['positive_product_hhi'],top3_product=m['top3_product'],positive_months=m['positive_warning_months'],negative_month_burden=m['negative_warning_month_burden'],worst_month=m['worst_warning_month'],peak_nominal_reservation_R=m['peak_reserved_nominal_cashR'],peak_sector_nominal_reservation_R=m['peak_one_sector_reserved_nominal_cashR'],reverse_tie_delta=m['reverse_tie_net']-b['reverse_tie_net'],gain_all_costs_and_policies=all(evaluation[name][pol][x]['flags']['total_gain'] for pol in results for x in SUFFIXES),profit_and_product_balance=all(evaluation[name]['original'][sf]['flags'][k] for k in ['total_gain','product_HHI_better','top3_product_better'])))
    score.sort(key=lambda z:z['total'],reverse=True);assert all(w.sha(k)==v for k,v in p['production_before'].items());assert all(w.sha(r.SNAP/k)==v for k,v in w.read(r.SNAP/'manifest.json').items());assert all(w.sha(r.ECON_SNAP/k)==v for k,v in w.read(r.ECON_SNAP/'manifest.json').items())
    for filename,obj in [('results.json',results),('scorecard.json',score),('evaluation.json',evaluation),('attribution.json',attribution),('risk-contributions.json',{str(k):v for k,v in contributions.items()}),('reservation-risk.json',{str(k):v for k,v in risk.items()}),('opportunity-ledgers.json',{str(k):v for k,v in ledger.items()}),('leave-product-out.json',{str(k):v for k,v in leave.items()})]:w.write(RUN/filename,obj)
    with (RUN/'accepted.jsonl').open('x',encoding='utf-8') as out:
        for (pol,case),aa in accepted.items():
            for z in aa:out.write(json.dumps(dict(policy=pol,case=case,period=z['period'],row=z),ensure_ascii=False)+'\n')
    w.write(RUN/'verification.json',dict(qa=dict(qa),controls=controls,new_combinations=(len(RULES)-1)*len(r.PROFILES),one_lot_unchanged=True,source_individual_execution_unchanged=True,production_unchanged=169,named_best_a_preserved=True,economic_candidates_preserved=True,recent80_gate=False,independent_validation=False,production_changed=False,qualified_for_trading=False))
    print(json.dumps([dict(name=z['name'],total=round(z['total'],2),delta=round(z['delta'],2),hhi=round(z['product_hhi'],4) if z['product_hhi'] is not None else None,drawdown=round(z['drawdown'],2),reverse_delta=round(z['reverse_tie_delta'],2),profit_and_product_balance=z['profit_and_product_balance']) for z in score],ensure_ascii=False,indent=2),flush=True)

if __name__=='__main__':run()
