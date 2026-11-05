"""V3 causal context: synchronous peers, close-location pressure, conditional rejection."""
import itertools
import numpy as np
import pandas as pd
import explore_v2_signals as v2

# Ex-ante research groupings, not return-fitted correlations or tradeable baskets.
GROUPS={'chemical':['BU','EB','EG','L','MA','PP','V'],
        'construction':['FG','SA','SH','RB','SS'],
        'materials':['SF','SM','SI','PB'],
        'agricultural':['C','CF','CJ','JD','SR','UR']}


def templates(universe):
    grids={'P1':dict(pullback_bars=[3,6],peer_strength_min=[1.,2.],peer_breadth_min=[.6,.75]),
           'P2':dict(pressure_bars=[6,12],pressure_min=[.2,.4],volume_min=[1.,1.5]),
           'P3':dict(range_bars=[12,24],peer_strength_max=[.5,1.],er_max=[.25,.4])}
    result=[]
    for family,grid in grids.items():
        for i,vals in enumerate(itertools.product(*grid.values()),1):
            result.append(dict(candidate_id=f'{family}-C{i:02}',hypothesis_id=family,parameters=dict(zip(grid,vals)),
                universe=universe,directions=['long','short'],entry_sessions=['day'],allow_overnight=False))
    return result


def features(f,spec,fees):
    f=v2.features(f,spec,fees)
    f['activity_ratio']=f.volume/f.volume_prior20.replace(0,np.nan)
    f['norm_day_move']=np.nan
    for _,g in f.loc[f.valid].groupby(['day','segment'],sort=False):
        if int(g.minute.iloc[0])==540:f.loc[g.index,'norm_day_move']=(g.close-g.open.iloc[0])/g.atr
    for n in (3,6,12):
        for name in (f'lagged_move{n}',f'pressure{n}',f'box_high{n}',f'box_low{n}'):f[name]=np.nan
    for _,g in f.loc[f.valid].groupby('segment',sort=False):
        clv=(2*g.close-g.high-g.low)/(g.high-g.low).replace(0,np.nan)
        clv=clv.fillna(0)
        for n in (3,6,12):
            f.loc[g.index,f'lagged_move{n}']=g.close.shift()-g.close.shift(n+1)
            f.loc[g.index,f'pressure{n}']=(clv*g.volume).shift().rolling(n,min_periods=n).sum()/g.volume.shift().rolling(n,min_periods=n).sum().replace(0,np.nan)
            f.loc[g.index,f'box_high{n}']=g.high.shift().rolling(n,min_periods=n).max()
            f.loc[g.index,f'box_low{n}']=g.low.shift().rolling(n,min_periods=n).min()
    return f


def peers(frames):
    """Exact same completed-bar timestamp only. Never ffill absent/stale peers."""
    out={name:f.copy() for name,f in frames.items()}
    for f in out.values():f['peer_median']=np.nan;f['peer_breadth']=np.nan;f['peer_count']=0
    for members in GROUPS.values():
        available=[name for name in members if name in out]
        if len(available)<3:continue
        panel=pd.concat({name:out[name].set_index('end_ns').norm_day_move for name in available},axis=1)
        for name in available:
            others=panel.drop(columns=name);count=others.notna().sum(axis=1)
            median=others.median(axis=1).where(count>=2)
            breadth=others.apply(np.sign).eq(np.sign(median),axis=0).sum(axis=1)/count.replace(0,np.nan)
            f=out[name];f['peer_median']=f.end_ns.map(median)
            f['peer_breadth']=f.end_ns.map(breadth).where(f.peer_median.notna())
            f['peer_count']=f.end_ns.map(count).fillna(0).astype(int)
    return out


def signals(f,candidate,tick):
    a={k:f[k].to_numpy() for k in f};p=candidate['parameters'];family=candidate['hypothesis_id']
    rows=[];cooldown=-1;day=None
    for i in range(len(f)):
        if a['day'][i]!=day:day=a['day'][i];cooldown=-1
        if not i or i<cooldown or not a['valid'][i] or not a['economic_gate'][i] or not a['activity_known'][i] or a['minute'][i]+5>=890 or a['segment'][i]!=a['segment'][i-1]:continue
        op,hi,lo,cl=[a[k][i] for k in ('open','high','low','close')];emitted=None
        if family=='P1':
            peer=a['peer_median'][i]
            if not np.isfinite(peer) or abs(peer)<p['peer_strength_min'] or a['peer_breadth'][i]<p['peer_breadth_min']:continue
            d=int(np.sign(peer));prior=a[f"lagged_move{p['pullback_bars']}"][i]
            if np.isfinite(prior) and prior*d<=0 and (cl-op)*d>0 and (cl-a['high' if d==1 else 'low'][i-1])*d>0:
                n=p['pullback_bars'];z=a['low' if d==1 else 'high'][max(0,i-n):i+1]
                emitted=(d,float(np.min(z)-tick if d==1 else np.max(z)+tick))
        elif family=='P2':
            n=p['pressure_bars'];pressure=a[f'pressure{n}'][i]
            prior=a[f'lagged_move{n}'][i];upper=a[f'box_high{n}'][i];lower=a[f'box_low{n}'][i]
            if not np.isfinite(pressure+prior+upper+lower) or abs(pressure)<p['pressure_min'] or abs(prior)>a['atr'][i-1] or a['activity_ratio'][i]<p['volume_min']:continue
            d=int(np.sign(pressure));level=upper if d==1 else lower
            if (cl-level)*d>=tick and (cl-op)*d>0:emitted=(d,lower-tick if d==1 else upper+tick)
        else:
            peer=a['peer_median'][i];er=a['er12'][i-1]
            if not np.isfinite(peer+er) or abs(peer)>p['peer_strength_max'] or er>p['er_max']:continue
            n=p['range_bars'];upper=a[f'range_high{n}'][i];lower=a[f'range_low{n}'][i];atr=a['atr'][i-1]
            if not np.isfinite(upper+lower+atr) or atr<=0:continue
            up=hi>=upper+.25*atr;down=lo<=lower-.25*atr
            if up and not down and lower<=cl<upper and cl<op and cl<a['close'][i-1]:emitted=(-1,hi+tick)
            elif down and not up and lower<cl<=upper and cl>op and cl>a['close'][i-1]:emitted=(1,lo-tick)
        if emitted:
            d,stop=emitted;rows.append(dict(index=i,side=d,stop=float(stop),origin_end=int(a['end_ns'][i])))
            cooldown=i+24
    return rows


def matching(events,pool,repetitions=200,seed=20261108):
    """Same product/4-month block/5m clock/side, with predeclared continuous calipers."""
    rng=np.random.default_rng(seed);result=np.full((repetitions,len(events)),-1,dtype=np.int32)
    groups={}
    for i,e in enumerate(events):groups.setdefault(e['stratum'],[]).append(i)
    eligible={}
    for i,e in enumerate(events):
        choices=[]
        for j in pool['by_stratum'].get(e['stratum'],[]):
            c=pool['rows'][j];atr=e['atr_pct_value']/c['atr_pct_value'];activity=e['activity_ratio']/c['activity_ratio']
            if .67<=atr<=1.5 and .5<=activity<=2.:choices.append(j)
        eligible[i]=np.asarray(choices,dtype=np.int32)
    for rep in range(repetitions):
        used=set()
        for key,indices in sorted(groups.items()):
            for i in rng.permutation(indices):
                choices=[j for j in eligible[int(i)] if pool['rows'][int(j)]['time_key'] not in used]
                if choices:
                    j=int(rng.choice(choices));result[rep,int(i)]=j;used.add(pool['rows'][j]['time_key'])
    return result
