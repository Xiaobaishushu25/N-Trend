"""Finite ABC failure reversal experiment, causal first-activation generation."""
import argparse
import collections
import copy
import datetime as dt
import json
import statistics as st
import subprocess
import unittest
import research_swing_jd_btail_confirmation as t

b=t.b
R=t.R
EV=t.EV
RUN=b.ROOT/'research-runs/swing-jd-abc-failure-reversal-20261026-v1'
LEVELS=['S0','BTRUE']
ENTRIES=['BREAK','RETEST']
TARGETS=['A1','R2']
NEW=[l+'_'+e+'_'+g for l in LEVELS for e in ENTRIES for g in TARGETS]
PLACEBO='S0_ORIGINAL_R2'
CONTROLS=['TOUCH_BTRUE','C15_BTRUE','E20S1']
MODELS=NEW+[PLACEBO]+CONTROLS
write=lambda n,x:b.z.s.m.old.write(RUN/n,x)
sha=t.sha

def direction(e):return 1 if e['direction']=='up' else -1

def failure(bs,start,end,seed,level,valid):
    """First close beyond level in reverse direction; original target cancels."""
    original=direction(seed);rev=-original;tick=seed['tick_size']
    q=bs[start];good=q['high'] if original>0 else q['low']
    if (good-seed['target'])*original>=0:return None,'seed_already_reached_S1'
    if (q['close']-level)*rev>=tick-1e-8:return None,'seed_already_failed'
    for j in range(start+1,min(start+61,end+1)):
        q=bs[j]
        if not valid[j]:return None,'missing_or_unverified_observation'
        good=q['high'] if original>0 else q['low']
        if (good-seed['target'])*original>=0:return None,'parent_S1_reached'
        if (q['close']-level)*rev>=tick-1e-8:
            if j<2 or not all(valid[j-2:j+1]):return None,'failure_stop_history_unverified'
            return j,'failure_confirmed'
    return None,'no_failure_before_expiry'

def retest(bs,first,end,level,seed,valid):
    original=direction(seed);rev=-original;tick=seed['tick_size'];target=level+rev*seed['a_move']
    q=bs[first];good=q['high'] if rev>0 else q['low']
    if (good-target)*rev>=0:return None,'A_extension_already_reached'
    for j in range(first+1,min(first+13,end+1)):
        if not valid[j]:return None,'missing_or_unverified_observation'
        q=bs[j];good=q['high'] if rev>0 else q['low'];bad=q['low'] if rev>0 else q['high']
        if (good-target)*rev>=0:return None,'A_extension_reached_before_retest'
        if (q['close']-level)*original>=tick-1e-8:return None,'level_reclaimed_on_close'
        tag=(bad-level)*rev<=0
        reject=(q['close']-level)*rev>=tick-1e-8 and (q['close']-q['open'])*rev>0
        if tag and reject:
            if j<2 or not all(valid[j-2:j+1]):return None,'retest_stop_history_unverified'
            return j,'retest_confirmed'
    return None,'no_retest_before_expiry'

def geometry(bs,j,seed,level,target,original=False):
    parent=seed['event'];sign=direction(parent) if original else -direction(parent);tick=parent['tick_size']
    buffer=abs(parent['s2_price']-parent['stop']);entry=bs[j]['close'];last3=bs[j-2:j+1]
    rev=-direction(parent)
    reverse_stop=(min(q['low'] for q in last3)-buffer) if rev>0 else (max(q['high'] for q in last3)+buffer)
    risk=(entry-reverse_stop)*rev
    stop=entry-sign*risk if original else reverse_stop
    goal=entry+sign*2*risk if target=='R2' else level+sign*parent['a_move']
    return dict(direction='up' if sign>0 else 'down',entry=entry,trigger_level=entry,stop=stop,target=goal,risk=risk,
        rr=(goal-entry)*sign/risk if risk>0 else 0.,warning_extreme=bs[j]['high'] if sign>0 else bs[j]['low'])

def prepare():
    RUN.mkdir(exist_ok=False);parent=R(b.RUN/'protocol.json');templates={}
    for m in NEW+[PLACEBO]:
        q=copy.deepcopy(parent['templates']['TOUCH_BTRUE']);q.update(name=m,b_extreme=False)
        q['rules']['entry_mode']='signal_next_open';templates[m]=q
    for m in CONTROLS:templates[m]=copy.deepcopy(parent['templates'][m])
    p=dict(id=RUN.name,created_at=dt.datetime.now().isoformat(),models=MODELS,candidates=NEW,placebo=PLACEBO,templates=templates,
        primary=parent['primary'],policies=parent['policies'],scenarios=parent['scenarios'],
        failures=dict(S0='Structure-level failure: first completed15m close >=1tick beyond originalA origin againstA. Seed must not alreadyhaveclosedbeyondS0 duringB.',
            BTRUE='Weaker diagnostic: close >=1tick beyond entireB extreme frozenatseed. This is not fullABC invalidation.'),
        eligibility='Original231score>=2.5mainseeds plus16knownJulyseeds. Whole originalABC formation must maptoonesameactualcontract; seedbodypositiveA gate retained. Missing/unverified15m observation cancels monitoring. Strictpostseed closefailure, not a stop-fill reaction. OriginalS1 touched fromseedthroughfailure cancels; failurelifetime60completed15mbars, scoringwindow/rollcapped.',
        first_activation='Before entry economics orretest outcome, choose firstfailure perAcluster+failuredefinition byfailureclock, thenearliestseed/key. Onlyone activation perA evenif retestneverforms; no choosing latermoreprofitable seeds. Each entry/target then runsactual continuousone-order ledger; standalone variants cannotbesummed.',
        entries=dict(BREAK='After failureclose,nextobserved1mOPEN plusadverseslip.',RETEST='Within12latercompleted15mbars, price tags failedlevel fromreverse side thencloses >=1tick back inreverse direction withdirectionalbody. A1extension touched, onecompletedclose reclaim>=1tick, missingdata,roll orscoringend cancels. Nextobserved1mOPEN. No order reserved beforeconfirmedrejection.'),
        risk='Frozen atdecision: adverseextremeagainstreverse oflast3completed15mbars plusoriginalABC stopbuffer. Buffer isoriginalS2-to-stop distance, not fitted. No ATR/stopwidth grid, no laterstopwidening. Actual-fill minrisk,netRR,chasechecks retained.',
        targets=dict(A1='Failedlevel + reverse*A_move, roundedtowardentry; no extension ifalreadyreachedatfailure orwhileretestpending.',R2='Decisionclose + reverse*2plannedrisk,notactualfilled2R; same pre-entry A1completioncancellation assharedlifetime guard.'),
        placebo_definition='S0_ORIGINAL_R2: sameS0failuredecisionclock, sameplannedriskdistance, same2R target, directionoriginalA. Symmetric artificialstop fordirectiondiagnostic; noteligibleforpromotion orclaimedasstructuralstrategy.',
        shared='ABC retained asparentcontext; reverseorientation,decisionstop andtarget are anewrulefamily, not exit-onlypatch. Shared parentApositivebody,score,cost<=.20R,cost/space<=20%,plan/fillnetRR>=1,min10tickrisk,60holdbuckets,holidays/weekend; nohouradaptive,noNoFollow. Nativepeak-onlysafe equivalence unchanged.',
        validation=parent['validation'],data='MainJan-Jun+July1-10+Aug10-Oct23; knownJuly13-24 fixedJD2608 scoredseparately, notfreshOOS. Descendants capped byeachscoringwindow; missedcrosswindowdescendants notclaimedzeroopportunity. AlloriginalfailedABC warningsincluded whether originaltradeeverentered orwasloser.',
        limitation='Sequential exploratory8candidates+1directionplacebo on repeatedlystudiedhistory. Matchedclock directioncontrol doesnotproveABC incrementalvalue overgenericmomentum. No60trade/halfEGreturnguarantee, noaccountROI claim. Old3controls retainownrules.',
        production_before=parent['production_before'],script_sha256=sha(__file__),dependencies={str(q):sha(q) for q in [b.ROOT/'scripts/research_swing_jd_btail_confirmation.py',b.ROOT/'scripts/research_swing_jd_abc_execution.py']},native_binary=R(b.RUN/'binary.json'))
    assert sha(p['native_binary']['path'])==p['native_binary']['sha256'];write('protocol.json',p)

def generate(period):
    p=R(RUN/'protocol.json');assert p['script_sha256']==sha(__file__)
    paths,recognition,seeds,dates=t.period_paths(period);inp=R(recognition);bs=b.z.x.identification_bars(inp['symbols']['JD0']['bars']);segments=R(paths['segments'])['JD0']
    index={q['datetime']:i for i,q in enumerate(bs)};counts=collections.Counter(q['datetime'] for q in bs)
    codes=[next((s['underlying'] for s in segments if s['first']<=q['datetime']<=s['last']),None) for q in bs]
    raw={k:{q['datetime']:q for q in v['minutes']} for k,v in R(paths['contracts']).items() if k.startswith('DCE.jd')}
    valid=[];actual={};quality=collections.Counter()
    for q,code in zip(bs,codes):
        end=dt.datetime.fromisoformat(q['datetime']);wanted=[(end-dt.timedelta(minutes=i)).isoformat(sep=' ') for i in range(14,-1,-1)];mins=[raw.get(code,{}).get(x) for x in wanted]
        ok=counts[q['datetime']]==1 and all(v is not None and v['volume']>0 for v in mins)
        if ok:
            c=dict(q,open=mins[0]['open'],close=mins[-1]['close'],high=max(v['high'] for v in mins),low=min(v['low'] for v in mins),volume=sum(v['volume'] for v in mins),hold=mins[-1]['hold'])
            ok=all(abs(c[k]-q[k])<1e-7 for k in ['open','close','high','low'])
            if ok:actual[q['datetime']]=c
        valid.append(ok);quality['verified' if ok else 'unverified']+=1
    proposals=collections.defaultdict(list);ledger=[];sources={s['event_key']:s for s in seeds};hi={'early':'2026-07-10 15:00:00','recent':'2026-10-23 11:05:00','july':'2026-07-24 15:00:00'}[period]
    for s in sorted(seeds,key=lambda s:(s['event']['warning_ts'],s['event_key'])):
        e=s['event'];key=s['event_key'];start=index[e['warning_ts']];s0=index[e['s0_ts']];s1=index[e['s1_ts']];code=codes[start];cl='|'.join(e[k] for k in ['symbol','direction','s0_ts','s1_ts']);end=start
        for j in range(start+1,min(start+73,len(bs))):
            if codes[j]!=code or bs[j]['datetime']>hi:break
            end=j
        same_contract=all(c==code for c in codes[s0:start+1])
        for levelname in LEVELS:
            level=e['s0_price'] if levelname=='S0' else s['entry_quality']['b_true_extreme'];reason=None;rev=-direction(e)
            if not same_contract:reason='ABC_crossed_contract'
            elif not valid[start]:reason='seed_unverified'
            elif levelname=='S0' and any((q['close']-level)*rev>=e['tick_size']-1e-8 for q in bs[s1+1:start+1]):reason='S0_already_failed_during_B'
            if reason:fi=None
            else:fi,reason=failure(bs,start,end,e,level,valid)
            rec=dict(parent_key=key,cluster=cl,seed_ts=e['warning_ts'],level_name=levelname,level=level,reason=reason,failure_ts=None if fi is None else bs[fi]['datetime']);ledger.append(rec)
            if fi is None:continue
            assert failure(bs[:fi+1],start,fi,e,level,valid[:fi+1])==(fi,'failure_confirmed')
            proposals[levelname].append(dict(rec,fi=fi,end=end,code=code))
    chosen=[]
    for name,items in proposals.items():
        seen=set()
        for q in sorted(items,key=lambda q:(q['failure_ts'],q['seed_ts'],q['parent_key'])):
            if q['cluster'] in seen:continue
            seen.add(q['cluster']);chosen.append(q)
    pools={m:[] for m in NEW+[PLACEBO]};meta={};activation=[]
    for q in chosen:
        source=sources[q['parent_key']];e=source['event'];fi=q['fi'];level=q['level'];rev=-direction(e)
        a1=level+rev*e['a_move'];good=bs[fi]['high'] if rev>0 else bs[fi]['low'];done=(good-a1)*rev>=0
        ri,reason=retest(bs,fi,q['end'],level,e,valid)
        if ri is not None:assert retest(bs[:ri+1],fi,ri,level,e,valid[:ri+1])==(ri,'retest_confirmed')
        activation.append(dict(q,break_extension_already_done=done,retest_reason=reason,retest_ts=None if ri is None else bs[ri]['datetime']))
        for entry,j in [('BREAK',None if done else fi),('RETEST',ri)]:
            if j is None:continue
            names=[q['level_name']+'_'+entry+'_'+g for g in TARGETS]
            if q['level_name']=='S0' and entry=='BREAK':names.append(PLACEBO)
            for m in names:
                target=m.split('_')[-1];geo=geometry(bs,j,source,level,target,m==PLACEBO);snap=copy.deepcopy(source);newkey='ABCFAIL|'+q['level_name']+'|'+entry+'|'+q['parent_key']+'|'+bs[j]['datetime'];ce=snap['event']
                ce.update(geo,warning_ts=bs[j]['datetime'],market_context=None,warning_kind='abc_failure');ce['setup_features']['direction']=geo['direction']
                snap.update(event_key=newkey,warning_candle=actual[bs[j]['datetime']],prior=[]);pools[m].append(snap)
                meta[newkey]=dict(parent_key=q['parent_key'],cluster=q['cluster'],seed_ts=q['seed_ts'],failure_ts=q['failure_ts'],warning_ts=bs[j]['datetime'],period=period,contract=q['code'],level_name=q['level_name'],entry_kind=entry,original_direction=e['direction'])
    for m,pool in pools.items():
        dest=RUN/period/m;dest.mkdir(parents=True);b.z.s.m.old.write(dest/(period+'-pool.json'),pool)
        b.z.s.m.old.write(dest/'execution.protocol.json',dict(templates=[p['templates'][m]],modes=['corrected_touch','next_minute_open'],periods={period:paths}))
    if period=='july':
        for m in ['TOUCH_BTRUE','E20S1']:
            dest=RUN/period/m;dest.mkdir();b.z.s.m.old.write(dest/(period+'-pool.json'),seeds)
            b.z.s.m.old.write(dest/'execution.protocol.json',dict(templates=[p['templates'][m]],modes=['corrected_touch','next_minute_open'],periods={period:paths}))
    write(period+'/metadata.json',meta);write(period+'/failure-ledger.json',ledger);write(period+'/first-activations.json',activation)
    write(period+'/generation-checks.json',dict(parent_count=len(seeds),pool_counts={m:len(v) for m,v in pools.items()},bar_quality=dict(quality),causal_prefix_checks=True,first_activation_before_retest_or_economics=True,source_hashes={str(q):sha(q) for q in [recognition,paths['contracts'],paths['segments'],paths['fees'],paths['holidays']]}))
    print(period,{m:len(v) for m,v in pools.items()},flush=True)

def replay(period):
    p=R(RUN/'protocol.json');exe=p['native_binary'];assert sha(exe['path'])==exe['sha256']
    for m in NEW+[PLACEBO]+(['TOUCH_BTRUE','E20S1'] if period=='july' else []):
        dest=RUN/period/m;subprocess.run([exe['path'],str(dest/'execution.protocol.json'),period,'replay',str(dest/'executions')],check=True)

def load(july=False):
    c,meta,old,ends,_,checks,_=b.load();data=collections.defaultdict(list);fees=R(b.ROOT/'scripts/exchange_fees_20261022.json')
    for (m,sf),rows in old.items():
        if m in CONTROLS:data[m,sf]=list(rows)
    for period in ['early','recent']+(['july'] if july else []):
        meta.update(R(RUN/period/'metadata.json'));cal=b.z.s.TradingDates({});cal.dates=t.period_paths(period)[3]
        # July intact controls keep original seed keys, not descendant keys.
        # Their parentA identity is warning-known and needed for occupancy.
        for seed in t.period_paths(period)[2]:
            e=seed['event'];meta.setdefault(seed['event_key'],dict(parent_key=seed['event_key'],cluster='|'.join(e[k] for k in ['symbol','direction','s0_ts','s1_ts']),seed_ts=e['warning_ts'],warning_ts=e['warning_ts'],period=period))
        for m in NEW+[PLACEBO]+(['TOUCH_BTRUE','E20S1'] if period=='july' else []):
            dest=RUN/period/m
            for code,end in R(dest/'executions/manifest.json')['contract_ends'].items():ends[code]=max(ends.get(code,''),end)
            for line in (dest/'executions/evaluations.jsonl').open(encoding='utf-8'):
                r=b.z.s.reprice(json.loads(line),fees,cal);r['period']=period;name,sf=r['scenario'].split('/',1);data[name,sf].append(r)
    return c,meta,data,ends,checks

def cases(p):return t.cases(p)

def evaluate(c,meta,data,ends,p,before=None):
    aa={};result={}
    for case in cases(p):
        pol,sf=case.split('/',1)
        for m in MODELS:
            rows=[r for r in data[m,sf] if before is None or r['warning_ts']<before];ar,_=b.admit(rows,ends,pol,c,meta);aa[case,m]=ar
            q=b.z.metric(ar);q['blocks']={f['label']:b.z.metric([r for r in ar if f['start']<=r['warning_ts']<f['end']]) for f in b.z.x.blocks()};result[case+'/'+m]=q
    return aa,result

def select(aa,cutoff,p):
    dates=R(b.RUN/'generation-checks.json')['covered_dates'];nday=sum(d<cutoff[:10] for d in dates);metrics={};eligible=[];reject={}
    for m in MODELS:metrics[m]={case:b.z.metric(b.z.x.visible(aa[case,m],cutoff)) for case in cases(p)}
    for m in NEW:
        zs=metrics[m];q=zs[p['primary']];why=[]
        if q['n']<20 or q['n']/nday<.35:why.append('train_frequency')
        if min(z['normalized_net'] for z in zs.values())<=0:why.append('stress_nonpositive')
        if q['normalized_ex_best']<=0:why.append('ex_best_nonpositive')
        if any(v['normalized_net']<metrics['E20S1'][case]['normalized_net']-1e-7 for case,v in zs.items()):why.append('below_reference')
        if why:reject[m]=why
        else:eligible.append((min(z['normalized_net'] for z in zs.values())-.25*max(z['normalized_realized_dd'] for z in zs.values()),m))
    eligible.sort(key=lambda q:(-round(q[0],8),NEW.index(q[1])))
    return dict(cutoff=cutoff,chosen=eligible[0][1] if eligible else 'E20S1',eligible=eligible,rejections=reject,metrics=metrics)

def analyze_main():
    p=R(RUN/'protocol.json');c,meta,data,ends,checks=load();aa,res=evaluate(c,meta,data,ends,p)
    write('main-results.json',res);schedule=[dict(select(aa,start,p),start=start,end=end) for start,end in b.FOLDS];write('chronological-selection.json',schedule)
    pre,_=evaluate(c,meta,data,ends,p,'2026-07-13 00:00:00');write('july-cutoff-selection.json',select(pre,'2026-07-13 00:00:00',p))
    with (RUN/'accepted-main-primary.jsonl').open('x',encoding='utf-8') as out:
        for m in MODELS:
            for r in aa[p['primary'],m]:out.write(json.dumps(dict(model=m,row=r),ensure_ascii=False)+'\n')
    wf={}
    for case in cases(p):
        pol,sf=case.split('/',1);stream=[]
        for m in MODELS:
            if m==PLACEBO:continue
            stream.extend(r for r in data[m,sf] if next((f['chosen'] for f in schedule if f['start']<=r['warning_ts']<f['end']),'E20S1')==m)
        ar,_=b.admit(stream,ends,pol,c,meta);wf[case]=b.z.metric([r for r in ar if r['warning_ts']>=b.FOLDS[0][0]])
    write('walkforward-results.json',wf);write('main-verification.json',dict(exact_native_controls=checks,script_hash_matches=sha(__file__)==p['script_sha256']))
    print(json.dumps({m:{k:res[p['primary']+'/'+m][k] for k in ['n','net','normalized_net','unknown']} for m in MODELS}),flush=True)

def analyze_all():
    p=R(RUN/'protocol.json');c,meta,data,ends,checks=load(True);aa,res=evaluate(c,meta,data,ends,p);write('combined-results.json',res)
    july={}
    for case in cases(p):
        for m in MODELS:
            if m=='C15_BTRUE':continue
            july[case+'/'+m]=b.z.metric([r for r in aa[case,m] if '2026-07-13'<=r['warning_ts']<'2026-07-25'])
    write('july-results.json',july)
    with (RUN/'accepted-combined-primary.jsonl').open('x',encoding='utf-8') as out:
        for m in MODELS:
            for r in aa[p['primary'],m]:out.write(json.dumps(dict(model=m,row=r),ensure_ascii=False)+'\n')

def paths_diagnostic():
    records=[]
    for period in ['early','recent']:
        pp=R(b.ROOT/'research-strategies/best-a-portfolio-observation-v1/protocol.json')['periods'][period];prices=b.a.Prices(pp);meta=R(RUN/period/'metadata.json');parents={s['event_key']:s for s in t.period_paths(period)[2]}
        for name in LEVELS:
            for s in R(RUN/period/(name+'_BREAK_R2')/(period+'-pool.json')):
                e=s['event'];m=meta[s['event_key']];path=prices.path(m['contract'],e['warning_ts'],16);raw,complete=b.a.window(path,16);atr=parents[m['parent_key']]['atr14'];sign=direction(e)
                q=dict(key=s['event_key'],failure_name=name,warning_ts=e['warning_ts'],original_direction=m['original_direction'],complete=complete)
                if complete:q.update(b.a.excursion(raw,s['warning_candle']['close'],atr,sign),race=b.a.barrier(raw,s['warning_candle']['close'],atr,sign)[0])
                records.append(q)
    write('failure-paths.json',records)

class Tests(unittest.TestCase):
    def bars(self):return [dict(datetime=f'2026-01-05 09:{i:02}:00',open=101.,high=103.,low=99.,close=101.) for i in range(10)]
    def seed(self):return dict(direction='up',tick_size=1.,target=120.,a_move=20.)
    def test_failure_needs_close_beyond_level_not_wick(self):
        bs=self.bars();bs[4].update(low=95.,close=100.);bs[5].update(close=99.)
        self.assertEqual(failure(bs,3,5,self.seed(),100.,[True]*10),(5,'failure_confirmed'))
    def test_failure_symmetric_and_prefix_invariant(self):
        bs=self.bars();bs[4]['close']=99.;e=self.seed();expected=failure(bs,3,9,e,100.,[True]*10);self.assertEqual(expected,(4,'failure_confirmed'));self.assertEqual(failure(bs[:5],3,4,e,100.,[True]*5),expected)
        mirrored=[dict(q,open=-q['open'],close=-q['close'],high=-q['low'],low=-q['high']) for q in bs];e.update(direction='down',target=-120.)
        self.assertEqual(failure(mirrored,3,9,e,-100.,[True]*10),expected)
    def test_parent_success_cancels_before_reverse_failure(self):
        bs=self.bars();bs[4]['high']=121.;bs[5]['close']=99.;self.assertEqual(failure(bs,3,9,self.seed(),100.,[True]*10)[1],'parent_S1_reached')
    def test_missing_bar_cancels_monitoring(self):
        bs=self.bars();bs[5]['close']=99.;valid=[True]*10;valid[4]=False
        self.assertEqual(failure(bs,3,9,self.seed(),100.,valid)[1],'missing_or_unverified_observation')
    def test_retest_must_be_later_tag_and_reject(self):
        bs=self.bars();bs[3].update(open=100.,close=98.,high=101.,low=97.);bs[4].update(open=99.,close=98.,high=100.,low=97.)
        self.assertEqual(retest(bs,3,9,100.,self.seed(),[True]*10),(4,'retest_confirmed'))
        self.assertEqual(retest(bs[:5],3,4,100.,self.seed(),[True]*5),(4,'retest_confirmed'))
    def test_reclaim_and_extension_cancel_retest(self):
        bs=self.bars();bs[3].update(close=98.,low=97.);bs[4]['close']=101.
        self.assertEqual(retest(bs,3,9,100.,self.seed(),[True]*10)[1],'level_reclaimed_on_close');bs[4]['low']=79.
        self.assertEqual(retest(bs,3,9,100.,self.seed(),[True]*10)[1],'A_extension_reached_before_retest')
    def test_stop_and_R2_use_only_closed_decision_bars(self):
        bs=self.bars();bs[4].update(close=95.,low=94.);parent=dict(event=dict(self.seed(),s2_price=102.,stop=101.));q=geometry(bs,4,parent,100.,'R2');self.assertEqual(q['stop'],104.);self.assertEqual(q['risk'],9.);self.assertEqual(q['target'],77.)
        bs[9]['high']=999.;self.assertEqual(q,geometry(bs,4,parent,100.,'R2'))
    def test_original_direction_placebo_matches_clock_risk_and_R2(self):
        bs=self.bars();bs[4].update(close=95.,low=94.);s=dict(event=dict(self.seed(),s2_price=102.,stop=101.));r=geometry(bs,4,s,100.,'R2');p=geometry(bs,4,s,100.,'R2',True)
        self.assertEqual(r['entry'],p['entry']);self.assertEqual(r['risk'],p['risk']);self.assertEqual(r['target']-r['entry'],-(p['target']-p['entry']))

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('stage',choices=['test','prepare','generate','replay','analyze_main','analyze_all','paths_diagnostic']);ap.add_argument('--period',choices=['early','recent','july']);args=ap.parse_args()
    if args.stage=='test':unittest.main(argv=[__file__])
    elif args.stage in ['generate','replay']:globals()[args.stage](args.period)
    else:globals()[args.stage]()
