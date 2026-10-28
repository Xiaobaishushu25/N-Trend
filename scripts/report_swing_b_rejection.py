"""Two post-entry evidence experiments; do not overwrite named best-a baseline."""
import bisect,collections,json,statistics
import research_swing_b_rejection as r
import research_swing_abstop as s
from report_swing_b_transition import enhance
from research_swing_combination import weekly_delta
w=r.w;RUN=r.RUN

def run():
    p=w.read(RUN/'protocol.json');ref=w.read(r.main.SNAP/'results.json');fees=w.read(p['periods']['early']['fees']);events={};cases=collections.defaultdict(list);ends={};qa=collections.Counter();funnel={}
    for q in ('early','recent'):
        pool=w.read(RUN/(q+'-pool.json'));snaps={z['event_key']:z for z in pool};events.update({k:z['event'] for k,z in snaps.items()});cal=s.TradingDates(w.read(p['periods'][q]['input'])['symbols']);base={};micro_closed=[];status=collections.defaultdict(collections.Counter)
        for code,ts in w.read(RUN/q/'manifest.json')['contract_ends'].items():ends[code]=max(ts,ends.get(code,''))
        for line in (RUN/q/'evaluations.jsonl').open(encoding='utf-8'):
            z=json.loads(line);profile,suffix=z['scenario'].removeprefix('P2_transition_').split('/',1);e=snaps[z['event_key']]['event'];f=snaps[z['event_key']]['b_transition'];enabled=profile!='base' and f['required'];assert z['micro_exit_enabled']==enabled;assert not z['micro_applied'];assert z['entry']==e['entry'] and z['stop']==e['stop'] and z['target']==z['baseline_target'];qa['original_entry_stop_target']+=1
            key=(z['event_key'],suffix)
            if profile=='base':base[key]=z['result']
            elif not enabled or s.ev(z)['exit_reason']!='micro_progress_rejected':assert z['result']==base[key];qa['unchanged_individual_results']+=1
            if s.ev(z)['status']=='closed' and s.ev(z)['exit_reason']=='micro_progress_rejected':micro_closed.append((z,f))
            status[profile+'/'+suffix][s.ev(z)['status']]+=1
            z=s.reprice(z,fees,cal);z['period']=q
            if s.ev(z)['status']=='closed':z['exit_trading_day']=cal.day(s.ev(z)['exit_ts']);assert abs(s.ev(z)['net_r']-s.ev(z)['net_amount']/(s.ev(z)['risk']*z['multiplier']))<1e-9
            cases[profile+'/'+suffix].append(z)
        # Independently verify the exit signal existed before the recorded fill,
        # only after actual entry; never reconstruct arm from pre-entry closes.
        data=w.read(p['periods'][q]['contracts']);minute_times={}
        for z,f in micro_closed:
            e=s.ev(z);code=z['execution_contract'];bs=data[code]['minutes'];ts=minute_times.setdefault(code,[b['datetime'] for b in bs]);i=bisect.bisect_left(ts,e['entry_ts']);j=bisect.bisect_left(ts,e['exit_ts']);assert i<j and ts[j]==e['exit_ts'];sign=1 if z['direction']=='up' else -1;prior=bs[j-1]
            assert (prior['close']-f['pivot_price'])*sign<0;assert any((b['close']-f['level'])*sign>=0 for b in bs[i:j-1]);assert e['exit_ts']>e['entry_ts']
            if z['micro_exit_profit_only']:assert (prior['close']-e['fill'])*sign>0
            assert abs(z['result']['fills'][-1]['price']-(bs[j]['open']-sign*z['slip']*z['tick_size']))<1e-7;qa['independent_post_fill_clear_reject_next_open']+=1
        del data;funnel[q]={k:dict(v) for k,v in status.items()}
    results={pol:{q:{} for q in ('early','recent','recent_aug_oct')} for pol in ('original','two_paths_flat')};accepted={};ledger={};controls=[];leave={}
    for case,rows in cases.items():
        for pol in results:
            select=(lambda zz,reverse=False:s.portfolio(zz,events,ends,reverse=reverse)) if pol=='original' else (lambda zz,reverse=False:s.flat.exposure_portfolio(zz,ends,reverse))
            aa,ll=select(rows);rev,_=select(rows,True);accepted[(pol,case)]=aa;ledger[(pol,case)]=ll
            for q in results[pol]:
                keep=lambda z:z['period']==q if q!='recent_aug_oct' else z['period']=='recent' and z['warning_ts']>='2026-08-01 00:00:00'
                m=enhance([z for z in aa if keep(z)],events);m['reverse_tie_net']=s.metrics([z for z in rev if keep(z)])['net'];results[pol][q][case]=m
                if case.startswith('base/'):
                    b=ref[pol][q][case.split('/',1)[1]];bad={k:(m[k],b[k]) for k in b if k in m and m[k]!=b[k]};assert not bad,(pol,q,case,bad);controls.append(dict(policy=pol,period=q,case=case,exact=True))
            leave[(pol,case)]={sym:{q:s.metrics([z for z in aa if z['period']==q and z['symbol']!=sym]) for q in ('early','recent')} for sym in sorted({z['symbol'] for z in rows})}
    gates={};attribution={};defensive=[];strict=[]
    for profile in r.PROFILES:
        gates[profile]={};attribution[profile]={};good=full=True
        for pol in results:
            gates[profile][pol]={};attribution[profile][pol]={}
            for q in ('early','recent'):
                gates[profile][pol][q]={};attribution[profile][pol][q]={}
                for suffix in ('corrected_touch/slip1','corrected_touch/slip2','next_minute_open/slip1','next_minute_open/slip2'):
                    case=profile+'/'+suffix;base='base/'+suffix;m=results[pol][q][case];b=results[pol][q][base];flags=dict(positive_cash=m['net']>0,positive_R=m['mean_r']>0,recent_retention=q!='recent' or m['net']>=.8*b['net']-1e-8,early_cash=q!='early' or m['net']>=b['net']-1e-8,early_R=q!='early' or m['mean_r']>=b['mean_r']-1e-12,drawdown=m['realized_cash_drawdown']<=b['realized_cash_drawdown']+1e-8,unknown=m['unknown']<=b['unknown'],minimum_sample=m['n']>=max(35,.3*b['n']),win_rate=q!='recent' or m['win_rate']>=b['win_rate']-1e-12,tail10=m['tail10_cash']>=b['tail10_cash']-1e-8)
                    gates[profile][pol][q][suffix]=flags;full &= all(flags.values());good &= flags['unknown'] and flags['minimum_sample'] and (flags['recent_retention'] if q=='recent' else flags['early_cash'] and flags['early_R'] and flags['drawdown'])
                    old=[z for z in accepted[(pol,base)] if z['period']==q];new=[z for z in accepted[(pol,case)] if z['period']==q];a={z['event_key']:z for z in old if s.ev(z)['status']=='closed'};bb={z['event_key']:z for z in new if s.ev(z)['status']=='closed'};common=a.keys()&bb.keys();added=bb.keys()-a.keys();lost=a.keys()-bb.keys();dc=sum(s.ev(bb[k])['net_amount']-s.ev(a[k])['net_amount'] for k in common);dn=sum(s.ev(bb[k])['net_amount'] for k in added);do=sum(s.ev(a[k])['net_amount'] for k in lost);assert abs(m['net']-b['net']-(dc+dn-do))<1e-6
                    prod=collections.defaultdict(float)
                    for sign,zz in ((1,new),(-1,old)):
                        for z in zz:
                            if s.ev(z)['status']=='closed':prod[z['symbol']]+=sign*s.ev(z)['net_amount']
                    attribution[profile][pol][q][suffix]=dict(common_closed=len(common),common_cash_delta=dc,new_only_n=len(added),new_only_net=dn,old_only_n=len(lost),old_only_net=do,by_product_delta=dict(sorted(prod.items(),key=lambda z:z[1],reverse=True)),warning_week_delta_interval=weekly_delta(old,new))
        gates[profile]['defensive_noninferior']=bool(good);gates[profile]['strict_stable']=bool(full)
        if profile!='base' and good:defensive.append(profile)
        if full:strict.append(profile)
    r.main.verify_snapshot();assert all(w.sha(k)==v for k,v in p['production_before'].items());assert all(w.sha(RUN/'fork'/k)==v for k,v in w.read(RUN/'implementation-manifest.json').items())
    for name,obj in [('results.json',results),('gates.json',gates),('attribution.json',attribution),('source-funnel.json',funnel),('leave-product-out.json',{str(k):v for k,v in leave.items()}),('opportunity-ledgers.json',{str(k):v for k,v in ledger.items()})]:w.write(RUN/name,obj)
    with (RUN/'accepted.jsonl').open('x',encoding='utf-8') as out:
        for (pol,case),aa in accepted.items():
            for z in aa:out.write(json.dumps(dict(policy=pol,case=case,period=z['period'],row=z),ensure_ascii=False)+'\n')
    w.write(RUN/'verification.json',dict(qa=dict(qa),controls=controls,defensive_candidates=defensive,strict_stable=strict,production_unchanged=169,best_a_unchanged=True,production_changed=False,qualified_for_trading=False))
    print([(q,k,round(m['net'],2),round(m['mean_r'],4),m['n']) for q in ('early','recent') for k,m in results['original'][q].items() if k.endswith('corrected_touch/slip1')],flush=True);print('Defensive',defensive,'Strict',strict,flush=True)

if __name__=='__main__':run()
