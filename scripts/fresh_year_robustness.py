"""Frozen parameter neighborhood diagnostics, never used to reselect the winner."""
import itertools,json
import pandas as pd
from fresh_year_research import prepare,trades_for,portfolio,OUT

def main():
    folder=OUT/'risk_aware';winner=json.loads((folder/'expanded_frozen.json').read_text());products,p=prepare()
    rows=[]
    for stop,reward,hold in itertools.product((1.75,2.,2.25),(3.5,4.,4.5),(96,128,160)):
        cfg=list(winner['config']);cfg[3:6]=[stop,reward,hold]
        trs={nm:trades_for(products[nm],cfg) for nm in winner['universe']}
        full,rr,_=portfolio(products,trs,list(range(12)));test,_,_=portfolio(products,trs,p['holdout'])
        rows.append(dict(stop=stop,reward=reward,hold=hold,year_return_pct=full['return_pct'],year_dd_pct=full['max_drawdown_pct'],tail_return_pct=test['return_pct'],worst_trade=min((r['pnl'] for r in rr),default=0),tail_positive=test['net']>0))
    pd.DataFrame(rows).to_csv(folder/'parameter_neighborhood.csv',index=False)
    x=pd.read_csv(folder/'audited_full_year_trades.csv');byproduct=x.groupby('product').pnl.sum();bymonth=x.groupby('month').pnl.sum();net=x.pnl.sum()
    summary=dict(count=len(rows),profitable_full_year=sum(r['year_return_pct']>0 for r in rows),target_year_pass=sum(r['year_return_pct']>=20 and r['year_dd_pct']<=20 for r in rows),positive_tail=sum(r['tail_positive'] for r in rows),risk_pass=sum(r['worst_trade']>=-600-.001 for r in rows),return_min=min(r['year_return_pct'] for r in rows),return_max=max(r['year_return_pct'] for r in rows),tail_return_min=min(r['tail_return_pct'] for r in rows),tail_return_max=max(r['tail_return_pct'] for r in rows),best_month_share=float(bymonth.max()/net),without_best_month_return_pct=float((net-bymonth.max())/500),without_best_product_return_pct=float((net-byproduct.max())/500),byproduct=byproduct.to_dict(),bymonth=bymonth.to_dict(),warning='All neighborhood diagnostics reuse observed data and are not independent validation.')
    (folder/'robustness.json').write_text(json.dumps(summary,indent=2),encoding='utf8');print(json.dumps(summary),flush=True)

if __name__=='__main__':main()
