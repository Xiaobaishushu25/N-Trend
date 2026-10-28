"""Predeclared warning timing/quality study; ABC detector and S1 preserved."""
import argparse
import bisect
import collections
import copy
import datetime as dt
import json
import shutil
import statistics as st
import subprocess
import unittest
import research_swing_jd_abc_execution as b

RUN=b.ROOT/'research-runs/swing-jd-warning-quality-20261026-v1'
R=b.READ
EV=b.EV
GROUPS=['BASE','FRESH','ORDERLY','CLEAN','HOUR','FIRST']
NEW=['EARLY_'+g for g in GROUPS]+['PULLBACK_BASE','PULLBACK_FRESH']
MODELS=NEW+['TOUCH_BTRUE','E20S1']
write=lambda n,x:b.z.s.m.old.write(RUN/n,x)
sha=b.z.s.m.old.sha
replace=b.z.s.m.old.replace

def labels(s,first):
    e=s['event'];f=s['entry_quality'];w=s['warning_candle'];sign=1 if e['direction']=='up' else -1
    room=(e['target']-f['b_true_extreme'])*sign
    recovery=(w['close']-f['b_true_extreme'])*sign/room if room>0 else None
    retrace=room/e['a_move'] if e['a_move']>0 else None
    h=e.get('market_context') or {}
    assert f['as_of']<=e['warning_ts']
    if h:assert h['as_of_ts']<=e['warning_ts']
    return dict(BASE=True,FRESH=recovery is not None and 0<=recovery<=.5,
        ORDERLY=retrace is not None and .25<=retrace<=.75 and f['b_true_speed_to_A']<=1,
        CLEAN=f['a_body_eff_ex_s0']>=.5 and f['a_positive_session_gap_share']<=.25 and f['a_largest_abs_body_share']<=.5,
        HOUR=h.get('trend_alignment_60') is not None and h['trend_alignment_60']>0,FIRST=first,
        recovery=recovery,retracement=retrace)

def prepare():
    RUN.mkdir(exist_ok=False);parent=R(b.RUN/'protocol.json');templates={}
    for name in NEW:
        t=copy.deepcopy(parent['templates']['TOUCH_BTRUE']);t['name']=name
        t['rules']['entry_mode']='signal_next_open' if name.startswith('EARLY') else 'warning_pullback'
        templates[name]=t
    for name in MODELS[-2:]:templates[name]=parent['templates'][name]
    p=dict(id=RUN.name,created_at=dt.datetime.now().isoformat(),models=MODELS,new=NEW,templates=templates,
        primary=parent['primary'],policies=parent['policies'],scenarios=parent['scenarios'],
        entries=dict(EARLY='Known warning close is planned entry; next observed minute OPEN market fill plus adverse slippage. Existing chase/stop/economic/holiday guards retained.',
            PULLBACK='Market-on-pullback to warning OPEN/CLOSE midpoint, grid rounded toward warning close. Must be >=one tick better than close in direction, else no order. No previous breakout required. Target hit before entry cancels. True OHLC trade disagreements remain unknown.'),
        groups=dict(BASE='All score>=2.5 ABC seeds, existing positive A-body gate retained.',
            FRESH='0<=direction*(warningclose-Btrue)/(direction*(S1-Btrue))<=.5.',
            ORDERLY='EntireB retracement/A in [.25,.75], trueB speed/A speed<=1.',
            CLEAN='A body efficiency>=.5, favorable session gap share<=.25, largest absolute body/A<=.5.',
            HOUR='Available warning-known trend_alignment_60>0.',FIRST='First qualified seed per direction/A cluster, chosen before entry economics, fill or outcome.'),
        design='6 early variants plus2 pullback variants, no threshold grid/conjunction search. Original S1 target and entireB stop frozen; same shared economics, actual1m replay, all8cost/execution/unknown cases. Two old controls. FIRST only among existing scoring-window qualified seeds, not claim of first-ever lifetime warning.',
        path_diagnostic='16 complete 15m bars after parent warning, same warning close and ATR14 scaling for every group; all qualified seeds including those failing order economics. No path labels used to filter orders.',
        selection=parent['validation'],supplement=dict(start='2026-07-13 00:00:00',end='2026-07-25 00:00:00',contract='DCE.jd2608',
            definition='Fixed contract already active asof July10, real1m ->complete5m only. June12 onward warmup, score July13-24; tail through July28. Choose strategy atJuly13 using only strict closed preJuly13 MAIN rows, before generating supplementary warnings/executions. Candidate portfolios carry orders from preJuly13 without reset; evaluate warnings in supplementary window. No tuning on supplement.',
            limitation='New scoring window from existing archive, not untouched OOS. Fixed contract differs from continuous main afterJuly14. Only10scheduled days, possible unknowns and sparse signals.'),
        limitation='Sequential exploratory research on repeatedly used JD history; multiple comparisons, no fresh OOS significance. No production strategy promotion or account ROI claims. No new data invented.',
        production_before=parent['production_before'],script_sha256=sha(__file__))
    write('protocol.json',p)
    fork=RUN/'fork';shutil.copytree(b.RUN/'fork',fork)
    path=fork/'crates/n-core/src/v3/ablation.rs';src=path.read_text(encoding='utf-8')
    src=replace(src,'    SignalNextOpen,','    SignalNextOpen,\n    WarningPullback,')
    src=replace(src,'                EntryMode::SignalNextOpen => { self.enter(price, ev, ex, sign, ts); }','''                EntryMode::SignalNextOpen => { self.enter(price, ev, ex, sign, ts); }
                EntryMode::WarningPullback => {
                    if (price-ev.entry)*sign<=0. {self.enter(price,ev,ex,sign,ts);}
                    else if (price-ev.target)*sign>=0. {self.finish("missed_target",ts);}
                }''')
    src=replace(src,'                EntryMode::CloseConfirmNextOpen | EntryMode::SignalNextOpen => {','''                EntryMode::WarningPullback => {
                    if !favorable && (to-ev.entry)*sign<=0. {self.enter(ev.entry,ev,ex,sign,ts);}
                    else if favorable && (to-ev.target)*sign>=0. {self.finish("missed_target",ts);}
                }
                EntryMode::CloseConfirmNextOpen | EntryMode::SignalNextOpen => {''')
    tests='''
    #[test]fn warning_pullback_enters_adverse_cross_without_prior_breakout_both_directions(){
        for short in [false,true]{let sign=if short{-1.}else{1.};let mut e=event(short,3.);e.entry=100.;
        let r=research(&e,&[if short{bar(46,99.,101.,98.,100.)}else{bar(46,101.,102.,99.,100.)}],ResearchRules{entry_mode:EntryMode::WarningPullback,allow_peak_only_agreement:true,..Default::default()});
        assert_eq!(r.evaluation.fill,Some(100.));assert_eq!(r.evaluation.entry_ts.as_deref(),Some("2026-01-05 09:46:00"));assert!(sign*(e.target-100.)>0.);}
    }
    #[test]fn warning_pullback_never_fills_above_pullback_or_after_target(){
        let e=event(false,3.);let rules=ResearchRules{entry_mode:EntryMode::WarningPullback,allow_peak_only_agreement:true,..Default::default()};
        let r=research(&e,&[bar(46,102.,105.,101.,104.)],rules);assert!(r.evaluation.fill.is_none());
        let r=research(&e,&[bar(46,102.,131.,101.,130.)],rules);assert_eq!(r.evaluation.status,"missed_target");assert!(r.evaluation.fill.is_none());
    }
    #[test]fn warning_pullback_keeps_stop_gap_and_true_target_entry_ambiguity(){
        let rules=ResearchRules{entry_mode:EntryMode::WarningPullback,allow_peak_only_agreement:true,..Default::default()};
        let r=research(&event(false,3.),&[bar(46,89.,101.,88.,100.)],rules);assert_eq!(r.evaluation.status,"invalidated");assert!(r.evaluation.fill.is_none());
        let r=research(&event(false,3.),&[bar(46,102.,131.,99.,105.)],rules);assert_eq!(r.evaluation.status,"ambiguous");
    }
'''
    src=replace(src,'mod tests {\n    use super::*;','mod tests {\n    use super::*;\n'+tests);path.write_text(src,encoding='utf-8')
    source=fork/'crates/n-core/src/bin/swing_jd_abc_execution.rs';target=source.with_name('swing_jd_warning_quality.rs')
    target.write_text(source.read_text(encoding='utf-8'),encoding='utf-8')
    write('implementation-manifest.json',{str(q.relative_to(fork)):sha(q) for q in fork.rglob('*') if q.is_file()})

def build():
    args=['--release','--offline','--manifest-path',str(RUN/'fork/Cargo.toml'),'--target-dir',str(b.ROOT/'target/swing-jd-warning-quality-isolated'),'-p','n-core']
    for filt in ['warning_pullback_','signal_open_','peak_only_']:
        subprocess.run(['cargo','test',*args,'--lib',filt],check=True)
    subprocess.run(['cargo','test',*args,'--bin','swing_jd_warning_quality'],check=True)
    subprocess.run(['cargo','build',*args,'--bin','swing_jd_warning_quality'],check=True)
    path=b.ROOT/'target/swing-jd-warning-quality-isolated/release/swing_jd_warning_quality.exe';write('binary.json',dict(path=str(path),sha256=sha(path)))

def pools(period,seeds,paths,meta,seen):
    p=R(RUN/'protocol.json');folders=[];counts=collections.Counter()
    for model in NEW:
        dest=RUN/period/model;dest.mkdir(parents=True);pool=[]
        for s in seeds:
            key=s['event_key'];lab=meta[key]['groups'];group=model.split('_')[1]
            if not lab[group]:continue
            t=copy.deepcopy(s);e=t['event'];sign=1 if e['direction']=='up' else -1;w=t['warning_candle']
            entry=w['close'] if model.startswith('EARLY') else b.z.grid((w['open']+w['close'])/2,e['tick_size'],sign>0)
            if model.startswith('PULLBACK') and (w['close']-entry)*sign<e['tick_size']-1e-8:continue
            e.update(entry=entry,trigger_level=entry,risk=(entry-e['stop'])*sign)
            pool.append(t)
        b.z.s.m.old.write(dest/(period+'-pool.json'),pool)
        b.z.s.m.old.write(dest/'execution.protocol.json',dict(templates=[p['templates'][model]],modes=['corrected_touch','next_minute_open'],periods={period:paths}))
        folders.append(dest);counts[model]=len(pool)
    return folders,dict(counts)

def seed_meta(seeds,period,segments,meta,seen):
    for s in sorted(seeds,key=lambda s:(s['event']['warning_ts'],s['event_key'])):
        e=s['event'];key=s['event_key'];cl='|'.join(e[k] for k in ['symbol','direction','s0_ts','s1_ts'])
        lab=labels(s,cl not in seen);seen.add(cl)
        meta[key]=dict(parent_key=key,cluster=cl,seed_ts=e['warning_ts'],warning_ts=e['warning_ts'],period=period,
            contract=next(q['underlying'] for q in segments if q['first']<=e['warning_ts']<=q['last']),groups=lab)

def run_native(folders,period):
    exe=R(RUN/'binary.json');assert sha(exe['path'])==exe['sha256']
    for d in folders:subprocess.run([exe['path'],str(d/'execution.protocol.json'),period,'replay',str(d/'executions')],check=True)

def main_replay():
    pp=R(b.ROOT/'research-strategies/best-a-portfolio-observation-v1/protocol.json');meta={};seen=set();counts={}
    for period in ['early','recent']:
        seeds=R(b.RUN/period/'seed'/(period+'-pool.json'));paths=R(b.RUN/period/'seed/execution.protocol.json')['periods'][period]
        seed_meta(seeds,period,R(paths['segments'])['JD0'],meta,seen)
        folders,counts[period]=pools(period,seeds,paths,meta,seen);run_native(folders,period)
    write('signal-metadata.json',meta);write('generation-checks.json',counts)

def load(periods=('early','recent')):
    c,oldmeta,olddata,ends,_,checks,_=b.load();meta=R(RUN/'signal-metadata.json');data=collections.defaultdict(list);fees=R(b.ROOT/'scripts/exchange_fees_20261022.json')
    for (m,sf),rows in olddata.items():
        if m in MODELS[-2:]:data[m,sf]=rows
    for period in periods:
        dates=R(b.RUN/period/'seed/trading-dates.json') if period!='supplement' else R(RUN/'supplement/trading-dates.json')
        cal=b.z.s.TradingDates({});cal.dates=dates
        for model in NEW+(MODELS[-2:] if period=='supplement' else []):
            dest=RUN/period/model
            for code,end in R(dest/'executions/manifest.json')['contract_ends'].items():ends[code]=max(ends.get(code,''),end)
            for line in (dest/'executions/evaluations.jsonl').open(encoding='utf-8'):
                row=b.z.s.reprice(json.loads(line),fees,cal);row['period']=period;m,sf=row['scenario'].split('/',1);data[m,sf].append(row)
    return c,meta,data,ends

def cases(p):return [pol+'/'+sf for pol in p['policies'] for sf in p['scenarios']]

def evaluate(c,meta,data,ends,p,cutoff=None):
    accepted={};fixed={}
    for case in cases(p):
        pol,sf=case.split('/',1)
        for model in MODELS:
            rows=data[model,sf]
            if cutoff:rows=[r for r in rows if r['warning_ts']<cutoff]
            aa,ll=b.admit(rows,ends,pol,c,meta);accepted[case,model]=aa
            q=b.z.metric(aa);q['blocks']={f['label']:b.z.metric([r for r in aa if f['start']<=r['warning_ts']<f['end']]) for f in b.z.x.blocks()};fixed[case+'/'+model]=q
    return accepted,fixed

def select(accepted,cutoff,p):
    dates=R(b.RUN/'generation-checks.json')['covered_dates'];nday=sum(d<cutoff[:10] for d in dates);visible={};eligible=[];rejected={}
    for m in MODELS:visible[m]={case:b.z.metric(b.z.x.visible(accepted[case,m],cutoff)) for case in cases(p)}
    for m in NEW:
        zs=visible[m];q=zs[p['primary']];why=[]
        if q['n']<20 or q['n']/nday<.35:why.append('train_frequency')
        if min(z['normalized_net'] for z in zs.values())<=0:why.append('stress_nonpositive')
        if q['normalized_ex_best']<=0:why.append('ex_best_nonpositive')
        if any(v['normalized_net']<visible['E20S1'][case]['normalized_net']-1e-7 for case,v in zs.items()):why.append('below_reference')
        if why:rejected[m]=why
        else:eligible.append((min(z['normalized_net'] for z in zs.values())-.25*max(z['normalized_realized_dd'] for z in zs.values()),m))
    eligible.sort(key=lambda z:(-round(z[0],8),NEW.index(z[1])))
    return dict(cutoff=cutoff,chosen=eligible[0][1] if eligible else 'E20S1',eligible=eligible,rejected=rejected,visible_metrics=visible)

def analyze_main():
    p=R(RUN/'protocol.json');assert p['script_sha256']==sha(__file__);c,meta,data,ends=load();aa,fixed=evaluate(c,meta,data,ends,p)
    schedule=[dict(select(aa,start,p),start=start,end=end) for start,end in b.FOLDS]
    # Freeze supplementary decision BEFORE generating or replaying supplementary warnings.
    pre,_=evaluate(c,meta,data,ends,p,'2026-07-13 00:00:00');decision=select(pre,'2026-07-13 00:00:00',p)
    write('supplement-decision-before-test.json',decision);write('selection-before-test.json',schedule);write('fixed-results.json',fixed)
    wf={}
    for case in cases(p):
        pol,sf=case.split('/',1);rows=[]
        for m in MODELS:
            rows.extend(r for r in data[m,sf] if next((f['chosen'] for f in schedule if f['start']<=r['warning_ts']<f['end']),'E20S1')==m)
        accepted,_=b.admit(rows,ends,pol,c,meta);wf[case]=b.z.metric([r for r in accepted if r['warning_ts']>=b.FOLDS[0][0]])
    write('walkforward-results.json',wf)
    with (RUN/'accepted-primary.jsonl').open('x',encoding='utf-8') as out:
        for m in MODELS:
            for r in aa[p['primary'],m]:out.write(json.dumps(dict(model=m,row=r),ensure_ascii=False)+'\n')
    write('main-checks.json',dict(old_control_rows_verified=1760,
        production_unchanged=all(sha(b.ROOT/q)==h for q,h in p['production_before'].items()),supplement_not_yet_generated=not (RUN/'supplement').exists()))
    print(json.dumps({m:{k:fixed[p['primary']+'/'+m][k] for k in ['n','net','unknown']} for m in MODELS}),flush=True)

def paths_diagnostic():
    pp=R(b.ROOT/'research-strategies/best-a-portfolio-observation-v1/protocol.json');meta=R(RUN/'signal-metadata.json');out=[]
    for period in ['early','recent']:
        prices=b.a.Prices(pp['periods'][period])
        for s in R(b.RUN/period/'seed'/(period+'-pool.json')):
            e=s['event'];code=meta[s['event_key']]['contract'];path=prices.path(code,e['warning_ts'],16);raw,complete=b.a.window(path,16)
            atr=s['atr14'];sign=1 if e['direction']=='up' else -1;anchor=s['warning_candle']['close']
            q=dict(key=s['event_key'],warning_ts=e['warning_ts'],groups=meta[s['event_key']]['groups'],complete=complete,quality=path['quality'])
            if complete:q.update(b.a.excursion(raw,anchor,atr,sign),race=b.a.barrier(raw,anchor,atr,sign)[0])
            out.append(q)
    groups={}
    for g in GROUPS:
        allrows=[r for r in out if r['groups'][g]];rows=[r for r in allrows if r['complete']]
        groups[g]=dict(total=len(allrows),complete=len(rows),mean_close_ATR=st.mean(r['end_r'] for r in rows) if rows else None,
            mean_MFE_ATR=st.mean(r['mfe'] for r in rows) if rows else None,mean_MAE_ATR=st.mean(r['mae'] for r in rows) if rows else None,
            race=dict(collections.Counter(r['race'] for r in rows)))
    write('warning-path-diagnostic.json',dict(groups=groups,rows=out))

def supplement():
    p=R(RUN/'protocol.json');decision=R(RUN/'supplement-decision-before-test.json');assert decision['cutoff']=='2026-07-13 00:00:00'
    dest=RUN/'supplement';dest.mkdir(exist_ok=False);pp=R(b.ROOT/'research-strategies/best-a-portfolio-observation-v1/protocol.json');paths=pp['periods']['early'];inp=R(paths['input'])
    code=p['supplement']['contract'];raw=R(paths['contracts'])[code]['minutes'];by=collections.defaultdict(list)
    for q in raw:
        t=dt.datetime.fromisoformat(q['datetime']);end=t+dt.timedelta(minutes=(-t.minute)%5);by[end.isoformat(sep=' ')].append(q)
    bars=[];incomplete=0
    for end,qs in sorted(by.items()):
        endt=dt.datetime.fromisoformat(end);wanted=[(endt-dt.timedelta(minutes=j)).isoformat(sep=' ') for j in range(4,-1,-1)]
        if [q['datetime'] for q in qs]!=wanted:incomplete+=1;continue
        bars.append(dict(datetime=end,open=qs[0]['open'],high=max(q['high'] for q in qs),low=min(q['low'] for q in qs),close=qs[-1]['close'],volume=sum(q['volume'] for q in qs),hold=qs[-1]['hold']))
    spec=copy.deepcopy(inp['symbols']['JD0']);spec.update(bars=bars,rollovers=[]);full=dict(execution_config=inp['execution_config'],symbols={'JD0':spec})
    write('supplement/input.json',full);write('supplement/contracts.json',{code:dict(minutes=raw)})
    segments=[dict(first=raw[0]['datetime'],last=raw[-1]['datetime'],underlying=code)]
    write('supplement/segments.json',dict(JD0=segments));write('supplement/trading-dates.json',sorted({q['datetime'][:10] for q in raw}))
    newpaths=dict(paths,input=str(dest/'input.json'),contracts=str(dest/'contracts.json'),segments=str(dest/'segments.json'))
    gen=R(b.a.RUN/'generation.protocol.json');gen['periods']['supplement']=newpaths;gen['hashes']['supplement']={'input':sha(dest/'input.json')};write('supplement/generation.protocol.json',gen)
    exe=R(b.a.RUN/'generator-binary-v2.json');assert sha(exe['path'])==exe['sha256']
    subprocess.run([exe['path'],str(dest/'generation.protocol.json'),'supplement',str(dest/'symbols'),str(dest/'all-pool.json')],check=True)
    seeds=[];bs=b.z.x.identification_bars(bars)
    for s in R(dest/'all-pool.json'):
        e=s['event']
        if not(p['supplement']['start']<=e['warning_ts']<p['supplement']['end']) or e['setup_features']['setup_quality']<2.5:continue
        f=b.warning_features(e,bs);s['entry_quality']=f;sign=1 if e['direction']=='up' else -1
        s['mechanism']=dict(true_b_stop=f['b_true_extreme']-sign*((e['s2_price']-e['stop'])*sign));seeds.append(s)
    write('supplement/qualified-seeds.json',seeds)
    meta=R(RUN/'signal-metadata.json');seen={m['cluster'] for m in meta.values() if m['warning_ts']<decision['cutoff']}
    seed_meta(seeds,'supplement',segments,meta,seen);write('signal-metadata-with-supplement.json',meta)
    folders,counts=pools('supplement',seeds,newpaths,meta,seen)
    for model in MODELS[-2:]:
        d=dest/model;d.mkdir();write('supplement/'+model+'/supplement-pool.json',seeds)
        write('supplement/'+model+'/execution.protocol.json',dict(templates=[p['templates'][model]],modes=['corrected_touch','next_minute_open'],periods={'supplement':newpaths}));folders.append(d)
    # Compare regenerated fixed-contract warning geometry where source was still continuous-main.
    overlap={s['event_key']:s for s in R(dest/'all-pool.json') if '2026-07-01'<=s['event']['warning_ts']<='2026-07-10 15:00:00' and s['event']['setup_features']['setup_quality']>=2.5}
    old={s['event_key']:s for s in R(b.RUN/'early/seed/early-pool.json') if s['event']['warning_ts'].startswith('2026-07')}
    common=set(overlap)&set(old);exact=sum(all(overlap[k]['event'][f]==old[k]['event'][f] for f in ['entry','stop','target','s0_price','s1_price','s2_price']) for k in common)
    write('supplement/data-checks.json',dict(five_minute_bars=len(bars),incomplete_groups_dropped=incomplete,qualified_seeds=len(seeds),counts=counts,
        overlap_old=len(old),overlap_new=len(overlap),overlap_common=len(common),overlap_exact_geometry=exact,
        scored_zero_volume_minutes=sum(q['volume']<=0 for q in raw if p['supplement']['start']<=q['datetime']<p['supplement']['end']),
        score_days=sorted({q['datetime'][:10] for q in raw if p['supplement']['start']<=q['datetime']<p['supplement']['end']}),
        decision_sha256=sha(RUN/'supplement-decision-before-test.json'),raw_source_sha256=sha(paths['contracts'])))
    run_native(folders,'supplement')
    c,_,data,ends=load(('early','recent','supplement'));results={}
    for case in cases(p):
        pol,sf=case.split('/',1)
        for m in MODELS:
            rows=[r for r in data[m,sf] if r['warning_ts']<p['supplement']['end']]
            aa,_=b.admit(rows,ends,pol,c,meta);results[case+'/'+m]=b.z.metric([r for r in aa if p['supplement']['start']<=r['warning_ts']<p['supplement']['end']])
    write('supplement/fixed-results.json',results)
    print('supplement',len(seeds),'seeds',json.dumps({m:results[p['primary']+'/'+m]['net'] for m in MODELS}),flush=True)

class Tests(unittest.TestCase):
    def test_fresh_midpoint_and_causality(self):
        s=dict(event=dict(direction='up',target=120.,a_move=40.,warning_ts='2026-01-01 10:00:00',market_context=None),warning_candle=dict(close=110.),entry_quality=dict(as_of='2026-01-01 10:00:00',b_true_extreme=100.,b_true_speed_to_A=1.,a_body_eff_ex_s0=.5,a_positive_session_gap_share=.25,a_largest_abs_body_share=.5))
        self.assertTrue(labels(s,True)['FRESH']);self.assertTrue(labels(s,True)['CLEAN']);s['warning_candle']['close']=111.;self.assertFalse(labels(s,False)['FRESH']);self.assertFalse(labels(s,False)['FIRST'])
        s['entry_quality']['as_of']='2026-01-02 10:00:00'
        with self.assertRaises(AssertionError):labels(s,True)

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('stage',choices=['test','prepare','build','main_replay','analyze_main','paths_diagnostic','supplement']);args=ap.parse_args()
    if args.stage=='test':unittest.main(argv=[__file__])
    else:globals()[args.stage]()
