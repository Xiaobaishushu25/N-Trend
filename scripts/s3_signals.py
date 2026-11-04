"""Causal implementations of the 24 S2 templates. No future labels here."""
import collections
import datetime as dt
import numpy as np
import pandas as pd

NS=1_000_000_000
STEP=300*NS
SLOTS=np.r_[np.arange(540,615,5),np.arange(630,690,5),np.arange(810,900,5)]


def prepare(raw,calendar):
    f=raw.sort_values('start_ns').reset_index(drop=True).copy()
    times=pd.to_datetime(f.start_ns,utc=True).dt.tz_convert('Asia/Shanghai')
    f['day']=times.dt.strftime('%Y-%m-%d');f['month']=times.dt.strftime('%Y-%m')
    f['minute']=times.dt.hour*60+times.dt.minute
    f=f.loc[f.minute.isin(SLOTS)].reset_index(drop=True)
    a=f[['open','high','low','close','volume']].to_numpy(float)
    good=np.isfinite(a).all(axis=1)&(f.low>0)&(f.low<=f[['open','close']].min(axis=1))&(
        f.high>=f[['open','close']].max(axis=1))&(f.volume>=0)&(f.complete==1)&(
        f.constituent_count==5)&(f.end_ns-f.start_ns==STEP)&(f.start_ns%STEP==0)
    covered=f.day.map(calendar.days)
    good &= covered.ne(False)
    f['calendar_known']=covered.eq(True)
    f['valid']=good
    segment=[];last=None;seg=0
    slotidx={int(v):i for i,v in enumerate(SLOTS)}
    for r in f.itertuples():
        reset=last is None or not r.valid or not last.valid or r.contract!=last.contract
        if not reset:
            if r.day==last.day:
                reset=slotidx[r.minute]!=slotidx[last.minute]+1
            else:
                reset=last.minute!=895 or r.minute!=540
                if not reset and r.day in calendar.days and last.day in calendar.days:
                    ix=np.searchsorted(calendar.trading,last.day,side='right')
                    reset=ix>=len(calendar.trading) or calendar.trading[ix]!=r.day
        if reset:seg+=1
        segment.append(seg);last=r
    f['segment']=segment
    f['tr']=np.nan;f['atr']=np.nan;f['volume_prior20']=np.nan
    for _,g in f.loc[f.valid].groupby('segment',sort=False):
        prev=g.close.shift()
        tr=pd.concat([g.high-g.low,(g.high-prev).abs(),(g.low-prev).abs()],axis=1).max(axis=1)
        f.loc[g.index,'tr']=tr
        f.loc[g.index,'atr']=tr.rolling(12,min_periods=12).mean()
        f.loc[g.index,'volume_prior20']=g.volume.shift().rolling(20,min_periods=20).mean()
    f['atr_pct']=f.atr/f.close
    f['vol_bin']=np.searchsorted([.0005,.001,.002,.004],f.atr_pct.to_numpy(),side='right')
    return f


def aggregate(f,minutes):
    size=minutes//5;rows=[]
    if minutes==5:
        for r in f.loc[f.valid].itertuples():
            rows.append(dict(end_i=r.Index,start_ns=r.start_ns,end_ns=r.end_ns,segment=r.segment,
                             open=r.open,high=r.high,low=r.low,close=r.close))
    else:
        a=f.loc[f.valid].copy();a['bucket']=a.start_ns//(minutes*60*NS)*(minutes*60*NS)
        for (seg,bucket),g in a.groupby(['segment','bucket'],sort=True):
            if len(g)!=size or g.start_ns.tolist()!=list(range(int(bucket),int(bucket)+minutes*60*NS,STEP)):continue
            rows.append(dict(end_i=int(g.index[-1]),start_ns=int(bucket),end_ns=int(g.end_ns.iloc[-1]),segment=seg,
                open=g.open.iloc[0],high=g.high.max(),low=g.low.min(),close=g.close.iloc[-1]))
    fields=['end_i','start_ns','end_ns','segment','open','high','low','close']
    out=pd.DataFrame(rows,columns=fields)
    for name in ['tr','atr_prior','median_prior20','mean_prior5','atr_before_two']:out[name]=np.nan
    for _,g in out.groupby('segment',sort=False):
        prev=g.close.shift()
        tr=pd.concat([g.high-g.low,(g.high-prev).abs(),(g.low-prev).abs()],axis=1).max(axis=1)
        out.loc[g.index,'tr']=tr
        out.loc[g.index,'atr_prior']=tr.rolling(12,min_periods=12).mean().shift()
        out.loc[g.index,'median_prior20']=tr.shift().rolling(20,min_periods=20).median()
        out.loc[g.index,'mean_prior5']=tr.shift().rolling(5,min_periods=5).mean()
        out.loc[g.index,'atr_before_two']=tr.rolling(12,min_periods=12).mean().shift(2)
    return out


def signals(f,candidate,tick,higher=None):
    """Returns signal-time decisions and every H2 first breakout (not labels)."""
    higher=higher or {m:aggregate(f,m) for m in (5,15,30,60)}
    p=candidate['parameters'];family=candidate['hypothesis_id'];events=[];breaks=[]
    n=len(f);arr={c:f[c].to_numpy() for c in f.columns}
    m30=higher[30];m30end=m30.end_ns.to_numpy();m30rows=m30.to_dict('records')
    lookup={m:{int(r['end_i']):r for r in higher[m].to_dict('records')} for m in (5,15,30,60)}
    active=[];pending=None;cooldown=-1;prior_day=None
    for i in range(n):
        if arr['day'][i]!=prior_day:
            active=[];pending=None;cooldown=-1;prior_day=arr['day'][i]
        valid=arr['valid'][i] and np.isfinite(arr['atr'][i]) and arr['atr'][i]>0
        if not arr['valid'][i]:active=[];pending=None;continue
        if i and arr['segment'][i]!=arr['segment'][i-1]:active=[];pending=None
        eligible=valid and arr['minute'][i]+5<890 and i>=cooldown
        op,hi,lo,cl=[arr[c][i] for c in ('open','high','low','close')]
        emitted=None
        if family=='H1':
            keep=[]
            for state in active:
                state['age']+=1;d=state['side']
                state['extreme']=min(state['extreme'],lo) if d==1 else max(state['extreme'],hi)
                depth=(state['endpoint']-state['extreme'])*d
                if state['age']>6 or depth>p['pullback_fraction_max']*state['body']:continue
                confirm=state['counter'] and depth>0 and i>0 and (cl-arr['high' if d==1 else 'low'][i-1])*d>0
                if confirm and eligible and emitted is None:
                    emitted=(d,state['extreme']-d*tick,state['impulse_end'])
                if (cl-op)*d<0:state['counter']=True
                keep.append(state)
            active=keep
            b=lookup[p['momentum_minutes']].get(i)
            if b is not None and np.isfinite(b['atr_prior']) and b['atr_prior']>0:
                body=b['close']-b['open'];d=int(np.sign(body))
                if d and abs(body)>=p['impulse_atr_min']*b['atr_prior']:
                    active.append(dict(side=d,body=abs(body),endpoint=b['high'] if d==1 else b['low'],
                        extreme=b['high'] if d==1 else b['low'],counter=False,age=0,impulse_end=int(b['end_ns'])))
        elif family=='H2':
            if pending is not None:
                state=pending;state['age']+=1;d=state['side'];level=state['level'];tol=p['retest_tolerance_atr']*state['atr']
                adverse=lo if d==1 else hi
                if state['age']>6 or (adverse-level)*d < -tol:pending=None
                elif eligible and (adverse-level)*d<=tol and (cl-level)*d>=0 and (cl-op)*d>0 and (cl-arr['close'][i-1])*d>0:
                    emitted=(d,adverse-d*tick,state['break_end']);pending=None
            j=int(np.searchsorted(m30end,arr['start_ns'][i],side='right')-1);k=p['box_30m_bars']
            if i and valid and j>=k-1 and arr['segment'][i]==arr['segment'][i-1]:
                hist=m30rows[j-k+1:j+1]
                if all(b['segment']==arr['segment'][i] for b in hist):
                    upper=max(b['high'] for b in hist);lower=min(b['low'] for b in hist);width=upper-lower
                    vp=arr['volume_prior20'][i]
                    if width>=2*tick and abs(hist[-1]['close']-hist[0]['open'])/width<=.5 and vp>0 and arr['volume'][i]>p['breakout_volume_multiple']*vp:
                        d=1 if cl>upper and arr['close'][i-1]<=upper else (-1 if cl<lower and arr['close'][i-1]>=lower else 0)
                        if d:
                            level=upper if d==1 else lower
                            breaks.append(dict(index=i,side=d,stop=lower-tick if d==1 else upper+tick,origin_end=int(arr['end_ns'][i])))
                            if pending is None and np.isfinite(arr['atr'][i-1]):
                                pending=dict(side=d,level=level,atr=arr['atr'][i-1],age=0,break_end=int(arr['end_ns'][i]))
        else:
            b=lookup[p['expansion_minutes']].get(i)
            if b is not None and eligible and b['median_prior20']>0 and b['mean_prior5']<=b['median_prior20']:
                d=int(np.sign(b['close']-b['open']))
                if b['tr']>=p['expansion_ratio_min']*b['median_prior20'] and d and abs(b['close']-b['open'])/b['tr']>=.5:
                    j=int(np.searchsorted(m30end,arr['end_ns'][i],side='right')-1)
                    if j>=1:
                        a,z=m30rows[j-1:j+1];disp=z['close']-a['open']
                        if a['segment']==z['segment']==arr['segment'][i] and z['atr_before_two']>0 and disp*d>=p['trend_displacement_atr_min']*z['atr_before_two']:
                            emitted=(d,(b['low'] if d==1 else b['high'])-d*tick,int(b['end_ns']))
        if emitted is not None:
            d,stop,origin=emitted
            events.append(dict(index=i,side=d,stop=float(stop),origin_end=int(origin)))
            cooldown=i+24;active=[];pending=None
    return events,breaks
