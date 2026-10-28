"""ABC B-tail confirmation, frozen finite rules, existing immutable native engine."""
import argparse
import collections
import copy
import datetime as dt
import json
import statistics as st
import subprocess
import unittest
import research_swing_jd_warning_quality as w

b=w.b
R=w.R
EV=w.EV
RUN=b.ROOT/'research-runs/swing-jd-btail-confirmation-20261026-v2'
FAMILIES=['HOLD2','PIVOT3','BOS3','ANY']
NEW=[f+'_'+e for f in FAMILIES for e in ['OPEN','TOUCH']]
CONTROLS=['TOUCH_BTRUE','C15_BTRUE','E20S1']
MODELS=NEW+CONTROLS
write=lambda n,x:b.z.s.m.old.write(RUN/n,x)
sha=w.sha

def confirmed(bs,j,start,s1,sign,family):
    """Only closed bars <= j; direction-transform makes short exactly symmetric."""
    if j<3 or j-3<=s1:return False
    def c(i):return bs[i]['close']*sign
    def lo(i):return bs[i]['low'] if sign>0 else -bs[i]['high']
    def hi(i):return bs[i]['high'] if sign>0 else -bs[i]['low']
    hold=j>=start+2 and lo(j)>=lo(j-1)>=lo(j-2) and c(j)>c(j-1)>c(j-2)
    pivot=lo(j-1)<=lo(j-2) and lo(j-1)<lo(j) and c(j)>hi(j-1)
    bos=c(j)>max(hi(k) for k in range(j-3,j)) and lo(j)>=lo(j-1)
    return dict(HOLD2=hold,PIVOT3=pivot,BOS3=bos,ANY=hold or pivot or bos)[family]

def first_confirmation(bs,start,end,s1,seed,stop,family,valid):
    sign=1 if seed['direction']=='up' else -1
    for j in range(start,min(start+13,end+1)):
        q=bs[j];adverse=q['low'] if sign>0 else q['high'];good=q['high'] if sign>0 else q['low']
        if (adverse-stop)*sign<=0:return None,'B_invalidated'
        if (good-seed['target'])*sign>=0:return None,'S1_reached'
        if j<3 or not all(valid[k] for k in range(j-3,j+1)):continue
        if confirmed(bs,j,start,s1,sign,family):return j,'confirmed'
    return None,'no_confirmation_in12'

def prepare():
    RUN.mkdir(exist_ok=False);parent=R(b.RUN/'protocol.json');templates={}
    for m in NEW:
        t=copy.deepcopy(parent['templates']['TOUCH_BTRUE']);t['name']=m;t['rules']['entry_mode']='signal_next_open' if m.endswith('OPEN') else 'touch';templates[m]=t
    for m in CONTROLS:templates[m]=copy.deepcopy(parent['templates'][m])
    p=dict(id=RUN.name,created_at=dt.datetime.now().isoformat(),models=MODELS,challengers=NEW,templates=templates,
        primary=parent['primary'],policies=parent['policies'],scenarios=parent['scenarios'],
        family_definitions=dict(HOLD2='At least2postseed completed bars; last3 adverse extremes nonworsening, last3 closes strictly directional.',
            PIVOT3='Middle adverse extreme <=left, strictly better right adverse extreme; right CLOSE directionally beyond middle favorable extreme.',
            BOS3='CLOSE directionally beyond favorable extremes of preceding3bars; adverse extreme no worse than precedingbar.',
            ANY='First observation satisfying any of these3 rules, no weighted fitting.'),
        confirmation='Original ABC seed through next12completed15m bars inclusive. PIVOT3/BOS3 may confirm at original seed; HOLD2 requires2laterbars. Previous3bars must be afterS1 and all4sameactualcontract, unique timestamp, complete15actual1m withpositivevolume, OHLC matching recognitionbars. First eligible confirmation only perseed; duplicate family/Acluster/decisiontime keeps earliestseed, independent of economics/outcome.',
        cancellation='From seed through confirmation, entireB stop touched or originalS1 touched cancels, even if same completedbar otherwise confirms. Original stop buffer andS1 frozen atseed. Roll/scoringwindow end caps confirmation. No stop widening afterseed.',
        entry='OPEN uses confirmationclose asplan, next observed1mOPEN plusadverse slip; TOUCH uses confirmation favorableextreme+1tick. Old chase/holiday/plan+fillnetRR/cost/risk/pending/maxhold safeguards. Eight deterministic variants, no threshold grid, no unconfirmed order reservation. Shared parent score>=2.5 andpositiveA-net-body retained.',
        validation=parent['validation'],comparison='3 intact controls, including previous C15 requiringpriorbreak; continuous one-order ledger andsameA2settledstops. Primary history and historical Julyfailure window reported separately. Combined chronology also reported; strategies cannot be summed.',
        data='Same main231qualifiedJDseeds; historical July13-24 fixedJD2608 window16seeds inherited from prior study. No new calendar periods described asuntouched. Supplemental decisionJuly13 reads only preJuly13closedmainrows, a retrospective time-cutoff diagnostic afterknownJulyfailure inspiredthisstudy.',
        limitation='Sequential exploration on repeatedly studiedJD; 8hypotheses, weaksmallblocks; strictchronology is not untouchedOOS. No promise60trades or halfEG returns. One-lotnet andfractional300yuan plannedrisk normalization, no accountreturn/marginclaim.',
        production_before=parent['production_before'],script_sha256=sha(__file__),dependencies={str(q):sha(q) for q in [b.ROOT/'scripts/research_swing_jd_abc_execution.py',b.ROOT/'scripts/research_swing_jd_warning_quality.py']},
        native_binary=R(b.RUN/'binary.json'))
    assert sha(p['native_binary']['path'])==p['native_binary']['sha256'];write('protocol.json',p)

def period_paths(period):
    if period!='july':
        paths=R(b.RUN/period/'seed/execution.protocol.json')['periods'][period]
        recognition=R(b.ROOT/'research-strategies/best-a-portfolio-observation-v1/protocol.json')['periods'][period]['input']
        return paths,recognition,R(b.RUN/period/'seed'/(period+'-pool.json')),R(b.RUN/period/'seed/trading-dates.json')
    old=w.RUN/'supplement';paths=R(old/'E20S1/execution.protocol.json')['periods']['supplement']
    return paths,old/'input.json',R(old/'qualified-seeds.json'),R(old/'trading-dates.json')

def generate(period):
    p=R(RUN/'protocol.json');assert p['script_sha256']==sha(__file__)
    paths,recognition,seeds,dates=period_paths(period);inp=R(recognition);bs=b.z.x.identification_bars(inp['symbols']['JD0']['bars']);segments=R(paths['segments'])['JD0']
    indexes={q['datetime']:i for i,q in enumerate(bs)};counts=collections.Counter(q['datetime'] for q in bs)
    codes=[next((s['underlying'] for s in segments if s['first']<=q['datetime']<=s['last']),None) for q in bs]
    raw={k:{q['datetime']:q for q in v['minutes']} for k,v in R(paths['contracts']).items() if k.startswith('DCE.jd')}
    valid=[];quality=collections.Counter()
    for q,code in zip(bs,codes):
        end=dt.datetime.fromisoformat(q['datetime']);wanted=[(end-dt.timedelta(minutes=j)).isoformat(sep=' ') for j in range(14,-1,-1)]
        minutes=[raw.get(code,{}).get(t) for t in wanted]
        if counts[q['datetime']]!=1:valid.append(False);quality['duplicate_label']+=1;continue
        if not all(v is not None and v['volume']>0 for v in minutes):valid.append(False);quality['missing_or_zero_volume']+=1;continue
        actual=dict(open=minutes[0]['open'],close=minutes[-1]['close'],high=max(v['high'] for v in minutes),low=min(v['low'] for v in minutes))
        ok=all(abs(actual[k]-q[k])<1e-7 for k in actual);valid.append(ok);quality['exact_actual_OHLC' if ok else 'OHLC_mismatch']+=1
    pools={m:[] for m in NEW};meta={};ledgers=[];seen=set();hi={'early':'2026-07-10 15:00:00','recent':'2026-10-23 11:05:00','july':'2026-07-24 15:00:00'}[period]
    for source in sorted(seeds,key=lambda s:(s['event']['warning_ts'],s['event_key'])):
        e=source['event'];key=source['event_key'];start=indexes[e['warning_ts']];s1=indexes[e['s1_ts']];code=codes[start];end=start
        for j in range(start+1,min(start+13,len(bs))):
            if codes[j]!=code or counts[bs[j]['datetime']]!=1 or bs[j]['datetime']>hi:break
            end=j
        cl='|'.join(e[k] for k in ['symbol','direction','s0_ts','s1_ts']);stop=source['mechanism']['true_b_stop'];sign=1 if e['direction']=='up' else -1
        for family in FAMILIES:
            usable=[v and c==code for v,c in zip(valid,codes)]
            ci,reason=first_confirmation(bs,start,end,s1,e,stop,family,usable)
            rec=dict(parent_key=key,family=family,seed_ts=e['warning_ts'],reason=reason,decision_ts=None if ci is None else bs[ci]['datetime']);ledgers.append(rec)
            if ci is None:continue
            assert first_confirmation(bs[:ci+1],start,ci,s1,e,stop,family,usable[:ci+1])==(ci,'confirmed')
            q=bs[ci];ident=family,cl,q['datetime']
            if ident in seen:rec['reason']='duplicate_cluster_clock';continue
            seen.add(ident)
            for entry in ['OPEN','TOUCH']:
                m=family+'_'+entry;t=copy.deepcopy(source);ce=t['event'];newkey='BTAIL|'+family+'|'+key+'|'+q['datetime']
                price=q['close'] if entry=='OPEN' else (q['high']+e['tick_size'] if sign>0 else q['low']-e['tick_size'])
                ce.update(warning_ts=q['datetime'],entry=price,trigger_level=price,risk=(price-e['stop'])*sign,
                    rr=(e['target']-price)*sign/((price-e['stop'])*sign) if price!=e['stop'] else 0.,warning_extreme=q['high'] if sign>0 else q['low'],market_context=None)
                stamp=dt.datetime.fromisoformat(q['datetime']);actual_minutes=[raw[code][(stamp-dt.timedelta(minutes=k)).isoformat(sep=' ')] for k in range(14,-1,-1)]
                candle=dict(q,volume=sum(v['volume'] for v in actual_minutes),hold=actual_minutes[-1]['hold'])
                t.update(event_key=newkey,warning_candle=candle,prior=[]);pools[m].append(t)
                # OPEN/TOUCH share decision metadata and keep original parentA.
                meta[newkey]=dict(parent_key=key,cluster=cl,seed_ts=e['warning_ts'],warning_ts=q['datetime'],period=period,contract=code,family=family,delay_bars=ci-start)
        meta[key]=dict(parent_key=key,cluster=cl,seed_ts=e['warning_ts'],warning_ts=e['warning_ts'],period=period,contract=code,kind='seed')
    for m,pool in pools.items():
        dest=RUN/period/m;dest.mkdir(parents=True);b.z.s.m.old.write(dest/(period+'-pool.json'),pool)
        b.z.s.m.old.write(dest/'execution.protocol.json',dict(templates=[p['templates'][m]],modes=['corrected_touch','next_minute_open'],periods={period:paths}))
    # Historical failure window controls are replayed by the same native binary.
    if period=='july':
        for m in ['TOUCH_BTRUE','E20S1']:
            dest=RUN/period/m;dest.mkdir();b.z.s.m.old.write(dest/(period+'-pool.json'),seeds)
            b.z.s.m.old.write(dest/'execution.protocol.json',dict(templates=[p['templates'][m]],modes=['corrected_touch','next_minute_open'],periods={period:paths}))
    write(period+'/metadata.json',meta);write(period+'/confirmation-ledger.json',ledgers)
    write(period+'/generation-checks.json',dict(qualified_parents=len(seeds),confirmation_counts={m:len(v) for m,v in pools.items()},bar_quality=dict(quality),source_hashes={str(q):sha(q) for q in [recognition,paths['contracts'],paths['segments']]},causal_prefix_checks=True))
    print(period,{m:len(v) for m,v in pools.items()},flush=True)

def replay(period):
    p=R(RUN/'protocol.json');exe=p['native_binary'];assert sha(exe['path'])==exe['sha256']
    for m in NEW+(['TOUCH_BTRUE','E20S1'] if period=='july' else []):
        d=RUN/period/m;subprocess.run([exe['path'],str(d/'execution.protocol.json'),period,'replay',str(d/'executions')],check=True)

def load(include_july=False):
    c,meta,old,ends,_,checks,_=b.load();data=collections.defaultdict(list);fees=R(b.ROOT/'scripts/exchange_fees_20261022.json')
    for (m,sf),rows in old.items():
        if m in CONTROLS:data[m,sf]=list(rows)
    for period in ['early','recent']+(['july'] if include_july else []):
        meta.update(R(RUN/period/'metadata.json'));_,_,_,dates=period_paths(period);cal=b.z.s.TradingDates({});cal.dates=dates
        for m in NEW+(['TOUCH_BTRUE','E20S1'] if period=='july' else []):
            dest=RUN/period/m
            for code,end in R(dest/'executions/manifest.json')['contract_ends'].items():ends[code]=max(ends.get(code,''),end)
            for line in (dest/'executions/evaluations.jsonl').open(encoding='utf-8'):
                r=b.z.s.reprice(json.loads(line),fees,cal);r['period']=period;name,sf=r['scenario'].split('/',1);data[name,sf].append(r)
    return c,meta,data,ends,checks

def cases(p):return w.cases(p)

def evaluate(c,meta,data,ends,p,before=None):
    accepted={};result={}
    for case in cases(p):
        pol,sf=case.split('/',1)
        for m in MODELS:
            rows=[r for r in data[m,sf] if before is None or r['warning_ts']<before];aa,_=b.admit(rows,ends,pol,c,meta);accepted[case,m]=aa
            q=b.z.metric(aa);q['blocks']={f['label']:b.z.metric([r for r in aa if f['start']<=r['warning_ts']<f['end']]) for f in b.z.x.blocks()};result[case+'/'+m]=q
    return accepted,result

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
    eligible.sort(key=lambda z:(-round(z[0],8),NEW.index(z[1])))
    return dict(cutoff=cutoff,chosen=eligible[0][1] if eligible else 'E20S1',eligible=eligible,rejections=reject,metrics=metrics)

def analyze_main():
    p=R(RUN/'protocol.json');c,meta,data,ends,checks=load();aa,res=evaluate(c,meta,data,ends,p)
    schedule=[dict(select(aa,start,p),start=start,end=end) for start,end in b.FOLDS];write('main-results.json',res);write('chronological-selection.json',schedule)
    pre,_=evaluate(c,meta,data,ends,p,'2026-07-13 00:00:00');write('july-cutoff-selection.json',select(pre,'2026-07-13 00:00:00',p))
    with (RUN/'accepted-main-primary.jsonl').open('x',encoding='utf-8') as out:
        for m in MODELS:
            for r in aa[p['primary'],m]:out.write(json.dumps(dict(model=m,row=r),ensure_ascii=False)+'\n')
    wf={}
    for case in cases(p):
        pol,sf=case.split('/',1);stream=[]
        for m in MODELS:stream.extend(r for r in data[m,sf] if next((f['chosen'] for f in schedule if f['start']<=r['warning_ts']<f['end']),'E20S1')==m)
        ar,_=b.admit(stream,ends,pol,c,meta);wf[case]=b.z.metric([r for r in ar if r['warning_ts']>=b.FOLDS[0][0]])
    write('walkforward-results.json',wf);write('main-verification.json',dict(exact_native_controls=checks,script_hash_matches=sha(__file__)==p['script_sha256']))
    print(json.dumps({m:{k:res[p['primary']+'/'+m][k] for k in ['n','net','normalized_net','unknown']} for m in MODELS}),flush=True)

def analyze_all():
    p=R(RUN/'protocol.json');c,meta,data,ends,checks=load(True)
    # C15 July counterpart was not generated: do not compare combined C15 as though fully covered.
    aa,res=evaluate(c,meta,data,ends,p);write('combined-results.json',res);july={}
    for case in cases(p):
        for m in MODELS:
            if m=='C15_BTRUE':continue
            july[case+'/'+m]=b.z.metric([r for r in aa[case,m] if '2026-07-13 00:00:00'<=r['warning_ts']<'2026-07-25 00:00:00'])
    write('july-results.json',july)
    with (RUN/'accepted-combined-primary.jsonl').open('x',encoding='utf-8') as out:
        for m in MODELS:
            for r in aa[p['primary'],m]:out.write(json.dumps(dict(model=m,row=r),ensure_ascii=False)+'\n')

def paths_diagnostic():
    """Confirmation path against seed, descriptive of conditional windows, not alpha proof."""
    records=[]
    for period in ['early','recent']:
        pp=R(b.ROOT/'research-strategies/best-a-portfolio-observation-v1/protocol.json')['periods'][period];pr=b.a.Prices(pp)
        meta=R(RUN/period/'metadata.json');_,_,seeds,_=period_paths(period);parents={s['event_key']:s for s in seeds}
        for m in [f+'_OPEN' for f in FAMILIES]:
            for s in R(RUN/period/m/(period+'-pool.json')):
                e=s['event'];info=meta[s['event_key']];parent=parents[info['parent_key']];atr=parent['atr14'];sign=1 if e['direction']=='up' else -1
                path=pr.path(info['contract'],e['warning_ts'],16);raw,complete=b.a.window(path,16)
                row=dict(family=m.split('_')[0],key=s['event_key'],warning_ts=e['warning_ts'],seed_ts=info['seed_ts'],delay_bars=info['delay_bars'],complete=complete)
                if complete:row.update(b.a.excursion(raw,s['warning_candle']['close'],atr,sign),race=b.a.barrier(raw,s['warning_candle']['close'],atr,sign)[0])
                records.append(row)
    write('confirmation-paths.json',records)

class Tests(unittest.TestCase):
    def bars(self):return [dict(datetime=f'2026-01-05 09:{i:02}:00',open=100.,high=105.,low=95.,close=100.) for i in range(8)]
    def test_hold_needs_two_postseed_bars_and_is_mirrored(self):
        bs=self.bars()
        for i in [4,5,6]:bs[i].update(low=90.+i,high=110.,close=100.+i)
        self.assertFalse(confirmed(bs,5,4,0,1,'HOLD2'));self.assertTrue(confirmed(bs,6,4,0,1,'HOLD2'))
        mirrored=[dict(q,open=-q['open'],close=-q['close'],high=-q['low'],low=-q['high']) for q in bs]
        self.assertTrue(confirmed(mirrored,6,4,0,-1,'HOLD2'))
    def test_pivot_known_only_on_right_close(self):
        bs=self.bars();bs[3].update(low=94.,high=102.);bs[4].update(low=96.,close=103.)
        self.assertTrue(confirmed(bs,4,4,0,1,'PIVOT3'));self.assertEqual(confirmed(bs[:5],4,4,0,1,'PIVOT3'),confirmed(bs,4,4,0,1,'PIVOT3'))
        bs[4]['close']=102.;self.assertFalse(confirmed(bs,4,4,0,1,'PIVOT3'))
    def test_BOS_requires_close_not_wick(self):
        bs=self.bars();bs[4].update(high=110.,close=104.);self.assertFalse(confirmed(bs,4,4,0,1,'BOS3'))
        bs[4]['close']=106.;self.assertTrue(confirmed(bs,4,4,0,1,'BOS3'))
    def test_stop_or_target_before_confirmation_cancels(self):
        bs=self.bars();bs[4].update(close=106.,high=110.);seed=dict(direction='up',target=109.)
        self.assertEqual(first_confirmation(bs,4,7,0,seed,90.,'BOS3',[True]*8),(None,'S1_reached'))
        bs[4]['low']=89.;self.assertEqual(first_confirmation(bs,4,7,0,seed,90.,'BOS3',[True]*8),(None,'B_invalidated'))
    def test_missing_confirmation_bar_not_treated_as_complete(self):
        bs=self.bars();bs[4].update(close=106.,high=110.);seed=dict(direction='up',target=120.);valid=[True]*8;valid[3]=False
        self.assertEqual(first_confirmation(bs,4,4,0,seed,90.,'BOS3',valid)[0],None)
    def test_confirmation_uses_first_shape_not_best_future_outcome(self):
        bs=self.bars();bs[4].update(close=106.,high=110.);bs[6].update(close=115.,high=116.);seed=dict(direction='up',target=120.)
        self.assertEqual(first_confirmation(bs,4,7,0,seed,90.,'ANY',[True]*8),(4,'confirmed'))

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('stage',choices=['test','prepare','generate','replay','analyze_main','analyze_all','paths_diagnostic']);ap.add_argument('--period',choices=['early','recent','july']);args=ap.parse_args()
    if args.stage=='test':unittest.main(argv=[__file__])
    elif args.stage in ['generate','replay']:globals()[args.stage](args.period)
    else:globals()[args.stage]()
