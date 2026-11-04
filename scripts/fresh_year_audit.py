"""One-minute execution replay of frozen candidates; no rule selection here."""
import json,sqlite3
import numpy as np
import pandas as pd
from fresh_year_research import prepare,trades_for,portfolio,OUT,DB

def audit_frozen(products,p,frozen,folder,segments=None,trade_sources=None,reward_by_entry=None):
    cfg=frozen['config']; names=frozen['universe']
    db=sqlite3.connect(f'file:{DB.as_posix()}?mode=ro',uri=True)
    minute={}; revised={}; issues=[]; changed=0; partial=[]
    for name in names:
        d=products[name]; parts=[]
        for contract in sorted(set(d['contract'])):
            q=pd.read_sql_query('select start_ns,end_ns,open,high,low,close,volume from bars where contract=? and period_seconds=60 and start_ns>=? and start_ns<? order by start_ns',db,params=(contract,int(d['start_ns'][0]),int(d['end_ns'][-1])))
            q['contract']=contract; parts.append(q)
        f=pd.concat(parts).sort_values('start_ns').reset_index(drop=True)
        parent=np.searchsorted(d['start_ns'],f.start_ns,side='right')-1
        valid=(parent>=0)&(f.start_ns.to_numpy()<d['end_ns'][np.maximum(parent,0)])&(f.contract.to_numpy()==d['contract'][np.maximum(parent,0)])
        f=f.loc[valid].reset_index(drop=True);parent=parent[valid]; f['month']=d['month'][parent]
        grouped=f.assign(parent=parent).groupby('parent').agg(open=('open','first'),high=('high','max'),low=('low','min'),close=('close','last'),volume=('volume','sum'),count=('start_ns','size')).reindex(range(len(d['start_ns'])))
        expected_counts=(d['end_ns']-d['start_ns'])//60000000000
        if not np.array_equal(grouped['count'].to_numpy(),expected_counts):issues.append(dict(product=name,issue='missing 1m constituents within recorded interval'))
        for j in np.where(expected_counts!=15)[0]:
            partial.append(dict(product=name,start_ns=int(d['start_ns'][j]),end_ns=int(d['end_ns'][j]),minutes=int(expected_counts[j]),complete=int(d['complete'][j])))
        for col in ('open','high','low','close','volume'):
            if not np.allclose(grouped[col],d[col]):issues.append(dict(product=name,issue='1m/15m aggregation mismatch',column=col))
        a={k:f[k].to_numpy() for k in f};a.update(mult=d['mult'],tick=d['tick']);minute[name]=a
        expected=int(expected_counts.sum())
        if len(f)!=expected:issues.append(dict(product=name,expected_minutes=expected,actual=len(f)))
        raw=trades_for(d,cfg) if trade_sources is None else trade_sources.get(name,[]);revised[name]=[]
        for tr in raw:
            stamp,end,oi,oj,s,entry,pnl,risk,margin,reason,month=tr
            i=int(np.searchsorted(a['start_ns'],stamp));last=int(np.searchsorted(a['end_ns'],end))
            fee=max(5,entry*d['mult']*.00015);dist=(risk-2*fee)/d['mult']-d['tick']
            reward=cfg[4] if reward_by_entry is None else reward_by_entry[(name,stamp)]
            stop=entry-s*dist;target=entry+s*dist*reward;exitprice=None;why='time'
            for j in range(i,last+1):
                if (s==1 and a['low'][j]<=stop) or (s==-1 and a['high'][j]>=stop):
                    exitprice=(min(a['open'][j],stop) if s==1 else max(a['open'][j],stop))-s*d['tick'];why='stop';break
                if (s==1 and a['high'][j]>=target) or (s==-1 and a['low'][j]<=target):
                    exitprice=target-s*d['tick'];why='target';break
            if exitprice is None:j=last;exitprice=a['close'][j]-s*d['tick']
            net=(exitprice-entry)*s*d['mult']-fee-max(5,exitprice*d['mult']*.00015)
            if abs(net-pnl)>.01:changed+=1
            revised[name].append((stamp,int(a['end_ns'][j]),i,j,s,entry,float(net),risk,margin,why,month))
    db.close();result=dict(frozen=frozen,minute_coverage_issues=issues,source_partial_bars=partial,changed_trade_pnl=changed)
    segments=segments or [('train',p['train']),('validation',p['validation']),('holdout',p['holdout']),('full_year',list(range(12)))]
    for label,months in segments:
        stats,rows,path=portfolio(minute,revised,months)
        times,equity=path; favorable=np.zeros(len(times));adverse=np.zeros(len(times));margin_delta=np.zeros(len(times)+1);risk_delta=np.zeros(len(times)+1)
        allfees=0.
        for row in rows:
            a=minute[row['product']]; i=int(np.searchsorted(a['start_ns'],row['entry_ns']));j=int(np.searchsorted(a['end_ns'],row['exit_ns']));s=row['side'];entry=row['entry'];mult=a['mult']
            ix=np.searchsorted(times,a['end_ns'][i:j+1]);fee=max(5,entry*mult*.00015)
            value=(a['close'][i:j+1]-entry)*s*mult-fee;value[-1]=row['pnl']
            worst=(np.where(s==1,a['low'][i:j+1],a['high'][i:j+1])-entry)*s*mult-fee
            best=(np.where(s==1,a['high'][i:j+1],a['low'][i:j+1])-entry)*s*mult-fee
            # Include the entire exit minute's range for conservative ambiguity bounds.
            adverse[ix]+=np.minimum(0,worst-value);favorable[ix]+=np.maximum(0,best-value)
            margin_delta[ix[0]]+=row['margin'];margin_delta[ix[-1]+1]-=row['margin']
            risk_delta[ix[0]]+=row['risk'];risk_delta[ix[-1]+1]-=row['risk']
            exitprice=entry+s*(row['pnl']+2*fee)/mult
            allfees+=fee+max(5,exitprice*mult*.00015)
        peak=np.maximum.accumulate(np.r_[50000,equity+favorable])[1:]
        dd=float(np.max((peak-equity-adverse)/peak))*100 if len(times) else 0
        margin=np.cumsum(margin_delta)[:-1];risk=np.cumsum(risk_delta)[:-1]
        stats.update(conservative_minute_drawdown_pct=dd,worst_trade=min((r['pnl'] for r in rows),default=0),losses_over_600=sum(r['pnl'] < -600-.001 for r in rows),max_margin=float(max(margin,default=0)),max_margin_to_equity_pct=float(max(margin/np.maximum(equity+adverse,1),default=0))*100,max_open_planned_risk=float(max(risk,default=0)),commission_sum=allfees)
        result[label]=stats
        pd.DataFrame(rows).to_csv(folder/f'audited_{label}_trades.csv',index=False)
        if label==segments[-1][0]:pd.DataFrame(dict(end_ns=times,equity=equity,adverse_equity=equity+adverse,favorable_equity=equity+favorable,margin=margin,planned_open_risk=risk)).to_csv(folder/'audited_equity.csv',index=False)
        stressed,_,_=portfolio(minute,revised,months,cost_extra=1)
        stressed['net_less_extra_commission']=stressed['net']-allfees
        stressed['return_pct_less_extra_commission']=(stressed['net']-allfees)/500
        result[label+'_stress']=stressed
    if 'full_year' in result and 'holdout' in result:
        result['historical_constraints_passed']=not issues and result['full_year']['return_pct']>=20 and result['full_year']['conservative_minute_drawdown_pct']<=20 and result['full_year']['losses_over_600']==0 and result['holdout']['net']>0
    else:
        result['risk_constraints_passed']=not issues and all(result[label]['conservative_minute_drawdown_pct']<=20 and result[label]['losses_over_600']==0 for label,_ in segments)
    (folder/'minute_audit.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result

def main():
    folder=OUT/'risk_aware'; frozen=json.loads((folder/'expanded_frozen.json').read_text())
    products,p=prepare();result=audit_frozen(products,p,frozen,folder)
    print(json.dumps(result),flush=True)

if __name__=='__main__':main()
