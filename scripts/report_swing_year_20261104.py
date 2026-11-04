"""Reconcile research artifacts and render the final offline study report."""
import hashlib
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import research_swing_year_20261104 as s

ROOT=s.ROOT
OUT=ROOT/'reports/swing-year-20261104'
RUNS=[('15分钟趋势波段','swing-year-20261104'),('日线多日波段','swing-daily-20261104'),('15分钟区间反转','swing-range-20261104')]


def read(p):return json.loads(p.read_text(encoding='utf8'))


def main():
    fees=read(s.FEES)['products'];audits=[];rows=[];comparisons=[]
    fig,ax=plt.subplots(figsize=(11,5))
    for label,run in RUNS:
        folder=ROOT/'reports'/run;summary=read(folder/'account-summary.json');comp=read(folder/'comparison.json')
        comparisons.append((label,comp))
        for slip,sc in summary.items():
            eq=pd.read_csv(folder/f'account-equity-slip{slip}.csv',index_col=0).iloc[:,0]
            trades=pd.read_csv(folder/f'account-trades-slip{slip}.csv')
            assert abs(sum(trades.net*trades.lots)-eq.iloc[-1])<1e-6
            assert abs(eq.iloc[-1]-sc['full']['net'])<.011
            assert abs(sum(sc[k]['net'] for k in ['train','validation','test'])-sc['full']['net'])<.021
            assert abs(sum(sc['full']['monthly'].values())-sc['full']['net'])<.06
            max_error=0.
            for t in trades.to_dict('records'):
                sym=t['symbol'];mult=s.SPECS[sym][0]
                net=t['side']*(t['exit_price']-t['entry_price'])*mult-t['entry_fee']-s.fee(fees[sym],t['exit_price'],mult,'close_today' if t['entry_day']==t['exit_day'] else 'close_yesterday')
                max_error=max(max_error,abs(net-t['net']))
                assert abs(net-t['net'])<1e-6
                assert t['lots']>=1 and float(t['lots']).is_integer()
                assert t['lots']*t['risk_cash']<=sc['limits']['risk_per_trade']+1e-6
                assert t['exit_ns']>t['entry_ns']
            audits.append(dict(run=run,slip=int(slip),trades=len(trades),ledger_reconciled=True,maximum_fill_pnl_error=max_error))
        b=summary['1'];f=b['full']
        rows.append(f"|{label}|{f['net']:+,.2f}|{f['net']/500:.2f}%|{f['max_drawdown']:,.2f}|{f['max_drawdown']/500:.2f}%|{f['trades']}|{'通过' if comp['passed_development_gate'] else '未通过'}|")
        eq=pd.read_csv(folder/'account-equity-slip1.csv',index_col=0).iloc[:,0]
        times=pd.to_datetime(eq.index.astype('int64'),utc=True).tz_convert('Asia/Shanghai')
        names={'swing-year-20261104':'15m trend control','swing-daily-20261104':'Daily swing control','swing-range-20261104':'15m range control'}
        ax.plot(times,50000+eq.values,label=names[run],linewidth=1.25)
    ax.axhline(42500,color='#c43b3b',ls='--',lw=1,label='15% loss from initial capital (not drawdown stop)')
    ax.set(title='Chronologically selected exploratory controls | Initial capital CNY 50,000',ylabel='Close-marked account equity (CNY)')
    ax.legend(fontsize=8);ax.grid(alpha=.2);fig.autofmt_xdate();fig.tight_layout();fig.savefig(OUT/'equity-comparison.png',dpi=170);plt.close(fig)
    day=read(ROOT/'reports/swing-daily-20261104/account-summary.json')
    chosen=read(ROOT/'reports/swing-daily-20261104/comparison.json')['selected']
    d=day['1'];parts=[]
    for key,title in [('train','开发段：2025-10-28—2026-04-27'),('validation','验证段：2026-04-28—2026-07-27'),('test','最后测试段：2026-07-28—2026-10-27')]:
        x=d[key];parts.append(f"|{title}|{x['net']:+,.2f}|{x['trades']}|{x['max_drawdown']:,.2f}|")
    costrows=[]
    for slip,v in day.items():
        costrows.append(f"|每边{slip}跳|{v['full']['net']:+,.2f}|{v['full']['max_drawdown']:,.2f}|{v['test']['net']:+,.2f}|")
    report='''# 近一年波段策略研究（2026-11-04）

**结论：本轮没有找到满足“5万元本金、15%回撤目标、分段持续正收益”的合格策略。36套规则均未通过各自预先记录的开发/验证筛选门槛。不能把全年微利的候选称为稳定增长策略。**

## 范围和口径

- 数据：本地 `data/research_market_history.sqlite`；最新到2026-10-27 15:00。研究交易日2025-10-28至2026-10-27。22品种各覆盖242个推断交易日；预热从2025-09-01开始。未使用另两年的收益选参。
- 本金50,000元；目标回撤不超过7,500元。它是筛选目标，不是跳空风险保证或自动熔断。
- 三个阶段各12套规则：15分钟突破/顺势回调、日线突破/顺势回调、15分钟假突破/均值回归。后两阶段分别比较250/500元单笔计划价格风险预算。追加家族是看过前阶段失败后的探索，增加多重试验风险。
- 第一阶段先用每笔250元价格风险的分数仓位筛选，再对选中对照验证整数手；后两阶段直接在整数手账户上筛选。各阶段没有通过筛选时，只保留按训练期收益/回撤排序的失败对照用于诊断。
- 所有品种保留，不按全年盈利品种事后挑选。信号用已闭合K线；下一根开盘入场；15分钟信号跨休市则取消。日线信号允许下一交易日开盘。
- 每个真实月份合约独立计算指标；主力映射滞后一个已观察交易日，避免依赖当天主力结果。切换时在旧合约真实开盘价平仓，不把合约价差当收益。
- 夜盘按后续已观察日盘推断交易日；未完成官方交易日历及主力切换逐笔审计。长期使用滞后主力的交易性仍需核验。
- 止损计不利滑点；跳空按止损和开盘两者中更差的价格；移动止损在当根收盘更新、下一根才生效。不设固定止盈。每边1跳为基准，2/3跳为压力。
- 手续费沿用仓库2026-10-22冻结快照（含既有SR往返2元覆盖值），不是重建的历史券商账单；保证金假定名义金额15%，总保证金入场上限权益40%。
- 账户最多4个持仓、同大类最多2个、初始价格风险之和不超过初始本金2%；单笔风险预算不含手续费和跳空超损。不加仓、不复利放大风险预算。
- 回撤为K线收盘浮动权益口径，不是逐分钟或盘中最坏回撤；保证金比例没有每日历史核验，也没有涨跌停无法成交模型。**下表的较小回撤不能据此证明实际回撤一定低于15%。**
- 历史已经被此前项目研究反复观察；最后三个月仅是本轮按时间留后的检验，不能称为完全未接触的独立样本。

## 资金约束后的阶段对照

全部金额为人民币，已扣模型手续费及每边1跳滑点。回撤百分比以初始5万元为分母，非动态峰值比例；不是只统计已平仓现金。

|阶段失败对照|全年净额|占初始本金|收盘权益最大回撤|占初始本金|交易数|开发/验证门槛|
|---|---:|---:|---:|---:|---:|---|
'''+ '\n'.join(rows)+'''

![各阶段失败对照账户权益](equity-comparison.png)

图中水平线为初始本金亏损15%的位置，与动态峰值回撤线含义不同；未把它实现为熔断机制。

## 相对更平缓的日线对照：仍未合格

日线40周期顺势回调、2倍ATR移动止损、每笔计划价格风险最多500元。只作为进一步研究对照，不是实盘推荐。

- 日线按推断交易日合并真实合约15分钟K线，包含此前夜盘。ATR14取TR简单均值。
- 慢EMA40、快EMA10，EMA以首价递推且至少40根预热。做多：EMA10>EMA40，当日最低价触及EMA10，收盘重回其上且收阳；做空镜像。
- 信号后下一交易日开盘加不利滑点入场。初始止损距离为2ATR14向上按tick取整，至少4tick；整数手数按500元风险与保证金双约束计算。一手超预算就跳过。
- 多头收盘后止损提高至 `max(原止损, 收盘-向上取整的2ATR14)`；空头镜像。只对后续K线生效。
- 多头收盘跌破此前20日最低价、空头收盘突破此前20日最高价，则下一交易日开盘退出；最多持有40根交易日K线。主力切换及样本末端强制平仓。
- 本次仅交易16笔，实际集中在玉米、螺纹钢、PVC，持仓自然时间中位数198小时（约8.25天）；258个独立机会因整数手风险/保证金预算被拒绝，无法实现理论上的22品种分散。

|时间段|净权益变动|当段平仓数|当段收盘权益回撤|
|---|---:|---:|---:|
'''+ '\n'.join(parts)+'''

跨段持仓按期末浮动权益计入各段，所以分段权益变动不等于该段平仓单净额。三段净额可核对至全年净额。

|日线对照成交压力|全年净额|收盘权益回撤|最后测试段净额|
|---|---:|---:|---:|
'''+ '\n'.join(costrows)+'''

全年基准盈利434.19元（0.87%），验证和最后测试段连续为负；3跳压力下全年转亏。开发段仅5笔平仓，未达到至少10笔门槛。没有足够证据支持稳定正期望。

## 研究决策

本轮不形成合格实盘策略。日线顺势回调保留为失败研究对照；5万元资金下，每笔250元风险会使本批日线参数全部无法开出整数手，放宽至500元仍主要只剩低波动、低名义金额合约。需要的是真实可执行的收益优势，而不仅是延长周期后曲线变平。

后续若继续研究，应事先固定新的假设与小范围参数，再使用新增行情做前向验证。反复按最后三个月表现改规则会消耗其检验价值。旧两年数据可以做后续失效检验，但本轮遵照请求未使用它们补选一个历史赢家。

## 复现与核验

```powershell
python scripts/research_swing_year_20261104.py
python scripts/research_swing_daily_20261104.py
python scripts/research_swing_range_20261104.py
python -m unittest discover -s tests -p test_swing_year_20261104.py -v
python scripts/report_swing_year_20261104.py
```

三个输出目录各保留 `protocol.json`、`comparison.json`、`data-audit.json`、`implementation.json`、三个滑点场景的逐笔交易和收盘浮动权益CSV。数据哈希基于固定排序的读取行，数据库只读；未改动生产策略或启用交易。

核验：5个合成执行测试通过，覆盖止损不回溯、跳空滑点、旧合约换月、整数手预算和空白月份统计；9组账户逐笔成交扣费净额与权益终值一致，分段及月度权益变动核对一致。此类核验验证程序约定，并不证明模型成交假设等于实际市场。

方法参考：[CME合约到期和换月说明](https://www.cmegroup.com/education/courses/understanding-the-benefits-of-futures/managing-contract-expiration)。本报告收益全部来自本地离线研究，不取自外部网页。
'''
    (OUT/'REPORT.md').write_text(report,encoding='utf8')
    s.OUT=OUT;s.save('reconciliation.json',audits)
    print('Report written; 9 scenarios reconciled.')


if __name__=='__main__':main()
