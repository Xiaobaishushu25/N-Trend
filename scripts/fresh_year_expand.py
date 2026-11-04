"""Expanded discovery: select products on training only, then fixed validation shortlist."""
import itertools,json,time,sys
import numpy as np
import pandas as pd
from fresh_year_research import prepare,trades_for,portfolio,OUT

def main():
    global OUT
    intraday='--intraday' in sys.argv
    riskaware='--risk-aware' in sys.argv
    if intraday:
        OUT=OUT/'intraday'; OUT.mkdir(exist_ok=True)
    if riskaware:
        OUT=OUT/'risk_aware'; OUT.mkdir(exist_ok=True)
    products,p=prepare(); configs=[]
    for family,n,stop,reward,hold in itertools.product(('breakout','momentum','revert'),(16,64,128,256),(1.,2.,3.),(2.,4.),(32,128)):
        for cap in ((300,450,600) if riskaware else (600,)):
            configs.append((family,n,2.,stop,reward,hold,not intraday,cap))
    results=[]; began=time.time(); cache={}
    for idx,cfg in enumerate(configs):
        trs={nm:trades_for(d,cfg) for nm,d in products.items()}; cache[cfg]=trs
        ranking=[]
        for nm,ts in trs.items():
            if riskaware and any(t[6]<-600 for t in ts if t[10] in p['train']):continue
            pnl=[t[6] for t in ts if t[10] in p['train']]
            monthly=[sum(t[6] for t in ts if t[10]==m) for m in p['train']]
            ranking.append((sum(pnl)-.5*np.std(monthly),nm,sum(x>0 for x in monthly)))
        ranking.sort(reverse=True)
        for count in (4,8,22):
            universe=[nm for score,nm,positive in ranking[:count] if score>0 and positive>=3]
            if not universe:continue
            selected={nm:trs[nm] for nm in universe}
            stat,_,_=portfolio(products,selected,p['train'])
            score=stat['net']-100000*max(0,stat['max_drawdown_pct']/100-.15)
            results.append(dict(config=cfg,universe=universe,count=count,train=stat,score=score))
        if idx%24==0:print('EXPAND',idx,len(configs),'seconds',round(time.time()-began),flush=True)
    results.sort(key=lambda r:r['score'],reverse=True)
    # At most 18 distinct finalists; duplicates of identical rule/universe are excluded.
    short=[]; seen=set(); buckets={}
    for row in results:
        key=(tuple(row['config']),tuple(sorted(row['universe'])))
        if key in seen:continue
        bucket=(row['config'][0],row['config'][-1])
        if riskaware and buckets.get(bucket,0)>=3:continue
        short.append(row);seen.add(key)
        buckets[bucket]=buckets.get(bucket,0)+1
        if len(short)==(27 if riskaware else 18):break
    for row in short:
        trs={nm:cache[tuple(row['config'])][nm] for nm in row['universe']}
        v,_,_=portfolio(products,trs,p['validation']);row['validation']=v
        row['selection_score']=v['net']-100000*max(0,v['max_drawdown_pct']/100-.15)
    (OUT/'expanded_train.json').write_text(json.dumps(results,indent=2),encoding='utf8')
    (OUT/'expanded_shortlist.json').write_text(json.dumps(short,indent=2),encoding='utf8')
    eligible=[r for r in short if r['train']['net']>0 and r['validation']['net']>0 and r['validation']['max_drawdown_pct']<20 and r['train']['max_drawdown_pct']<20 and (not riskaware or all(t[6]>=-600 for nm in r['universe'] for t in cache[tuple(r['config'])][nm] if t[10] in p['validation']))]
    winner=max(eligible or short,key=lambda r:r['selection_score']) if short else None
    print('DISCOVERY',json.dumps(dict(winner=winner,eligible=len(eligible))),flush=True)
    if winner:
        (OUT/'expanded_frozen.json').write_text(json.dumps(winner,indent=2),encoding='utf8')
        trs={nm:cache[tuple(winner['config'])][nm] for nm in winner['universe']}
        final=dict(winner=winner,configurations=len(configs),portfolio_candidates=len(results))
        for label,months in [('holdout',p['holdout']),('full_year',list(range(12)))]:
            stats,rows,path=portfolio(products,trs,months);final[label]=stats
            pd.DataFrame(rows).to_csv(OUT/f'expanded_{label}_trades.csv',index=False)
            pd.DataFrame(dict(end_ns=path[0],equity=path[1])).to_csv(OUT/f'expanded_{label}_equity.csv',index=False)
            final[label+'_double_slippage']=portfolio(products,trs,months,cost_extra=1)[0]
        final['passed']=bool(eligible) and final['full_year']['return_pct']>=20 and final['full_year']['max_drawdown_pct']<=20 and final['holdout']['return_pct']>=5
        (OUT/'expanded_results.json').write_text(json.dumps(final,indent=2),encoding='utf8')
        print('FINAL',json.dumps(final),flush=True)

if __name__=='__main__':main()
