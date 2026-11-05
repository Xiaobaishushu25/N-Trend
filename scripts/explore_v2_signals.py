"""Version 2: opening acceptance, failed auction reversal, VWAP inventory reversion.

All hypotheses and grids are frozen before evaluating real version-2 returns.
"""
import itertools
import numpy as np
import pandas as pd


def templates(universe):
    grids={
        'N1':dict(range_min_ticks=[8,16],break_buffer_atr=[0,.25],opening_efficiency_min=[.25,.5]),
        'N2':dict(range_bars=[12,24],spike_atr_min=[.25,.5],atr_min_ticks=[8,12]),
        'N3':dict(stretch_atr_min=[1.5,2.5],er_max=[.25,.4],atr_min_ticks=[8,12])}
    rows=[]
    for family,grid in grids.items():
        for i,values in enumerate(itertools.product(*grid.values()),1):
            rows.append(dict(candidate_id=f'{family}-C{i:02d}',hypothesis_id=family,parameters=dict(zip(grid,values)),
                universe=universe,directions=['long','short'],entry_sessions=['day'],allow_overnight=False))
    return rows


def features(f,spec,fees):
    f=f.copy()
    for k in ('er12','range_high12','range_low12','range_high24','range_low24'):f[k]=np.nan
    for _,g in f.loc[f.valid].groupby('segment',sort=False):
        path=g.close.diff().abs().rolling(12,min_periods=12).sum()
        f.loc[g.index,'er12']=(g.close-g.close.shift(12)).abs()/path.replace(0,np.nan)
        f.loc[g.index,'er12']=f.loc[g.index,'er12'].mask(path==0,0)
        for n in (12,24):
            f.loc[g.index,f'range_high{n}']=g.high.shift().rolling(n,min_periods=n).max()
            f.loc[g.index,f'range_low{n}']=g.low.shift().rolling(n,min_periods=n).min()
    weights=f.volume.where(f.valid,0)
    pv=((f.high+f.low+f.close)/3*weights).groupby([f.day,f.segment]).cumsum()
    volume=weights.groupby([f.day,f.segment]).cumsum()
    f['vwap_proxy']=pv/volume.replace(0,np.nan)
    f['atr_ticks']=f.atr/spec['tick']
    multiplier=spec['multiplier'];tick=spec['tick']
    # Signal-time forecast only: no future entry or exit quote is used by this gate.
    opening=fees['open']['fixed_yuan_per_lot']+f.close*multiplier*fees['open']['notional_rate']
    closing=fees['close_today']['fixed_yuan_per_lot']+f.close*multiplier*fees['close_today']['notional_rate']
    f['friction_R']=(opening+closing+2*tick*multiplier)/(f.atr*multiplier)
    f['economic_gate']=f.friction_R.le(.25)&f.atr.gt(0)
    relative=f.volume/f.volume_prior20.replace(0,np.nan)
    f['activity_bin']=np.searchsorted([.5,1,2],relative.to_numpy(),side='right')
    f['activity_known']=relative.notna()&np.isfinite(relative)
    f['scale_bin']=np.searchsorted([8,12,20],f.atr_ticks.to_numpy(),side='right')
    return f


def signals(f,candidate,tick):
    """Signal decisions use only completed observations; no labels or future gates."""
    a={k:f[k].to_numpy() for k in f};p=candidate['parameters'];family=candidate['hypothesis_id']
    events=[];cooldown=-1;previous_day=None;opening=[];range_state=None;consumed=False
    for i in range(len(f)):
        if a['day'][i]!=previous_day:
            previous_day=a['day'][i];cooldown=-1;opening=[];range_state=None;consumed=False
        if 540<=a['minute'][i]<570:
            opening.append(i)
            if a['minute'][i]==565 and len(opening)==6:
                expected=list(range(540,570,5))
                if [int(a['minute'][j]) for j in opening]==expected and all(a['valid'][j] for j in opening) and len({a['segment'][j] for j in opening})==1:
                    high=max(a['high'][j] for j in opening);low=min(a['low'][j] for j in opening)
                    body=a['close'][i]-a['open'][opening[0]]
                    if high>low and np.isfinite(a['atr'][i]):
                        range_state=dict(high=high,low=low,width=high-low,efficiency=abs(body)/(high-low),
                                         side=int(np.sign(body)),atr=float(a['atr'][i]),segment=a['segment'][i])
        valid=bool(a['valid'][i]) and np.isfinite(a['atr'][i]) and a['atr'][i]>0
        if not valid or a['minute'][i]+5>=890:continue
        eligible=i>=cooldown and a['economic_gate'][i] and a['activity_known'][i]
        emitted=None;op,hi,lo,cl=[a[k][i] for k in ('open','high','low','close')]
        if family=='N1':
            if range_state is None or consumed or not 570<=a['minute'][i]<660 or a['segment'][i]!=range_state['segment']:continue
            z=range_state;buffer=p['break_buffer_atr']*z['atr']
            d=1 if cl>z['high']+buffer else (-1 if cl<z['low']-buffer else 0)
            if d:
                consumed=True
                if eligible and d==z['side'] and z['width']/tick>=p['range_min_ticks'] and z['efficiency']>=p['opening_efficiency_min']:
                    emitted=(d,(z['low']-tick) if d==1 else (z['high']+tick))
        elif family=='N2':
            n=p['range_bars'];upper=a[f'range_high{n}'][i];lower=a[f'range_low{n}'][i]
            if not eligible or not i or a['atr_ticks'][i]<p['atr_min_ticks'] or a['segment'][i]!=a['segment'][i-1] or not np.isfinite(upper+lower):continue
            # Prior ATR freezes the excursion scale before the rejection candle.
            atr=a['atr'][i-1]
            if not np.isfinite(atr) or atr<=0:continue
            up=hi>=upper+p['spike_atr_min']*atr;down=lo<=lower-p['spike_atr_min']*atr
            if up and not down and lower<=cl<upper and cl<op and cl<a['close'][i-1]:emitted=(-1,hi+tick)
            elif down and not up and lower<cl<=upper and cl>op and cl>a['close'][i-1]:emitted=(1,lo-tick)
        else:
            if not eligible or not i or a['day'][i]!=a['day'][i-1] or a['segment'][i]!=a['segment'][i-1] or a['atr_ticks'][i]<p['atr_min_ticks']:continue
            if not np.isfinite(a['er12'][i-1]) or a['er12'][i-1]>p['er_max']:continue
            deviation=a['close'][i-1]-a['vwap_proxy'][i-1];atr=a['atr'][i-1]
            if not np.isfinite(deviation+atr) or atr<=0 or abs(deviation)<p['stretch_atr_min']*atr:continue
            side=-int(np.sign(deviation))
            if side and (cl-op)*side>0 and (cl-a['close'][i-1])*side>0 and (cl-a['vwap_proxy'][i])*side<0:
                emitted=(side,max(hi,a['high'][i-1])+tick if side==-1 else min(lo,a['low'][i-1])-tick)
        if emitted is not None:
            side,stop=emitted;events.append(dict(index=i,side=side,stop=float(stop),origin_end=int(a['end_ns'][i])))
            cooldown=i+24
    return events
