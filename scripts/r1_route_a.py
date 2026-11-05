"""Two preregistered larger-scale structures; exposed D1/D2 only."""
import argparse
import datetime as dt
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'.s1-deps'))
import numpy as np
import pandas as pd
import r1_diagnostics as old
import s3_signals as prep
import explore_v4_logic as execution
from factor_study_data import digest, Calendar, specifications
from s1_market_regimes import fee_rule, select_main

BASE=ROOT/'artifacts/r1_route_a'


def protocol():
    return dict(version='R1-A1',prior_feedback='80 templates/256 evaluations plus R1 diagnostics; older history unknown',
        templates=[dict(id='A30',minutes=30),dict(id='A60',minutes=60)],periods=old.PERIODS,
        history_role='EXPOSED_DEVELOPMENT',prohibited_outcomes_from=old.CUTOFF,
        structure='two latest completed exact clock buckets in same concrete-contract segment: higher high and low with current close>prior close and current close>open => long; exact reverse => short',
        lookback='previous bucket may be prior day; actual overnight price changes are observed features, never held positions or stitched profits',
        confirmation='only first immediately adjacent complete5m after structure end; aligned body and close strictly beyond structure close, else cancel without retry',
        entry='next observed complete positive-volume5m open, same day/segment; structure+confirmation known before fill',
        stop='latest completed structure adverse extreme plus1tick, fixed; no target or trailing stop',
        holding='24 active5m bars (120 active minutes), fixed; force flat at15:00; reject beforehand if fewer24 scheduled day bars remain',
        overlap='one planned24-bar slot per product/template from accepted entry; reserve whole slot even after early stop; failed unknown paths reserve to day end',
        cost='2026-10-22 exchange snapshot, separate open/today/yesterday; primary1tick/side and independently replayed2tick/side',
        budget=400,max_lot_risk=600,capital_reference=50000,
        simple_baseline='same signal times; symmetric long/short, projected same signal-time stop distance; no future entry geometry used; diagnostic directional reference, not independent control experiment',
        economic_gate=dict(minimum_complete_trades=100,minimum_active_dates=60,each_period_date_equal_net_positive=True,
            each_period_2tick_date_equal_net_positive=True,each_period_date_bootstrap_95_lower_positive=True,
            each_period_paired_baseline_effect_positive=True,minimum_paired_baseline_coverage=.8,unknown_accepted_paths=0),
        inference='1000 moving-block5-observed-date resamples, all products jointly by source day, seed20261105; descriptive intervals, no independent alpha claim',
        selection='both rules must be reported; any future tie chooses A30 first, no return ranking; no parameter tuning after feedback',
        execution_readiness='NOT_READY: BU/JD/SH historical specs, dated margin/fees/calendar, mapping availability and shared-account integration unresolved',
        holdout='no reserved period implementation in this script; economics plus execution readiness and new frozen acceptance procedure required',
        no_parameter_grid=True,no_shared_account_claim=True)


def signals(f,minutes,tick):
    h=prep.aggregate(f,minutes).to_dict('records');out=[]
    for p,b in zip(h,h[1:]):
        if p['segment']!=b['segment']:continue
        d=0
        if b['high']>p['high'] and b['low']>p['low'] and b['close']>p['close'] and b['close']>b['open']:d=1
        elif b['high']<p['high'] and b['low']<p['low'] and b['close']<p['close'] and b['close']<b['open']:d=-1
        if not d:continue
        # Emit the known structure even if its following confirmation is not present yet.
        i=int(b['end_i']);out.append(dict(structure_index=i,structure_end_ns=int(b['end_ns']),side=d,
            stop=float(b['low']-tick if d==1 else b['high']+tick),structure_close=float(b['close']),
            structure_high=float(b['high']),structure_low=float(b['low']),prior_structure_end_ns=int(p['end_ns'])))
    return out


def confirmation(a,q):
    i=q['structure_index']+1;b=q['structure_index'];d=q['side']
    if i>=len(a['day']):return None,'confirmation_not_observed'
    if not a['valid'][i] or a['segment'][i]!=a['segment'][b] or a['day'][i]!=a['day'][b] or a['start_ns'][i]!=q['structure_end_ns']:
        return None,'confirmation_gap_day_or_invalid'
    if (a['close'][i]-a['open'][i])*d<=0 or (a['close'][i]-q['structure_close'])*d<=0:return None,'confirmation_failed'
    return i,'confirmed'


def trade(a,i,d,stop,spec,fee,end,slip=1):
    """Fixed-stop/time exit one-lot path. Missing accepted outcomes remain unknown."""
    r=dict(status='rejected',reason=None,entry_ns=None,exit_ns=None,entry=None,exit=None,
        plan_risk_yuan=None,gross_yuan=None,slippage_yuan=None,fees_yuan=None,net_yuan=None,
        held_bars=None,MFE_yuan=None,MAE_yuan=None,slot_end_index=None)
    j=i+1;n=len(a['day']);tick=spec['tick'];mult=spec['multiplier']
    # Eligibility derives from known session schedule, not whether future bars happened to exist.
    remaining=int(np.sum(prep.SLOTS>=a['minute'][i]+5))
    if remaining<24:r['reason']='insufficient_scheduled_holding_time';return r
    if j>=n:r['reason']='no_next_bar';return r
    if a['day'][j]!=a['day'][i] or a['segment'][j]!=a['segment'][i] or not a['valid'][j] or a['volume'][j]<=0:
        r['reason']='entry_gap_or_unknown';return r
    ref=float(a['open'][j]);entry=ref+d*slip*tick;stop=execution.round_price(stop,tick,d==-1);stopfill=stop-d*slip*tick
    risk=(entry-stopfill)*d*mult+execution.fee(fee['open'],entry,mult)+execution.fee(fee['close_today'],stopfill,mult)
    r['plan_risk_yuan']=float(risk)
    if (entry-stop)*d<=0:r['reason']='entry_beyond_stop';return r
    if risk>400:r['reason']='one_lot_budget';return r
    r.update(status='unknown',entry_ns=int(a['start_ns'][j]),entry=entry,reason='unfinished_path',slot_end_index=j+23)
    mfe=mae=0.
    for k in range(j,min(n,j+24)):
        if a['day'][k]!=a['day'][i] or a['segment'][k]!=a['segment'][i] or not a['valid'][k] or a['volume'][k]<=0 or a['end_ns'][k]>=end:
            r['reason']='missing_or_boundary_path';return r
        op,hi,lo,cl=[float(a[c][k]) for c in ('open','high','low','close')]
        stopped=(lo<=stop if d==1 else hi>=stop)
        # Include exit-bar extrema only as OHLC path bounds, never achievable exit optimization.
        mfe=max(mfe,(hi-ref)*d*mult if d==1 else (lo-ref)*d*mult)
        mae=max(mae,(ref-lo)*mult if d==1 else (hi-ref)*mult)
        if (op-stop)*d<=0:base=op;reason='gap_stop';exit_ns=int(a['start_ns'][k])
        elif stopped:base=stop;reason='stop';exit_ns=int(a['end_ns'][k])
        elif k==j+23 or a['minute'][k]==895:base=cl;reason='time_or_day_close';exit_ns=int(a['end_ns'][k])
        else:continue
        price=base-d*slip*tick;fees=execution.fee(fee['open'],entry,mult)+execution.fee(fee['close_today'],price,mult)
        gross=(base-ref)*d*mult;friction=2*slip*tick*mult
        r.update(status='complete',reason=reason,exit_ns=exit_ns,exit=price,gross_yuan=gross,
            slippage_yuan=friction,fees_yuan=fees,net_yuan=gross-friction-fees,held_bars=k-j+1,
            MFE_yuan=mfe,MAE_yuan=mae)
        return r
    return r


def freeze():
    BASE.mkdir(exist_ok=False)
    checks=[]
    for pattern in ['test_r1_route_a.py']:
        p=subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-p',pattern,'-v'],capture_output=True,text=True)
        checks.append(p.stdout+p.stderr)
        (BASE/'tests_before_results.txt').write_text('\n'.join(checks),encoding='utf8')
        if p.returncode:raise ValueError('synthetic regression failed; stop before results')
    assert digest(old.SOURCE)==old.EXPECTED
    old.verify()
    files=[Path(__file__),ROOT/'tests/test_r1_route_a.py',ROOT/'scripts/r1_diagnostics.py',ROOT/'scripts/s3_signals.py',ROOT/'scripts/explore_v4_logic.py',ROOT/'scripts/factor_study_data.py',ROOT/'scripts/s1_market_regimes.py',ROOT/'scripts/explore_v3_signals.py',ROOT/'scripts/explore_v2_signals.py',ROOT/'scripts/factor-study-v1.json',ROOT/'scripts/exchange_fees_20261022.json',ROOT/'crates/n-core/src/precision.rs',ROOT/'data/trading_calendar.json',old.BASE/'diagnostics/artifact_hashes.json',old.BASE/'diagnostics/R1_verdict.md']
    snapshot=BASE/'snapshot';snapshot.mkdir();hashes={}
    for p in files:
        rel=p.relative_to(ROOT);dst=snapshot/rel;dst.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,dst);hashes[str(rel)]=digest(p)
    old.write_json(BASE/'protocol.json',protocol())
    old.write_json(BASE/'freeze.json',dict(created_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
        git_sha=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),data_sha256=digest(old.SOURCE),
        source_sha256=hashes,protocol_sha256=digest(BASE/'protocol.json'),strategy_outcomes_before_freeze=0,
        prior_template_count=80,new_template_count=2,total_template_count=82,prior_config_window_evaluations=256,
        planned_new_config_window_evaluations=4,holdout_accessed=False))
    print('A30/A60 frozen, zero new strategy outcomes viewed',flush=True)


def verify_freeze():
    f=json.loads((BASE/'freeze.json').read_text(encoding='utf8'))
    assert digest(old.SOURCE)==f['data_sha256']
    assert digest(BASE/'protocol.json')==f['protocol_sha256']
    for name,sha in f['source_sha256'].items():
        assert digest(ROOT/name)==sha,name
        assert digest(BASE/'snapshot'/name)==sha,name
    return f


def load(db,start,end,cal):
    if end>old.CUTOFF or start<'2023-11-01':raise ValueError('reserved-period access prohibited')
    maps=pd.read_sql_query('select product,effective_date,contract from mappings where effective_date<? order by product,effective_date',db,params=(end,))
    frames={}
    for product,mp in maps.groupby('product',sort=True):
        name=product.split('.')[1].upper();parts=[]
        for contract in mp.contract.unique():
            q=pd.read_sql_query('select '+','.join(old.RAW)+' from bars where contract=? and period_seconds=300 and start_ns>=? and end_ns<? order by start_ns',db,params=(contract,old.stamp(start),old.stamp(end)))
            if len(q):parts.append(q)
        if parts:frames[name]=prep.prepare(select_main(pd.concat(parts,ignore_index=True),mp),cal)
    return frames


def interval(values):
    x=np.asarray(values,float);n=len(x)
    if not n:return None,None
    rng=np.random.default_rng(20261105);means=[]
    for _ in range(1000):
        starts=rng.integers(0,n,size=(n+4)//5);ix=np.concatenate([(s+np.arange(5))%n for s in starts])[:n];means.append(np.mean(x[ix]))
    return [float(v) for v in np.quantile(means,[.025,.975])]


def run():
    verify_freeze();out=BASE/'development';out.mkdir(exist_ok=False)
    meta=json.loads((ROOT/'scripts/factor-study-v1.json').read_text(encoding='utf8'));specs=specifications(meta)
    fees=json.loads((ROOT/'scripts/exchange_fees_20261022.json').read_text(encoding='utf8'));cal=Calendar(json.loads((ROOT/'data/trading_calendar.json').read_text(encoding='utf8')),meta['night_ends'])
    db=sqlite3.connect(old.SOURCE.resolve().as_uri()+'?mode=ro',uri=True);db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
    ledger=[];dispositions=[];baseline=[];prefixes=[];access=[];summaries=[]
    try:
        for period,start,end in old.PERIODS:
            print('Route A preparing',period,flush=True);frames=load(db,start,end,cal)
            cut='2024-05-08 11:00' if period=='D1' else '2025-05-08 11:00';prefix=load(db,start,cut,cal)
            for name in ['C','JD','PB']:
                full=frames[name];short=prefix[name];pd.testing.assert_frame_equal(full.loc[full.end_ns<old.stamp(cut)].reset_index(drop=True),short)
                for m in [30,60]:assert [q for q in signals(full,m,specs[name+'0']['tick']) if q['structure_end_ns']<old.stamp(cut)]==signals(short,m,specs[name+'0']['tick'])
                prefixes.append(dict(period=period,product=name,rows=len(short),rules=2,passed=True))
            for name,f in frames.items():
                print('Route A',period,name,flush=True);a={k:f[k].to_numpy() for k in f};spec=specs[name+'0'];fee=fee_rule(fees,name)
                access.append(dict(period=period,product=name,rows=len(f),max_end_ns=int(f.end_ns.max()),end_exclusive=end))
                for minutes in [30,60]:
                    cs=signals(f,minutes,spec['tick'])
                    for slip in [1,2]:
                        reserved=-1
                        for q in cs:
                            i,status=confirmation(a,q);eid=f'{period}:A{minutes}:{name}:{q["structure_end_ns"]}'
                            common=dict(period=period,candidate_id=f'A{minutes}',product=name,event_id=eid,slip_ticks=slip,structure_ns=q['structure_end_ns'],day=str(a['day'][q['structure_index']]),side=q['side'])
                            if i is None:dispositions.append(dict(**common,reason=status));continue
                            common['signal_ns']=int(a['end_ns'][i])
                            if i+1<=reserved:dispositions.append(dict(**common,reason='reserved_nonoverlap_slot'));continue
                            r=trade(a,i,q['side'],q['stop'],spec,fee,old.stamp(end),slip)
                            dispositions.append(dict(**common,reason=r['reason'],status=r['status'],plan_risk_yuan=r['plan_risk_yuan']))
                            if r['status']=='rejected':continue
                            reserved=r['slot_end_index'] if r['status']=='complete' else int(np.flatnonzero(a['day']==a['day'][i])[-1])
                            ledger.append(dict(**common,**r))
                            # Geometry fixed at signal close, never derived from future actual entry.
                            distance=(float(a['close'][i])+q['side']*slip*spec['tick']-q['stop'])*q['side']
                            for d in [1,-1]:
                                ref=float(a['close'][i])+d*slip*spec['tick'];stop=ref-d*distance
                                b=trade(a,i,d,stop,spec,fee,old.stamp(end),slip)
                                baseline.append(dict(**{k:v for k,v in common.items() if k!='side'},side=d,**b))
            pd.DataFrame(ledger).to_parquet(out/'trades.parquet',index=False)
            pd.DataFrame(dispositions).to_csv(out/'setup_dispositions.csv',index=False)
    finally:db.close()
    tr=pd.DataFrame(ledger);bs=pd.DataFrame(baseline);ds=pd.DataFrame(dispositions)
    tr.to_csv(out/'trades.csv',index=False);bs.to_parquet(out/'baseline.parquet',index=False)
    pd.DataFrame(access).to_csv(out/'data_access.csv',index=False);old.write_json(out/'prefix_checks.json',prefixes)
    daily=[]
    for period,start,end in old.PERIODS:
        for candidate in ['A30','A60']:
            for slip in [1,2]:
                x=tr.loc[(tr.period==period)&(tr.candidate_id==candidate)&(tr.slip_ticks==slip)];q=x.loc[x.status=='complete']
                d=q.groupby('day').net_yuan.mean();lower,upper=interval(d.to_numpy())
                pair=bs.loc[(bs.period==period)&(bs.candidate_id==candidate)&(bs.slip_ticks==slip)]
                complete_ids=pair.loc[pair.status=='complete'].groupby('event_id').size();ids=complete_ids.loc[complete_ids==2].index
                match=q.loc[q.event_id.isin(ids)].set_index('event_id');ref=pair.loc[pair.event_id.isin(match.index)].groupby('event_id').net_yuan.mean()
                delta=match.net_yuan-ref;delta_dates=delta.groupby(match.day).mean()
                row=dict(period=period,candidate_id=candidate,slip_ticks=slip,accepted=len(x),complete_trades=len(q),unknown_paths=int((x.status=='unknown').sum()),active_dates=len(d),
                    net_yuan=q.net_yuan.mean(),net_yuan_date_equal=d.mean(),net_sum_yuan=q.net_yuan.sum(),gross_yuan=q.gross_yuan.mean(),
                    fees_yuan=q.fees_yuan.mean(),slippage_yuan=q.slippage_yuan.mean(),ci_lower=lower,ci_upper=upper,
                    paired_events=len(match),paired_effect_date_equal=delta_dates.mean(),matched_signal_net_date_equal=match.groupby('day').net_yuan.mean().mean(),
                    shared_account_MDD='not_computed',execution_readiness='not_ready',independent_unseen=False)
                summaries.append(row)
                for day,v in d.items():daily.append(dict(period=period,candidate_id=candidate,slip_ticks=slip,day=day,event_mean_net=v,closed_net=q.loc[q.day==day,'net_yuan'].sum()))
    summary=pd.DataFrame(summaries);summary.to_csv(out/'candidate_comparison.csv',index=False);pd.DataFrame(daily).to_csv(out/'daily_closed_pnl.csv',index=False)
    ds.groupby(['period','candidate_id','slip_ticks','reason']).size().rename('count').reset_index().to_csv(out/'opportunity_funnel.csv',index=False)
    decisions=[]
    for c in ['A30','A60']:
        q=summary.loc[summary.candidate_id==c];primary=q.loc[q.slip_ticks==1]
        passed=bool((primary.complete_trades>=100).all() and (primary.active_dates>=60).all() and (q.net_yuan_date_equal>0).all() and (primary.ci_lower>0).all() and (primary.paired_effect_date_equal>0).all() and (primary.paired_events>=.8*primary.complete_trades).all() and q.unknown_paths.sum()==0)
        decisions.append(dict(candidate_id=c,economic_gate_pass=passed,execution_readiness=False,holdout_authorized_by_frozen_gate=False,
            decision='economics_pass_execution_blocked' if passed else 'not_passed_development_economic_gate'))
    assert len(summary)==8 and all(r['max_end_ns']<old.stamp(old.CUTOFF) for r in access)
    complete=tr.loc[tr.status=='complete'];assert np.allclose(complete.gross_yuan-complete.slippage_yuan-complete.fees_yuan,complete.net_yuan)
    old.write_json(out/'verification.json',dict(prefix_checks=prefixes,decisions=decisions,new_templates=2,new_config_window_evaluations=4,
        cumulative_templates=82,cumulative_config_window_evaluations=260,stress_scenarios_separately_recorded=4,
        baseline='symmetric geometry control, not new searched strategies',reserved_outcomes_read=False,shared_account_certified=False,
        max_data_end_ns=max(r['max_end_ns'] for r in access)))
    # Append only after final computation; avoid claiming unfinished evaluations complete.
    exp=[]
    for c in ['A30','A60']:
        for p,start,end in old.PERIODS:exp.append(dict(version='R1-A1',candidate_id=c,period=p,start=start,end_exclusive=end,result_seen=True,classification='EXPOSED_DEVELOPMENT',exposure_type='historical_strategy_evaluation',reason='User continued research; two preregistered larger structures; no outcome tuning',source='artifacts/r1_route_a/development/candidate_comparison.csv'))
    pd.DataFrame(exp).to_csv(out/'evaluation_exposures.csv',index=False)
    registry=ROOT/'registry/evaluation_exposures.csv';prev=pd.read_csv(registry)
    if (prev.version=='R1-A1').any():raise ValueError('duplicate feedback run')
    pd.concat([prev,pd.DataFrame(exp)],ignore_index=True).to_csv(registry,index=False)
    print(summary.to_string(index=False));print(decisions,flush=True)


def seal():
    verify_freeze();out=BASE/'development'
    old.write_json(out/'artifact_hashes.json',{p.name:digest(p) for p in out.iterdir() if p.is_file() and p.name!='artifact_hashes.json'})


def verify():
    verify_freeze();out=BASE/'development'
    for name,sha in json.loads((out/'artifact_hashes.json').read_text(encoding='utf8')).items():assert digest(out/name)==sha,name
    print('Route A source/data/output checks passed')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['freeze','run','seal','verify']);args=p.parse_args();globals()[args.action]()
