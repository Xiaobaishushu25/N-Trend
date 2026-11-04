"""Frozen backward-year validation. No rule selection, tuning or broker access.

Standalone date-aware loader; original engines/configurations are hash checked.
Includes partial observations as supplied, matching discovery feature semantics.
"""
import argparse
import bisect
import collections
import hashlib
import json
import math
from pathlib import Path
import pickle
import sqlite3

import numpy as np
import pandas as pd

import research_swing_year_20261104 as b
import research_swing_expansion_20261104 as x
import research_swing_growth_20261104 as g
import audit_swing_growth_20261104 as audit

ROOT=b.ROOT
OUT=ROOT/'reports/swing-previous-year-20261104'
START,END,WARM='2024-10-28','2025-10-28','2024-09-01'
LABELS={'A_original_20margin':'原A一手','B_original_20margin':'原B一手','A_scale_750_m60':'A均衡','A_scale_1250_m80':'A增长','A_pyramid':'A盈利加仓','B_target2_guard3':'B成本改良'}


def write(name,data):
    (OUT/name).write_text(json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf8')


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def frozen_configs():
    frozen=json.loads((ROOT/'reports/swing-growth-20261104/audit-implementation.json').read_text(encoding='utf8'))
    paths=[Path(g.__file__),Path(x.__file__),Path(b.__file__),b.FEES]
    for p in paths:assert sha(p)==frozen[p.name],f'Frozen source changed: {p.name}'
    protocol=json.loads((ROOT/'reports/swing-growth-20261104/protocol.json').read_text(encoding='utf8'))
    byid={c['id']:c for c in protocol['configurations']}
    return {name:byid[name] for name in LABELS},{p.name:sha(p) for p in paths}


def load(start=START,end=END,warm=WARM,cache_key='previous'):
    cache=OUT/f'inputs-{cache_key}.pkl';identity=dict(period=[start,end],warmup=warm,db_bytes=b.DB.stat().st_size,db_mtime_ns=b.DB.stat().st_mtime_ns,loader_sha256=sha(Path(__file__)),features_sha256=sha(Path(x.__file__)))
    ident=OUT/f'inputs-{cache_key}-identity.json'
    if cache.exists() and ident.exists() and json.loads(ident.read_text())==identity:
        with cache.open('rb') as f:data=pickle.load(f)
        b.CALENDAR=data[-1];return data[:-1]
    c=sqlite3.connect(f'file:{b.DB.as_posix()}?mode=ro',uri=True)
    dates=[r[0] for r in c.execute("SELECT DISTINCT date(start_ns/1000000000,'unixepoch','+8 hours') FROM bars WHERE period_seconds=3600 AND cast(strftime('%H',start_ns/1000000000,'unixepoch','+8 hours') AS int) BETWEEN 9 AND 14 ORDER BY 1")]
    b.CALENDAR=dates
    def trading_day(t):
        day=t.strftime('%Y-%m-%d');j=bisect.bisect_right(dates,day) if t.hour>=18 else bisect.bisect_left(dates,day)
        return dates[j] if j<len(dates) else None
    mappings=pd.read_sql_query('SELECT product,effective_date,contract FROM mappings ORDER BY product,effective_date',c)
    lo=pd.Timestamp(warm,tz='Asia/Shanghai').value;hi=pd.Timestamp(end,tz='Asia/Shanghai').value
    tape=collections.defaultdict(dict);signals=collections.defaultdict(dict);opens={};atrs={};coverage={};digest=hashlib.sha256()
    expected=0
    for product,mp in mappings.groupby('product',sort=True):
        sym=product.split('.')[1].upper()
        if sym not in b.SPECS:continue
        ms=mp.effective_date.tolist();mc=mp.contract.tolist();chosen=[]
        for code in sorted(set(mc)):
            raw=pd.read_sql_query('SELECT start_ns,end_ns,bucket_start_ns,open,high,low,close,volume,close_oi AS oi,constituent_count,complete FROM bars WHERE contract=? AND period_seconds=900 AND start_ns>=? AND start_ns<? ORDER BY start_ns',c,params=(code,lo,hi))
            if raw.empty:continue
            digest.update(code.encode());digest.update(raw.to_csv(index=False).encode())
            times=pd.to_datetime(raw.start_ns,utc=True).dt.tz_convert('Asia/Shanghai');raw['day']=[trading_day(t) for t in times];raw=raw.loc[raw.day.notna()].copy()
            assert raw.oi.notna().all()
            opens[code]=dict(zip(raw.start_ns.astype(int),raw.open))
            for tf in [15,60]:
                ff=x.feature_frame(raw,tf);atrs[(code,tf)]=dict(zip(ff.end_ns.astype(int),ff.atr))
                stream='A' if tf==60 else 'B';family='pullback_end' if stream=='A' else 'oi_drive'
                for row in ff.to_dict('records'):
                    sig=x.signal(row,family,32,b.SPECS[sym][1])
                    if sig:
                        strength=abs(row['daily_fast']-row['daily_slow'])/max(row['atr']*math.sqrt(15/tf),b.SPECS[sym][1])
                        signals[(code,stream)][sig['signal_ns']]={**sig,'contract':code,'stream':stream,'tf':tf,'eff':float(row['daily_eff']),'strength':float(strength),'oi_change':float(row['oi32'])}
            keep=[]
            for day in raw.day:
                di=bisect.bisect_left(dates,day)-1;j=bisect.bisect_right(ms,dates[di])-1 if di>=0 else -1
                keep.append(j>=0 and mc[j]==code and start<=day<end)
            selected=raw.loc[keep].copy();selected['contract']=code;chosen.append(selected)
        if not chosen:continue
        selected=pd.concat(chosen).sort_values('start_ns');assert not selected.start_ns.duplicated().any()
        coverage[sym]=dict(days=int(selected.day.nunique()),bars=len(selected),partial_bars=int((selected.complete==0).sum()),zero_volume_bars=int((selected.volume==0).sum()),months=selected.groupby(selected.day.str[:7]).size().to_dict(),partial_observations=selected.loc[selected.complete==0,['day','contract','start_ns','end_ns','constituent_count']].to_dict('records'))
        expected+=int(selected.constituent_count.sum())
        for row in selected.to_dict('records'):tape[int(row['start_ns'])][sym]=row
        print('LOADED',cache_key,sym,len(selected),flush=True)
    c.close();tape=sorted(tape.items())
    write(f'data-audit-{cache_key}.json',dict(period=[start,end],warmup=warm,input_rows_sha256=digest.hexdigest(),coverage=coverage,expected_minutes=expected,calendar_method='observed daytime union; prior-day lagged main mapping'))
    data=(tape,dict(signals),opens,atrs,expected,dates)
    with cache.open('wb') as f:pickle.dump(data,f,protocol=5)
    write(f'inputs-{cache_key}-identity.json',identity)
    return data[:-1]


def minute_tape(tape,expected,tag):
    membership=collections.defaultdict(list);bounds={};decision=collections.defaultdict(set)
    for _,batch in tape:
        for sym,row in batch.items():
            code=row['contract'];membership[(code,row['bucket_start_ns'])].append((sym,row['day'],row['start_ns'],row['end_ns']))
            a,z=bounds.get(code,(row['start_ns'],row['end_ns']));bounds[code]=(min(a,row['start_ns']),max(z,row['end_ns']));decision[sym].add(row['end_ns'])
    conn=sqlite3.connect(f'file:{b.DB.as_posix()}?mode=ro',uri=True);mini=collections.defaultdict(dict);digest=hashlib.sha256();count=0
    for code,(a,z) in sorted(bounds.items()):
        rows=conn.execute('SELECT start_ns,end_ns,open,high,low,close,volume FROM bars WHERE contract=? AND period_seconds=60 AND start_ns>=? AND start_ns<? ORDER BY start_ns',(code,a,z)).fetchall()
        digest.update(code.encode());digest.update(json.dumps(rows,separators=(',',':')).encode())
        for row in rows:
            for sym,day,begin,finish in membership.get((code,row[0]//900_000_000_000*900_000_000_000),[]):
                if begin<=row[0]<finish:
                    assert sym not in mini[row[0]]
                    mini[row[0]][sym]=audit.MinuteBar(*row,day,code);count+=1
    conn.close();assert count==expected,(count,expected)
    write(f'minute-data-audit-{tag}.json',dict(minutes=count,expected_minutes=expected,source_sha256=digest.hexdigest(),steps=len(mini)))
    return sorted(mini.items()),dict(decision)


def set_period(start,end):
    b.START=start;b.END=end


def summarize(ts,eq,lim,start=START,end=END):
    stats=b.metrics(eq,ts,start,end)
    # Add an opening zero so empty calendar months and time concentration are
    # based on the whole predetermined validation window, not first trade date.
    return dict(full=stats,return_on_initial=round(stats['net']/50000,6),by_product={s:round(sum(t['net'] for t in ts if t['symbol']==s),2) for s in b.SPECS},limits=lim,median_hold_hours=float(np.median([t['hold_hours'] for t in ts])) if ts else 0)


def save_case(name,label,ts,eq,lim,mini,start,end,tag='previous'):
    result=summarize(ts,eq,lim,start,end);result['reconciliation']=audit.reconcile(ts,eq);result['collateral']=audit.collateral_check(mini,ts,eq)
    stem=f'{tag}-{name}-{label}'
    pd.DataFrame([{k:v for k,v in t.items() if k!='fills'} for t in ts]).to_csv(OUT/f'{stem}-trades.csv',index=False)
    write(f'{stem}-fills.json',ts);eq.to_csv(OUT/f'{stem}-equity.csv')
    return result


def main():
    OUT.mkdir(parents=True,exist_ok=True);configs,hashes=frozen_configs()
    protocol=dict(validation_period=[START,'2025-10-27'],discovery_period=['2025-10-28','2026-10-27'],warmup=WARM,capital=50000,margin=.2,drawdown_target=10000,configurations=configs,frozen_source_sha256=hashes,selection='No selection or tuning. Report all six saved candidates. Baseline/slip3 minute account validation, fee2/margin25 15m diagnostic. Positive net and drawdown<=10000 are descriptive validation gates, not optimized thresholds.',notes=['Backward extension is outside this conversation discovery year, but older project research may already have observed it.','Both years start with50000 and flat positions independently; not a continuous two-year account.','Frozen20261022 fee snapshot applied to2024-25, not historically reconstructed broker billing.','Same September1 warmup relative to each evaluation year, actual-contract indicators and lagged main membership.','Incomplete source bars are kept with observed minutes; missing minutes never fabricated.','No new trend-runner strategy added; no production changes.'])
    if (OUT/'protocol.json').exists():assert json.loads((OUT/'protocol.json').read_text())==protocol,'Existing validation protocol differs'
    else:write('protocol.json',protocol)
    tape,signals,opens,atrs,expected=load();mini,decision=minute_tape(tape,expected,'previous');set_period(START,END)
    results={}
    for name,cfg in configs.items():
        cases={}
        for label,kw in [('base',{}),('slip3',dict(slip=3))]:
            ts,eq,lim=g.run(mini,signals,opens,atrs,cfg,decision_ends=decision,**kw)
            cases[label]=save_case(name,label,ts,eq,lim,mini,START,END)
            print('PREVIOUS',name,label,cases[label]['full']['net'],cases[label]['full']['max_drawdown'],flush=True)
            results[name]=dict(label=LABELS[name],config=cfg,cases=cases);write('results-progress.json',results)
        for label,kw in [('fee2',dict(fee_scale=2)),('margin25',dict(margin=.25))]:
            ts,eq,lim=g.run(tape,signals,opens,atrs,cfg,**kw)
            cases[label]=summarize(ts,eq,lim);cases[label]['execution_resolution']='15m diagnostic'
            # The collateral reconstructor is minute-specific; ledger check is not.
            cases[label]['reconciliation']=audit.reconcile(ts,eq)
            pd.DataFrame([{k:v for k,v in t.items() if k!='fills'} for t in ts]).to_csv(OUT/f'previous-{name}-{label}-trades.csv',index=False);eq.to_csv(OUT/f'previous-{name}-{label}-equity.csv')
        results[name]=dict(label=LABELS[name],config=cfg,cases=cases);write('results-progress.json',results)
    write('results.json',results)
    del mini,tape,signals,opens,atrs
    # Recompute only the two previously15m reference accounts at1m on recent
    # data so the final year-to-year table does not mix execution resolutions.
    tape,signals,opens,atrs,expected=load('2025-10-28','2026-10-28','2025-09-01','recent')
    prior=ROOT/'reports/swing-growth-20261104/minute-replay.json'
    recent=json.loads(prior.read_text(encoding='utf8'))
    # Regression of the date-aware loader against all saved coarse controls.
    oldcomp=json.loads((ROOT/'reports/swing-growth-20261104/comparison.json').read_text(encoding='utf8'))
    oldrows={r['config']['id']:r for r in oldcomp['results']}
    for name,cfg in configs.items():
        ts,eq,lim=g.run(tape,signals,opens,atrs,cfg)
        assert abs(float(eq.iloc[-1])-oldrows[name]['full']['net'])<.011,('loader regression',name,float(eq.iloc[-1]),oldrows[name]['full']['net'])
    mini,decision=minute_tape(tape,expected,'recent');set_period('2025-10-28','2026-10-28')
    for name in ['A_original_20margin','B_original_20margin']:
        ts,eq,lim=g.run(mini,signals,opens,atrs,configs[name],decision_ends=decision)
        recent[name]={'base':save_case(name,'base',ts,eq,lim,mini,'2025-10-28','2026-10-28','recent')}
        print('RECENT_REFERENCE',name,recent[name]['base']['full']['net'],flush=True)
    write('recent-comparable.json',{name:recent[name] for name in LABELS})
    write('implementation.json',{p.name:sha(p) for p in [Path(__file__),Path(g.__file__),Path(x.__file__),Path(b.__file__),Path(audit.__file__),b.FEES]})
    print('COMPLETE: frozen six-strategy backward validation',flush=True)


if __name__=='__main__':main()
