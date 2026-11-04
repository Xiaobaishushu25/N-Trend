"""S1: causal market descriptors only. No strategy search or trade simulation.

Run from the repository root: python scripts/s1_market_regimes.py
"""
import argparse
import collections
import datetime as dt
import importlib.metadata
import json
import math
from pathlib import Path
import sqlite3
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
if (ROOT/'.s1-deps').exists():sys.path.insert(0,str(ROOT/'.s1-deps'))
import numpy as np
import pandas as pd
from factor_study_data import Calendar, digest, local

NS=1_000_000_000
BAR=300*NS


def save_json(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf8')


def select_main(frame,mapping):
    """Conservative publication convention; future mapping rows never select earlier bars."""
    frame=frame.sort_values('start_ns').copy()
    dates=pd.to_datetime(frame.start_ns,utc=True).dt.tz_convert('Asia/Shanghai').dt.strftime('%Y-%m-%d').to_numpy()
    m=mapping.sort_values('effective_date')
    index=np.searchsorted(m.effective_date.to_numpy(),dates,side='left')-1
    active=(index>=0)&(m.contract.to_numpy()[np.maximum(index,0)]==frame.contract.to_numpy())
    chosen=frame.loc[active].copy().reset_index(drop=True)
    if chosen.start_ns.duplicated().any():raise ValueError('multiple selected main contracts at same time')
    return chosen


def bar_features(frame,er_bars=12,atr_bars=12):
    """Only data at or before end_ns. Every gap/contract/invalid row breaks history."""
    f=frame.sort_values('start_ns').reset_index(drop=True).copy()
    values=f[['open','high','low','close','volume']].to_numpy(float)
    valid=(np.isfinite(values).all(axis=1)&(f.low>0)&(f.low<=f[['open','close']].min(axis=1))&
           (f.high>=f[['open','close']].max(axis=1))&(f.volume>=0)&
           (f.end_ns-f.start_ns==BAR)&(f.constituent_count==5)&(f.complete==1)&(f.start_ns%BAR==0))
    f['valid_bar']=valid
    f['history_segment']=((f.contract!=f.contract.shift())|(f.start_ns!=f.end_ns.shift())|~valid|~valid.shift(fill_value=False)).cumsum()
    f['asof_ns']=f.end_ns
    names=['er','atr_pct','realized_vol_12','bar_return','intrabar_return','gap_pct','volatility_expansion']
    for name in names:f[name]=np.nan
    for _,g in f.loc[valid].groupby('history_segment',sort=False):
        close=g.close;prev=close.shift();delta=close.diff()
        tr=pd.concat([g.high-g.low,(g.high-prev).abs(),(g.low-prev).abs()],axis=1).max(axis=1)
        path=delta.abs().rolling(er_bars,min_periods=er_bars).sum()
        er=(close-close.shift(er_bars)).abs()/path.replace(0,np.nan)
        er=er.mask(path==0,0)
        atr=tr.rolling(atr_bars,min_periods=atr_bars).mean()/close
        ret=np.log(close/prev)
        f.loc[g.index,'er']=er
        f.loc[g.index,'atr_pct']=atr
        f.loc[g.index,'realized_vol_12']=np.sqrt(ret.pow(2).rolling(er_bars,min_periods=er_bars).sum())
        f.loc[g.index,'bar_return']=ret
        f.loc[g.index,'intrabar_return']=np.log(close/g.open)
        f.loc[g.index,'gap_pct']=g.open/prev-1
        f.loc[g.index,'volatility_expansion']=atr/atr.shift().rolling(12,min_periods=12).mean()
    # Market return descriptors omit overnight/roll gaps; they are never strategy PnL.
    f['roll_neutral_log_return']=f.bar_return.fillna(f.intrabar_return).where(valid)
    return f


def label_calendar(frame,calendar,symbol):
    f=frame.copy();times=pd.to_datetime(f.start_ns,utc=True).dt.tz_convert('Asia/Shanghai')
    f['natural_date']=times.dt.strftime('%Y-%m-%d')
    f['time_slot']=times.dt.strftime('%H:%M')
    f['year']=times.dt.year;f['quarter']=times.dt.tz_localize(None).dt.to_period('Q').astype(str)
    f['session']=np.where((times.dt.hour>=18)|(times.dt.hour<6),'night',
        np.where(times.dt.hour<12,'morning','afternoon'))
    days=[];status=[]
    for time in times:
        t=time.to_pydatetime().replace(tzinfo=None)
        try:
            if not calendar.expected(t,symbol):days.append('');status.append('outside_session_template');continue
            d=calendar.day(t)
            if t.date().isoformat() not in calendar.days:raise ValueError('calendar unknown')
            days.append(d);status.append('calendar_covered')
        except (ValueError,KeyError):days.append('');status.append('calendar_unknown')
    f['trading_day']=days;f['calendar_status']=status
    return f


def expected_day_starts(calendar,day,symbol):
    """Frozen session template plus covered exchange-day calendar; no inferred holidays."""
    d=dt.date.fromisoformat(day);times=[]
    if not calendar.is_day(d):return []
    for a,b in ((540,615),(630,690),(810,900)):
        for m in range(a,b,5):times.append(dt.datetime.combine(d,dt.time())+dt.timedelta(minutes=m))
    index=calendar.trading.index(day)
    if index==0:raise ValueError('prior trading date unavailable')
    previous=dt.date.fromisoformat(calendar.trading[index-1]);night_end=calendar.night_ends[symbol]
    if night_end is not None and calendar.has_night(previous):
        end=night_end if night_end>=1260 else night_end+1440
        for m in range(1260,end,5):times.append(dt.datetime.combine(previous,dt.time())+dt.timedelta(minutes=m))
    return sorted(int(t.replace(tzinfo=dt.timezone(dt.timedelta(hours=8))).timestamp())*NS for t in times)


def daily_context(frame,calendar,symbol,past_days=20,volume_min=5):
    """Past complete trading-day context; current day's final totals are never features."""
    f=frame.copy();known=f.loc[(f.trading_day!='')&f.valid_bar].copy()
    columns=['past20_return','past20_direction','past_day_rv','past20_rv_mean','past_day_rv_percentile20',
             'volume_relative_past20_same_slot','intraday_realized_vol_so_far']
    for c in columns:f[c]=np.nan
    if known.empty:return f,pd.DataFrame()
    calendar_days=[d for d in calendar.trading if known.trading_day.min()<=d<=known.trading_day.max()]
    groups=[]
    for day,g in known.groupby('trading_day',sort=True):
        try:complete=sorted(g.start_ns.tolist())==expected_day_starts(calendar,day,symbol)
        except ValueError:complete=False
        # Daily summaries are retrospective descriptors; only lagged values become features.
        groups.append(dict(trading_day=day,day_complete=complete,contracts=g.contract.nunique(),bars=len(g),
            realized_log_return=g.roll_neutral_log_return.sum(),realized_vol=math.sqrt(g.roll_neutral_log_return.pow(2).sum()),
            intraday_range_pct=((g.high-g.low)/g.close).median(),volume=g.volume.sum()))
    daily=pd.DataFrame(groups).set_index('trading_day').reindex(calendar_days)
    daily['day_complete']=daily.day_complete.fillna(False).astype(bool)
    good=daily.day_complete
    rv=daily.realized_vol.where(good);returns=daily.realized_log_return.where(good)
    # Drop missing days into the calendar grid; never compress a 20-day window across them.
    daily['past20_return']=returns.shift().rolling(past_days,min_periods=past_days).sum()
    daily['past20_direction']=np.sign(daily.past20_return)
    daily['past_day_rv']=rv.shift()
    daily['past20_rv_mean']=rv.shift().rolling(past_days,min_periods=past_days).mean()
    rank=rv.rolling(past_days+1,min_periods=past_days+1).apply(lambda a:np.mean(a[:-1]<=a[-1]),raw=True)
    daily['past_day_rv_percentile20']=rank.shift()
    for c in columns[:5]:f[c]=f.trading_day.map(daily[c])
    # Only previous days at this same clock slot. No today's totals in the baseline.
    pivot=known.pivot(index='trading_day',columns='time_slot',values='volume').reindex(calendar_days)
    past=pivot.shift().rolling(past_days,min_periods=volume_min).mean()
    for slot,g in known.groupby('time_slot'):
        baseline=g.trading_day.map(past[slot])
        f.loc[g.index,'volume_relative_past20_same_slot']=g.volume/baseline.replace(0,np.nan)
    cum=known.roll_neutral_log_return.pow(2).groupby(known.trading_day).cumsum().pow(.5)
    f.loc[known.index,'intraday_realized_vol_so_far']=cum
    return f,daily.reset_index()


def freeze_thresholds(frame,freeze_ns,er_quantile=.67,atr_quantile=.75,minimum=1000):
    d=frame.loc[(frame.asof_ns<freeze_ns)&frame.valid_bar,['er','atr_pct']].dropna()
    if len(d)<minimum:raise ValueError('insufficient discovery calibration bars')
    return dict(er=float(d.er.quantile(er_quantile)),atr_pct=float(d.atr_pct.quantile(atr_quantile)),
                available_at_ns=int(freeze_ns),calibration_rows=len(d),maximum_source_asof_ns=int(frame.loc[d.index,'asof_ns'].max()))


def classify(frame,threshold):
    f=frame.copy();f['market_state']='warmup_or_invalid'
    ready=f.er.notna()&f.atr_pct.notna()&f.valid_bar
    after=f.asof_ns>=threshold['available_at_ns']
    f.loc[ready&~after,'market_state']='calibration_only'
    f.loc[ready&after,'market_state']='range'
    f.loc[ready&after&(f.er>=threshold['er']),'market_state']='directional'
    f.loc[ready&after&(f.atr_pct>=threshold['atr_pct']),'market_state']='high_volatility'
    return f


def fee_rule(snapshot,product):
    p=snapshot['products'][product]
    return p.get('published_schedule_before_override',p)


def add_cost_proxy(f,product,specs,snapshot):
    """Fee-only amplitude floor; excludes user SR override, not a trading return."""
    s=specs[product+'0'];rule=fee_rule(snapshot,product)
    opening=rule['open']['fixed_yuan_per_lot']+f.close*s['multiplier']*rule['open']['notional_rate']
    closing=rule['close_today']['fixed_yuan_per_lot']+f.close*s['multiplier']*rule['close_today']['notional_rate']
    f['fee_floor_roundtrip_cny']=opening+closing
    f['cost_floor_with_1tick_each_side_pct']=(opening+closing+2*s['tick']*s['multiplier'])/(f.close*s['multiplier'])
    f['bar_range_pct']=(f.high-f.low)/f.close
    f['bar_range_less_cost_proxy_pct']=f.bar_range_pct-f.cost_floor_with_1tick_each_side_pct
    return f


def summaries(features):
    outputs={}
    for name,keys in [('year_product',['year','product']),('quarter_product',['quarter','product']),
                      ('state_product',['market_state','product']),('session_product',['session','product'])]:
        outputs[name]=features.loc[features.valid_bar].groupby(keys,observed=True).agg(
            bars=('close','size'),er_median=('er','median'),atr_pct_median=('atr_pct','median'),
            bar_range_pct_median=('bar_range_pct','median'),volume_median=('volume','median'),
            relative_volume_median=('volume_relative_past20_same_slot','median'),
            past_rv_percentile_median=('past_day_rv_percentile20','median'),
            range_less_cost_median=('bar_range_less_cost_proxy_pct','median')).reset_index()
    return outputs


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--out',type=Path,default=ROOT/'artifacts/s1')
    parser.add_argument('--config',type=Path,default=ROOT/'artifacts/s1/research_config.json')
    args=parser.parse_args();out=args.out.resolve();out.mkdir(parents=True,exist_ok=True)
    cfg=json.loads(args.config.read_text(encoding='utf8'));source=ROOT/cfg['source']
    paths=[source,args.config,ROOT/cfg['calendar'],ROOT/cfg['session_template'],ROOT/cfg['fee_snapshot']]
    inputs={str(p.relative_to(ROOT)):digest(p) for p in paths}
    protocol=json.loads((ROOT/cfg['session_template']).read_text(encoding='utf8'))
    calendar=Calendar(json.loads((ROOT/cfg['calendar']).read_text(encoding='utf8')),protocol['night_ends'])
    from factor_study_data import specifications
    specs=specifications(protocol)
    fees=json.loads((ROOT/cfg['fee_snapshot']).read_text(encoding='utf8'))
    stamp=lambda s:int(pd.Timestamp(s,tz='Asia/Shanghai').value)
    lo,hi,freeze=[stamp(cfg[k]) for k in ('start','end_exclusive','discovery_end_exclusive')]
    db=sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True);db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
    mapping=pd.read_sql_query('select product,effective_date,contract from mappings order by product,effective_date',db)
    parts=[];thresholds={};coverage=[];days=[]
    for name,mp in mapping.groupby('product',sort=True):
        product=name.split('.')[1].upper();frames=[]
        for contract in mp.contract.unique():
            q=pd.read_sql_query('''select contract,start_ns,end_ns,open,high,low,close,volume,constituent_count,complete
                from bars where period_seconds=300 and contract=? and start_ns>=? and end_ns<=? order by start_ns''',db,params=(contract,lo,hi))
            if not q.empty:frames.append(q)
        if not frames:continue
        raw=pd.concat(frames,ignore_index=True);selected=select_main(raw,mp)
        f=bar_features(selected,cfg['er_bars'],cfg['atr_bars'])
        f=label_calendar(f,calendar,product+'0')
        f,daily=daily_context(f,calendar,product+'0',cfg['past_days'],cfg['volume_min_history_days'])
        threshold=freeze_thresholds(f,freeze,cfg['trend_er_quantile'],cfg['high_atr_quantile'],cfg['minimum_calibration_bars'])
        thresholds[product]=threshold;f=classify(f,threshold);f['product']=product
        f=add_cost_proxy(f,product,specs,fees)
        if not daily.empty:daily['product']=product;days.append(daily)
        coverage.append(dict(product=product,raw_rows=len(raw),selected_rows=len(f),invalid_rows=int((~f.valid_bar).sum()),
            calendar_unknown_rows=int((f.calendar_status!='calendar_covered').sum()),
            classified_rows=int(f.market_state.isin(cfg['states']).sum()),past20_context_rows=int(f.past20_return.notna().sum()),
            first_start=str(local(int(f.start_ns.min()))),last_end=str(local(int(f.end_ns.max()))),
            segments=int(f.history_segment.nunique()),rolls=int((f.contract!=f.contract.shift()).sum()-1)))
        parts.append(f);print('S1',product,'selected',len(f),'classified',coverage[-1]['classified_rows'],flush=True)
    db.close()
    features=pd.concat(parts,ignore_index=True).sort_values(['start_ns','product']).reset_index(drop=True)
    daily=pd.concat(days,ignore_index=True)
    features.to_parquet(out/'regime_features.parquet',index=False,compression='zstd')
    daily.to_csv(out/'daily_market_descriptors.csv',index=False)
    pd.DataFrame(coverage).to_csv(out/'data_coverage.csv',index=False)
    for name,summary in summaries(features).items():summary.to_csv(out/(name+'.csv'),index=False)
    save_json(out/'frozen_thresholds.json',thresholds)
    for p in paths:
        if digest(p)!=inputs[str(p.relative_to(ROOT))]:raise ValueError('input changed during S1: '+str(p))
    manifest=dict(run_id='s1-'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ'),
        created_utc=dt.datetime.now(dt.timezone.utc).isoformat(),research_version=cfg['research_version'],
        git_sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        input_sha256=inputs,code_sha256={str(p.relative_to(ROOT)):digest(p) for p in [Path(__file__),ROOT/'scripts/factor_study_data.py',ROOT/'crates/n-core/src/precision.rs']},
        runtime=dict(python=sys.version.split()[0],pandas=pd.__version__,numpy=np.__version__,pyarrow=importlib.metadata.version('pyarrow')),
        data_rows=len(features),coverage=coverage,source_assertion=cfg['source_assertion'],strategies_evaluated=0,
        thresholds=thresholds,temporal_check=all(t['maximum_source_asof_ns']<t['available_at_ns'] for t in thresholds.values()))
    save_json(out/'manifest.json',manifest)
    print('FEATURES',len(features),'TEMPORAL_CHECK',manifest['temporal_check'],flush=True)


if __name__=='__main__':main()
