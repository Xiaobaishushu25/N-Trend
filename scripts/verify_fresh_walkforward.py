"""Causality checks for new signals and regression of the original frozen ledger."""
import json
import numpy as np
import pandas as pd
from fresh_year_research import prepare,trades_for,portfolio,OUT
from fresh_year_walkforward import configs,intraday_features,signal_for
from fresh_year_walkforward_v2 import configs as configs2,signal_for as signal2

def synthetic(mutation_start=None):
    rng=np.random.default_rng(167)
    ts=np.concatenate([(pd.Timestamp('2025-01-06 09:00',tz='Asia/Shanghai')+pd.Timedelta(days=k)+pd.to_timedelta(np.arange(32)*15,unit='m')).asi8 for k in range(12)])
    c=3000+np.cumsum(rng.normal(0,6,len(ts)));o=np.r_[c[0],c[:-1]];h=np.maximum(c,o)+2;l=np.minimum(c,o)-2
    if mutation_start is not None:
        c[mutation_start:]+=np.arange(len(c)-mutation_start)*50+1000
        o[mutation_start:]=c[mutation_start:]-20;h[mutation_start:]=c[mutation_start:]+3;l[mutation_start:]=o[mutation_start:]-3
    f=pd.DataFrame(dict(start_ns=ts,end_ns=ts+900000000000,open=o,high=h,low=l,close=c,volume=np.full(len(ts),100),complete=np.ones(len(ts),dtype=int),contract=np.full(len(ts),'X'),month=np.zeros(len(ts),dtype=int)))
    f['date']=pd.to_datetime(f.start_ns,utc=True).dt.tz_convert('Asia/Shanghai').dt.strftime('%Y-%m-%d')
    f['last']=(f.date!=f.date.shift(-1));f['roll']=False;f.loc[len(f)-1,'roll']=True
    tr=pd.concat([f.high-f.low,(f.high-f.close.shift()).abs(),(f.low-f.close.shift()).abs()],axis=1).max(axis=1)
    f['atr']=tr.rolling(20).mean().shift()
    for n in (8,16,32,64,128):
        f[f'hi{n}']=f.high.rolling(n).max().shift();f[f'lo{n}']=f.low.rolling(n).min().shift()
        f[f'mean{n}']=f.close.rolling(n).mean().shift();f[f'std{n}']=f.close.rolling(n).std().shift()
        f[f'er{n}']=(f.close-f.close.shift(n)).abs()/f.close.diff().abs().rolling(n).sum()
    d={key:f[key].to_numpy() for key in f};d.update(mult=10,tick=1)
    return d

def main():
    checks=0;nonzero={'intraday':0,'swing':0}
    for cutoff in (50,173,305):
        original=synthetic();mutated=synthetic(cutoff)
        day1=intraday_features(original);day2=intraday_features(mutated)
        for candidate in configs():
            a,b=(day1,day2) if candidate['track']=='intraday' else (original,mutated)
            first=signal_for(a,candidate['config']);second=signal_for(b,candidate['config'])
            assert np.array_equal(first[:cutoff],second[:cutoff]),('v1',cutoff,candidate)
            nonzero[candidate['track']]+=int(np.count_nonzero(first[:cutoff]))
            checks+=1
        for candidate in configs2():
            a,b=(day1,day2) if candidate['track']=='intraday' else (original,mutated)
            first=signal2(a,candidate);second=signal2(b,candidate)
            assert np.array_equal(first[:cutoff],second[:cutoff]),('v2',cutoff,candidate)
            nonzero[candidate['track']]+=int(np.count_nonzero(first[:cutoff]))
            checks+=1
    source=OUT/'risk_aware/expanded_frozen.json';w=json.loads(source.read_text())
    data,p=prepare(start='2025-10-28',endday='2026-10-28',names=w['universe'],output_dir=OUT/'chronological_checks')
    raw={name:trades_for(d,w['config']) for name,d in data.items()};stats,rows,_=portfolio(data,raw,list(range(12)))
    assert len(rows)==201
    assert abs(stats['net']-28697.6965)<.0001,stats
    for version in ('chronological_v1','chronological_v2'):
        protocol=json.loads((OUT/version/'protocol.json').read_text())
        for fold in protocol['folds']:
            assert max(fold['fit'])<min(fold['inner']) and max(fold['inner'])<min(fold['outer'])
    assert all(value>0 for value in nonzero.values()),nonzero
    day_trades=pd.read_csv(OUT/'chronological_v1/intraday/audited_outer_oos_trades.csv')
    entry_day=pd.to_datetime(day_trades.entry_ns,utc=True).dt.tz_convert('Asia/Shanghai').dt.date
    exit_day=pd.to_datetime(day_trades.exit_ns-1,utc=True).dt.tz_convert('Asia/Shanghai').dt.date
    assert (entry_day==exit_day).all()
    result=dict(causal_prefix_checks=checks,nonzero_signal_prefixes=nonzero,original_frozen_trade_count=len(rows),original_frozen_net=stats['net'],chronological_splits_checked=True,intraday_no_overnight_checked=True)
    (OUT/'chronological_checks/verification.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print(json.dumps(result))

if __name__=='__main__':main()
