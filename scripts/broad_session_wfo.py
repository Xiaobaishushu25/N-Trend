"""Broad exposed research batch: night/multisession, cross asset, OI and curve.

One small ridge learner, four input views; causal monthly fitting and frozen WFO.
No production orders. Historical fee/margin and quote limitations remain explicit.
"""
import argparse
import collections
import datetime as dt
import itertools
import json
import math
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
import numpy as np
import pandas as pd
import simple_wfo as c
from daily_context_wfo import utility

ROOT=c.ROOT
OUT=ROOT/'artifacts/broad_session_wfo_20261105'
PRIOR=ROOT/'artifacts/simple_wfo_20261105'
HOLDS=[12,48,144]
ALPHAS=[.1,10.]
QUANTILES=[.6,.8]
STOPS=[2.,4.]
LOCAL=['ret6','ret24','ret96','eff24','location24','volume24','range_ratio','body','sin','cos','night']
CROSS=['peer12','residual12']
STRUCTURE=['oi12','price_oi','curve','curve_change']
VIEWS={'local':LOCAL,'cross_asset':LOCAL+CROSS,'position_curve':LOCAL+STRUCTURE,'combined':LOCAL+CROSS+STRUCTURE}


def iso(t):return c.iso(t)
def ns(t):return c.ns(t)
def fee(s,price,factor=1.,opening=False):
    return c.fee(s,'open',price,factor) if opening else np.maximum(c.fee(s,'close_today',price,factor),c.fee(s,'close_yesterday',price,factor))


def features(f):
    """Concrete-contract closed-bar features. All right-aligned, no future fills."""
    f=f.sort_values('start_ns').reset_index(drop=True).copy()
    t=pd.to_datetime(f.start_ns,utc=True).dt.tz_convert('Asia/Shanghai')
    f['date']=t.dt.strftime('%Y-%m-%d');f['minute']=t.dt.hour*60+t.dt.minute
    # After midnight still use yesterday's pre-known day selection, never next morning's.
    key=t.dt.tz_localize(None).dt.normalize()-pd.to_timedelta((t.dt.hour<6).astype(int),unit='D')
    f['market_date']=key.dt.strftime('%Y-%m-%d')
    f['valid']=np.isfinite(f[['open','high','low','close','volume']]).all(axis=1)&(f.low>0)&(f.low<=f[['open','close']].min(axis=1))&(f.high>=f[['open','close']].max(axis=1))&(f.volume>=0)&(f.complete==1)&(f.constituent_count==5)&(f.end_ns-f.start_ns==c.STEP)&(f.start_ns%c.STEP==0)
    for col in LOCAL+CROSS+STRUCTURE+['ret12','atr']:f[col]=np.nan
    # One invocation per real contract. No prices are joined across a roll.
    cl=f.close.where(f.valid);prev=cl.shift()
    tr=pd.concat([f.high-f.low,(f.high-prev).abs(),(f.low-prev).abs()],axis=1).max(axis=1).where(f.valid)
    atr=tr.rolling(12,min_periods=12).mean();f['atr']=atr
    for h in [6,12,24,96]:f[f'ret{h}']=(cl-cl.shift(h))/atr.replace(0,np.nan)
    path=cl.diff().abs().rolling(24).sum();f['eff24']=(cl-cl.shift(24))/path.replace(0,np.nan)
    hi=f.high.where(f.valid).rolling(24).max();lo=f.low.where(f.valid).rolling(24).min()
    f['location24']=(cl-lo)/(hi-lo).replace(0,np.nan)
    f['volume24']=np.log1p(f.volume)-np.log1p(f.volume.shift().rolling(24).median())
    f['range_ratio']=(f.high-f.low)/atr.replace(0,np.nan);f['body']=(cl-f.open)/atr.replace(0,np.nan)
    oi=f.close_oi.where(f.close_oi>0);f['oi12']=np.log(oi/oi.shift(12));f['price_oi']=np.sign(f.ret12)*f.oi12
    angle=f.minute*2*np.pi/1440;f['sin']=np.sin(angle);f['cos']=np.cos(angle);f['night']=((f.minute>=1260)|(f.minute<360)).astype(float)
    return f


def monthcode(contract,reference_year):
    import re
    code=re.search(r'(\d+)$',contract).group(1)
    year=2000+int(code[:2]) if len(code)==4 else reference_year//10*10+int(code[0])
    if len(code)==3 and year<reference_year-5:year+=10
    return year*12+int(code[-2:])


def prepare():
    OUT.mkdir(parents=True,exist_ok=True);dest=OUT/'prepared';dest.mkdir(exist_ok=False);(dest/'raw').mkdir()
    choices=pd.read_csv(PRIOR/'prepared/contract_choices.csv')
    assert not (choices.known_at_day>=choices.day).any()
    shutil.copy2(PRIOR/'prepared/specs.json',dest/'specs.json');shutil.copy2(PRIOR/'prepared/folds.json',dest/'folds.json');choices.to_csv(dest/'contract_choices.csv',index=False)
    db=sqlite3.connect(c.DB.resolve().as_uri()+'?mode=ro',uri=True);db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
    products=sorted(choices['product'].unique());panels={};coverage=[];curve_choices=[]
    for product in products:
        contracts=sorted(choices.loc[choices['product']==product,'contract'].unique())
        # Load ALL source contracts for that product, including secondary liquidity.
        import re
        contracts=[r[0] for r in db.execute('select distinct contract from bars where period_seconds=300') if re.sub(r'\d+$','',r[0].split('.')[-1]).upper()==product]
        raw={}
        for contract in contracts:
            f=pd.read_sql_query('select contract,start_ns,end_ns,open,high,low,close,volume,close_oi,complete,constituent_count from bars where period_seconds=300 and contract=? order by start_ns',db,params=(contract,))
            f=features(f);raw[contract]=f
            f.to_pickle(dest/'raw'/f'{contract}.pkl')
        pc=choices.loc[choices['product']==product].copy();selected=[]
        allbars=pd.concat(raw.values(),ignore_index=True)
        day=allbars.loc[allbars.minute.isin(c.SLOTS)&allbars.valid]
        volumes=day.groupby(['date','contract']).volume.sum()
        for q in pc.itertuples():
            try:vs=volumes.loc[q.known_at_day].sort_values(ascending=False)
            except KeyError:continue
            others=[code for code in vs.index if code!=q.contract and monthcode(code,int(q.day[:4]))>int(q.day[:4])*12+int(q.day[5:7])]
            other=others[0] if others else None
            f=raw[q.contract];g=f.loc[f.market_date==q.day].copy()
            if not len(g):continue
            g['raw_i']=g.index.to_numpy();g['secondary']=other or ''
            if other:
                b=raw[other].set_index('end_ns');diff=monthcode(other,int(q.day[:4]))-monthcode(q.contract,int(q.day[:4]))
                if diff:
                    op=g.end_ns.map(b.close.where(b.valid));g['curve']=(op-g.close)/(diff*g.atr.replace(0,np.nan))
            selected.append(g);curve_choices.append(dict(product=product,date=q.day,known_at=q.known_at_day,main=q.contract,secondary=other))
        chosen=pd.concat(selected,ignore_index=True).sort_values('start_ns').reset_index(drop=True)
        # Curve changes never bridge a different pair.
        grp=(chosen.contract.ne(chosen.contract.shift())|chosen.secondary.ne(chosen.secondary.shift())).cumsum()
        chosen['curve_change']=chosen.groupby(grp).curve.diff(6)
        panels[product]=chosen
        coverage.append(dict(product=product,raw_rows=len(allbars),chosen_rows=len(chosen),night_rows=int(chosen.night.sum()),curve_rows=int(chosen.curve.notna().sum()),oi_rows=int(chosen.oi12.notna().sum())))
        print('prepared broad',product,len(chosen),'raw',len(allbars),flush=True)
    db.close()
    # Synchronous completed-bar peer return; no ffill and own contribution removed.
    stack=pd.concat([pd.DataFrame({'product':p,'end_ns':f.end_ns,'ret12':f.ret12.where(f.valid)}) for p,f in panels.items()],ignore_index=True)
    ag=stack.groupby('end_ns').ret12.agg(['sum','count'])
    for p,f in panels.items():
        own=f.ret12.where(f.valid);count=f.end_ns.map(ag['count'])-own.notna().astype(int)
        f['peer12']=(f.end_ns.map(ag['sum'])-own.fillna(0))/count.where(count>=2)
        f['residual12']=f.ret12-f.peer12
        # Trade only following a completed immediate5m quote; prior volume>=100
        # makes one lot <=1% of previous observed5m activity (not future volume).
        samples=[]
        for contract,g in f.groupby('contract',sort=False):
            raw=pd.read_pickle(dest/'raw'/f'{contract}.pkl')
            # Predict from the selected completed bar, execute next real-contract bar.
            inds=g.raw_i.to_numpy(int);nxt=inds+1;valid=nxt<len(raw);inds=inds[valid];nxt=nxt[valid];gg=g.loc[valid]
            times=raw.start_ns.iloc[nxt].to_numpy();minutes=raw.minute.iloc[nxt].to_numpy()
            ok=(times==gg.end_ns.to_numpy())&(minutes%30==5)&gg.valid.to_numpy()&(gg.volume.to_numpy()>=100)&(gg.high.to_numpy()>gg.low.to_numpy())&np.isfinite(gg.atr.to_numpy())
            h=gg.loc[ok].copy();h['signal_ns']=h.end_ns;h['entry_ns']=times[ok];h['entry_i']=nxt[ok]
            h['entry_open']=raw.open.iloc[nxt[ok]].to_numpy()
            h['entry_valid']=raw.valid.iloc[nxt[ok]].to_numpy()&(raw.volume.iloc[nxt[ok]].to_numpy()>0)&(raw.high.iloc[nxt[ok]].to_numpy()>raw.low.iloc[nxt[ok]].to_numpy())
            for hold in HOLDS:
                target=nxt[ok]+hold;inside=target<len(raw);y=np.full(len(h),np.nan);end=np.full(len(h),np.iinfo(np.int64).max,dtype=np.int64)
                positions=np.flatnonzero(inside);ti=target[inside];good=raw.valid.iloc[ti].to_numpy();positions=positions[good];ti=ti[good]
                y[positions]=(raw.open.iloc[ti].to_numpy()-h.entry_open.iloc[positions].to_numpy())/h.atr.iloc[positions].to_numpy()
                end[positions]=raw.start_ns.iloc[ti].to_numpy();h[f'y{hold}']=y;h[f'yend{hold}']=end
            samples.append(h)
        panel=pd.concat(samples,ignore_index=True).sort_values('entry_ns').reset_index(drop=True);panel['month']=pd.to_datetime(panel.entry_ns,utc=True).dt.tz_convert('Asia/Shanghai').dt.strftime('%Y-%m')
        panel['product']=p;panel.to_pickle(dest/f'{p}.pkl')
    pd.DataFrame(coverage).to_csv(dest/'coverage.csv',index=False);pd.DataFrame(curve_choices).to_csv(dest/'curve_choices.csv',index=False)
    c.js(dest/'manifest.json',dict(database_sha256=c.rd(PRIOR/'prepared/manifest.json')['database_sha256'],files={str(p.relative_to(dest)):c.sha(p) for p in sorted(dest.rglob('*')) if p.is_file()}))


def fit_model(f,view,hold,alpha,cutoff):
    g=f.loc[(f.entry_ns<cutoff)&(f[f'yend{hold}']<cutoff)&np.isfinite(f[f'y{hold}'])]
    cols=VIEWS[view]
    if len(g)<2:return None
    x=g[cols].to_numpy(float);finite=np.isfinite(x)
    med=np.array([np.median(x[finite[:,i],i]) if finite[:,i].any() else 0. for i in range(len(cols))])
    filled=np.where(finite,x,med);scale=filled.std(axis=0);scale[scale<1e-9]=1.
    z=np.clip((filled-med)/scale,-5,5);z=np.column_stack([np.ones(len(z)),z,(~finite).astype(float)])
    y=g[f'y{hold}'].to_numpy();lo,hi=np.quantile(y,[.01,.99]);y=np.clip(y,lo,hi)
    reg=np.eye(z.shape[1])*alpha;reg[0,0]=1e-8
    coef=np.linalg.solve(z.T@z/len(z)+reg,z.T@y/len(z))
    pred=z@coef
    return dict(columns=cols,median=med.tolist(),scale=scale.tolist(),coef=coef.tolist(),n=len(g),last_label_ns=int(g[f'yend{hold}'].max()),
        quantiles={str(q):float(np.quantile(abs(pred),q)) for q in QUANTILES},target_clip=[float(lo),float(hi)])


def predict(f,model):
    if model is None:return np.full(len(f),np.nan)
    x=f[model['columns']].to_numpy(float);good=np.isfinite(x);z=np.clip((np.where(good,x,np.array(model['median']))-np.array(model['median']))/np.array(model['scale']),-5,5)
    return np.column_stack([np.ones(len(f)),z,(~good).astype(float)])@np.array(model['coef'])


def paths(panel,raw,hold,k,s,end,slip=2,factor=1.):
    """Vector paths for both sides; every exit remains on the actual held contract."""
    result={};tick=s['tick'];mult=s['multiplier']
    for contract,g in panel.groupby('contract',sort=False):
        a=raw[contract];times=a.start_ns.to_numpy();ends=a.end_ns.to_numpy();op=a.open.to_numpy();high=a.high.to_numpy();low=a.low.to_numpy()
        ex=a.valid.to_numpy()&(a.volume.to_numpy()>0)&(high>low)
        ii=g.entry_i.to_numpy(int);keys=g.index.to_numpy(int);n=len(g);atr=g.atr.to_numpy();signal=g.close.to_numpy();opens=op[ii]
        for side in [-1,1]:
            entry=opens+side*slip*tick;stop=np.floor((signal-side*k*atr)/tick)*tick if side==1 else np.ceil((signal-side*k*atr)/tick)*tick
            stopfill=stop-side*max(3,slip)*tick
            fo=fee(s,entry,factor,True);fs=fee(s,stopfill,factor);risk=(entry-stopfill)*side*mult+fo+fs
            eligible=((entry-stop)*side>0)&(risk>0)&(risk<=600)
            active=eligible.copy();unknown=active&~ex[ii]
            active&=ex[ii];exit_i=np.full(n,-1);base=np.full(n,np.nan);reason=np.full(n,'',dtype=object);pending=np.zeros(n,bool)
            # Extra room is for locked bars. Quote expiration is a clock known at entry.
            # Last7 calendar days of any scoring/evaluation window admit no new risk;
            # mandatory liquidation requests begin3 days before window boundary.
            active&=times[ii]<end-7*86400*10**9;eligible&=times[ii]<end-7*86400*10**9;unknown&=eligible
            for offset in range(hold+400):
                ids=np.flatnonzero(active)
                if not len(ids):break
                j=ii[ids]+offset;inside=(j<len(a))
                expired=ids[~inside];unknown[expired]=True;active[expired]=False
                ids=ids[inside];j=j[inside]
                before=times[j]<end
                expired=ids[~before];unknown[expired]=True;active[expired]=False;ids=ids[before];j=j[before]
                if not len(ids):continue
                executable=ex[j];due=(offset>=hold)|(times[j]-times[ii[ids]]>=3*86400*10**9)|(times[j]>=end-3*86400*10**9)|pending[ids]
                gap=(op[j]-stop[ids])*side<=0
                touch=((low[j] if side==1 else high[j])-stop[ids])*side<=0
                close=executable&(gap|due|touch);out=ids[close];oj=j[close]
                if len(out):
                    at_open=(gap|due)[close];base[out]=np.where(at_open,op[oj],stop[out]);exit_i[out]=oj
                    reason[out]=np.where(gap[close],'gap_stop',np.where(due[close],'time_or_window','stop'));active[out]=False
                pending[ids]|=(~executable)&(due|touch)
            unknown|=active
            for z in np.flatnonzero(eligible):
                key=(int(keys[z]),side)
                if unknown[z] or exit_i[z]<0:
                    result[key]=dict(unknown=True,entry_ns=int(times[ii[z]]),product=str(g['product'].iloc[z]),contract=contract);continue
                j=exit_i[z];exitprice=base[z]-side*slip*tick;fc=float(fee(s,exitprice,factor));net=(exitprice-entry[z])*side*mult-float(fo[z])-fc
                result[key]=dict(unknown=False,entry_ns=int(times[ii[z]]),signal_ns=int(g.signal_ns.iloc[z]),exit_ns=int(ends[j] if reason[z]=='stop' else times[j]),
                    contract=contract,product=str(g['product'].iloc[z]),side=side,lots=1,entry=float(entry[z]),exit=float(exitprice),stop=float(stop[z]),planned_risk=float(risk[z]),
                    open_fee=float(fo[z]),fees=float(fo[z])+fc,gross=float(net+fo[z]+fc),net=float(net),tick=tick,multiplier=mult,slippage_yuan=2*slip*tick*mult,reason=str(reason[z]),month=str(g.month.iloc[z]))
    return result


def isolated(indices,sides,table):
    tr=[];last=-1;unknown=0
    for i,d in zip(indices,sides):
        t=table.get((int(i),int(d)))
        if not t or t['entry_ns']<=last:continue
        if t['unknown']:unknown+=1;break
        tr.append(t);last=t['exit_ns']
    return tr,unknown


def protocol():
    return dict(version='broad-session-WFO-3',created=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(),
        exposure='EXPOSED_ADAPTIVE_RESEARCH; first2 workflows172800 candidate-window evaluations seen; older history counts unknown; independent prospective count0',
        folds=c.rd(OUT/'prepared/folds.json'),universe='top20 by train sample count, alphabetic ties, independently per fold; metadata22 products, no future PnL universe selection',
        information_views=VIEWS,learner='ridge with intercept; train-only median imputation, missing indicators, std normalization, X clipped+-5, Y train1/99 winsorization; continuous future raw return / known ATR. Direction learned; no named strategy library.',
        grid=dict(hold_observed5m_bars=HOLDS,ridge_alpha=ALPHAS,abs_prediction_train_quantiles=QUANTILES,stop_signal_ATR=STOPS,candidates_per_product=96),
        inner='first3 calendar months calibrate then6 expanding-past one-month validations; labels must exit strictly before fit cutoff. Training quantile from past fitted predictions. Final fit all9 months, same purge. All fits, coefficients and losers retained.',
        selection='same revision2 continuous economic utility versus cash0: (net+neighbor median net+6*median monthly net)/3 -.25*tradeDD -extra1tick/side cost, times n/(n+12); neighbors same view adjacent hold/alpha/quantile/stop; incomplete neighbor contribution capped0. Pick utility>0 complete candidate, max1/product and3products. Count/concentration/cost/parameter flags only.',
        contracts='main selection from previous observed day daytime volume, no mapping table; night market-date before06 belongs previous natural date, so no tomorrow choice. Secondary most liquid other eligible contract known same prior day; curve normalized by maturity difference; signal asset only, not a two-leg arbitrage.',
        peers='exact synchronous completed5m quotes of available selected assets, exclude own, at least2 others or missing. No forward fill. Inputs do not see current entry bar path.',
        execution='all source day and night concrete bars; entry05/35 minutes, signal previous immediate completed5m, prior volume>=100 (one lot<=1%prior5m volume), prior range>0. Fixed stop rounded away. Entry locked/invalid=unknown not zero; exit locked/missing defers on same concrete held contract.',
        holding='12/48/144 observed5m bars, absolute3-day clock requests liquidation; time exits at next executable actual open, stop at adverse stop/open; no profit targets. All quarters forbid new orders last7 calendar days and request exit last3 days, to avoid using out-of-window exit evidence. Does not use future last trading date.',
        fees='20261022 snapshot +1yuan broker/side; close uses MAX(today,yesterday) universally, avoids guessed historical trading-day calendar. Baseline2ticks/side. Fixed stress3ticks,2x fees,25%margin. Historical actual schedules unverified.',
        account='new50000 shared account over8 folds;1lot,perlot plan<=600 with3tick stop-exit buffer;totalrisk<=900,margin<=60%equity at20%notional baseline. Risk day09:00 clock reset,loss900halt;12%DD permanent stop and next-executable-open exit; continuous cash/peak/positions, never reset per fold.',
        test='all train selections frozen before one-shot all8 OOS and fixed stress; no fold scores printed until all done; no reset/rearm after losses',
        limits='historical data previously exposed; supplier contract coverage bias, no orderbook or limit parameters; 20/25%margin scenarios not actual broker schedules; future loss can exceed600 and20%DD. New market forward simulation NOT_STARTED; no live orders.')


def freeze():
    assert not (OUT/'freeze.json').exists()
    run=subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-p','test_broad_session_wfo.py','-v'],capture_output=True,text=True)
    (OUT/'tests.txt').write_text(run.stdout+run.stderr,encoding='utf8');assert run.returncode==0
    c.js(OUT/'protocol.json',protocol());dest=OUT/'snapshot';dest.mkdir()
    inputs=[Path(__file__),ROOT/'scripts/simple_wfo.py',ROOT/'scripts/daily_context_wfo.py',ROOT/'tests/test_broad_session_wfo.py',OUT/'protocol.json',OUT/'prepared/manifest.json']
    for p in inputs:shutil.copy2(p,dest/p.name)
    c.js(OUT/'freeze.json',dict(files={str(p.relative_to(ROOT)):c.sha(p) for p in inputs},database_sha256=c.rd(OUT/'prepared/manifest.json')['database_sha256']))
    print('Broad research workflow frozen; no candidate results yet.',flush=True)


def check():
    for p,h in c.rd(OUT/'freeze.json')['files'].items():assert c.sha(ROOT/p)==h,p
    for p,h in c.rd(OUT/'prepared/manifest.json')['files'].items():assert c.sha(OUT/'prepared'/p)==h,p


def load_raw(contracts):return {code:pd.read_pickle(OUT/'prepared/raw'/f'{code}.pkl') for code in contracts}


def train():
    check();dest=OUT/'training';dest.mkdir(exist_ok=False);specs=c.rd(OUT/'prepared/specs.json');folds=c.rd(OUT/'prepared/folds.json')
    full={p.stem:pd.read_pickle(p) for p in (OUT/'prepared').glob('*.pkl')};grid=list(itertools.product(VIEWS,HOLDS,ALPHAS,QUANTILES,STOPS));assert len(grid)==96
    for fold in folds:
        fd=dest/f"fold_{fold['fold']:02d}";fd.mkdir();start=ns(fold['train']);end=ns(fold['test'])
        frames={p:f.loc[(f.entry_ns>=start)&(f.entry_ns<end)].reset_index(drop=True) for p,f in full.items()}
        universe=sorted(frames,key=lambda p:(-len(frames[p]),p))[:20];c.js(fd/'universe.json',dict(selected=universe,counts={p:len(f) for p,f in frames.items()}))
        records=[];months=pd.period_range(fold['train'],periods=9,freq='M').astype(str).tolist()[3:]
        for product in universe:
            f=frames[product];raw=load_raw(f.contract.unique());tables={(h,k):paths(f,raw,h,k,specs[product],end) for h,k in itertools.product(HOLDS,STOPS)}
            models={};preds={};fitlog=[]
            for view,h,a in itertools.product(VIEWS,HOLDS,ALPHAS):
                for month in months:
                    model=fit_model(f,view,h,a,ns(month+'-01'));models[view,h,a,month]=model
                    part=f.loc[f.month==month];preds[view,h,a,month]=(part.index.to_numpy(),predict(part,model))
                    fitlog.append(dict(view=view,hold=h,alpha=a,validation_month=month,model=model))
            c.js(fd/f'{product}_all_fits.json',fitlog)
            local=[]
            for ci,param in enumerate(grid):
                view,h,a,q,k=param;idx=[];sides=[]
                for month in months:
                    model=models[view,h,a,month]
                    if model is None:continue
                    inds,pred=preds[view,h,a,month];keep=np.isfinite(pred)&(abs(pred)>model['quantiles'][str(q)])
                    idx.extend(inds[keep]);sides.extend(np.sign(pred[keep]).astype(int))
                order=np.argsort(idx);inds=np.array(idx,dtype=int)[order];directions=np.array(sides,dtype=int)[order]
                tr,unknown=isolated(inds,directions,tables[h,k]);m=c.metrics(tr,months)
                r=dict(product=product,candidate_id=f'{product}-B{ci:03d}',param=list(param),view=view,hold=h,alpha=a,quantile=q,stop=k,unknown=unknown,
                    **{key:value for key,value in m.items() if key!='monthly'},monthly_json=json.dumps(m['monthly']))
                local.append(r)
            lookup={tuple(r['param']):r for r in local}
            for r in local:
                param=r['param'];neighbors=[]
                for axis,vals in [(1,HOLDS),(2,ALPHAS),(3,QUANTILES),(4,STOPS)]:
                    ix=vals.index(param[axis])
                    for j in [ix-1,ix+1]:
                        if 0<=j<len(vals):
                            other=param.copy();other[axis]=vals[j];v=lookup[tuple(other)];neighbors.append(min(v['net'],0) if v['unknown'] else v['net'])
                r['neighbor_median_net']=float(np.median(neighbors));r['score']=utility(r['net'],r['neighbor_median_net'],r['median_month'],r['drawdown_yuan'],r['net']-r['extra_tick_net'],r['trades'])
                monthly=list(json.loads(r['monthly_json']).values());positive=sum(max(v,0.) for v in monthly)
                r['flags']=';'.join(flag for flag,yes in [('few_trades',r['trades']<30),('month_concentration',positive>0 and max(monthly)/positive>.5),('parameter_peak',r['net']>0 and r['neighbor_median_net']<=0),('extra_tick_sensitive',r['net']>0 and r['extra_tick_net']<=0),('unknown_path',r['unknown']>0)] if yes)
            records.extend(local);print('broad train',fold['fold'],product,len(local),flush=True)
        df=pd.DataFrame(records);eligible=df.loc[(df.score>0)&(df.unknown==0)].sort_values(['score','candidate_id'],ascending=[False,True]);selected=[]
        for _,r in eligible.drop_duplicates('product').head(3).iterrows():
            view,h,a,q,k=r['param'];model=fit_model(frames[r['product']],view,h,a,end)
            selected.append(dict(product=r['product'],candidate_id=r['candidate_id'],param=r['param'],model=model,score=r['score'],validation_net=r['net'],flags=r['flags']))
        df.drop(columns='param').to_csv(fd/'all_candidates.csv',index=False);c.js(fd/'selection.json',dict(**fold,selected=selected,search_count=len(df),fitted_models=len(universe)*24*6,empty_reason=None if selected else 'cash_utility_not_beaten'))
    c.js(dest/'manifest.json',dict(candidate_windows=15360,monthly_model_fits=23040,prior_family_windows=172800,files={str(p.relative_to(dest)):c.sha(p) for p in sorted(dest.rglob('*')) if p.is_file()}))
    print('ALL broad training selections sealed. No OOS summaries released.',flush=True)


def simulate(folds,full,specs,slip=2,factor=1.,margin_rate=.20):
    """Shared event ledger, same held-contract marks, all nights, no quarter reset."""
    orders=collections.defaultdict(list);needed=set();selections={}
    for fold in folds:
        fno=fold['fold'];sel=c.rd(OUT/f'training/fold_{fno:02d}/selection.json')['selected'];selections[fno]=sel
        for rank,r in enumerate(sel):
            p=r['product'];f=full[p].loc[(full[p].entry_ns>=ns(fold['test']))&(full[p].entry_ns<ns(fold['end']))].reset_index(drop=True)
            view,h,a,q,k=r['param'];pred=predict(f,r['model']);keep=np.isfinite(pred)&(abs(pred)>r['model']['quantiles'][str(q)])
            raw=load_raw(f.contract.unique());needed.update(raw)
            table=paths(f,raw,h,k,specs[p],ns(fold['end']),slip,factor)
            for i in np.flatnonzero(keep):
                t=table.get((int(i),int(np.sign(pred[i]))))
                if t:orders[t['entry_ns']].append((rank,r,t,fno))
    first=ns(folds[0]['test']);end=ns(folds[-1]['end']);raw=load_raw(needed)
    quotes=collections.defaultdict(dict)
    # Include all held contract quotes, not just whichever contract is newly selected.
    for code,f in raw.items():
        selected=f.loc[(f.start_ns>=first)&(f.start_ns<end),['start_ns','open','high','low','close','volume','valid']]
        for r in selected.itertuples():quotes[int(r.start_ns)][code]=r
    # Cash-only dates still get complete observed5m timeline from all panels.
    timeline=sorted(set(orders)|set(quotes)|set().union(*(set(f.entry_ns.loc[(f.entry_ns>=first)&(f.entry_ns<end)]) for f in full.values())))
    cash=50000.;peak=cash;halt=False;dayhalt=False;daykey=None;daystart=cash;active={};trades=[];snap=[];reject=[];unknown=[]
    def equity():return cash+sum((pos['mark']-pos['t']['entry'])*pos['t']['side']*pos['t']['multiplier'] for pos in active.values())
    intervals=[(ns(f['test']),ns(f['end']),f['fold']) for f in folds]
    for tm in timeline:
        bars=quotes.get(tm,{});fno=next((number for lo,hi,number in intervals if lo<=tm<hi),None)
        if fno is None:continue
        for p,pos in active.items():
            b=bars.get(pos['t']['contract'])
            if b is not None:pos['mark']=b.open
        stamp=pd.Timestamp(tm,tz='UTC').tz_convert('Asia/Shanghai');key=str((stamp-pd.Timedelta(hours=9)).date())
        if key!=daykey:daykey=key;daystart=equity();dayhalt=False
        eq=equity();peak=max(peak,eq);halt|=eq<=peak*.88;dayhalt|=daystart-eq>=900;riskhalt=halt or dayhalt
        for rank,r,t,entryfold in sorted(orders.get(tm,[]),key=lambda x:(x[0],x[1]['product'])):
            p=r['product'];eq=equity();risk=sum(z['t']['planned_risk'] for z in active.values());margin=sum(z['mark']*z['t']['multiplier']*margin_rate for z in active.values())
            if p in active:reason='active_product'
            elif riskhalt:reason='account_halt'
            elif t['unknown']:
                unknown.append(dict(product=p,time=iso(tm),contract=t['contract']));continue
            elif risk+t['planned_risk']>900:reason='shared_risk'
            elif margin+t['entry']*t['multiplier']*margin_rate>(eq-t['open_fee'])*.6:reason='margin'
            else:reason=None
            if reason:reject.append(dict(time=iso(tm),product=p,reason=reason,fold=fno));continue
            cash-=t['open_fee'];active[p]=dict(t=t.copy(),mark=float(t['entry']-t['side']*slip*t['tick']),candidate_id=r['candidate_id'],entry_fold=entryfold)
        for p,pos in list(active.items()):
            t=pos['t'];b=bars.get(t['contract']);exit_now=t['exit_ns']<=tm or (t['reason']=='stop' and t['exit_ns']==tm+c.STEP)
            if riskhalt and tm>t['entry_ns'] and b is not None and b.valid and b.volume>0 and b.high>b.low:
                price=b.open-t['side']*slip*t['tick'];fc=float(fee(specs[p],price,factor));gross=(price-t['entry'])*t['side']*t['multiplier']
                t.update(exit=float(price),exit_ns=tm,gross=float(gross),fees=float(t['open_fee']+fc),net=float(gross-t['open_fee']-fc),reason='account_risk_exit');exit_now=True
            if exit_now:
                cash+=t['net']+t['open_fee'];trades.append(dict(**t,entry_fold=pos['entry_fold'],exit_fold=fno,candidate_id=pos['candidate_id'],entry_time=iso(t['entry_ns']),exit_time=iso(t['exit_ns'])));del active[p]
            elif b is not None:pos['mark']=b.close
        eq=equity();peak=max(peak,eq);halt|=eq<=peak*.88;dayhalt|=daystart-eq>=900
        snap.append(dict(time_ns=tm+c.STEP,time=iso(tm+c.STEP),fold=fno,balance=cash,equity=eq,drawdown=(peak-eq)/peak,peak=peak,
            margin=sum(z['mark']*z['t']['multiplier']*margin_rate for z in active.values()),planned_risk=sum(z['t']['planned_risk'] for z in active.values()),halted=bool(halt)))
    if active:unknown.append(dict(reason='positions_outstanding_at_end',contracts=[z['t']['contract'] for z in active.values()]))
    return dict(trades=trades,equity=snap,rejections=reject,unknown=unknown,final_equity=cash if not unknown else None,selections=selections)


def evaluate():
    check();manifest=c.rd(OUT/'training/manifest.json')
    for p,h in manifest['files'].items():assert c.sha(OUT/'training'/p)==h,p
    dest=OUT/'evaluation';dest.mkdir(exist_ok=False);c.js(dest/'started.json',dict(training_manifest_sha256=c.sha(OUT/'training/manifest.json'),single_shot=True))
    full={p.stem:pd.read_pickle(p) for p in (OUT/'prepared').glob('*.pkl')};folds=c.rd(OUT/'prepared/folds.json');specs=c.rd(OUT/'prepared/specs.json')
    for label,slip,factor,margin in [('base',2,1.,.20),('stress',3,2.,.25)]:
        result=simulate(folds,full,specs,slip,factor,margin)
        for key in ['trades','equity','rejections']:pd.DataFrame(result.pop(key)).to_csv(dest/f'{label}_{key}.csv',index=False)
        c.js(dest/f'{label}_summary.json',result)
    c.js(dest/'complete.json',dict(files={p.name:c.sha(p) for p in sorted(dest.iterdir()) if p.is_file()}));print('ALL broad OOS and fixed stress scenarios finished; aggregate release permitted.',flush=True)


def report():
    check()
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    stats={};fold_rows=[];selected=[];counts=[];folds=c.rd(OUT/'prepared/folds.json');specs=c.rd(OUT/'prepared/specs.json')
    for fold in folds:
        fd=OUT/f"training/fold_{fold['fold']:02d}";sel=c.rd(fd/'selection.json');df=pd.read_csv(fd/'all_candidates.csv')
        counts.append(dict(fold=fold['fold'],candidates=len(df),positive_net=int((df.net>0).sum()),positive_utility=int(((df.score>0)&(df.unknown==0)).sum()),selected=len(sel['selected']),unknown_candidates=int((df.unknown>0).sum())))
        for r in sel['selected']:selected.append(dict(fold=fold['fold'],product=r['product'],candidate_id=r['candidate_id'],view=r['param'][0],hold=r['param'][1],alpha=r['param'][2],quantile=r['param'][3],stop=r['param'][4],validation_net=r['validation_net'],score=r['score'],flags=r['flags']))
    pd.DataFrame(selected,columns=['fold','product','candidate_id','view','hold','alpha','quantile','stop','validation_net','score','flags']).to_csv(OUT/'selected_parameters.csv',index=False)
    pd.DataFrame(counts).to_csv(OUT/'search_counts.csv',index=False)
    fig,ax=plt.subplots(figsize=(12,4.5))
    for label,factor in [('base',1.),('stress',2.)]:
        summary=c.rd(OUT/f'evaluation/{label}_summary.json');eq=pd.read_csv(OUT/f'evaluation/{label}_equity.csv');p=OUT/f'evaluation/{label}_trades.csv';tr=pd.read_csv(p) if p.stat().st_size>3 else pd.DataFrame()
        if len(tr):
            assert (tr.lots==1).all() and (tr.planned_risk<=600+1e-7).all();np.testing.assert_allclose(tr.gross,(tr.exit-tr.entry)*tr.side*tr.multiplier,atol=1e-7)
            np.testing.assert_allclose(tr.net,tr.gross-tr.fees,atol=1e-7)
            fees=[float(fee(specs[t.product],t.entry,factor,True)+fee(specs[t.product],t.exit,factor)) for t in tr.itertuples()];np.testing.assert_allclose(tr.fees,fees,atol=1e-7)
            assert abs(50000+tr.net.sum()-eq.balance.iloc[-1])<1e-6
            for _,g in tr.groupby('product'):
                g=g.sort_values('entry_ns');assert (g.entry_ns.iloc[1:].to_numpy()>=g.exit_ns.iloc[:-1].to_numpy()).all()
            win=tr.loc[tr.net>0].net;loss=tr.loc[tr.net<0].net;wr=float((tr.net>0).mean());pay=float(win.mean()/-loss.mean()) if len(win) and len(loss) else None
        else:wr=pay=None
        assert (eq.planned_risk<=900+1e-6).all();assert eq.time_ns.is_unique and eq.time_ns.is_monotonic_increasing
        halted=eq.loc[eq.halted];stats[label]=dict(final_equity=summary['final_equity'],net=summary['final_equity']-50000 if summary['final_equity'] is not None else None,known_balance=float(eq.balance.iloc[-1]),
            max_drawdown=float(eq.drawdown.max()),trades=len(tr),win_rate=wr,payoff=pay,unknown=summary['unknown'],first_halt=str(halted.iloc[0].time) if len(halted) else None,
            worst_trade=float(tr.net.min()) if len(tr) else None,losses_beyond600=int((tr.net< -600).sum()) if len(tr) else 0,overnight_trades=int((tr.entry_time.str[:10]!=tr.exit_time.str[:10]).sum()) if len(tr) else 0)
        starting=50000.
        for fold in folds:
            es=eq.loc[eq.fold==fold['fold']];ts=tr.loc[tr.exit_fold==fold['fold']] if len(tr) else []
            wins=np.array([t for t in (ts.net.to_numpy() if len(ts) else []) if t>0]);losses=np.array([t for t in (ts.net.to_numpy() if len(ts) else []) if t<0])
            curve=np.r_[starting,es.equity.to_numpy()];peak=np.maximum.accumulate(curve);ending=float(curve[-1]);sel=summary['selections'][str(fold['fold'])]
            fold_rows.append(dict(scenario=label,fold=fold['fold'],test_start=fold['test'],test_end=fold['end'],products=','.join(r['product'] for r in sel),net=ending-starting,
                max_drawdown=float(np.max((peak-curve)/peak)),trades=len(ts),win_rate=float((ts.net>0).mean()) if len(ts) else None,payoff=float(wins.mean()/-losses.mean()) if len(wins) and len(losses) else None,
                halted=bool(es.halted.iloc[-1]) if len(es) else False));starting=ending
        ax.plot(pd.to_datetime(eq.time,utc=True),eq.equity,label=label)
    ax.axhline(50000,color='gray',lw=.7);ax.set_ylabel('Shared equity CNY (day and night)');ax.legend();ax.grid(alpha=.2);fig.tight_layout();fig.savefig(OUT/'oos_equity.png',dpi=160);plt.close(fig)
    pd.DataFrame(fold_rows).to_csv(OUT/'fold_summary.csv',index=False);c.js(OUT/'reconciliation.json',dict(scenarios=stats,ledger_fees_integer_lots_and_risk_verified=True))
    lines=['# 第三轮：跨品种、持仓量、期限结构、夜盘和跨日持仓','',
        '**研究暴露数据上的自适应探索；独立前向样本为0。没有覆盖前两版结果，也没有按测试收益继续改动本版。**','',
        '|情景|期末权益|净收益|5分钟盯市回撤|交易数|胜率|盈亏比|','|---|---:|---:|---:|---:|---:|---:|']
    fmt=lambda v:'未知' if v is None else f'{v:,.2f}'
    for label,s in stats.items():lines.append(f"|{label}|{fmt(s['final_equity'])}|{fmt(s['net'])}|{s['max_drawdown']:.2%}|{s['trades']}|{'—' if s['win_rate'] is None else format(s['win_rate'],'.2%')}|{fmt(s['payoff'])}|")
    lines+=['','|轮次|测试开始|训练选择|净额|轮内回撤|交易数|胜率|盈亏比|停机|','|---|---|---|---:|---:|---:|---:|---:|---|']
    for f in fold_rows:
        if f['scenario']=='base':lines.append(f"|{f['fold']}|{f['test_start']}|{f['products'] or '空选'}|{f['net']:.2f}|{f['max_drawdown']:.2%}|{f['trades']}|{'—' if f['win_rate'] is None else format(f['win_rate'],'.1%')}|{fmt(f['payoff'])}|{f['halted']}|")
    if any(v['unknown'] for v in stats.values()):conclusion='存在未知成交/未平仓路径，账户最终净额未知。曲线已知部分不能作为完整收益或证明稳定盈利。'
    elif all(v['net']>0 for v in stats.values()):conclusion='固定两种成本场景均为正，但仍只是暴露历史候选，不能宣称已独立验证或已可实盘。'
    else:conclusion='本版未取得可确认的稳定正收益流程。不通过继续放宽规则、重置账户或在测试数据调参寻找盈利结果。'
    lines+=['',conclusion,'',
        '## 本轮思路和冻结方法','',
        '不预设命名交易策略，用同一个带正则化的线性预测器比较4种信息输入：单品种价格/成交量/时钟；加入同步跨品种；加入持仓量/期限结构；全部信息。方向和开仓预测阈值由训练数据学习。对比12/48/144根已观察5分钟bar持有期限（约1/4/12观察小时，跨休市可能更长）、2种正则强度、2种训练预测分位数和2种ATR止损倍数。','',
        '日夜盘全部纳入；多日持仓价格始终取入场真实合约，不随主力切换。主合约、次合约仅用更早观察日的日盘成交量选择；00:00—06:00沿用前一自然日夜盘选择，不借用次日主力。期限结构仅作为单腿方向预测信息，没有宣称双腿无风险套利。','',
        '跨品种用完全同步的已完成5分钟收益，目标自身剔除，缺少至少2个其他品种则留缺失，不能前填未来报价。所有标准化、缺失填补、目标截尾、系数及开仓分位数只在对应过去训练子窗拟合。每次拟合要求目标退出时刻严格早于拟合截止，避免训练标签越界。','',
        '仍为9个月训练、3个月冻结测试、3个月前移；最初3个月校准，其后6个月逐月扩展验证。每轮训练按样本覆盖度选择20品种；每品种96个候选，共15,360候选窗口评估，23,040个月度模型拟合（不含最多24个最终模型）。全部系数和失败候选保存。与前两版合计188,160次候选窗口；更早项目次数未知，相关拟合不能视作独立试验。','',
        '排序沿用第二版的连续效用与现金0比较，综合净额、月份中位数、参数邻域、回撤、额外1跳成本和交易次数缩减；不增加显著性检验或交易笔数门槛。最多3个品种，允许空选。所有8轮训练选择先封存，再一次性评估测试及事先固定压力情景。','',
        '## 执行、资金和局限','',
        '5万元共享账户、固定1整数手。信号由上一根立即相邻的已完成5分钟bar产生，在05/35分实际下一开盘参考价成交。上一根成交量至少100，使一手不超过此前观察成交量1%；不依据当前未来成交量筛信号。止损按信号ATR固定并向外取整；计划一手损失含成本及至少3跳退出缓冲，超过600不交易。未知一字价/零成交量入场不填零收益；已有仓位无法退出时在同一真实合约后续可执行报价处理。','',
        '12/48/144个已观察bar后退出，另有入场即确定的3自然日时间上限；休市/一字价可继续延迟。每个训练/测试窗口最后7自然日停止新开、最后3自然日开始请求平仓；这是预先声明的窗口退出规则，不根据未来最后交易日取理想收盘价。','',
        '跨轮共享权益、高水位、风险停机状态不重置；总计划风险900，保证金占权益最多60%，12%回撤永久停开并请求下一可成交开盘退出。日亏900以本地09:00时钟重置的风险日计算，不冒充逐日结算账单交易日。后续停机零收益和训练空选分别报告，不能当作策略表现稳定。','',
        '乘数/tick沿用核查表。手续费为2026-10-22快照加每边1元佣金，平仓一律取平今/平昨较高者，避免猜测历史夜盘交易日；基准每边2跳，压力3跳及双倍费用。保证金20%/25%名义价值占用均为保守场景，非真实逐日经纪商保证金认证。来源覆盖、缺盘口/涨跌停队列、历史参数缺失仍限制实际成交可信度。未来跳空亏损仍可超过600元/笔及20%账户回撤。','',
        '## 文件与复现','',
        '- fold_summary.csv：每轮共享账户净额、回撤、胜率、盈亏比、交易数。\n- selected_parameters.csv及training/fold_XX/selection.json：冻结参数和最终系数。\n- training/fold_XX/all_candidates.csv、*_all_fits.json：所有候选和逐月拟合，不仅成功案例。\n- evaluation/*_trades.csv、*_equity.csv：逐笔明细及连续日夜盘账户曲线。\n- prepared/coverage.csv、curve_choices.csv、contract_choices.csv：覆盖和因果合约选择。\n- protocol.json、freeze.json、snapshot、tests.txt、reconciliation.json：冻结与核验。','',
        '复现顺序python scripts/broad_session_wfo.py prepare → freeze → train → evaluate → report。输出阶段拒绝覆盖；新复现需独立输出目录。研究流程已经冻结，新行情前向模拟尚未开始，未授权或发送任何实盘委托。']
    (OUT/'REPORT.md').write_text('\n'.join(lines),encoding='utf8');c.js(OUT/'paper_forward.json',dict(status='NOT_STARTED',freeze_sha256=c.sha(OUT/'freeze.json'),independent_sample=0,live_orders=False))
    c.js(OUT/'delivery_manifest.json',dict(files={str(p.relative_to(OUT)):c.sha(p) for p in sorted(OUT.rglob('*')) if p.is_file() and p.name!='delivery_manifest.json'}));print(json.dumps(stats,ensure_ascii=False,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('stage',choices=['prepare','freeze','train','evaluate','report']);args=parser.parse_args();globals()[args.stage]()
