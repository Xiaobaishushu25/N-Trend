"""Evaluate frozen warning-only vetoes with full opportunity recompetition."""
import collections,json,statistics
import research_swing_entry_economics as r
import research_swing_abstop as s
from swing_entry_economics import MODELS,plan_features,allows,eligible_rows
from report_swing_c_structure import distribution,equal,SUFFIXES
from research_swing_combination import weekly_delta
w=r.w;RUN=r.RUN

def run():
    p=w.read(RUN/'protocol.json');assert w.sha(w.REPO/'scripts/swing_entry_economics.py')==p['feature_sha256'];allfeatures=w.read(RUN/'warning-features.json');fees=w.read(p['periods']['early']['fees']);references={'best_a':w.read(r.SNAP/'results.json'),'unmerged':w.read(w.RUN/'results.json')};qa=collections.Counter();results={pol:{q:{} for q in ('early','recent','recent_aug_oct','combined')} for pol in ('original','two_paths_flat')};accepted={};ledgers={};controls=[];funnel={};masks={};plan_archive={};leave={}
    for family,root in r.FAMILIES.items():
        events={};ends={};cases=collections.defaultdict(list);decisions={};plans={};family_features={};family_funnel={}
        for q in ('early','recent'):
            source=p['sources'][family][q];assert w.sha(source['raw'])==source['raw_sha256'];assert w.sha(source['pool'])==source['pool_sha256'];assert w.sha(source['manifest'])==source['manifest_sha256'];pool=w.read(source['pool']);snaps={z['event_key']:z for z in pool};events.update({k:z['event'] for k,z in snaps.items()});features=allfeatures[family+'/'+q];family_features.update(features);cal=s.TradingDates(w.read(p['periods'][q]['input'])['symbols']);counts=collections.Counter()
            for code,end in w.read(source['manifest'])['contract_ends'].items():ends[code]=max(end,ends.get(code,''))
            for line in open(source['raw'],encoding='utf-8'):
                z=json.loads(line);prefix,suffix=z['scenario'].split('/',1);assert prefix=='P2_wave_adaptive';assert suffix in SUFFIXES;k=z['event_key'];assert k in snaps
                pp=plan_features(z)
                if k in plans:assert pp==plans[k];qa['same_warning_plan_all_stresses']+=1
                else:
                    plans[k]=pp;f=features[k];assert f['as_of']==z['warning_ts'];decisions[k]={model:allows(model,f,pp) for model in MODELS};qa['warning_only_decisions']+=1
                e=s.ev(z)
                if e['status']=='closed':
                    fs=z['result']['fills'];assert len(fs)==2;gross=(fs[1]['price']-fs[0]['price'])*(1 if z['direction']=='up' else -1)*z['multiplier'];assert abs(gross-e['net_amount'])<1e-6;qa['source_gross_cash']+=1
                counts[e['status']]+=1;z=s.reprice(z,fees,cal);z['period']=q
                if s.ev(z)['status']=='closed':z['exit_trading_day']=cal.day(s.ev(z)['exit_ts']);assert abs(s.ev(z)['net_r']-s.ev(z)['net_amount']/(s.ev(z)['risk']*z['multiplier']))<1e-9
                cases[suffix].append(z)
            assert set(features)==set(snaps);family_funnel[q]=dict(counts)
        assert set(plans)==set(events);masks[family]=decisions;plan_archive[family]=plans;funnel[family]=family_funnel
        for suffix,rows in cases.items():
            assert len(rows)==len(events)
            for model in MODELS:
                case=family+'/'+model+'/'+suffix;chosen=eligible_rows(rows,decisions,model)
                for pol in results:
                    select=(lambda zz,reverse=False:s.portfolio(zz,events,ends,reverse=reverse)) if pol=='original' else (lambda zz,reverse=False:s.flat.exposure_portfolio(zz,ends,reverse))
                    aa,ll=select(chosen);rev,_=select(chosen,True);accepted[(pol,case)]=aa;ledgers[(pol,case)]=ll
                    for q in results[pol]:
                        keep=lambda z:True if q=='combined' else z['period']=='recent' and z['warning_ts']>='2026-08-01 00:00:00' if q=='recent_aug_oct' else z['period']==q
                        m=distribution([z for z in aa if keep(z)],events);m['reverse_tie_net']=s.metrics([z for z in rev if keep(z)])['net'];results[pol][q][case]=m
                        if model=='base' and q!='combined':
                            ref=references[family][pol][q];b=ref[suffix if family=='best_a' else 'unmerged/adaptive/'+suffix];bad={k:[m[k],b[k]] for k in b if k in m and not equal(m[k],b[k])};assert not bad,(family,pol,q,suffix,bad);controls.append(dict(family=family,policy=pol,period=q,scenario=suffix,exact=True))
                    assert abs(results[pol]['combined'][case]['net']-results[pol]['early'][case]['net']-results[pol]['recent'][case]['net'])<1e-7
                    leave[(pol,case)]={sym:s.metrics([z for z in aa if z['symbol']!=sym]) for sym in sorted({z['symbol'] for z in rows})}
    evaluation={};attribution={};scorecard=[]
    for family in r.FAMILIES:
        for model in MODELS:
            name=family+'/'+model;evaluation[name]={};attribution[name]={}
            for pol in results:
                evaluation[name][pol]={};attribution[name][pol]={}
                for sf in SUFFIXES:
                    case=name+'/'+sf;base=family+'/base/'+sf;m=results[pol]['combined'][case];b=results[pol]['combined'][base];strongest=results[pol]['combined']['unmerged/base/'+sf]
                    flags=dict(total_gain_within_family=m['net']>b['net']+1e-8,total_gain_vs_best_known=m['net']>strongest['net']+1e-8,unknown_not_more=m['unknown']<=b['unknown'],drawdown_better=m['realized_cash_drawdown']<b['realized_cash_drawdown']-1e-8,negative_month_burden_better=m['negative_warning_month_burden']<b['negative_warning_month_burden']-1e-8,worst_month_better=m['worst_warning_month']>b['worst_warning_month']+1e-8,product_hhi_better=m['positive_product_hhi'] is not None and b['positive_product_hhi'] is not None and m['positive_product_hhi']<b['positive_product_hhi']-1e-12)
                    evaluation[name][pol][sf]=dict(flags=flags,delta_within_family=m['net']-b['net'],delta_vs_best_known=m['net']-strongest['net'],early_net=results[pol]['early'][case]['net'],recent_net=results[pol]['recent'][case]['net'])
                    old=accepted[(pol,base)];new=accepted[(pol,case)];oc={z['event_key']:z for z in old if s.ev(z)['status']=='closed'};nc={z['event_key']:z for z in new if s.ev(z)['status']=='closed'};common=oc.keys()&nc.keys();removed=oc.keys()-nc.keys();added=nc.keys()-oc.keys()
                    assert all(oc[k]['result']==nc[k]['result'] and oc[k]['cost_repricing']==nc[k]['cost_repricing'] for k in common);qa['unchanged_common_execution_and_fee']+=len(common)
                    dn=sum(s.ev(nc[k])['net_amount'] for k in added);do=sum(s.ev(oc[k])['net_amount'] for k in removed);assert abs(m['net']-b['net']-(dn-do))<1e-6
                    contribution=collections.defaultdict(float)
                    for sign,zz in ((1,new),(-1,old)):
                        for z in zz:
                            if s.ev(z)['status']=='closed':contribution[z['symbol']]+=sign*s.ev(z)['net_amount']
                    attribution[name][pol][sf]=dict(common_closed=len(common),common_cash_delta=0.,removed_n=len(removed),removed_original_net=do,new_n=len(added),new_net=dn,by_product_delta=dict(sorted(contribution.items(),key=lambda kv:kv[1],reverse=True)),warning_week_delta_interval=weekly_delta(old,new),periods={})
                    for q in ('early','recent'):
                        rr=[k for k in removed if oc[k]['period']==q];nn=[k for k in added if nc[k]['period']==q];attribution[name][pol][sf]['periods'][q]=dict(removed_n=len(rr),removed_original_net=sum(s.ev(oc[k])['net_amount'] for k in rr),new_n=len(nn),new_net=sum(s.ev(nc[k])['net_amount'] for k in nn),common_closed=sum(oc[k]['period']==q for k in common))
            sf=SUFFIXES[0];m=results['original']['combined'][name+'/'+sf];b=results['original']['combined'][family+'/base/'+sf];scorecard.append(dict(family=family,model=model,name=name,total=m['net'],early=results['original']['early'][name+'/'+sf]['net'],recent=results['original']['recent'][name+'/'+sf]['net'],delta_within_family=m['net']-b['net'],delta_vs_best_a=m['net']-results['original']['combined']['best_a/base/'+sf]['net'],delta_vs_unmerged=m['net']-results['original']['combined']['unmerged/base/'+sf]['net'],n=m['n'],unknown=m['unknown'],drawdown=m['realized_cash_drawdown'],negative_month_burden=m['negative_warning_month_burden'],worst_month=m['worst_warning_month'],positive_months=m['positive_warning_months'],product_hhi=m['positive_product_hhi'],top3_product=m['top3_product'],top3_day=m['top3_day'],within_family_stresses_improved=sum(evaluation[name]['original'][x]['flags']['total_gain_within_family'] for x in SUFFIXES),gain_both_A_families=all(results['original']['combined'][f+'/'+model+'/'+sf]['net']>results['original']['combined'][f+'/base/'+sf]['net']+1e-8 for f in r.FAMILIES),all_stress_and_policies_gain=all(evaluation[name][pol][x]['flags']['total_gain_within_family'] for pol in results for x in SUFFIXES)))
    scorecard.sort(key=lambda z:z['total'],reverse=True)
    assert all(w.sha(r.SNAP/k)==v for k,v in w.read(r.SNAP/'manifest.json').items());assert all(w.sha(k)==v for k,v in p['production_before'].items())
    for family in r.FAMILIES:
        for q in ('early','recent'):source=p['sources'][family][q];assert w.sha(source['raw'])==source['raw_sha256'] and w.sha(source['pool'])==source['pool_sha256']
    for name,obj in [('results.json',results),('scorecard.json',scorecard),('evaluation.json',evaluation),('attribution.json',attribution),('selection-masks.json',masks),('warning-plans.json',plan_archive),('source-funnel.json',funnel),('leave-product-out.json',{str(k):v for k,v in leave.items()}),('opportunity-ledgers.json',{str(k):v for k,v in ledgers.items()})]:w.write(RUN/name,obj)
    with (RUN/'accepted.jsonl').open('x',encoding='utf-8') as out:
        for (pol,case),aa in accepted.items():
            for z in aa:out.write(json.dumps(dict(policy=pol,case=case,period=z['period'],row=z),ensure_ascii=False)+'\n')
    w.write(RUN/'verification.json',dict(qa=dict(qa),controls=controls,production_unchanged=169,best_a_preserved=True,source_individual_execution_unchanged=True,new_combinations=16,controls_families=2,recent80_gate=False,production_changed=False,qualified_for_trading=False));print(json.dumps([dict(name=z['name'],total=round(z['total'],2),delta_vs_unmerged=round(z['delta_vs_unmerged'],2),drawdown=round(z['drawdown'],2),gain_both_A_families=z['gain_both_A_families']) for z in scorecard],ensure_ascii=False,indent=2),flush=True)

if __name__=='__main__':run()
