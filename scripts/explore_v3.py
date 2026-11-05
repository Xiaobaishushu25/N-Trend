"""One frozen V3 cycle, synchronous cross-product features and caliper controls."""
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
import explore_v3_signals as logic
import explore_v2 as prior
import s3_signals as prep
import s3_alpha as stats
import run_s3_alpha as old
import s2_preregister as reg
from factor_study_data import Calendar,specifications,digest
from s1_market_regimes import select_main,fee_rule

BASE=ROOT/'artifacts/exploration_v3'
PERIODS=prior.PERIODS
SOURCE=prior.SOURCE


def protocol():
    p=prior.protocol();p['research_version']='3.0-EXP';p['purpose']='test cross-product information and local acceptance, instead of only changing single-instrument candle filters'
    p['prior_feedback']='V1 and V2 historical outcomes already observed; never reclassify them as unseen'
    p['mechanisms']={
        'P1':dict(name='同组共振下的落后品种回补',rules='Leave-one-out exact-timestamp peer median day displacement in own ATR units >=1/2, agreeing sign breadth>=.6/.75, >=2 peers. Local last3/6 completed-bar displacement before current <=0 in peer direction, then current directional candle closes through prior high/low. Stop adverse extreme of pullback plus tick.'),
        'P2':dict(name='价格接受压力后的区间释放',rules='Prior6/12 completed bars volume-weighted close-location pressure abs>=.2/.4, prior net drift<=one previousATR. Current volume/prior20 mean>=1/1.5, aligned body closes >=one tick beyond prior range. Stop opposite range plus tick. Close-location is a price-volume proxy, not buy/sell order flow.'),
        'P3':dict(name='同组平静且本品种低趋势时的假突破回归',rules='Absolute peer median<=.5/1ATR, priorER12<=.25/.4, prior12/24 bar range rejection >=.25 previousATR, closes inside with reversal body/close, double-sided rejection omitted. Stop rejection extreme plus tick.')}
    p['peer_groups']=logic.GROUPS
    p['peer_convention']='fixed research groups, not fitted correlations; leave target out, >=2 exact same end_ns valid peers, no ffill; day move requires observed09:00 open in same contract segment'
    p['matching']=dict(exact=['product','chronological4-month block','5-minute clock slot','direction'],
        calipers=dict(signal_ATRpct_div_control=[.67,1.5],signal_activity_ratio_div_control=[.5,2.]),
        activity_ratio='current completed volume/prior20 completed-bar mean',
        pool='same period, same cost gate<=.25R and activity known; primary12 label complete; exclude all24 actual signal timestamps',
        repeats=200,seed=20261108,without_replacement=True,
        interpretation='new protocol before V3 outcomes; V2 remains insufficient, no retroactive upgrades',
        no_posthoc_relaxation=True)
    p['inference']['seed']=20261109
    p['inference']['baseline']='same joint source-date inference, 3 chronological4-month blocks; controls cannot cross these blocks'
    p['common']['cooldown']='24 subsequent observed active bars or day close; no outcome-conditioned reuse'
    p['universe']=sorted(sum(logic.GROUPS.values(),[]))
    return p


def source_files():
    return [Path(__file__),ROOT/'scripts/explore_v3_signals.py',ROOT/'scripts/explore_v2_signals.py',ROOT/'scripts/explore_v2.py',
        ROOT/'scripts/s3_alpha.py',ROOT/'scripts/s3_signals.py',ROOT/'scripts/run_s3_alpha.py',ROOT/'scripts/s1_market_regimes.py',
        ROOT/'scripts/factor_study_data.py',ROOT/'crates/n-core/src/precision.rs',ROOT/'tests/test_explore_v3.py',ROOT/'tests/test_s3_alpha.py']


def freeze():
    BASE.mkdir(parents=True,exist_ok=True)
    if (BASE/'freeze.json').exists():raise ValueError('already frozen, refusing overwrite')
    prior.verify()
    p=protocol();universe=p['universe'];assert len(universe)==22 and len(set(universe))==22
    cs=logic.templates(universe);assert len(cs)==24
    for c in cs:c['candidate_sha256']=reg.canonical(c)
    reg.write_json(BASE/'protocol.json',p);reg.write_json(BASE/'candidate_manifest.json',dict(candidates=cs))
    test=subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-p','test_explore_v3.py','-v'],cwd=ROOT,text=True,capture_output=True)
    (BASE/'tests.txt').write_text(test.stdout+test.stderr,encoding='utf8')
    if test.returncode:raise ValueError('V3 regression failed')
    previous=ROOT/json.loads((prior.BASE/'latest_run.json').read_text(encoding='utf8'))['path']
    source_hash=json.loads((prior.BASE/'freeze.json').read_text(encoding='utf8'))['source_sha256']
    if digest(SOURCE)!=source_hash:raise ValueError('source changed')
    reg.write_json(BASE/'freeze.json',dict(version='3.0-EXP',created_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
        git_sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),source_sha256=source_hash,
        code_sha256={str(p.relative_to(ROOT)):digest(p) for p in source_files()},
        input_sha256={str(p.relative_to(ROOT)):digest(p) for p in [BASE/'protocol.json',BASE/'candidate_manifest.json',
            previous/'evidence.csv',previous/'verification.json',ROOT/'data/trading_calendar.json',ROOT/'scripts/factor-study-v1.json',ROOT/'scripts/exchange_fees_20261022.json']},
        prior_feedback_acknowledged=True,V3_returns_seen_before_freeze=0))
    print('V3 frozen:24 templates, paired historical years, no V3 returns seen',flush=True)


def verify():
    seal=json.loads((BASE/'freeze.json').read_text(encoding='utf8'))
    for section in ('code_sha256','input_sha256'):
        for name,expected in seal[section].items():
            if digest(ROOT/name)!=expected:raise ValueError('V3 source/input changed: '+name)
    return seal


def block(day,start):return ((int(day[:4])-int(start[:4]))*12+int(day[5:7])-int(start[5:7]))//4


def annotate(row,a,i,period):
    b=block(row['day'],period['discovery_start']);row['discovery_block']=b
    row.update(peer_median=float(a['peer_median'][i]),peer_breadth=float(a['peer_breadth'][i]),peer_count=int(a['peer_count'][i]),
        activity_ratio=float(a['activity_ratio'][i]),atr_pct_value=float(a['atr_pct'][i]),friction_R=float(a['friction_R'][i]))
    row['stratum']=(row['product'],b,row['minute'],row['side']);row['stratum_key']=json.dumps(row['stratum'])
    return row


def run():
    seal=verify()
    if digest(SOURCE)!=seal['source_sha256']:raise ValueError('source changed')
    out=BASE/('run_'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ'));out.mkdir(exist_ok=False)
    meta=json.loads((ROOT/'scripts/factor-study-v1.json').read_text(encoding='utf8'))
    calendar=Calendar(json.loads((ROOT/'data/trading_calendar.json').read_text(encoding='utf8')),meta['night_ends']);specs=specifications(meta)
    fees=json.loads((ROOT/'scripts/exchange_fees_20261022.json').read_text(encoding='utf8'))
    cs=json.loads((BASE/'candidate_manifest.json').read_text(encoding='utf8'))['candidates']
    schema=old.event_schema()
    for n in ('peer_median','peer_breadth','activity_ratio','atr_pct_value','friction_R'):schema=schema.append(pa.field(n,pa.float64()))
    schema=schema.append(pa.field('peer_count',pa.int32()))
    sinks={n:old.Sink(out/(n+'.parquet'),schema) for n in ('events','controls')};pairs=old.Sink(out/'matched_baseline.parquet',old.MATCH_SCHEMA)
    db=sqlite3.connect(SOURCE.resolve().as_uri()+'?mode=ro',uri=True);db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
    all_results=[];audit=[];prefixes=[];boots=[];exposure=[]
    try:
        for period in PERIODS:
            print('V3 PERIOD',period,flush=True);lo=int(pd.Timestamp(period['discovery_start'],tz='Asia/Shanghai').value);hi=int(pd.Timestamp(period['discovery_end'],tz='Asia/Shanghai').value)
            maps=pd.read_sql_query('select product,effective_date,contract from mappings where effective_date<? order by product,effective_date',db,params=(period['discovery_end'],))
            frames={};fee_table={}
            for product,mp in maps.groupby('product',sort=True):
                name=product.split('.')[1].upper();parts=[]
                for contract in mp.contract.unique():
                    q=pd.read_sql_query('select '+','.join(old.RAW)+' from bars where contract=? and period_seconds=300 and start_ns>=? and end_ns<=? order by start_ns',db,params=(contract,lo,hi))
                    audit.append(dict(period=period['fold_id'],product=name,contract=contract,start=period['discovery_start'],end_exclusive=period['discovery_end'],rows=len(q)))
                    if len(q):parts.append(q)
                if not parts:continue
                selected=select_main(pd.concat(parts,ignore_index=True),mp);fee_table[name]=fee_rule(fees,name)
                frames[name]=logic.features(prep.prepare(selected,calendar),specs[name+'0'],fee_table[name])
            frames=logic.peers(frames)
            short_frames=None
            if period['fold_id']=='D1':
                cut=int(pd.Timestamp('2024-05-08 11:00',tz='Asia/Shanghai').value)
                short_frames=logic.peers({name:logic.features(prep.prepare(f.loc[f.end_ns<=cut,old.RAW],calendar),specs[name+'0'],fee_table[name]) for name,f in frames.items()})
            events={c['candidate_id']:[] for c in cs};pool=[];strata=collections.defaultdict(list)
            for name,f in sorted(frames.items()):
                a={k:f[k].to_numpy() for k in f};spec=specs[name+'0'];fee=fee_table[name]
                generated={c['candidate_id']:logic.signals(f,c,spec['tick']) for c in cs}
                if short_frames is not None and name in ('C','JD','PB'):
                    short=short_frames[name];cutlen=len(short)
                    pd.testing.assert_frame_equal(f.iloc[:cutlen],short)
                    for c in cs:assert logic.signals(short,c,spec['tick'])==[r for r in generated[c['candidate_id']] if r['index']<cutlen]
                    prefixes.append(dict(product=name,rows=cutlen,templates=24,synchronous_peer_prefix_passed=True))
                union={int(a['end_ns'][q['index']]) for rows in generated.values() for q in rows};needed=set();count=0
                for c in cs:
                    rows=[]
                    for q in generated[c['candidate_id']]:
                        r=old.record(period,c,name,q,a,spec,fee,hi);annotate(r,a,q['index'],period);rows.append(r)
                        if r['status_12']=='complete':needed.add(r['stratum'])
                    sinks['events'].add(rows);events[c['candidate_id']].extend(rows);count+=len(rows)
                control_rows=[];c=dict(candidate_id='CONTROL',hypothesis_id='BASELINE')
                for i in range(len(f)):
                    if not a['valid'][i] or not a['economic_gate'][i] or not a['activity_known'][i] or not np.isfinite(a['atr'][i]) or a['atr'][i]<=0 or a['minute'][i]+5>=890 or int(a['end_ns'][i]) in union or a['activity_ratio'][i]<=0:continue
                    for side in (1,-1):
                        key=(name,block(str(a['day'][i]),period['discovery_start']),int(a['minute'][i]),side)
                        if key not in needed:continue
                        r=old.record(period,c,name,dict(index=i,side=side),a,spec,fee,hi,'control');annotate(r,a,i,period)
                        if r['status_12']!='complete':continue
                        strata[key].append(len(pool));pool.append(r);control_rows.append(r)
                sinks['controls'].add(control_rows)
                print(period['fold_id'],name,'events',count,'controls',len(control_rows),flush=True)
            current=[]
            for c in cs:
                cid=c['candidate_id'];raw=events[cid];e=[r for r in raw if r['status_12']=='complete']
                match=logic.matching(e,dict(rows=pool,by_stratum=strata),200,20261108)
                r,boot=stats.inference(e,pool,match,1000,20261109) if e else (dict(effect=None,ci_lower=None,ci_upper=None,p_value=1.,signal_net_R=None,matched_fraction=0.,matched_days=0,positive_blocks=0,product_effects={}),[])
                r.update(fold_id=period['fold_id'],candidate_id=cid,hypothesis_id=c['hypothesis_id'],raw_events=len(raw),complete_primary=len(e),
                    one_lot_600_fraction=old.finite_mean([int(q['one_lot_600_feasible']) for q in raw if q['one_lot_600_feasible'] is not None]))
                for h in (6,12,24):
                    z=[q for q in raw if q[f'status_{h}']=='complete'];r[f'n_h{h}']=len(z)
                    for k in ('gross_R','net_R','MFE_R','MAE_R','p1_lower','p1_upper'):r[f'{k}_h{h}']=old.finite_mean([q[f'{k}_{h}'] for q in z])
                for rep,row in enumerate(match):
                    batch=[]
                    for i in np.flatnonzero(row>=0):
                        event=e[int(i)];control=pool[int(row[i])]
                        batch.append(dict(fold_id=period['fold_id'],candidate_id=cid,event_id=event['event_id'],control_id=control['event_id'],
                            event_day=event['day'],control_day=control['day'],replicate=rep,primary_delta_R=event['net_R_12']-control['net_R_12']))
                    pairs.add(batch)
                boots.extend(dict(period=period['fold_id'],candidate_id=cid,replicate=i,effect_R=v) for i,v in enumerate(boot))
                current.append(r);print('V3 ALPHA',period['fold_id'],cid,'n',len(e),'match',r['matched_fraction'],'net',r['signal_net_R'],flush=True)
            adjusted=stats.holm([r['p_value'] for r in current]);gate=dict(s3_design=dict(decision=dict(minimum_events=60,minimum_independent_days=30,minimum_matched_fraction=.8)))
            for r,p in zip(current,adjusted):r['holm_p']=float(p);r['decision']=old.gate(r,gate)
            all_results.extend(current);reg.write_json(out/'summary.json',all_results)
            pd.DataFrame([{k:v for k,v in r.items() if k not in ('product_effects','discovery_blocks')} for r in all_results]).to_csv(out/'summary.csv',index=False)
            pd.DataFrame(audit).to_csv(out/'data_access.csv',index=False)
            exposure.append(dict(period=period['fold_id'],start=period['discovery_start'],end_exclusive=period['discovery_end'],stage='V3_diagnostic',feedback_round=1,independent_unseen=False))
            pd.DataFrame(exposure).to_csv(out/'evaluation_exposures.csv',index=False)
        pd.DataFrame(boots).to_csv(out/'bootstrap_effects.csv',index=False)
    except BaseException as exc:
        reg.write_json(out/'failure.json',dict(error=repr(exc),traceback=traceback.format_exc(),partial_results_preserved=True));raise
    finally:
        db.close()
        for sink in sinks.values():sink.close()
        pairs.close()
    verify()
    if digest(SOURCE)!=seal['source_sha256']:raise ValueError('source changed during V3')
    assert len(all_results)==48
    passed=[c['candidate_id'] for c in cs if all(r['decision']=='alpha_screen_pass_research_only' for r in all_results if r['candidate_id']==c['candidate_id'])]
    reg.write_json(out/'verification.json',dict(computation_complete=True,candidate_period_evaluations=48,actual_peer_prefix_checks=prefixes,
        pass_both_periods=passed,decisions=dict(collections.Counter(r['decision'] for r in all_results)),
        raw_event_rows=sinks['events'].rows,control_rows=sinks['controls'].rows,matched_pair_rows=pairs.rows,
        no_new_strategy_outcomes_after_2025_11_01=True,historical_only=True,source_unchanged=True,full_WFO_evaluations=0))
    reg.write_json(out/'manifest.json',dict(version='3.0-EXP',freeze_sha256=digest(BASE/'freeze.json'),source_sha256=seal['source_sha256'],prior_versions_unchanged=True))
    reg.write_json(BASE/'latest_run.json',dict(path=str(out.relative_to(ROOT)),status='complete'))
    print('V3 COMPLETE',passed,flush=True)


def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['freeze','verify','run']);args=p.parse_args()
    if args.action=='freeze':freeze()
    elif args.action=='verify':verify();print('V3 freeze verified')
    else:run()


if __name__=='__main__':main()
