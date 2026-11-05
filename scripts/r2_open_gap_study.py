"""Frozen opening discontinuity event study. No strategy optimization or holdout API."""
import argparse
import datetime as dt
import json
import shutil
import subprocess
import sys
import sqlite3
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'.s1-deps'))
import numpy as np
import pandas as pd
import r1_diagnostics as core
import r1_route_a as prior
import s3_signals as prep
import s3_alpha as stats
import explore_v4_logic as money
from factor_study_data import Calendar,specifications,digest
from s1_market_regimes import fee_rule
BASE=ROOT/'artifacts/r2_open_gap'


def protocol():
    return dict(version='R2-GAP-1',question='Does a large observed day-open discontinuity predict continuation or closure after09:05, net of unchanged friction?',
        purpose='mechanism event study, not new executable strategy',prior_templates=82,prior_config_window_evaluations=260,
        prior_feedback='R1/A30/A60 costs and outcomes exposed; older project feedback unknown',
        periods=core.PERIODS,prohibited_strategy_outcomes_from=core.CUTOFF,
        gap='current09:00 open - immediately prior observed trading-day15:00 close, same concrete contract/segment; previous full45 valid day bars required',
        event='abs(gap)>=0.5*previous completed day high-low; zero range/gap rejected; threshold not fitted',
        signal='09:00-09:05 bar completed; gap direction fixed from09:00open, earliest reference entry next observed09:05 bar open',
        directions=['continuation','closure'],horizons=[6,12,24],primary_horizon=12,
        observations='no overnight positions; day-open gap includes intervening night trading where present, not isolated overnight news or settlement return',
        control='most recent earlier low-gap day within60 observed trading dates, abs(control gap)<0.1 prior-day range, same product/09:05 reference time, control prior-range/event prior-range in[.67,1.5]; no outcome availability used to choose; reuse allowed and clustered by original date',
        costs='fixed exchange20261022, open+close_today, adverse1tick/side and separately computed2tick/side at scenario fills; no ideal limit fills',
        risk='forecast reference: signal close+direction*tick, first5m adverse extreme±1tick stop, adverse exit tick and both fees; only diagnostic400/600 feasibility, no sample filtering and no simulated stop strategy',
        inference='1000 circular moving-block5 observed-date draws, all products jointly; control reuse contributions attributed to original control dates; seed20261105',
        budget='2 directional mechanism probes x3 horizons x2 exposed periods=12 descriptive outcome cells; four primary direction/period tests Holm corrected; zero executable strategy templates',
        gate=dict(minimum_complete_events=100,minimum_active_dates=60,minimum_matched_fraction=.8,
            primary_net_positive_both_periods=True,primary_net_CI_lower_positive=True,matched_effect_CI_lower_positive=True,
            primary_effect_Holm_p_max=.05,stress_primary_net_positive=True),
        next_step='only if a mechanism passes both periods may a separate executable rule be preregistered; no automatic holdout, deployment, stop/target search or extra gap thresholds',
        technical_limits='OHLC proxy; historical metadata/calendar/mapping limitations retained; same source only; no account equity or drawdown claim')


def freeze():
    BASE.mkdir(exist_ok=False)
    test=subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-p','test_r2_open_gap_study.py','-v'],capture_output=True,text=True)
    (BASE/'tests.txt').write_text(test.stdout+test.stderr,encoding='utf8')
    if test.returncode:raise ValueError('synthetic tests failed before results')
    prior.verify();assert digest(core.SOURCE)==core.EXPECTED
    paths=[Path(__file__),ROOT/'tests/test_r2_open_gap_study.py',ROOT/'scripts/r1_route_a.py',ROOT/'scripts/r1_diagnostics.py',ROOT/'scripts/s3_signals.py',ROOT/'scripts/s3_alpha.py',ROOT/'scripts/explore_v4_logic.py',ROOT/'scripts/explore_v3_signals.py',ROOT/'scripts/explore_v2_signals.py',ROOT/'scripts/s1_market_regimes.py',ROOT/'scripts/factor_study_data.py',ROOT/'scripts/factor-study-v1.json',ROOT/'scripts/exchange_fees_20261022.json',ROOT/'data/trading_calendar.json',ROOT/'crates/n-core/src/precision.rs',prior.BASE/'development/artifact_hashes.json',prior.BASE/'development/R1_route_A_report.md',ROOT/'registry/evaluation_exposures.csv']
    hashes={}
    for p in paths:
        rel=p.relative_to(ROOT);dst=BASE/'snapshot'/rel;dst.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,dst);hashes[str(rel)]=digest(p)
    core.write_json(BASE/'protocol.json',protocol())
    core.write_json(BASE/'freeze.json',dict(created_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
        git_sha=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),source_sha256=digest(core.SOURCE),
        input_code_sha256=hashes,protocol_sha256=digest(BASE/'protocol.json'),new_outcomes_seen_before_freeze=0,
        new_strategy_templates=0,known_templates=82,known_strategy_window_evaluations=260))
    print('R2 opening-gap protocol frozen before outcomes',flush=True)


def check():
    s=json.loads((BASE/'freeze.json').read_text(encoding='utf8'));assert digest(core.SOURCE)==s['source_sha256']
    assert digest(BASE/'protocol.json')==s['protocol_sha256']
    for name,sha in s['input_code_sha256'].items():
        assert digest(BASE/'snapshot'/name)==sha,name
        if name!='registry\\evaluation_exposures.csv':assert digest(ROOT/name)==sha,name
    return s


def days(f,all_dates):
    """Build only features available at first5m completion; future day bars not required."""
    out=[];previous=None;order={d:i for i,d in enumerate(all_dates)}
    for day,g in f.groupby('day',sort=True):
        first=g.iloc[0]
        if previous is not None and first.minute==540 and first.valid:
            prevday,pg=previous
            consecutive=order[day]==order[prevday]+1
            full=pg.minute.tolist()==prep.SLOTS.tolist() and bool(pg.valid.all()) and pg.segment.nunique()==1
            same=first.contract==pg.contract.iloc[-1] and first.segment==pg.segment.iloc[-1]
            width=float(pg.high.max()-pg.low.min())
            if consecutive and full and same and width>0:
                gap=float(first.open-pg.close.iloc[-1])
                out.append(dict(day=day,index=int(first.name),signal_ns=int(first.end_ns),gap=gap,prior_range=width,
                    gap_ratio=abs(gap)/width,gap_sign=int(np.sign(gap)),is_large=bool(abs(gap)>=.5*width),
                    is_control=bool(abs(gap)<.1*width),observed_day_index=order[day],calendar_known=bool(first.calendar_known),
                    prior_contract=str(pg.contract.iloc[-1]),current_contract=str(first.contract)))
        previous=(day,g)
    return out


def match(event,records):
    eligible=[q for q in records if q['day']<event['day'] and q['is_control'] and
        0<event['observed_day_index']-q['observed_day_index']<=60 and .67<=q['prior_range']/event['prior_range']<=1.5]
    return max(eligible,key=lambda q:q['day']) if eligible else None


def label(a,i,d,h,spec,fee,end):
    # Reuse the audited fixed-horizon cost algebra, not a strategy/stop executor.
    q=core.path_label(a,i,d,h,spec,fee,end)
    tick=spec['tick'];mult=spec['multiplier'];ref=float(a['close'][i]+d*tick)
    stop=float(a['low'][i]-tick if d==1 else a['high'][i]+tick)
    sf=stop-d*tick
    risk=(ref-sf)*d*mult+money.fee(fee['open'],ref,mult)+money.fee(fee['close_today'],sf,mult)
    q.update(forecast_risk_yuan=risk,forecast_budget400=bool(0<risk<=400),forecast_budget600=bool(0<risk<=600))
    if q['status']=='complete':
        z=slice(i+1,i+h+1);q['forecast_stop_touched']=bool(np.min(a['low'][z])<=stop if d==1 else np.max(a['high'][z])>=stop)
    else:q['forecast_stop_touched']=None
    return q


def boot(contributions):
    a=np.asarray(contributions,float);n=len(a);point=float(a.sum());rng=np.random.default_rng(20261105);values=[]
    if not n:return dict(point=None,lower=None,upper=None,p=1.)
    for _ in range(1000):
        starts=rng.integers(0,n,size=(n+4)//5);idx=np.concatenate([(s+np.arange(5))%n for s in starts])[:n]
        values.append(float(a[idx].sum()))
    lo,hi=np.quantile(values,[.025,.975]);p=(1+np.sum(np.asarray(values)-point>=point))/1001
    return dict(point=point,lower=float(lo),upper=float(hi),p=float(p))


def inference(g,date_list):
    q=g.loc[g.status=='complete'];valid=q.loc[q.control_status=='complete']
    net=q.groupby('day').net_yuan.mean();netvec=pd.Series(0.,index=date_list)
    if len(net):netvec.loc[net.index]=net/len(net)
    netboot=boot(netvec.to_numpy());effect=pd.Series(0.,index=date_list)
    if len(valid):
        counts=valid.groupby('day').size();nd=len(counts)
        for e in valid.to_dict('records'):
            w=1/(nd*counts[e['day']]);effect.loc[e['day']]+=w*e['net_yuan'];effect.loc[e['control_day']]-=w*e['control_net_yuan']
    eb=boot(effect.to_numpy())
    stress=q.groupby('day').net_2tick_yuan.mean().mean()
    return dict(event_count=len(g),complete_events=len(q),active_dates=q.day.nunique(),matched_events=len(valid),
        matched_fraction=len(valid)/len(q) if len(q) else 0.,calendar_known_fraction=g.calendar_known.mean(),
        gross_yuan=q.gross_yuan.mean(),cost_yuan=q.cost_yuan.mean(),net_yuan=q.net_yuan.mean(),
        gross_yuan_date_equal=q.groupby('day').gross_yuan.mean().mean(),cost_yuan_date_equal=q.groupby('day').cost_yuan.mean().mean(),
        net_yuan_date_equal=net.mean(),net_2tick_yuan_date_equal=stress,net_ci_lower=netboot['lower'],net_ci_upper=netboot['upper'],
        matched_effect=eb['point'],effect_ci_lower=eb['lower'],effect_ci_upper=eb['upper'],effect_p=eb['p'],
        matched_signal_net_date_equal=valid.groupby('day').net_yuan.mean().mean(),
        forecast_budget400_fraction=g.forecast_budget400.mean(),forecast_budget600_fraction=g.forecast_budget600.mean(),
        forecast_stop_touch_fraction=q.forecast_stop_touched.mean(),execution='fixed_horizon_proxy_no_stop_fills')


def run():
    check();out=BASE/'development';out.mkdir(exist_ok=False)
    meta=json.loads((ROOT/'scripts/factor-study-v1.json').read_text(encoding='utf8'));specs=specifications(meta)
    fee_snapshot=json.loads((ROOT/'scripts/exchange_fees_20261022.json').read_text(encoding='utf8'));cal=Calendar(json.loads((ROOT/'data/trading_calendar.json').read_text(encoding='utf8')),meta['night_ends'])
    db=sqlite3.connect(core.SOURCE.resolve().as_uri()+'?mode=ro',uri=True);db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
    events=[];coverage=[];prefixes=[];access=[];date_lists={};descriptors=[]
    try:
        for period,start,end in core.PERIODS:
            print('R2 preparing',period,flush=True);frames=prior.load(db,start,end,cal)
            all_dates=sorted(set().union(*(set(f.day) for f in frames.values())));date_lists[period]=all_dates
            cut='2024-05-08 11:00' if period=='D1' else '2025-05-08 11:00';short=prior.load(db,start,cut,cal)
            short_dates=sorted(set().union(*(set(f.day) for f in short.values())))
            for name in ['C','JD','PB']:
                expected=[q for q in days(frames[name],all_dates) if q['signal_ns']<core.stamp(cut)]
                actual=days(short[name],short_dates);assert expected==actual
                for e in actual:
                    assert match(e,actual)==match(e,days(frames[name],all_dates))
                prefixes.append(dict(period=period,product=name,events=len(actual),features_and_past_control_prefix_pass=True))
            for name,f in frames.items():
                spec=specs[name+'0'];fee=fee_rule(fee_snapshot,name);a={k:f[k].to_numpy() for k in f};records=days(f,all_dates)
                access.append(dict(period=period,product=name,rows=len(f),max_end_ns=int(f.end_ns.max()),end_exclusive=end))
                large=[e for e in records if e['is_large']]
                coverage.append(dict(period=period,product=name,observed_days=f.day.nunique(),usable_day_pairs=len(records),large_gap_events=len(large),
                    low_gap_control_dates=sum(q['is_control'] for q in records),night_session_proxy=meta['night_ends'][name+'0'] is not None))
                print('R2',period,name,'large gaps',len(large),flush=True)
                for e in records:descriptors.append(dict(period=period,product=name,**e))
                for e in large:
                    control=match(e,records)
                    for mechanism in ['continuation','closure']:
                        d=e['gap_sign']*(1 if mechanism=='continuation' else -1)
                        for horizon in [6,12,24]:
                            value=label(a,e['index'],d,horizon,spec,fee,core.stamp(end))
                            baseline=label(a,control['index'],d,horizon,spec,fee,core.stamp(end)) if control else dict(status='no_eligible_past_control',net_yuan=None)
                            events.append(dict(period=period,product=name,mechanism=mechanism,horizon=horizon,event_id=f'{period}:{name}:{e["signal_ns"]}',
                                day=e['day'],signal_ns=e['signal_ns'],gap=e['gap'],gap_ratio=e['gap_ratio'],side=d,calendar_known=e['calendar_known'],
                                control_day=control['day'] if control else None,control_status=baseline['status'],control_net_yuan=baseline['net_yuan'],
                                control_range_ratio=control['prior_range']/e['prior_range'] if control else None,**value))
    finally:db.close()
    df=pd.DataFrame(events);df.to_parquet(out/'events.parquet',index=False)
    pd.DataFrame(descriptors).to_csv(out/'opening_descriptors.csv',index=False);pd.DataFrame(coverage).to_csv(out/'coverage.csv',index=False)
    pd.DataFrame(access).to_csv(out/'data_access.csv',index=False);core.write_json(out/'prefix_checks.json',prefixes)
    summary=[]
    for (period,mechanism,horizon),g in df.groupby(['period','mechanism','horizon']):
        summary.append(dict(period=period,mechanism=mechanism,horizon=horizon,**inference(g,date_lists[period])))
    summary=pd.DataFrame(summary);summary['primary']=summary.horizon==12;summary['holm_primary_p']=np.nan
    idx=summary.index[summary.primary];summary.loc[idx,'holm_primary_p']=stats.holm(summary.loc[idx,'effect_p'].to_numpy())
    summary.to_csv(out/'mechanism_comparison.csv',index=False)
    product=[]
    for key,g in df.groupby(['period','mechanism','horizon','product']):
        q=g.loc[g.status=='complete'];product.append(dict(zip(['period','mechanism','horizon','product'],key))|dict(events=len(g),complete_events=len(q),dates=q.day.nunique(),gross_yuan=q.gross_yuan.mean(),cost_yuan=q.cost_yuan.mean(),net_yuan=q.net_yuan.mean(),net_date_equal=q.groupby('day').net_yuan.mean().mean(),matched_events=int((q.control_status=='complete').sum())))
    pd.DataFrame(product).to_csv(out/'by_product.csv',index=False)
    control=df.drop_duplicates(['period','event_id']).groupby(['period','product','control_day'],dropna=False).size().rename('event_reuses').reset_index()
    control.to_csv(out/'control_reuse.csv',index=False)
    decisions=[]
    for mechanism in ['continuation','closure']:
        q=summary.loc[(summary.mechanism==mechanism)&summary.primary]
        passed=bool((q.complete_events>=100).all() and (q.active_dates>=60).all() and (q.matched_fraction>=.8).all() and
            (q.net_yuan_date_equal>0).all() and (q.net_2tick_yuan_date_equal>0).all() and (q.net_ci_lower>0).all() and
            (q.effect_ci_lower>0).all() and (q.holm_primary_p<=.05).all())
        decisions.append(dict(mechanism=mechanism,passed_both_periods=passed,decision='eligible_to_preregister_executable_hypothesis' if passed else 'not_supported_for_strategy_development'))
    assert len(summary)==12;assert all(r['max_end_ns']<core.stamp(core.CUTOFF) for r in access)
    core.write_json(out/'verification.json',dict(decisions=decisions,reserved_outcomes_read=False,new_strategy_templates=0,
        cumulative_strategy_templates=82,cumulative_strategy_window_evaluations=260,new_mechanism_direction_horizon_period_cells=12,
        primary_tests_Holm_corrected=4,prefix_checks=prefixes,source_sha256=digest(core.SOURCE),
        shared_account_or_deployment_certified=False))
    exposures=[]
    for row in summary.to_dict('records'):
        period,start,end=next(p for p in core.PERIODS if p[0]==row['period'])
        exposures.append(dict(version='R2-GAP-1',candidate_id='mechanism_'+row['mechanism'],period=period,start=start,end_exclusive=end,
            horizon=row['horizon'],result_seen=True,classification='EXPOSED_DEVELOPMENT',exposure_type='mechanism_event_study',
            reason='User requested next study after route A failed; preregistered gap-response mechanism, no gap-threshold search',source='artifacts/r2_open_gap/development/mechanism_comparison.csv'))
    pd.DataFrame(exposures).to_csv(out/'evaluation_exposures.csv',index=False)
    reg=ROOT/'registry/evaluation_exposures.csv';prev=pd.read_csv(reg)
    if (prev.version=='R2-GAP-1').any():raise ValueError('duplicate R2 exposure')
    pd.concat([prev,pd.DataFrame(exposures)],ignore_index=True).to_csv(reg,index=False)
    print(summary.to_string(index=False));print(decisions,flush=True)


def seal():
    check();out=BASE/'development';core.write_json(out/'artifact_hashes.json',{p.name:digest(p) for p in out.iterdir() if p.is_file() and p.name!='artifact_hashes.json'})


def verify():
    check();out=BASE/'development'
    for name,sha in json.loads((out/'artifact_hashes.json').read_text(encoding='utf8')).items():assert digest(out/name)==sha,name
    print('R2 source/data/output hashes passed')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['freeze','run','seal','verify']);args=p.parse_args();globals()[args.action]()
