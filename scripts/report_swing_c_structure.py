"""Audit C-owned structure causality and rank under combined-profit objective."""
import bisect,collections,datetime as dt,json,math,statistics
import research_swing_c_structure as r
import research_swing_abstop as s
from report_swing_b_transition import enhance
from research_swing_combination import weekly_delta
w=r.w;RUN=r.RUN
SUFFIXES=['corrected_touch/slip1','corrected_touch/slip2','next_minute_open/slip1','next_minute_open/slip2']

def equal(a,b):
    if isinstance(a,(int,float)) and isinstance(b,(int,float)):return abs(a-b)<1e-7
    return a==b
def post_entry5(raw):
    groups=collections.defaultdict(list)
    for b in raw:
        t=dt.datetime.fromisoformat(b['datetime']);end=t+dt.timedelta(minutes=(-t.minute)%5);groups[end.isoformat(sep=' ')].append(b)
    out=[]
    for end,bs in sorted(groups.items()):
        if len(bs)!=5 or bs[-1]['datetime']!=end or any(b['volume']<=0 for b in bs):continue
        if any((dt.datetime.fromisoformat(b['datetime'])-dt.datetime.fromisoformat(a['datetime'])).total_seconds()!=60 for a,b in zip(bs,bs[1:])):continue
        out.append(dict(datetime=end,high=max(b['high'] for b in bs),low=min(b['low'] for b in bs)))
    return out
def audit_trace(z,bs,times,qa):
    traces=z['c_structure_trace'];e=s.ev(z);origin=None
    if not traces:return
    assert e['entry_ts'] is not None;start=bisect.bisect_right(times,e['entry_ts']);end=bisect.bisect_right(times,max(t['ts'] for t in traces));candles=post_entry5(bs[start:end]);index={b['datetime']:i for i,b in enumerate(candles)};sign=1 if z['direction']=='up' else -1;tick=z['tick_size'];wick=z['c_structure_mode'].endswith('wick');push=z['c_structure_mode'].startswith('push');candidate={};active=None
    def pivot(ts,adverse):
        i=index[ts];assert 0<i<len(candles)-1;aa,b,cc=candles[i-1:i+2];assert (dt.datetime.fromisoformat(b['datetime'])-dt.datetime.fromisoformat(aa['datetime'])).total_seconds()==300;assert (dt.datetime.fromisoformat(cc['datetime'])-dt.datetime.fromisoformat(b['datetime'])).total_seconds()==300
        field='low' if (sign>0)==adverse else 'high';good=(b[field]<aa[field] and b[field]<cc[field]) if field=='low' else (b[field]>aa[field] and b[field]>cc[field]);assert good
        return b[field],cc['datetime']
    for tr in traces:
        ts=tr['ts'];raw=bs[bisect.bisect_left(times,ts)];assert raw['datetime']==ts
        if tr['kind']=='origin':
            assert z.get('c_initial_origin') is not None and ts==e['entry_ts'] and tr['pivot_ts']==e['entry_ts'];assert abs(tr['price']-z['c_initial_origin'])<1e-8;origin=tr['price'];qa['known_B_origin_is_reference_not_observed_pivot']+=1;continue
        assert ts>e['entry_ts']
        if tr['kind'] in ('adverse_pivot','favorable_pivot'):
            price,known=pivot(tr['pivot_ts'],tr['kind']=='adverse_pivot');assert abs(tr['price']-price)<1e-8 and ts==known;qa['independent_strict_post_entry_pivot']+=1
        elif tr['kind']=='candidate':
            price,known=pivot(tr['pivot_ts'],True);prev=origin if origin is not None and tr['previous_pivot_ts']==e['entry_ts'] else pivot(tr['previous_pivot_ts'],True)[0];opp,_=pivot(tr['opposite_ts'],False);assert tr['previous_pivot_ts']<tr['opposite_ts']<tr['pivot_ts'];assert (price-prev)*sign>0;assert abs(tr['trigger']-(opp+sign*tick))<1e-8 and ts==known;candidate[tr['pivot_ts']]=tr;qa['independent_higher_low_with_intervening_push']+=1
        elif tr['kind']=='activated':
            c=candidate[tr['pivot_ts']];assert ts>=c['ts'];assert active is None or (tr['price']-active['price'])*sign>0
            if push:assert ts>c['ts'] and (raw['close']-tr['trigger'])*sign>=0
            else:assert ts==c['ts']
            active=tr;qa['activation_no_future_and_monotone']+=1
        elif tr['kind'] in ('failed','candidate_invalidated'):
            src=active if tr['kind']=='failed' else candidate[tr['pivot_ts']];assert src is not None and src['pivot_ts']==tr['pivot_ts'] and ts>src['ts'];observed=(raw['low'] if sign>0 else raw['high']) if wick else raw['close'];assert abs(observed-tr['observed'])<1e-8;assert abs(tr['trigger']-(tr['price']-sign*tick))<1e-8;assert (observed-tr['trigger'])*sign<=1e-8;qa['independent_failure_after_known_support']+=1
        elif tr['kind']=='stall':
            assert active is None and z['c_structure_mode']=='stall5';assert (raw['close']-e['fill'])*sign<0 and abs(raw['close']-tr['observed'])<1e-8 and abs(e['fill']-tr['trigger'])<1e-8
            i=bisect.bisect_left(times,e['entry_ts']);j=bisect.bisect_left(times,ts);labels=[]
            for mb in bs[i:j+1]:
                at=dt.datetime.fromisoformat(mb['datetime']);labels.append((at+dt.timedelta(minutes=(-at.minute)%15)).isoformat(sep=' '))
            assert len(set(labels))==5 and dt.datetime.fromisoformat(ts).minute%15==0 and e['mfe_r']>=.5-1e-8;qa['no_C_losing_on_exact_original_fifth_bucket']+=1
        else:raise AssertionError(tr['kind'])
    if e['status']=='closed' and e['exit_reason'] in ('c_structure_failed','c_not_formed'):
        failed=[t for t in traces if t['kind']==('stall' if e['exit_reason']=='c_not_formed' else 'failed')];assert failed;signal=failed[-1];i=bisect.bisect_left(times,e['exit_ts']);assert i>0 and times[i-1]==signal['ts'];assert abs(z['result']['fills'][-1]['price']-(bs[i]['open']-sign*z['slip']*tick))<1e-7;qa['independent_failure_next_observed_open_exit']+=1

def distribution(rows,events):
    m=enhance(rows,events);known=[z for z in rows if s.ev(z)['status']=='closed'];bins=collections.defaultdict(float)
    for z in known:bins[z['warning_ts'][:7]]+=s.ev(z)['net_amount']
    months={f'2026-{i:02}':bins[f'2026-{i:02}'] for i in range(1,11)};pos=[max(0,v) for v in m['bins']['product'].values()];den=sum(pos)
    m.update(warning_month_net=months,positive_warning_months=sum(v>0 for v in months.values()),negative_warning_months=sum(v<0 for v in months.values()),worst_warning_month=min(months.values()),negative_warning_month_burden=-sum(min(0,v) for v in months.values()),warning_month_std=statistics.pstdev(months.values()),positive_product_hhi=sum((v/den)**2 for v in pos) if den else None,positive_products=sum(v>0 for v in m['bins']['product'].values()))
    return m

def run():
    r.main.verify_snapshot();p=w.read(RUN/'protocol.json');fees=w.read(p['periods']['early']['fees']);ref=w.read(r.main.SNAP/'results.json');qa=collections.Counter();cases=collections.defaultdict(list);events={};ends={};funnel={};trace_counts={}
    for q in ('early','recent'):
        pool=w.read(RUN/(q+'-pool.json'));snaps={z['event_key']:z for z in pool};events.update({k:z['event'] for k,z in snaps.items()});cal=s.TradingDates(w.read(p['periods'][q]['input'])['symbols']);data=w.read(p['periods'][q]['contracts']);times={};base={};status=collections.defaultdict(collections.Counter);tc=collections.Counter()
        for code,end in w.read(RUN/q/'manifest.json')['contract_ends'].items():ends[code]=max(end,ends.get(code,''))
        for line in (RUN/q/'evaluations.jsonl').open(encoding='utf-8'):
            z=json.loads(line);profile,suffix=z['scenario'].removeprefix('P2_transition_').split('/',1);e=snaps[z['event_key']]['event'];assert z['c_structure_mode']==profile;assert not z['micro_exit_enabled'] and not z['micro_applied'];assert z['entry']==e['entry'] and z['stop']==e['stop'] and z['target']==z['baseline_target'];qa['fixed_entry_stop_absolute_target']+=1
            if profile.startswith('first') or profile=='stall5':
                prior=snaps[z['event_key']]['b_transition']['prior_b_extreme'];expected=min(prior,e['s2_price']) if e['direction']=='up' else max(prior,e['s2_price']);assert abs(expected-z['c_initial_origin'])<1e-8;qa['warning_time_whole_B_reference']+=1
            key=(z['event_key'],suffix)
            if profile=='base':base[key]=z['result'];assert not z['c_structure_trace']
            else:
                for field in ('fill','entry_ts','trigger_ts','risk','volume'):assert s.ev(z)[field]==base[key]['evaluation'][field];qa['same_individual_fill_and_risk']+=1
                if s.ev(z)['exit_reason'] not in ('c_structure_failed','c_not_formed'):assert z['result']==base[key];qa['unchanged_individual_full_result']+=1
                code=z['execution_contract'];bs=data[code]['minutes']
                if z['c_structure_trace']:
                    if code not in times:times[code]=[b['datetime'] for b in bs]
                    audit_trace(z,bs,times[code],qa)
                tc.update(t['kind'] for t in z['c_structure_trace'])
            for fill in z['result']['fills']:
                assert fill['ts']>z['warning_ts'] and abs(fill['price']/z['tick_size']-round(fill['price']/z['tick_size']))<1e-6;qa['fill_time_tick']+=1
            status[profile+'/'+suffix][s.ev(z)['status']]+=1;z=s.reprice(z,fees,cal);z['period']=q
            if s.ev(z)['status']=='closed':z['exit_trading_day']=cal.day(s.ev(z)['exit_ts']);assert abs(s.ev(z)['net_r']-s.ev(z)['net_amount']/(s.ev(z)['risk']*z['multiplier']))<1e-9
            cases[profile+'/'+suffix].append(z)
        del data;funnel[q]={k:dict(v) for k,v in status.items()};trace_counts[q]=dict(tc)
    results={pol:{q:{} for q in ('early','recent','recent_aug_oct','combined')} for pol in ('original','two_paths_flat')};accepted={};ledgers={};leave={};controls=[];unknown={}
    for case,rows in cases.items():
        for pol in results:
            select=(lambda zz,reverse=False:s.portfolio(zz,events,ends,reverse=reverse)) if pol=='original' else (lambda zz,reverse=False:s.flat.exposure_portfolio(zz,ends,reverse))
            aa,ll=select(rows);rev,_=select(rows,True);accepted[(pol,case)]=aa;ledgers[(pol,case)]=ll
            for q in results[pol]:
                keep=lambda z:True if q=='combined' else z['period']=='recent' and z['warning_ts']>='2026-08-01 00:00:00' if q=='recent_aug_oct' else z['period']==q
                zz=[z for z in aa if keep(z)];m=distribution(zz,events);m['reverse_tie_net']=s.metrics([z for z in rev if keep(z)])['net'];results[pol][q][case]=m;unknown[(pol,q,case)]=[z['event_key'] for z in zz if s.ev(z)['status'] in s.flat.s.UNKNOWN]
                if case.startswith('base/') and q!='combined':
                    b=ref[pol][q][case.split('/',1)[1]];bad={k:[m[k],b[k]] for k in b if k in m and not equal(m[k],b[k])};assert not bad,(pol,q,case,bad);controls.append(dict(policy=pol,period=q,scenario=case,exact=True))
            assert abs(results[pol]['combined'][case]['net']-results[pol]['early'][case]['net']-results[pol]['recent'][case]['net'])<1e-6
            leave[(pol,case)]={sym:s.metrics([z for z in aa if z['symbol']!=sym]) for sym in sorted({z['symbol'] for z in rows})}
    evaluation={};attribution={};scorecard=[]
    for profile in r.PROFILES:
        evaluation[profile]={};attribution[profile]={}
        for pol in results:
            evaluation[profile][pol]={};attribution[profile][pol]={}
            for suffix in SUFFIXES:
                case=profile+'/'+suffix;base='base/'+suffix;m=results[pol]['combined'][case];b=results[pol]['combined'][base]
                flags=dict(total_profit_higher=m['net']>b['net']+1e-8,unknown_not_more=m['unknown']<=b['unknown'],drawdown_better=m['realized_cash_drawdown']<b['realized_cash_drawdown']-1e-8,negative_month_burden_better=m['negative_warning_month_burden']<b['negative_warning_month_burden']-1e-8,worst_month_better=m['worst_warning_month']>b['worst_warning_month']+1e-8,product_hhi_better=m['positive_product_hhi'] is not None and b['positive_product_hhi'] is not None and m['positive_product_hhi']<b['positive_product_hhi']-1e-12,top3_day_better=m['top3_day']<b['top3_day']-1e-12)
                evaluation[profile][pol][suffix]=dict(flags=flags,total_delta=m['net']-b['net'],recent_retention=results[pol]['recent'][case]['net']/results[pol]['recent'][base]['net'],early_net=results[pol]['early'][case]['net'],recent_net=results[pol]['recent'][case]['net'])
                old=accepted[(pol,base)];new=accepted[(pol,case)];a={z['event_key']:z for z in old if s.ev(z)['status']=='closed'};bb={z['event_key']:z for z in new if s.ev(z)['status']=='closed'};common=a.keys()&bb.keys();lost=a.keys()-bb.keys();added=bb.keys()-a.keys();dc=sum(s.ev(bb[k])['net_amount']-s.ev(a[k])['net_amount'] for k in common);dn=sum(s.ev(bb[k])['net_amount'] for k in added);do=sum(s.ev(a[k])['net_amount'] for k in lost);assert abs(m['net']-b['net']-(dc+dn-do))<1e-6
                attribution[profile][pol][suffix]=dict(common_closed=len(common),common_cash_delta=dc,new_only_n=len(added),new_only_net=dn,old_only_n=len(lost),old_only_net=do,warning_week_delta_interval=weekly_delta(old,new),periods={})
                for q in ('early','recent'):
                    ks=[k for k in common if a[k]['period']==q];changed=[k for k in ks if s.ev(bb[k])['exit_reason'] in ('c_structure_failed','c_not_formed')];attribution[profile][pol][suffix]['periods'][q]=dict(common_closed=len(ks),common_cash_delta=sum(s.ev(bb[k])['net_amount']-s.ev(a[k])['net_amount'] for k in ks),new_C_exit_common=len(changed),original_exit_reasons=dict(collections.Counter(s.ev(a[k])['exit_reason'] for k in changed)),original_net=sum(s.ev(a[k])['net_amount'] for k in changed),new_net=sum(s.ev(bb[k])['net_amount'] for k in changed))
        m=results['original']['combined'][profile+'/'+SUFFIXES[0]];scorecard.append(dict(profile=profile,total=m['net'],delta_vs_best_a=evaluation[profile]['original'][SUFFIXES[0]]['total_delta'],early=results['original']['early'][profile+'/'+SUFFIXES[0]]['net'],recent=results['original']['recent'][profile+'/'+SUFFIXES[0]]['net'],negative_month_burden=m['negative_warning_month_burden'],worst_month=m['worst_warning_month'],drawdown=m['realized_cash_drawdown'],product_hhi=m['positive_product_hhi'],top3_product=m['top3_product'],positive_months=m['positive_warning_months'],unknown=m['unknown'],improved_stresses=sum(evaluation[profile]['original'][sf]['flags']['total_profit_higher'] for sf in SUFFIXES),primary_gain_both_policies=all(evaluation[profile][pol][SUFFIXES[0]]['flags']['total_profit_higher'] for pol in results),all_stress_gain_both_policies=all(evaluation[profile][pol][sf]['flags']['total_profit_higher'] for pol in results for sf in SUFFIXES)))
    scorecard.sort(key=lambda z:z['total'],reverse=True);candidates=[z['profile'] for z in scorecard if z['profile']!='base' and z['delta_vs_best_a']>1e-8]
    r.main.verify_snapshot();assert all(w.sha(k)==v for k,v in p['production_before'].items());assert all(w.sha(RUN/'fork'/k)==v for k,v in w.read(RUN/'implementation-manifest.json').items())
    for name,obj in [('results.json',results),('evaluation.json',evaluation),('attribution.json',attribution),('scorecard.json',scorecard),('source-funnel.json',funnel),('trace-counts.json',trace_counts),('unknown-identities.json',{str(k):v for k,v in unknown.items()}),('leave-product-out.json',{str(k):v for k,v in leave.items()}),('opportunity-ledgers.json',{str(k):v for k,v in ledgers.items()})]:w.write(RUN/name,obj)
    with (RUN/'accepted.jsonl').open('x',encoding='utf-8') as out:
        for (pol,case),aa in accepted.items():
            for z in aa:out.write(json.dumps(dict(policy=pol,case=case,period=z['period'],row=z),ensure_ascii=False)+'\n')
    w.write(RUN/'verification.json',dict(qa=dict(qa),controls=controls,primary_total_improved=candidates,recent80_gate_used=False,best_a_snapshot_unchanged=True,production_unchanged=169,production_changed=False,qualified_for_trading=False))
    print(json.dumps(scorecard,ensure_ascii=False,indent=2),flush=True)

if __name__=='__main__':run()
