"""A bounded version-2 research cycle, never overwriting version-1 evidence.

freeze -> run; first/second historical years checked without touching Nov2025+.
"""
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
import explore_v2_signals as logic
import s3_signals as prep
import s3_alpha as stats
import run_s3_alpha as old
import s2_preregister as registry
from factor_study_data import Calendar, specifications, digest
from s1_market_regimes import select_main,fee_rule

BASE=ROOT/'artifacts/exploration_v2'
SOURCE=ROOT/'data/research_market_history.sqlite'
OLD=ROOT/'artifacts/s3/run_20261104T152133_122747Z'
PERIODS=[dict(fold_id='D1',discovery_start='2023-11-01',discovery_end='2024-11-01'),
         dict(fold_id='D2',discovery_start='2024-11-01',discovery_end='2025-11-01')]


def protocol():
    return dict(research_version='2.0-EXP',purpose='bounded new-mechanism exploration after version-1 failure',
        prior_feedback=str(OLD.relative_to(ROOT)),independence='all historical outcomes diagnostic; previous market/strategy exposure acknowledged',
        source=str(SOURCE.relative_to(ROOT)),universe='all 22 products, no result-based subsets',
        mechanisms=dict(N1=dict(name='开盘区间接受后的顺势延续',intuition='opening inventory and price discovery may persist after the first accepted break',
            rules='09:00-09:30 full six-bar range; first buffered close outside between09:30 and11:00 consumes day even if rejected. Direction must agree with opening body and opening body/range threshold; stop opposite range plus tick.'),
            N2=dict(name='假突破后的反向回归',intuition='failed auctions can trap breakout orders and induce inventory reversal',
                rules='prior12/24 completed5m high/low; current excursion >= prior ATR multiple then closes inside with opposing body and close direction; two-sided excursions rejected; stop rejection extreme plus tick.'),
            N3=dict(name='低趋势环境的日内均价偏离回归',intuition='stretched prices in inefficient paths may revert toward observed intraday inventory average',
                rules='previous close deviation from day VWAP proxy >=1.5/2.5 priorATR and previous ER12<=0.25/0.4; current opposing candle/close while still on original side of currentVWAP; stop extreme of last two bars plus tick.')),
        common=dict(only_day=True,overnight=False,last_entry_before='14:50',
            cost_gate='forecast(exchange open+close_today fees + 2 adverse ticks)/(signal ATR12*multiplier)<=0.25, evaluated at signal close only',
            cooldown='24 subsequent active bars or day close, same as V1',
            economic_scope='cost gate is new version2 signal rule; one-lot400/600 feasibility reported separately without stop tightening',
            vwap_proxy='cumulative volume-weighted typical(high+low+close)/3 within observed day and valid segment; not tick VWAP',
            entry='next actual same-day same-contract complete5m open plus1 adverse tick; no signal-bar fill',
            fees='existing fixed exchange base-fee snapshot; SR published rule; no broker addition',
            specs='same V1 proxies, retained conflicts; full execution uncertified'),
        budget=dict(hypotheses=3,candidates_per_hypothesis=8,candidates_total=24,
            continuous_parameter_cap=2,periods=2,maximum_candidate_period_evaluations=48,
            feedback_rounds_this_cycle=1,unplanned_neighbors=0,full_WFO_evaluations=0),
        periods=PERIODS,never_read_new_strategy_outcomes_after='2025-11-01',
        primary_horizon=12,secondary_horizons=[6,24],secondary_use='diagnostic only; no best-horizon selection',
        matching=dict(exact=['product','month','clock_slot','direction','ATR_pct_bin','volume_activity_bin','ATR_tick_scale_bin'],
            atr_pct_bins=[.0005,.001,.002,.004],volume_activity_bins=[.5,1,2],atr_tick_scale_bins=[8,12,20],
            pool='same period generic eligible bars satisfying common cost gate, prior20 volume available, complete12-bar label; exclude union of24 actual signal timestamps',
            repeats=200,seed=20261106,without_replacement=True,no_posthoc_relaxation=True),
        inference=dict(unit='calendar date, all products jointly; signal/control assigned to their own dates',
            bootstrap=1000,seed=20261107,CI=.95,Holm_family=24,
            baseline='reuse frozen V1 joint-date inference method with new matching strata',
            minimum_events=60,minimum_matched_days=30,minimum_match_fraction=.8,
            stability='three chronological4-month blocks, >=2 positive with >=10 matched events',
            period_pass='positive matched signal net, positive relative effect and95% lowerCI, Holm<=0.05 plus scale/coverage/stability gates',
            cycle_pass='same candidate must pass both D1 and D2 unchanged; no D2-based selection, both periods still historically exposed',
            multiple_periods='two dependent-on-research historical checks, not independent final proof'),
        stop_rule='finish the24x2 cycle even if none pass; no automatic unlimited research or tuning after results')


def sources():
    return [Path(__file__),ROOT/'scripts/explore_v2_signals.py',ROOT/'scripts/s3_signals.py',ROOT/'scripts/s3_alpha.py',
            ROOT/'scripts/run_s3_alpha.py',ROOT/'scripts/s1_market_regimes.py',ROOT/'scripts/factor_study_data.py',
            ROOT/'crates/n-core/src/precision.rs',ROOT/'tests/test_explore_v2.py',ROOT/'tests/test_s3_alpha.py']


def freeze():
    BASE.mkdir(parents=True,exist_ok=True)
    if (BASE/'freeze.json').exists():raise ValueError('already frozen; cannot overwrite')
    old.verify()
    source_expected=json.loads((OLD/'manifest.json').read_text(encoding='utf8'))['database_sha256']
    if digest(SOURCE)!=source_expected:raise ValueError('source differs from V1')
    s1=json.loads((ROOT/'artifacts/s1/manifest.json').read_text(encoding='utf8'))
    universe=sorted(r['product'] for r in s1['coverage']);candidates=logic.templates(universe)
    assert len(candidates)==24 and len({c['candidate_id'] for c in candidates})==24
    for c in candidates:c['candidate_sha256']=registry.canonical(c)
    p=protocol();p['universe']=universe
    registry.write_json(BASE/'protocol.json',p);registry.write_json(BASE/'candidate_manifest.json',dict(candidates=candidates))
    result=subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-p','test_explore_v2.py','-v'],cwd=ROOT,capture_output=True,text=True)
    (BASE/'tests.txt').write_text(result.stdout+result.stderr,encoding='utf8')
    if result.returncode:raise ValueError('V2 regression failure')
    registry.write_json(BASE/'freeze.json',dict(created_utc=dt.datetime.now(dt.timezone.utc).isoformat(),research_version='2.0-EXP',
        git_sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),source_sha256=source_expected,
        code_sha256={str(p.relative_to(ROOT)):digest(p) for p in sources()},
        input_sha256={str(p.relative_to(ROOT)):digest(p) for p in [BASE/'protocol.json',BASE/'candidate_manifest.json',
            ROOT/'scripts/factor-study-v1.json',ROOT/'scripts/exchange_fees_20261022.json',ROOT/'data/trading_calendar.json',OLD/'manifest.json',OLD/'alpha_summary.csv']},
        new_strategy_outcomes_seen_before_freeze=0,prior_version_feedback_seen=True,candidates=24))
    print('V2 FROZEN: 24 templates, 2 historical periods, no new outcomes read',flush=True)


def verify():
    p=json.loads((BASE/'freeze.json').read_text(encoding='utf8'))
    for section in ('code_sha256','input_sha256'):
        for name,expected in p[section].items():
            if digest(ROOT/name)!=expected:raise ValueError('V2 frozen source changed: '+name)
    return p


def annotate(row,arr,i):
    row.update(activity_bin=int(arr['activity_bin'][i]),scale_bin=int(arr['scale_bin'][i]),
               friction_R=float(arr['friction_R'][i]),atr_ticks=float(arr['atr_ticks'][i]))
    row['stratum']=row['stratum']+(row['activity_bin'],row['scale_bin'])
    row['stratum_key']=json.dumps(row['stratum'])
    return row


def run():
    seal=verify()
    if digest(SOURCE)!=seal['source_sha256']:raise ValueError('database changed')
    out=BASE/('run_'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ'));out.mkdir(exist_ok=False)
    protocol_meta=json.loads((ROOT/'scripts/factor-study-v1.json').read_text(encoding='utf8'))
    calendar=Calendar(json.loads((ROOT/'data/trading_calendar.json').read_text(encoding='utf8')),protocol_meta['night_ends'])
    specs=specifications(protocol_meta);fees=json.loads((ROOT/'scripts/exchange_fees_20261022.json').read_text(encoding='utf8'))
    candidates=json.loads((BASE/'candidate_manifest.json').read_text(encoding='utf8'))['candidates']
    schema=old.event_schema().append(pa.field('activity_bin',pa.int32())).append(pa.field('scale_bin',pa.int32())).append(pa.field('friction_R',pa.float64())).append(pa.field('atr_ticks',pa.float64()))
    events_sink=old.Sink(out/'events.parquet',schema);controls_sink=old.Sink(out/'controls.parquet',schema)
    pair_sink=old.Sink(out/'matched_baseline.parquet',old.MATCH_SCHEMA)
    db=sqlite3.connect(SOURCE.resolve().as_uri()+'?mode=ro',uri=True);db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
    results=[];audit=[];prefixes=[];bootrows=[];exposures=[]
    try:
        for period in PERIODS:
            lo=int(pd.Timestamp(period['discovery_start'],tz='Asia/Shanghai').value);hi=int(pd.Timestamp(period['discovery_end'],tz='Asia/Shanghai').value)
            maps=pd.read_sql_query('select product,effective_date,contract from mappings where effective_date<? order by product,effective_date',db,params=(period['discovery_end'],))
            signal_rows={c['candidate_id']:[] for c in candidates};pool=[];strata=collections.defaultdict(list)
            print('V2 PERIOD',period,flush=True)
            for product,mp in maps.groupby('product',sort=True):
                name=product.split('.')[1].upper();parts=[]
                for contract in mp.contract.unique():
                    q=pd.read_sql_query('select '+','.join(old.RAW)+' from bars where contract=? and period_seconds=300 and start_ns>=? and end_ns<=? order by start_ns',db,params=(contract,lo,hi))
                    audit.append(dict(period=period['fold_id'],product=name,contract=contract,start=period['discovery_start'],end_exclusive=period['discovery_end'],rows=len(q)))
                    if len(q):parts.append(q)
                if not parts:continue
                selected=select_main(pd.concat(parts,ignore_index=True),mp);spec=specs[name+'0'];fee=fee_rule(fees,name)
                f=logic.features(prep.prepare(selected,calendar),spec,fee);a={k:f[k].to_numpy() for k in f}
                generated={c['candidate_id']:logic.signals(f,c,spec['tick']) for c in candidates}
                if period['fold_id']=='D1' and name in ('C','JD','PB'):
                    cut=len(f)//2+13;short=logic.features(prep.prepare(f[old.RAW].iloc[:cut],calendar),spec,fee)
                    for c in candidates:
                        assert logic.signals(short,c,spec['tick'])==[r for r in generated[c['candidate_id']] if r['index']<cut],(name,c['candidate_id'])
                    prefixes.append(dict(product=name,rows=cut,templates=24,passed=True))
                union={int(a['end_ns'][q['index']]) for e in generated.values() for q in e};needed=set();count=0
                for c in candidates:
                    rows=[]
                    for q in generated[c['candidate_id']]:
                        row=old.record(period,c,name,q,a,spec,fee,hi);annotate(row,a,q['index'])
                        day=row['day'];row['discovery_block']=((int(day[:4])-int(period['discovery_start'][:4]))*12+int(day[5:7])-int(period['discovery_start'][5:7]))//4
                        rows.append(row)
                        if row['status_12']=='complete':needed.add(row['stratum'])
                    events_sink.add(rows);signal_rows[c['candidate_id']].extend(rows);count+=len(rows)
                controls=[];base=dict(candidate_id='CONTROL',hypothesis_id='BASELINE')
                for i in range(len(f)):
                    if not a['valid'][i] or not np.isfinite(a['atr'][i]) or not a['economic_gate'][i] or not a['activity_known'][i] or a['minute'][i]+5>=890 or int(a['end_ns'][i]) in union:continue
                    for side in (1,-1):
                        key=(name,str(a['month'][i]),int(a['minute'][i]),int(a['vol_bin'][i]),side,int(a['activity_bin'][i]),int(a['scale_bin'][i]))
                        if key not in needed:continue
                        row=old.record(period,base,name,dict(index=i,side=side),a,spec,fee,hi,'control');annotate(row,a,i)
                        if row['status_12']!='complete':continue
                        row['discovery_block']=0
                        strata[key].append(len(pool));pool.append(row);controls.append(row)
                controls_sink.add(controls)
                print(period['fold_id'],name,'events',count,'controls',len(controls),flush=True)
            current=[]
            for c in candidates:
                cid=c['candidate_id'];raw=signal_rows[cid];e=[r for r in raw if r['status_12']=='complete']
                match=stats.matching(e,dict(rows=pool,by_stratum=strata),200,20261106)
                r,boot=stats.inference(e,pool,match,1000,20261107) if e else (dict(effect=None,ci_lower=None,ci_upper=None,p_value=1.,signal_net_R=None,matched_fraction=0.,matched_days=0,positive_blocks=0,product_effects={}),[])
                r.update(fold_id=period['fold_id'],candidate_id=cid,hypothesis_id=c['hypothesis_id'],raw_events=len(raw),complete_primary=len(e),
                    one_lot_600_fraction=old.finite_mean([int(z['one_lot_600_feasible']) for z in raw if z['one_lot_600_feasible'] is not None]))
                for h in (6,12,24):
                    valid=[z for z in raw if z[f'status_{h}']=='complete'];r[f'n_h{h}']=len(valid)
                    for k in ('gross_R','net_R','MFE_R','MAE_R','p1_lower','p1_upper'):r[f'{k}_h{h}']=old.finite_mean([z[f'{k}_{h}'] for z in valid])
                for rep,matchrow in enumerate(match):
                    records=[]
                    for i in np.flatnonzero(matchrow>=0):
                        z=e[int(i)];b=pool[int(matchrow[i])]
                        records.append(dict(fold_id=period['fold_id'],candidate_id=cid,event_id=z['event_id'],control_id=b['event_id'],
                            event_day=z['day'],control_day=b['day'],replicate=rep,primary_delta_R=z['net_R_12']-b['net_R_12']))
                    pair_sink.add(records)
                bootrows.extend(dict(period=period['fold_id'],candidate_id=cid,replicate=i,effect_R=v) for i,v in enumerate(boot))
                current.append(r);print('V2 ALPHA',period['fold_id'],cid,'n',len(e),'effect',r['effect'],'net',r['signal_net_R'],flush=True)
            adjusted=stats.holm([r['p_value'] for r in current])
            gate_budget=dict(s3_design=dict(decision=dict(minimum_events=60,minimum_independent_days=30,minimum_matched_fraction=.8)))
            for r,p in zip(current,adjusted):r['holm_p']=float(p);r['decision']=old.gate(r,gate_budget)
            results.extend(current);registry.write_json(out/'summary.json',results)
            pd.DataFrame([{k:v for k,v in r.items() if k not in ('product_effects','discovery_blocks')} for r in results]).to_csv(out/'summary.csv',index=False)
            pd.DataFrame(audit).to_csv(out/'data_access.csv',index=False)
            exposures.append(dict(period=period['fold_id'],start=period['discovery_start'],end_exclusive=period['discovery_end'],role='bounded_exploration',feedback_round=1,independent_unseen=False))
            pd.DataFrame(exposures).to_csv(out/'evaluation_exposures.csv',index=False)
        pd.DataFrame(bootrows).to_csv(out/'bootstrap_effects.csv',index=False)
    except BaseException as exc:
        registry.write_json(out/'failure.json',dict(error=repr(exc),traceback=traceback.format_exc(),partial_results_preserved=True));raise
    finally:
        db.close();events_sink.close();controls_sink.close();pair_sink.close()
    verify()
    if digest(SOURCE)!=seal['source_sha256']:raise ValueError('source changed during V2')
    assert len(results)==48
    both=[c['candidate_id'] for c in candidates if len([r for r in results if r['candidate_id']==c['candidate_id']])==2 and all(r['decision']=='alpha_screen_pass_research_only' for r in results if r['candidate_id']==c['candidate_id'])]
    registry.write_json(out/'verification.json',dict(computation_complete=True,candidate_period_evaluations=len(results),expected_evaluations=48,
        actual_prefix_checks=prefixes,pass_both_periods=both,decisions=dict(collections.Counter(r['decision'] for r in results)),
        raw_event_rows=events_sink.rows,control_rows=controls_sink.rows,matched_pair_rows=pair_sink.rows,
        historical_only=True,source_unchanged=True,new_strategy_results_after_2025_11_01_read=False,full_WFO_evaluations=0))
    registry.write_json(out/'manifest.json',dict(version='2.0-EXP',freeze_sha256=digest(BASE/'freeze.json'),source_sha256=seal['source_sha256'],
        prior_feedback_acknowledged=True,created_utc=dt.datetime.now(dt.timezone.utc).isoformat()))
    registry.write_json(BASE/'latest_run.json',dict(path=str(out.relative_to(ROOT)),status='complete'))
    print('V2 COMPLETE',both,flush=True)


def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['freeze','verify','run']);args=p.parse_args()
    if args.action=='freeze':freeze()
    elif args.action=='verify':verify();print('V2 freeze verified')
    else:run()


if __name__=='__main__':main()
