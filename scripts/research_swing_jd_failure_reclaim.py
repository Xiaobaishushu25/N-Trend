"""Sequential follow-up: reclaim after ABC failure, actual failure-extreme stop."""
import argparse
import collections
import copy
import datetime as dt
import json
import subprocess
import unittest
import research_swing_jd_abc_failure_reversal as f

b=f.b
R=f.R
EV=f.EV
RUN=f.RUN/'reclaim-followup'
NEW=[l+'_RECLAIM_'+g for l in ['S0','BTRUE'] for g in ['S1','R2']]
MODELS=NEW+['E20S1','TOUCH_BTRUE',f.PLACEBO]
write=lambda n,x:b.z.s.m.old.write(RUN/n,x)
sha=f.sha

def reclaim(bs,first,end,level,seed,valid):
    sign=f.direction(seed);reverse=-sign;tick=seed['tick_size'];extension=level+reverse*seed['a_move']
    q=bs[first];bad=q['low'] if sign>0 else q['high']
    if (bad-extension)*reverse>=0:return None,'reverse_extension_already_done'
    for j in range(first+1,min(first+13,end+1)):
        if not valid[j]:return None,'unverified_bar'
        q=bs[j];good=q['high'] if sign>0 else q['low'];bad=q['low'] if sign>0 else q['high']
        if (good-seed['target'])*sign>=0:return None,'original_S1_already_reached'
        if (bad-extension)*reverse>=0:return None,'reverse_A_extension_reached'
        if (q['close']-level)*sign>=tick-1e-8:return j,'level_reclaimed'
    return None,'no_reclaim_within12'

def geometry(bs,first,j,e,target):
    sign=f.direction(e);buffer=abs(e['s2_price']-e['stop']);entry=bs[j]['close'];path=bs[first:j+1]
    stop=min(q['low'] for q in path)-buffer if sign>0 else max(q['high'] for q in path)+buffer
    risk=(entry-stop)*sign;goal=e['target'] if target=='S1' else entry+sign*2*risk
    return dict(direction=e['direction'],entry=entry,trigger_level=entry,stop=stop,target=goal,risk=risk,rr=(goal-entry)*sign/risk if risk>0 else 0.,warning_extreme=bs[j]['high'] if sign>0 else bs[j]['low'])

def prepare():
    RUN.mkdir(exist_ok=False);parent=R(f.RUN/'protocol.json');templates={}
    for m in NEW:
        templates[m]=copy.deepcopy(parent['templates']['S0_BREAK_R2']);templates[m]['name']=m
    p=dict(id=RUN.name,created_at=dt.datetime.now().isoformat(),models=MODELS,candidates=NEW,templates=templates,
        primary=parent['primary'],policies=parent['policies'],scenarios=parent['scenarios'],
        hypothesis='AFTER observing reversal losses andoriginal-direction symmetriccontrol profit, testfalsebreak recovery. Explicitsequential exploration, notpreregisteredwithparent oruntouchedOOS.',
        definition='Use alreadyfrozen firstfailure activationsperA/level; within12latercomplete15m bars, firstCLOSE >=1tick backacrossfailedlevel inoriginalA direction. No futurebody/volume threshold fitting. Missingbars,originalS1 touched orreverseA1extension touched cancels. Noorderbefore reclaimclose.',
        stop='Actual adverseextreme fromfailurebar THROUGH reclaimbar plusoriginalABC stopbuffer, frozenatreclaim, noartificialmirroredrisk. Originaldirection nextobserved1mOPEN plusadverseslip; originalS1 ordecisionplanned2R target, allcost/netRR/minrisk/chase/holiday/maxholdrules unchanged.',
        data='Same mainhistory andknownJulyfailurewindow; firstactivations inherited independentoforiginalwinner/lossandno lateractivationrescue. No chronological schedule claimedfresh.',
        validation=parent['validation'],production_before=parent['production_before'],native_binary=parent['native_binary'],script_sha256=sha(__file__),
        sources={str(q):sha(q) for q in [f.RUN/'protocol.json',f.RUN/'main-results.json',b.ROOT/'scripts/research_swing_jd_abc_failure_reversal.py']})
    write('protocol.json',p)

def generate(period):
    p=R(RUN/'protocol.json');assert p['script_sha256']==sha(__file__)
    paths,recognition,seeds,dates=f.t.period_paths(period);inp=R(recognition);bs=b.z.x.identification_bars(inp['symbols']['JD0']['bars']);raw={k:{q['datetime']:q for q in v['minutes']} for k,v in R(paths['contracts']).items() if k.startswith('DCE.jd')};parents={s['event_key']:s for s in seeds}
    pool={m:[] for m in NEW};meta={};ledger=[]
    for a in R(f.RUN/period/'first-activations.json'):
        parent=parents[a['parent_key']];e=parent['event'];first=a['fi'];end=a['end'];code=a['code'];valid=[False]*len(bs);candles={}
        for j in range(max(0,first),min(first+13,end+1)):
            q=bs[j];stamp=dt.datetime.fromisoformat(q['datetime']);minutes=[raw[code].get((stamp-dt.timedelta(minutes=k)).isoformat(sep=' ')) for k in range(14,-1,-1)]
            ok=all(x is not None and x['volume']>0 for x in minutes)
            if ok:
                c=dict(q,open=minutes[0]['open'],close=minutes[-1]['close'],high=max(v['high'] for v in minutes),low=min(v['low'] for v in minutes),volume=sum(v['volume'] for v in minutes),hold=minutes[-1]['hold'])
                ok=all(abs(c[k]-q[k])<1e-7 for k in ['open','close','high','low'])
                if ok:candles[j]=c
            valid[j]=ok
        j,reason=reclaim(bs,first,end,a['level'],e,valid)
        ledger.append(dict(parent_key=a['parent_key'],cluster=a['cluster'],level_name=a['level_name'],failure_ts=a['failure_ts'],reason=reason,reclaim_ts=None if j is None else bs[j]['datetime']))
        if j is None:continue
        assert reclaim(bs[:j+1],first,j,a['level'],e,valid[:j+1])==(j,'level_reclaimed')
        for goal in ['S1','R2']:
            model=a['level_name']+'_RECLAIM_'+goal;s=copy.deepcopy(parent);geo=geometry(bs,first,j,e,goal);key='ABCRECLAIM|'+a['level_name']+'|'+a['parent_key']+'|'+bs[j]['datetime']
            s['event'].update(geo,warning_ts=bs[j]['datetime'],market_context=None,warning_kind='abc_failure_reclaim');s.update(event_key=key,warning_candle=candles[j],prior=[]);pool[model].append(s)
            meta[key]=dict(parent_key=a['parent_key'],cluster=a['cluster'],seed_ts=a['seed_ts'],failure_ts=a['failure_ts'],warning_ts=bs[j]['datetime'],period=period,contract=code,level_name=a['level_name'])
    for m,items in pool.items():
        dest=RUN/period/m;dest.mkdir(parents=True);b.z.s.m.old.write(dest/(period+'-pool.json'),items)
        b.z.s.m.old.write(dest/'execution.protocol.json',dict(templates=[p['templates'][m]],modes=['corrected_touch','next_minute_open'],periods={period:paths}))
    write(period+'/metadata.json',meta);write(period+'/reclaim-ledger.json',ledger);write(period+'/generation-checks.json',dict(pool_counts={m:len(v) for m,v in pool.items()},prefix_checked=True,first_activation_source_hash=sha(f.RUN/period/'first-activations.json')))
    print(period,{m:len(v) for m,v in pool.items()},flush=True)

def replay(period):
    p=R(RUN/'protocol.json');exe=p['native_binary'];assert sha(exe['path'])==exe['sha256']
    for m in NEW:
        dest=RUN/period/m;subprocess.run([exe['path'],str(dest/'execution.protocol.json'),period,'replay',str(dest/'executions')],check=True)

def load(july=False):
    c,meta,old,ends,checks=f.load(july);data=collections.defaultdict(list);fees=R(b.ROOT/'scripts/exchange_fees_20261022.json')
    for (m,sf),rows in old.items():
        if m in MODELS[4:]:data[m,sf]=rows
    for per in ['early','recent']+(['july'] if july else []):
        meta.update(R(RUN/per/'metadata.json'));cal=b.z.s.TradingDates({});cal.dates=f.t.period_paths(per)[3]
        for m in NEW:
            dest=RUN/per/m
            for code,end in R(dest/'executions/manifest.json')['contract_ends'].items():ends[code]=max(ends.get(code,''),end)
            for line in (dest/'executions/evaluations.jsonl').open(encoding='utf-8'):
                r=b.z.s.reprice(json.loads(line),fees,cal);r['period']=per;name,sf=r['scenario'].split('/',1);data[name,sf].append(r)
    return c,meta,data,ends,checks

def evaluate(c,meta,data,ends,p,before=None):
    aa={};result={}
    for case in f.cases(p):
        pol,sf=case.split('/',1)
        for m in MODELS:
            rows=[r for r in data[m,sf] if before is None or r['warning_ts']<before];ar,_=b.admit(rows,ends,pol,c,meta);aa[case,m]=ar
            q=b.z.metric(ar);q['blocks']={f['label']:b.z.metric([r for r in ar if f['start']<=r['warning_ts']<f['end']]) for f in b.z.x.blocks()};result[case+'/'+m]=q
    return aa,result

def analyze(july=False):
    p=R(RUN/'protocol.json');c,meta,data,ends,checks=load(july);aa,result=evaluate(c,meta,data,ends,p);write('combined-results.json' if july else 'main-results.json',result)
    with (RUN/('accepted-combined-primary.jsonl' if july else 'accepted-main-primary.jsonl')).open('x',encoding='utf-8') as out:
        for m in MODELS:
            for r in aa[p['primary'],m]:out.write(json.dumps(dict(model=m,row=r),ensure_ascii=False)+'\n')
    if july:
        write('july-results.json',{case+'/'+m:b.z.metric([r for r in aa[case,m] if '2026-07-13'<=r['warning_ts']<'2026-07-25']) for case in f.cases(p) for m in MODELS})
    else:
        # Localcopy of the unchanged strictselector; placebo nevereligible.
        selections=[]
        for cutoff,end in b.FOLDS:
            days=sum(d<cutoff[:10] for d in R(b.RUN/'generation-checks.json')['covered_dates']);eligible=[];rejections={}
            baseline={case:b.z.metric(b.z.x.visible(aa[case,'E20S1'],cutoff)) for case in f.cases(p)}
            for m in NEW:
                zs={case:b.z.metric(b.z.x.visible(aa[case,m],cutoff)) for case in f.cases(p)};q=zs[p['primary']];why=[]
                if q['n']<20 or q['n']/days<.35:why.append('train_frequency')
                if min(z['normalized_net'] for z in zs.values())<=0:why.append('stress_nonpositive')
                if q['normalized_ex_best']<=0:why.append('ex_best_nonpositive')
                if any(z['normalized_net']<baseline[case]['normalized_net']-1e-7 for case,z in zs.items()):why.append('below_reference')
                if why:rejections[m]=why
                else:eligible.append((min(z['normalized_net'] for z in zs.values())-.25*max(z['normalized_realized_dd'] for z in zs.values()),m))
            eligible.sort(key=lambda q:(-round(q[0],8),NEW.index(q[1])));selections.append(dict(start=cutoff,end=end,chosen=eligible[0][1] if eligible else 'E20S1',rejections=rejections))
        write('chronological-selection.json',selections)
    print(json.dumps({m:{k:result[p['primary']+'/'+m][k] for k in ['n','net','normalized_net','unknown']} for m in NEW}),flush=True)

class Tests(unittest.TestCase):
    def seed(self):return dict(direction='up',tick_size=1.,target=120.,a_move=20.,s2_price=102.,stop=101.)
    def bars(self):return [dict(open=99.,high=100.,low=97.,close=98.) for _ in range(6)]
    def test_reclaim_requires_later_close_and_prefix(self):
        bs=self.bars();bs[3].update(close=101.,high=102.);self.assertEqual(reclaim(bs,1,5,100.,self.seed(),[True]*6),(3,'level_reclaimed'));self.assertEqual(reclaim(bs[:4],1,3,100.,self.seed(),[True]*4),(3,'level_reclaimed'))
    def test_reclaim_wick_without_close_does_not_enter(self):
        bs=self.bars();bs[3]['high']=102.;self.assertEqual(reclaim(bs,1,5,100.,self.seed(),[True]*6)[0],None)
    def test_real_stop_includes_all_failure_extremes_and_is_mirrored(self):
        bs=self.bars();bs[2]['low']=90.;bs[4].update(close=102.,high=104.);e=self.seed();q=geometry(bs,1,4,e,'S1');self.assertEqual(q['stop'],89.);self.assertEqual(q['target'],120.);self.assertEqual(q['risk'],13.)
        short=dict(e,direction='down',target=-120.,s2_price=-102.,stop=-101.);mirror=[dict(open=-a['open'],close=-a['close'],high=-a['low'],low=-a['high']) for a in bs];s=geometry(mirror,1,4,short,'S1');self.assertEqual(s['stop'],-q['stop']);self.assertEqual(s['risk'],q['risk'])
    def test_move_completed_or_missing_cancels(self):
        bs=self.bars();bs[2]['low']=79.;self.assertEqual(reclaim(bs,1,5,100.,self.seed(),[True]*6)[1],'reverse_A_extension_reached');valid=[True]*6;valid[2]=False;self.assertEqual(reclaim(bs,1,5,100.,self.seed(),valid)[1],'unverified_bar')

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('stage',choices=['test','prepare','generate','replay','analyze_main','analyze_all']);ap.add_argument('--period',choices=['early','recent','july']);args=ap.parse_args()
    if args.stage=='test':unittest.main(argv=[__file__])
    elif args.stage in ['generate','replay']:globals()[args.stage](args.period)
    elif args.stage.startswith('analyze'):analyze(args.stage=='analyze_all')
    else:globals()[args.stage]()
