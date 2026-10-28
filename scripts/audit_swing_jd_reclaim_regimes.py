"""Past-only same-contract regime audit; no new filters or optimized strategies."""
import bisect
import collections
import datetime as dt
import json
import statistics as st
import research_swing_jd_reclaim_robustness as r

RUN=r.b.ROOT/'research-runs/swing-jd-reclaim-regime-audit-20261026-v1'
R=r.R
write=lambda n,x:r.b.z.s.m.old.write(RUN/n,x)
SLOTS=r.b.a.SLOTS
MINUTES=r.b.a.MINUTES
NAMES={'UP':'上涨推进','DOWN':'下跌推进','RANGE':'区间／推进弱','UNKNOWN':'历史不足'}

def history(raw):
    lookup={q['datetime']:q for q in raw};byday=collections.defaultdict(list)
    for q in raw:byday[q['datetime'][:10]].append(q)
    daily=[];quarters=[]
    for day,qs in sorted(byday.items()):
        wanted=[f'{day} {m//60:02}:{m%60:02}:00' for m in MINUTES]
        vals=[lookup.get(t) for t in wanted]
        if all(v is not None and v['volume']>0 for v in vals):daily.append(dict(ts=day,open=vals[0]['open'],high=max(q['high'] for q in vals),low=min(q['low'] for q in vals),close=vals[-1]['close']))
        for slot in SLOTS:
            end=dt.datetime.fromisoformat(f'{day} {slot//60:02}:{slot%60:02}:00');vals=[lookup.get((end-dt.timedelta(minutes=k)).isoformat(sep=' ')) for k in range(14,-1,-1)]
            if all(v is not None and v['volume']>0 for v in vals):quarters.append(dict(ts=end.isoformat(sep=' '),open=vals[0]['open'],high=max(q['high'] for q in vals),low=min(q['low'] for q in vals),close=vals[-1]['close']))
    return dict(daily=daily,quarters=quarters,daily_labels=[q['ts'] for q in daily],quarter_labels=[q['ts'] for q in quarters])

def classify(value):return 'UP' if value>=1 else 'DOWN' if value<=-1 else 'RANGE'

def features(h,ts):
    # Daily ONLYstrictlyearlierdates; current-daylaterclose never visible.
    end=bisect.bisect_left(h['daily_labels'],ts[:10]);d=h['daily'][:end];daily=dict(state='UNKNOWN',ratio=None,as_of=d[-1]['ts']+' 15:00:00' if d else None)
    if len(d)>=11:
        tr=st.mean(max(d[j]['high']-d[j]['low'],abs(d[j]['high']-d[j-1]['close']),abs(d[j]['low']-d[j-1]['close'])) for j in range(len(d)-10,len(d)))
        if tr>0:
            ratio=(d[-1]['close']-d[-11]['close'])/tr;daily.update(state=classify(ratio),ratio=ratio,first_date=d[-11]['ts'],last_date=d[-1]['ts'])
    end=bisect.bisect_right(h['quarter_labels'],ts);q=h['quarters'][:end];short=dict(state='UNKNOWN',ratio=None,as_of=q[-1]['ts'] if q else None)
    if len(q)>=31:
        atr=st.mean(max(q[j]['high']-q[j]['low'],abs(q[j]['high']-q[j-1]['close']),abs(q[j]['low']-q[j-1]['close'])) for j in range(len(q)-14,len(q)))
        if atr>0:
            ratio=(q[-1]['close']-q[-31]['close'])/atr;short.update(state=classify(ratio),ratio=ratio,first_ts=q[-31]['ts'])
    for v in [daily,short]:
        if v['as_of']:assert v['as_of']<=ts
    return dict(daily10=daily,quarter30=short)

def run():
    RUN.mkdir(exist_ok=False)
    write('protocol.json',dict(id=RUN.name,created_at=dt.datetime.now().isoformat(),script_sha256=r.sha(__file__),
        purpose='Auditlong-only regimefitrisk, notselectdirectionornewtrendgate. Fullsavedcandidateunchanged. No newparameteroptimization.',
        daily10='ActualSAMEcontract, previous11FULLpositivevolume225minuteJDdays ONLY strictlybeforedcisiondate. (lastclose-close10daysago)/meanTRlast10days. UP>=1,DOWN<=-1,elseRANGE; insufficientdataUNKNOWN. Currentdaydailycloseforbidden.',
        quarter30='Actualsamecontract last31FULLpositivevolume15m observations withclose<=decisiontime; (lastclose-close30barsago)/ATR14. Samefixed+-1states. IntradaybackgrounddistinctfromdailybackgroundandparentA direction.',
        coverage='Same2026mainwarningwindows, noJulyhistoricalfixedcontractaddition. No continuousmainrollgapsusedaspricechanges; insufficientsamecontractwarmupkeptUNKNOWN. All32reclaimproposals shown, notonlyacceptedwinners.',
        accounting='Classifyatactualorderdecisionwarningtime. ExistingE20/FULL/LONG_ONLY/SHORT_ONLY acceptedledgers groupedwithoutrecompetition. Descriptiveconditionalattribution, notnewregimefilteredstrategy. One-lotnet and300plannedrisk normalization, unknownnot0.',
        validation='Causalprefixequality checked forALL32newmoduledecisions, usingactualminutehistoriestruncatedatdecision; marketcoverage andallopportunities reported; noadoptionbasedonthissamehistory.',
        limitation='Twofixedlookbackscales aredescriptiveconventions, nottruefuturetrendlabels orfittedoptimaltrenddefinition. Sparsecells,overlappingA andtimeblocks,retrospectiveselection preventclaims ofgeneralization.',snapshot_manifest_sha256=r.sha(r.SNAP/'manifest.json')))
    manifest=R(r.SNAP/'manifest.json');assert all(r.sha(r.SNAP/q)==h for q,h in manifest['files'].items())
    histories={};rawdata={};sources={};coverage=[]
    allowed=set(R(r.b.RUN/'generation-checks.json')['covered_dates'])
    for per in ['early','recent']:
        paths,recognition,_,_=r.f.t.period_paths(per);raw=R(paths['contracts']);rawdata[per]={code:v['minutes'] for code,v in raw.items() if code.startswith('DCE.jd')}
        histories[per]={code:history(qs) for code,qs in rawdata[per].items()};sources[per]={str(q):r.sha(q) for q in [paths['contracts'],paths['segments'],recognition]}
        inp=R(recognition);bars=r.b.z.x.identification_bars(inp['symbols']['JD0']['bars']);segments=R(paths['segments'])['JD0'];seen=set()
        for q in bars:
            day=q['datetime'][:10]
            if day not in allowed or day in seen:continue
            code=next((s['underlying'] for s in segments if s['first']<=q['datetime']<=s['last']),None)
            if code is None:continue
            h=histories[per][code]
            if q['datetime'] not in h['quarter_labels']:continue
            seen.add(day);coverage.append(dict(period=per,day=day,first_quote=q['datetime'],contract=code,**features(h,q['datetime'])))
    ctl,meta,data,ends,priorchecks=r.load();primary='original/corrected_touch/slip1';suffix=primary.split('/',1)[1]
    featuremeta={};opportunities=[];prefixchecks=0
    for row in data['S0_RECLAIM_R2',suffix]:
        if row['period']=='july':continue
        per=row['period'];code=row['execution_contract'];ts=row['warning_ts'];h=histories[per][code];f=features(h,ts)
        truncated=history([q for q in rawdata[per][code] if q['datetime']<=ts]);assert features(truncated,ts)==f;prefixchecks+=1
        featuremeta[row['event_key']]=f;opportunities.append(dict(key=row['event_key'],warning_ts=ts,direction=row['direction'],native_status=r.f.EV(row)['status'],**f))
    accepted={};summary={};time={};rowsout=[]
    for variant in ['E20_ONLY','FULL','LONG_ONLY','SHORT_ONLY']:
        aa,_=r.admit_variant(data,suffix,variant,ctl,meta,ends,'original',False);accepted[variant]=aa
        for row in aa:
            key=row['original_event_key']
            if key not in featuremeta:featuremeta[key]=features(histories[row['period']][row['execution_contract']],row['warning_ts'])
            rowsout.append(dict(variant=variant,row=row,features=featuremeta[key]))
        for scale in ['daily10','quarter30']:
            for state in ['UP','DOWN','RANGE','UNKNOWN']:
                for module in ['all','S0_RECLAIM_R2']:
                    rows=[q for q in aa if featuremeta[q['original_event_key']][scale]['state']==state and (module=='all' or q['module']==module)]
                    summary[variant+'/'+scale+'/'+state+'/'+module]=r.b.z.metric(rows)
        for block in r.b.z.x.blocks():
            for direction in ['up','down']:
                rows=[q for q in aa if q['module']=='S0_RECLAIM_R2' and q['direction']==direction and block['start']<=q['warning_ts']<block['end']]
                time[variant+'/'+block['label']+'/'+direction]=r.b.z.metric(rows)
    write('market-state-coverage.json',coverage);write('opportunities.json',opportunities);write('state-features.json',featuremeta);write('regime-results.json',summary);write('time-direction-results.json',time);write('sources.json',sources)
    with (RUN/'accepted-with-regimes.jsonl').open('x',encoding='utf-8') as out:
        for q in rowsout:out.write(json.dumps(q,ensure_ascii=False)+'\n')
    write('verification.json',dict(causal_prefix_checks=prefixchecks,prior_individual_rows_exact=priorchecks,classification_as_of_decision=True,current_day_daily_close_never_used=True,no_rollover_price_jump_used=True,production_unchanged=all(r.sha(r.b.ROOT/q)==h for q,h in R(r.RUN/'protocol.json')['production_before'].items())))
    print(json.dumps(dict(market_daily_states=dict(collections.Counter(q['daily10']['state'] for q in coverage)),long_new_module_daily={state:{k:summary['LONG_ONLY/daily10/'+state+'/S0_RECLAIM_R2'][k] for k in ['n','net','normalized_net','unknown']} for state in ['UP','DOWN','RANGE','UNKNOWN']}),ensure_ascii=False))

if __name__=='__main__':run()
