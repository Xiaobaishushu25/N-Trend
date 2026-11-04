"""Offline causal swing study. No network, broker, or production writes.

12 predeclared models; chronological 6/3/3 month split. Fractional equal-risk
discovery, then integer-lot capital-constrained execution of the selected model.
"""
import bisect
import hashlib
import json
import math
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'reports/swing-year-20261104'
DB = ROOT / 'data/research_market_history.sqlite'
FEES = ROOT / 'scripts/exchange_fees_20261022.json'
START, TRAIN_END, VALID_END, END = '2025-10-28', '2026-04-28', '2026-07-28', '2026-10-28'
CALENDAR = []
SPECS = { # multiplier, minimum tick
    'BU':(10,1),'C':(10,1),'CF':(5,5),'CJ':(5,5),'EB':(5,1),
    'EG':(10,1),'FG':(20,1),'JD':(10,1),'L':(5,1),'MA':(10,1),
    'PB':(5,5),'PP':(5,1),'RB':(10,1),'SA':(20,1),'SF':(5,2),
    'SH':(30,1),'SI':(5,5),'SM':(5,2),'SR':(10,1),'SS':(5,5),
    'UR':(20,1),'V':(5,1)}
SECTORS = {**dict.fromkeys(['BU','EB','EG','L','PP','V','MA'],'chemical'),
           **dict.fromkeys(['RB','SS','PB','SI','SF','SM'],'metal'),
           **dict.fromkeys(['FG','SA','SH'],'glass'),
           **dict.fromkeys(['C','CF','CJ','JD','SR','UR'],'agri')}


def save(name, value):
    (OUT/name).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf8')


def load():
    global CALENDAR
    c = sqlite3.connect(f'file:{DB.as_posix()}?mode=ro', uri=True)
    mapping = pd.read_sql_query('SELECT product,effective_date,contract FROM mappings ORDER BY product,effective_date',c)
    # Observed exchange-day union; nights assigned to following day session.
    dates = [r[0] for r in c.execute("SELECT DISTINCT date(start_ns/1000000000,'unixepoch','+8 hours') FROM bars WHERE period_seconds=3600 AND cast(strftime('%H',start_ns/1000000000,'unixepoch','+8 hours') AS int) BETWEEN 9 AND 14 ORDER BY 1")]
    CALENDAR = dates
    def trading_day(t):
        day=t.strftime('%Y-%m-%d')
        j=bisect.bisect_right(dates,day) if t.hour>=18 else bisect.bisect_left(dates,day)
        return dates[j] if j<len(dates) else None
    start_ns=pd.Timestamp('2025-09-01',tz='Asia/Shanghai').value
    end_ns=pd.Timestamp(END,tz='Asia/Shanghai').value
    frames={}; raws={}; audit={}; digest=hashlib.sha256()
    for product,mp in mapping.groupby('product',sort=True):
        sym=product.split('.')[1].upper()
        if sym not in SPECS: continue
        ms=mp.effective_date.tolist(); mc=mp.contract.tolist()
        selected=[]; total=0
        for code in sorted(set(mc)):
            df=pd.read_sql_query('SELECT start_ns,end_ns,open,high,low,close,volume,complete FROM bars WHERE contract=? AND period_seconds=900 AND start_ns>=? AND start_ns<? ORDER BY start_ns',c,params=(code,start_ns,end_ns))
            if df.empty:continue
            digest.update(code.encode());digest.update(df.to_csv(index=False).encode())
            total+=len(df)
            ts=pd.to_datetime(df.start_ns,utc=True).dt.tz_convert('Asia/Shanghai')
            df['day']=[trading_day(t) for t in ts]
            df=df[df.day.notna()].copy()
            df['month']=df.day.str[:7]
            df['contract']=code
            h,l,cl=df.high,df.low,df.close
            tr=pd.concat([h-l,(h-cl.shift()).abs(),(l-cl.shift()).abs()],axis=1).max(axis=1)
            df['atr']=tr.rolling(32).mean()
            for n in [64,128,256]:
                df[f'h{n}']=h.rolling(n).max().shift()
                df[f'l{n}']=l.rolling(n).min().shift()
                df[f'e{n}']=cl.ewm(span=n,adjust=False,min_periods=n).mean()
                df[f'x{n}']=cl.ewm(span=n//4,adjust=False,min_periods=n).mean()
                df[f'hi{n}']=h.rolling(n//2).max().shift()
                df[f'lo{n}']=l.rolling(n//2).min().shift()
            df['prev_close']=cl.shift()
            for n in [64,128,256]:
                df[f'm{n}']=cl.rolling(n).mean()
                df[f'sd{n}']=cl.rolling(n).std(ddof=0)
            raws[code]=df.set_index('start_ns',drop=False)
            # Lag map by one observed trading day, removing same-day hindsight.
            keep=[]
            for day in df.day:
                di=bisect.bisect_left(dates,day)-1
                j=bisect.bisect_right(ms,dates[di])-1 if di>=0 else -1
                keep.append(j>=0 and mc[j]==code)
            selected.append(df.loc[keep])
        if not selected:continue
        z=pd.concat(selected).sort_values('start_ns').reset_index(drop=True)
        assert not z.start_ns.duplicated().any()
        frames[sym]=z
        audit[sym]=dict(selected_bars=len(z),raw_bars=total,partial_bars=int((z.complete==0).sum()),first=str(pd.Timestamp(z.start_ns.iloc[0],tz='UTC').tz_convert('Asia/Shanghai')),last=str(pd.Timestamp(z.end_ns.iloc[-1],tz='UTC').tz_convert('Asia/Shanghai')),days=int(z.loc[z.day>=START,'day'].nunique()),months=z.loc[z.day>=START].groupby('month').size().to_dict())
        print('Loaded',sym,len(z),flush=True)
    c.close()
    return frames,raws,dates,audit,digest.hexdigest()


def fee(spec,price,mult,kind):
    if 'roundtrip_fixed_yuan_per_lot' in spec:return spec['roundtrip_fixed_yuan_per_lot']/2
    r=spec[kind];return r['fixed_yuan_per_lot']+r['notional_rate']*price*mult


def simulate(frames,raws,model,slip=1):
    family,n,k=model['family'],model['n'],model['k']
    costs=json.loads(FEES.read_text(encoding='utf8'))['products']
    trades=[]; paths={}; missing_rolls=0
    for sym,df in frames.items():
        mult,tick=SPECS[sym];fs=costs[sym]
        cash=0.;pos=None;pending=None;curve=[]
        def close(p,price,t,day,reason):
            nonlocal cash
            fill=price-p['side']*slip*tick
            ff=fee(fs,fill,mult,'close_today' if day==p['entry_day'] else 'close_yesterday')
            net=p['side']*(fill-p['entry_price'])*mult-p['entry_fee']-ff
            p.update(exit_ns=int(t),exit_day=day,exit_price=fill,net=net,r=net/p['risk_cash'],reason=reason,hold_hours=(t-p['entry_ns'])/3.6e12)
            cash+=net*250/p['risk_cash']
            trades.append(p)
        for r in df.to_dict('records'):
            t=int(r['start_ns']); day=r['day']; op=r['open'];cl=r['close']
            if day<START:continue
            if pos and pos['contract']!=r['contract']:
                old=raws[pos['contract']]
                if t not in old.index:
                    # Never book a fabricated fill; fail if roll price unavailable.
                    raise RuntimeError(f'Missing old-contract roll open {sym} {t}')
                ro=old.loc[t]
                close(pos,float(ro.open),t,day,'roll');pos=None;pending=None
            if pos and pending=='exit':
                close(pos,op,t,day,'signal');pos=None;pending=None
            if not pos and isinstance(pending,dict):
                if pending['contract']==r['contract'] and (t==pending['end_ns'] or model.get('daily',False)) and r['volume']>0:
                    side=pending['side']; fill=op+side*slip*tick
                    distance=max(math.ceil(k*pending['atr']/tick)*tick,4*tick)
                    pos=dict(symbol=sym,contract=r['contract'],entry_ns=t,entry_day=day,entry_price=fill,entry_fee=fee(fs,fill,mult,'open'),side=side,stop=fill-side*distance,risk_cash=distance*mult,bars=0,marks=[])
                pending=None
            if pos:
                side=pos['side'];stop=pos['stop']
                hit=r['low']<=stop if side>0 else r['high']>=stop
                if hit:
                    px=min(op,stop) if side>0 else max(op,stop)
                    # Timestamp at bar end conservatively reserves resources entire bar.
                    close(pos,px,int(r['end_ns']),day,'stop');pos=None
                else:
                    pos['bars']+=1
                    pos['marks'].append([int(r['end_ns']),cl,r['low'] if side>0 else r['high'],day])
                    # Updated trailing stop applies only from the NEXT bar.
                    dist=math.ceil(k*r['atr']/tick)*tick
                    pos['stop']=max(stop,cl-dist) if side>0 else min(stop,cl+dist)
                    exit_signal=(cl>=r[f'm{n}'] if side>0 else cl<=r[f'm{n}']) if family in ['falsebreak','meanrevert'] else (cl<r[f'lo{n}'] if side>0 else cl>r[f'hi{n}'])
                    if pos['bars']>=model.get('max_bars',320) or exit_signal:pending='exit'
            if not pos and pending is None and day<END and np.isfinite(r[f'e{n}']) and r['atr']>0 and r['volume']>0:
                fast,slow=r[f'x{n}'],r[f'e{n}']
                if family=='breakout':
                    side=1 if cl>r[f'h{n}'] and fast>slow else -1 if cl<r[f'l{n}'] and fast<slow else 0
                elif family=='pullback':
                    side=1 if fast>slow and r['low']<=fast and cl>fast and cl>r['open'] else -1 if fast<slow and r['high']>=fast and cl<fast and cl<r['open'] else 0
                elif family=='falsebreak':
                    side=1 if r['low']<r[f'l{n}'] and cl>r[f'l{n}'] and cl>r['open'] else -1 if r['high']>r[f'h{n}'] and cl<r[f'h{n}'] and cl<r['open'] else 0
                elif family=='meanrevert':
                    side=1 if cl<r[f'm{n}']-2*r[f'sd{n}'] else -1 if cl>r[f'm{n}']+2*r[f'sd{n}'] else 0
                else:raise ValueError(family)
                if side:pending=dict(side=side,atr=r['atr'],contract=r['contract'],end_ns=int(r['end_ns']))
            floating=(pos['side']*(cl-pos['entry_price'])*mult-pos['entry_fee'])*250/pos['risk_cash'] if pos else 0
            curve.append([int(r['end_ns']),cash+floating,day])
        if pos:
            close(pos,cl,int(r['end_ns']),day,'end');curve[-1][1]=cash
        paths[sym]=curve
    return trades,paths,missing_rolls


def curve_frame(paths):
    parts=[]
    for sym,rows in paths.items():
        z=pd.DataFrame(rows,columns=['ns','pnl','day']).drop_duplicates('ns',keep='last').set_index('ns').pnl.rename(sym)
        parts.append(z)
    return pd.concat(parts,axis=1).sort_index().ffill().fillna(0).sum(axis=1)


def metrics(curve,trades,start,end):
    times=pd.to_datetime(curve.index,utc=True).tz_convert('Asia/Shanghai')
    if CALENDAR:
        assigned=[]
        for t in times:
            day=t.strftime('%Y-%m-%d')
            j=bisect.bisect_right(CALENDAR,day) if t.hour>=18 else bisect.bisect_left(CALENDAR,day)
            assigned.append(CALENDAR[j] if j<len(CALENDAR) else day)
        bins=pd.DatetimeIndex(assigned,tz='Asia/Shanghai')
    else:bins=times
    mask=(bins>=pd.Timestamp(start,tz='Asia/Shanghai'))&(bins<pd.Timestamp(end,tz='Asia/Shanghai'))
    z=curve.loc[mask]; prev=curve.loc[bins<pd.Timestamp(start,tz='Asia/Shanghai')]
    base=float(prev.iloc[-1]) if len(prev) else 0
    delta=z-base; vals=np.r_[0,delta.values]
    dd=float(np.max(np.maximum.accumulate(vals)-vals))
    net=float(delta.iloc[-1]) if len(delta) else 0
    monthly=pd.Series(z.values,index=bins[mask]).resample('ME').last().ffill()
    if len(monthly):
        first=monthly.iloc[0]-base
        monthly=monthly.diff();monthly.iloc[0]=first
    ts=[t for t in trades if start<=t['exit_day']<end]
    wins=sum(t['net']>0 for t in ts)
    return dict(net=round(net,2),max_drawdown=round(dd,2),trades=len(ts),win_rate=round(wins/len(ts),4) if ts else 0,positive_months=int((monthly>0).sum()),months=len(monthly),monthly={str(i.date())[:7]:round(v,2) for i,v in monthly.items()},mean_r=round(float(np.mean([t['r'] for t in ts])),4) if ts else 0)


def portfolio(trades,capital=50000,risk=250,slip_note=''):
    # Chronological entries compete; stop closes timestamped at end of stop bar.
    accepted=[];active=[];realized=0.;skipped={};marks=[];peak_margin=0.;peak_positions=0
    for t in sorted(trades,key=lambda x:(x['entry_ns'],x['symbol'])):
        remaining=[]
        for a in active:
            if a['exit_ns']<t['entry_ns']:
                realized+=a['net']*a['lots']
            else:remaining.append(a)
        active=remaining
        # Each open trade valued at last fully closed bar, including accrued fees.
        equity=capital+realized;used=0.;openrisk=0.
        for a in active:
            prior=[m for m in a['marks'] if m[0]<=t['entry_ns']]
            px=prior[-1][1] if prior else a['entry_price']
            mult=SPECS[a['symbol']][0]
            equity+=(a['side']*(px-a['entry_price'])*mult-a['entry_fee'])*a['lots']
            used+=px*mult*.15*a['lots'];openrisk+=a['risk_cash']*a['lots']
        one_margin=t['entry_price']*SPECS[t['symbol']][0]*.15
        qty=min(math.floor(risk/t['risk_cash']),math.floor(max(0,min(equity*.4-used,equity-used))/one_margin))
        reason=''
        if qty<1:reason='lot_risk_or_margin'
        elif len(active)>=4:reason='position_cap'
        elif sum(SECTORS[a['symbol']]==SECTORS[t['symbol']] for a in active)>=2:reason='sector_cap'
        elif openrisk+t['risk_cash']*qty>capital*.02:reason='risk_cap'
        elif any(a['symbol']==t['symbol'] for a in active):reason='symbol_busy'
        if reason:skipped[reason]=skipped.get(reason,0)+1;continue
        a={**t,'lots':qty};active.append(a);accepted.append(a)
        peak_margin=max(peak_margin,used+qty*one_margin);peak_positions=max(peak_positions,len(active))
    # Aggregate actual-lot marked paths. Independent accepted paths include fees.
    paths={}
    adverse={}
    for j,a in enumerate(accepted):
        mult=SPECS[a['symbol']][0];q=a['lots'];side=a['side'];ep=a['entry_price']
        rows=[[a['entry_ns'],-a['entry_fee']*q,a['entry_day']]]
        worst=list(rows)
        for ns,px,bad,day in a['marks']:
            rows.append([ns,(side*(px-ep)*mult-a['entry_fee'])*q,day])
            worst.append([ns,(side*(bad-ep)*mult-a['entry_fee'])*q,day])
        rows.append([a['exit_ns'],a['net']*q,a['exit_day']]);worst.append(rows[-1])
        paths[str(j)]=rows;adverse[str(j)]=worst
    curve=curve_frame(paths) if paths else pd.Series([0.],index=[pd.Timestamp(START,tz='Asia/Shanghai').value])
    # Forward filled per-trade worst extrema are overly conservative and not a
    # simultaneous executable path, so only close marked drawdown is reported.
    return accepted,curve,dict(skipped=skipped,peak_entry_margin=round(peak_margin,2),peak_positions=peak_positions,capital=capital,risk_per_trade=risk,margin_assumption=.15,margin_cap=.4)


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    models=[dict(id=f'{f}_{n}_{k}',family=f,n=n,k=k) for f in ['breakout','pullback'] for n in [64,128,256] for k in [2,3]]
    protocol=dict(period=[START,END],split=[TRAIN_END,VALID_END],models=models,selection='Train: net>0, at least 4/6 positive months and 50 trades; rank net/max(dd,250). Validation must net>0 and at least 2/3 positive months; choose first train-ranked survivor. If none, retain top train score as FAILED exploratory control, never claim stability.',capital=50000,risk=250,account_max_drawdown_target=7500,notes=['Existing history has prior research exposure; chronological test is not untouched data.','15m natural-clock complete and partial bars, indicators use observed bars of each actual contract.','Prior observed trading-day mapping used at each day; contract OHLC never spliced for indicators.','Next contiguous bar open entries, one tick adverse slippage per fill, stops gap to worse open.','No stop update on same bar; no fixed profit target; max320 observed bars.','15% margin proxy and frozen fee snapshot, not historical broker schedules.'])
    save('protocol.json',protocol)
    frames,raws,dates,audit,digest=load();save('data-audit.json',dict(products=audit,input_rows_sha256=digest,db_bytes=DB.stat().st_size,source_mtime=DB.stat().st_mtime,fee_sha256=hashlib.sha256(FEES.read_bytes()).hexdigest()))
    scores=[];cache={}
    for model in models:
        trades,paths,_=simulate(frames,raws,model)
        curve=curve_frame(paths)
        result=dict(model=model,train=metrics(curve,trades,START,TRAIN_END),validation=metrics(curve,trades,TRAIN_END,VALID_END),test=metrics(curve,trades,VALID_END,END),full=metrics(curve,trades,START,END))
        scores.append(result);cache[model['id']]=(trades,curve)
        print(model['id'],result['train']['net'],result['validation']['net'],result['test']['net'],flush=True)
    ranked=sorted(scores,key=lambda x:x['train']['net']/max(x['train']['max_drawdown'],250),reverse=True)
    eligible=[x for x in ranked if x['train']['net']>0 and x['train']['positive_months']>=4 and x['train']['trades']>=50 and x['validation']['net']>0 and x['validation']['positive_months']>=2]
    chosen=eligible[0] if eligible else ranked[0]
    save('comparison.json',dict(results=scores,selected=chosen['model'],passed_development_gate=bool(eligible),train_rank=[x['model']['id'] for x in ranked]))
    model=chosen['model'];trades,curve=cache[model['id']]
    final={}
    for slip in [1,2,3]:
        ts=trades if slip==1 else simulate(frames,raws,model,slip)[0]
        acc,eq,limits=portfolio(ts)
        eq.to_csv(OUT/f'account-equity-slip{slip}.csv',header=['net_equity'])
        pd.DataFrame([{k:v for k,v in t.items() if k!='marks'} for t in acc]).to_csv(OUT/f'account-trades-slip{slip}.csv',index=False)
        final[str(slip)]=dict(full=metrics(eq,acc,START,END),train=metrics(eq,acc,START,TRAIN_END),validation=metrics(eq,acc,TRAIN_END,VALID_END),test=metrics(eq,acc,VALID_END,END),limits=limits,by_product={s:round(sum(t['net']*t['lots'] for t in acc if t['symbol']==s),2) for s in SPECS},median_hold_hours=float(np.median([t['hold_hours'] for t in acc])) if acc else 0)
        # metrics trade average R is per lot; account net curve correctly includes lots.
    save('account-summary.json',final)
    save('selected-trades.json',[{k:v for k,v in t.items() if k!='marks'} for t in trades])
    save('implementation.json',dict(script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),numpy=np.__version__,pandas=pd.__version__))
    print('SELECTED',model,'development_gate',bool(eligible),flush=True)
    print(json.dumps(final,ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':main()
