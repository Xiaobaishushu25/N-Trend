"""Explain frozen-strategy failure without selecting or optimizing rules."""
import json,sqlite3
import numpy as np
import pandas as pd
from fresh_year_research import prepare,trades_for,portfolio,OUT,DB

def mean(a):return float(np.mean(a)) if len(a) else None

def main():
    frozen=json.loads((OUT/'risk_aware/expanded_frozen.json').read_text())
    dest=OUT/'failure_diagnostic';dest.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(f'file:{DB.as_posix()}?mode=ro',uri=True)
    periods=[('older_six','2024-10-28','2025-04-28',OUT/'prior_two_halfyears/period_1/audited_six_months_trades.csv'),('prior_six','2025-04-28','2025-10-28',OUT/'prior_two_halfyears/period_2/audited_six_months_trades.csv'),('research_year','2025-10-28','2026-10-28',OUT/'risk_aware/audited_full_year_trades.csv')]
    allrows=[];summaries=[];productstats=[]
    for label,start,end,path in periods:
        products,p=prepare(start=start,endday=end,names=frozen['universe'],output_dir=dest/label)
        ts=pd.read_csv(path);gate=[]
        for name,d in products.items():
            signal=np.where(d['close']>d['mean128']+2*np.maximum(d['std128'],d['tick']),1,np.where(d['close']<d['mean128']-2*np.maximum(d['std128'],d['tick']),-1,0))
            dist=np.ceil(d['atr']*2/d['tick'])*d['tick'];entry=d['open']+np.r_[0,signal[:-1]]*d['tick']
            risk=(dist+d['tick'])*d['mult']+2*np.maximum(5,entry*d['mult']*.00015)
            raw=np.r_[False,signal[:-1]!=0]&np.isfinite(risk)&np.r_[False,d['complete'][:-1].astype(bool)]&d['complete'].astype(bool)&~np.r_[True,d['roll'][:-1]]
            gate.append(dict(product=name,signal_bars=int(raw.sum()),risk_eligible_bars=int((raw&(risk<=450)&(dist>=2*d['tick'])).sum()),median_signal_risk=float(np.median(risk[raw])),risk_gate_pass_pct=float(np.mean(risk[raw]<=450)*100)))
            episode=np.cumsum(np.r_[True,(signal[1:]!=signal[:-1])|(d['contract'][1:]!=d['contract'][:-1])])
            for row in ts.loc[ts['product']==name].itertuples():
                i=int(np.searchsorted(d['start_ns'],row.entry_ns));j=int(np.searchsorted(d['end_ns'],row.exit_ns))
                m=d['mult'];tick=d['tick'];entryfee=max(5,row.entry*m*.00015)
                exitprice=row.entry+row.side*(row.pnl+entryfee+5)/m
                if exitprice*m*.00015>5:exitprice=(row.pnl+row.side*m*row.entry+entryfee)/(row.side*m-.00015*m)
                fee=entryfee+max(5,exitprice*m*.00015);slip=2*tick*m;distance=(row.risk-2*entryfee)/m-tick
                bars=pd.read_sql_query('select start_ns,open,high,low,close from bars where contract=? and period_seconds=60 and start_ns>=? and end_ns<=? order by start_ns',db,params=(str(d['contract'][i]),int(row.entry_ns),int(row.exit_ns)))
                mfe=max(0,float((np.max(bars.high)-row.entry) if row.side==1 else row.entry-np.min(bars.low)))/distance
                mae=max(0,float((row.entry-np.min(bars.low)) if row.side==1 else np.max(bars.high)-row.entry))/distance
                et=pd.Timestamp(row.entry_ns,tz='UTC').tz_convert('Asia/Shanghai');xt=pd.Timestamp(row.exit_ns,tz='UTC').tz_convert('Asia/Shanghai')
                x=dict(period=label,product=name,entry_ns=row.entry_ns,exit_ns=row.exit_ns,entry_time=str(et),exit_time=str(xt),side=row.side,pnl=row.pnl,risk=row.risk,reason=row.reason,bars=j-i+1,wall_hours=(row.exit_ns-row.entry_ns)/3.6e12,overnight=et.date()!=xt.date(),weekend=(xt-et).days>=2,fee=fee,slippage=slip,before_cost=row.pnl+fee+slip,R=row.pnl/row.risk,mfe_stop_units=mfe,mae_stop_units=mae,episode=int(episode[i-1]),fresh_cross=bool(i<2 or signal[i-2]!=signal[i-1]),atr_money=float(d['atr'][i]*m),stop_money=float(distance*m),cost_to_stop=(fee+slip)/(distance*m))
                history=db.execute('select start_ns from bars where contract=? and period_seconds=900 and start_ns<? and start_ns>=? order by start_ns desc limit 128',(str(d['contract'][i]),int(d['start_ns'][i-1]),int(pd.Timestamp(start,tz='Asia/Shanghai').value))).fetchall()
                x['lookback_calendar_days']=(int(d['end_ns'][i-1])-int(history[-1][0]))/864e11 if len(history)==128 else None
                for h in (4,16,32,64):
                    k=i+h-1
                    x[f'forward_{h}_atr']=(float((d['close'][k]-d['open'][i])*row.side/d['atr'][i]) if k<len(d['close']) and d['contract'][k]==d['contract'][i] else None)
                allrows.append(x)
        f=pd.DataFrame([r for r in allrows if r['period']==label]);positive=f[f.pnl>0];negative=f[f.pnl<0]
        f['episode_trade_index']=f.groupby(['product','episode']).entry_ns.rank(method='first')
        re=f[f.episode_trade_index>1]
        summary=dict(period=label,start=start,end_exclusive=end,months=12 if label=='research_year' else 6,trades=len(f),net=float(f.pnl.sum()),mean_trade=float(f.pnl.mean()),win_rate_pct=float((f.pnl>0).mean()*100),average_win=float(positive.pnl.mean()),average_loss=float(-negative.pnl.mean()),breakeven_win_rate_pct=float(-negative.pnl.mean()/(positive.pnl.mean()-negative.pnl.mean())*100),profit_factor=float(positive.pnl.sum()/-negative.pnl.sum()),gross_before_fees_slippage=float(f.before_cost.sum()),fees=float(f.fee.sum()),slippage=float(f.slippage.sum()),overnight_pct=float(f.overnight.mean()*100),median_wall_hours=float(f.wall_hours.median()),median_bars=float(f.bars.median()),target_rate_pct=float((f.reason=='target').mean()*100),stop_rate_pct=float((f.reason=='stop').mean()*100),time_rate_pct=float((f.reason=='time').mean()*100),stop_within4bars_pct=float(((f.reason=='stop')&(f.bars<=4)).mean()*100),median_mfe=float(f.mfe_stop_units.median()),mfe_ge2_pct=float((f.mfe_stop_units>=2).mean()*100),median_planned_risk=float(f.risk.median()),cost_to_stop_pct=float(f.cost_to_stop.mean()*100),fresh_cross_pct=float(f.fresh_cross.mean()*100),reentry_count=len(re),reentry_net=float(re.pnl.sum()),first_episode_net=float(f.loc[f.episode_trade_index==1,'pnl'].sum()),risk_gate=gate)
        summary['exit_attribution']=f.groupby('reason').agg(trades=('pnl','size'),net=('pnl','sum'),average=('pnl','mean')).reset_index().to_dict('records')
        summary['sides']=f.groupby('side').agg(trades=('pnl','size'),net=('pnl','sum'),win_rate=('pnl',lambda a:float((a>0).mean()))).reset_index().to_dict('records')
        summary['holding_buckets']=f.assign(bucket=pd.cut(f.bars,[0,4,16,32,64,128],include_lowest=True).astype(str)).groupby('bucket').agg(trades=('pnl','size'),net=('pnl','sum')).reset_index().to_dict('records')
        stopped=f[f.reason=='stop']
        summary.update(lookback_median_calendar_days=float(f.lookback_calendar_days.median()),winner_median_wall_hours=float(positive.wall_hours.median()),winner_overnight_pct=float(positive.overnight.mean()*100),overnight_net=float(f.loc[f.overnight,'pnl'].sum()),same_day_net=float(f.loc[~f.overnight,'pnl'].sum()),holding_more32_bars_net=float(f.loc[f.bars>32,'pnl'].sum()),holding_at_most32_bars_net=float(f.loc[f.bars<=32,'pnl'].sum()),stopped_then64bar_positive_pct=float((stopped.forward_64_atr.dropna()>0).mean()*100))
        summary['forward_atr']={str(h):dict(mean=float(f[f'forward_{h}_atr'].mean()),median=float(f[f'forward_{h}_atr'].median()),positive_pct=float((f[f'forward_{h}_atr'].dropna()>0).mean()*100)) for h in (4,16,32,64)}
        summaries.append(summary)
        for name,g in f.groupby('product'):
            productstats.append(dict(period=label,product=name,trades=len(g),net=float(g.pnl.sum()),win_rate_pct=float((g.pnl>0).mean()*100),target_rate_pct=float((g.reason=='target').mean()*100),before_cost=float(g.before_cost.sum()),median_holding_bars=float(g.bars.median()),overnight_pct=float(g.overnight.mean()*100)))
        print('DIAGNOSTIC',json.dumps(summary),flush=True)
    db.close()
    # Isolate the only warm-up difference: second half-year from a continuous prior-year feed.
    combined,p=prepare(start='2024-10-28',endday='2025-10-28',names=frozen['universe'],output_dir=dest/'warmup_control')
    raw={nm:trades_for(d,frozen['config']) for nm,d in combined.items()};warm,_,_=portfolio(combined,raw,list(range(6,12)))
    cold=next(s for s in summaries if s['period']=='prior_six')
    warmup=dict(cold_six_month_net=cold['net'],continuous_history_six_month_net=warm['net'],difference=warm['net']-cold['net'],cold_trades=cold['trades'],continuous_history_trades=warm['trades'],note='Same fixed rules and half-year capital reset. Only pre-start indicator history differs.')
    result=dict(periods=summaries,products=productstats,warmup_control=warmup,notes=['Diagnostic only; frozen rules not modified.','Forward returns overlap and are descriptive, not independent hypothesis tests.','MFE/MAE include entire exit minute, hence intraminute order ambiguity.','Removing fees/slippage adds costs back to the fixed executed ledger; it does not rerun risk-based admission or change signal timing.'])
    (dest/'results.json').write_text(json.dumps(result,indent=2),encoding='utf8');pd.DataFrame(allrows).to_csv(dest/'trade_diagnostics.csv',index=False);pd.DataFrame(productstats).to_csv(dest/'product_diagnostics.csv',index=False)
    report=['# 冻结策略跨时期失效诊断','','本次只诊断已冻结的四品种Z分数动量策略，没有修改或优化策略参数。早期：2024-10-28～2025-04-27；中期：2025-04-28～2025-10-27；研究年：2025-10-28～2026-10-27。','','## 时间尺度','','15分钟是采样频率。128根回看为32个交易小时，入场时实际回看跨度中位数约7.57个自然日；最多持仓128根，止损距离2ATR、止盈距离8ATR。它交易的是数日尺度的延续，并非只预测下一根15分钟涨跌。研究年65.67%的交易跨日，盈利交易86.30%跨日，盈利交易持有自然时间中位数60.85小时。','','研究年跨日交易净利润40,696.61元，同日开平交易净亏损11,998.91元；按事后持仓时长分组，超过32根K线的交易贡献41,079.34元，其余交易亏损12,381.64元。这是贡献归因，不能推断强行延长持仓或只做隔夜会取得这些结果。','','## 胜负分布变化','','| 指标 | 早期六个月 | 中期六个月 | 研究年 |','|---|---:|---:|---:|']
    for field,title,fmt in [('trades','交易数','d'),('win_rate_pct','胜率/%','.2f'),('average_win','平均盈利/元','.2f'),('average_loss','平均亏损绝对值/元','.2f'),('breakeven_win_rate_pct','按实绩盈亏比的盈亏平衡胜率/%','.2f'),('target_rate_pct','固定止盈触发率/%','.2f'),('stop_rate_pct','止损触发率/%','.2f'),('mean_trade','每笔净期望实绩/元','.2f'),('gross_before_fees_slippage','固定成交账本加回手续费和滑点/元','.2f')]:
        report.append('| '+title+' | '+' | '.join(format(s[field],fmt) for s in summaries)+' |')
    report+=['','实绩盈亏平衡胜率=平均亏损/(平均盈利+平均亏损)，是按样本平均盈亏计算的描述性指标，不是对未来概率的估计。固定4倍距离止盈不意味着实际平均盈亏比为4；到期平仓、换月/换合约平仓及成本都会改变平均盈亏。','','Z>2只表明价格相对过去均值偏高。跟随该偏离能否获利，取决于随后是否在先回撤2ATR之前延续到8ATR；该条件概率跨期明显变化。早期止盈触发15/137笔，中期16/142笔，研究年47/201笔。不能仅凭这些结果认定某个宏观事件或长期单边趋势是原因。','','## 月份切分与筛选偏差','','随机月份切分检验了同一年度内部分月份的迁移，未确保跨市场状态迁移。研究年自然月3～6月合计贡献全年净利润约76.25%，有利阶段同时出现在挖掘与验证部分。验证三个研究月块分别为−396.42、+4,987.45、+5,204.52元，验证利润主要来自两个相邻阶段的盈利月块。','','品种、策略族、止损、止盈、持仓、风险预算与组合宽度都参与筛选。最终风险扫描432个配置形成993个组合候选，27个候选参与验证；验证收益又用于最终选优，因此验证并非一次独立的资格检查。探索过程中最后三个月也被多次查看，不再是未触碰的最终测试集。邻近参数27组有效，只能支持该年度附近参数稳定，不能解决共同市场状态或共同品种偏差。','','选定后的历史回溯不等同于按时间前推检验；随机时间样本及反复调参会削弱验证估计的独立性。方法参考：[scikit-learn时间序列交叉验证说明](https://scikit-learn.org/stable/modules/cross_validation.html)、[Bailey等关于回测过拟合的原始论文](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf)。这两类文献支持一般风险，不能据此量化本策略的过拟合概率。','','## 风险门槛并非跨期等价','','| 时段 | 有信号K线中通过450元风险门槛且止损不少于两跳的比例 | 成交计划风险中位数/元 | 平均成本/初始止损金额 |','|---|---:|---:|---:|']
    for s in summaries:
        raw=sum(g['signal_bars'] for g in s['risk_gate']);passed=sum(g['risk_eligible_bars'] for g in s['risk_gate'])
        report.append(f"| {s['period']} | {passed/raw:.2%} | {s['median_planned_risk']:.2f} | {s['cost_to_stop_pct']:.2f}% |")
    report+=['','上述信号K线含持仓期间的信号，不能理解为独立可执行交易机会。它揭示固定人民币门槛在不同波动状态下过滤强度不同，而非已经证明该过滤造成全部收益差异。Z分数无量纲不等于整个策略完全具有尺度不变性：最小一手、450元预算、固定价位滑点及最低手续费仍然依赖价格和波动。','','## 排查过的替代解释','','- 数据：15分钟与原始一分钟OHLCV一致，三个时期一分钟复核没有改变逐笔净利润。早期有四根14分钟不完整聚合K线，影响两笔最终一分钟平仓精度，这一数据局限保留，不能当作完全无缺失样本。没有证据表明它可解释上万元亏损。',f"- 预热：中期六个月采用前六个月已有历史计算指标后，净利润从{warmup['cold_six_month_net']:.2f}元变为{warmup['continuous_history_six_month_net']:.2f}元，仅差{warmup['difference']:.2f}元，仍亏损。",'- 交易成本：早期把账本成本加回仍亏7,425元；中期加回后仅赚2,485元，弱毛利被5,524.83元费用和滑点消耗。研究年加回后赚37,155元。成本是中期净亏的重要组成，但不是早期失效的全部解释。','- 多空方向：早期多单净亏6,929.98元、空单净亏5,772.10元；研究年多单净赚19,330.85元、空单净赚9,366.85元。结果并非仅由做多方向遇到熊市所解释。','- 跳空：中期只有一笔实际亏损超过600元，不足以解释早期大量普通止损与低止盈率。','', '## 判断边界','','直接确认的机制是：同样的动量入场在较早时期较少兑现较远止盈，平均盈利也较小，净期望由正转负；固定风险门槛与成本进一步改变结果。研究流程的多重筛选、同年度随机切分与重复查看留出集，使最初的泛化证据偏弱。这些因素可能同时存在，目前不能把亏损严格分解为“市场状态占多少、过拟合占多少”。','','下一步若继续研究，应先明确目标是日内短周期优势还是允许跨日的数日趋势优势；用预先固定规则做按时间的外层验证，并把品种选择和调参限制在内层。已经查看过的两个六个月区间可以用于诊断，但一旦据此修改规则，它们便不能再次作为独立样本外证据。']
    (dest/'REPORT.md').write_text('\n'.join(report),encoding='utf8')
    print('WARMUP',json.dumps(warmup),flush=True)

if __name__=='__main__':main()
