"""Two fixed six-month backward validations. No optimization or product selection."""
import hashlib,json,pathlib
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from fresh_year_research import prepare,OUT
from fresh_year_audit import audit_frozen

def main():
    source=OUT/'risk_aware/expanded_frozen.json';snapshot=source.read_bytes();frozen=json.loads(snapshot)
    folder=OUT/'prior_two_halfyears';folder.mkdir(parents=True,exist_ok=True)
    (folder/'frozen_strategy.json').write_bytes(snapshot)
    specification=dict(source=str(source),sha256=hashlib.sha256(snapshot).hexdigest(),config=frozen['config'],universe=frozen['universe'],capital_each=50000,periods=[['2024-10-28','2025-04-28'],['2025-04-28','2025-10-28']],validation_only=True,selection_or_optimization=False,warmup='Only bars within each independent six-month interval, matching original window treatment.',cost='Same fees and slippage as frozen study; each period starts flat with CNY 50000.',force_flat='Same monthly block and main-contract switch flattening as frozen study.')
    (folder/'validation_protocol.json').write_text(json.dumps(specification,indent=2),encoding='utf8')
    summaries=[];monthly_rows=[];product_rows=[];fig,axes=plt.subplots(2,1,figsize=(10,7))
    for n,(start,end) in enumerate(specification['periods'],1):
        destination=folder/f'period_{n}';destination.mkdir(exist_ok=True)
        print('VALIDATE',n,start,end,flush=True)
        products,p=prepare(start=start,endday=end,names=frozen['universe'],output_dir=destination)
        assert set(products)==set(frozen['universe']),'Missing fixed product data'
        p={key:value for key,value in p.items() if key not in ('train','validation','holdout','seed')}
        p.update(blocks=p['blocks'][:7],evaluation='frozen validation only',months=list(range(6)),capital=50000,lot_risk=450,initial_positions='flat',optimization=False)
        (destination/'protocol.json').write_text(json.dumps(p,indent=2),encoding='utf8')
        audit=audit_frozen(products,p,frozen,destination,segments=[('six_months',list(range(6)))])
        stats=audit['six_months'];stress=audit['six_months_stress'];stats.update(period=n,start=start,end_exclusive=end,risk_constraints_passed=audit['risk_constraints_passed'],minute_coverage_issues=audit['minute_coverage_issues'],source_partial_bars=audit['source_partial_bars'],changed_trade_pnl=audit['changed_trade_pnl'],costs_doubled_return_pct=stress['return_pct_less_extra_commission'])
        t=pd.read_csv(destination/'audited_six_months_trades.csv');e=pd.read_csv(destination/'audited_equity.csv')
        affected=t.loc[[any(row.entry_ns<bar['end_ns'] and row.exit_ns>bar['start_ns'] and row.product==bar['product'] for bar in audit['source_partial_bars']) for row in t.itertuples()]]
        affected.to_csv(destination/'trades_touching_partial_bars.csv',index=False)
        stats['trades_touching_partial_bars']=affected.to_dict('records')
        stats.update(win_rate_pct=float((t.pnl>0).mean()*100),best_trade=float(t.pnl.max()),ending_equity=float(e.equity.iloc[-1]))
        pos=t.loc[t.pnl>0,'pnl'].sort_values(ascending=False)
        stats['top5_profit']=float(pos.head(5).sum());stats['net_without_top5']=float(stats['net']-pos.head(5).sum())
        for m in range(6):
            monthly_rows.append(dict(period=n,start=p['blocks'][m][:10],end_exclusive=p['blocks'][m+1][:10],net=float(t.loc[t.month==m,'pnl'].sum())))
        for name in frozen['universe']:
            trades=t[t['product']==name];product_rows.append(dict(period=n,product=name,trades=len(trades),net=float(trades.pnl.sum()),worst_trade=float(trades.pnl.min()) if len(trades) else 0))
        assert abs(t.pnl.sum()-stats['net'])<.001
        assert abs(e.equity.iloc[-1]-(50000+stats['net']))<.001
        summaries.append(stats)
        dates=pd.to_datetime(e.end_ns,utc=True).dt.tz_convert('Asia/Shanghai')
        axes[n-1].plot(dates,e.equity,lw=1,color='#176b73');axes[n-1].axhline(50000,color='gray',lw=.7)
        axes[n-1].set_title(f'Period {n}: {start} to {end} (exclusive), frozen rules')
        axes[n-1].set_ylabel('Equity (CNY)');axes[n-1].grid(alpha=.15)
        print('RESULT',json.dumps(stats),flush=True)
    assert source.read_bytes()==snapshot,'Frozen strategy was changed'
    pd.DataFrame(summaries).to_csv(folder/'summary.csv',index=False);pd.DataFrame(monthly_rows).to_csv(folder/'monthly.csv',index=False);pd.DataFrame(product_rows).to_csv(folder/'products.csv',index=False)
    (folder/'results.json').write_text(json.dumps(dict(protocol=specification,periods=summaries,monthly=monthly_rows,products=product_rows),indent=2),encoding='utf8')
    fig.tight_layout();fig.savefig(folder/'equity.png',dpi=150);plt.close(fig)
    lines=['# 冻结策略向前两期六个月验证','','固定使用原策略：MA、jd、eb、ss；15分钟 Z 分数动量，128根均值与标准差，阈值±2，20根ATR，2ATR止损，4倍止损距离止盈，最多128根，单手计划风险450元。每品种一手，组合计划风险1800元，保证金20%，入场保证金不超过权益80%。','','没有优化参数、重新选择品种或根据验证结果改变规则。各期独立从五万元、空仓开始，指标只使用当期数据预热。保持原研究的每月28日划分及月块末强制平仓，保持原手续费、滑点和真实跳空处理。','','| 区间（结束日不含） | 净利润/元 | 收益率 | 保守一分钟最大回撤 | 交易数 | 最大单笔亏损/元 | 双倍成本收益率 |','|---|---:|---:|---:|---:|---:|---:|']
    for s in summaries:lines.append(f"| {s['start']}～{s['end_exclusive']} | {s['net']:,.2f} | {s['return_pct']:.2f}% | {s['conservative_minute_drawdown_pct']:.2f}% | {s['trades']} | {-s['worst_trade']:.2f} | {s['costs_doubled_return_pct']:.2f}% |")
    lines+=['','收益率均是六个月实际收益率，未年化。双倍成本是原始滑点、手续费均加倍的敏感性检查；追加手续费没有重新模拟手续费导致的资金准入变化。回撤包括持仓浮盈亏与分钟内有利/不利极值的保守边界。','','| 月块起点 | 月块结束日（不含） | 净利润/元 |','|---|---|---:|']
    for r in monthly_rows:lines.append(f"| {r['start']} | {r['end_exclusive']} | {r['net']:,.2f} |")
    lines+=['','| 品种 | 2024-10-28～2025-04-27 | 2025-04-28～2025-10-27 |','|---|---:|---:|']
    for name in frozen['universe']:
        amounts=[next(r['net'] for r in product_rows if r['product']==name and r['period']==n) for n in (1,2)]
        lines.append(f'| {name} | {amounts[0]:,.2f} | {amounts[1]:,.2f} |')
    lines+=['','![两期独立权益曲线](equity.png)','','数据校验包括：15分钟OHLCV与其记录区间内原始一分钟聚合比较；记录区间内分钟覆盖检查；逐笔利润和最终权益核对；冻结策略文件运行前后SHA256一致。详见各期 minute_audit.json。','','数据局限：2025-01-27 四个品种各有一根14:45～14:59的14分钟不完整聚合K线，源库已标记complete=0。记录区间内分钟没有缺口，聚合价格一致，但通常交易时段最后一分钟未覆盖。eb和ss两笔月块末平仓使用了14:58分钟收盘价，两笔净利润分别347.01元和155.06元；若要求15:00准确平仓，当前数据库不足以验证这两笔的最终一分钟价格。未插值、补造价格或修改源库。','','这属于策略选定后的向更早历史回溯验证，未重新拟合。不能等同于未来实盘验证。隔夜跳空或止损无法成交可能超过450元计划风险乃至600元原上限，统计保留实际损失。没有交易盘口和排队数据，因此不能验证涨跌停时的真实可成交性。','','复现：`python scripts/fresh_year_prior_validation.py`。逐笔交易和分钟权益保存在 period_1、period_2 子目录。']
    (folder/'REPORT.md').write_text('\n'.join(lines),encoding='utf8')

if __name__=='__main__':main()
