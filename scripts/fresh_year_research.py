"""Independent one-year research. Never imports or reads prior strategy research."""
import argparse, bisect, datetime as dt, itertools, json, pathlib, sqlite3, time
import numpy as np
import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT/'outputs/fresh_year_20261104'
DB = ROOT/'data/research_market_history.sqlite'
# Contract point values and minimum ticks; execution costs below are research assumptions.
SPECS = dict(CF=(5,5), CJ=(5,5), FG=(20,1), MA=(10,1), SA=(20,1), SF=(5,2),
 SH=(30,1), SM=(5,2), SR=(10,1), UR=(20,1), c=(10,1), eb=(5,1), eg=(10,1),
 jd=(10,1), l=(5,1), pp=(5,1), v=(5,1), si=(5,5), bu=(10,1), pb=(5,5), rb=(10,1), ss=(5,5))

def prepare(start=None,endday=None,names=None,output_dir=None):
    db=sqlite3.connect(f'file:{DB.as_posix()}?mode=ro',uri=True)
    if endday is None:
        end=int(db.execute('select max(end_ns) from bars where period_seconds=900').fetchone()[0])
        endday=pd.Timestamp(end,tz='UTC').tz_convert('Asia/Shanghai').normalize()+pd.Timedelta(days=1)
    else:
        endday=pd.Timestamp(endday,tz='Asia/Shanghai')
    start=endday-pd.DateOffset(years=1) if start is None else pd.Timestamp(start,tz='Asia/Shanghai')
    lo,hi=start.tz_convert('UTC').value,endday.tz_convert('UTC').value
    maps=pd.read_sql_query('select product,effective_date,contract from mappings',db)
    products={}; summary=[]
    for product,mp in maps.groupby('product'):
        name=product.split('.')[1]; mult,tick=SPECS[name]
        if names is not None and name not in names:continue
        # Previous calendar day's published mapping avoids same-day selection lookahead.
        mp=mp.sort_values('effective_date'); dates=mp.effective_date.to_numpy(); contracts=mp.contract.to_numpy()
        parts=[]
        for contract in sorted(set(contracts)):
            f=pd.read_sql_query('select start_ns,end_ns,open,high,low,close,volume,complete from bars where contract=? and period_seconds=900 and start_ns>=? and start_ns<? order by start_ns',db,params=(contract,int(lo),int(hi)))
            if f.empty: continue
            local=pd.to_datetime(f.start_ns,utc=True).dt.tz_convert('Asia/Shanghai')
            previous=(local-pd.Timedelta(days=1)).dt.strftime('%Y-%m-%d').to_numpy()
            idx=np.searchsorted(dates,previous,side='right')-1
            active=(idx>=0)&(contracts[np.maximum(idx,0)]==contract)
            # Indicators are calculated only on in-window history, separately per contract.
            tr=pd.concat([f.high-f.low,(f.high-f.close.shift()).abs(),(f.low-f.close.shift()).abs()],axis=1).max(axis=1)
            f['atr']=tr.rolling(20,min_periods=20).mean().shift()
            for n in (8,16,32,64,128,256):
                f[f'hi{n}']=f.high.rolling(n).max().shift()
                f[f'lo{n}']=f.low.rolling(n).min().shift()
                f[f'mean{n}']=f.close.rolling(n).mean().shift()
                f[f'std{n}']=f.close.rolling(n).std().shift()
                f[f'er{n}']=(f.close-f.close.shift(n)).abs()/f.close.diff().abs().rolling(n).sum()
            f['contract']=contract; f['month']=((local-start)/pd.Timedelta(days=1)).astype(int)//31
            # Exact calendar blocks anchored to window start, not fixed 31-day months.
            month_count=(endday.year-start.year)*12+endday.month-start.month
            bounds=[start+pd.DateOffset(months=i) for i in range(month_count+1)]
            f['month']=np.searchsorted(np.array([b.value for b in bounds]),f.start_ns,side='right')-1
            f['date']=local.dt.strftime('%Y-%m-%d')
            f=f.loc[active].copy(); parts.append(f)
        if not parts: continue
        f=pd.concat(parts).sort_values('start_ns').reset_index(drop=True)
        # Flat before any gap exceeding 30 minutes, date boundary, or contract switch.
        f['last']=(f.start_ns.shift(-1)-f.start_ns>1800e9)|(f.contract.shift(-1)!=f.contract)|(f.date.shift(-1)!=f.date)|(f.month.shift(-1)!=f.month)
        f['roll']=(f.contract.shift(-1)!=f.contract)|(f.month.shift(-1)!=f.month)
        f.loc[len(f)-1,'last']=True
        products[name]={k:f[k].to_numpy() for k in f.columns}
        products[name].update(mult=mult,tick=tick)
        summary.append(dict(product=name,rows=len(f),incomplete=int((f.complete==0).sum()),start=str(pd.to_datetime(f.start_ns.min(),utc=True)),end=str(pd.to_datetime(f.end_ns.max(),utc=True))))
    db.close()
    rng=np.random.default_rng(20261104); order=rng.permutation(9).tolist()
    month_count=(endday.year-start.year)*12+endday.month-start.month
    protocol=dict(start=str(start),end_exclusive=str(endday),train=sorted(order[:6]),validation=sorted(order[6:]),holdout=[9,10,11],seed=20261104,blocks=[str(start+pd.DateOffset(months=i)) for i in range(month_count+1)],capital=50000,margin=.2,lot_risk=600,portfolio_risk=1800,cost='each side max(5 CNY, notional*0.00015) commission plus one tick slippage',data=summary)
    destination=OUT if output_dir is None else pathlib.Path(output_dir)
    destination.mkdir(exist_ok=True,parents=True); (destination/'protocol.json').write_text(json.dumps(protocol,ensure_ascii=False,indent=2),encoding='utf8')
    return products,protocol

def trades_for(d,cfg,signal_override=None):
    family,n,k,stopmul,reward,holding,*extras=cfg
    swing=bool(extras and extras[0]); last=d['roll'] if swing else d['last']
    o,h,l,c,t=[d[x] for x in ('open','high','low','close','start_ns')]
    atr=d['atr']; mult=d['mult']; tick=d['tick']; size=len(c)
    if signal_override is not None:signal=np.asarray(signal_override)
    elif family=='breakout':
        signal=np.where(c>d[f'hi{n}'],1,np.where(c<d[f'lo{n}'],-1,0))
    else:
        z=(c-d[f'mean{n}'])/np.maximum(d[f'std{n}'],tick)
        signal=np.where(z>k,-1,np.where(z<-k,1,0))
        if family=='momentum': signal=-signal
    result=[]; i=1
    while i<size-1:
        s=int(signal[i-1]); a=atr[i]
        if not s or not np.isfinite(a) or last[i-1] or not d['complete'][i-1] or not d['complete'][i]: i+=1; continue
        dist=np.ceil(a*stopmul/tick)*tick
        entry=o[i]+s*tick; fee=max(5,entry*mult*.00015)
        risk=(dist+tick)*mult+2*fee
        lotcap=extras[1] if len(extras)>1 else 600
        if risk>lotcap or dist<2*tick: i+=1; continue
        stop=entry-s*dist; target=entry+s*dist*reward
        j=i; exitprice=None; reason='time'
        while j<size:
            # Stops have priority if target and stop both touch. Gaps fill adversely.
            if (s==1 and l[j]<=stop) or (s==-1 and h[j]>=stop):
                exitprice=(min(o[j],stop) if s==1 else max(o[j],stop))-s*tick; reason='stop'; break
            if (s==1 and h[j]>=target) or (s==-1 and l[j]<=target):
                exitprice=target-s*tick; reason='target'; break
            if last[j] or j-i+1>=holding:
                exitprice=c[j]-s*tick; break
            j+=1
        if exitprice is None: j=size-1; exitprice=c[j]-s*tick
        pnl=(exitprice-entry)*s*mult-fee-max(5,exitprice*mult*.00015)
        result.append((int(t[i]),int(d['end_ns'][j]),i,j,s,entry,float(pnl),float(risk),float(entry*mult*.2),reason,int(d['month'][i])))
        i=j+1
    return result

def portfolio(products,alltrades,months,cost_extra=0,riskcap=1800):
    candidates=sorted((tr[0],name,tr) for name,trs in alltrades.items() for tr in trs if tr[10] in months)
    active=[]; realized=0.; accepted=[]; reject=0
    for stamp,name,tr in candidates:
        still=[]
        for nm,old in active:
            if old[1]<=stamp: realized+=old[6]-cost_extra*products[nm]['tick']*products[nm]['mult']*2
            else: still.append((nm,old))
        active=still; equity=50000+realized; margin=0.; risk=0.
        for nm,old in active:
            d=products[nm]; idx=np.searchsorted(d['end_ns'],stamp,side='right')-1
            equity+=(d['close'][idx]-old[5])*old[4]*d['mult']-max(5,old[5]*d['mult']*.00015)
            margin+=old[8]; risk+=old[7]
        if margin+tr[8]>equity*.8 or risk+tr[7]>riskcap:
            reject+=1; continue
        active.append((name,tr)); accepted.append((name,tr))
    times=sorted(set(int(t) for d in products.values() for t,m in zip(d['end_ns'],d['month']) if m in months))
    times=np.array(times,dtype=np.int64); delta=np.zeros(len(times)); adverse=np.zeros(len(times)); monthly=np.zeros(max(12,max(months,default=-1)+1))
    rows=[]
    for name,tr in accepted:
        d=products[name]; _,_,i,j,s,entry,pnl,risk,margin,reason,month=tr
        ix=np.searchsorted(times,d['end_ns'][i:j+1]); mult=d['mult']; tick=d['tick']
        fee=max(5,entry*mult*.00015)
        values=(d['close'][i:j+1]-entry)*s*mult-fee
        extra=cost_extra*tick*mult*2; values[-1]=pnl-extra
        delta[ix]+=np.diff(np.r_[0.,values])
        worst=(np.where(s==1,d['low'][i:j+1],d['high'][i:j+1])-entry)*s*mult-fee
        # Stop exits bound the adverse excursion; gap fills already included in actual P&L.
        worst=np.maximum(worst,-risk) if reason=='stop' else worst
        adverse[ix]+=np.minimum(0,worst-values)-extra
        monthly[month]+=pnl-extra
        rows.append(dict(product=name,entry_ns=tr[0],exit_ns=tr[1],side=s,entry=entry,pnl=pnl-extra,risk=risk,margin=margin,reason=reason,month=month))
    equity=50000+np.cumsum(delta); peak=np.maximum.accumulate(np.r_[50000,equity])[:-1]
    dd=float(np.max((peak-equity-adverse)/peak)) if len(times) else 0.
    net=float(sum(x['pnl'] for x in rows)); profits=[x['pnl'] for x in rows]
    return dict(net=net,return_pct=net/500,max_drawdown_pct=dd*100,trades=len(rows),rejected=reject,positive_months=int(sum(monthly[m]>0 for m in months)),monthly=monthly.tolist(),profit_factor=sum(x for x in profits if x>0)/max(1,-sum(x for x in profits if x<0))),rows,(times,equity)

def main():
    products,protocol=prepare(); print('PROTOCOL',json.dumps(protocol),flush=True)
    configs=[]
    for family,n,stop,reward,hold in itertools.product(('breakout','revert','momentum'),(8,16,32,64),(1.,1.5,2.),(1.5,3.,5.),(8,24)):
        configs.append((family,n,1.5,stop,reward,hold))
    results=[]; cache={}; began=time.time()
    for idx,cfg in enumerate(configs):
        trs={name:trades_for(d,cfg) for name,d in products.items()}
        train,_,_=portfolio(products,trs,protocol['train'])
        score=train['net']-100000*max(0,train['max_drawdown_pct']/100-.15)
        results.append(dict(config=cfg,train=train,score=score)); cache[tuple(cfg)]=trs
        if idx%24==0: print('SEARCH',idx,len(configs),'seconds',round(time.time()-began), 'train_net',round(train['net']),flush=True)
    results.sort(key=lambda x:x['score'],reverse=True)
    # Validation is accessed only for a predeclared shortlist of 12 training winners.
    shortlist=results[:12]
    for row in shortlist:
        row['validation'],_,_=portfolio(products,cache[tuple(row['config'])],protocol['validation'])
        v=row['validation']; row['selection_score']=v['net']-100000*max(0,v['max_drawdown_pct']/100-.15)
    eligible=[r for r in shortlist if r['train']['net']>0 and r['validation']['net']>0 and r['validation']['max_drawdown_pct']<20]
    winner=max(eligible or shortlist,key=lambda x:x['selection_score'])
    (OUT/'frozen_selection.json').write_text(json.dumps(dict(protocol=protocol,winner=winner,shortlist=shortlist),indent=2),encoding='utf8')
    trs=cache[tuple(winner['config'])]
    final=dict(selected=winner,search_count=len(configs))
    for label,months in [('holdout',protocol['holdout']),('full_year',list(range(12)))]:
        stats,rows,path=portfolio(products,trs,months); final[label]=stats
        pd.DataFrame(rows).to_csv(OUT/f'{label}_trades.csv',index=False)
        pd.DataFrame(dict(end_ns=path[0],equity=path[1])).to_csv(OUT/f'{label}_equity.csv',index=False)
        final[label+'_double_slippage']=portfolio(products,trs,months,cost_extra=1)[0]
    final['passed']=final['full_year']['return_pct']>=20 and final['full_year']['max_drawdown_pct']<=20 and final['holdout']['return_pct']>=5 and final['holdout']['max_drawdown_pct']<=20 and bool(eligible)
    (OUT/'results.json').write_text(json.dumps(final,indent=2),encoding='utf8')
    (OUT/'search_train.json').write_text(json.dumps(results,indent=2),encoding='utf8')
    print('FINAL',json.dumps(final),flush=True)

if __name__=='__main__':main()
