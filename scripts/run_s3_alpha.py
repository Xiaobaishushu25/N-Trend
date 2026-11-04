"""Freeze S3 code, then evaluate only F1..F6 discovery windows once per run.

python scripts/run_s3_alpha.py freeze
python scripts/run_s3_alpha.py run
"""
import argparse
import collections
import csv
import datetime as dt
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import traceback

ROOT=Path(__file__).resolve().parents[1]
if (ROOT/'.s1-deps').exists():sys.path.insert(0,str(ROOT/'.s1-deps'))
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import s2_preregister as prereg
import s3_signals as sig
import s3_alpha as alpha
from factor_study_data import Calendar, specifications, digest
from s1_market_regimes import select_main,fee_rule

BASE=ROOT/'artifacts/s3'
S2=ROOT/'artifacts/s2'
RAW=['contract','start_ns','end_ns','open','high','low','close','volume','constituent_count','complete']


def utc():return dt.datetime.now(dt.timezone.utc).isoformat()


def event_schema():
    fields=[]
    for name in ('fold_id','candidate_id','hypothesis_id','product','event_id','source_event','day','month','contract','stratum_key'):
        fields.append(pa.field(name,pa.string()))
    for name in ('index','side','minute','vol_bin','discovery_block'):fields.append(pa.field(name,pa.int32()))
    for name in ('signal_ns','entry_ns','origin_end'):fields.append(pa.field(name,pa.int64()))
    for name in ('R_price','entry_price','stop','lot_planned_risk'):fields.append(pa.field(name,pa.float64()))
    for name in ('calendar_known','execution_verified','one_lot_600_feasible'):fields.append(pa.field(name,pa.bool_()))
    for h in alpha.HORIZONS:
        fields.extend([pa.field(f'status_{h}',pa.string()),pa.field(f'label_end_ns_{h}',pa.int64())])
        for k in ('gross_R','net_R','MFE_R','MAE_R'):fields.append(pa.field(f'{k}_{h}',pa.float64()))
        for k in ('p1_lower','p1_upper','p2_lower','p2_upper'):fields.append(pa.field(f'{k}_{h}',pa.int8()))
        for k in ('ambiguous','p1_unresolved','p2_unresolved'):fields.append(pa.field(f'{k}_{h}',pa.bool_()))
    return pa.schema(fields)


MATCH_SCHEMA=pa.schema([pa.field(k,pa.string()) for k in ('fold_id','candidate_id','event_id','control_id','event_day','control_day')]+
                      [pa.field('replicate',pa.int32()),pa.field('primary_delta_R',pa.float64())])


class Sink:
    def __init__(self,path,schema):self.writer=pq.ParquetWriter(path,schema,compression='zstd');self.schema=schema;self.rows=0
    def add(self,rows):
        if rows:self.writer.write_table(pa.Table.from_pylist(rows,schema=self.schema));self.rows+=len(rows)
    def close(self):self.writer.close()


def source_files():
    return [Path(__file__),ROOT/'scripts/s3_signals.py',ROOT/'scripts/s3_alpha.py',ROOT/'tests/test_s3_alpha.py',
            ROOT/'scripts/s1_market_regimes.py',ROOT/'scripts/factor_study_data.py',ROOT/'crates/n-core/src/precision.rs']


def input_files():
    return [S2/'freeze_manifest.json',BASE/'implementation_conventions.md',BASE/'development_failures.md',
            ROOT/'data/trading_calendar.json',ROOT/'scripts/factor-study-v1.json',ROOT/'scripts/exchange_fees_20261022.json']


def freeze():
    prereg.verify(S2);seal_path=BASE/'implementation_freeze.json'
    if seal_path.exists():raise ValueError('implementation already frozen; will not overwrite')
    result=subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-p','test_s3_alpha.py','-v'],cwd=ROOT,text=True,capture_output=True)
    (BASE/'tests_before_evaluation.txt').write_text(result.stdout+result.stderr,encoding='utf8')
    if result.returncode:raise ValueError('synthetic regression failure')
    prereg.write_json(seal_path,dict(schema='s3-implementation-freeze-v1',created_utc=utc(),
        git_sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        code_sha256={str(p.relative_to(ROOT)):digest(p) for p in source_files()},
        input_sha256={str(p.relative_to(ROOT)):digest(p) for p in input_files()},
        s2_freeze_sha256=digest(S2/'freeze_manifest.json'),real_signal_evaluations_before_freeze=0,
        signal_template_count=24,synthetic_test_methods=10,tests_sha256=digest(BASE/'tests_before_evaluation.txt')))
    print('S3 implementation frozen; 10 synthetic regressions passed; no real results read',flush=True)


def verify():
    prereg.verify(S2);seal=json.loads((BASE/'implementation_freeze.json').read_text(encoding='utf8'))
    for section in ('code_sha256','input_sha256'):
        for path,expected in seal[section].items():
            if digest(ROOT/path)!=expected:raise ValueError('frozen S3 source changed: '+path)
    return seal


def metadata(fold,candidate,product,i,f,side,source,origin=None,stop=None):
    day=str(f['day'][i]);month=str(f['month'][i]);minute=int(f['minute'][i]);vb=int(f['vol_bin'][i])
    block=((int(day[:4])-int(fold['discovery_start'][:4]))*12+int(day[5:7])-int(fold['discovery_start'][5:7]))//3
    key=(product,month,minute,vb,int(side))
    return dict(fold_id=fold['fold_id'],candidate_id=candidate['candidate_id'],hypothesis_id=candidate['hypothesis_id'],
        product=product,event_id=f"{fold['fold_id']}:{candidate['candidate_id']}:{product}:{int(f['end_ns'][i])}:{side}",
        source_event=source,index=int(i),day=day,month=month,minute=minute,vol_bin=vb,discovery_block=int(block),
        contract=str(f['contract'][i]),origin_end=origin,stop=stop,stratum=key,stratum_key=json.dumps(key),
        time_key=(product,int(f['end_ns'][i])))


def record(fold,candidate,product,q,arr,spec,fees,hi,source='signal'):
    row=metadata(fold,candidate,product,q['index'],arr,q['side'],source,q.get('origin_end'),q.get('stop'))
    row.update(alpha.label(arr,q['index'],q['side'],spec,fees,hi,q.get('stop')))
    return row


def gate(result,budget):
    d=budget['s3_design']['decision']
    if result['complete_primary']<d['minimum_events'] or result['matched_days']<d['minimum_independent_days'] or result['matched_fraction']<d['minimum_matched_fraction']:
        return 'unconfirmed_insufficient_evidence'
    if result['effect'] is None or result['effect']<=0 or result['ci_lower']<=0 or result['holm_p']>.05 or result['positive_blocks']<2 or result['signal_net_R']<=0:
        return 'not_passed_registered_screen'
    return 'alpha_screen_pass_research_only'


def finite_mean(values):
    a=[v for v in values if v is not None and np.isfinite(v)]
    return float(np.mean(a)) if a else None


def evaluate(run):
    seal=verify();s2seal=json.loads((S2/'freeze_manifest.json').read_text(encoding='utf8'))
    source=ROOT/s2seal['database']['path']
    if digest(source)!=s2seal['database']['sha256']:raise ValueError('database differs from frozen S2 source')
    candidates=json.loads((S2/'candidate_manifest.json').read_text(encoding='utf8'))['candidates']
    import yaml
    budget=yaml.safe_load((S2/'search_budget.yaml').read_text(encoding='utf8'))
    protocol=json.loads((ROOT/'scripts/factor-study-v1.json').read_text(encoding='utf8'))
    specs=specifications(protocol);fees=json.loads((ROOT/'scripts/exchange_fees_20261022.json').read_text(encoding='utf8'))
    calendar=Calendar(json.loads((ROOT/'data/trading_calendar.json').read_text(encoding='utf8')),protocol['night_ends'])
    sinks={name:Sink(run/(name+'.parquet'),event_schema()) for name in ('raw_alpha_events','baseline_pool','mechanical_breakouts')}
    pairs=Sink(run/'matched_baseline.parquet',MATCH_SCHEMA)
    accesses=(run/'data_access.csv').open('w',encoding='utf8',newline='')
    aw=csv.DictWriter(accesses,fieldnames=['fold_id','role','product','contract','start_inclusive','end_exclusive','rows','query_sha256']);aw.writeheader()
    db=sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True);db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
    summaries=[];prefix_checks=[];history=[];bootrows=[]
    try:
        for fold in prereg.folds():
            lo=int(pd.Timestamp(fold['discovery_start'],tz='Asia/Shanghai').value);hi=int(pd.Timestamp(fold['discovery_end'],tz='Asia/Shanghai').value)
            mapping=pd.read_sql_query('select product,effective_date,contract from mappings where effective_date<? order by product,effective_date',db,params=(fold['discovery_end'],))
            events_by={c['candidate_id']:[] for c in candidates};mechanical_by=collections.defaultdict(list)
            controls=[];by_stratum=collections.defaultdict(list)
            print('DISCOVERY',fold['fold_id'],fold['discovery_start'],fold['discovery_end'],flush=True)
            for product,mp in mapping.groupby('product',sort=True):
                name=product.split('.')[1].upper();frames=[]
                for contract in mp.contract.unique():
                    sql='select '+','.join(RAW)+' from bars where period_seconds=300 and contract=? and start_ns>=? and end_ns<=? order by start_ns'
                    q=pd.read_sql_query(sql,db,params=(contract,lo,hi))
                    aw.writerow(dict(fold_id=fold['fold_id'],role='Researcher.discovery',product=name,contract=contract,start_inclusive=fold['discovery_start'],
                        end_exclusive=fold['discovery_end'],rows=len(q),query_sha256=prereg.canonical([sql,contract,lo,hi])))
                    if len(q):frames.append(q)
                if not frames:continue
                selected=select_main(pd.concat(frames,ignore_index=True),mp)
                f=sig.prepare(selected,calendar);arr={c:f[c].to_numpy() for c in f};higher={m:sig.aggregate(f,m) for m in (5,15,30,60)}
                spec=specs[name+'0'];fee=fee_rule(fees,name)
                generated={c['candidate_id']:sig.signals(f,c,spec['tick'],higher) for c in candidates}
                if fold['fold_id']=='F1' and name in ('C','JD','PB'):
                    cut=len(f)//2+13;short=sig.prepare(f[RAW].iloc[:cut],calendar);hshort={m:sig.aggregate(short,m) for m in (5,15,30,60)}
                    for c in candidates:
                        actual=sig.signals(short,c,spec['tick'],hshort)
                        expected=tuple([e for e in r if e['index']<cut] for r in generated[c['candidate_id']])
                        if actual!=expected:raise ValueError('actual prefix mismatch: '+name+' '+c['candidate_id'])
                    prefix_checks.append(dict(fold_id='F1',product=name,prefix_rows=cut,candidates=24,signals_and_first_breaks_match=True))
                event_times={int(arr['end_ns'][q['index']]) for e,_ in generated.values() for q in e}
                needed=set();n_events=0
                for c in candidates:
                    e,b=generated[c['candidate_id']]
                    rows=[record(fold,c,name,q,arr,spec,fee,hi) for q in e]
                    sinks['raw_alpha_events'].add(rows);events_by[c['candidate_id']].extend(rows);n_events+=len(rows)
                    needed.update(r['stratum'] for r in rows if r['status_12']=='complete')
                    br=[record(fold,c,name,q,arr,spec,fee,hi,'first_breakout') for q in b]
                    sinks['mechanical_breakouts'].add(br);mechanical_by[c['candidate_id']].extend(br)
                control_template=dict(candidate_id='CONTROL',hypothesis_id='BASELINE')
                control_batch=[]
                for i in range(len(f)):
                    if not arr['valid'][i] or not np.isfinite(arr['atr'][i]) or arr['atr'][i]<=0 or arr['minute'][i]+5>=890 or int(arr['end_ns'][i]) in event_times:continue
                    for side in (1,-1):
                        key=(name,str(arr['month'][i]),int(arr['minute'][i]),int(arr['vol_bin'][i]),side)
                        if key not in needed:continue
                        row=record(fold,control_template,name,dict(index=i,side=side),arr,spec,fee,hi,'matched_control_pool')
                        if row['status_12']!='complete':continue
                        index=len(controls);controls.append(row);by_stratum[key].append(index);control_batch.append(row)
                sinks['baseline_pool'].add(control_batch)
                print(fold['fold_id'],name,'events',n_events,'controls',len(control_batch),flush=True)
            accesses.flush()
            fold_results=[];pool=dict(rows=controls,by_stratum=by_stratum)
            for candidate in candidates:
                cid=candidate['candidate_id'];all_events=events_by[cid];events=[r for r in all_events if r['status_12']=='complete']
                match=alpha.matching(events,pool,200,20261104)
                result,boot=alpha.inference(events,controls,match,1000,20261105) if events else (dict(effect=None,ci_lower=None,ci_upper=None,p_value=1.,signal_net_R=None,matched_fraction=0.,matched_days=0,positive_blocks=0,product_effects={}),[])
                result.update(fold_id=fold['fold_id'],candidate_id=cid,hypothesis_id=candidate['hypothesis_id'],
                    raw_events=len(all_events),complete_primary=len(events),raw_event_days=len({r['day'] for r in events}),
                    calendar_known_fraction=finite_mean([int(r['calendar_known']) for r in all_events]),
                    one_lot_600_fraction=finite_mean([int(r['one_lot_600_feasible']) for r in all_events if r['one_lot_600_feasible'] is not None]),
                    one_lot_400_fraction=finite_mean([int(0<r['lot_planned_risk']<=400 and r['one_lot_600_feasible']) for r in all_events if r['lot_planned_risk'] is not None]),
                    execution_verified=False)
                for h in alpha.HORIZONS:
                    complete=[r for r in all_events if r[f'status_{h}']=='complete']
                    result[f'complete_h{h}']=len(complete)
                    for metric in ('gross_R','net_R','MFE_R','MAE_R','p1_lower','p1_upper','p2_lower','p2_upper','ambiguous','p1_unresolved','p2_unresolved'):
                        result[f'{metric}_h{h}']=finite_mean([r[f'{metric}_{h}'] for r in complete])
                baseline=[r for r in mechanical_by[cid] if r['status_12']=='complete']
                result['mechanical_first_break_complete']=len(baseline);result['mechanical_first_break_net_R']=finite_mean([r['net_R_12'] for r in baseline])
                for rep,row in enumerate(match):
                    batch=[]
                    for i in np.flatnonzero(row>=0):
                        event=events[int(i)];control=controls[int(row[i])]
                        batch.append(dict(fold_id=fold['fold_id'],candidate_id=cid,event_id=event['event_id'],control_id=control['event_id'],
                            event_day=event['day'],control_day=control['day'],replicate=rep,primary_delta_R=event['net_R_12']-control['net_R_12']))
                    pairs.add(batch)
                bootrows.extend(dict(fold_id=fold['fold_id'],candidate_id=cid,replicate=i,effect_R=v) for i,v in enumerate(boot))
                fold_results.append(result)
                print('ALPHA',fold['fold_id'],cid,'n',len(events),'effect',result['effect'],flush=True)
            corrected=alpha.holm([r['p_value'] for r in fold_results])
            for r,p in zip(fold_results,corrected):
                r['holm_p']=float(p);r['decision']=gate(r,budget)
                history.append(dict(experiment_id=f"S3-{run.name}-{fold['fold_id']}-{r['candidate_id']}",fold_id=fold['fold_id'],candidate_id=r['candidate_id'],
                    status=r['decision'],feedback_count=1,result_artifact=str((run/'alpha_summary.json').relative_to(ROOT))))
            summaries.extend(fold_results)
            prereg.write_json(run/'alpha_summary.json',summaries)
            pd.DataFrame([{k:v for k,v in r.items() if k not in ('discovery_blocks','product_effects')} for r in summaries]).to_csv(run/'alpha_summary.csv',index=False)
            pd.DataFrame(history).to_csv(run/'experiment_registry.csv',index=False)
            del controls,by_stratum,pool,events_by,mechanical_by
        pd.DataFrame(bootrows).to_csv(run/'bootstrap_effects.csv',index=False)
    finally:
        db.close();accesses.close()
        for sink in sinks.values():sink.close()
        pairs.close()
    if digest(source)!=s2seal['database']['sha256']:raise ValueError('source changed during discovery evaluation')
    verify()
    prereg.write_json(run/'verification.json',dict(s3_computation_complete=True,implementation_frozen_before_real_results=True,
        actual_prefix_checks=prefix_checks,candidate_fold_evaluations=len(summaries),expected_evaluations=144,
        candidate_templates=24,full_wfo_reservations_used=0,selection_or_test_strategy_results_read=False,
        source_unchanged=True,table_row_counts={**{n:s.rows for n,s in sinks.items()},'matched_baseline':pairs.rows},
        decisions=dict(collections.Counter(r['decision'] for r in summaries)),execution_certified=False))
    exposures=[dict(fold_id=f['fold_id'],stage='S3',role='Researcher.discovery',start=f['discovery_start'],end_exclusive=f['discovery_end'],
        exposure='raw_alpha_and_matched_labels',feedback_round=1,selection_or_test_evaluation=False) for f in prereg.folds()]
    pd.DataFrame(exposures).to_csv(run/'evaluation_exposures.csv',index=False)
    prereg.write_json(run/'manifest.json',dict(run_id=run.name,created_utc=utc(),phase='S3',research_version='1.0',
        implementation_freeze_sha256=digest(BASE/'implementation_freeze.json'),s2_freeze_sha256=digest(S2/'freeze_manifest.json'),
        database_sha256=s2seal['database']['sha256'],source_code_sha256=seal['code_sha256'],
        hypotheses_unchanged=True,seed_matching=20261104,seed_bootstrap=20261105,full_wfo_evaluations=0,
        result_scope='discovery research only; prior history not independently unseen'))
    prereg.write_json(BASE/'latest_run.json',dict(path=str(run.relative_to(ROOT)),status='computation_complete'))
    print('S3 COMPLETE',run.name,dict(collections.Counter(r['decision'] for r in summaries)),flush=True)


def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['freeze','verify','run']);args=p.parse_args()
    if args.action=='freeze':freeze();return
    if args.action=='verify':verify();print('S3 frozen implementation verified');return
    verify();run=BASE/('run_'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ'));run.mkdir(parents=True,exist_ok=False)
    try:evaluate(run)
    except BaseException as exc:
        prereg.write_json(run/'failure.json',dict(created_utc=utc(),error=repr(exc),traceback=traceback.format_exc(),partial_results_preserved=True))
        raise


if __name__=='__main__':main()
