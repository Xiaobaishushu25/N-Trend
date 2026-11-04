"""Trading-day marked monthly/daily PnL and full-year product attribution.

No parameter changes or simulations. All inputs are prior saved account results.
"""
import bisect
import json
from pathlib import Path
import sqlite3

import numpy as np
import pandas as pd

import research_swing_year_20261104 as b

ROOT=b.ROOT
OUT=ROOT/'outputs/swing-attribution-20261104'
RUN=ROOT/'reports/swing-growth-20261104'
ARCHIVE=ROOT/'research-archives/swing-ab-complete-20261104-v1/reports/swing-expansion-20261104'
NAMES={'BU':'沥青','C':'玉米','CF':'棉花','CJ':'红枣','EB':'苯乙烯','EG':'乙二醇','FG':'玻璃','JD':'鸡蛋','L':'塑料','MA':'甲醇','PB':'沪铅','PP':'聚丙烯','RB':'螺纹钢','SA':'纯碱','SF':'硅铁','SH':'烧碱','SI':'工业硅','SM':'锰硅','SR':'白糖','SS':'不锈钢','UR':'尿素','V':'PVC'}


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    conn=sqlite3.connect(f'file:{b.DB.as_posix()}?mode=ro',uri=True)
    calendar=[r[0] for r in conn.execute("SELECT DISTINCT date(start_ns/1000000000,'unixepoch','+8 hours') FROM bars WHERE period_seconds=3600 AND cast(strftime('%H',start_ns/1000000000,'unixepoch','+8 hours') AS int) BETWEEN 9 AND 14 ORDER BY 1")];conn.close()
    days=[day for day in calendar if b.START<=day<b.END]
    configs=[('A均衡','主要观察','A_scale_750_m60',RUN/'minute-A_scale_750_m60-base-equity.csv',RUN/'minute-A_scale_750_m60-base-trades.csv','1分钟完整账户'),('A增长','主要观察','A_scale_1250_m80',RUN/'minute-A_scale_1250_m80-base-equity.csv',RUN/'minute-A_scale_1250_m80-base-trades.csv','1分钟完整账户'),('A盈利加仓','次级观察','A_pyramid',RUN/'minute-A_pyramid-base-equity.csv',RUN/'minute-A_pyramid-base-trades.csv','1分钟完整账户'),('B成本改良','次级观察','B_target2_guard3',RUN/'minute-B_target2_guard3-base-equity.csv',RUN/'minute-B_target2_guard3-base-trades.csv','1分钟完整账户'),('原A一手','归档基线','pullback_end_60_32_target3_r250',ARCHIVE/'audit-pullback_end_60_32_target3_r250-margin20-equity.csv',ARCHIVE/'audit-pullback_end_60_32_target3_r250-margin20-trades.csv','15分钟完整账户'),('原B一手','归档基线','oi_drive_15_32_target3_r500',ARCHIVE/'audit-oi_drive_15_32_target3_r500-margin20-equity.csv',ARCHIVE/'audit-oi_drive_15_32_target3_r500-margin20-trades.csv','15分钟完整账户')]
    daily=pd.DataFrame(index=pd.Index(days,name='交易日'));products=pd.DataFrame(index=pd.Index(b.SPECS,name='品种'));trade_counts=products.copy();daily_realized=daily.copy()
    trade_detail=[];summary={};top_rows=[];bottom_rows=[];meta=[]
    for label,role,key,eqpath,tspath,resolution in configs:
        eq=pd.read_csv(eqpath,index_col=0).iloc[:,0]
        ts=pd.read_csv(tspath)
        times=pd.to_datetime(eq.index.astype('int64'),utc=True).tz_convert('Asia/Shanghai')
        assigned=[]
        for t in times:
            day=t.strftime('%Y-%m-%d')
            i=bisect.bisect_right(calendar,day) if t.hour>=18 else bisect.bisect_left(calendar,day)
            assigned.append(calendar[i] if i<len(calendar) else day)
        close=pd.Series(eq.values,index=assigned).groupby(level=0,sort=True).last().reindex(days).ffill().fillna(0.)
        dp=close.diff();dp.iloc[0]=close.iloc[0];daily[label]=dp
        realized=ts.groupby('exit_day').net.sum().reindex(days,fill_value=0.);daily_realized[label]=realized
        prod=ts.groupby('symbol').net.sum().reindex(b.SPECS,fill_value=0.);products[label]=prod;trade_counts[label]=ts.groupby('symbol').size().reindex(b.SPECS,fill_value=0)
        assert abs(dp.sum()-ts.net.sum())<1e-6
        assert abs(prod.sum()-eq.iloc[-1])<1e-6
        positive=dp[dp>1e-8].sort_values(ascending=False);negative=dp[dp< -1e-8].sort_values();pp=prod[prod>0].sort_values(ascending=False)
        net=float(dp.sum());gross_up=float(positive.sum());gross_prod=float(pp.sum())
        dd=float(np.max(np.maximum.accumulate(np.r_[0,close.values])-np.r_[0,close.values]))
        realized_positive=realized[realized>0].sort_values(ascending=False)
        stats=dict(net=net,return_on_initial=net/50000,max_daily_close_drawdown=dd,campaigns=len(ts),trading_days=len(days),positive_days=int((dp>1e-8).sum()),negative_days=int((dp< -1e-8).sum()),zero_days=int((dp.abs()<=1e-8).sum()),gross_positive_daily_pnl=gross_up,top1_day=dict(day=positive.index[0],pnl=float(positive.iloc[0])),worst_day=dict(day=negative.index[0],pnl=float(negative.iloc[0])),top5_days_pnl=float(positive.iloc[:5].sum()),top10_days_pnl=float(positive.iloc[:10].sum()),top5_days_share_positive=float(positive.iloc[:5].sum()/gross_up),top10_days_share_positive=float(positive.iloc[:10].sum()/gross_up),net_without_best_day=net-float(positive.iloc[0]),net_without_best5_days=net-float(positive.iloc[:5].sum()),net_without_best10_days=net-float(positive.iloc[:10].sum()),top_product=pp.index[0],top_product_pnl=float(pp.iloc[0]),top3_products=pp.index[:3].tolist(),top3_products_pnl=float(pp.iloc[:3].sum()),top3_products_share_positive=float(pp.iloc[:3].sum()/gross_prod),net_without_top_product=net-float(pp.iloc[0]),net_without_top3_products=net-float(pp.iloc[:3].sum()),hhi_positive_products=float(((pp/gross_prod)**2).sum()),top5_realized_close_days_share_positive=float(realized_positive.iloc[:5].sum()/realized_positive.sum()))
        assert all(trade_counts.loc[s,label]==0 for s in b.SPECS if abs(prod[s])<1e-8)
        summary[label]=stats
        for rank,(day,val) in enumerate(positive.iloc[:10].items(),1):top_rows.append([label,rank,day,float(val),float(val/gross_up),float(val/net)])
        for rank,(day,val) in enumerate(negative.iloc[:10].items(),1):bottom_rows.append([label,rank,day,float(val)])
        for row in ts.to_dict('records'):
            trade_detail.append([label,row['symbol'],row['entry_day'],row['exit_day'],int(row['lots']),float(row['net']),row['reason'],row['contract']])
        meta.append(dict(label=label,role=role,id=key,resolution=resolution,equity_source=str(eqpath.relative_to(ROOT)),trade_source=str(tspath.relative_to(ROOT))))
    monthly=daily.groupby(daily.index.str[:7]).sum()
    product_names=pd.DataFrame({'代码':products.index,'品种':[NAMES[s] for s in products.index]})
    ptable=product_names.copy()
    for label in products.columns:ptable[label]=products[label].values
    ds=daily.copy();ds.insert(0,'月份',daily.index.str[:7]);ds=ds.reset_index()
    realized_detail=daily_realized.reset_index()
    payload=dict(period=[b.START,'2026-10-27'],capital=50000,margin=.2,metadata=meta,schemes=list(daily.columns),monthly=dict(headers=['月份']+list(monthly.columns),rows=[[month]+[float(v) for v in row] for month,row in monthly.iterrows()]),products=dict(headers=list(ptable.columns),rows=ptable.values.tolist()),daily=dict(headers=list(ds.columns),rows=ds.values.tolist()),realized_daily=dict(headers=list(realized_detail.columns),rows=realized_detail.values.tolist()),trades=dict(headers=['方案','品种','入场交易日','平仓交易日','手数','净盈亏','退出原因','真实合约'],rows=trade_detail),top_days=dict(headers=['方案','排名','交易日','权益增加','占盈利日正收益','占全年净收益'],rows=top_rows),worst_days=dict(headers=['方案','排名','交易日','权益变动'],rows=bottom_rows),summary=summary,trade_counts=dict(headers=['代码']+list(trade_counts.columns),rows=[[s]+[int(v) for v in row] for s,row in trade_counts.iterrows()]))
    (OUT/'attribution.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf8')
    monthly.to_csv(OUT/'monthly.csv',encoding='utf-8-sig');ptable.to_csv(OUT/'product.csv',index=False,encoding='utf-8-sig');ds.to_csv(OUT/'daily-marked.csv',index=False,encoding='utf-8-sig')
    selected=list(daily.columns[:4])
    def table(headers,rows):return '|'+ '|'.join(headers)+'|\n|'+ '|'.join(['---']+['---:']*(len(headers)-1))+'|\n'+'\n'.join('|'+ '|'.join(str(v) if isinstance(v,str) else f'{v:,.2f}' for v in row)+'|' for row in rows)
    month_rows=[[month]+[float(row[k]) for k in selected] for month,row in monthly.iterrows()]+[['合计']+[summary[k]['net'] for k in selected]]
    active=[s for s,row in products.iterrows() if any(abs(row[k])>1e-8 for k in selected)]
    prod_rows=[[NAMES[s]+' '+s]+[float(products.loc[s,k]) for k in selected] for s in active]+[['合计']+[summary[k]['net'] for k in selected]]
    concentration=[]
    for label in selected:
        z=summary[label]
        concentration.append([label,z['top5_days_share_positive']*100,z['top10_days_share_positive']*100,z['net_without_best5_days'],z['net_without_best10_days'],z['top3_products_share_positive']*100,z['net_without_top3_products']])
    report='''# 波段候选逐月、逐品种与盈利日期集中度

第二轮A/B没有被替代。A均衡、A增长属于原A信号的不同资金配置；它们高度重合，不能当两套独立策略分散风险。当前主要观察A均衡和A增长；A盈利加仓、B成本改良为次级候选；第二轮原A一手、原B一手作为已归档基线。原B没有被批准为与A同等优先的主方案，其成本压力曾失败。

范围：2025-10-28—2026-10-27，初始5万元，统一20%保证金假设、每边1跳、冻结手续费。前四方案为一分钟完整账户结果；Excel中附原A/B的20%保证金15分钟账户参照，执行粒度不同，不能把差异全部归因于策略。

## 逐月盈亏（元）

按推断交易日的最后收盘权益变动统计，包含已实现盈亏、浮动盈亏和模型费用。夜盘归后续日盘交易日；2025年10月只有28—31日，2026年10月只到27日。显示分项四舍五入可能有几分钱尾差。

'''+table(['月份']+selected,month_rows)+'''

## 逐品种全年净盈亏（元）

样本末端全部平仓，所以逐品种平仓净额与全年权益变动总额一致。表中只列至少一个方案有盈亏的品种；其他10个品种均未产生交易。完整22品种及交易次数在Excel中。

'''+table(['品种']+selected,prod_rows)+'''

## 是否依赖少数几天或品种

“前5/10盈利日占比”的分母为所有盈利交易日权益增加之和；“前三品种占比”的分母为全部正贡献品种净收益之和，避免被亏损分母误导。剔除日/品种只作算术敏感性，不是禁止交易后重新回测，不能直接解释为可实现的新策略收益。

'''+table(['方案','前5日正收益占比%','前10日正收益占比%','去掉前5日后净额','去掉前10日后净额','前三品种正收益占比%','去掉前三品种后净额'],concentration)+'''

每天已实现平仓利润也单独保留在Excel，供区分“几天集中平仓”与“几天创造了价格收益”。波段交易的盈利通常随持仓多日积累，只看平仓日会夸大利润日期集中度。所有日集中度均采用账户日末浮动权益变动。

具体判断：A均衡最好的10个交易日合计占全年净利润约80.6%，A增长约89.6%；两者去掉这些日期后仍盈利，但余下净额显著降低，不能说对大行情日依赖很低。A增长最大盈利日为2026-03-09，权益增加2,752.80元；前5日合计9,952.80元，占全年净利润52.5%。A增长2025年11月净额7,115.34元，约占全年37.6%。

A均衡和增长的前三盈利品种均为玉米、PVC和白糖，分别占全年净利润约62.3%和62.4%。B成本改良的前三为塑料、聚丙烯、乙二醇，同属化工，占全年净利润85.3%；去掉最好10个交易日后净额转为−550.43元。就本轮分布而言，B成本改良比A两档更依赖少数盈利日和化工品种。

## 进一步判断的边界

A均衡/增长共享原A信号，增加手数不会产生更多独立交易样本。即使剔除最好几天后仍为正，也不证明未来不依赖大行情；反之，趋势策略依赖一部分大行情也不自动意味着无效。应结合剩余收益、月份分布、品种分布和成本压力一起判断。所有数据均为历史回测，没有加入新行情验证。

Excel含当前4方案与原A/B参照的月度、22品种、242个交易日浮动权益盈亏、已实现日盈亏、前10盈利/亏损日和逐笔交易。总额均与此前保存的权益终值核对，未改变任何策略参数。
'''
    (OUT/'REPORT.md').write_text(report,encoding='utf8')
    print(json.dumps({k:{a:z[a] for a in ['net','positive_days','negative_days','top1_day','top5_days_share_positive','top10_days_share_positive','net_without_best5_days','net_without_best10_days','top3_products','top3_products_share_positive','net_without_top3_products']} for k,z in summary.items()},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
