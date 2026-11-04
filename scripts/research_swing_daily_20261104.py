"""Second prespecified family: multi-day swings, without asset selection.

Daily bars use inferred exchange trading day (including preceding night).
Daily signals execute at next daily open; daily ATR stop updated after close.
"""
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

import research_swing_year_20261104 as s


def daily_frames(frames,raws):
    daily={};dr={}
    for code,z in raws.items():
        d=z.groupby('day',sort=True).agg(start_ns=('start_ns','min'),end_ns=('end_ns','max'),open=('open','first'),high=('high','max'),low=('low','min'),close=('close','last'),volume=('volume','sum')).reset_index()
        d['month']=d.day.str[:7];d['contract']=code
        tr=pd.concat([d.high-d.low,(d.high-d.close.shift()).abs(),(d.low-d.close.shift()).abs()],axis=1).max(axis=1)
        d['atr']=tr.rolling(14).mean()
        for n in [10,20,40]:
            d[f'h{n}']=d.high.rolling(n).max().shift();d[f'l{n}']=d.low.rolling(n).min().shift()
            d[f'e{n}']=d.close.ewm(span=n,adjust=False,min_periods=n).mean()
            d[f'x{n}']=d.close.ewm(span=max(3,n//4),adjust=False,min_periods=n).mean()
            d[f'hi{n}']=d.high.rolling(n//2).max().shift();d[f'lo{n}']=d.low.rolling(n//2).min().shift()
        dr[code]=d.set_index('start_ns',drop=False)
    for sym,z in frames.items():
        # The first selected 15m bar identifies daily selection without future price use.
        choices=z.groupby('day',sort=True).contract.first()
        rows=[]
        for day,code in choices.items():
            rows.append(dr[code].loc[dr[code].day==day])
        daily[sym]=pd.concat(rows).reset_index(drop=True)
    return daily,dr


def main(out_name='swing-daily-20261104', models=None, daily=True):
    s.OUT=s.ROOT/'reports'/out_name;s.OUT.mkdir(exist_ok=True)
    models=models or [dict(id=f'daily_{f}_{n}_{k}',family=f,n=n,k=k,daily=True,max_bars=40) for f in ['breakout','pullback'] for n in [10,20,40] for k in [2,3]]
    s.save('protocol.json',dict(models=models,risk_budgets=[250,500],capital=50000,selection='Joint model and risk budget: training net>0, at least 4 positive calendar bins (partial endpoints count), at least 10 trades, training drawdown<=5000; validation net>0, at least2 positive calendar bins and drawdown<=5000. Rank passing candidates by training net/max(dd,250); test not used. If none, top train ratio is failed exploratory control.',notes=['Extension motivated by first family failing development; additional multiple testing exposure.','All 22 products retained; no posthoc winner product selection.','Daily warmup starts September1; n40 may start after first evaluation date.','No claim of untouched holdout.','Actual contract daily OHLC and lagged historical main mapping; 15% margin proxy.']))
    frames,raws,dates,audit,digest=s.load()
    if daily:frames,raws=daily_frames(frames,raws)
    if not daily:
        protocol=json.loads((s.OUT/'protocol.json').read_text(encoding='utf8'))
        protocol['notes']=[n for n in protocol['notes'] if not n.startswith(('Daily warmup','Actual contract daily'))]
        protocol['notes'].append('15m actual-contract range swings; rolling mean exits; 15% margin proxy.')
        s.save('protocol.json',protocol)
    s.save('data-audit.json',dict(input_rows_sha256=digest,daily_bars={sym:len(z) for sym,z in frames.items()}))
    scores=[];cache={}
    for model in models:
        trades,paths,_=s.simulate(frames,raws,model)
        for risk in [250,500]:
            acc,eq,limits=s.portfolio(trades,risk=risk)
            result=dict(model=model,risk=risk,train=s.metrics(eq,acc,s.START,s.TRAIN_END),validation=s.metrics(eq,acc,s.TRAIN_END,s.VALID_END),test=s.metrics(eq,acc,s.VALID_END,s.END),full=s.metrics(eq,acc,s.START,s.END),limits=limits)
            scores.append(result);cache[(model['id'],risk)]=(acc,eq,trades)
            print(model['id'],risk,result['train']['net'],result['validation']['net'],result['test']['net'],len(acc),flush=True)
    ranked=sorted(scores,key=lambda x:x['train']['net']/max(x['train']['max_drawdown'],250),reverse=True)
    eligible=[r for r in ranked if r['train']['net']>0 and r['train']['positive_months']>=4 and r['train']['trades']>=10 and r['train']['max_drawdown']<=5000 and r['validation']['net']>0 and r['validation']['positive_months']>=2 and r['validation']['max_drawdown']<=5000]
    chosen=eligible[0] if eligible else ranked[0]
    s.save('comparison.json',dict(results=scores,selected=chosen,passed_development_gate=bool(eligible)))
    model,risk=chosen['model'],chosen['risk'];acc,eq,trades=cache[(model['id'],risk)]
    summary={}
    for slip in [1,2,3]:
        ts=trades if slip==1 else s.simulate(frames,raws,model,slip)[0]
        a,e,limits=s.portfolio(ts,risk=risk)
        summary[str(slip)]=dict(full=s.metrics(e,a,s.START,s.END),train=s.metrics(e,a,s.START,s.TRAIN_END),validation=s.metrics(e,a,s.TRAIN_END,s.VALID_END),test=s.metrics(e,a,s.VALID_END,s.END),limits=limits,by_product={sym:round(sum(t['net']*t['lots'] for t in a if t['symbol']==sym),2) for sym in s.SPECS},median_hold_hours=float(np.median([t['hold_hours'] for t in a])) if a else 0)
        e.to_csv(s.OUT/f'account-equity-slip{slip}.csv',header=['net_equity'])
        pd.DataFrame([{k:v for k,v in t.items() if k!='marks'} for t in a]).to_csv(s.OUT/f'account-trades-slip{slip}.csv',index=False)
    s.save('account-summary.json',summary)
    s.save('implementation.json',dict(script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),engine_sha256=hashlib.sha256(Path(s.__file__).read_bytes()).hexdigest()))
    print('SELECTED',model,risk,'PASSED',bool(eligible),flush=True)
    print(json.dumps(summary,ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':main()
