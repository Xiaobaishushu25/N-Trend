"""New structural hypotheses; same frozen nested validation and risk gates as v1."""
import hashlib,itertools,pathlib
import numpy as np
import pandas as pd
from fresh_year_research import OUT
from fresh_year_walkforward import run

def configs():
    result=[]
    for opening,stop,reward,hold in itertools.product((2,4),(1.5,2.),(1.5,2.),(4,8)):
        result.append(dict(track='intraday',hypothesis='opening_range',config=('opening_range',opening,0.,stop,reward,hold,False,450)))
    for threshold,stop,reward,hold in itertools.product((1.,1.5),(1.5,2.),(1.,1.5),(4,8)):
        result.append(dict(track='intraday',hypothesis='vwap_reversal',config=('vwap_reversal',4,threshold,stop,reward,hold,False,450)))
    for width,stop,reward,hold in itertools.product((3.,4.),(1.5,2.),(2.,3.),(4,8)):
        result.append(dict(track='intraday',hypothesis='compression_breakout',config=('compression_breakout',4,width,stop,reward,hold,False,450)))
    for n,er,stop,reward,hold in itertools.product((16,32),(0.,.2),(2.,3.),(2.,3.),(32,64)):
        result.append(dict(track='swing',hypothesis='trend_pullback_recovery',config=('trend_pullback',n,er,stop,reward,hold,True,450)))
    return result

def anchor_features(d):
    if 'vwap_anchor' in d:return
    f=pd.DataFrame({k:d[k] for k in ('start_ns','open','high','low','close','contract','date','volume')})
    group=((f.start_ns.diff()>6*3600e9)|(f.date!=f.date.shift())|(f.contract!=f.contract.shift())).cumsum()
    position=f.groupby(group).cumcount()
    price=(f.high+f.low+f.close)/3
    d['vwap_anchor']=(price.mul(f.volume).groupby(group).cumsum()/f.volume.groupby(group).cumsum().replace(0,np.nan)).to_numpy()
    d['block_position']=position.to_numpy()
    for n in (2,4):
        for label,col,op in [('opening_hi','high','max'),('opening_lo','low','min')]:
            level=f[col].groupby(group).transform(lambda s:getattr(s.rolling(n),op)().shift())
            d[f'{label}{n}']=level.where(position==n).groupby(group).ffill().to_numpy()

def signal_for(d,candidate):
    family,n,k,stop,reward,hold,swing,cap=candidate['config'];c=d['close'];o=d['open'];a=d['atr']
    if family=='opening_range':
        anchor_features(d)
        sig=np.where((c>d[f'opening_hi{n}'])&(c>o),1,np.where((c<d[f'opening_lo{n}'])&(c<o),-1,0))
    elif family=='vwap_reversal':
        anchor_features(d)
        sig=np.where((c<d['vwap_anchor']-k*a)&(c>o),1,np.where((c>d['vwap_anchor']+k*a)&(c<o),-1,0))
    elif family=='compression_breakout':
        width=d['hi4']-d['lo4'];body_location=(c-d['low'])/np.maximum(d['high']-d['low'],d['tick'])
        sig=np.where((c>d['hi4'])&(body_location>=.7),1,np.where((c<d['lo4'])&(body_location<=.3),-1,0))
        sig=np.where(width<=k*a,sig,0)
    else:
        fast=d[f'mean{n}'];slow=d['mean128'];medium=d['mean64'];prior=np.r_[np.nan,c[:-1]-fast[:-1]]
        sig=np.where((c>fast)&(prior<=0)&(medium>slow)&(c>slow),1,np.where((c<fast)&(prior>=0)&(medium<slow)&(c<slow),-1,0))
        if k>0:sig=np.where(d['er64']>=k,sig,0)
    sig=np.where(sig!=np.r_[0,sig[:-1]],sig,0)
    distance=np.ceil(a*stop/d['tick'])*d['tick'];next_distance=np.r_[distance[1:],np.nan]
    costs=2*np.maximum(5,c*d['mult']*.00015)+2*d['tick']*d['mult']
    return np.where(costs<=.2*next_distance*d['mult'],sig,0)

if __name__=='__main__':
    run(candidates=configs(),signal_builder=signal_for,destination=OUT/'chronological_v2',protocol_overrides=dict(version=2,parent='chronological_v1',prior_outer_results_already_seen=True,source_v2_sha256=hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),hypotheses='日内：首2/4根开盘区间突破、相对当时段成交量加权典型价格的反向偏离后转向、前4根窄区间后的收盘突破。跨日：长期均价方向一致时，回调后重新站回16/32根均价。未根据外层成绩挑单个配置，沿用v1的产品选择和嵌套验证门槛。',regime='日内不使用v1的ER过滤；趋势回调有无ER64>=.2为预先声明的两个配置分支。',intraday='Same current-session/date-only data and ATR4; first2/4 bar range, cumulative volume-weighted typical price, or prior4 bar compression; hold4/8 and flat at recorded gap>30m, date/month/contract change.',swing='ATR20; close recrosses mean16/32 in mean64 vs mean128 trend direction; optional ER64>=.2; holds32/64; monthly/contract flattening.'))
