"""Conditional path null for ALL reclaim decisions, not accepted/winner-only."""
import bisect
import collections
import datetime as dt
import json
import random
import statistics as st
import research_swing_jd_reclaim_robustness as r

RUN=r.RUN/'matched-null'
R=r.R
N=199

def sign(v):return 1 if v>0 else -1 if v<0 else 0

def run():
    RUN.mkdir(exist_ok=False);write=lambda n,x:r.b.z.s.m.old.write(RUN/n,x)
    write('protocol.json',dict(id=RUN.name,created_at=dt.datetime.now().isoformat(),script_sha256=r.sha(__file__),seed=20261026,replications=N,
        purpose='Conditionaldirectionpathdiagnostic onALL32mainS0reclaimdecisions, notacceptedtrade/winnerselection. Sequentialexploration, notfreshOOS orstrategyoptimization.',
        match='Sameperiod,actualcontract,calendarmonth,15mslot,signofpast8barclosemove; hostATR14/signalATR14 in[.5,2]. Bothprefixpast20bars sameactualmember, completepositivevolume15m actualOHLC. Exclude within16barindices ofanytruereclaim decision inperiod. Need>=3hosts, no fallbackrelaxation.',
        outcome='Forward16complete15m actualmemberbars; directioncopiedfromsignal, anchoredatownclockCLOSE andscaledbyownprefixATR14. MeancloseATR,MFE/MAEATR,first+1ATR vs-1ATR; missingfuturepath is censored, never0.',
        sampling='199seededreplications; onehostpereachmatchedsignal, uniquehostclockwithinreplication. Overlappingforwardwindows/reusedmonthsremain, empiricalpercentile isnotcalibratedpvalue orfutureprofitprobability.',
        limits='Pathcheckignoresstop,target,feesandportfoliooccupancy; cannotbeinterpretedasnativePnLedge. Allgenerationrules/stressresultsunchanged. Unmatchedsignalsdisclosed.'))
    datasets={};signals=[];hostcache={};sources={}
    for per in ['early','recent']:
        paths,recognition,parents,dates=r.f.t.period_paths(per);inp=R(recognition);bars=r.b.z.x.identification_bars(inp['symbols']['JD0']['bars']);segments=R(paths['segments'])['JD0'];codes=[next((s['underlying'] for s in segments if s['first']<=q['datetime']<=s['last']),None) for q in bars];index={q['datetime']:i for i,q in enumerate(bars)}
        prices=r.b.a.Prices(R(r.b.ROOT/'research-strategies/best-a-portfolio-observation-v1/protocol.json')['periods'][per]);count=collections.Counter(q['datetime'] for q in bars);valid=[]
        for q,code in zip(bars,codes):
            stamp=dt.datetime.fromisoformat(q['datetime']);mins=[prices.maps.get(code,{}).get((stamp-dt.timedelta(minutes=k)).isoformat(sep=' ')) for k in range(14,-1,-1)]
            ok=count[q['datetime']]==1 and all(v is not None and v['volume']>0 for v in mins)
            if ok:ok=all(abs(q[k]-v)<1e-7 for k,v in dict(open=mins[0]['open'],close=mins[-1]['close'],high=max(v['high'] for v in mins),low=min(v['low'] for v in mins)).items())
            valid.append(ok)
        lookup={}
        for i in range(20,len(bars)):
            if not all(valid[i-20:i+1]) or codes[i] is None or any(c!=codes[i] for c in codes[i-20:i+1]):continue
            q=bars[i];atr=st.mean(max(bars[j]['high']-bars[j]['low'],abs(bars[j]['high']-bars[j-1]['close']),abs(bars[j]['low']-bars[j-1]['close'])) for j in range(i-13,i+1))
            if atr<=0:continue
            lookup[i]=dict(index=i,ts=q['datetime'],code=codes[i],atr=atr,momentum_sign=sign(q['close']-bars[i-8]['close']),close=q['close'])
        items=R(r.c.RUN/per/'S0_RECLAIM_R2'/(per+'-pool.json'));datasets[per]=dict(prices=prices,bars=bars,lookup=lookup)
        signal_indices=[index[s['event']['warning_ts']] for s in items]
        for s in items:
            e=s['event'];at=index[e['warning_ts']];q=lookup.get(at)
            if q is None:signals.append(dict(period=per,key=s['event_key'],ts=e['warning_ts'],status='signal_prefix_unverified'));continue
            real=outcome(datasets[per],q,r.f.direction(e))
            if real is None:signals.append(dict(period=per,key=s['event_key'],ts=q['ts'],status='signal_forward_censored'));continue
            candidates=[]
            for j,h in lookup.items():
                ts=h['ts'];in_window=('2026-01-01'<=ts<='2026-07-10 15:00:00') if per=='early' else ('2026-08-10'<=ts<='2026-10-23 11:05:00')
                if not in_window or h['code']!=q['code'] or ts[:7]!=q['ts'][:7] or ts[11:16]!=q['ts'][11:16] or h['momentum_sign']!=q['momentum_sign']:continue
                if not(.5<=h['atr']/q['atr']<=2) or any(abs(j-k)<=16 for k in signal_indices):continue
                key=per,j,r.f.direction(e)
                if key not in hostcache:hostcache[key]=outcome(datasets[per],h,r.f.direction(e))
                if hostcache[key] is not None:candidates.append(dict(host_id=per+'|'+ts,**hostcache[key]))
            signals.append(dict(period=per,key=s['event_key'],ts=q['ts'],direction=e['direction'],status='matched' if len(candidates)>=3 else 'insufficient_hosts',candidate_count=len(candidates),actual=real,candidates=candidates))
        sources[per]={str(q):r.sha(q) for q in [recognition,paths['contracts'],paths['segments']]}
    matched=[s for s in signals if s['status']=='matched'];rng=random.Random(20261026);rep=[];sampling_failures=0
    for k in range(N):
        picked=[];used=set()
        for s in sorted(matched,key=lambda q:(q['candidate_count'],q['ts'],q['key'])):
            choices=[q for q in s['candidates'] if q['host_id'] not in used]
            if not choices:sampling_failures+=1;picked=[];break
            q=rng.choice(choices);picked.append(q);used.add(q['host_id'])
        if picked:rep.append(summarize(picked))
    actual=summarize([s['actual'] for s in matched]);summary=dict(all_decisions=len(signals),matched=len(matched),statuses=dict(collections.Counter(s['status'] for s in signals)),actual=actual,replications=len(rep),sampling_failures=sampling_failures,null={})
    for name in ['mean_close_ATR','mean_MFE_ATR','mean_MAE_ATR','upper_first_rate']:
        xs=sorted(q[name] for q in rep);summary['null'][name]=dict(median=st.median(xs),p10=xs[int(.1*(len(xs)-1))],p90=xs[int(.9*(len(xs)-1))],actual_percentile=100*sum(v<=actual[name] for v in xs)/len(xs))
    write('results.json',summary);write('signals-and-matches.json',signals);write('null-replications.json',rep);write('sources.json',sources)
    print(json.dumps(summary,ensure_ascii=False),flush=True)

def outcome(dataset,q,direction):
    path=dataset['prices'].path(q['code'],q['ts'],16);raw,complete=r.b.a.window(path,16)
    if not complete:return None
    return dict(r.b.a.excursion(raw,q['close'],q['atr'],direction),race=r.b.a.barrier(raw,q['close'],q['atr'],direction)[0])

def summarize(xs):
    return dict(n=len(xs),mean_close_ATR=st.mean(q['end_r'] for q in xs),mean_MFE_ATR=st.mean(q['mfe'] for q in xs),mean_MAE_ATR=st.mean(q['mae'] for q in xs),upper_first_rate=sum(q['race']=='upper_first' for q in xs)/len(xs),race=dict(collections.Counter(q['race'] for q in xs)))

if __name__=='__main__':run()
