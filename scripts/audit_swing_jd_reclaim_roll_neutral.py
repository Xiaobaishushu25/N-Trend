"""Supplementary past-only daily index; explicitly remove unmeasurable rollgap."""
import bisect
import collections
import datetime as dt
import json
import statistics as st
import audit_swing_jd_reclaim_regimes as a

r=a.r
R=a.R
RUN=a.RUN/'roll-neutral-supplement'
write=lambda n,x:r.b.z.s.m.old.write(RUN/n,x)

def build(days):
    """days include missing placeholders, so unverified tradingdays reset chain."""
    out=[];last=None;segment=0
    for item in days:
        if item['bar'] is None:segment+=1;last=None;continue
        q=item['bar'];code=item['contract'];changed=last is not None and last['contract']!=code
        if last is None:factor=100/q['open']
        elif changed:factor=last['close']/q['open']
        else:factor=last['close']/last['raw_close']
        rec=dict(ts=q['ts'],contract=code,segment=segment,raw_close=q['close'],roll_gap_excluded=changed,
            open=q['open']*factor,high=q['high']*factor,low=q['low']*factor,close=q['close']*factor)
        out.append(rec);last=rec
    return out

def features(rows,ts):
    end=bisect.bisect_left([q['ts'] for q in rows],ts[:10]);ds=rows[:end];out=dict(state='UNKNOWN',ratio=None,as_of=ds[-1]['ts']+' 15:00:00' if ds else None)
    if len(ds)>=11 and ds[-1]['segment']==ds[-11]['segment']:
        tr=st.mean(max(ds[i]['high']-ds[i]['low'],abs(ds[i]['high']-ds[i-1]['close']),abs(ds[i]['low']-ds[i-1]['close'])) for i in range(len(ds)-10,len(ds)))
        if tr>0:
            v=(ds[-1]['close']-ds[-11]['close'])/tr;out.update(state=a.classify(v),ratio=v,first_date=ds[-11]['ts'],last_date=ds[-1]['ts'],roll_days=sum(q['roll_gap_excluded'] for q in ds[-10:]))
    if out['as_of']:assert out['as_of']<ts
    return out

def run():
    RUN.mkdir(exist_ok=False)
    write('protocol.json',dict(id=RUN.name,script_sha256=r.sha(__file__),created_at=dt.datetime.now().isoformat(),
        declaration='Supplementary descriptive definition AFTER seeingmanysamecontractwarmupUNKNOWNcells. Notanewstrategyortrendfilter; fixed10daylookback and+-1ATRthresholdunchanged.',
        index='Atcompletedmainactiveday, scaleactualmemberOHLC. Ifsamememberasprecedingverifiedday, retainactualclose-to-closemove. Atmemberchange, carrypriorindexclosetonewmemberDAYOPEN thenkeeprealintradayOHLCreturns; excludeentireoldclose/newopenjump, includingpossiblyrealovernightmove. Do notclaimthatunobservedreturnwas0. No futurebackadjustment.',
        missing='Any source tradingdaywithoutoneverifiedfull225minuteactiveday resetsindexsegment; no bridgingarchivegap. Need11completedindexdays inthesamesegment STRICTLYBEFORE decisiondate. Rawprovenance androllflagsreported.',
        limitation='Rollneutralindexisresearchconvention, notatradedreturnoruniquegroundtruthtrend. Comparewithsamecontractdaily10+short30, sparsecells andknownhistoryremain; noadoptionorparameterselection.'))
    timelines={};sources={}
    for per in ['early','recent']:
        paths,recognition,_,_=r.f.t.period_paths(per);inp=R(recognition);segments=R(paths['segments'])['JD0'];raw=R(paths['contracts']);daily={code:{q['ts']:q for q in a.history(v['minutes'])['daily']} for code,v in raw.items() if code.startswith('DCE.jd')}
        days=sorted({q['datetime'][:10] for q in inp['symbols']['JD0']['bars']});items=[]
        for d in days:
            first=next((s['underlying'] for s in segments if s['first']<=d+' 09:15:00'<=s['last']),None);last=next((s['underlying'] for s in segments if s['first']<=d+' 15:00:00'<=s['last']),None)
            q=daily.get(first,{}).get(d) if first is not None and first==last else None
            items.append(dict(day=d,contract=first,bar=q))
        timeline=build(items);timelines[per]=timeline;write(per+'-index.json',timeline)
        # Prefixconstructionproof for EVERYdecision day, nofuture scaling.
        for d in sorted({q['warning_ts'][:10] for q in R(a.RUN/'opportunities.json') if q['key'] in R(r.c.RUN/per/'metadata.json')}):
            assert features(build([v for v in items if v['day']<d]),d+' 09:15:00')==features(timeline,d+' 09:15:00')
        sources[per]={str(q):r.sha(q) for q in [recognition,paths['contracts'],paths['segments']]}
    grouped=collections.defaultdict(list);featuresout={};rows=[]
    for line in (a.RUN/'accepted-with-regimes.jsonl').open(encoding='utf-8'):
        x=json.loads(line);q=x['row'];f=features(timelines[q['period']],q['warning_ts']);featuresout[q['original_event_key']]=f;rows.append(dict(variant=x['variant'],row=q,roll_neutral_daily=f))
        grouped[x['variant'],f['state'],'all'].append(q)
        if q['module']=='S0_RECLAIM_R2':grouped[x['variant'],f['state'],'S0_RECLAIM_R2'].append(q)
    result={}
    for v in ['E20_ONLY','FULL','LONG_ONLY','SHORT_ONLY']:
        for state in ['UP','DOWN','RANGE','UNKNOWN']:
            for mod in ['all','S0_RECLAIM_R2']:result[v+'/'+state+'/'+mod]=r.b.z.metric(grouped[v,state,mod])
    opportunities=[]
    for per in ['early','recent']:
        for s in R(r.c.RUN/per/'S0_RECLAIM_R2'/(per+'-pool.json')):
            e=s['event'];opportunities.append(dict(key=s['event_key'],direction=e['direction'],warning_ts=e['warning_ts'],**features(timelines[per],e['warning_ts'])))
    coverage=[dict(q,roll_neutral_daily=features(timelines[q['period']],q['first_quote'])) for q in R(a.RUN/'market-state-coverage.json')]
    write('regime-results.json',result);write('opportunities.json',opportunities);write('state-features.json',featuresout);write('market-coverage.json',coverage);write('sources.json',sources)
    print(json.dumps(dict(long_new={state:{k:result['LONG_ONLY/'+state+'/S0_RECLAIM_R2'][k] for k in ['n','net','normalized_net','unknown']} for state in ['UP','DOWN','RANGE','UNKNOWN']},market_days=dict(collections.Counter(q['roll_neutral_daily']['state'] for q in coverage))),ensure_ascii=False))

if __name__=='__main__':run()
