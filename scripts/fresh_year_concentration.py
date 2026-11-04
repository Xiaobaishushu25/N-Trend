"""Concentration and calendar-month attribution of the frozen audited ledger."""
import json
import numpy as np
import pandas as pd
from fresh_year_research import OUT

def main():
    folder=OUT/'risk_aware'
    t=pd.read_csv(folder/'audited_full_year_trades.csv');e=pd.read_csv(folder/'audited_equity.csv')
    protocol=json.loads((OUT/'protocol.json').read_text())
    t['entry_time']=pd.to_datetime(t.entry_ns,utc=True).dt.tz_convert('Asia/Shanghai')
    t['exit_time']=pd.to_datetime(t.exit_ns,utc=True).dt.tz_convert('Asia/Shanghai')
    t['calendar_month']=t.exit_time.dt.strftime('%Y-%m')
    e['calendar_month']=pd.to_datetime(e.end_ns-1,utc=True).dt.tz_convert('Asia/Shanghai').dt.strftime('%Y-%m')
    monthly=e.groupby('calendar_month',sort=True).equity.last().to_frame('ending_equity')
    monthly['marked_pnl']=monthly.ending_equity.diff();monthly.iloc[0,monthly.columns.get_loc('marked_pnl')]=monthly.ending_equity.iloc[0]-50000
    monthly['closed_trade_pnl']=t.groupby('calendar_month').pnl.sum().reindex(monthly.index,fill_value=0)
    monthly.to_csv(folder/'calendar_month_pnl.csv')
    byproduct=t.groupby('product').agg(trades=('pnl','size'),net=('pnl','sum'),worst=('pnl','min'),best=('pnl','max'))
    byproduct['net_share']=byproduct.net/t.pnl.sum();byproduct.to_csv(folder/'product_attribution.csv')
    positive=t[t.pnl>0].sort_values('pnl',ascending=False);negative=t[t.pnl<0];net=t.pnl.sum();gross=positive.pnl.sum()
    concentration=[]
    for n in (1,3,5,10,20):
        amount=positive.head(n).pnl.sum()
        concentration.append(dict(top_n=n,profit=float(amount),share_net=float(amount/net),share_gross_profit=float(amount/gross),remaining_net=float(net-amount),remaining_return_pct=float((net-amount)/500)))
    positive.head(20).to_csv(folder/'largest_20_winners.csv',index=False)
    block=[]
    for m in range(12):
        label='train' if m in protocol['train'] else 'validation' if m in protocol['validation'] else 'repeated_tail'
        block.append(dict(block=m,start=protocol['blocks'][m][:10],end_exclusive=protocol['blocks'][m+1][:10],role=label,pnl=float(t.loc[t.month==m,'pnl'].sum())))
    pd.DataFrame(block).to_csv(folder/'research_block_pnl.csv',index=False)
    summary=dict(net=float(net),gross_profit=float(gross),gross_loss=float(-negative.pnl.sum()),trades=len(t),winners=len(positive),losers=len(negative),win_rate=float(len(positive)/len(t)),mean_winner=float(positive.pnl.mean()),mean_loser=float(negative.pnl.mean()),median_trade=float(t.pnl.median()),top_winners=concentration,largest_10=positive.head(10)[['product','entry_time','exit_time','side','pnl','risk','reason']].astype({'entry_time':str,'exit_time':str}).to_dict('records'),calendar_month=monthly.reset_index().to_dict('records'),products=byproduct.reset_index().to_dict('records'),research_blocks=block,note='Removing top trades is arithmetic attribution only; does not resimulate admission or reinvestment. Monthly marked P&L uses minute-close equity, not settlement prices.')
    assert abs(monthly.marked_pnl.sum()-net)<.001
    (folder/'concentration.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(summary,ensure_ascii=False))

if __name__=='__main__':main()
