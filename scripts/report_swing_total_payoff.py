"""Rank payoff2x2 under user-requested combined-profit and distribution objective."""
import collections,json
import research_swing_total_payoff as r
import research_swing_abstop as s
from report_swing_c_structure import distribution,equal,SUFFIXES
from research_swing_combination import weekly_delta
w=r.w;RUN=r.RUN

def run():
    p=w.read(RUN/'protocol.json');fees=w.read(p['periods']['early']['fees']);ref=w.read(r.main.SNAP/'results.json');events={};cases=collections.defaultdict(list);ends={};qa=collections.Counter()
    for q in ('early','recent'):
        pool=w.read(RUN/(q+'-pool.json'));snaps={z['event_key']:z for z in pool};events.update({k:z['event'] for k,z in snaps.items()});cal=s.TradingDates(w.read(p['periods'][q]['input'])['symbols']);base={}
        for code,ts in w.read(RUN/q/'manifest.json')['contract_ends'].items():ends[code]=max(ts,ends.get(code,''))
        for line in (RUN/q/'evaluations.jsonl').open(encoding='utf-8'):
            z=json.loads(line);profile,suffix=z['scenario'].removeprefix('P2_pay_').split('/',1);e=snaps[z['event_key']]['event'];assert z['entry']==e['entry'] and z['stop']==e['stop'];qa['fixed_entry_and_stop']+=1;key=(z['event_key'],suffix)
            if profile=='base':base[key]=z
            else:
                assert z['target']==base[key]['target']
                for field in ('fill','entry_ts','trigger_ts','risk','volume'):assert s.ev(z)[field]==s.ev(base[key])[field];qa['same_individual_fill_and_risk']+=1
            for fill in z['result']['fills']:
                assert fill['ts']>z['warning_ts'] and abs(fill['price']/z['tick_size']-round(fill['price']/z['tick_size']))<1e-6;qa['fill_tick_time']+=1
            if profile in ('cap2','cap2_hold16') and s.ev(z)['status']=='closed' and s.ev(z)['exit_reason']=='target_s1':assert s.ev(z)['gross_points']/s.ev(z)['risk']<=2.+1e-7;qa['capped_target_gross_R']+=1
            z=s.reprice(z,fees,cal);z['period']=q
            if s.ev(z)['status']=='closed':z['exit_trading_day']=cal.day(s.ev(z)['exit_ts']);assert abs(s.ev(z)['net_r']-s.ev(z)['net_amount']/(s.ev(z)['risk']*z['multiplier']))<1e-9
            cases[profile+'/'+suffix].append(z)
    results={pol:{q:{} for q in ('early','recent','recent_aug_oct','combined')} for pol in ('original','two_paths_flat')};accepted={};ledgers={};controls=[];leave={}
    for case,rows in cases.items():
        for pol in results:
            select=(lambda zz,reverse=False:s.portfolio(zz,events,ends,reverse=reverse)) if pol=='original' else (lambda zz,reverse=False:s.flat.exposure_portfolio(zz,ends,reverse))
            aa,ll=select(rows);rev,_=select(rows,True);accepted[(pol,case)]=aa;ledgers[(pol,case)]=ll
            for q in results[pol]:
                keep=lambda z:True if q=='combined' else z['period']=='recent' and z['warning_ts']>='2026-08-01 00:00:00' if q=='recent_aug_oct' else z['period']==q
                m=distribution([z for z in aa if keep(z)],events);m['reverse_tie_net']=s.metrics([z for z in rev if keep(z)])['net'];results[pol][q][case]=m
                if case.startswith('base/') and q!='combined':
                    b=ref[pol][q][case.split('/',1)[1]];bad={k:[m[k],b[k]] for k in b if k in m and not equal(m[k],b[k])};assert not bad,(pol,q,case,bad);controls.append(dict(policy=pol,period=q,case=case,exact=True))
            assert abs(results[pol]['combined'][case]['net']-results[pol]['early'][case]['net']-results[pol]['recent'][case]['net'])<1e-7
            leave[(pol,case)]={sym:s.metrics([z for z in aa if z['symbol']!=sym]) for sym in sorted({z['symbol'] for z in rows})}
    scores=[];evaluation={};attribution={}
    for profile in r.PROFILES:
        evaluation[profile]={};attribution[profile]={}
        for pol in results:
            evaluation[profile][pol]={};attribution[profile][pol]={}
            for sf in SUFFIXES:
                case=profile+'/'+sf;base='base/'+sf;m=results[pol]['combined'][case];b=results[pol]['combined'][base]
                flags=dict(total_profit_higher=m['net']>b['net']+1e-8,unknown_not_more=m['unknown']<=b['unknown'],drawdown_better=m['realized_cash_drawdown']<b['realized_cash_drawdown']-1e-8,negative_month_burden_better=m['negative_warning_month_burden']<b['negative_warning_month_burden']-1e-8,worst_month_better=m['worst_warning_month']>b['worst_warning_month']+1e-8,product_hhi_better=m['positive_product_hhi']<b['positive_product_hhi']-1e-12,top3_day_better=m['top3_day']<b['top3_day']-1e-12)
                evaluation[profile][pol][sf]=dict(flags=flags,total_delta=m['net']-b['net'],early_net=results[pol]['early'][case]['net'],recent_net=results[pol]['recent'][case]['net'],recent_retention=results[pol]['recent'][case]['net']/results[pol]['recent'][base]['net'])
                old=accepted[(pol,base)];new=accepted[(pol,case)];a={z['event_key']:z for z in old if s.ev(z)['status']=='closed'};bb={z['event_key']:z for z in new if s.ev(z)['status']=='closed'};common=a.keys()&bb.keys();lost=a.keys()-bb.keys();added=bb.keys()-a.keys();dc=sum(s.ev(bb[k])['net_amount']-s.ev(a[k])['net_amount'] for k in common);dn=sum(s.ev(bb[k])['net_amount'] for k in added);do=sum(s.ev(a[k])['net_amount'] for k in lost);assert abs(m['net']-b['net']-(dc+dn-do))<1e-7
                attribution[profile][pol][sf]=dict(common_closed=len(common),common_cash_delta=dc,new_only_n=len(added),new_only_net=dn,old_only_n=len(lost),old_only_net=do,warning_week_delta_interval=weekly_delta(old,new),periods={})
                for q in ('early','recent'):
                    ks=[k for k in common if a[k]['period']==q];changed=[k for k in ks if s.ev(a[k])['net_amount']!=s.ev(bb[k])['net_amount']];attribution[profile][pol][sf]['periods'][q]=dict(common_closed=len(ks),changed=len(changed),common_cash_delta=sum(s.ev(bb[k])['net_amount']-s.ev(a[k])['net_amount'] for k in ks),original_exit_reasons=dict(collections.Counter(s.ev(a[k])['exit_reason'] for k in changed)),new_exit_reasons=dict(collections.Counter(s.ev(bb[k])['exit_reason'] for k in changed)))
        m=results['original']['combined'][profile+'/'+SUFFIXES[0]];scores.append(dict(profile=profile,total=m['net'],delta_vs_best_a=evaluation[profile]['original'][SUFFIXES[0]]['total_delta'],early=results['original']['early'][profile+'/'+SUFFIXES[0]]['net'],recent=results['original']['recent'][profile+'/'+SUFFIXES[0]]['net'],negative_month_burden=m['negative_warning_month_burden'],worst_month=m['worst_warning_month'],drawdown=m['realized_cash_drawdown'],product_hhi=m['positive_product_hhi'],top3_product=m['top3_product'],positive_months=m['positive_warning_months'],unknown=m['unknown'],improved_stresses=sum(evaluation[profile]['original'][sf]['flags']['total_profit_higher'] for sf in SUFFIXES),primary_gain_both_policies=all(evaluation[profile][pol][SUFFIXES[0]]['flags']['total_profit_higher'] for pol in results),all_stress_gain_both_policies=all(evaluation[profile][pol][sf]['flags']['total_profit_higher'] for pol in results for sf in SUFFIXES)))
    scores.sort(key=lambda z:z['total'],reverse=True);r.main.verify_snapshot();assert all(w.sha(k)==v for k,v in p['production_before'].items())
    for name,obj in [('results.json',results),('scorecard.json',scores),('evaluation.json',evaluation),('attribution.json',attribution),('leave-product-out.json',{str(k):v for k,v in leave.items()}),('opportunity-ledgers.json',{str(k):v for k,v in ledgers.items()})]:w.write(RUN/name,obj)
    with (RUN/'accepted.jsonl').open('x',encoding='utf-8') as out:
        for (pol,case),aa in accepted.items():
            for z in aa:out.write(json.dumps(dict(policy=pol,case=case,period=z['period'],row=z),ensure_ascii=False)+'\n')
    w.write(RUN/'verification.json',dict(qa=dict(qa),controls=controls,recent80_gate_used=False,best_a_snapshot_unchanged=True,production_unchanged=169,production_changed=False,qualified_for_trading=False));print(json.dumps(scores,ensure_ascii=False,indent=2),flush=True)

if __name__=='__main__':run()
