"""Second-round swing discovery with integrated account state and structural risk.

Actual-contract 15m execution; 15m/4x15m closed-bar signals; closed daily regime.
All candidates, rejected orders, costs and chronological selection are retained.
"""
import argparse
import bisect
import collections
import hashlib
import json
import math
from pathlib import Path
import sqlite3

import numpy as np
import pandas as pd

import research_swing_year_20261104 as base

ROOT=base.ROOT
OUT=ROOT/'reports/swing-expansion-20261104'
FEES=json.loads(base.FEES.read_text(encoding='utf8'))['products']
FAMILIES=['squeeze','pullback_end','oi_drive','failed_auction','retest']


def write(name,data):
    (OUT/name).write_text(json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf8')


def feature_frame(raw,tf):
    """Every feature is measurable at the row's end_ns, independently by contract."""
    z=raw.reset_index(drop=True).copy()
    daily=z.groupby('day',sort=True).agg(end_ns=('end_ns','max'),close=('close','last'),high=('high','max'),low=('low','min')).reset_index()
    dc=daily.close
    daily['daily_fast']=dc.ewm(span=10,adjust=False,min_periods=30).mean()
    daily['daily_slow']=dc.ewm(span=30,adjust=False,min_periods=30).mean()
    daily['daily_eff']=(dc-dc.shift(20)).abs()/dc.diff().abs().rolling(20).sum().replace(0,np.nan)
    if tf==60:
        # Four observed trading bars within each trading day; final incomplete
        # group is not a signal bar. This is trading-time, not native TQ hourly.
        z['chunk']=z.groupby('day').cumcount()//4
        z=z.groupby(['day','chunk'],sort=True).agg(start_ns=('start_ns','min'),end_ns=('end_ns','max'),open=('open','first'),high=('high','max'),low=('low','min'),close=('close','last'),volume=('volume','sum'),oi=('oi','last'),count=('start_ns','size')).reset_index()
        z=z[z['count']==4].copy()
    c,h,l=z.close,z.high,z.low
    tr=pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
    z['atr']=tr.rolling(16).mean()
    ratio=tr.rolling(4).mean()/tr.rolling(32).mean()
    z['squeezed']=ratio.shift().rolling(8).min()<.8
    z['h3']=h.shift().rolling(3).max();z['l3']=l.shift().rolling(3).min()
    z['sl']=l.rolling(5).min();z['sh']=h.rolling(5).max()
    z['volbase']=z.volume.shift().rolling(20).mean()
    for n in [16,32]:
        z[f'h{n}']=h.shift().rolling(n).max();z[f'l{n}']=l.shift().rolling(n).min()
        z[f'fast{n}']=c.ewm(span=n//4,adjust=False,min_periods=n).mean()
        z[f'slow{n}']=c.ewm(span=n,adjust=False,min_periods=n).mean()
        z[f'oi{n}']=z.oi/z.oi.shift(n).replace(0,np.nan)-1
        # A level known before the most recent four bars; the preceding
        # three closes must have crossed it before a retest can qualify.
        z[f'retest_h{n}']=h.shift(4).rolling(n).max()
        z[f'retest_l{n}']=l.shift(4).rolling(n).min()
        z[f'cross_h{n}']=c.shift().rolling(3).max()>z[f'retest_h{n}']
        z[f'cross_l{n}']=c.shift().rolling(3).min()<z[f'retest_l{n}']
    return pd.merge_asof(z.sort_values('end_ns'),daily[['end_ns','daily_fast','daily_slow','daily_eff']].sort_values('end_ns'),on='end_ns',direction='backward')


def signal(row,family,n,tick):
    c,o,h,l,a=row['close'],row['open'],row['high'],row['low'],row['atr']
    if not np.isfinite(a) or a<=0 or not np.isfinite(row['daily_fast']):return None
    trend=1 if row['daily_fast']>row['daily_slow'] else -1
    fast,slow=row[f'fast{n}'],row[f'slow{n}']
    hi,lo=row[f'h{n}'],row[f'l{n}'];side=0
    if family=='squeeze' and row['squeezed']:
        side=1 if trend==1 and c>hi else -1 if trend==-1 and c<lo else 0
    elif family=='pullback_end':
        side=1 if trend==1 and fast>slow and row['l3']<fast and c>row['h3'] and c>o else -1 if trend==-1 and fast<slow and row['h3']>fast and c<row['l3'] and c<o else 0
    elif family=='oi_drive' and row[f'oi{n}']>.01 and row['volume']>row['volbase']:
        side=1 if trend==1 and c>hi else -1 if trend==-1 and c<lo else 0
    elif family=='failed_auction' and row['daily_eff']<.35 and h>l:
        side=1 if l<lo and c>lo and c>o and (c-l)/(h-l)>.6 else -1 if h>hi and c<hi and c<o and (h-c)/(h-l)>.6 else 0
    elif family=='retest':
        rh,rl=row[f'retest_h{n}'],row[f'retest_l{n}']
        side=1 if trend==1 and row[f'cross_h{n}'] and l<=rh+.25*a and c>rh and c>o else -1 if trend==-1 and row[f'cross_l{n}'] and h>=rl-.25*a and c<rl and c<o else 0
    if not side:return None
    # Reversal extremes are the invalidation; continuation uses five-bar swing.
    stop=(l if family=='failed_auction' else row['sl'])-tick if side>0 else (h if family=='failed_auction' else row['sh'])+tick
    stop=math.floor(stop/tick)*tick if side>0 else math.ceil(stop/tick)*tick
    return dict(side=side,stop=stop,atr=a,signal_close=c,signal_ns=int(row['end_ns']))


def load():
    frames,raws,dates,audit,digest=base.load()
    c=sqlite3.connect(f'file:{base.DB.as_posix()}?mode=ro',uri=True)
    oi_hash=hashlib.sha256();features={};signals={}
    for code,raw in raws.items():
        oi=pd.read_sql_query('SELECT start_ns,close_oi AS oi FROM bars WHERE contract=? AND period_seconds=900 AND start_ns>=? AND start_ns<=? ORDER BY start_ns',c,params=(code,int(raw.start_ns.min()),int(raw.start_ns.max())))
        oi_hash.update(code.encode());oi_hash.update(oi.to_csv(index=False).encode())
        z=raw.reset_index(drop=True).merge(oi,on='start_ns',how='left',validate='one_to_one')
        assert z.oi.notna().all()
        sym=''.join(x for x in code.split('.')[1] if x.isalpha()).upper();tick=base.SPECS[sym][1]
        features[code]={tf:feature_frame(z,tf) for tf in [15,60]}
        for tf,ff in features[code].items():
            rows=ff.to_dict('records')
            for family in FAMILIES:
                for n in [16,32]:
                    out={}
                    for row in rows:
                        sig=signal(row,family,n,tick)
                        if sig:out[sig['signal_ns']]={**sig,'contract':code}
                    signals[(code,tf,family,n)]=out
    c.close()
    tape=collections.defaultdict(dict)
    for sym,z in frames.items():
        for row in z.loc[z.day>=base.START,['start_ns','end_ns','day','contract','open','high','low','close','volume']].to_dict('records'):
            tape[int(row['start_ns'])][sym]=row
    tape=sorted(tape.items())
    old_open={code:dict(zip(z.start_ns.astype(int),z.open)) for code,z in raws.items()}
    atrs={(code,tf):dict(zip(ff.end_ns.astype(int),ff.atr)) for code,fs in features.items() for tf,ff in fs.items()}
    write('data-audit.json',dict(base_rows_sha256=digest,oi_rows_sha256=oi_hash.hexdigest(),coverage=audit,signal_counts={str(k):len(v) for k,v in signals.items()},tape_steps=len(tape)))
    return tape,signals,old_open,atrs


def run(tape,signals,old_open,atrs,model,slip=1,margin=.15,fee_scale=1.,symbol_order='normal',fixed_plan=False):
    """Single account; rejected opportunities never create hidden positions."""
    capital=50000.;cash=capital;positions={};trades=[];curve=[];log=collections.Counter();lastmark={};cooldown={};peakmargin=0.;pospeak=0
    tf,family,n,risk,exit_mode=model['tf'],model['family'],model['n'],model['risk'],model['exit']
    def commission(sym,price,kind):return fee_scale*base.fee(FEES[sym],price,base.SPECS[sym][0],kind)
    def close(sym,px,ns,day,reason):
        nonlocal cash
        p=positions.pop(sym);mult,tick=base.SPECS[sym]
        fill=px-p['side']*slip*tick
        ff=commission(sym,fill,'close_today' if day==p['entry_day'] else 'close_yesterday')
        cash+=p['side']*(fill-p['entry_price'])*mult-ff
        net=p['side']*(fill-p['entry_price'])*mult-ff-p['entry_fee']
        trades.append({**p,'symbol':sym,'exit_ns':int(ns),'exit_day':day,'exit_price':fill,'net':net,'r':net/p['risk_cash'],'reason':reason,'hold_hours':(ns-p['entry_ns'])/3.6e12,'lots':1})
        cooldown[sym]=int(ns)+4*900_000_000_000
    for time,batch in tape:
        # All opening prices are contemporaneously available; opening exits
        # precede entries, intrabar exits free resources only at next step.
        for sym,row in batch.items():
            lastmark[sym]=row['open']
            if sym not in positions:continue
            p=positions[sym];side=p['side'];op=row['open'];tick=base.SPECS[sym][1]
            if p['contract']!=row['contract']:
                if time not in old_open[p['contract']]:raise RuntimeError('Missing old contract open')
                close(sym,old_open[p['contract']][time],time,row['day'],'roll')
            elif side*(op-p['stop'])<=0:close(sym,op,time,row['day'],'gap_stop')
            elif p['target'] and side*(op-p['target'])>=slip*tick:close(sym,p['target'],time,row['day'],'gap_target')
            elif p['exit_next']:close(sym,op,time,row['day'],'time')
        order=sorted(batch,reverse=symbol_order=='reverse')
        for sym in order:
            row=batch[sym]
            if sym in positions or cooldown.get(sym,0)>time or row['volume']<=0:continue
            sig=signals.get((row['contract'],tf,family,n),{}).get(time)
            if not sig:continue
            log['signals']+=1
            mult,tick=base.SPECS[sym];side=sig['side'];fill=row['open']+side*slip*tick
            distance=side*(fill-sig['stop']);original=side*(sig['signal_close']-sig['stop'])
            plan_slip=1 if fixed_plan else slip
            planned_fill=row['open']+side*plan_slip*tick
            plan_distance=side*(planned_fill-sig['stop'])
            if plan_distance<4*tick or plan_distance>1.5*max(original,4*tick):log['gap_or_distance']+=1;continue
            ef=commission(sym,fill,'open')
            stopfee=max(commission(sym,sig['stop'],'close_today'),commission(sym,sig['stop'],'close_yesterday'))
            plan_ef=base.fee(FEES[sym],planned_fill,mult,'open') if fixed_plan else ef
            plan_sf=max(base.fee(FEES[sym],sig['stop'],mult,'close_today'),base.fee(FEES[sym],sig['stop'],mult,'close_yesterday')) if fixed_plan else stopfee
            totalrisk=plan_distance*mult+plan_slip*tick*mult+plan_ef+plan_sf
            if totalrisk>risk:log['risk_budget']+=1;continue
            if (2*plan_slip*tick*mult+plan_ef+plan_sf)/(plan_distance*mult)>.15:log['friction']+=1;continue
            equity=cash+sum(p['side']*(lastmark[s]-p['entry_price'])*base.SPECS[s][0] for s,p in positions.items())
            used=sum(lastmark[s]*base.SPECS[s][0]*margin for s in positions)
            if used+fill*mult*margin>equity*.4:log['margin_cap']+=1;continue
            if len(positions)>=4 or sum(base.SECTORS[s]==base.SECTORS[sym] for s in positions)>=2:log['concurrency']+=1;continue
            if sum(p['planned_risk'] for p in positions.values())+totalrisk>1000:log['total_risk']+=1;continue
            cash-=ef
            positions[sym]=dict(contract=row['contract'],entry_ns=int(time),entry_day=row['day'],entry_price=fill,entry_fee=ef,side=side,stop=sig['stop'],risk_cash=distance*mult,planned_risk=totalrisk,actual_initial_stop_risk=distance*mult+slip*tick*mult+ef+stopfee,distance=distance,target=fill+side*3*distance if exit_mode=='target3' else None,bars=0,exit_next=False,atr=sig['atr'])
            peakmargin=max(peakmargin,used+fill*mult*margin);pospeak=max(pospeak,len(positions));log['entries']+=1
        # This conservative ordering resolves both stop/target hit in a bar as
        # stop first; target requires one slippage distance of penetration.
        for sym,row in batch.items():
            lastmark[sym]=row['close']
            if sym not in positions:continue
            p=positions[sym];side=p['side'];stop=p['stop'];target=p['target'];tick=base.SPECS[sym][1]
            hit=row['low']<=stop if side>0 else row['high']>=stop
            target_hit=target is not None and (row['high']>=target+slip*tick if side>0 else row['low']<=target-slip*tick)
            end=int(row['end_ns'])
            if hit:
                close(sym,stop,end,row['day'],'stop');continue
            if target_hit:
                close(sym,target,end,row['day'],'target');continue
            p['bars']+=1
            at=atrs.get((p['contract'],tf),{}).get(end)
            if at is not None and np.isfinite(at):p['atr']=at
            if exit_mode=='trail' and side*(row['close']-p['entry_price'])>=2*p['distance']:
                new=row['close']-side*math.ceil(3*p['atr']/tick)*tick
                p['stop']=max(stop,new) if side>0 else min(stop,new)
            if p['bars']>=192:p['exit_next']=True
        equity=cash+sum(p['side']*(lastmark[s]-p['entry_price'])*base.SPECS[s][0] for s,p in positions.items())
        curve.append([max(int(r['end_ns']) for r in batch.values()),equity-capital,next(iter(batch.values()))['day']])
    # Marked positions must be liquidated on their actual last observed price.
    for sym,p in list(positions.items()):
        row=next(batch[sym] for _,batch in reversed(tape) if sym in batch)
        close(sym,row['close'],row['end_ns'],row['day'],'end')
    if curve:curve[-1][1]=cash-capital
    eq=pd.DataFrame(curve,columns=['ns','net','day']).drop_duplicates('ns',keep='last').set_index('ns').net
    assert abs(sum(t['net'] for t in trades)-(cash-capital))<1e-6
    return trades,eq,dict(rejections=dict(log),peak_entry_margin=peakmargin,peak_positions=pospeak,slip=slip,margin=margin,fee_scale=fee_scale,fixed_baseline_cost_plan=fixed_plan)


def evaluate(ts,eq,limits):
    return dict(full=base.metrics(eq,ts,base.START,base.END),train=base.metrics(eq,ts,base.START,base.TRAIN_END),validation=base.metrics(eq,ts,base.TRAIN_END,base.VALID_END),test=base.metrics(eq,ts,base.VALID_END,base.END),limits=limits,by_product={sym:round(sum(t['net'] for t in ts if t['symbol']==sym),2) for sym in base.SPECS},median_hold_hours=float(np.median([t['hold_hours'] for t in ts])) if ts else 0)


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    models=[dict(id=f'{family}_{tf}_{n}_{exit_mode}_r{risk}',family=family,tf=tf,n=n,exit=exit_mode,risk=risk) for family in FAMILIES for tf in [15,60] for n in [16,32] for exit_mode in ['trail','target3'] for risk in [250,500]]
    write('protocol.json',dict(models=models,capital=50000,period=[base.START,base.END],split=[base.TRAIN_END,base.VALID_END],selection='Rank train-eligible (net>0, >=20 trades, >=4 positive calendar bins, maxDD<=5000) by train net/max(DD,250); pick first with validation net>0, >=10 trades, >=2 positive bins and maxDD<=5000. Test not used. If none passes, retain train-ranked nonzero-trade diagnostic only.',notes=['Additional hypothesis search on previously observed year; no untouched holdout claim.','Integrated chronological one-lot account, no hidden positions for rejected signals.','Structural invalidation stop; single risk includes fees and stop slippage.','Maximum4 positions; sector2; total planned initial risk1000; margin15%, cap40%equity.','Next contiguous15m opening fills only; one-lot initial entries, no pyramiding.','4x15m trading-time signals grouped within each trading day, full four-bar groups only.','Daily trend only available after daily last observed end_ns.','Three-R target with penetration requirement and stop-first ambiguity; trail only after2R closed profit; max192 observed15m bars.','Cooldown60 calendar minutes after exit.','Pressure scenarios freeze baseline risk/friction gates, then vary actual fills/fees; actual stop risk can exceed planning budget.','Fee snapshot20261022, not reconstructed historical broker fees.']))
    tape,signals,old_open,atrs=load();results=[];cache={}
    for model in models:
        ts,eq,lim=run(tape,signals,old_open,atrs,model)
        stats=evaluate(ts,eq,lim);row=dict(model=model,**stats);results.append(row)
        cache[model['id']]=(ts,eq,lim)
        print(model['id'],stats['train']['net'],stats['validation']['net'],stats['test']['net'],stats['full']['trades'],flush=True)
        write('comparison-progress.json',results)
    ranked=sorted([r for r in results if r['train']['trades']>=5],key=lambda r:r['train']['net']/max(r['train']['max_drawdown'],250),reverse=True)
    train_eligible=[r for r in ranked if r['train']['net']>0 and r['train']['trades']>=20 and r['train']['positive_months']>=4 and r['train']['max_drawdown']<=5000]
    passed=[r for r in train_eligible if r['validation']['net']>0 and r['validation']['trades']>=10 and r['validation']['positive_months']>=2 and r['validation']['max_drawdown']<=5000]
    chosen=(passed or train_eligible or ranked)[0]
    write('comparison.json',dict(results=results,selected=chosen['model'],passed_development_gate=bool(passed),train_eligible=[r['model']['id'] for r in train_eligible],eligible=[r['model']['id'] for r in passed],train_rank=[r['model']['id'] for r in ranked]))
    model=chosen['model'];summary={}
    scenarios=[('base',{}),('slip2',dict(slip=2,fixed_plan=True)),('slip3',dict(slip=3,fixed_plan=True)),('fee2',dict(fee_scale=2,fixed_plan=True)),('margin20',dict(margin=.2)),('reverse',dict(symbol_order='reverse'))]
    for name,kwargs in scenarios:
        ts,eq,lim=cache[model['id']] if name=='base' else run(tape,signals,old_open,atrs,model,**kwargs)
        summary[name]=evaluate(ts,eq,lim)
        pd.DataFrame(ts).to_csv(OUT/f'trades-{name}.csv',index=False)
        eq.to_csv(OUT/f'equity-{name}.csv')
    write('selected-summary.json',dict(model=model,passed_development_gate=bool(passed),scenarios=summary))
    write('implementation.json',{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in [Path(__file__),Path(base.__file__),base.FEES]})
    print('SELECTED',model,'PASSED',bool(passed),flush=True)


if __name__=='__main__':main()
