"""Frozen V4 structural prototypes and event-geometry matched mechanical controls."""
import argparse
import collections
import datetime as dt
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import traceback

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'.s1-deps'))
import numpy as np
import pandas as pd
import pyarrow as pa
import explore_v4_logic as logic
import explore_v3 as prior
import explore_v3_signals as features
import s3_signals as prep
import s3_alpha as stats
import run_s3_alpha as old
import s2_preregister as reg
from factor_study_data import Calendar,specifications,digest
from s1_market_regimes import select_main,fee_rule

BASE=ROOT/'artifacts/exploration_v4'
SOURCE=prior.SOURCE
PERIODS=prior.PERIODS


def protocol():
    return dict(version='4.0-EXP',purpose='refine V3 quiet-context rejection and test a fixed structural payoff without tuning exits',
        prior_feedback='V1/V2/V3 results viewed; prior failures unchanged; historical diagnostic only',source=str(SOURCE.relative_to(ROOT)),
        periods=PERIODS,never_read_new_strategy_outcomes_after='2025-11-01',universe=sorted(sum(features.GROUPS.values(),[])),
        grid=dict(peer_state=['quiet','divergent'],range_bars=[12,24],confirmation_bars=[1,2]),
        peer_definition=dict(minimum_other_peers=2,exact_timestamp=True,self_exclusion=True,no_ffill=True,
            setup_state='immediately preceding5m bar; require its end==setup start and same natural day',
            quiet='abs(peer displacement median)<=1 and median(abs(displacements))<=1',
            divergent='abs(peer displacement median)<=1, median(abs(displacements))>=2 and peer MAD>=1',
            displacement='since observed09:00 open / own currentATR12; fixed V3 research groups'),
        signal='priorER12<=.4, fake breakout >=.25 previousATR beyond previous12/24 range, reverse candle closes inside; double-sided excursion rejected',
        confirmation='1=setup close; 2=exactly next activebar holds inside, same reversal candle/close direction, never reaches target or stop first; otherwise cancel, no retries',
        common=dict(only_day=True,overnight=False,last_entry_before='14:50',cost_gate_R=.25,cooldown_active_bars=24),
        prototype=dict(target='midpoint of prior range known at setup, rounded toward smaller reward',stop='rejection extreme plus1 adverse tick',
            maximum_holding_active_bars=12,force_flat='known daytime close15:00',entry='next observed complete bar open plus1 adverse tick',
            exit='one adverse tick, gaps at adverse open, gap target conservative target, samebar stop first',
            one_lot=True,trade_budget_cny=400,max_lot_risk_cny=600,
            risk='forecast stop loss with adverse exit tick and both fees, recomputed at actual entry; skip unaffordable, never tighten stop',
            excluded='missing/unknown path outcomes are null, not zero; unresolved exposures retained',
            accounting='single-event one-lot reference only, no shared equity/margin simulation',
            gap_losses='never clipped, actual losses over600 separately counted'),
        primary='date-equal net prototype return in signalATR lot value units and matched incremental advantage; NOT raw12bar mark return',
        secondary='unchanged raw6/12/24 forward labels retained, no best-horizon/exit selection',
        controls='same product/4-monthblock/5mclock/side, V3 ATR/activity calipers; project event signal-time normalized stop/target geometry onto control signal-time reference, not event future fill; require same one-lot400 budget and complete path, never select by profit',
        inference=dict(matching=200,seed_matching=20261110,bootstrap=1000,seed_bootstrap=20261111,Holm_family=8,
            minimum_events=60,minimum_days=30,minimum_coverage=.8,CI=.95,
            stability='>=2 of3 chronological4-month blocks positive with >=10 events',
            period_pass='positive date-equal net, positive incremental effect and95% lower bound, Holm<=.05, coverage/sample/stability',
            cycle_pass='same prototype passes both D1,D2 unchanged, still not independent evidence'),
        budget=dict(templates=8,periods=2,maximum_candidate_period_evaluations=16,full_WFO_evaluations=0,exit_search_parameters=0),
        changes_from_V3='new payoff contract, quiet/divergent distinction and1/2bar confirmation preregistered; cannot retroactively turn V3 failure into a pass')


def source_files():
    return [Path(__file__),ROOT/'scripts/explore_v4_logic.py',ROOT/'tests/test_explore_v4.py',ROOT/'tests/test_s3_alpha.py',
        ROOT/'scripts/explore_v3.py',ROOT/'scripts/explore_v3_signals.py',ROOT/'scripts/explore_v2_signals.py',ROOT/'scripts/s3_alpha.py',
        ROOT/'scripts/s3_signals.py',ROOT/'scripts/run_s3_alpha.py',ROOT/'scripts/s1_market_regimes.py',ROOT/'scripts/factor_study_data.py',ROOT/'crates/n-core/src/precision.rs']


def freeze():
    BASE.mkdir(exist_ok=True,parents=True)
    if (BASE/'freeze.json').exists():raise ValueError('already frozen')
    prior.verify();p=protocol();cs=logic.templates(p['universe'])
    assert len(cs)==8
    for c in cs:c['candidate_sha256']=reg.canonical(c)
    reg.write_json(BASE/'protocol.json',p);reg.write_json(BASE/'candidate_manifest.json',dict(candidates=cs))
    t=subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-p','test_explore_v4.py','-v'],cwd=ROOT,capture_output=True,text=True)
    (BASE/'tests.txt').write_text(t.stdout+t.stderr,encoding='utf8')
    if t.returncode:raise ValueError('V4 tests failed')
    previous=ROOT/json.loads((prior.BASE/'latest_run.json').read_text(encoding='utf8'))['path']
    h=json.loads((prior.BASE/'freeze.json').read_text(encoding='utf8'))['source_sha256']
    if digest(SOURCE)!=h:raise ValueError('source changed')
    reg.write_json(BASE/'freeze.json',dict(version='4.0-EXP',created_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
        git_sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),source_sha256=h,
        code_sha256={str(p.relative_to(ROOT)):digest(p) for p in source_files()},
        input_sha256={str(p.relative_to(ROOT)):digest(p) for p in [BASE/'protocol.json',BASE/'candidate_manifest.json',BASE/'development_notes.md',
            previous/'evidence.csv',previous/'verification.json',ROOT/'data/trading_calendar.json',ROOT/'scripts/factor-study-v1.json',ROOT/'scripts/exchange_fees_20261022.json']},
        prior_feedback_acknowledged=True,V4_returns_seen_before_freeze=0))
    print('V4 frozen:8 fixed-exit prototypes, 9 regression methods passed',flush=True)


def verify():
    seal=json.loads((BASE/'freeze.json').read_text(encoding='utf8'))
    for section in ('code_sha256','input_sha256'):
        for name,h in seal[section].items():
            if digest(ROOT/name)!=h:raise ValueError('frozen source changed:'+name)
    return seal


def schema():
    s=old.event_schema()
    for n in ('parent_event_id','base_control_id','peer_state','trade_status','trade_reason'):s=s.append(pa.field(n,pa.string()))
    for n in ('peer_median','peer_abs_median','peer_mad','activity_ratio','atr_pct_value','friction_R','target_price','forecast_stop_R','forecast_target_R',
              'trade_entry','trade_exit','trade_stop','trade_target','trade_net_R','trade_net_cny','trade_fees_cny','trade_plan_risk_cny'):s=s.append(pa.field(n,pa.float64()))
    for n in ('setup_ns','trade_entry_ns','trade_exit_ns'):s=s.append(pa.field(n,pa.int64()))
    for n in ('trade_actual_loss_over_600','trade_ambiguous'):s=s.append(pa.field(n,pa.bool_()))
    return s


def annotate(r,a,i,period):
    r['discovery_block']=prior.block(r['day'],period['discovery_start'])
    for n in ('peer_median','peer_abs_median','peer_mad','activity_ratio','friction_R'):r[n]=float(a[n][i])
    r['atr_pct_value']=float(a['atr_pct'][i]);r['stratum']=(r['product'],r['discovery_block'],r['minute'],r['side'])
    r['stratum_key']=json.dumps(r['stratum']);return r


def match_projected(events,pool,arrays,specs,fees,hi,sink):
    projected=[];choices={};cache_count=collections.Counter()
    for i,e in enumerate(events):
        accepted=[]
        for j in pool['by_stratum'].get(e['stratum'],[]):
            c=pool['rows'][j]
            ratio=e['atr_pct_value']/c['atr_pct_value'];activity=e['activity_ratio']/c['activity_ratio']
            if not(.67<=ratio<=1.5 and .5<=activity<=2.):continue
            name=c['product'];a=arrays[name];spec=specs[name+'0'];stop,target=logic.projection(e,a,c['index'],spec)
            result=logic.trade(a,c['index'],c['side'],stop,target,spec,fees[name],hi)
            row={**c,**result,'event_id':c['event_id']+'|for|'+e['event_id'],'parent_event_id':e['event_id'],'base_control_id':c['event_id'],
                 'stop':float(stop),'target_price':float(target),'forecast_stop_R':e['forecast_stop_R'],'forecast_target_R':e['forecast_target_R'],'source_event':'projected_control'}
            cache_count[result['trade_status']+':'+str(result['trade_reason'])]+=1
            sink.add([row])
            if result['trade_status']=='complete':accepted.append(len(projected));projected.append(row)
        choices[i]=accepted
    rng=np.random.default_rng(20261110);matches=np.full((200,len(events)),-1,dtype=np.int32)
    groups=collections.defaultdict(list)
    for i,e in enumerate(events):groups[e['stratum']].append(i)
    for rep in range(200):
        used=set()
        for _,indices in sorted(groups.items()):
            for i in rng.permutation(indices):
                available=[j for j in choices[int(i)] if projected[j]['time_key'] not in used]
                if available:
                    j=int(rng.choice(available));matches[rep,int(i)]=j;used.add(projected[j]['time_key'])
    return matches,projected,dict(cache_count)


def run():
    seal=verify()
    if digest(SOURCE)!=seal['source_sha256']:raise ValueError('source changed')
    out=BASE/('run_'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ'));out.mkdir(exist_ok=False)
    meta=json.loads((ROOT/'scripts/factor-study-v1.json').read_text(encoding='utf8'));specs=specifications(meta)
    calendar=Calendar(json.loads((ROOT/'data/trading_calendar.json').read_text(encoding='utf8')),meta['night_ends'])
    snapshot=json.loads((ROOT/'scripts/exchange_fees_20261022.json').read_text(encoding='utf8'));cs=json.loads((BASE/'candidate_manifest.json').read_text(encoding='utf8'))['candidates']
    sinks={n:old.Sink(out/(n+'.parquet'),schema()) for n in ('events','control_pool','control_trades')};pairs=old.Sink(out/'matched_baseline.parquet',old.MATCH_SCHEMA)
    db=sqlite3.connect(SOURCE.resolve().as_uri()+'?mode=ro',uri=True);db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
    all_results=[];audit=[];prefixes=[];boots=[];exposures=[]
    try:
        for period in PERIODS:
            print('V4 PERIOD',period,flush=True);lo=int(pd.Timestamp(period['discovery_start'],tz='Asia/Shanghai').value);hi=int(pd.Timestamp(period['discovery_end'],tz='Asia/Shanghai').value)
            maps=pd.read_sql_query('select product,effective_date,contract from mappings where effective_date<? order by product,effective_date',db,params=(period['discovery_end'],))
            frames={};fee_table={}
            for product,mp in maps.groupby('product',sort=True):
                name=product.split('.')[1].upper();parts=[]
                for contract in mp.contract.unique():
                    f=pd.read_sql_query('select '+','.join(old.RAW)+' from bars where contract=? and period_seconds=300 and start_ns>=? and end_ns<=? order by start_ns',db,params=(contract,lo,hi))
                    audit.append(dict(period=period['fold_id'],product=name,contract=contract,start=period['discovery_start'],end_exclusive=period['discovery_end'],rows=len(f)))
                    if len(f):parts.append(f)
                if not parts:continue
                fee_table[name]=fee_rule(snapshot,name);selected=select_main(pd.concat(parts,ignore_index=True),mp)
                frames[name]=features.features(prep.prepare(selected,calendar),specs[name+'0'],fee_table[name])
            frames=logic.peers(frames);arrays={n:{k:f[k].to_numpy() for k in f} for n,f in frames.items()}
            short=None
            if period['fold_id']=='D1':
                cut=int(pd.Timestamp('2024-05-08 11:00',tz='Asia/Shanghai').value)
                short=logic.peers({n:features.features(prep.prepare(f.loc[f.end_ns<=cut,old.RAW],calendar),specs[n+'0'],fee_table[n]) for n,f in frames.items()})
            by_candidate={c['candidate_id']:[] for c in cs};pool=[];strata=collections.defaultdict(list)
            for name,f in sorted(frames.items()):
                a=arrays[name];spec=specs[name+'0'];fee=fee_table[name];generated={c['candidate_id']:logic.signals(f,c,spec['tick']) for c in cs}
                if short is not None and name in ('C','JD','PB'):
                    n=len(short[name]);pd.testing.assert_frame_equal(f.iloc[:n],short[name])
                    for c in cs:assert logic.signals(short[name],c,spec['tick'])==[q for q in generated[c['candidate_id']] if q['index']<n]
                    prefixes.append(dict(product=name,rows=n,templates=8,peer_and_signal_prefix_passed=True))
                union={int(a['end_ns'][q['index']]) for e in generated.values() for q in e};needed=set();count=0
                for c in cs:
                    rows=[]
                    for q in generated[c['candidate_id']]:
                        i=q['index'];r=old.record(period,c,name,q,a,spec,fee,hi);annotate(r,a,i,period)
                        reference=a['close'][i]+q['side']*spec['tick'];R=a['atr'][i]
                        r.update(target_price=q['target'],forecast_stop_R=float((reference-q['stop'])*q['side']/R),
                            forecast_target_R=float((q['target']-reference)*q['side']/R),peer_state=q['peer_state'],setup_ns=int(a['end_ns'][q['setup_index']]))
                        r.update(logic.trade(a,i,q['side'],q['stop'],q['target'],spec,fee,hi));rows.append(r)
                        if r['trade_status']=='complete':needed.add(r['stratum'])
                    sinks['events'].add(rows);by_candidate[c['candidate_id']].extend(rows);count+=len(rows)
                control_rows=[];base=dict(candidate_id='CONTROL',hypothesis_id='BASELINE')
                for i in range(len(f)):
                    if not a['valid'][i] or not a['economic_gate'][i] or not a['activity_known'][i] or not np.isfinite(a['atr'][i]) or a['atr'][i]<=0 or a['activity_ratio'][i]<=0 or a['minute'][i]+5>=890 or int(a['end_ns'][i]) in union:continue
                    for side in (1,-1):
                        key=(name,prior.block(str(a['day'][i]),period['discovery_start']),int(a['minute'][i]),side)
                        if key not in needed:continue
                        r=old.record(period,base,name,dict(index=i,side=side),a,spec,fee,hi,'generic_control');annotate(r,a,i,period)
                        strata[key].append(len(pool));pool.append(r);control_rows.append(r)
                sinks['control_pool'].add(control_rows);print(period['fold_id'],name,'setups',count,'controls',len(control_rows),flush=True)
            current=[]
            for c in cs:
                cid=c['candidate_id'];raw=by_candidate[cid];e=[q for q in raw if q['trade_status']=='complete']
                matches,controls,control_status=match_projected(e,dict(rows=pool,by_stratum=strata),arrays,specs,fee_table,hi,sinks['control_trades'])
                estat=[{**q,'net_R_12':q['trade_net_R']} for q in e];cstat=[{**q,'net_R_12':q['trade_net_R']} for q in controls]
                r,boot=stats.inference(estat,cstat,matches,1000,20261111) if e else (dict(effect=None,ci_lower=None,ci_upper=None,p_value=1.,signal_net_R=None,matched_fraction=0.,matched_days=0,positive_blocks=0,product_effects={}),[])
                r.update(fold_id=period['fold_id'],candidate_id=cid,hypothesis_id='Q',parameters=c['parameters'],raw_setups=len(raw),complete_primary=len(e),
                    raw_date_equal_trade_R=old.finite_mean([float(np.mean(v)) for v in pd.DataFrame(e).groupby('day').trade_net_R.apply(list)]) if e else None,
                    prototype_net_cny_sum=float(sum(q['trade_net_cny'] for q in e)),worst_trade_cny=float(min((q['trade_net_cny'] for q in e),default=0)),
                    actual_losses_over_600=sum(q['trade_actual_loss_over_600'] for q in e),
                    setup_outcomes=dict(collections.Counter(q['trade_status']+':'+str(q['trade_reason']) for q in raw)),control_projection_outcomes=control_status)
                for rep,row in enumerate(matches):
                    pairs.add([dict(fold_id=period['fold_id'],candidate_id=cid,event_id=e[int(i)]['event_id'],control_id=controls[int(row[i])]['event_id'],
                        event_day=e[int(i)]['day'],control_day=controls[int(row[i])]['day'],replicate=rep,
                        primary_delta_R=e[int(i)]['trade_net_R']-controls[int(row[i])]['trade_net_R']) for i in np.flatnonzero(row>=0)])
                boots.extend(dict(period=period['fold_id'],candidate_id=cid,replicate=i,effect_R=x) for i,x in enumerate(boot));current.append(r)
                print('V4 PROTOTYPE',period['fold_id'],cid,'setups',len(raw),'scorable',len(e),'raw_day_net',r['raw_date_equal_trade_R'],'matched_net',r['signal_net_R'],flush=True)
            adjusted=stats.holm([r['p_value'] for r in current]);gate=dict(s3_design=dict(decision=dict(minimum_events=60,minimum_independent_days=30,minimum_matched_fraction=.8)))
            for r,p in zip(current,adjusted):r['holm_p']=float(p);r['decision']=old.gate(r,gate)
            all_results.extend(current);reg.write_json(out/'summary.json',all_results)
            excluded=('parameters','setup_outcomes','control_projection_outcomes','discovery_blocks','product_effects')
            pd.DataFrame([{k:v for k,v in r.items() if k not in excluded} for r in all_results]).to_csv(out/'summary.csv',index=False)
            pd.DataFrame(audit).to_csv(out/'data_access.csv',index=False)
            exposures.append(dict(period=period['fold_id'],start=period['discovery_start'],end_exclusive=period['discovery_end'],phase='V4_fixed_structural_payoff',independent_unseen=False,feedback_round=1))
            pd.DataFrame(exposures).to_csv(out/'evaluation_exposures.csv',index=False)
        pd.DataFrame(boots).to_csv(out/'bootstrap_effects.csv',index=False)
    except BaseException as exc:
        reg.write_json(out/'failure.json',dict(error=repr(exc),traceback=traceback.format_exc(),partial_results_preserved=True));raise
    finally:
        db.close()
        for sink in sinks.values():sink.close()
        pairs.close()
    verify()
    if digest(SOURCE)!=seal['source_sha256']:raise ValueError('source changed')
    assert len(all_results)==16
    passed=[c['candidate_id'] for c in cs if all(r['decision']=='alpha_screen_pass_research_only' for r in all_results if r['candidate_id']==c['candidate_id'])]
    reg.write_json(out/'verification.json',dict(computation_complete=True,candidate_period_evaluations=16,actual_prefix_checks=prefixes,pass_both_periods=passed,
        decisions=dict(collections.Counter(r['decision'] for r in all_results)),row_counts={**{n:s.rows for n,s in sinks.items()},'matched_baseline':pairs.rows},
        historical_only=True,source_unchanged=True,new_strategy_results_after_2025_11_01_read=False,full_WFO_evaluations=0,shared_account_simulated=False))
    reg.write_json(out/'manifest.json',dict(version='4.0-EXP',freeze_sha256=digest(BASE/'freeze.json'),source_sha256=seal['source_sha256'],prior_results_unchanged=True))
    reg.write_json(BASE/'latest_run.json',dict(path=str(out.relative_to(ROOT)),status='complete'));print('V4 COMPLETE',passed,flush=True)


def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['freeze','verify','run']);args=p.parse_args()
    if args.action=='freeze':freeze()
    elif args.action=='verify':verify();print('V4 freeze verified')
    else:run()


if __name__=='__main__':main()
